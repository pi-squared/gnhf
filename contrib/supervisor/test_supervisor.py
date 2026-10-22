import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from storage import read, save

HERE = Path(__file__).absolute().parent


def isolation_available():
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-net", "--ro-bind", "/", "/", "--", "true"],
                          capture_output=True).returncode == 0


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "project").mkdir()
        (self.root / "project/worker.py").write_text("answer = 'broken'\n")
        (self.root / "project/test_worker.py").write_text("# original regression test\n")
        (self.root / "main.py").write_text(
            "from pathlib import Path\nfrom datetime import datetime,timezone\nimport json,os\n"
            "p=Path('project/worker.py'); fixed=\"'fixed'\" in p.read_text()\n"
            "r=Path('runs/one'); r.mkdir(parents=True,exist_ok=True)\n"
            "v={'status':'completed' if fixed else 'aborted',"
            "'endedAt':datetime.now(timezone.utc).isoformat()}\n"
            "(r/'end-state.json').write_text(json.dumps(v))\n")
        (self.root / "repair.py").write_text(
            "from pathlib import Path\nimport json,sys\n"
            "assert sys.stdin.read()\n"
            "Path('worker.py').write_text(\"answer = 'fixed'\\n\")\n"
            "Path('test_worker.py').write_text(\"from worker import answer\\nassert answer == 'fixed'\\n\")\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'cached_input_tokens':5,'output_tokens':3}}))\n")
        self.config = {
            "state_dir": str(self.root / "state"), "project_dir": str(self.root),
            "native_runs": str(self.root / "runs"), "total_tokens": 1000, "repair_tokens": 300,
            "main_attempt_tokens": 200, "repair_attempt_tokens": 100, "timeout_seconds": 10,
            "main_command": [sys.executable, str(self.root / "main.py")],
            "repair_command": [sys.executable, str(self.root / "repair.py")],
            "repair_files": {"worker.py": "project/worker.py", "test_worker.py": "project/test_worker.py"},
            "fixture_files": {}, "regression_command": [sys.executable, "test_worker.py"],
            "verify_command": [sys.executable, "-c", "from pathlib import Path; "
                               "assert \"'fixed'\" in Path('worker.py').read_text()"],
            "completion_command": [sys.executable, "-c", "from pathlib import Path; "
                                   "assert \"'fixed'\" in Path('project/worker.py').read_text()"],
        }

    def run_owner(self):
        save(self.root / "config.json", self.config)
        return subprocess.run([sys.executable, str(HERE / "supervisor.py"), "watch",
                               str(self.root / "config.json")], capture_output=True, text=True, timeout=25)

    @unittest.skipUnless(isolation_available(), "requires working Linux bubblewrap")
    def test_abort_is_repaired_verified_installed_and_resumed_once(self):
        result = self.run_owner()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = read(self.root / "state/lifecycle.json")
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["attempt"], 2)
        self.assertIn("'fixed'", (self.root / "project/worker.py").read_text())
        self.assertEqual(len(list((self.root / "state/operations").glob("repair-*"))), 1)
        self.assertEqual(read(self.root / "state/budget.json")["operations"]["repair-1"]["used"], 15)
        self.assertEqual(self.run_owner().returncode, 0)
        self.assertEqual(len(list((self.root / "state/operations").glob("repair-*"))), 1)

    def test_healthy_completion_never_starts_repair(self):
        (self.root / "project/worker.py").write_text("answer = 'fixed'\n")
        result = self.run_owner()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "state/operations").glob("repair-*")), [])

    @unittest.skipUnless(isolation_available(), "requires working Linux bubblewrap")
    def test_owner_restart_reattaches_to_the_same_repair(self):
        script = self.root / "repair.py"
        script.write_text("import time\nfrom pathlib import Path\n"
                          f"p=Path({str(self.root / 'repair-started')!r}); "
                          "p.write_text(p.read_text()+'x' if p.exists() else 'x')\ntime.sleep(1.5)\n" + script.read_text())
        save(self.root / "config.json", self.config)
        process = subprocess.Popen([sys.executable, str(HERE / "supervisor.py"), "watch",
                                    str(self.root / "config.json")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 8
            while not (self.root / "repair-started").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.root / "repair-started").exists())
            process.kill()
            process.wait(timeout=3)
            result = self.run_owner()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.root / "repair-started").read_text(), "x")
            self.assertEqual(read(self.root / "state/lifecycle.json")["state"], "completed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    @unittest.skipUnless(isolation_available(), "requires working Linux bubblewrap")
    def test_a_repair_cannot_weaken_protected_assets(self):
        (self.root / "fixture.json").write_text('{"protected":"synthetic evidence"}')
        self.config["fixture_files"] = {"fixture.json": "fixture.json"}
        script = self.root / "repair.py"
        script.write_text(script.read_text() + "Path('fixture.json').write_text('{}')\n")
        result = self.run_owner()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(read(self.root / "state/lifecycle.json")["reason"], "protected_asset_changed")
        self.assertIn("broken", (self.root / "project/worker.py").read_text())
        self.assertEqual(len(list((self.root / "state/operations").glob("repair-*"))), 3)

    def test_operator_stop_never_starts_repair(self):
        save(self.root / "state/stop.json", {"operator": True})
        result = self.run_owner()
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(read(self.root / "state/lifecycle.json")["state"], "stopped_by_operator")
        self.assertFalse((self.root / "state/operations").exists())


if __name__ == "__main__":
    unittest.main()
