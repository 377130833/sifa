"""MySQL 存储、迁移与题库初始化。SQLite 仅供自动化测试使用。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = ROOT_DIR / "instance"


def _load_local_env() -> None:
    """读取项目本地忽略的 .env；现有进程环境变量优先于文件配置。"""
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("SIFA_DB_") and key not in os.environ:
            os.environ[key] = value.strip("\"'")


_load_local_env()

DB_BACKEND = os.environ.get("SIFA_DB_BACKEND", "mysql").strip().lower()
DB_PATH = Path(os.environ.get("SIFA_DB_PATH", INSTANCE_DIR / "sifa.sqlite3"))
DB_HOST = os.environ.get("SIFA_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("SIFA_DB_PORT", "3306"))
DB_USER = os.environ.get("SIFA_DB_USER", "root")
DB_PASSWORD = os.environ.get("SIFA_DB_PASSWORD", "")
DB_NAME = os.environ.get("SIFA_DB_NAME", "sifa_learning")

DEFAULT_EXAM_CONFIG = {
    "name": "模拟考试（可按正式规则调整）",
    "duration_minutes": 120,
    "counts": {"single": 25, "multiple": 25, "true_false": 30, "fill_blank": 20},
    "points": {"single": 1, "multiple": 1, "true_false": 1, "fill_blank": 1},
    "wrong_penalties": {"single": 0, "multiple": 0, "true_false": 0, "fill_blank": 0},
    "multiple_partial_credit": False,
    "fill_partial_credit": False,
    "official_verified": False,
    "official_reference": "",
}


class HybridRow(dict):
    """提供 SQLite Row 风格的字段名和数字下标读取，兼容两种数据库。"""

    def __init__(self, values):
        super().__init__(values)
        self._ordered_values = tuple(values.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._ordered_values[key]
        return super().__getitem__(key)


class ManagedSQLiteConnection(sqlite3.Connection):
    """让 SQLite 测试连接遵循与 MySQL 相同的关闭和提交时机。"""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class MysqlCursor:
    """包装 MySQL 游标，使查询结果支持项目现有的统一行访问方式。"""

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        row = self._cursor.fetchone()
        return HybridRow(row) if row is not None else None

    def fetchall(self):
        return [HybridRow(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        """让 MySQL 查询结果支持与 SQLite Cursor 一致的逐行遍历。"""
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class MysqlConnection:
    """为 MySQL 提供与 sqlite3 相近的 execute、executemany 和事务接口。"""

    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()

    def execute(self, sql, params=()):
        cursor = self._connection.cursor()
        cursor.execute(_mysql_sql(sql), params)
        return MysqlCursor(cursor)

    def executemany(self, sql, params):
        cursor = self._connection.cursor()
        cursor.executemany(_mysql_sql(sql), params)
        return MysqlCursor(cursor)


def _mysql_sql(sql: str) -> str:
    """转换应用层通用 SQL 的占位符及少量 SQLite 方言到 MySQL 方言。"""
    sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", sql, flags=re.I)
    sql = re.sub(r"\bON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET\b", "ON DUPLICATE KEY UPDATE", sql, flags=re.I)
    sql = re.sub(r"\bexcluded\.([A-Za-z_][A-Za-z0-9_]*)", r"VALUES(\1)", sql, flags=re.I)
    sql = re.sub(r"\bORDER\s+BY\s+RANDOM\s*\(\s*\)", "ORDER BY RAND()", sql, flags=re.I)
    sql = re.sub(r"CAST\(([^()]+)\s+AS\s+TEXT\)", r"CAST(\1 AS CHAR)", sql, flags=re.I)

    # 仅替换 SQL 字符串和标识符之外的问号，避免误改字面字符串内容。
    converted = []
    quote = None
    escaped = False
    for char in sql:
        if quote:
            converted.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"', "`"):
            quote = char
            converted.append(char)
        elif char == "?":
            converted.append("%s")
        else:
            converted.append(char)
    return "".join(converted)


def _require_mysql_driver():
    """延迟加载驱动，让只运行逻辑单元测试时无需连接 MySQL。"""
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError("缺少 MySQL 驱动，请先执行：python -m pip install -r requirements.txt") from exc
    return pymysql, DictCursor


def _mysql_options(*, database: str | None = None, dict_cursor: bool = True, user: str | None = None, password: str | None = None) -> dict:
    """集中生成 MySQL 连接参数，优先使用 .env 或运行环境中的配置。"""
    selected_password = DB_PASSWORD if password is None else password
    if not selected_password:
        raise RuntimeError("未配置 MySQL 密码；请在项目根目录 .env 中设置 SIFA_DB_PASSWORD。")
    pymysql, dict_cursor_type = _require_mysql_driver()
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": user or DB_USER,
        "password": selected_password,
        "database": database,
        "charset": "utf8mb4",
        "cursorclass": dict_cursor_type if dict_cursor else pymysql.cursors.Cursor,
        "autocommit": False,
        "connect_timeout": 10,
        "read_timeout": 60,
        "write_timeout": 60,
    }


def _validate_database_name() -> None:
    """拒绝不能安全放入建库语句的数据库名称。"""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", DB_NAME):
        raise RuntimeError("SIFA_DB_NAME 仅支持 1-64 位英文字母、数字和下划线。")


def database_label() -> str:
    """返回不包含账号密码的数据库目标描述，供运维日志展示。"""
    if DB_BACKEND == "sqlite":
        return str(DB_PATH)
    return f"MySQL {DB_HOST}:{DB_PORT}/{DB_NAME}"


def connect():
    """打开当前配置的数据库连接；SQLite 后端仅用于隔离的测试数据。"""
    if DB_BACKEND == "sqlite":
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(DB_PATH, timeout=30, factory=ManagedSQLiteConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection
    if DB_BACKEND != "mysql":
        raise RuntimeError("SIFA_DB_BACKEND 仅支持 mysql 或 sqlite。")
    pymysql, _ = _require_mysql_driver()
    return MysqlConnection(pymysql.connect(**_mysql_options(database=DB_NAME)))


def _ensure_mysql_database() -> None:
    """使用 .env 配置的现有 MySQL 账号创建项目数据库，不创建 MySQL 用户。"""
    _validate_database_name()
    pymysql, _ = _require_mysql_driver()
    connection = pymysql.connect(**_mysql_options(database=None, dict_cursor=False))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        connection.commit()
    finally:
        connection.close()


def _run_mysql_migrations() -> None:
    """按文件名顺序执行未应用的 SQL 迁移，并记录已成功版本。"""
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(120) NOT NULL COMMENT '迁移文件版本',
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '迁移完成时间',
                PRIMARY KEY (version)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='数据库结构迁移记录'"""
        )
        migration_dir = ROOT_DIR / "migrations"
        for migration_path in sorted(migration_dir.glob("*.sql")):
            version = migration_path.name
            if db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
                continue
            statements = [statement.strip() for statement in migration_path.read_text(encoding="utf-8").split(";") if statement.strip()]
            for statement in statements:
                db.execute(statement)
            db.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))


