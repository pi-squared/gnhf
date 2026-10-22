import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jobs import run_job
from storage import read, save

HERE = Path(__file__).absolute().parent


class RecordedJobTests(unittest.TestCase):
    def test_job_result_is_delivered_once_and_billed_to_the_main_pool(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.json"
            save(config, {"state_dir": str(root / "state"), "project_dir": str(root),
                          "total_tokens": 1000, "repair_tokens": 300, "queue_binding": "source-v1"})
            command = [sys.executable, "-c", "import json,sys; assert json.load(sys.stdin)['source']=='synthetic'; "
                       "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':4}}))"]
            first = run_job(config, "task-a", {"source": "synthetic"}, command, 100)
            self.assertEqual(run_job(config, "task-a", {"source": "synthetic"}, command, 100), first)
            self.assertEqual(first["tokens"], 24)
            self.assertEqual(len(read(root / "state/budget.json")["operations"]), 1)
            with self.assertRaises(ValueError):
                run_job(config, "task-a", {"source": "different"}, command, 100)

    def test_command_line_records_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save(root / "config.json", {"state_dir": str(root / "state"), "project_dir": str(root),
                                       "total_tokens": 1000, "repair_tokens": 300, "queue_binding": "source-v1"})
            save(root / "request.json", {"source": "synthetic"})
            result = subprocess.run([sys.executable, str(HERE / "jobs.py"), str(root / "config.json"),
                                     "task-a", str(root / "request.json"), "--max-tokens", "50", "--",
                                     sys.executable, "-c", "print('saved result')"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
