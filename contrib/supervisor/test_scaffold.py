from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from scaffold import scaffold
from storage import read


class ScaffoldTests(unittest.TestCase):
    def test_setup_is_offline_and_refuses_to_overwrite_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "new project"
            with redirect_stdout(io.StringIO()):
                scaffold(root)
            config = read(root / "config.json")
            self.assertEqual(config["total_tokens"], 500_000_000)
            self.assertEqual(config["repair_tokens"], 50_000_000)
            self.assertIn("gpt-6.1-sol", config["repair_command"])
            self.assertIn("gpt-6-luna", config["main_command"])
            self.assertFalse((root / ".supervision").exists())
            with self.assertRaises(FileExistsError):
                scaffold(root)


if __name__ == "__main__":
    unittest.main()
