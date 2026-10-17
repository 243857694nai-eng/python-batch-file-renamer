"""阶段一测试：文件和数据库均在每次新建的临时目录内，不接触原有测试文件。"""

import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import operation_history
from operation_history import HistoryError, HistoryStore
from rename_tool import CONFIRM_TEXT, ExecutionError, RenameError, RenameItem, execute_plan, file_snapshot, main, make_plan


class OperationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="历史自动测试_", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "新建数据"
        self.folder.mkdir()
        self.history = self.root / ".rename_history"
        self.addCleanup(self.temporary.cleanup)
        history_patch = patch("operation_history.DEFAULT_HISTORY_DIR", self.history)
        history_patch.start()
        self.addCleanup(history_patch.stop)

    def write(self, name, content=None):
        path = self.folder / name
        path.write_text(content or "全新临时数据：" + name, encoding="utf-8")
        return path

    def snapshot(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest().upper()
                for path in self.folder.iterdir() if path.is_file()}

    def prepare(self, count=2):
        for name in ("a.txt", "b.txt", "c.txt")[:count]:
            self.write(name)
        return make_plan(self.folder)

    def latest(self):
        return HistoryStore.read_history()[0]

    def test_empty_history_is_read_only_and_creates_no_directory(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--history"]), 0)
        self.assertIn("暂无", output.getvalue())
        self.assertFalse(self.history.exists())

    def test_preview_creates_no_history_and_changes_nothing(self):
        self.prepare()
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--folder", str(self.folder)]), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_cancel_and_wrong_confirmation_create_no_history(self):
        plan = self.prepare()
        with self.assertRaises(RenameError):
            execute_plan(plan, "yes")
        with patch("builtins.input", return_value="取消"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--folder", str(self.folder), "--apply"]), 0)
        self.assertFalse(self.history.exists())

    def test_success_is_durable_and_has_names_directory_time_hashes_and_events(self):
        plan = self.prepare()
        before = self.snapshot()
        self.assertEqual(execute_plan(plan, CONFIRM_TEXT), plan)
        operation = self.latest()
        self.assertEqual(operation["effective_status"], "completed")
        self.assertEqual(operation["folder"], str(self.folder))
        self.assertTrue(operation["created_at"].endswith("+00:00"))
        self.assertTrue(operation["finished_at"])
        self.assertEqual([item["status"] for item in operation["items"]], ["success", "success"])
        for item in operation["items"]:
            self.assertEqual(item["sha256"], before[item["original_name"]])
            self.assertEqual(item["sha256"], self.snapshot()[item["new_name"]])
            self.assertTrue(item["prepared_at"])
            self.assertTrue(item["finished_at"])
        with contextlib.closing(sqlite3.connect(self.history / operation_history.DATABASE_NAME)) as connection:
            kinds = [row[0] for row in connection.execute("SELECT kind FROM events ORDER BY id")]
        self.assertEqual(kinds, ["operation_started", "rename_intent_saved", "rename_success", "rename_intent_saved", "rename_success", "completed"])

    def test_filtered_real_temporary_files_and_unselected_contents_are_preserved(self):
        for name in ("a.csv", "b.txt", "c.md", "d.TXT"):
            self.write(name)
        before = self.snapshot()
        plan = make_plan(self.folder, extension=".txt")
        execute_plan(plan, CONFIRM_TEXT)
        expected = {"a.csv": before["a.csv"], "学习文件001.txt": before["b.txt"],
                    "c.md": before["c.md"], "学习文件002.TXT": before["d.TXT"]}
        self.assertEqual(self.snapshot(), expected)
        self.assertEqual([item["original_name"] for item in self.latest()["items"]], ["b.txt", "d.TXT"])

    def test_existing_no_op_still_creates_no_history(self):
        self.write("学习文件001.txt")
        self.assertEqual(execute_plan(make_plan(self.folder), CONFIRM_TEXT), [])
        self.assertFalse(self.history.exists())

    def test_two_separate_operations_survive_reopening(self):
        self.prepare(1)
        execute_plan(make_plan(self.folder, "第一轮"), CONFIRM_TEXT)
        execute_plan(make_plan(self.folder, "第二轮"), CONFIRM_TEXT)
        history = HistoryStore.read_history()
        self.assertEqual(len(history), 2)
        self.assertNotEqual(history[0]["id"], history[1]["id"])
        self.assertTrue(all(operation["effective_status"] == "completed" for operation in history))

    def test_database_open_failure_prevents_any_rename(self):
        plan = self.prepare()
        before = self.snapshot()
        with patch("operation_history.sqlite3.connect", side_effect=sqlite3.OperationalError("模拟无法打开数据库")), patch("rename_tool.os.rename") as rename:
            with self.assertRaises(HistoryError):
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_plan_record_failure_prevents_any_rename(self):
        plan = self.prepare()
        before = self.snapshot()
        with patch.object(HistoryStore, "begin_operation", side_effect=HistoryError("模拟计划记录失败")), patch("rename_tool.os.rename") as rename:
            with self.assertRaises(HistoryError):
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()
        self.assertEqual(HistoryStore.read_history(), [])
        self.assertEqual(self.snapshot(), before)

    def test_intent_commit_failure_prevents_rename_and_leaves_batch_for_review(self):
        plan = self.prepare()
        before = self.snapshot()
        commit = HistoryStore._commit

        def fail_intent(store):
            pending = store.connection.execute("SELECT COUNT(*) FROM items WHERE status='in_progress'").fetchone()[0]
            if pending:
                raise sqlite3.OperationalError("模拟意图提交时磁盘写入失败")
            commit(store)

        with patch.object(HistoryStore, "_commit", fail_intent), patch("rename_tool.os.rename") as rename:
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.latest()["effective_status"], "needs_review")

    def test_success_commit_failure_after_real_rename_is_pending_not_failed_or_success(self):
        plan = self.prepare()
        commit = HistoryStore._commit
        real_rename = os.rename

        def fail_success(store):
            done = store.connection.execute("SELECT COUNT(*) FROM items WHERE status='success'").fetchone()[0]
            if done:
                raise sqlite3.OperationalError("模拟文件已改名但成功记录提交失败")
            commit(store)

        with patch.object(HistoryStore, "_commit", fail_success), patch("rename_tool.os.rename", wraps=real_rename) as rename:
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
            self.assertEqual(rename.call_count, 1)
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(caught.exception.completed, [])
        self.assertTrue(plan[0].target.exists())
        self.assertFalse(plan[0].source.exists())
        self.assertTrue(plan[1].source.exists())
        operation = self.latest()
        self.assertEqual(operation["effective_status"], "needs_review")
        self.assertEqual([item["effective_status"] for item in operation["items"]], ["needs_review", "planned"])
        self.assertEqual(operation["items"][0]["status"], "in_progress")

    def test_failure_after_committed_success_still_does_not_claim_batch_completed(self):
        plan = self.prepare()
        commit = HistoryStore._commit

        def commit_then_interrupt(store):
            done = store.connection.execute("SELECT COUNT(*) FROM items WHERE status='success'").fetchone()[0]
            commit(store)
            if done:
                raise RuntimeError("模拟成功提交后进程异常")

        with patch.object(HistoryStore, "_commit", commit_then_interrupt):
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
        self.assertTrue(caught.exception.needs_review)
        operation = self.latest()
        self.assertEqual(operation["effective_status"], "needs_review")
        self.assertEqual(operation["items"][0]["status"], "success")
        self.assertEqual(operation["items"][1]["status"], "planned")

    def test_known_os_failure_is_recorded_failed_and_rest_not_attempted(self):
        plan = self.prepare()
        before = self.snapshot()
        with patch("rename_tool.os.rename", side_effect=PermissionError("模拟拒绝访问")):
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
        self.assertFalse(caught.exception.needs_review)
        operation = self.latest()
        self.assertEqual(operation["effective_status"], "failed")
        self.assertEqual([item["status"] for item in operation["items"]], ["failed", "planned"])
        self.assertIn("拒绝访问", operation["items"][0]["error"])
        self.assertEqual(self.snapshot(), before)

    def test_partial_success_records_exact_success_failure_and_unattempted_items(self):
        plan = self.prepare(3)
        before = self.snapshot()
        real_rename = os.rename

        def fail_second(source, target):
            if source.name == "b.txt":
                raise PermissionError("模拟第二个文件被占用")
            real_rename(source, target)

        with patch("rename_tool.os.rename", side_effect=fail_second) as rename:
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
            self.assertEqual(rename.call_count, 2)
        self.assertEqual(caught.exception.completed, [plan[0]])
        operation = self.latest()
        self.assertEqual(operation["effective_status"], "partial_failure")
        self.assertEqual([item["status"] for item in operation["items"]], ["success", "failed", "planned"])
        self.assertEqual(self.snapshot(), {"学习文件001.txt": before["a.txt"], "b.txt": before["b.txt"], "c.txt": before["c.txt"]})

    def test_failure_to_save_known_failure_is_not_misreported_as_confirmed_failure(self):
        plan = self.prepare()
        with patch("rename_tool.os.rename", side_effect=PermissionError("拒绝访问")), patch.object(HistoryStore, "mark_failed", side_effect=HistoryError("失败结果无法记账")):
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(self.latest()["items"][0]["effective_status"], "needs_review")

    def test_unexpected_exception_after_os_rename_leaves_pending_and_stops(self):
        plan = self.prepare()
        real_rename = os.rename

        def rename_then_raise(source, target):
            real_rename(source, target)
            raise RuntimeError("模拟改名后非预期异常")

        with patch("rename_tool.os.rename", side_effect=rename_then_raise) as rename:
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
            self.assertEqual(rename.call_count, 1)
        self.assertTrue(caught.exception.needs_review)
        self.assertTrue(plan[0].target.exists())
        self.assertTrue(plan[1].source.exists())
        self.assertEqual(self.latest()["items"][0]["effective_status"], "needs_review")

    def test_keyboard_interrupt_keeps_durable_pending_record(self):
        plan = self.prepare()
        with patch("rename_tool.os.rename", side_effect=KeyboardInterrupt):
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(self.latest()["items"][0]["effective_status"], "needs_review")

    def crash_child(self, after_rename):
        plan = self.prepare()
        script = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import operation_history, rename_tool
operation_history.DEFAULT_HISTORY_DIR = Path(sys.argv[3])
real_rename = os.rename
def crash(source, target):
    if sys.argv[4] == 'after':
        real_rename(source, target)
    os._exit(91)
rename_tool.os.rename = crash
rename_tool.execute_plan(rename_tool.make_plan(Path(sys.argv[2])), rename_tool.CONFIRM_TEXT)
'''
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script,
                                 str(Path(__file__).parent), str(self.folder), str(self.history),
                                 "after" if after_rename else "before"], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 91, result.stderr)
        self.assertEqual(plan[0].target.exists(), after_rename)
        self.assertEqual(plan[0].source.exists(), not after_rename)
        self.assertTrue(plan[1].source.exists())
        operation = self.latest()
        self.assertEqual(operation["effective_status"], "needs_review")
        self.assertEqual(operation["items"][0]["status"], "in_progress")
        self.assertEqual(operation["items"][0]["effective_status"], "needs_review")
        self.assertEqual(operation["items"][1]["status"], "planned")

    def test_process_exit_before_os_rename_is_not_guessed_to_be_failure(self):
        self.crash_child(after_rename=False)

    def test_process_exit_after_os_rename_is_not_guessed_to_be_success(self):
        self.crash_child(after_rename=True)

    def test_pending_operation_blocks_new_mutation_in_same_directory(self):
        plan = self.prepare()
        with patch("rename_tool.os.rename", side_effect=RuntimeError("中断")):
            with self.assertRaises(ExecutionError):
                execute_plan(plan, CONFIRM_TEXT)
        before = self.snapshot()
        with patch("rename_tool.os.rename") as rename:
            with self.assertRaises(HistoryError):
                execute_plan(make_plan(self.folder, prefix="下一轮"), CONFIRM_TEXT)
            rename.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_history_display_is_read_only_and_explicitly_reports_pending(self):
        plan = self.prepare()
        with patch("rename_tool.os.rename", side_effect=RuntimeError("中断")):
            with self.assertRaises(ExecutionError):
                execute_plan(plan, CONFIRM_TEXT)
        database = self.history / operation_history.DATABASE_NAME
        before = database.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--history"]), 0)
        self.assertIn("待核查", output.getvalue())
        self.assertIn("未执行", output.getvalue())
        self.assertIn("a.txt", output.getvalue())
        self.assertEqual(database.read_bytes(), before)

    def test_final_batch_write_failure_does_not_claim_complete(self):
        plan = self.prepare()
        with patch.object(HistoryStore, "finish", side_effect=HistoryError("整批结束记录失败")):
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(len(caught.exception.completed), 2)
        operation = self.latest()
        self.assertEqual(operation["effective_status"], "needs_review")
        self.assertEqual([item["status"] for item in operation["items"]], ["success", "success"])

    def test_database_and_descendant_directory_cannot_be_renamed(self):
        self.history.mkdir()
        (self.history / "a.txt").write_text("数据库目录里的保护文件", encoding="utf-8")
        nested = self.history / "子目录"
        nested.mkdir()
        for folder in (self.history, nested):
            with self.subTest(folder=folder), self.assertRaises(RenameError):
                make_plan(folder)
        self.assertTrue((self.history / "a.txt").exists())

    def test_direct_execution_cannot_bypass_history_directory_protection(self):
        self.history.mkdir()
        source = self.history / "a.txt"
        source.write_text("不能被改名", encoding="utf-8")
        plan = [RenameItem(source, self.history / "b.txt", file_snapshot(source))]
        with patch("rename_tool.os.rename") as rename:
            with self.assertRaises(HistoryError):
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()

    def test_backup_directory_is_also_protected(self):
        backup = self.root / "版本备份"
        backup.mkdir()
        (backup / "a.txt").write_text("备份", encoding="utf-8")
        with patch("operation_history.BACKUP_DIR", backup):
            with self.assertRaises(RenameError):
                make_plan(backup)

    def test_database_hardlink_alias_is_rejected(self):
        with HistoryStore():
            pass
        alias = self.folder / "数据库别名.txt"
        os.link(self.history / operation_history.DATABASE_NAME, alias)
        with self.assertRaises(RenameError):
            make_plan(self.folder)
        self.assertTrue(alias.exists())

    def test_second_writer_is_rejected(self):
        with HistoryStore():
            with self.assertRaises(HistoryError):
                with HistoryStore():
                    self.fail("不应同时获得两个写入锁")

    def test_corrupted_database_is_not_overwritten_or_repaired(self):
        plan = self.prepare()
        self.history.mkdir()
        database = self.history / operation_history.DATABASE_NAME
        database.write_bytes(b"this is not a database")
        before_database = database.read_bytes()
        before_files = self.snapshot()
        with self.assertRaises(HistoryError):
            HistoryStore.read_history()
        with self.assertRaises(HistoryError):
            execute_plan(plan, CONFIRM_TEXT)
        self.assertEqual(database.read_bytes(), before_database)
        self.assertEqual(self.snapshot(), before_files)

    def test_unknown_database_version_is_rejected_without_migration(self):
        plan = self.prepare()
        with HistoryStore():
            pass
        database = self.history / operation_history.DATABASE_NAME
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA user_version=99")
        before = self.snapshot()
        with self.assertRaises(HistoryError):
            execute_plan(plan, CONFIRM_TEXT)
        with contextlib.closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
        self.assertEqual(self.snapshot(), before)

    def test_history_cannot_be_combined_with_apply(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--history", "--apply"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.history.exists())

    def test_plan_cannot_target_a_different_directory(self):
        source = self.write("a.txt")
        plan = [RenameItem(source, self.root / "未授权目录.txt", file_snapshot(source))]
        with self.assertRaises(RenameError):
            execute_plan(plan, CONFIRM_TEXT)
        self.assertTrue(source.exists())
        self.assertFalse(self.history.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
