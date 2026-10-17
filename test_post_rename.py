"""操作后复核：所有故障只作用于本用例新建的文件和账本。"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rename_tool
from operation_history import HistoryStore
from rename_tool import CONFIRM_TEXT, ExecutionError, execute_plan, file_digest, file_snapshot, main, make_plan
from undo_tool import UndoExecutionError, confirmation_text, execute_undo, make_undo_plan


class PostRenameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="历史自动测试_复核_", dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    @contextlib.contextmanager
    def scenario(self, undo):
        root = self.root / ("撤销" if undo else "正向")
        root.mkdir()
        folder = root / "数据"
        folder.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (folder / name).write_bytes(("payload-" + name + "\n").encode("utf-8"))
        history = root / ".rename_history"
        with patch("operation_history.DEFAULT_HISTORY_DIR", history):
            forward = make_plan(folder)
            if undo:
                execute_plan(forward, CONFIRM_TEXT)
                operation_id = HistoryStore.read_history()[0]["id"]
                undo_plan = make_undo_plan(operation_id)
                plan = undo_plan.items
                action = lambda: execute_undo(undo_plan, confirmation_text(operation_id))
            else:
                operation_id = None
                plan = forward
                action = lambda: execute_plan(plan, CONFIRM_TEXT)
            yield SimpleNamespace(root=root, folder=folder, history=history, undo=undo,
                                  plan=plan, action=action, operation_id=operation_id,
                                  original_hashes=[item.sha256 for item in forward], real_rename=os.rename,
                                  error_type=UndoExecutionError if undo else ExecutionError)

    def assert_pending(self, case, error, successful=0):
        self.assertTrue(error.needs_review)
        self.assertEqual(len(error.completed), successful)
        operation = HistoryStore.read_history()[0]
        self.assertEqual([row["sha256"] for row in operation["items"]], case.original_hashes)
        if case.undo:
            self.assertEqual(operation["status"], "completed")
            self.assertEqual(operation["undo_status"], "needs_review")
            record = operation["undo_attempts"][-1]
        else:
            record = operation
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["effective_status"], "needs_review")
        self.assertIsNone(record["finished_at"])
        self.assertEqual([row["status"] for row in record["items"]],
                         ["success"] * successful + ["in_progress"] + ["planned"] * (2 - successful))
        for item in case.plan[successful + 1:]:
            self.assertTrue(item.source.exists())

    def fail_after_rename(self, case, change, position=1):
        calls = []

        def rename_then_change(source, target):
            case.real_rename(source, target)
            calls.append((source, target))
            if len(calls) == position:
                change(source, target)

        with patch("rename_tool.os.rename", side_effect=rename_then_change):
            with self.assertRaises(case.error_type) as caught:
                case.action()
        self.assertEqual(len(calls), position)
        self.assert_pending(case, caught.exception, successful=position - 1)

    def change_same_size_and_mtime(self, path):
        info = path.stat()
        content = path.read_bytes()
        path.write_bytes(content[:-1] + b"X")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    def test_normal_forward_and_undo_finish_with_verified_targets(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                self.assertEqual(len(case.action()), 3)
                operation = HistoryStore.read_history()[0]
                record = operation["undo_attempts"][-1] if undo else operation
                self.assertEqual(record["status"], "completed")
                self.assertEqual([row["status"] for row in record["items"]], ["success"] * 3)
                self.assertEqual(len(list(case.folder.iterdir())), 3)
                for item in case.plan:
                    self.assertFalse(item.source.exists())
                    self.assertEqual(file_digest(item.target), item.sha256)

    def test_replaced_target_with_identical_bytes_and_mtime_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                saved = case.root / "保留原件.txt"

                def replace(source, target):
                    info = target.stat()
                    content = target.read_bytes()
                    case.real_rename(target, saved)
                    target.write_bytes(content)
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
                    self.assertNotEqual(target.stat().st_ino, info.st_ino)

                self.fail_after_rename(case, replace)
                self.assertEqual(saved.read_bytes(), case.plan[0].target.read_bytes())

    def test_same_size_and_mtime_content_change_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                self.fail_after_rename(case, lambda source, target: self.change_same_size_and_mtime(target))
                self.assertEqual(file_snapshot(case.plan[0].target), case.plan[0].snapshot)
                self.assertNotEqual(file_digest(case.plan[0].target), case.plan[0].sha256)

    def test_target_size_change_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                self.fail_after_rename(case, lambda source, target: target.write_bytes(target.read_bytes() + b"extra"))

    def test_missing_target_stays_pending_without_restoring_or_deleting(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                saved = case.root / "移走后保留.txt"
                self.fail_after_rename(case, lambda source, target: case.real_rename(target, saved))
                self.assertTrue(saved.exists())
                self.assertFalse(case.plan[0].target.exists())
                self.assertFalse(case.plan[0].source.exists())

    def test_directory_instead_of_target_file_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                saved = case.root / "保留文件.txt"

                def replace_with_directory(source, target):
                    case.real_rename(target, saved)
                    target.mkdir()

                self.fail_after_rename(case, replace_with_directory)
                self.assertTrue(saved.is_file())
                self.assertTrue(case.plan[0].target.is_dir())

    def test_target_hardlink_added_after_rename_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                alias = case.root / "保留别名.txt"
                self.fail_after_rename(case, lambda source, target: os.link(target, alias))
                self.assertTrue(alias.samefile(case.plan[0].target))
                self.assertEqual(alias.stat().st_nlink, 2)

    def test_recreated_source_is_preserved_and_result_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                self.fail_after_rename(case, lambda source, target: source.write_bytes(b"external new file"))
                self.assertEqual(case.plan[0].source.read_bytes(), b"external new file")
                self.assertEqual(file_digest(case.plan[0].target), case.plan[0].sha256)

    def test_postcheck_read_failure_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                real_digest = rename_tool.file_digest

                def unreadable_target(path):
                    if path == case.plan[0].target:
                        raise PermissionError("模拟操作后无法读取目标")
                    return real_digest(path)

                with patch("rename_tool.file_digest", side_effect=unreadable_target):
                    with self.assertRaises(case.error_type) as caught:
                        case.action()
                self.assert_pending(case, caught.exception)
                self.assertTrue(case.plan[0].target.is_file())

    def test_directory_identity_change_is_detected(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                changed = [False]
                real_identity = rename_tool.directory_identity

                def identity(folder):
                    value = real_identity(folder)
                    return (value[0], value[1] + 1) if changed[0] else value

                with patch("rename_tool.directory_identity", side_effect=identity):
                    self.fail_after_rename(case, lambda source, target: changed.__setitem__(0, True))

    def test_partial_success_then_postcheck_failure_stops_remaining_files(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                self.fail_after_rename(case, lambda source, target: self.change_same_size_and_mtime(target), position=2)
                self.assertEqual(file_digest(case.plan[0].target), case.plan[0].sha256)
                self.assertTrue(case.plan[2].source.exists())

    def test_success_commit_failure_after_verified_rename_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                commit = HistoryStore._commit
                table = "undo_items" if undo else "items"

                def fail_success(store):
                    if store.connection.execute("SELECT COUNT(*) FROM " + table + " WHERE status='success'").fetchone()[0]:
                        raise sqlite3.OperationalError("模拟复核后成功记录写入失败")
                    commit(store)

                with patch.object(HistoryStore, "_commit", fail_success):
                    with self.assertRaises(case.error_type) as caught:
                        case.action()
                self.assert_pending(case, caught.exception)
                self.assertEqual(file_digest(case.plan[0].target), case.plan[0].sha256)

    def test_access_time_and_directory_timestamp_changes_do_not_block_success(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                def rename_and_touch_access_time(source, target):
                    case.real_rename(source, target)
                    info = target.stat()
                    os.utime(target, ns=(info.st_atime_ns + 2_000_000_000, info.st_mtime_ns))
                    directory = case.folder.stat()
                    os.utime(case.folder, ns=(directory.st_atime_ns, directory.st_mtime_ns + 2_000_000_000))

                with patch("rename_tool.os.rename", side_effect=rename_and_touch_access_time):
                    self.assertEqual(len(case.action()), 3)
                operation = HistoryStore.read_history()[0]
                record = operation["undo_attempts"][-1] if undo else operation
                self.assertEqual(record["status"], "completed")

    def test_content_modification_time_change_is_conservatively_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                def touch_write_time(source, target):
                    info = target.stat()
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))

                self.fail_after_rename(case, touch_write_time)
                self.assertEqual(file_digest(case.plan[0].target), case.plan[0].sha256)

    def test_cli_reports_postcheck_problem_as_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                real_digest = rename_tool.file_digest

                def unreadable_target(path):
                    if path == case.plan[0].target:
                        raise PermissionError("模拟复核读取失败")
                    return real_digest(path)

                args = ["--undo", case.operation_id, "--apply"] if undo else ["--folder", str(case.folder), "--apply"]
                answer = confirmation_text(case.operation_id) if undo else CONFIRM_TEXT
                output = io.StringIO()
                with patch("rename_tool.file_digest", side_effect=unreadable_target), patch("builtins.input", return_value=answer), contextlib.redirect_stdout(output):
                    self.assertEqual(main(args), 1)
                self.assertIn("待核查", output.getvalue())
                self.assertNotIn("成功重命名", output.getvalue())
                self.assertNotIn("成功撤销", output.getvalue())

    def test_directory_link_detection_after_rename_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                changed = [False]
                original = Path.is_junction

                def is_junction(path):
                    return (changed[0] and path == case.folder) or original(path)

                with patch.object(Path, "is_junction", is_junction):
                    self.fail_after_rename(case, lambda source, target: changed.__setitem__(0, True))

    def test_target_changes_while_postcheck_hash_finishes_stays_pending(self):
        for undo in (False, True):
            with self.subTest(undo=undo), self.scenario(undo) as case:
                real_digest = rename_tool.file_digest

                def digest_then_change(path):
                    value = real_digest(path)
                    if path == case.plan[0].target:
                        path.write_bytes(path.read_bytes() + b"new data")
                    return value

                with patch("rename_tool.file_digest", side_effect=digest_then_change):
                    with self.assertRaises(case.error_type) as caught:
                        case.action()
                self.assert_pending(case, caught.exception)


if __name__ == "__main__":
    unittest.main(verbosity=2)
