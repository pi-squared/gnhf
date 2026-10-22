"""Project-owned model/tool work with shared budgets and replay-free delivery."""
import argparse
import json
import os
from pathlib import Path
import sys

from processes import start_operation, wait_operation
from storage import Budget, WorkQueue, digest, read, save


def queue_for(config):
    return WorkQueue(Path(config["state_dir"]) / "queue.json", config["queue_binding"])


def run_job(config_path, task_id, request, command, max_tokens):
    os.umask(0o077)
    config = read(config_path)
    queue = queue_for(config)
    queue.add(task_id, {"payload": request, "command": command, "max_tokens": max_tokens})
    execution = queue.claim(task_id)
    if queue.result(task_id) is not None:
        return queue.result(task_id)
    root = Path(config["state_dir"])
    identity = "job-" + digest(execution)
    folder = root / "operations" / identity
    budget = Budget(root / "budget.json", config["total_tokens"], config["repair_tokens"])
    budget.reserve(identity, "main", max_tokens)
    # The exact request, effective argv and token allowance are checkpointed
    # before invocation. Platform recorders can wrap this boundary themselves.
    start_operation(folder, command, config["project_dir"], max_tokens,
                    prompt=json.dumps(request), timeout=config.get("timeout_seconds", 3600))
    result = wait_operation(folder, root / "stop.json")
    budget.settle(identity, result["tokens"])
    if result["exit_code"] != 0 or result["reason"] != "exit":
        raise RuntimeError("Job failed; retain its recorded execution before explicitly retrying")
    receipt = {"execution": execution, "result": str(folder / "stdout.jsonl"), "tokens": result["tokens"]}
    queue.record(task_id, execution, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("task_id")
    parser.add_argument("request", type=Path)
    parser.add_argument("--max-tokens", required=True, type=int)
    argv = sys.argv[1:]
    separator = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:separator])
    command = argv[separator + 1:]
    if not command:
        parser.error("a command argv after -- is required")
    print(json.dumps(run_job(args.config, args.task_id, read(args.request), command, args.max_tokens)))


if __name__ == "__main__":
    main()
