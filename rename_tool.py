"""Windows 批量重命名入门工具：默认只预览，确认后才执行。"""

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from operation_history import HistoryError, HistoryStore, local_time, protect_path


DEMO_FOLDER = Path(__file__).parent / "测试文件"
CONFIRM_TEXT = "确认重命名"


@dataclass(frozen=True)
class RenameItem:
    source: Path
    target: Path
    snapshot: tuple
    sha256: str


class RenameError(Exception):
    """将安全检查失败显示成易懂的提示。"""


class ExecutionError(RenameError):
    def __init__(self, message, completed, operation_id=None, needs_review=False):
        super().__init__(message)
        self.completed = completed
        self.operation_id = operation_id
        self.needs_review = needs_review


def file_snapshot(path):
    # 记录文件身份、大小和修改时间，避免预览后文件变化却仍然执行。
    if path.is_symlink() or not path.is_file():
        raise RenameError(f"文件已不存在，或不再是普通文件：{path.name}")
    info = path.stat()
    if info.st_nlink != 1:
        raise RenameError(f"文件存在多个硬链接或链接数异常（{info.st_nlink}），拒绝处理：{path.name}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def validate_prefix(prefix):
    # Windows 文件名不能包含这些字符；第一版要求一个非空前缀。
    forbidden = '<>:"/\\|?*'
    if not prefix.strip():
        raise RenameError("前缀不能为空。")
    if any(character in forbidden or ord(character) < 32 for character in prefix):
        raise RenameError('前缀不能包含控制字符或这些符号：<>:"/\\|?*')
    if prefix.endswith((" ", ".")):
        raise RenameError("前缀不能以空格或句点结尾。")


def normalize_extension(extension):
    """省略时不过滤；指定时统一成 .txt 这样的形式。"""
    if extension is None:
        return None
    extension = extension.strip().casefold()
    if not extension:
        raise RenameError("扩展名不能为空；不过滤时请省略 --extension。")
    if not extension.startswith("."):
        extension = "." + extension
    forbidden = '<>:"/\\|?*'
    if (
        len(extension) == 1
        or "." in extension[1:]
        or any(character in forbidden or character.isspace() or ord(character) < 32 for character in extension)
    ):
        raise RenameError("请指定单个最后一级扩展名，例如 .txt 或 .gz；不要使用通配符或 .tar.gz。")
    return extension


def make_plan(folder, prefix="学习文件", start=1, extension=None):
    """只生成计划，不修改文件。"""
    validate_prefix(prefix)
    selected_extension = normalize_extension(extension)
    if start < 1:
        raise RenameError("起始编号必须大于或等于 1。")
    try:
        protect_path(folder)
    except HistoryError as error:
        raise RenameError(str(error)) from error
    folder = Path(folder).resolve(strict=True)
    if not folder.is_dir():
        raise RenameError("请选择一个文件夹。")

    entries = list(folder.iterdir())
    files = sorted(
        (
            path for path in entries
            if path.is_file() and not path.is_symlink()
            and (selected_extension is None or path.suffix.casefold() == selected_extension)
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )
    existing_names = {path.name.casefold() for path in entries}
    target_names = set()
    plan = []
    for number, source in enumerate(files, start=start):
        try:
            protect_path(source)
        except HistoryError as error:
            raise RenameError(str(error)) from error
        # :03d 把 1 写成 001；suffixes 保留 .txt 或 .tar.gz 等扩展名。
        extension = "".join(source.suffixes)
        new_name = f"{prefix}{number:03d}{extension}"
        reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        reserved.update(f"{kind}{number}" for kind in ("COM", "LPT") for number in "123456789¹²³")
        if new_name.split(".")[0].rstrip(" ").upper() in reserved:
            raise RenameError(f"Windows 不允许使用这个设备保留名字：{new_name}")
        if len(new_name.encode("utf-16-le")) // 2 > 255:
            raise RenameError(f"生成的文件名过长：{new_name}")
        if new_name.casefold() in target_names:
            raise RenameError(f"生成的名字重复：{new_name}")
        target_names.add(new_name.casefold())
        if new_name != source.name and new_name.casefold() in existing_names:
            raise RenameError(f"目标名字已被占用：{new_name}。请换一个前缀。")
        snapshot = file_snapshot(source)
        digest = file_digest(source)
        if file_snapshot(source) != snapshot:
            raise RenameError(f"生成预览期间文件发生变化：{source.name}。请重新预览。")
        plan.append(RenameItem(source, folder / new_name, snapshot, digest))
    return plan


def validate_plan(plan):
    for item in plan:
        if file_snapshot(item.source) != item.snapshot:
            raise RenameError(f"预览后文件发生变化：{item.source.name}。请重新预览。")
        if item.source.name != item.target.name:
            # Windows 名字通常不区分大小写；文件和文件夹都算占用。
            names = {path.name.casefold() for path in item.source.parent.iterdir()}
            if item.target.name.casefold() in names:
                raise RenameError(f"目标名字已被占用：{item.target.name}。未覆盖任何文件。")
        if file_digest(item.source) != item.sha256:
            raise RenameError(f"预览后文件内容发生变化（SHA256 不一致）：{item.source.name}。请重新预览。")
        if file_snapshot(item.source) != item.snapshot:
            raise RenameError(f"文件状态已变化，请重新预览：{item.source.name}")


def file_digest(path):
    """分块计算 SHA256，不把大文件一次性读入内存。"""
    before = file_snapshot(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if file_snapshot(path) != before:
        raise RenameError(f"计算校验值期间文件发生变化：{path.name}")
    return digest.hexdigest().upper()


def execute_plan(plan, confirmation):
    # 无论调用入口是什么，没有准确的确认文字，都不允许执行。
    if confirmation != CONFIRM_TEXT:
        raise RenameError("没有收到明确确认，未执行重命名。")
    if os.name != "nt":
        raise RenameError("本版本只在 Windows 上执行重命名。")
    validate_plan(plan)
    if not any(item.source.name != item.target.name for item in plan):
        return []
    folder = plan[0].source.parent
    store = HistoryStore()
    for item in plan:
        if not item.source.is_absolute() or not item.target.is_absolute() or item.source.parent != folder or item.target.parent != folder:
            raise RenameError("计划必须只包含同一个目录内的绝对路径。")
        protect_path(item.source, store.directory)
        protect_path(item.target, store.directory)
    rows = []
    for item in plan:
        rows.append({"original_name": item.source.name, "new_name": item.target.name,
                     "sha256": item.sha256, "before_state": item.snapshot})
    completed = []
    with store:
        operation_id = store.begin_operation(folder, rows)
        try:
            for position, item in enumerate(plan):
                if item.source.name == item.target.name:
                    continue
                try:
                    validate_plan([item])
                    protect_path(item.source, store.directory)
                    protect_path(item.target, store.directory)
                except (OSError, RenameError, HistoryError) as error:
                    store.mark_failed(operation_id, position, error)
                    store.finish(operation_id)
                    raise ExecutionError(f"处理 {item.source.name} 前停止：{error}", completed, operation_id) from error
                # 必须先把意图提交到磁盘，再让操作系统改名。
                store.mark_in_progress(operation_id, position)
                try:
                    os.rename(item.source, item.target)
                except OSError as error:
                    # 只有能证实源文件仍完整、目标不存在时，才记录确定失败。
                    if (item.source.exists() and not item.target.exists()
                            and file_snapshot(item.source) == item.snapshot
                            and file_digest(item.source) == rows[position]["sha256"]):
                        store.mark_failed(operation_id, position, error)
                        store.finish(operation_id)
                        raise ExecutionError(f"处理 {item.source.name} 时停止：{error}", completed, operation_id) from error
                    raise
                # 若这里保存失败，不把这一项加入“已确认成功”清单。
                store.mark_success(operation_id, position)
                completed.append(item)
            store.finish(operation_id)
        except ExecutionError:
            raise
        except BaseException as error:
            # 不重试写入、不回滚文件。已保存的 in_progress/running 在历史中显示“待核查”。
            raise ExecutionError(
                f"操作中断或账本未能确认结果：{error}。有文件可能已经改名，请查看历史并人工核查；不要直接重试。",
                completed, operation_id, needs_review=True,
            ) from error
    return completed


def show_history():
    labels = {"planned": "未执行", "skipped": "无需修改", "success": "成功", "failed": "失败",
              "completed": "全部完成", "partial_failure": "部分成功后停止", "needs_review": "待核查（执行中或异常中断）"}
    operations = HistoryStore.read_history()
    if not operations:
        print("暂无本工具自动保存的操作记录。")
        return
    for operation in operations:
        print(f"\n操作编号：{operation['id']}")
        print(f"时间（北京时间）：{local_time(operation['created_at'])}")
        print(f"目录：{operation['folder']}")
        print(f"状态：{labels[operation['effective_status']]}")
        for item in operation["items"]:
            print(f"  {item['original_name']} → {item['new_name']}：{labels[item['effective_status']]}")
            if item["error"]:
                print(f"    原因：{item['error']}")
        if operation["effective_status"] == "needs_review":
            print("  该批次没有完整结束记录；不自动判断或修复，也不允许继续修改这个目录。")
        undo_labels = {"not_undone": "未撤销", "partially_undone": "部分已撤销", "undone": "已全部撤销", "needs_review": "撤销待核查"}
        print(f"撤销状态：{undo_labels[operation.get('undo_status', 'not_undone')]}")
        for attempt in operation.get("undo_attempts", []):
            print(f"  撤销尝试 {attempt['id']}：{labels[attempt['effective_status']]}")
            for item in attempt["items"]:
                print(f"    {item['source_name']} → {item['target_name']}：{labels[item['effective_status']]}")
                if item["error"]:
                    print(f"      原因：{item['error']}")


def show_preview(folder, plan, extension=None):
    print(f"\n文件夹：{folder}")
    if extension is not None:
        print(f"筛选条件：{extension}（不区分大小写，匹配最后一级扩展名）")
    print("预览（尚未修改文件）：")
    for item in plan:
        note = "（无需修改）" if item.source.name == item.target.name else ""
        print(f"  {item.source.name}  →  {item.target.name}{note}")
    changed_count = sum(item.source.name != item.target.name for item in plan)
    if extension is None:
        print(f"共找到 {len(plan)} 个普通文件，需要重命名 {changed_count} 个。")
    else:
        print(f"匹配 {len(plan)} 个普通文件，需要重命名 {changed_count} 个；其他类型不处理。")
    print("不会处理子文件夹或符号链接，不会修改文件内容。")
    return changed_count


def main(argv=None):
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="先预览、确认后执行的 Windows 批量重命名工具", allow_abbrev=False)
    parser.add_argument("--folder", type=Path, default=DEMO_FOLDER, help="文件夹；默认使用项目中的测试文件")
    parser.add_argument("--prefix", default="学习文件", help="新名字前缀，默认为学习文件")
    parser.add_argument("--start", type=int, default=1, help="起始编号，默认为 1")
    parser.add_argument("--extension", help="只处理单个扩展名，例如 .txt；省略时处理所有普通文件")
    parser.add_argument("--apply", action="store_true", help="预览后询问是否执行；仍需输入确认文字")
    parser.add_argument("--history", action="store_true", help="只读查看自动保存的操作历史")
    parser.add_argument("--undo", metavar="操作编号", help="根据操作编号预览撤销；执行还需 --apply 和明确确认")
    args = parser.parse_args(raw_args)
    if args.history and any(argument.split("=", 1)[0] in {"--folder", "--prefix", "--start", "--extension", "--apply", "--undo"} for argument in raw_args):
        parser.error("--history 不能与重命名选项同时使用。")
    if args.undo is not None and any(argument.split("=", 1)[0] in {"--folder", "--prefix", "--start", "--extension"} for argument in raw_args):
        parser.error("--undo 不能与正向改名选项同时使用。")
    try:
        if args.history:
            show_history()
            return 0
        if args.undo is not None:
            from undo_tool import UndoError, UndoExecutionError, run_undo
            try:
                run_undo(args.undo, apply=args.apply)
                return 0
            except UndoExecutionError as error:
                print(f"\n撤销停止：{error}")
                print(f"撤销尝试编号：{error.attempt_id}")
                print(f"已持久确认撤销 {len(error.completed)} 项。")
                if error.needs_review:
                    print("状态：待核查。不会自动重试、修复或继续处理。")
                for item in error.completed:
                    print(f"已撤销：{item.source.name} → {item.target.name}")
                return 1
            except UndoError as error:
                print(f"\n拒绝撤销：{error}")
                return 1
        extension = normalize_extension(args.extension)
        plan = make_plan(args.folder, args.prefix, args.start, extension=extension)
        changed_count = show_preview(args.folder.resolve(), plan, extension=extension)
        if extension is not None and not plan:
            print("没有匹配该扩展名的文件，未修改任何文件。")
            return 0
        if not changed_count:
            print("没有需要修改的文件，已结束。")
            return 0
        if not args.apply:
            print("当前是预览模式。要执行，请使用相同参数加上 --apply。")
            return 0
        print(f"\n请核对以上目录和对照表。只有输入“{CONFIRM_TEXT}”才会执行。")
        try:
            answer = input("输入确认文字，或直接回车取消：")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未修改任何文件。")
            return 0
        if answer != CONFIRM_TEXT:
            print("已取消，未修改任何文件。")
            return 0
        completed = execute_plan(plan, answer)
        for item in completed:
            print(f"已完成：{item.source.name} → {item.target.name}")
        print(f"成功重命名 {len(completed)} 个文件。")
        print("操作记录已保存，可使用 --history 查看。")
        return 0
    except ExecutionError as error:
        print(f"\n错误：{error}")
        if error.operation_id:
            print(f"操作编号：{error.operation_id}")
        if error.needs_review:
            print(f"状态：待核查。已持久确认成功 {len(error.completed)} 个；当前项目结果未确认，后续操作已停止。")
        else:
            print(f"已完成 {len(error.completed)} 个；其余操作已停止。")
        for item in error.completed:
            print(f"已完成：{item.source.name} → {item.target.name}")
        print("请检查上面的清单；程序不会自动继续或自动回滚。")
        return 1
    except (OSError, RenameError, HistoryError) as error:
        print(f"\n无法执行：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
