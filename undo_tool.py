"""根据已有账本生成撤销计划；默认只读，明确确认后才执行。"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from operation_history import HistoryError, HistoryStore, protect_path
from rename_tool import RenameError, file_digest, file_snapshot


class UndoError(Exception):
    pass


class UndoExecutionError(UndoError):
    def __init__(self, message, completed, attempt_id, needs_review=False):
        super().__init__(message)
        self.completed = completed
        self.attempt_id = attempt_id
        self.needs_review = needs_review


@dataclass(frozen=True)
class UndoItem:
    forward_position: int
    source: Path
    target: Path
    sha256: str
    snapshot: tuple


@dataclass(frozen=True)
class UndoPlan:
    operation_id: str
    folder: Path
    items: tuple
    token: str
    reason: str = ""


def confirmation_text(operation_id):
    return f"确认撤销 {operation_id}"


def _safe_name(name):
    if not isinstance(name, str) or not name or name in (".", "..") or name.endswith((" ", ".")):
        raise UndoError("账本文件名格式异常，拒绝执行。")
    if any(character in '<>:"/\\|?*' or ord(character) < 32 for character in name):
        raise UndoError("账本文件名包含路径或非法字符，拒绝执行。")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{kind}{number}" for kind in ("COM", "LPT") for number in "123456789¹²³")
    if name.split(".")[0].rstrip(" ").upper() in reserved or len(name.encode("utf-16-le")) // 2 > 255:
        raise UndoError("账本文件名是保留名字或过长，拒绝执行。")


def validate_undo_item(item):
    try:
        protect_path(item.source)
        protect_path(item.target)
        if not item.source.is_absolute() or item.source.parent != item.target.parent:
            raise UndoError("撤销路径超出记录目录。")
        if item.source.stat().st_nlink != 1:
            raise UndoError(f"文件存在多个硬链接，状态不明确：{item.source.name}")
        if file_snapshot(item.source) != item.snapshot:
            raise UndoError(f"文件身份、大小或修改时间异常：{item.source.name}")
        if file_digest(item.source) != item.sha256:
            raise UndoError(f"SHA256 不一致，文件内容已变化：{item.source.name}")
        occupied = {path.name.casefold() for path in item.source.parent.iterdir()}
        if item.target.name.casefold() in occupied:
            raise UndoError(f"原文件名已被占用，拒绝覆盖：{item.target.name}")
    except (OSError, RenameError, HistoryError) as error:
        raise UndoError(f"撤销检查失败，未恢复文件：{error}") from error


def make_undo_plan(operation_id):
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise UndoError("请提供历史中完整的 32 位操作编号。")
    operations = HistoryStore.read_history()
    operation = next((row for row in operations if row["id"] == operation_id), None)
    if operation is None:
        raise UndoError("找不到该操作编号；不会猜测或使用其他记录。")
    try:
        folder = Path(operation["folder"])
        if not folder.is_absolute():
            raise UndoError("账本目录不是绝对路径。")
        protect_path(folder)
        if folder.resolve(strict=True) != folder or not folder.is_dir():
            raise UndoError("账本目录不存在或状态异常。")
    except (OSError, HistoryError) as error:
        raise UndoError(f"操作目录不安全：{error}") from error
    for row in operations:
        if Path(row["folder"]) == folder and (row["effective_status"] == "needs_review" or row.get("undo_status") == "needs_review"):
            raise UndoError("该目录存在待核查的改名或撤销记录，拒绝执行。")
    if operation["status"] not in ("completed", "partial_failure", "failed"):
        raise UndoError("原操作状态不允许撤销。")
    # 不使用数据库版本号生成令牌，保证只读旧账本与执行时的附加表升级兼容。
    signature = {key: operation[key] for key in ("id", "folder", "created_at", "finished_at", "status")}
    signature["items"] = [{key: row[key] for key in (
        "position", "original_name", "new_name", "sha256", "before_state", "status", "prepared_at", "finished_at", "error"
    )} for row in operation["items"]]
    signature["undo_attempts"] = operation.get("undo_attempts", [])
    token = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    items = []
    targets = set()
    sources = set()
    successful = [row for row in operation["items"] if row["status"] == "success"]
    for row in reversed(successful):
        if row.get("undo_state") == "undone":
            continue
        _safe_name(row["original_name"])
        _safe_name(row["new_name"])
        try:
            state = json.loads(row["before_state"])
        except (TypeError, ValueError) as error:
            raise UndoError("文件身份记录损坏，拒绝撤销。") from error
        if (not isinstance(state, list) or len(state) != 4 or any(type(value) is not int for value in state)
                or state[0] <= 0 or state[1] <= 0 or state[2] < 0 or state[3] < 0
                or not isinstance(row["sha256"], str) or not re.fullmatch(r"[0-9A-F]{64}", row["sha256"])):
            raise UndoError("文件身份或 SHA256 记录异常，拒绝撤销。")
        source = folder / row["new_name"]
        target = folder / row["original_name"]
        if source.name.casefold() in sources or target.name.casefold() in targets:
            raise UndoError("账本包含重复的源文件或目标名字。")
        sources.add(source.name.casefold())
        targets.add(target.name.casefold())
        item = UndoItem(row["position"], source, target, row["sha256"], tuple(state))
        validate_undo_item(item)
        items.append(item)
    reason = "该操作已经全部撤销，不会重复处理。" if successful and not items else "没有成功改名的文件可撤销。"
    return UndoPlan(operation_id, folder, tuple(items), token, reason if not items else "")


def show_undo_preview(plan):
    print(f"\n原操作编号：{plan.operation_id}")
    print(f"目录：{plan.folder}")
    print("撤销预览（尚未修改文件）：")
    for item in plan.items:
        print(f"  {item.source.name} → {item.target.name}")
    print(f"需要恢复文件名 {len(plan.items)} 个；文件内容不会恢复或覆盖。")
    if plan.reason:
        print(plan.reason)


def execute_undo(plan, confirmation):
    if confirmation != confirmation_text(plan.operation_id):
        raise UndoError("没有收到包含操作编号的明确撤销确认。")
    if os.name != "nt":
        raise UndoError("本版本只在 Windows 上执行撤销。")
    if not plan.items:
        return []
    completed = []
    with HistoryStore() as store:
        # 同一个系统写入锁内重读账本并重新检查所有文件，防止重复或过期预览。
        fresh = make_undo_plan(plan.operation_id)
        if fresh != plan:
            raise UndoError("账本或文件状态在预览后发生变化，请重新预览。")
        rows = [{"forward_position": item.forward_position, "source_name": item.source.name,
                 "target_name": item.target.name, "sha256": item.sha256, "before_state": item.snapshot} for item in plan.items]
        attempt_id = store.begin_undo(plan.operation_id, plan.folder, rows)
        try:
            for position, item in enumerate(plan.items):
                try:
                    validate_undo_item(item)
                except UndoError as error:
                    store.mark_undo_failed(attempt_id, position, error)
                    store.finish_undo(attempt_id)
                    raise UndoExecutionError(str(error), completed, attempt_id) from error
                store.mark_undo_in_progress(attempt_id, position)
                try:
                    os.rename(item.source, item.target)
                except OSError as error:
                    try:
                        validate_undo_item(item)
                    except UndoError:
                        raise error
                    store.mark_undo_failed(attempt_id, position, error)
                    store.finish_undo(attempt_id)
                    raise UndoExecutionError(f"撤销 {item.source.name} 时停止：{error}", completed, attempt_id) from error
                store.mark_undo_success(attempt_id, position)
                completed.append(item)
            store.finish_undo(attempt_id)
        except UndoExecutionError:
            raise
        except BaseException as error:
            raise UndoExecutionError(
                f"撤销中断或记录未确认：{error}。文件可能已恢复原名，必须核查，不要直接重试。",
                completed, attempt_id, needs_review=True,
            ) from error
    return completed


def run_undo(operation_id, apply=False):
    plan = make_undo_plan(operation_id)
    show_undo_preview(plan)
    if not plan.items:
        return
    if not apply:
        print("当前仅预览；实际撤销需要 --apply，并再次输入明确确认文字。")
        return
    expected = confirmation_text(plan.operation_id)
    print(f"只有输入“{expected}”才会执行。")
    try:
        answer = input("输入完整确认文字，或回车取消：")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消撤销，未修改文件或新增撤销记录。")
        return
    if answer != expected:
        print("已取消撤销，未修改文件或新增撤销记录。")
        return
    completed = execute_undo(plan, answer)
    for item in completed:
        print(f"已撤销：{item.source.name} → {item.target.name}")
    print(f"成功撤销 {len(completed)} 项；原操作和本次撤销记录均已保留。")
