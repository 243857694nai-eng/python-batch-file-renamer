"""真实 Tk 控件测试；文件操作只发生在本测试新建的目录和账本内。"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import rename_gui
import rename_tool
from operation_history import HistoryStore
from rename_gui import RenameWindow
from rename_tool import CONFIRM_TEXT, file_digest


class RenameGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="历史自动测试_界面_", dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.folder = self.base / "测试数据"
        self.folder.mkdir()
        self.history = self.base / ".rename_history"
        self.addCleanup(patch.stopall)
        patch("operation_history.DEFAULT_HISTORY_DIR", self.history).start()
        self.dialog = patch("rename_gui.ask_confirmation", return_value=CONFIRM_TEXT).start()
        self.window = RenameWindow(self.root)
        self.addCleanup(self.window.destroy)
        self.window.folder.set(str(self.folder))
        self.window.extension.set(".txt")

    def write(self, name, content=b"test content"):
        path = self.folder / name
        path.write_bytes(content)
        return path

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.folder.iterdir() if path.is_file()}

    def preview(self):
        self.window.preview_button.invoke()
        return self.window.plan

    def apply(self):
        self.window.execute_button.invoke()

    def states(self):
        return [self.window.tree.set(row, "result") for row in self.window.tree.get_children()]

    def details(self):
        return self.window.details.get("1.0", "end")

    def assert_disabled(self):
        self.assertTrue(self.window.execute_button.instate(["disabled"]))

    def test_imports_do_not_open_window_rename_or_connect_database(self):
        script = '''from unittest.mock import patch
with patch("os.rename", side_effect=AssertionError("unexpected rename")), patch("sqlite3.connect", side_effect=AssertionError("unexpected database")), patch("tkinter.Tk", side_effect=AssertionError("unexpected window")):
    import rename_tool, operation_history, undo_tool, rename_gui
print("IMPORT_SAFE")
'''
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script],
                                cwd=Path(__file__).parent, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("IMPORT_SAFE", result.stdout)
        self.assertFalse(self.history.exists())

    def test_preview_uses_real_core_without_rename_or_database_write(self):
        self.write("a.txt")
        self.write("b.TXT", b"different")
        self.write("c.csv", b"csv")
        before = self.snapshot()
        with patch("os.rename", side_effect=AssertionError("preview cannot rename")), patch("sqlite3.connect", side_effect=AssertionError("preview cannot open database")):
            plan = self.preview()
        self.assertEqual(len(plan), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.states(), ["未执行", "未执行"])
        self.assertEqual(self.window.tree.set("0", "old"), "a.txt")
        self.assertEqual(self.window.tree.set("1", "new"), "学习文件002.TXT")
        self.assertTrue(all(len(item.sha256) == 64 for item in plan))
        self.assertFalse(self.history.exists())

    def test_execute_requires_preview(self):
        self.write("a.txt")
        with patch("rename_gui.execute_plan") as execute:
            self.window.apply()
        execute.assert_not_called()
        self.dialog.assert_not_called()
        self.assertFalse(self.history.exists())

    def test_cancel_or_inexact_confirmation_does_not_execute(self):
        self.write("a.txt")
        before = self.snapshot()
        plan = self.preview()
        with patch("rename_gui.execute_plan") as execute:
            for answer in (None, "", "确认", CONFIRM_TEXT + " "):
                self.dialog.return_value = answer
                self.apply()
                self.assertEqual(self.window.plan, plan)
            execute.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_confirmed_execution_preserves_other_types_and_records_hashes(self):
        self.write("a.txt")
        self.write("b.TXT", b"second")
        self.write("c.csv", b"csv")
        self.write("d.md", b"markdown")
        before = self.snapshot()
        plan = self.preview()
        self.apply()
        expected = {"学习文件001.txt": before["a.txt"], "学习文件002.TXT": before["b.TXT"],
                    "c.csv": before["c.csv"], "d.md": before["d.md"]}
        self.assertEqual(self.snapshot(), expected)
        operation = HistoryStore.read_history()[0]
        self.assertEqual(operation["status"], "completed")
        self.assertEqual([row["status"] for row in operation["items"]], ["success", "success"])
        self.assertEqual([row["sha256"] for row in operation["items"]], [item.sha256 for item in plan])
        self.assertEqual(self.states(), ["成功（已记录）"] * 2)
        self.assertIn("成功改名并记录 2", self.window.status.get())
        self.assertIn(str(self.folder), self.dialog.call_args.args[0])
        self.assert_disabled()

    def test_changing_either_setting_invalidates_preview_even_when_changed_back(self):
        self.write("a.txt")
        for variable, other in ((self.window.folder, str(self.base)), (self.window.extension, ".csv")):
            with self.subTest(setting=variable):
                self.preview()
                previous = variable.get()
                variable.set(other)
                variable.set(previous)
                self.assertEqual(self.window.plan, ())
                self.assertEqual(self.window.tree.get_children(), ())
                self.assert_disabled()

    def test_setting_change_during_confirmation_cannot_use_old_plan(self):
        self.write("a.txt")
        self.preview()
        def change_setting(*args, **kwargs):
            self.window.extension.set(".csv")
            return CONFIRM_TEXT
        self.dialog.side_effect = change_setting
        with patch("rename_gui.execute_plan") as execute:
            self.apply()
        execute.assert_not_called()
        self.assert_disabled()

    def test_same_size_same_mtime_content_change_rejects_old_preview(self):
        source = self.write("a.txt", b"old")
        self.preview()
        info = source.stat()
        source.write_bytes(b"NEW")
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.apply()
        self.assertIn("SHA256", self.details())
        self.assertEqual(self.snapshot(), {"a.txt": b"NEW"})
        self.assertFalse(self.history.exists())
        self.assert_disabled()

    def test_occupied_target_after_preview_is_never_overwritten(self):
        self.write("a.txt")
        self.preview()
        self.write("学习文件001.txt", b"must stay")
        before = self.snapshot()
        self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertIn("占用", self.details())
        self.assertFalse(self.history.exists())

    def test_source_directory_is_rejected_by_existing_protection(self):
        self.window.folder.set(str(Path(__file__).parent))
        self.window.extension.set(".py")
        self.preview()
        self.assertIn("程序自身所在目录受到保护", self.details())
        self.assert_disabled()
        self.assertFalse(self.history.exists())

    def test_hardlink_added_after_preview_is_rejected(self):
        source = self.write("a.txt")
        self.preview()
        alias = self.base / "别名.txt"
        os.link(source, alias)
        self.apply()
        self.assertIn("硬链接", self.details())
        self.assertTrue(source.samefile(alias))
        self.assertFalse(self.history.exists())

    def test_partial_known_failure_displays_success_failed_and_unexecuted(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(name)
        plan = self.preview()
        rename = os.rename
        def deny_second(source, target):
            if source == plan[1].source:
                raise PermissionError("模拟第二个文件拒绝访问")
            rename(source, target)
        with patch("rename_tool.os.rename", side_effect=deny_second):
            self.apply()
        self.assertEqual(self.states(), ["成功（已记录）", "失败", "未执行"])
        self.assertEqual(HistoryStore.read_history()[0]["status"], "partial_failure")
        self.assertIn("操作编号", self.details())
        self.assertTrue(plan[2].source.exists())
        self.assert_disabled()

    def test_postcheck_failure_displays_pending_and_stops_remaining(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(name)
        plan = self.preview()
        digest = rename_tool.file_digest
        def unreadable(path):
            if path == plan[1].target:
                raise PermissionError("模拟操作后读取失败")
            return digest(path)
        with patch("rename_tool.file_digest", side_effect=unreadable):
            self.apply()
        self.assertEqual(self.states(), ["成功（已记录）", "待核查", "未执行"])
        record = HistoryStore.read_history()[0]
        self.assertEqual(record["effective_status"], "needs_review")
        self.assertEqual([row["status"] for row in record["items"]], ["success", "in_progress", "planned"])
        self.assertTrue(plan[2].source.exists())
        self.assert_disabled()

    def test_success_record_write_failure_is_pending_not_success(self):
        self.write("a.txt")
        plan = self.preview()
        commit = HistoryStore._commit
        def fail_success(store):
            if store.connection.execute("SELECT COUNT(*) FROM items WHERE status='success'").fetchone()[0]:
                raise sqlite3.OperationalError("模拟写入失败")
            commit(store)
        with patch.object(HistoryStore, "_commit", fail_success):
            self.apply()
        self.assertEqual(self.states(), ["待核查"])
        self.assertEqual(HistoryStore.read_history()[0]["effective_status"], "needs_review")
        self.assertEqual(file_digest(plan[0].target), plan[0].sha256)

    def test_unexpected_execution_error_is_visible_and_does_not_retry(self):
        self.write("a.txt")
        self.preview()
        with patch("rename_gui.execute_plan", side_effect=RuntimeError("模拟意外异常")) as execute:
            self.apply()
            self.window.apply()
        execute.assert_called_once()
        self.assertIn("待核查", self.details() + self.window.status.get())
        self.assert_disabled()

    def test_invalid_or_empty_filter_and_noop_have_no_executable_plan(self):
        self.write("学习文件001.txt")
        for extension in (".txt", ".csv", "*.txt"):
            self.window.extension.set(extension)
            self.preview()
            self.assert_disabled()
        self.assertIn("扩展名", self.details())
        self.assertFalse(self.history.exists())

    def test_empty_filter_keeps_existing_all_file_default(self):
        self.write("a.txt")
        self.write("b.csv")
        self.window.extension.set("")
        self.assertEqual(len(self.preview()), 2)
        self.assertFalse(self.history.exists())

    def test_folder_chooser_cancel_keeps_preview_and_selection_invalidates_it(self):
        self.write("a.txt")
        plan = self.preview()
        with patch("rename_gui.filedialog.askdirectory", return_value=""):
            self.window.choose_button.invoke()
        self.assertEqual(self.window.plan, plan)
        with patch("rename_gui.filedialog.askdirectory", return_value=str(self.base)):
            self.window.choose_button.invoke()
        self.assertEqual(self.window.plan, ())
        self.assert_disabled()

    def test_busy_window_rejects_reentrant_actions(self):
        self.write("a.txt")
        plan = self.preview()
        self.window.set_busy(True)
        with patch("rename_gui.make_plan") as make, patch("rename_gui.execute_plan") as execute, patch("rename_gui.filedialog.askdirectory") as chooser:
            self.window.preview()
            self.window.apply()
            self.window.choose_folder()
        make.assert_not_called()
        execute.assert_not_called()
        chooser.assert_not_called()
        self.assertEqual(self.window.plan, plan)
        self.window.set_busy(False)

    def test_actual_confirmation_dialog_has_chinese_buttons_and_no_prefilled_consent(self):
        observations = []
        def confirm():
            dialog = next(child for child in self.root.winfo_children() if isinstance(child, rename_gui.ConfirmDialog))
            observations.append(dialog.answer.get())
            captions = [str(button.cget("text")) for frame in dialog.winfo_children()
                        for button in frame.winfo_children() if button.winfo_class() == "TButton"]
            observations.append(captions)
            dialog.answer.insert(0, CONFIRM_TEXT)
            dialog.ok()
        self.root.after(50, confirm)
        answer = rename_gui.ConfirmDialog(self.root, "请核对测试预览后输入确认文字。").result
        self.assertEqual(observations, ["", ["确认执行", "取消"]])
        self.assertEqual(answer, CONFIRM_TEXT)
        self.assertFalse(self.history.exists())

    def test_actual_confirmation_dialog_cancel_returns_no_consent(self):
        def cancel():
            dialog = next(child for child in self.root.winfo_children() if isinstance(child, rename_gui.ConfirmDialog))
            dialog.cancel()
        self.root.after(50, cancel)
        self.assertIsNone(rename_gui.ConfirmDialog(self.root, "此处只测试取消。").result)
        self.assertFalse(self.history.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
