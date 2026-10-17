"""持久操作账本：保留正向操作，并独立追加每次撤销尝试。"""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_HISTORY_DIR = PROJECT_ROOT / ".rename_history"
BACKUP_DIR = PROJECT_ROOT / "版本备份"
DATABASE_NAME = "operations.sqlite3"
SCHEMA_VERSION = 2


class HistoryError(Exception):
    """账本不可用时必须停止操作。"""


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def local_time(value):
    if not value:
        return "尚未结束"
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _plain_path(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise HistoryError(f"为避免目录被重定向，不处理符号链接或目录联接：{component}")
    return path


def protect_path(path, history_dir=None):
    """同时检查原始路径、解析后的路径和数据库的硬链接别名。"""
    path = _plain_path(path)
    resolved = path.resolve(strict=False)
    history_dir = Path(history_dir if history_dir is not None else DEFAULT_HISTORY_DIR).absolute()
    roots = (PROJECT_ROOT / ".rename_history", history_dir, BACKUP_DIR)
    if any(part.casefold() == ".rename_history" for part in path.parts):
        raise HistoryError("历史数据库目录受到保护，不能作为重命名对象。")
    for root in roots:
        if resolved.is_relative_to(root.resolve(strict=False)):
            raise HistoryError(f"历史或版本备份目录受到保护，不能重命名其中的文件：{path}")
    if path.is_file():
        for root in (PROJECT_ROOT / ".rename_history", history_dir):
            for name in (DATABASE_NAME, "writer.lock", DATABASE_NAME + "-journal", DATABASE_NAME + "-wal", DATABASE_NAME + "-shm"):
                protected = root / name
                if protected.exists() and path.samefile(protected):
                    raise HistoryError(f"文件是历史数据库或其辅助文件的别名，拒绝重命名：{path}")


class HistoryStore:
    def __init__(self, directory=None):
        self.directory = Path(directory if directory is not None else DEFAULT_HISTORY_DIR).absolute()
        self.database = self.directory / DATABASE_NAME
        self.connection = None
        self.lock_file = None

    def _check_storage(self):
        _plain_path(self.directory)
        if self.directory.exists() and not self.directory.is_dir():
            raise HistoryError("历史目录被普通文件占用。")
        for name in (DATABASE_NAME, "writer.lock", DATABASE_NAME + "-journal", DATABASE_NAME + "-wal", DATABASE_NAME + "-shm"):
            path = self.directory / name
            _plain_path(path)
            if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                raise HistoryError(f"历史文件不是独立普通文件，拒绝使用：{path}")

    def _acquire_lock(self):
        descriptor = os.open(self.directory / "writer.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.lock_file = os.fdopen(descriptor, "r+b")
        if self.lock_file.seek(0, os.SEEK_END) == 0:
            self.lock_file.write(b"0")
            self.lock_file.flush()
        self.lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.lock_file is not None:
            # 关闭句柄也会释放系统锁；不删除锁文件，以免制造第二个锁入口。
            self.lock_file.close()
            self.lock_file = None

    def __enter__(self):
        try:
            self._check_storage()
            self.directory.mkdir(parents=True, exist_ok=True)
            self._acquire_lock()
            self.connection = sqlite3.connect(self.database, timeout=2)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA synchronous = FULL")
            if self.connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise HistoryError("历史数据库的日志模式异常，未自动修改。")
            self._initialize()
            self._validate_database(self.connection)
            return self
        except (OSError, sqlite3.Error) as error:
            self._close()
            raise HistoryError(f"无法打开历史账本或已有另一个程序正在执行：{error}") from error
        except BaseException:
            self._close()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        self._close()

    def _commit(self):
        self.connection.commit()

    @contextmanager
    def _write(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self._commit()
        except BaseException as error:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            if isinstance(error, sqlite3.Error):
                raise HistoryError(f"历史数据库写入失败：{error}") from error
            raise

    def _initialize(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 1:
            self._validate_database(self.connection)
            with self._write() as connection:
                self._create_undo_tables(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version != 0 or tables:
            raise HistoryError("历史数据库版本或结构不受支持，未覆盖或重建。")
        with self._write() as connection:
            connection.execute("""CREATE TABLE operations (
                id TEXT PRIMARY KEY, folder TEXT NOT NULL, folder_key TEXT NOT NULL,
                created_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('running','completed','failed','partial_failure')),
                error TEXT
            )""")
            connection.execute("""CREATE TABLE items (
                operation_id TEXT NOT NULL REFERENCES operations(id), position INTEGER NOT NULL,
                original_name TEXT NOT NULL, new_name TEXT NOT NULL,
                sha256 TEXT NOT NULL, before_state TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('planned','skipped','in_progress','success','failed')),
                prepared_at TEXT, finished_at TEXT, error TEXT,
                PRIMARY KEY(operation_id, position)
            )""")
            connection.execute("""CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL REFERENCES operations(id), position INTEGER,
                at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
            )""")
            self._create_undo_tables(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _create_undo_tables(connection):
        connection.execute("""CREATE TABLE undo_attempts (
            id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
            folder TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('running','completed','failed','partial_failure')),
            error TEXT, UNIQUE(id,operation_id)
        )""")
        connection.execute("""CREATE TABLE undo_items (
            attempt_id TEXT NOT NULL, operation_id TEXT NOT NULL,
            position INTEGER NOT NULL, forward_position INTEGER NOT NULL,
            source_name TEXT NOT NULL, target_name TEXT NOT NULL,
            sha256 TEXT NOT NULL, before_state TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('planned','in_progress','success','failed')),
            prepared_at TEXT, finished_at TEXT, error TEXT,
            PRIMARY KEY(attempt_id,position), UNIQUE(attempt_id,forward_position),
            FOREIGN KEY(attempt_id,operation_id) REFERENCES undo_attempts(id,operation_id),
            FOREIGN KEY(operation_id,forward_position) REFERENCES items(operation_id,position)
        )""")
        connection.execute("""CREATE TABLE undo_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL REFERENCES undo_attempts(id), position INTEGER,
            at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
        )""")

    @staticmethod
    def _validate_database(connection):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, SCHEMA_VERSION):
            raise HistoryError("历史数据库版本不受支持。")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise HistoryError("历史数据库校验失败，未自动修复。")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise HistoryError("历史数据库的记录关联异常，未自动修复。")
        for table, columns in {
            "operations": "id, folder, folder_key, created_at, finished_at, status, error",
            "items": "operation_id, position, original_name, new_name, sha256, before_state, status, prepared_at, finished_at, error",
            "events": "id, operation_id, position, at, kind, detail",
        }.items():
            connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")
        if version == 2:
            for table, columns in {
                "undo_attempts": "id,operation_id,folder,created_at,finished_at,status,error",
                "undo_items": "attempt_id,operation_id,position,forward_position,source_name,target_name,sha256,before_state,status,prepared_at,finished_at,error",
                "undo_events": "id,attempt_id,position,at,kind,detail",
            }.items():
                connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")

    def ensure_directory_finished(self, folder):
        folder_key = str(Path(folder).resolve(strict=True)).casefold()
        if self.connection.execute(
            "SELECT 1 FROM operations WHERE folder_key=? AND status='running' LIMIT 1", (folder_key,)
        ).fetchone():
            raise HistoryError("这个目录有尚未完整结束的记录，必须先人工核查，不能继续改名或撤销。")
        if self.connection.execute("""SELECT 1 FROM undo_attempts AS u
            JOIN operations AS o ON o.id=u.operation_id
            WHERE o.folder_key=? AND u.status='running' LIMIT 1""", (folder_key,)).fetchone():
            raise HistoryError("这个目录有待核查的撤销记录，不能继续改名或撤销。")

    def _event(self, operation_id, position, kind, detail=None):
        self.connection.execute(
            "INSERT INTO events(operation_id, position, at, kind, detail) VALUES(?,?,?,?,?)",
            (operation_id, position, now_utc(), kind, detail),
        )

    def begin_operation(self, folder, rows):
        folder = Path(folder).resolve(strict=True)
        folder_key = str(folder).casefold()
        self.ensure_directory_finished(folder)
        operation_id = uuid.uuid4().hex
        with self._write() as connection:
            connection.execute(
                "INSERT INTO operations(id,folder,folder_key,created_at,status) VALUES(?,?,?,?,?)",
                (operation_id, str(folder), folder_key, now_utc(), "running"),
            )
            for position, row in enumerate(rows):
                connection.execute(
                    "INSERT INTO items(operation_id,position,original_name,new_name,sha256,before_state,status) VALUES(?,?,?,?,?,?,?)",
                    (operation_id, position, row["original_name"], row["new_name"], row["sha256"],
                     json.dumps(row["before_state"]), "skipped" if row["original_name"] == row["new_name"] else "planned"),
                )
            self._event(operation_id, None, "operation_started")
        return operation_id

    def mark_in_progress(self, operation_id, position):
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE items SET status='in_progress',prepared_at=? WHERE operation_id=? AND position=? AND status='planned'",
                (now_utc(), operation_id, position),
            ).rowcount
            if changed != 1:
                raise HistoryError("准备执行的记录状态不正确。")
            self._event(operation_id, position, "rename_intent_saved")

    def mark_success(self, operation_id, position):
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE items SET status='success',finished_at=? WHERE operation_id=? AND position=? AND status='in_progress'",
                (now_utc(), operation_id, position),
            ).rowcount
            if changed != 1:
                raise HistoryError("成功记录的前置状态不正确。")
            self._event(operation_id, position, "rename_success")

    def mark_failed(self, operation_id, position, error):
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE items SET status='failed',finished_at=?,error=? WHERE operation_id=? AND position=? AND status IN ('planned','in_progress')",
                (now_utc(), str(error), operation_id, position),
            ).rowcount
            if changed != 1:
                raise HistoryError("失败记录的前置状态不正确。")
            self._event(operation_id, position, "rename_failed", str(error))

    def finish(self, operation_id):
        rows = self.connection.execute("SELECT status,error FROM items WHERE operation_id=?", (operation_id,)).fetchall()
        statuses = {row["status"] for row in rows}
        if "in_progress" in statuses:
            raise HistoryError("存在结果未确认的项目，不能标记整批完成。")
        if statuses <= {"success", "skipped"}:
            status = "completed"
        elif "failed" in statuses:
            status = "partial_failure" if "success" in statuses else "failed"
        else:
            raise HistoryError("记录尚未全部处理，不能标记完成。")
        error = next((row["error"] for row in rows if row["error"]), None)
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE operations SET status=?,finished_at=?,error=? WHERE id=? AND status='running'",
                (status, now_utc(), error, operation_id),
            ).rowcount
            if changed != 1:
                raise HistoryError("整批记录的前置状态不正确。")
            self._event(operation_id, None, status, error)

    def begin_undo(self, operation_id, folder, rows):
        self.ensure_directory_finished(folder)
        operation = self.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not operation or operation["status"] not in ("completed", "partial_failure") or Path(operation["folder"]) != Path(folder):
            raise HistoryError("原操作不存在、目录不符或状态不允许撤销。")
        forward = {row["position"]: dict(row) for row in self.connection.execute(
            "SELECT * FROM items WHERE operation_id=? AND status='success'", (operation_id,))}
        undone = {row[0] for row in self.connection.execute(
            "SELECT forward_position FROM undo_items WHERE operation_id=? AND status='success'", (operation_id,))}
        if not rows or {row["forward_position"] for row in rows} != set(forward) - undone:
            raise HistoryError("撤销清单与尚未撤销的成功项目不一致。")
        for row in rows:
            original = forward[row["forward_position"]]
            if (row["source_name"] != original["new_name"] or row["target_name"] != original["original_name"]
                    or row["sha256"] != original["sha256"] or list(row["before_state"]) != json.loads(original["before_state"])):
                raise HistoryError("撤销项目与原始记录不一致。")
        attempt_id = uuid.uuid4().hex
        with self._write() as connection:
            connection.execute("INSERT INTO undo_attempts(id,operation_id,folder,created_at,status) VALUES(?,?,?,?,?)",
                               (attempt_id, operation_id, str(folder), now_utc(), "running"))
            for position, row in enumerate(rows):
                connection.execute("""INSERT INTO undo_items(
                    attempt_id,operation_id,position,forward_position,source_name,target_name,sha256,before_state,status)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, operation_id, position, row["forward_position"], row["source_name"], row["target_name"],
                     row["sha256"], json.dumps(row["before_state"]), "planned"))
            self._undo_event(attempt_id, None, "undo_started")
        return attempt_id

    def _undo_event(self, attempt_id, position, kind, detail=None):
        self.connection.execute("INSERT INTO undo_events(attempt_id,position,at,kind,detail) VALUES(?,?,?,?,?)",
                                (attempt_id, position, now_utc(), kind, detail))

    def mark_undo_in_progress(self, attempt_id, position):
        with self._write() as connection:
            count = connection.execute("""UPDATE undo_items SET status='in_progress',prepared_at=?
                WHERE attempt_id=? AND position=? AND status='planned'""", (now_utc(), attempt_id, position)).rowcount
            if count != 1:
                raise HistoryError("撤销执行意图的前置状态不正确。")
            self._undo_event(attempt_id, position, "undo_intent_saved")

    def mark_undo_success(self, attempt_id, position):
        with self._write() as connection:
            count = connection.execute("""UPDATE undo_items SET status='success',finished_at=?
                WHERE attempt_id=? AND position=? AND status='in_progress'""", (now_utc(), attempt_id, position)).rowcount
            if count != 1:
                raise HistoryError("撤销成功记录的前置状态不正确。")
            self._undo_event(attempt_id, position, "undo_success")

    def mark_undo_failed(self, attempt_id, position, error):
        with self._write() as connection:
            count = connection.execute("""UPDATE undo_items SET status='failed',finished_at=?,error=?
                WHERE attempt_id=? AND position=? AND status IN ('planned','in_progress')""",
                (now_utc(), str(error), attempt_id, position)).rowcount
            if count != 1:
                raise HistoryError("撤销失败记录的前置状态不正确。")
            self._undo_event(attempt_id, position, "undo_failed", str(error))

    def finish_undo(self, attempt_id):
        rows = self.connection.execute("SELECT status,error FROM undo_items WHERE attempt_id=?", (attempt_id,)).fetchall()
        statuses = {row["status"] for row in rows}
        if rows and statuses == {"success"}:
            status = "completed"
        elif "failed" in statuses and "in_progress" not in statuses:
            status = "partial_failure" if "success" in statuses else "failed"
        else:
            raise HistoryError("撤销结果尚未全部确认，不能标记结束。")
        error = next((row["error"] for row in rows if row["error"]), None)
        with self._write() as connection:
            count = connection.execute("UPDATE undo_attempts SET status=?,finished_at=?,error=? WHERE id=? AND status='running'",
                                       (status, now_utc(), error, attempt_id)).rowcount
            if count != 1:
                raise HistoryError("撤销批次的前置状态不正确。")
            self._undo_event(attempt_id, None, status, error)

    @classmethod
    def read_history(cls, directory=None):
        """只读打开；不创建目录、数据库、改名记录，也不自动修改中断状态。"""
        store = cls(directory)
        connection = None
        try:
            store._check_storage()
            if not store.database.exists():
                return []
            connection = sqlite3.connect(store.database.as_uri() + "?mode=ro", uri=True, timeout=2)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            cls._validate_database(connection)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            operations = []
            for row in connection.execute("SELECT * FROM operations ORDER BY created_at DESC,id DESC"):
                operation = dict(row)
                datetime.fromisoformat(operation["created_at"])
                if operation["finished_at"]:
                    datetime.fromisoformat(operation["finished_at"])
                operation["items"] = [dict(item) for item in connection.execute(
                    "SELECT * FROM items WHERE operation_id=? ORDER BY position", (row["id"],)
                )]
                for item in operation["items"]:
                    item["effective_status"] = "needs_review" if item["status"] == "in_progress" else item["status"]
                operation["effective_status"] = "needs_review" if (
                    row["status"] == "running" or any(item["effective_status"] == "needs_review" for item in operation["items"])
                ) else row["status"]
                if version == 2:
                    operation["undo_attempts"] = []
                    undone = set()
                    for attempt_row in connection.execute("SELECT * FROM undo_attempts WHERE operation_id=? ORDER BY created_at,id", (row["id"],)):
                        attempt = dict(attempt_row)
                        attempt["items"] = [dict(item) for item in connection.execute(
                            "SELECT * FROM undo_items WHERE attempt_id=? ORDER BY position", (attempt["id"],))]
                        for item in attempt["items"]:
                            item["effective_status"] = "needs_review" if item["status"] == "in_progress" else item["status"]
                            if item["status"] == "success":
                                undone.add(item["forward_position"])
                        attempt["effective_status"] = "needs_review" if attempt["status"] == "running" or any(
                            item["effective_status"] == "needs_review" for item in attempt["items"]) else attempt["status"]
                        operation["undo_attempts"].append(attempt)
                    successful = {item["position"] for item in operation["items"] if item["status"] == "success"}
                    if any(attempt["effective_status"] == "needs_review" for attempt in operation["undo_attempts"]):
                        operation["undo_status"] = "needs_review"
                    elif successful and successful <= undone:
                        operation["undo_status"] = "undone"
                    elif undone:
                        operation["undo_status"] = "partially_undone"
                    else:
                        operation["undo_status"] = "not_undone"
                    for item in operation["items"]:
                        item["undo_state"] = "undone" if item["position"] in undone else "not_undone"
                operations.append(operation)
            return operations
        except (OSError, sqlite3.Error, ValueError) as error:
            raise HistoryError(f"无法读取历史账本，记录完整性待人工核查；未自动修复：{error}") from error
        finally:
            if connection is not None:
                connection.close()
