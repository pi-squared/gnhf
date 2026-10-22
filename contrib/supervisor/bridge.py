"""Optional Codex wrapper: refresh the task frontier and reject no-progress success."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from jobs import queue_for
from processes import terminate, identity
from storage import read


def main():
    config = read(os.environ["GNHF_SUPERVISOR_CONFIG"])
    queue = queue_for(config)
    before = queue.frontier()
    completed_before = set(queue.completed())
    args = sys.argv[1:]
    # Native GNHF uses `exec -` after this PR. Refuse positional-prompt mode so
    # private prompts cannot accidentally reappear on the command line.
    if "-" not in args:
        raise ValueError("The supervised bridge requires GNHF's stdin prompt transport")
    prompt = sys.stdin.read()
    prompt += ("\n\nAuthoritative current durable task frontier (refreshes each iteration):\n" +
               json.dumps(before) + "\nReconcile admitted executions before starting new model work. "
               "Use the project's recorded job interface; never repeat completed deliveries.")
    if len(prompt) > config.get("max_prompt_characters", 900_000):
        raise ValueError("Prepared prompt exceeds the configured input limit; split the task losslessly")
    child = subprocess.Popen([config.get("codex_binary", "codex"), *args], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, start_new_session=True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: terminate(identity(child.pid)))
    def send_prompt():
        try:
            child.stdin.write(prompt)
            child.stdin.close()
        except BrokenPipeError:
            pass
    threading.Thread(target=send_prompt, daemon=True).start()
    final = None
    for line in child.stdout:
        try:
            event = json.loads(line)
            if isinstance(event, dict) and event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                value = json.loads(event["item"]["text"])
                if isinstance(value, dict) and "success" in value:
                    final = event
                    continue
        except (ValueError, TypeError):
            pass
        # Usage streams immediately so GNHF and the host can enforce limits.
        print(line, end="", flush=True)
    code = child.wait()
    if final:
        value = json.loads(final["item"]["text"])
        if value.get("success") and before and not set(queue.completed()) - completed_before:
            value.update(success=False, should_fully_stop=False,
                         summary="No completed durable task; reconcile the current frontier")
        if queue.frontier():
            value["should_fully_stop"] = False
        final["item"]["text"] = json.dumps(value)
        print(json.dumps(final), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
