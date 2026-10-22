"""Generate explicit project adapter files; never start a model during setup."""
from pathlib import Path
import json
import shlex
import sys

from storage import save


def scaffold(destination):
    root = Path(destination).absolute()
    # Never overwrite an existing project or experiment directory.
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    here = Path(__file__).absolute().parent
    python = str(Path(sys.executable).absolute())  # Preserve a virtualenv symlink.
    (root / "adapter.py").write_text("def transform(rows):\n    return rows\n")
    (root / "test_adapter.py").write_text(
        "from adapter import transform\nassert transform([{'id': 'synthetic-1'}]) == [{'id': 'synthetic-1'}]\n")
    (root / "verify.py").write_text(
        "# Host-owned verification. Add your native validator and exact source-preservation checks.\n"
        "raise SystemExit('Configure project verification before starting a run')\n")
    (root / "complete.py").write_text(
        "# Host-owned completion criteria; a successful CLI exit alone is insufficient.\n"
        "raise SystemExit('Configure project completion before starting a run')\n")
    (root / "fixture.json").write_text('{"rows":[{"id":"synthetic-1","text":"Synthetic evidence"}]}\n')
    (root / "objective.md").write_text(
        "Replace this with acceptance criteria, native blockers and verification commands.\n"
        "Use the durable queue and recorded job interface for expensive work.\n")
    launcher = root / "codex-bridge"
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(python) + " " + shlex.quote(str(here / "bridge.py")) + ' "$@"\n')
    launcher.chmod(0o700)
    (root / "gnhf-config.yml").write_text(
        "agent: codex\npreventSleep: false\nagentArgsOverride:\n  codex:\n"
        "    - -c\n    - 'model_reasoning_effort=\"medium\"'\n"
        "agentPathOverride:\n  codex: " + json.dumps(str(launcher)) + "\n")
    config = {
        "prepared": False,
        "state_dir": str(root / ".supervision"), "project_dir": str(root),
        "native_runs": str(root / ".gnhf/runs"), "queue_binding": "replace-with-source-and-protocol-hash",
        "total_tokens": 500_000_000, "repair_tokens": 50_000_000,
        "main_attempt_tokens": 10_000_000, "repair_attempt_tokens": 5_000_000,
        "timeout_seconds": 14400, "max_repair_attempts": 3, "max_repairs_without_progress": 3,
        "max_prompt_characters": 900_000,
        "gnhf_config_file": "gnhf-config.yml", "main_prompt_file": "objective.md",
        "main_command": ["gnhf", "--current-branch", "--agent", "codex", "--model", "gpt-6-luna",
                         "--max-tokens", "{max_tokens}"],
        "repair_command": ["codex", "exec", "--model", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"',
                           "-c", 'web_search="disabled"', "--sandbox", "workspace-write",
                           "--skip-git-repo-check", "--json", "-"],
        "repair_files": {"adapter.py": "adapter.py", "test_adapter.py": "test_adapter.py"},
        "test_files": ["test_adapter.py"], "fixture_files": {"fixture.json": "fixture.json"},
        "trusted_files": {"verify.py": "verify.py", "complete.py": "complete.py"},
        "regression_command": [python, "test_adapter.py"],
        "verify_command": [python, "{trusted}/verify.py", "{candidate}"],
        "completion_command": [python, "{trusted}/complete.py", "{project}"],
    }
    save(root / "config.json", config)
    (root / ".gitignore").write_text(".supervision/\n.gnhf/\n__pycache__/\n")
    print(f"Created {root}. Configure validators, tasks and objective before watch. No model was started.")