def init_db() -> None:
    """初始化 MySQL 库并应用迁移；测试后端则创建对应 SQLite 表结构。"""
    if DB_BACKEND == "mysql":
        _ensure_mysql_database()
        _run_mysql_migrations()
    elif DB_BACKEND == "sqlite":
        _init_sqlite_test_schema()
    else:
        raise RuntimeError("SIFA_DB_BACKEND 仅支持 mysql 或 sqlite。")

    with connect() as db:
        if DB_BACKEND == "sqlite":
            db.execute(
                "INSERT OR IGNORE INTO settings(`key`, value_json) VALUES ('exam_config', ?)",
                (json.dumps(DEFAULT_EXAM_CONFIG, ensure_ascii=False),),
            )
        else:
            db.execute(
                "INSERT IGNORE INTO settings(`key`, value_json) VALUES ('exam_config', ?)",
                (json.dumps(DEFAULT_EXAM_CONFIG, ensure_ascii=False),),
            )
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


def _init_sqlite_test_schema() -> None:
    """创建兼容旧自动化测试的临时 SQLite 表，不用于正式运行。"""
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
                password_salt TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','student')),
                enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT NOT NULL CHECK(section IN ('single','multiple','true_false','fill_blank')),
                number INTEGER NOT NULL, source_page INTEGER NOT NULL DEFAULT 0, stem TEXT NOT NULL, options_json TEXT NOT NULL DEFAULT '[]',
                answer TEXT NOT NULL, explanation TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', difficulty TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(section, number)
            );
            CREATE INDEX IF NOT EXISTS questions_section_idx ON questions(section, active, number);
            CREATE TABLE IF NOT EXISTS practice_progress (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                attempts INTEGER NOT NULL DEFAULT 0, correct_count INTEGER NOT NULL DEFAULT 0, incorrect_count INTEGER NOT NULL DEFAULT 0,
                last_answer TEXT NOT NULL DEFAULT '', last_result INTEGER NOT NULL DEFAULT 0, last_seen_at REAL NOT NULL, PRIMARY KEY(user_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                created_at REAL NOT NULL, PRIMARY KEY(user_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS exam_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                started_at REAL NOT NULL, ends_at REAL NOT NULL, submitted_at REAL, status TEXT NOT NULL DEFAULT 'in_progress'
                CHECK(status IN ('in_progress','submitted')), config_json TEXT NOT NULL, score REAL, max_score REAL
            );
            CREATE INDEX IF NOT EXISTS exam_attempt_user_idx ON exam_attempts(user_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS exam_items (
                exam_id INTEGER NOT NULL REFERENCES exam_attempts(id) ON DELETE CASCADE, order_index INTEGER NOT NULL,
                question_id INTEGER NOT NULL REFERENCES questions(id), snapshot_json TEXT NOT NULL, answer_json TEXT NOT NULL DEFAULT 'null',
                PRIMARY KEY(exam_id, question_id), UNIQUE(exam_id, order_index)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS import_jobs (
                id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES users(id), source_name TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, questions_json TEXT NOT NULL, counts_json TEXT NOT NULL, created_at REAL NOT NULL, committed_at REAL
            );
            """
        )


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    """使用 PBKDF2-HMAC-SHA256 对用户密码做带盐哈希。"""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, expected_hash: str) -> bool:
    """以恒定时间比较密码哈希，降低时序侧信道风险。"""
    actual = hash_password(password, salt_hex)[1]
    return secrets.compare_digest(actual, expected_hash)


def audit(db, admin_id: int, action: str, details: str = "") -> None:
    """记录管理员的题库、账号或考试配置变更。"""
    db.execute(
        "INSERT INTO admin_audit(admin_id, action, details, created_at) VALUES (?, ?, ?, ?)",
        (admin_id, action, details[:500], time.time()),
    )


def import_bundled_pdf_if_empty() -> int:
    """题库为空时按 PDF 原题型与题号顺序导入，已有数据时不覆盖。"""
    with connect() as db:
        existing = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    if existing:
        return 0
    pdf_path = ROOT_DIR / "题库（电子版）.pdf"
    if not pdf_path.exists():
        return 0
    from pdf_importer import parse_question_bank

    questions = parse_question_bank(pdf_path.read_bytes())
    now = time.time()
    with connect() as db:
        if db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]:
            return 0
        db.executemany(
            """INSERT INTO questions(section,number,source_page,stem,options_json,answer,explanation,
               subject,difficulty,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'','',1,?,?)""",
            [
                (
                    q.section,
                    q.number,
                    q.page,
                    q.stem,
                    json.dumps(q.options, ensure_ascii=False),
                    q.answer,
                    q.explanation,
                    now,
                    now,
                )
                for q in questions
            ],
        )
    return len(questions)
