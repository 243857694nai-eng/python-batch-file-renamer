"""安全撤销测试：所有改名、撤销、故障和账本均为全新临时数据。"""

import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from operation_history import HistoryError, HistoryStore
from rename_tool import CONFIRM_TEXT, ExecutionError, execute_plan, main, make_plan
from undo_tool import UndoError, UndoExecutionError, confirmation_text, execute_undo, make_undo_plan


class UndoToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="撤销自动测试_", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "新建数据"
        self.folder.mkdir()
        self.history = self.root / ".rename_history"
        self.database = self.history / "operations.sqlite3"
        self.addCleanup(self.temporary.cleanup)
        history_patch = patch("operation_history.DEFAULT_HISTORY_DIR", self.history)
        history_patch.start()
        self.addCleanup(history_patch.stop)

    def snapshot(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.folder.iterdir() if path.is_file()}

    def prepare(self, count=2, csv=False):
        for name in ("a.txt", "b.txt", "c.txt")[:count]:
            (self.folder / name).write_text("原始内容：" + name, encoding="utf-8")
        if csv:
            (self.folder / "数据.csv").write_text("项目,数量\n测试,3\n", encoding="utf-8")
        self.original = self.snapshot()
        execute_plan(make_plan(self.folder, extension=".txt"), CONFIRM_TEXT)
        self.operation_id = HistoryStore.read_history()[0]["id"]
        return self.operation_id

    def operation(self):
        return next(row for row in HistoryStore.read_history() if row["id"] == self.operation_id)

    def undo(self):
        plan = make_undo_plan(self.operation_id)
        return execute_undo(plan, confirmation_text(self.operation_id))

    def original_rows(self):
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return {table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("operations", "items", "events")}

    def change_sql(self, statement, parameters=()):
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(statement, parameters)

    def legacy_database(self):
        self.change_sql("DROP TABLE undo_events")
        self.change_sql("DROP TABLE undo_items")
        self.change_sql("DROP TABLE undo_attempts")
        self.change_sql("PRAGMA user_version=1")

    def test_normal_undo_preserves_contents_forward_history_and_reverse_order(self):
        self.prepare()
        forward = self.original_rows()
        plan = make_undo_plan(self.operation_id)
        self.assertEqual([item.target.name for item in plan.items], ["b.txt", "a.txt"])
        self.assertEqual(execute_undo(plan, confirmation_text(self.operation_id)), list(plan.items))
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual(self.original_rows(), forward)
        operation = self.operation()
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(operation["undo_status"], "undone")
        attempt = operation["undo_attempts"][0]
        self.assertEqual(attempt["effective_status"], "completed")
        self.assertEqual([item["status"] for item in attempt["items"]], ["success", "success"])
        self.assertTrue(attempt["created_at"] and attempt["finished_at"])
        self.assertTrue(all(item["prepared_at"] and item["finished_at"] for item in attempt["items"]))
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            kinds = [row[0] for row in connection.execute("SELECT kind FROM undo_events ORDER BY id")]
        self.assertEqual(kinds, ["undo_started", "undo_intent_saved", "undo_success", "undo_intent_saved", "undo_success", "completed"])

    def test_default_undo_cli_only_previews_without_writing_database(self):
        self.prepare()
        before = self.snapshot()
        database_before = self.database.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.input") as prompt:
            self.assertEqual(main(["--undo", self.operation_id]), 0)
        prompt.assert_not_called()
        self.assertIn("撤销预览", output.getvalue())
        self.assertIn("学习文件002.txt → b.txt", output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_direct_undo_requires_confirmation_with_operation_id(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        before = self.snapshot()
        for text in ("yes", "确认撤销", "确认重命名", "确认撤销 " + "0" * 32):
            with self.subTest(text=text), self.assertRaises(UndoError):
                execute_undo(plan, text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_cli_exact_confirmation_executes_only_temp_files(self):
        self.prepare(csv=True)
        with patch("builtins.input", return_value=confirmation_text(self.operation_id)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--undo", self.operation_id, "--apply"]), 0)
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual(len(list(self.folder.iterdir())), 3)

    def test_cancel_eof_and_keyboard_interrupt_create_no_undo_attempt(self):
        self.prepare()
        before = self.snapshot()
        for failure in (None, EOFError, KeyboardInterrupt):
            with self.subTest(failure=failure):
                with patch("builtins.input", return_value="取消", side_effect=failure), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--undo", self.operation_id, "--apply"]), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_invalid_id_is_rejected_without_creating_history(self):
        for operation_id in ("", "123", "../anything", "x" * 32, "'; DROP TABLE items; --"):
            with self.subTest(operation_id=operation_id), self.assertRaises(UndoError):
                make_undo_plan(operation_id)
        self.assertFalse(self.history.exists())

    def test_unknown_id_never_falls_back_to_latest_operation(self):
        self.prepare()
        before = self.snapshot()
        with self.assertRaises(UndoError):
            make_undo_plan("0" * 32)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_repeat_undo_changes_nothing_and_does_not_prompt_or_add_history(self):
        self.prepare()
        self.undo()
        before = self.database.read_bytes()
        with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--undo", self.operation_id, "--apply"]), 0)
        prompt.assert_not_called()
        self.assertIn("已经全部撤销", output.getvalue())
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(len(self.operation()["undo_attempts"]), 1)

    def test_original_name_occupied_by_file_blocks_entire_batch(self):
        self.prepare()
        (self.folder / "a.txt").write_text("不能覆盖的另一个文件", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(UndoError, "占用"):
            make_undo_plan(self.operation_id)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_original_name_occupied_by_directory_blocks_batch(self):
        self.prepare()
        (self.folder / "a.txt").mkdir()
        before = self.snapshot()
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.folder / "a.txt").is_dir())

    def test_case_insensitive_conflict_is_rejected(self):
        self.prepare()
        (self.folder / "A.TXT").write_text("另一个文件", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)
        self.assertEqual(self.snapshot(), before)

    def test_deleted_source_blocks_entire_batch(self):
        self.prepare()
        (self.folder / "学习文件001.txt").unlink()
        before = self.snapshot()
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_changed_content_with_same_size_and_mtime_is_detected_by_sha256(self):
        self.prepare()
        source = self.folder / "学习文件001.txt"
        info = source.stat()
        content = source.read_bytes()
        source.write_bytes(content[:-1] + (b"0" if content[-1:] != b"0" else b"1"))
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaisesRegex(UndoError, "SHA256"):
            make_undo_plan(self.operation_id)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_replaced_file_with_identical_content_and_mtime_is_rejected(self):
        self.prepare()
        source = self.folder / "学习文件001.txt"
        info = source.stat()
        content = source.read_bytes()
        source.rename(self.root / "原件留存.txt")
        source.write_bytes(content)
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(source.stat().st_ino, info.st_ino)
        with self.assertRaisesRegex(UndoError, "身份"):
            make_undo_plan(self.operation_id)

    def test_timestamp_only_change_is_rejected(self):
        self.prepare()
        source = self.folder / "学习文件001.txt"
        info = source.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        with self.assertRaisesRegex(UndoError, "修改时间"):
            make_undo_plan(self.operation_id)

    def test_multiple_hardlinks_are_rejected(self):
        self.prepare()
        os.link(self.folder / "学习文件001.txt", self.root / "文件别名.txt")
        with self.assertRaisesRegex(UndoError, "硬链接"):
            make_undo_plan(self.operation_id)

    def test_hardlink_added_after_undo_preview_is_rejected_without_changes(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        source = self.folder / "学习文件001.txt"
        alias = self.root / "预览后新增别名.txt"
        os.link(source, alias)
        before = self.snapshot()
        database_before = self.database.read_bytes()
        with patch("undo_tool.os.rename") as rename:
            with self.assertRaisesRegex(UndoError, "硬链接"):
                execute_undo(plan, confirmation_text(self.operation_id))
            rename.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual(self.operation()["undo_attempts"], [])
        self.assertEqual(source.read_bytes(), alias.read_bytes())
        self.assertTrue(source.samefile(alias))

    def test_conflict_after_preview_is_rechecked_before_any_change(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        (self.folder / "a.txt").write_text("预览后新建的文件", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(UndoError):
            execute_undo(plan, confirmation_text(self.operation_id))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_modified_file_after_preview_blocks_execution(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        (self.folder / "学习文件001.txt").write_text("预览后被编辑", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(UndoError):
            execute_undo(plan, confirmation_text(self.operation_id))
        self.assertEqual(self.snapshot(), before)

    def test_stale_preview_cannot_execute_twice(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        execute_undo(plan, confirmation_text(self.operation_id))
        with self.assertRaises(UndoError):
            execute_undo(plan, confirmation_text(self.operation_id))
        self.assertEqual(len(self.operation()["undo_attempts"]), 1)

    def test_only_confirmed_successes_of_partial_forward_operation_are_undone(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.folder / name).write_text("内容：" + name, encoding="utf-8")
        before = self.snapshot()
        real_rename = os.rename

        def fail_second(source, target):
            if source.name == "b.txt":
                raise PermissionError("模拟正向操作第二项失败")
            real_rename(source, target)

        with patch("rename_tool.os.rename", side_effect=fail_second), self.assertRaises(ExecutionError):
            execute_plan(make_plan(self.folder), CONFIRM_TEXT)
        self.operation_id = HistoryStore.read_history()[0]["id"]
        plan = make_undo_plan(self.operation_id)
        self.assertEqual([item.target.name for item in plan.items], ["a.txt"])
        execute_undo(plan, confirmation_text(self.operation_id))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([item["status"] for item in self.operation()["items"]], ["success", "failed", "planned"])

    def test_pending_forward_operation_cannot_be_undone(self):
        (self.folder / "a.txt").write_text("新建临时内容", encoding="utf-8")
        with patch("rename_tool.os.rename", side_effect=RuntimeError("模拟中断")), self.assertRaises(ExecutionError):
            execute_plan(make_plan(self.folder), CONFIRM_TEXT)
        self.operation_id = HistoryStore.read_history()[0]["id"]
        with self.assertRaisesRegex(UndoError, "待核查"):
            make_undo_plan(self.operation_id)

    def test_partial_undo_is_recorded_and_new_preview_selects_only_remaining_item(self):
        self.prepare()
        real_rename = os.rename

        def fail_second(source, target):
            if target.name == "a.txt":
                raise PermissionError("模拟撤销第二项失败")
            real_rename(source, target)

        with patch("undo_tool.os.rename", side_effect=fail_second) as rename:
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
            self.assertEqual(rename.call_count, 2)
        self.assertFalse(caught.exception.needs_review)
        self.assertEqual([item.target.name for item in caught.exception.completed], ["b.txt"])
        operation = self.operation()
        self.assertEqual(operation["undo_status"], "partially_undone")
        self.assertEqual([item["status"] for item in operation["undo_attempts"][0]["items"]], ["success", "failed"])
        self.assertEqual(operation["undo_attempts"][0]["status"], "partial_failure")
        next_plan = make_undo_plan(self.operation_id)
        self.assertEqual([item.target.name for item in next_plan.items], ["a.txt"])
        execute_undo(next_plan, confirmation_text(self.operation_id))
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual(self.operation()["undo_status"], "undone")
        self.assertEqual(len(self.operation()["undo_attempts"]), 2)

    def test_known_undo_failure_does_not_rename_or_claim_success(self):
        self.prepare()
        before = self.snapshot()
        with patch("undo_tool.os.rename", side_effect=PermissionError("模拟拒绝访问")):
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
        self.assertFalse(caught.exception.needs_review)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([item["status"] for item in self.operation()["undo_attempts"][0]["items"]], ["failed", "planned"])

    def test_exception_after_real_undo_is_pending_and_blocks_undo_and_forward(self):
        self.prepare()
        real_rename = os.rename

        def undo_then_raise(source, target):
            real_rename(source, target)
            raise RuntimeError("模拟文件已恢复但程序中断")

        with patch("undo_tool.os.rename", side_effect=undo_then_raise) as rename:
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
            self.assertEqual(rename.call_count, 1)
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(caught.exception.completed, [])
        self.assertTrue((self.folder / "b.txt").exists())
        self.assertEqual(self.operation()["undo_status"], "needs_review")
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)
        with self.assertRaises(HistoryError):
            execute_plan(make_plan(self.folder, prefix="下一轮"), CONFIRM_TEXT)

    def test_keyboard_interrupt_keeps_pending_undo_record(self):
        self.prepare()
        with patch("undo_tool.os.rename", side_effect=KeyboardInterrupt):
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(self.operation()["undo_attempts"][0]["items"][0]["effective_status"], "needs_review")

    def crash_child(self, after_rename):
        self.prepare()
        script = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import operation_history, undo_tool
operation_history.DEFAULT_HISTORY_DIR = Path(sys.argv[2])
plan = undo_tool.make_undo_plan(sys.argv[3])
real_rename = os.rename
def crash(source, target):
    if sys.argv[4] == 'after':
        real_rename(source, target)
    os._exit(92)
undo_tool.os.rename = crash
undo_tool.execute_undo(plan, undo_tool.confirmation_text(plan.operation_id))
'''
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script,
                                 str(Path(__file__).parent), str(self.history), self.operation_id,
                                 "after" if after_rename else "before"], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 92, result.stderr)
        self.assertEqual((self.folder / "b.txt").exists(), after_rename)
        self.assertEqual((self.folder / "学习文件002.txt").exists(), not after_rename)
        self.assertTrue((self.folder / "学习文件001.txt").exists())
        attempt = self.operation()["undo_attempts"][0]
        self.assertEqual(attempt["effective_status"], "needs_review")
        self.assertEqual([item["status"] for item in attempt["items"]], ["in_progress", "planned"])
        self.assertEqual(self.operation()["status"], "completed")

    def test_process_exit_before_undo_is_not_guessed_to_be_failure(self):
        self.crash_child(after_rename=False)

    def test_process_exit_after_undo_is_not_guessed_to_be_success(self):
        self.crash_child(after_rename=True)

    def test_undo_intent_commit_failure_prevents_file_changes(self):
        self.prepare()
        before = self.snapshot()
        commit = HistoryStore._commit

        def fail_intent(store):
            if store.connection.execute("SELECT COUNT(*) FROM undo_items WHERE status='in_progress'").fetchone()[0]:
                raise sqlite3.OperationalError("模拟撤销意图写入失败")
            commit(store)

        with patch.object(HistoryStore, "_commit", fail_intent), patch("undo_tool.os.rename") as rename:
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
            rename.assert_not_called()
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_status"], "needs_review")

    def test_undo_success_commit_failure_leaves_pending_after_real_name_restore(self):
        self.prepare()
        commit = HistoryStore._commit
        real_rename = os.rename

        def fail_success(store):
            if store.connection.execute("SELECT COUNT(*) FROM undo_items WHERE status='success'").fetchone()[0]:
                raise sqlite3.OperationalError("模拟撤销结果提交失败")
            commit(store)

        with patch.object(HistoryStore, "_commit", fail_success), patch("undo_tool.os.rename", wraps=real_rename) as rename:
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
            self.assertEqual(rename.call_count, 1)
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(caught.exception.completed, [])
        self.assertTrue((self.folder / "b.txt").exists())
        self.assertEqual(self.operation()["undo_attempts"][0]["items"][0]["status"], "in_progress")

    def test_failure_to_record_undo_failure_is_pending(self):
        self.prepare()
        with patch("undo_tool.os.rename", side_effect=PermissionError("模拟拒绝访问")), patch.object(HistoryStore, "mark_undo_failed", side_effect=HistoryError("失败结果未保存")):
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(self.operation()["undo_status"], "needs_review")

    def test_final_undo_finish_failure_keeps_batch_pending_with_confirmed_item_successes(self):
        self.prepare()
        with patch.object(HistoryStore, "finish_undo", side_effect=HistoryError("整批结束记录失败")):
            with self.assertRaises(UndoExecutionError) as caught:
                self.undo()
        self.assertTrue(caught.exception.needs_review)
        self.assertEqual(len(caught.exception.completed), 2)
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual(self.operation()["undo_status"], "needs_review")
        self.assertEqual([item["status"] for item in self.operation()["undo_attempts"][0]["items"]], ["success", "success"])
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)

    def test_begin_undo_record_failure_prevents_file_changes(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        before = self.snapshot()
        with patch.object(HistoryStore, "begin_undo", side_effect=HistoryError("无法保存撤销计划")):
            with self.assertRaises(HistoryError):
                execute_undo(plan, confirmation_text(self.operation_id))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_old_schema_preview_is_read_only_and_execution_migrates_without_losing_history(self):
        self.prepare()
        self.legacy_database()
        forward_before = self.original_rows()
        bytes_before = self.database.read_bytes()
        plan = make_undo_plan(self.operation_id)
        self.assertEqual(self.database.read_bytes(), bytes_before)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        execute_undo(plan, confirmation_text(self.operation_id))
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.original_rows(), forward_before)
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual(self.operation()["undo_status"], "undone")

    def test_migration_failure_rolls_back_schema_and_does_not_change_files(self):
        self.prepare()
        self.legacy_database()
        plan = make_undo_plan(self.operation_id)
        before = self.snapshot()
        forward_before = self.original_rows()
        create = HistoryStore._create_undo_tables

        def create_then_fail(connection):
            create(connection)
            raise sqlite3.OperationalError("模拟升级中断")

        with patch.object(HistoryStore, "_create_undo_tables", staticmethod(create_then_fail)):
            with self.assertRaises(HistoryError):
                execute_undo(plan, confirmation_text(self.operation_id))
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'undo_%'").fetchone()[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.original_rows(), forward_before)

    def test_recorded_path_traversal_stream_and_reserved_names_are_rejected(self):
        self.prepare(1)
        before = self.snapshot()
        for bad_name in ("../outside.txt", "C:\\outside.txt", "..", "a:stream", "NUL.txt"):
            self.change_sql("UPDATE items SET original_name=? WHERE operation_id=?", (bad_name, self.operation_id))
            with self.subTest(name=bad_name), self.assertRaises(UndoError):
                make_undo_plan(self.operation_id)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_forged_preview_cannot_redirect_execution_outside_recorded_folder(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        forged = replace(plan, items=(replace(plan.items[0], target=self.root / "不允许的文件.txt"), *plan.items[1:]))
        before = self.snapshot()
        with self.assertRaises(UndoError):
            execute_undo(forged, confirmation_text(self.operation_id))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "不允许的文件.txt").exists())

    def test_invalid_file_identity_and_hash_records_are_rejected(self):
        self.prepare(1)
        self.change_sql("UPDATE items SET before_state='[1,0,10,20]' WHERE operation_id=?", (self.operation_id,))
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)
        self.change_sql("UPDATE items SET before_state='not json' WHERE operation_id=?", (self.operation_id,))
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)

    def test_undo_cli_rejects_mixed_forward_or_history_options(self):
        self.prepare()
        for extra in (["--extension", ".txt"], ["--folder", str(self.folder)], ["--history"], ["--prefix", "new"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["--undo", self.operation_id, *extra])
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.operation()["undo_attempts"], [])

    def test_unknown_undo_id_does_not_prompt_even_with_apply(self):
        self.prepare()
        with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--undo", "0" * 32, "--apply"]), 1)
        prompt.assert_not_called()

    def test_history_keeps_original_and_undo_attempt_after_reopening(self):
        self.prepare()
        self.undo()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--history"]), 0)
        self.assertIn(self.operation_id, output.getvalue())
        self.assertIn(self.operation()["undo_attempts"][0]["id"], output.getvalue())
        self.assertIn("已全部撤销", output.getvalue())
        self.assertIn("a.txt → 学习文件001.txt：成功", output.getvalue())
        self.assertIn("学习文件001.txt → a.txt：成功", output.getvalue())

    def test_later_rename_must_be_undone_before_earlier_rename(self):
        self.prepare(1)
        first_id = self.operation_id
        execute_plan(make_plan(self.folder, prefix="第二轮"), CONFIRM_TEXT)
        second_id = HistoryStore.read_history()[0]["id"]
        with self.assertRaises(UndoError):
            make_undo_plan(first_id)
        second_plan = make_undo_plan(second_id)
        execute_undo(second_plan, confirmation_text(second_id))
        first_plan = make_undo_plan(first_id)
        execute_undo(first_plan, confirmation_text(first_id))
        self.assertEqual(self.snapshot(), self.original)
        self.assertEqual(len(HistoryStore.read_history()), 2)
        self.assertTrue(all(row["undo_status"] == "undone" for row in HistoryStore.read_history()))

    def test_pending_other_operation_in_same_folder_also_blocks_undo(self):
        self.prepare()
        with patch("rename_tool.os.rename", side_effect=RuntimeError("另一批改名中断")), self.assertRaises(ExecutionError):
            execute_plan(make_plan(self.folder, prefix="另一批"), CONFIRM_TEXT)
        with self.assertRaisesRegex(UndoError, "待核查"):
            make_undo_plan(self.operation_id)

    def test_forward_failure_without_success_has_nothing_to_undo(self):
        (self.folder / "a.txt").write_text("临时内容", encoding="utf-8")
        with patch("rename_tool.os.rename", side_effect=PermissionError("拒绝访问")), self.assertRaises(ExecutionError):
            execute_plan(make_plan(self.folder), CONFIRM_TEXT)
        self.operation_id = HistoryStore.read_history()[0]["id"]
        plan = make_undo_plan(self.operation_id)
        self.assertEqual(plan.items, ())
        self.assertIn("没有成功", plan.reason)
        with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--undo", self.operation_id, "--apply"]), 0)
        prompt.assert_not_called()

    def test_database_open_failure_blocks_undo(self):
        self.prepare()
        plan = make_undo_plan(self.operation_id)
        before = self.snapshot()
        with patch("operation_history.sqlite3.connect", side_effect=sqlite3.OperationalError("模拟数据库打不开")):
            with self.assertRaises(HistoryError):
                execute_undo(plan, confirmation_text(self.operation_id))
        self.assertEqual(self.snapshot(), before)

    def test_recorded_protected_directory_cannot_be_used_for_undo(self):
        self.prepare()
        self.change_sql("UPDATE operations SET folder=? WHERE id=?", (str(self.history), self.operation_id))
        with self.assertRaises(UndoError):
            make_undo_plan(self.operation_id)

    def cli_process(self, arguments, input_text=""):
        script = r'''
import runpy, sys
from pathlib import Path
project = Path(sys.argv[1])
history = Path(sys.argv[2])
arguments = sys.argv[3:]
sys.path.insert(0, str(project))
import operation_history
operation_history.DEFAULT_HISTORY_DIR = history
sys.argv = [str(project / 'rename_tool.py'), *arguments]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        return subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script,
                               str(Path(__file__).parent), str(self.history), *arguments],
                              input=input_text, capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_independent_cli_preview_is_read_only(self):
        self.prepare()
        before = self.snapshot()
        database_before = self.database.read_bytes()
        result = self.cli_process(["--undo", self.operation_id])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("撤销预览", result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.database.read_bytes(), database_before)

    def test_independent_cli_applies_after_exact_confirmation_and_history_persists(self):
        self.prepare(csv=True)
        result = self.cli_process(["--undo", self.operation_id, "--apply"], confirmation_text(self.operation_id) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), self.original)
        history = self.cli_process(["--history"])
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertIn(self.operation_id, history.stdout)
        self.assertIn("已全部撤销", history.stdout)

    def test_independent_cli_reports_conflict_without_traceback_or_changes(self):
        self.prepare()
        (self.folder / "a.txt").write_text("不能覆盖的文件", encoding="utf-8")
        before = self.snapshot()
        result = self.cli_process(["--undo", self.operation_id, "--apply"], confirmation_text(self.operation_id) + "\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("拒绝撤销", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
