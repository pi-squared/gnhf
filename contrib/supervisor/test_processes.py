from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from processes import native_terminal, start_operation, wait_operation
from storage import save


class ProcessTests(unittest.TestCase):
    def test_old_abort_events_do_not_abort_a_resumed_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "run-a"
            run.mkdir()
            old = "2000-01-01T00:00:00+00:00"
            events = [{"timestamp": old, "event": name} for name in
                      ("orchestrator:abort", "orchestrator:end")]
            (run / "gnhf.log").write_text("\n".join(map(json.dumps, events)) + "\n")
            started = time.time()
            self.assertIsNone(native_terminal(root, started))
            stamp = datetime.now(timezone.utc).isoformat()
            with (run / "gnhf.log").open("a") as handle:
                for name in ("orchestrator:abort", "orchestrator:end"):
                    handle.write(json.dumps({"timestamp": stamp, "event": name}) + "\n")
            self.assertEqual(native_terminal(root, started)["status"], "aborted")

    def test_saved_worker_result_is_not_executed_again(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            counter = root / "counter"
            command = [sys.executable, "-c", "from pathlib import Path; "
                       f"p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
            op = root / "operation"
            start_operation(op, command, root, 100, timeout=10)
            first = wait_operation(op)
            start_operation(op, command, root, 100, timeout=10)
            self.assertEqual(wait_operation(op), first)
            self.assertEqual(counter.read_text(), "x")

    def test_native_abort_quiesces_a_process_that_never_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runs = root / "runs"
            command = [sys.executable, "-c", "import time; from pathlib import Path; "
                       "from datetime import datetime,timezone; import json; "
                       f"p=Path({str(runs / 'one')!r}); p.mkdir(parents=True); "
                       "v={'status':'aborted','endedAt':datetime.now(timezone.utc).isoformat()}; "
                       "(p/'end-state.json').write_text(json.dumps(v)); time.sleep(30)"]
            start_operation(root / "operation", command, root, 100, native_runs=runs, timeout=10)
            result = wait_operation(root / "operation")
            self.assertEqual(result["terminal"]["status"], "aborted")
            self.assertEqual(result["reason"], "native_terminal")

    def test_reported_tokens_stop_a_worker_before_its_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command = [sys.executable, "-c", "import json,time; "
                       "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':5}}),flush=True); "
                       "time.sleep(30)"]
            start_operation(root / "operation", command, root, 100, timeout=10)
            result = wait_operation(root / "operation")
            self.assertEqual(result["reason"], "token_limit")
            self.assertEqual(result["tokens"], 125)


if __name__ == "__main__":
    unittest.main()
