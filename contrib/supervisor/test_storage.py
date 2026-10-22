import tempfile
import unittest
from pathlib import Path

from storage import Budget, WorkQueue


class DurableWorkTests(unittest.TestCase):
    def test_completed_delivery_is_reconciled_without_repeating_work(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = WorkQueue(Path(folder) / "queue.json", "source-and-protocol-v1")
            queue.add("task-a", {"source": "synthetic-source"})
            self.assertEqual(queue.claim("task-a"), "task-a:1")
            queue.record("task-a", "task-a:1", {"artifact": "saved-result.json"})
            reopened = WorkQueue(queue.path, "source-and-protocol-v1")
            self.assertEqual(reopened.claim("task-a"), "task-a:1")
            self.assertEqual(reopened.frontier(), [])
            self.assertEqual(reopened.result("task-a"), {"artifact": "saved-result.json"})
            with self.assertRaises(ValueError):
                reopened.add("task-a", {"source": "changed-source"})
            with self.assertRaises(ValueError):
                WorkQueue(queue.path, "changed-source-binding")

    def test_research_and_coding_share_a_pool_and_repairs_keep_their_reserve(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "budget.json"
            budget = Budget(path, total=100, repair=30)
            budget.reserve("coding-1", "main", 40)
            budget.reserve("research-1", "main", 30)
            with self.assertRaises(ValueError):
                budget.reserve("research-2", "main", 1)
            budget.settle("coding-1", 25)
            budget.settle("coding-1", 25)
            self.assertEqual(Budget(path, 100, 30).available("main"), 15)
            self.assertEqual(budget.available("repair"), 30)
            budget.reserve("repair-1", "repair", 20)
            budget.settle("repair-1", 10)
            self.assertEqual(budget.available("repair"), 20)
            with self.assertRaises(ValueError):
                Budget(path, 101, 30)


if __name__ == "__main__":
    unittest.main()
