"""Detached, checkpointed execution. Restarting the owner never repeats admission."""
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from storage import locked, read, save

_children = {}


def identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {"pid": pid, "ticks": stat[19],
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(owner):
    return bool(owner) and identity(owner["pid"]) == owner


def terminate(owner):
    # Only the exact process session we started may be signalled.
    if not owner or owner["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        return
    pid = owner["pid"]
    current = identity(pid)
    if current and current != owner:
        return
    try:
        if current and os.getpgid(pid) != pid:
            raise RuntimeError("Refusing to terminate an unowned process group")
        os.killpg(pid, signal.SIGTERM)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            os.killpg(pid, 0)
            if identity(pid) not in (None, owner):
                return
            time.sleep(0.05)
        if identity(pid) in (None, owner):
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def epoch(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def native_terminal(runs, started):
    if not runs:
        return None
    for path in sorted(Path(runs).glob("*/end-state.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            value = read(path)
            if isinstance(value, dict) and epoch(value.get("endedAt")) >= started:
                return value
        except (ValueError, OSError):
            continue
    for path in Path(runs).glob("*/gnhf.log"):
        aborted = False
        # Resume appends to this file. Its mtime cannot identify a new abort.
        with path.open() as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or epoch(event.get("timestamp")) < started:
                    continue
                if event.get("event") == "orchestrator:abort":
                    aborted = True
                if aborted and event.get("event") == "orchestrator:end":
                    return {"status": "aborted", "endedAt": event["timestamp"],
                            "source": "native_terminal_events"}
    return None


def token_usage(paths, offsets=None):
    total = 0
    for path in paths:
        try:
            with path.open() as handle:
                handle.seek((offsets or {}).get(str(path), 0))
                for line in handle:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and event.get("type") == "turn.completed":
                            usage = event["usage"]
                            # Codex input_tokens already includes cached input.
                            total += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            continue
    return total


def start_operation(folder, command, cwd, allowance, *, native_runs=None, prompt=None,
                    timeout=3600, environment=None):
    folder = Path(folder).absolute()
    request = {"command": command, "cwd": str(cwd), "allowance": allowance,
               "native_runs": str(native_runs) if native_runs else None, "prompt": prompt,
               "timeout": timeout, "environment": environment or {}}
    with locked(folder / "admission.lock"):
        if (folder / "request.json").exists():
            if read(folder / "request.json") != request:
                raise ValueError("Admitted execution settings changed")
            return
        save(folder / "request.json", request)
        # Admission precedes invocation. A crash in this narrow gap is ambiguous
        # and blocks reconciliation rather than risking a duplicate model call.
        with (folder / "worker.log").open("ab") as log:
            _children[str(folder)] = subprocess.Popen(
                [sys.executable, str(Path(__file__).absolute()), str(folder)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, env=os.environ.copy())


def wait_operation(folder, stop=None):
    folder = Path(folder)
    while not (folder / "result.json").exists():
        if stop and stop.exists():
            save(folder / "stop.json", {"operator": True})
        worker = folder / "worker.json"
        if worker.exists() and not alive(read(worker)["identity"]):
            # Allow a result rename racing the last liveness check.
            if (folder / "result.json").exists():
                break
            terminate(read(worker).get("child"))
            raise RuntimeError("Worker disappeared; reconcile saved output before any new admission")
        if not worker.exists() and time.time() - (folder / "request.json").stat().st_mtime > 5:
            raise RuntimeError("Ambiguous admission; no automatic replay is allowed")
        time.sleep(0.1)
    child = _children.pop(str(folder), None)
    if child:
        child.wait(timeout=5)
    return read(folder / "result.json")


def run_worker(folder):
    os.umask(0o077)
    with locked(folder / "worker.lock", nonblocking=True):
        if (folder / "result.json").exists():
            return
        request = read(folder / "request.json")
        owner = {"identity": identity(os.getpid())}
        save(folder / "worker.json", owner)
        runs = Path(request["native_runs"]) if request["native_runs"] else None
        offsets = {str(p): p.stat().st_size for p in runs.glob("*/iteration-*.jsonl")} if runs else {}
        started = time.time()
        output = folder / "stdout.jsonl"
        terminal = None
        reason = "exit"
        env = {**os.environ, **request["environment"], "GNHF_TOKEN_ALLOWANCE": str(request["allowance"])}
        with output.open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
            child = subprocess.Popen(request["command"], cwd=request["cwd"], env=env,
                                     stdin=subprocess.PIPE if request["prompt"] is not None else subprocess.DEVNULL,
                                     stdout=stdout, stderr=stderr, start_new_session=True)
            owner["child"] = identity(child.pid)
            save(folder / "worker.json", owner)
            def send_prompt():
                try:
                    child.stdin.write(request["prompt"].encode())
                    child.stdin.close()
                except BrokenPipeError:
                    pass
            if child.stdin:
                threading.Thread(target=send_prompt, daemon=True).start()
            while child.poll() is None:
                terminal = native_terminal(runs, started)
                paths = list(runs.glob("*/iteration-*.jsonl")) if runs else [output]
                used = token_usage(paths, offsets)
                if (folder / "stop.json").exists():
                    reason = "operator_stop"
                elif used >= request["allowance"]:
                    reason = "token_limit"
                elif time.time() - started >= request["timeout"]:
                    reason = "timeout"
                elif terminal and terminal.get("status") == "aborted":
                    reason = "native_terminal"
                else:
                    time.sleep(0.1)
                    continue
                terminate(owner["child"])
                break
            code = child.wait()
            terminate(owner["child"])
        terminal = terminal or native_terminal(runs, started)
        paths = list(runs.glob("*/iteration-*.jsonl")) if runs else [output]
        save(folder / "result.json", {"exit_code": code, "reason": reason, "terminal": terminal,
                                     "tokens": token_usage(paths, offsets)})


if __name__ == "__main__":
    run_worker(Path(sys.argv[1]))
