import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jobs import queue_for
from storage import save

HERE = Path(__file__).absolute().parent


class BridgeTests(unittest.TestCase):
    def test_stale_success_is_rejected_and_the_latest_frontier_is_forwarded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / "codex"
            fake.write_text("#!" + sys.executable + "\nimport json,sys\n"
                            "p=sys.stdin.read()\n"
                            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}))\n"
                            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':"
                            "json.dumps({'success':True,'should_fully_stop':True,'summary':p})}}))\n")
            fake.chmod(0o700)
            config = {"state_dir": str(root / "state"), "queue_binding": "source-v1", "codex_binary": str(fake)}
            save(root / "config.json", config)
            queue = queue_for(config)
            queue.add("task-new", {"evidence": "synthetic evidence"})
            result = subprocess.run([sys.executable, str(HERE / "bridge.py"), "exec", "-"],
                                    input="objective", text=True, capture_output=True,
                                    env={**os.environ, "GNHF_SUPERVISOR_CONFIG": str(root / "config.json")})
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[0]["usage"]["input_tokens"], 10)
            final = json.loads(events[-1]["item"]["text"])
            self.assertFalse(final["success"])
            self.assertFalse(final["should_fully_stop"])


if __name__ == "__main__":
    unittest.main()
