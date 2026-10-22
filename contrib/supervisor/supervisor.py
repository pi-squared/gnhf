"""Opt-in Linux outer automation for a GNHF run, not part of its native loop."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from processes import start_operation, wait_operation
from storage import Budget, digest, locked, read, save


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_file(root, name):
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts):
        raise ValueError("Asset names must be relative paths without parent traversal")
    target = root / path
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError("Symlink assets are not allowed")
    return target


class Supervisor:
    def __init__(self, config_path):
        if sys.platform != "linux":
            raise RuntimeError("The optional supervisor requires Linux; native GNHF is unchanged")
        os.umask(0o077)
        self.config_path = Path(config_path).absolute()
        self.config = read(config_path)
        self.root = Path(self.config["state_dir"]).absolute()
        self.project = Path(self.config["project_dir"]).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.root / "lifecycle.json"
        self.stop = self.root / "stop.json"
        self.budget = Budget(self.root / "budget.json", self.config["total_tokens"], self.config["repair_tokens"])

    def checkpoint(self, state, status, **details):
        state.update(state=status, **details)
        save(self.state_path, state)

    def command(self, key, candidate=None, allowance=None):
        values = {"project": str(self.project), "state_dir": str(self.root),
                  "candidate": str(candidate or ""), "trusted": str(self.root / "trusted"),
                  "max_tokens": str(allowance or "")}
        command = self.config[key]
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            raise ValueError(f"{key} must be a nonempty argv list")
        # Replace only our placeholders: Python snippets and JSON may have braces.
        for key, value in values.items():
            command = [arg.replace("{" + key + "}", value) for arg in command]
        return command

    def freeze(self):
        if not self.config.get("prepared", True):
            raise ValueError("Configure the scaffold's project hooks and set prepared=true before starting")
        binding = digest(self.config)
        path = self.root / "config-binding.json"
        if path.exists():
            if read(path)["sha256"] != binding:
                raise ValueError("Supervisor configuration changed; retain the old run for reconciliation")
        else:
            save(path, {"sha256": binding})
        if "gnhf_config_file" in self.config:
            directory = self.root / "gnhf-config"
            directory.mkdir(exist_ok=True, mode=0o700)
            source = safe_file(self.project, self.config["gnhf_config_file"])
            destination = directory / "config.yml"
            if not destination.exists():
                shutil.copyfile(source, destination)
            elif sha(source) != sha(destination):
                raise ValueError("Protected GNHF config changed")
        trusted = self.root / "trusted"
        trusted.mkdir(exist_ok=True, mode=0o700)
        manifest_path = trusted / "manifest.json"
        if not manifest_path.exists():
            manifest = {}
            for name, source in self.config.get("trusted_files", {}).items():
                destination = safe_file(trusted, name)
                source = safe_file(self.project, source)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, destination)
                manifest[name] = sha(destination)
            save(manifest_path, manifest)
        for name, expected in read(manifest_path).items():
            if sha(safe_file(trusted, name)) != expected:
                raise ValueError("Frozen project verifier changed")

    def operation(self, state, key, pool, command, cwd, *, candidate=None, native_runs=None, prompt=None):
        operation = self.root / "operations" / key
        admission = operation / "allowance.json"
        if admission.exists():
            allowance = read(admission)["tokens"]
        else:
            allowance = min(self.config[pool + "_attempt_tokens"], self.budget.available(pool))
            save(admission, {"tokens": allowance})
        self.budget.reserve(key, pool, allowance)
        start_operation(operation, command(allowance), cwd, allowance,
                        prompt=prompt, native_runs=native_runs,
                        timeout=self.config.get("timeout_seconds", 3600),
                        environment={"GNHF_CONFIG_DIR": str(self.root / "gnhf-config"), "GNHF_TELEMETRY": "0",
                                     "GNHF_SUPERVISOR_CONFIG": str(self.config_path)})
        result = wait_operation(operation, self.stop)
        self.budget.settle(key, result["tokens"])
        return result

    def project_check(self, key, candidate=None):
        command = self.command(key, candidate)
        if candidate:
            # Candidate code never runs in the supervisor interpreter, with write
            # access to its private state, or with a network interface.
            command = ["bwrap", "--unshare-net", "--ro-bind", "/", "/",
                       "--proc", "/proc", "--dev", "/dev", "--chdir", str(candidate), "--", *command]
        result = subprocess.run(command, cwd=candidate or self.project,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, timeout=self.config.get("verification_timeout_seconds", 120))
        # Hook output may contain project data. Retain it privately, never put it
        # in the repair prompt or automatically publish it.
        folder = self.root / "verification"
        folder.mkdir(exist_ok=True, mode=0o700)
        (folder / (key + ".log")).write_bytes(result.stdout + result.stderr)
        return result

    def prepare(self, state):
        candidate = self.root / "incidents" / str(state["attempt"]) / "candidate"
        manifest = candidate.parent / "manifest.json"
        if manifest.exists():
            return candidate
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        originals = {}
        for name, source in self.config["repair_files"].items():
            target = safe_file(candidate, name)
            source_path = safe_file(self.project, source)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source_path, target)
            originals[name] = sha(target)
        protected = {}
        for name, source in self.config.get("fixture_files", {}).items():
            if name in originals:
                raise ValueError("Fixture cannot also be editable")
            target = safe_file(candidate, name)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(safe_file(self.project, source), target)
            protected[name] = sha(target)
        # Only host-authored categories reach the repair model, not raw errors,
        # configuration, native logs, corpus contents or model responses.
        save(candidate / "incident.json", {"kind": state["failure_kind"], "attempt": state["attempt"]})
        protected["incident.json"] = sha(candidate / "incident.json")
        save(manifest, {"originals": originals, "protected": protected})
        return candidate

    def verify(self, candidate):
        manifest = read(candidate.parent / "manifest.json")
        for name, expected in manifest["protected"].items():
            if sha(safe_file(candidate, name)) != expected:
                raise ValueError("protected_asset_changed")
        changed = [name for name, before in manifest["originals"].items()
                   if sha(safe_file(candidate, name)) != before]
        test_files = self.config.get("test_files", [n for n in changed if Path(n).name.startswith("test_")])
        if not any(name in test_files for name in changed) or not any(name not in test_files for name in changed):
            raise ValueError("implementation_and_regression_test_required")
        if self.project_check("regression_command", candidate).returncode:
            raise ValueError("regression_failed")
        if self.project_check("verify_command", candidate).returncode:
            raise ValueError("project_verification_failed")
        return {name: sha(safe_file(candidate, name)) for name in changed}

    def install(self, candidate, verified):
        manifest = read(candidate.parent / "manifest.json")
        backups = candidate.parent / "backups"
        backups.mkdir(exist_ok=True, mode=0o700)
        # This journal is checkpointed before installation. A restart completes
        # the same verified installation and never spends another repair call.
        for name, after in verified.items():
            source = safe_file(candidate, name)
            destination = safe_file(self.project, self.config["repair_files"][name])
            if sha(source) != after:
                raise ValueError("Verified candidate changed before installation")
            current = sha(destination)
            if current == after:
                continue
            if current != manifest["originals"][name]:
                raise ValueError("Project changed concurrently; preserving both versions")
            backup = safe_file(backups, name)
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not backup.exists():
                shutil.copyfile(destination, backup)
            # Replace atomically in the destination filesystem, retaining mode.
            temporary = destination.with_name(destination.name + ".gnhf-repair")
            if temporary.is_symlink():
                raise ValueError("Installation temporary path is a symlink")
            if temporary.exists():
                if sha(temporary) != after:
                    raise ValueError("Interrupted installation has conflicting temporary data")
            else:
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != after:
                    raise ValueError("Verified candidate changed while reading installation data")
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            temporary.chmod(destination.stat().st_mode & 0o777)
            os.replace(temporary, destination)
            descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def recover(self, state):
        if state["state"] not in ("aborted", "repairing") or not state.get("failure_kind"):
            raise ValueError("Repair requires a failed or aborted main run")
        if state.get("stagnant_repairs", 0) >= self.config.get("max_repairs_without_progress", 3):
            self.checkpoint(state, "recovery_blocked", reason="repair_progress_limit")
            return
        candidate = self.prepare(state)
        if not shutil.which("bwrap") or subprocess.run(
                ["bwrap", "--unshare-net", "--ro-bind", "/", "/", "--", "true"],
                capture_output=True).returncode:
            self.checkpoint(state, "recovery_blocked", reason="verification_isolation_unavailable")
            return
        index = state.get("repair_index", 1)
        self.checkpoint(state, "repairing", repair_index=index)
        prompt = ("Repair the supplied implementation and add a regression test. "
                  "Use the synthetic fixture and incident.json. Preserve every original source row and identity. "
                  "Do not weaken validators, edit protected assets or claim success without a code change. "
                  "Only these files can be installed: " + ", ".join(self.config["repair_files"]) + ". "
                  "Previous verification category: " + state.get("verification_category", "none"))
        key = f"repair-{state['attempt']}" + (f"-{index}" if index > 1 else "")
        result = self.operation(state, key, "repair", lambda allowance: self.command("repair_command", candidate, allowance),
                                candidate, candidate=candidate, prompt=prompt)
        if result["reason"] == "operator_stop":
            self.checkpoint(state, "stopped_by_operator")
            return
        try:
            if result["exit_code"] != 0 or result["reason"] in ("token_limit", "timeout"):
                raise ValueError("repair_execution_failed")
            verified = self.verify(candidate)
        except ValueError as error:
            # These categories are host literals; arbitrary hook output stays private.
            if index >= self.config.get("max_repair_attempts", 3):
                self.checkpoint(state, "recovery_blocked", reason=str(error))
            else:
                self.checkpoint(state, "aborted", repair_index=index + 1, verification_category=str(error))
            return
        save(candidate.parent / "verified.json", verified)
        self.checkpoint(state, "installing", candidate=str(candidate), verified=verified)

    def watch(self):
        with locked(self.root / "supervisor.lock", nonblocking=True):
            self.freeze()
            state = read(self.state_path) if self.state_path.exists() else {"state": "starting", "attempt": 1}
            while True:
                if self.stop.exists() and state["state"] not in ("running", "repairing"):
                    self.checkpoint(state, "stopped_by_operator")
                status = state["state"]
                if status in ("completed", "stopped_by_operator", "recovery_blocked"):
                    return {"completed": 0, "stopped_by_operator": 130, "recovery_blocked": 3}[status]
                if status == "installing":
                    self.install(Path(state["candidate"]), state["verified"])
                    save(self.root / "attempts" / f"{state['attempt']}.json", state)
                    state = {"state": "starting", "attempt": state["attempt"] + 1,
                             "stagnant_repairs": state.get("stagnant_repairs", 0) + 1,
                             "progress": state.get("progress")}
                    save(self.state_path, state)
                    continue
                if status in ("aborted", "repairing"):
                    self.recover(state)
                    continue
                if status not in ("starting", "running"):
                    raise ValueError("Unknown lifecycle state")
                self.checkpoint(state, "running")
                prompt = None
                if "main_prompt_file" in self.config:
                    prompt = safe_file(self.project, self.config["main_prompt_file"]).read_text()
                result = self.operation(state, f"main-{state['attempt']}", "main",
                                        lambda allowance: self.command("main_command", allowance=allowance),
                                        self.project, native_runs=Path(self.config["native_runs"]), prompt=prompt)
                if result["reason"] == "operator_stop":
                    self.checkpoint(state, "stopped_by_operator")
                    continue
                if result["reason"] == "token_limit":
                    self.checkpoint(state, "recovery_blocked", reason="main_token_limit")
                    continue
                terminal = result["terminal"]
                if terminal and terminal.get("status") != "aborted" and result["exit_code"] == 0:
                    if self.project_check("completion_command").returncode == 0:
                        self.checkpoint(state, "completed")
                        continue
                    failure = "unverified_completion"
                else:
                    failure = "native_abort" if terminal and terminal.get("status") == "aborted" else "unfinished_exit"
                if "progress_command" in self.config:
                    progress = digest(self.project_check("progress_command").stdout.hex())
                    if progress != state.get("progress"):
                        state["stagnant_repairs"] = 0
                    state["progress"] = progress
                self.checkpoint(state, "aborted", failure_kind=failure, main_result=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("watch", "stop", "status", "scaffold"))
    parser.add_argument("path", type=Path, help="config.json, or a new directory for scaffold")
    args = parser.parse_args()
    if args.action == "scaffold":
        from scaffold import scaffold
        scaffold(args.path)
        return 0
    owner = Supervisor(args.path)
    if args.action == "stop":
        save(owner.stop, {"operator": True})
        return 0
    if args.action == "status":
        print(owner.state_path.read_text() if owner.state_path.exists() else '{"state":"not_started"}')
        return 0
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: save(owner.stop, {"operator": True}))
    try:
        return owner.watch()
    except Exception as error:
        # Preserve execution identities and every workspace for reconciliation.
        save(owner.root / "controller-error.json", {"type": type(error).__name__, "message": str(error)})
        print(f"Supervisor stopped: {type(error).__name__}; inspect private controller-error.json", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
