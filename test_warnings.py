"""Standard-library tests for the persistent warning decay rules.

The store class is extracted with ast so no Discord login or dependencies are needed.
Run: python -m unittest test_warning_manager.py
"""
import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
source = ast.parse((ROOT / "warning_manager.py").read_text(encoding="utf-8"))
store_class = next(node for node in source.body
                   if isinstance(node, ast.ClassDef) and node.name == "WarningStore")
ns = {"sqlite3": sqlite3, "time": __import__("time"), "PERIOD": 30 * 86400}
exec(compile(ast.Module(body=[store_class], type_ignores=[]), str(ROOT / "warning_manager.py"), "exec"), ns)
WarningStore = ns["WarningStore"]
DAY = 86400


class WarningStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "warnings.sqlite3"

        def connect():
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            return db

        self.connect = connect
        self.store = WarningStore(connect)

    def test_new_warning_does_not_reset_first_due(self):
        first = 100 * DAY
        self.assertEqual(self.store.change(1, 10, 20, 1, "첫 위반", "grant", first),
                         (1, first + 30 * DAY))
        self.assertEqual(self.store.change(1, 10, 20, 1, "추가 위반", "grant", first + 5 * DAY),
                         (2, first + 30 * DAY))
        self.assertEqual(self.store.get(1, 10, first + 29 * DAY), (2, first + 30 * DAY))
        self.assertEqual(self.store.get(1, 10, first + 30 * DAY), (1, first + 60 * DAY))
        self.assertEqual(self.store.get(1, 10, first + 60 * DAY), (0, None))

    def test_bot_restart_and_missed_intervals_are_caught_up(self):
        first = 100 * DAY
        self.store.change(1, 10, 20, 3, "다중 위반", "grant", first)
        self.store = WarningStore(self.connect)  # restart same persisted db
        self.assertEqual(self.store.get(1, 10, first + 100 * DAY), (0, None))
        history = self.store.history(1, 10)
        self.assertEqual(sum(1 for row in history if row["event_type"] == "decay"), 3)
        self.assertEqual(self.store.decay(first + 100 * DAY), [])  # no duplicate

    def test_cancel_keeps_due_until_zero_then_resets(self):
        first = 100 * DAY
        self.store.change(1, 10, 20, 2, "부여", "grant", first)
        self.assertEqual(self.store.change(1, 10, 20, -1, "취소", "revoke", first + DAY),
                         (1, first + 30 * DAY))
        self.assertEqual(self.store.change(1, 10, 20, -1, "취소", "revoke", first + 2 * DAY),
                         (0, None))
        self.assertEqual(self.store.change(1, 10, 20, 1, "새 위반", "grant", first + 4 * DAY),
                         (1, first + 34 * DAY))

    def test_cannot_cancel_more_than_current_count(self):
        self.store.change(1, 10, 20, 1, "부여", "grant", 100 * DAY)
        with self.assertRaises(ValueError):
            self.store.change(1, 10, 20, -2, "취소", "revoke", 101 * DAY)
        self.assertEqual(self.store.get(1, 10, 101 * DAY)[0], 1)

    def test_manager_can_grant_until_two_but_not_three(self):
        first = 100 * DAY
        self.assertEqual(
            self.store.change(1, 10, 20, 2, "직업 무단 변경", "grant", first, max_total=2),
            (2, first + 30 * DAY))
        with self.assertRaisesRegex(ValueError, "3회부터"):
            self.store.change(1, 10, 20, 1, "추가 경고", "grant", first + DAY, max_total=2)
        self.assertEqual(self.store.get(1, 10, first + DAY), (2, first + 30 * DAY))
        # Mayor may add a third without resetting the existing decay date.
        self.assertEqual(
            self.store.change(1, 10, 99, 1, "마을장 승인", "grant", first + DAY),
            (3, first + 30 * DAY))

    def test_manager_can_add_second_warning_to_existing_one(self):
        first = 100 * DAY
        self.store.change(1, 10, 20, 1, "첫 경고", "grant", first, max_total=2)
        self.assertEqual(
            self.store.change(1, 10, 20, 1, "두 번째", "grant", first + 5 * DAY, max_total=2),
            (2, first + 30 * DAY))

    def test_guild_data_is_separate(self):
        self.store.change(1, 10, 20, 1, "위반", "grant", 100 * DAY)
        self.assertEqual(self.store.get(2, 10, 101 * DAY), (0, None))


if __name__ == "__main__":
    unittest.main()
