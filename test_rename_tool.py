"""安全测试：真实创建测试文件；重命名操作使用模拟，不执行实际重命名。"""

import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rename_tool import CONFIRM_TEXT, ExecutionError, RenameError, execute_plan, file_digest, file_snapshot, main, make_plan, validate_plan


class RenameToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="自动测试_", dir=Path(__file__).parent)
        self.folder = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        # 所有旧测试也使用独立临时账本，绝不写入项目的正式历史。
        history_patch = patch("operation_history.DEFAULT_HISTORY_DIR", self.folder / ".rename_history")
        history_patch.start()
        self.addCleanup(history_patch.stop)

    def write_file(self, name, content="测试内容"):
        path = self.folder / name
        path.write_text(content, encoding="utf-8")
        return path

    def content_snapshot(self):
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.folder.iterdir() if path.is_file()
        }

    def test_extension_filter_selects_only_txt_and_numbers_continuously(self):
        self.write_file("a.csv", "数据,100")
        self.write_file("b.txt", "第一段文字")
        self.write_file("c.md", "# 说明")
        self.write_file("d.TXT", "另一段文字")
        before = self.content_snapshot()
        plan = make_plan(self.folder, extension=".txt")
        self.assertEqual([item.source.name for item in plan], ["b.txt", "d.TXT"])
        self.assertEqual([item.target.name for item in plan], ["学习文件001.txt", "学习文件002.TXT"])
        self.assertEqual(self.content_snapshot(), before)

    def test_omitted_extension_preserves_original_all_file_behavior(self):
        for name in ("a.csv", "b.txt", "c.md", "d"):
            self.write_file(name)
        plan = make_plan(self.folder)
        self.assertEqual([item.source.name for item in plan], ["a.csv", "b.txt", "c.md", "d"])
        self.assertEqual([item.target.name for item in plan], ["学习文件001.csv", "学习文件002.txt", "学习文件003.md", "学习文件004"])

    def test_extension_accepts_dotless_and_case_variants(self):
        self.write_file("a.TXT")
        self.write_file("b.csv")
        for extension in (".txt", "txt", ".TXT", "TXT", " .txt "):
            with self.subTest(extension=extension):
                self.assertEqual([item.source.name for item in make_plan(self.folder, extension=extension)], ["a.TXT"])

    def test_extension_matches_last_suffix_and_preserves_original_suffixes(self):
        self.write_file("a.v1.txt")
        self.write_file("b.tar.gz")
        txt_plan = make_plan(self.folder, extension=".txt")
        self.assertEqual(txt_plan[0].target.name, "学习文件001.v1.txt")
        gz_plan = make_plan(self.folder, extension=".gz")
        self.assertEqual(gz_plan[0].target.name, "学习文件001.tar.gz")

    def test_invalid_extension_is_rejected_instead_of_processing_all_files(self):
        self.write_file("a.txt")
        for extension in ("", " ", ".", ".tar.gz", "*.txt", "tx?t", "x/y", "x\\y", "x:", "tx t"):
            with self.subTest(extension=extension), self.assertRaises(RenameError):
                make_plan(self.folder, extension=extension)

    def test_filter_still_checks_target_names_occupied_by_folders(self):
        self.write_file("a.txt")
        self.write_file("b.csv")
        (self.folder / "学习文件001.TXT").mkdir()
        with self.assertRaises(RenameError):
            make_plan(self.folder, extension=".txt")

    def test_filtered_cli_preview_excludes_other_types_and_does_not_rename(self):
        self.write_file("a.txt")
        self.write_file("b.csv")
        before = self.content_snapshot()
        output = io.StringIO()
        with patch("rename_tool.os.rename") as rename, contextlib.redirect_stdout(output):
            result = main(["--folder", str(self.folder), "--extension", ".txt"])
        rename.assert_not_called()
        self.assertEqual(result, 0)
        self.assertIn("a.txt  →  学习文件001.txt", output.getvalue())
        self.assertNotIn("b.csv", output.getvalue())
        self.assertEqual(self.content_snapshot(), before)

    def test_filtered_apply_routes_only_matching_files_to_simulated_rename(self):
        self.write_file("a.csv", "数据,200")
        self.write_file("b.txt", "第一份文本")
        self.write_file("c.md", "# 原始说明")
        self.write_file("d.TXT", "第二份文本")
        before = self.content_snapshot()
        with patch("builtins.input", return_value=CONFIRM_TEXT), patch("rename_tool.os.name", "nt"), patch("rename_tool.os.rename") as rename, contextlib.redirect_stdout(io.StringIO()):
            result = main(["--folder", str(self.folder), "--extension", ".txt", "--apply"])
        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args for call in rename.call_args_list],
            [(self.folder / "b.txt", self.folder / "学习文件001.txt"),
             (self.folder / "d.TXT", self.folder / "学习文件002.TXT")],
        )
        self.assertEqual(self.content_snapshot(), before)

    def test_filtered_apply_still_requires_confirmation(self):
        self.write_file("a.txt")
        self.write_file("b.csv")
        before = self.content_snapshot()
        with patch("builtins.input", return_value="取消"), patch("rename_tool.os.rename") as rename, contextlib.redirect_stdout(io.StringIO()):
            result = main(["--folder", str(self.folder), "--extension", ".txt", "--apply"])
        self.assertEqual(result, 0)
        rename.assert_not_called()
        self.assertEqual(self.content_snapshot(), before)

    def test_no_matching_files_does_not_prompt_or_rename(self):
        self.write_file("a.csv")
        before = self.content_snapshot()
        with patch("builtins.input") as prompt, patch("rename_tool.os.rename") as rename, contextlib.redirect_stdout(io.StringIO()):
            result = main(["--folder", str(self.folder), "--extension", ".txt", "--apply"])
        self.assertEqual(result, 0)
        prompt.assert_not_called()
        rename.assert_not_called()
        self.assertEqual(self.content_snapshot(), before)

    def test_filtered_plan_rechecks_new_target_collision_before_execution(self):
        self.write_file("a.txt")
        self.write_file("b.csv")
        plan = make_plan(self.folder, extension=".txt")
        self.write_file("学习文件001.txt", "后来出现的文件，不能覆盖")
        with patch("rename_tool.os.name", "nt"), patch("rename_tool.os.rename") as rename:
            with self.assertRaises(RenameError):
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()

    def test_preview_preserves_contents_and_names_and_skips_subfolders(self):
        first = self.write_file("b.txt")
        self.write_file("a.tar.gz")
        (self.folder / "子目录").mkdir()
        plan = make_plan(self.folder)
        self.assertEqual([item.target.name for item in plan], ["学习文件001.tar.gz", "学习文件002.txt"])
        self.assertTrue(first.exists())
        self.assertEqual(first.read_text(encoding="utf-8"), "测试内容")
        self.assertFalse((self.folder / "学习文件002.txt").exists())

    def test_existing_target_folder_blocks_plan(self):
        self.write_file("a.txt")
        (self.folder / "学习文件001.txt").mkdir()
        with self.assertRaises(RenameError):
            make_plan(self.folder)

    def test_windows_case_insensitive_collision(self):
        self.write_file("z.txt")
        (self.folder / "NEW001.TXT").mkdir()
        with self.assertRaises(RenameError):
            make_plan(self.folder, "new")

    def test_invalid_prefixes_and_start(self):
        self.write_file("a.txt")
        for prefix in ("", "../x", "x\\y", "x:", "x.", "x ", "x\n"):
            with self.subTest(prefix=prefix), self.assertRaises(RenameError):
                make_plan(self.folder, prefix)
        with self.assertRaises(RenameError):
            make_plan(self.folder, start=0)

    def test_start_number(self):
        self.write_file("a.csv")
        self.assertEqual(make_plan(self.folder, "报表", 10)[0].target.name, "报表010.csv")

    def test_windows_reserved_device_name(self):
        self.write_file("a.txt")
        with self.assertRaises(RenameError):
            make_plan(self.folder, "CON.练习")

    def test_confirmation_is_mandatory(self):
        self.write_file("a.txt")
        plan = make_plan(self.folder)
        with patch("rename_tool.os.rename") as rename:
            with self.assertRaises(RenameError):
                execute_plan(plan, "yes")
            rename.assert_not_called()

    def test_changed_file_blocks_execution(self):
        source = self.write_file("a.txt")
        plan = make_plan(self.folder)
        source.write_text("预览后被修改的内容", encoding="utf-8")
        with patch("rename_tool.os.name", "nt"), patch("rename_tool.os.rename") as rename:
            with self.assertRaises(RenameError):
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()

    def test_new_collision_blocks_execution(self):
        self.write_file("a.txt")
        plan = make_plan(self.folder)
        self.write_file("学习文件001.txt", "不能覆盖的内容")
        with patch("rename_tool.os.name", "nt"), patch("rename_tool.os.rename") as rename:
            with self.assertRaises(RenameError):
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()

    def test_cancel_changes_nothing(self):
        source = self.write_file("a.txt")
        with patch("builtins.input", return_value="取消"), contextlib.redirect_stdout(io.StringIO()):
            result = main(["--folder", str(self.folder), "--apply"])
        self.assertEqual(result, 0)
        self.assertTrue(source.exists())

    def test_execution_calls_are_simulated(self):
        self.write_file("a.txt")
        plan = make_plan(self.folder)
        with patch("rename_tool.os.name", "nt"), patch("rename_tool.os.rename") as rename:
            completed = execute_plan(plan, CONFIRM_TEXT)
            rename.assert_called_once_with(plan[0].source, plan[0].target)
        self.assertEqual(completed, plan)
        self.assertTrue(plan[0].source.exists())

    def test_failure_stops_after_completed_items(self):
        self.write_file("a.txt")
        self.write_file("b.txt")
        self.write_file("c.txt")
        plan = make_plan(self.folder)
        with patch("rename_tool.os.name", "nt"), patch("rename_tool.os.rename", side_effect=[None, PermissionError("模拟拒绝访问")]) as rename:
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(plan, CONFIRM_TEXT)
            self.assertEqual(rename.call_count, 2)
        self.assertEqual(caught.exception.completed, [plan[0]])

    def test_already_named_file_is_not_renamed(self):
        self.write_file("学习文件001.txt")
        plan = make_plan(self.folder)
        with patch("rename_tool.os.name", "nt"), patch("rename_tool.os.rename") as rename:
            self.assertEqual(execute_plan(plan, CONFIRM_TEXT), [])
            rename.assert_not_called()

    def change_content_preserving_metadata(self, path):
        before = path.stat()
        content = path.read_bytes()
        path.write_bytes(content[:-1] + (b"0" if content[-1:] != b"0" else b"1"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_preview_captures_sha256_without_changing_files_or_history(self):
        self.write_file("a.txt", "原始测试内容")
        self.write_file("b.txt", "")
        before = self.content_snapshot()
        plan = make_plan(self.folder)
        self.assertEqual({item.source.name: item.sha256.lower() for item in plan}, before)
        self.assertEqual(self.content_snapshot(), before)
        self.assertFalse((self.folder / ".rename_history").exists())

    def test_same_size_and_mtime_content_change_rejects_old_plan_before_history(self):
        source = self.write_file("a.txt")
        plan = make_plan(self.folder)
        self.change_content_preserving_metadata(source)
        self.assertEqual(file_snapshot(source), plan[0].snapshot)
        self.assertNotEqual(file_digest(source), plan[0].sha256)
        with patch("rename_tool.HistoryStore") as store, patch("rename_tool.os.rename") as rename:
            with self.assertRaisesRegex(RenameError, "SHA256"):
                execute_plan(plan, CONFIRM_TEXT)
            store.assert_not_called()
            rename.assert_not_called()

    def test_changed_second_file_blocks_entire_old_batch(self):
        self.write_file("a.txt")
        source = self.write_file("b.txt")
        plan = make_plan(self.folder)
        self.change_content_preserving_metadata(source)
        before = self.content_snapshot()
        with patch("rename_tool.HistoryStore") as store, patch("rename_tool.os.rename") as rename:
            with self.assertRaisesRegex(RenameError, "SHA256"):
                execute_plan(plan, CONFIRM_TEXT)
            store.assert_not_called()
            rename.assert_not_called()
        self.assertEqual(self.content_snapshot(), before)

    def test_cli_rejects_same_metadata_change_after_preview_and_confirmation(self):
        source = self.write_file("a.txt")

        def confirm_after_change(prompt):
            self.change_content_preserving_metadata(source)
            return CONFIRM_TEXT

        output = io.StringIO()
        with patch("builtins.input", side_effect=confirm_after_change), patch("rename_tool.os.rename") as rename, contextlib.redirect_stdout(output):
            self.assertEqual(main(["--folder", str(self.folder), "--apply"]), 1)
            rename.assert_not_called()
        self.assertIn("SHA256", output.getvalue())
        self.assertFalse((self.folder / ".rename_history").exists())

    def test_new_preview_accepts_current_content_but_old_preview_stays_invalid(self):
        source = self.write_file("a.txt")
        old = make_plan(self.folder)
        self.change_content_preserving_metadata(source)
        fresh = make_plan(self.folder)
        self.assertNotEqual(old[0].sha256, fresh[0].sha256)
        self.assertEqual(old[0].snapshot, fresh[0].snapshot)
        with self.assertRaisesRegex(RenameError, "SHA256"):
            validate_plan(old)
        validate_plan(fresh)
        self.assertFalse((self.folder / ".rename_history").exists())

    def test_no_op_candidate_content_change_also_invalidates_old_plan(self):
        source = self.write_file("学习文件001.txt")
        plan = make_plan(self.folder)
        self.change_content_preserving_metadata(source)
        with patch("rename_tool.os.rename") as rename:
            with self.assertRaisesRegex(RenameError, "SHA256"):
                execute_plan(plan, CONFIRM_TEXT)
            rename.assert_not_called()
        self.assertFalse((self.folder / ".rename_history").exists())

    def test_extension_filter_hashes_only_matching_candidates(self):
        self.write_file("a.txt")
        self.write_file("b.csv")
        self.write_file("c.TXT")
        with patch("rename_tool.file_digest", wraps=file_digest) as digest:
            plan = make_plan(self.folder, extension=".txt")
        self.assertEqual([call.args[0].name for call in digest.call_args_list], ["a.txt", "c.TXT"])
        self.assertEqual(len(plan), 2)

    def test_preview_hash_read_error_creates_no_history_or_rename(self):
        self.write_file("a.txt")
        before = self.content_snapshot()
        with patch("rename_tool.file_digest", side_effect=PermissionError("模拟文件不可读")), patch("rename_tool.os.rename") as rename, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--folder", str(self.folder), "--apply"]), 1)
            rename.assert_not_called()
        self.assertEqual(self.content_snapshot(), before)
        self.assertFalse((self.folder / ".rename_history").exists())

    def test_file_digest_reads_large_content_in_bounded_blocks(self):
        source = self.write_file("a.txt")
        content = b"sample" * 500_000
        sizes = []

        class BoundedReader(io.BytesIO):
            def read(self, size=-1):
                sizes.append(size)
                if size != 1024 * 1024:
                    raise AssertionError("不允许一次读取整个文件")
                return super().read(size)

        with patch.object(Path, "open", return_value=BoundedReader(content)):
            digest = file_digest(source)
        self.assertEqual(digest, hashlib.sha256(content).hexdigest().upper())
        self.assertGreater(len(sizes), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
