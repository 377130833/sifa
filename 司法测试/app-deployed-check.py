"""Small, dependency-light web application for the SIFA question bank."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from database import (
    ROOT_DIR,
    audit,
    connect,
    database_label,
    hash_password,
    import_bundled_pdf_if_empty,
    init_db,
    verify_password,
)
from logic import SECTION_LABELS, SECTIONS, grade_answer, public_question, validate_exam_config
from pdf_importer import EXPECTED_COUNTS, parse_question_bank

SESSION_COOKIE = "sifa_session"
SESSION_TTL = 60 * 60 * 24 * 14
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 45 * 1024 * 1024
STATIC_DIR = ROOT_DIR / "static"
LOGGER = logging.getLogger("sifa")


def configure_logging(log_directory: Path | None = None):
    """配置服务端日志，同时输出到控制台和可轮转的日志文件。"""
    target_directory = log_directory or ROOT_DIR / "instance" / "logs"
    target_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    # 重复配置时先释放旧文件句柄，防止日志重复写入或文件被占用。
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        target_directory / "sifa.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(console_handler)
    LOGGER.addHandler(file_handler)


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def now_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def json_value(value):
    return json.loads(value) if value else None


def db_exam_config(db) -> dict:
    row = db.execute("SELECT value_json FROM settings WHERE `key`='exam_config'").fetchone()
    return validate_exam_config(json.loads(row[0]))


def question_snapshot(row) -> dict:
    return {
        "id": row["id"],
        "section": row["section"],
        "number": row["number"],
        "source_page": row["source_page"],
        "stem": row["stem"],
        "options": json.loads(row["options_json"] or "[]"),
        "answer": row["answer"],
        "explanation": row["explanation"] or "",
        "subject": row["subject"] or "",
        "difficulty": row["difficulty"] or "",
    }


def exam_public_payload(db, attempt) -> dict:
    items = db.execute(
        "SELECT order_index, question_id, snapshot_json, answer_json FROM exam_items WHERE exam_id=? ORDER BY order_index",
        (attempt["id"],),
    ).fetchall()
    public_items = []
    for item in items:
        snapshot = json.loads(item["snapshot_json"])
        snapshot.pop("answer", None)
        snapshot.pop("explanation", None)
        snapshot["order_index"] = item["order_index"]
        snapshot["user_answer"] = json.loads(item["answer_json"] or "null")
        public_items.append(snapshot)
    return {
        "id": attempt["id"],
        "status": attempt["status"],
        "started_at": attempt["started_at"],
        "ends_at": attempt["ends_at"],
        "config": json.loads(attempt["config_json"]),
        "items": public_items,
    }


def grade_exam(db, exam_id: int) -> dict:
    """依据该场考试配置快照评分并锁定交卷结果。"""
    attempt = db.execute("SELECT * FROM exam_attempts WHERE id=?", (exam_id,)).fetchone()
    if attempt is None:
        raise ApiError(404, "没有找到这场考试。")
    if attempt["status"] == "submitted":
        return exam_result(db, attempt)

    config = validate_exam_config(json.loads(attempt["config_json"]))
    rows = db.execute(
        "SELECT order_index, question_id, snapshot_json, answer_json FROM exam_items WHERE exam_id=? ORDER BY order_index",
        (exam_id,),
    ).fetchall()
    result_items = []
    score = 0.0
    max_score = 0.0
    for row in rows:
        question = json.loads(row["snapshot_json"])
        section = question["section"]
        points = float(config["points"].get(section, 0))
        max_score += points
        submitted = json.loads(row["answer_json"] or "null")
        graded = grade_answer(
            section,
            submitted,
            question["answer"],
            points,
            config["fill_partial_credit"],
            config["multiple_partial_credit"],
            config["wrong_penalties"].get(section, 0),
        )
        score += graded["earned"]
        result_items.append(
            {
                "order_index": row["order_index"],
                "id": question["id"],
                "section": section,
                "number": question["number"],
                "source_page": question["source_page"],
                "stem": question["stem"],
                "options": question["options"],
                "explanation": question["explanation"],
                "user_answer": submitted,
                "correct_answer": question["answer"],
                "is_correct": graded["correct"],
                "earned": round(graded["earned"], 2),
                "points": points,
            }
        )
    submitted_at = time.time()
    db.execute(
        "UPDATE exam_attempts SET submitted_at=?,status='submitted',score=?,max_score=? WHERE id=?",
        (submitted_at, round(score, 2), round(max_score, 2), exam_id),
    )
    updated = db.execute("SELECT * FROM exam_attempts WHERE id=?", (exam_id,)).fetchone()
    return {
        "id": exam_id,
        "status": "submitted",
        "started_at": updated["started_at"],
        "submitted_at": submitted_at,
        "score": round(score, 2),
        "max_score": round(max_score, 2),
        "accuracy": round(100 * sum(item["is_correct"] for item in result_items) / max(1, len(result_items)), 1),
        "config": config,
        "items": result_items,
    }


def exam_result(db, attempt) -> dict:
    """从已保存的试卷快照重建逐题复盘结果。"""
    rows = db.execute(
        "SELECT order_index, snapshot_json, answer_json FROM exam_items WHERE exam_id=? ORDER BY order_index",
        (attempt["id"],),
    ).fetchall()
    config = json.loads(attempt["config_json"])
    items = []
    for row in rows:
        question = json.loads(row["snapshot_json"])
        submitted = json.loads(row["answer_json"] or "null")
        points = float(config["points"].get(question["section"], 0))
        graded = grade_answer(
            question["section"],
            submitted,
            question["answer"],
            points,
            config.get("fill_partial_credit", False),
            config.get("multiple_partial_credit", False),
            config.get("wrong_penalties", {}).get(question["section"], 0),
        )
        items.append(
            {
                "order_index": row["order_index"],
                "id": question["id"],
                "section": question["section"],
                "number": question["number"],
                "source_page": question["source_page"],
                "stem": question["stem"],
                "options": question["options"],
                "explanation": question["explanation"],
                "user_answer": submitted,
                "correct_answer": question["answer"],
                "is_correct": graded["correct"],
                "earned": round(graded["earned"], 2),
                "points": points,
            }
        )
    return {
        "id": attempt["id"],
        "status": attempt["status"],
        "started_at": attempt["started_at"],
        "submitted_at": attempt["submitted_at"],
        "score": attempt["score"],
        "max_score": attempt["max_score"],
        "accuracy": round(100 * sum(item["is_correct"] for item in items) / max(1, len(items)), 1),
        "config": config,
        "items": items,
    }


class SifaHandler(BaseHTTPRequestHandler):
    server_version = "SifaPractice/1.0"

    def log_message(self, format_string, *args):
        """记录 HTTP 请求访问日志，包括响应状态码和客户端地址。"""
        client_address = self.client_address[0] if self.client_address else "unknown"
        LOGGER.info("PID %s 客户端 %s - %s", os.getpid(), client_address, format_string % args)

    def _send_bytes(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status: int, payload: dict, extra_headers: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8", extra_headers)

    def send_error_json(self, error: ApiError):
        self.send_json(error.status, {"error": error.message, **error.extra})

    def read_body(self, limit: int = MAX_JSON_BYTES) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "请求长度无效。") from exc
        if size < 0 or size > limit:
            raise ApiError(413, "上传内容超过允许大小。")
        return self.rfile.read(size)

    def read_json(self) -> dict:
        raw = self.read_body()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "请求数据不是有效的 JSON。") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "请求数据格式不正确。")
        return value

    def session(self):
        """校验会话 Cookie 并读取有效用户，停用账号不能继续使用会话。"""
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        token_hash = hashlib.sha256(morsel.value.encode("ascii", "ignore")).hexdigest()
        with connect() as db:
            row = db.execute(
                """SELECT s.token_hash,s.csrf_token,u.id,u.username,u.display_name,u.role,u.enabled
                   FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1""",
                (token_hash, time.time()),
            ).fetchone()
        return dict(row) if row else None

    def require_user(self) -> dict:
        user = self.session()
        if not user:
            raise ApiError(401, "请先登录。")
        return user

    def require_admin(self) -> dict:
        user = self.require_user()
        if user["role"] != "admin":
            raise ApiError(403, "只有管理员可以执行此操作。")
        return user

    def require_csrf(self, user: dict):
        if not secrets.compare_digest(str(user["csrf_token"]), str(self.headers.get("X-CSRF-Token", ""))):
            raise ApiError(403, "安全令牌已失效，请刷新页面后重试。")

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/" or parsed.path.startswith("/static/"):
                self.serve_static(parsed.path)
                return
            user = self.require_user()
            params = parse_qs(parsed.query)
            self.route_get(parsed.path, params, user)
        except ApiError as error:
            self.send_error_json(error)
        except Exception:
            LOGGER.exception("GET 请求发生未处理异常，路径：%s", urlparse(self.path).path)
            self.send_error_json(ApiError(500, "服务器处理请求时发生错误。"))

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/login":
                self.login()
                return
            user = self.require_user()
            self.require_csrf(user)
            self.route_post(path, user)
        except ApiError as error:
            self.send_error_json(error)
        except Exception:
            LOGGER.exception("POST 请求发生未处理异常，路径：%s", urlparse(self.path).path)
            self.send_error_json(ApiError(500, "服务器处理请求时发生错误。"))

    def do_DELETE(self):
        try:
            user = self.require_admin()
            self.require_csrf(user)
            self.route_delete(urlparse(self.path).path, user)
        except ApiError as error:
            self.send_error_json(error)
        except Exception:
            LOGGER.exception("DELETE 请求发生未处理异常，路径：%s", urlparse(self.path).path)
            self.send_error_json(ApiError(500, "服务器处理请求时发生错误。"))

    def serve_static(self, path: str):
        relative = "index.html" if path == "/" else path.removeprefix("/static/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            raise ApiError(404, "页面不存在。")
        if not target.is_file():
            raise ApiError(404, "页面不存在。")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._send_bytes(200, target.read_bytes(), content_type)

    def login(self):
        """验证账号密码，创建服务端会话并设置安全 Cookie。"""
        data = self.read_json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if not username or not password:
            raise ApiError(400, "请输入用户名和密码。")
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE username=? AND enabled=1", (username,)).fetchone()
            if not user or not verify_password(password, user["password_salt"], user["password_hash"]):
                # Keep the response indistinguishable for unknown and incorrect credentials.
                raise ApiError(401, "用户名或密码不正确。")
            token = secrets.token_urlsafe(40)
            csrf = secrets.token_urlsafe(24)
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            db.execute("INSERT INTO sessions(token_hash,csrf_token,user_id,expires_at) VALUES(?,?,?,?)", (token_hash, csrf, user["id"], time.time() + SESSION_TTL))
            result = {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "role": user["role"], "csrf_token": csrf}
        secure = "; Secure" if os.environ.get("SIFA_COOKIE_SECURE") == "1" else ""
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}{secure}"
        self.send_json(200, {"user": result}, {"Set-Cookie": cookie})

    def route_get(self, path: str, params: dict[str, list[str]], user: dict):
        if path == "/api/me":
            self.send_json(200, {"user": {key: user[key] for key in ("id", "username", "display_name", "role")}, "csrf_token": user["csrf_token"]})
            return
        if path == "/api/dashboard":
            self.dashboard(user)
            return
        if path == "/api/bank":
            self.bank()
            return
        if path == "/api/exam/config":
            with connect() as db:
                config = db_exam_config(db)
                self.send_json(200, {"config": config, "official_values_found": config["official_verified"]})
            return
        if path == "/api/practice/progress":
            self.practice_progress(user)
            return
        if path == "/api/practice/question":
            self.practice_question(params, user)
            return
        if path == "/api/practice/wrong":
            self.question_collection("wrong", params, user)
            return
        if path == "/api/favorites":
            self.question_collection("favorites", params, user)
            return
        if path == "/api/exams/current":
            self.exam_current(user)
            return
        if path == "/api/exams/history":
            self.exam_history(user)
            return
        if path.startswith("/api/exams/"):
            self.exam_review(path, user)
            return
        if path == "/api/admin/settings":
            self.require_admin()
            with connect() as db:
                config = db_exam_config(db)
                self.send_json(200, {"config": config, "official_values_found": config["official_verified"]})
            return
        if path == "/api/admin/users":
            self.admin_users(user)
            return
        if path == "/api/admin/questions":
            self.admin_questions(params, user)
            return
        if path == "/api/admin/audit":
            self.admin_audit(user)
            return
        raise ApiError(404, "接口不存在。")

    def route_post(self, path: str, user: dict):
        if path == "/api/logout":
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (user["token_hash"],))
            self.send_json(200, {"ok": True}, {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
            return
        if path == "/api/password":
            self.change_password(user)
            return
        if path == "/api/practice/answer":
            self.practice_answer(user)
            return
        if path == "/api/favorites/toggle":
            self.favorite_toggle(user)
            return
        if path == "/api/exams/start":
            self.exam_start(user)
            return
        if path == "/api/exams/answer":
            self.exam_save_answer(user)
            return
        if path == "/api/exams/submit":
            self.exam_submit(user)
            return
        if path == "/api/admin/settings":
            self.admin_save_settings(user)
            return
        if path == "/api/admin/users":
            self.admin_create_user(user)
            return
        if path == "/api/admin/users/import":
            self.admin_import_users(user)
            return
        if path == "/api/admin/questions":
            self.admin_save_question(user)
            return
        if path == "/api/admin/import/pdf":
            self.admin_preview_pdf(user)
            return
        if path == "/api/admin/import/commit":
            self.admin_commit_pdf(user)
            return
        if path.startswith("/api/admin/users/") and path.endswith("/reset-password"):
            self.admin_reset_password(path, user)
            return
        if path.startswith("/api/admin/users/") and path.endswith("/toggle"):
            self.admin_toggle_user(path, user)
            return
        raise ApiError(404, "接口不存在。")

    def route_delete(self, path: str, user: dict):
        if path.startswith("/api/admin/questions/"):
            try:
                question_id = int(path.rsplit("/", 1)[1])
            except ValueError as exc:
                raise ApiError(400, "题目编号无效。") from exc
            with connect() as db:
                cursor = db.execute("UPDATE questions SET active=0,updated_at=? WHERE id=?", (time.time(), question_id))
                if cursor.rowcount == 0:
                    raise ApiError(404, "题目不存在。")
                audit(db, user["id"], "停用题目", str(question_id))
            self.send_json(200, {"ok": True})
            return
        raise ApiError(404, "接口不存在。")

    def dashboard(self, user: dict):
        with connect() as db:
            total = db.execute("SELECT COUNT(*) FROM questions WHERE active=1").fetchone()[0]
            done = db.execute("SELECT COUNT(*) FROM practice_progress p JOIN questions q ON q.id=p.question_id WHERE p.user_id=? AND q.active=1", (user["id"],)).fetchone()[0]
            wrong = db.execute("SELECT COUNT(*) FROM practice_progress WHERE user_id=? AND incorrect_count>0", (user["id"],)).fetchone()[0]
            favorites = db.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (user["id"],)).fetchone()[0]
            exams = db.execute("SELECT COUNT(*) FROM exam_attempts WHERE user_id=? AND status='submitted'", (user["id"],)).fetchone()[0]
            bank = self._bank_counts(db)
            last = db.execute("SELECT id,started_at,submitted_at,score,max_score FROM exam_attempts WHERE user_id=? AND status='submitted' ORDER BY submitted_at DESC LIMIT 1", (user["id"],)).fetchone()
            last_exam = dict(last) if last else None
        self.send_json(200, {"total_questions": total, "completed_questions": done, "completion_percent": round(done * 100 / total, 1) if total else 0, "wrong_count": wrong, "favorite_count": favorites, "exam_count": exams, "bank": bank, "last_exam": last_exam})

    def _bank_counts(self, db):
        counts = {section: 0 for section in SECTIONS}
        for row in db.execute("SELECT section,COUNT(*) AS amount FROM questions WHERE active=1 GROUP BY section"):
            counts[row["section"]] = row["amount"]
        return counts

    def bank(self):
        with connect() as db:
            counts = self._bank_counts(db)
        self.send_json(200, {"counts": counts, "labels": SECTION_LABELS, "expected_counts": EXPECTED_COUNTS})

    def practice_progress(self, user: dict):
        with connect() as db:
            result = {}
            for section in SECTIONS:
                total = db.execute("SELECT COUNT(*) FROM questions WHERE active=1 AND section=?", (section,)).fetchone()[0]
                row = db.execute(
                    """SELECT COUNT(*) AS done, SUM(CASE WHEN p.incorrect_count>0 THEN 1 ELSE 0 END) AS wrong
                       FROM practice_progress p JOIN questions q ON q.id=p.question_id
                       WHERE p.user_id=? AND q.section=? AND q.active=1""",
                    (user["id"], section),
                ).fetchone()
                result[section] = {"total": total, "done": row["done"], "wrong": row["wrong"] or 0}
        self.send_json(200, {"sections": result})

    def practice_question(self, params, user: dict):
        """按 PDF 顺序找到学生尚未完成的下一题，并隐藏标准答案。"""
        section = (params.get("section") or [""])[0]
        if section not in SECTIONS:
            raise ApiError(400, "题型无效。")
        number_text = (params.get("number") or [""])[0]
        with connect() as db:
            if number_text:
                try:
                    number = int(number_text)
                except ValueError as exc:
                    raise ApiError(400, "题号无效。") from exc
                row = db.execute("SELECT * FROM questions WHERE section=? AND number=? AND active=1", (section, number)).fetchone()
                completed = False
            else:
                row = db.execute(
                    """SELECT q.* FROM questions q LEFT JOIN practice_progress p
                       ON p.question_id=q.id AND p.user_id=?
                       WHERE q.section=? AND q.active=1 AND p.question_id IS NULL
                       ORDER BY q.number LIMIT 1""",
                    (user["id"], section),
                ).fetchone()
                completed = row is None
                if row is None:
                    row = db.execute("SELECT * FROM questions WHERE section=? AND active=1 ORDER BY number LIMIT 1", (section,)).fetchone()
            if not row:
                raise ApiError(404, "该题型暂无题目。")
            progress = db.execute("SELECT * FROM practice_progress WHERE user_id=? AND question_id=?", (user["id"], row["id"])).fetchone()
            favorite = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND question_id=?", (user["id"], row["id"])).fetchone() is not None
            question = public_question(row, favorite=favorite)
            question["completed"] = completed
            question["progress"] = {"attempts": progress["attempts"], "last_result": bool(progress["last_result"])} if progress else None
        self.send_json(200, {"question": question})

    def practice_answer(self, user: dict):
        """即时判定练习答案并原子更新学生的逐题统计。"""
        data = self.read_json()
        try:
            question_id = int(data.get("question_id"))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "题目编号无效。") from exc
        with connect() as db:
            row = db.execute("SELECT * FROM questions WHERE id=? AND active=1", (question_id,)).fetchone()
            if row is None:
                raise ApiError(404, "题目不存在或已停用。")
            result = grade_answer(row["section"], data.get("answer"), row["answer"], 1)
            now = time.time()
            serialized = json.dumps(data.get("answer"), ensure_ascii=False)
            db.execute(
                """INSERT INTO practice_progress(user_id,question_id,attempts,correct_count,incorrect_count,last_answer,last_result,last_seen_at)
                   VALUES(?,?,1,?,?,?,?,?) ON CONFLICT(user_id,question_id) DO UPDATE SET
                   attempts=practice_progress.attempts+1,
                   correct_count=practice_progress.correct_count+excluded.correct_count,
                   incorrect_count=practice_progress.incorrect_count+excluded.incorrect_count,
                   last_answer=excluded.last_answer,last_result=excluded.last_result,last_seen_at=excluded.last_seen_at""",
                (user["id"], question_id, int(result["correct"]), int(not result["correct"]), serialized, int(result["correct"]), now),
            )
            explanation = row["explanation"] or ""
            answer = row["answer"]
        self.send_json(200, {"correct": result["correct"], "correct_answer": answer, "explanation": explanation, "submitted_answer": data.get("answer")})

    def question_collection(self, kind: str, params, user: dict):
        page = max(1, int((params.get("page") or ["1"])[0]))
        limit = 40
        offset = (page - 1) * limit
        where = "f.user_id=?" if kind == "favorites" else "p.user_id=? AND p.incorrect_count>0"
        join = "JOIN favorites f ON f.question_id=q.id" if kind == "favorites" else "JOIN practice_progress p ON p.question_id=q.id"
        with connect() as db:
            rows = db.execute(
                f"SELECT q.* FROM questions q {join} WHERE {where} AND q.active=1 ORDER BY CASE q.section WHEN 'single' THEN 0 WHEN 'multiple' THEN 1 WHEN 'true_false' THEN 2 WHEN 'fill_blank' THEN 3 ELSE 4 END,q.number LIMIT ? OFFSET ?",
                (user["id"], limit, offset),
            ).fetchall()
            questions = [public_question(row, favorite=(kind == "favorites")) for row in rows]
        self.send_json(200, {"questions": questions, "page": page, "has_more": len(questions) == limit})

    def favorite_toggle(self, user: dict):
        data = self.read_json()
        try:
            question_id = int(data.get("question_id"))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "题目编号无效。") from exc
        with connect() as db:
            if not db.execute("SELECT 1 FROM questions WHERE id=? AND active=1", (question_id,)).fetchone():
                raise ApiError(404, "题目不存在。")
            existing = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND question_id=?", (user["id"], question_id)).fetchone()
            if existing:
                db.execute("DELETE FROM favorites WHERE user_id=? AND question_id=?", (user["id"], question_id))
                favorite = False
            else:
                db.execute("INSERT INTO favorites(user_id,question_id,created_at) VALUES(?,?,?)", (user["id"], question_id, time.time()))
                favorite = True
        self.send_json(200, {"favorite": favorite})

    def exam_start(self, user: dict):
        """按管理员配置随机组卷，并保存不可变的考试题目与配置快照。"""
        with connect() as db:
            existing = db.execute("SELECT * FROM exam_attempts WHERE user_id=? AND status='in_progress' ORDER BY started_at DESC LIMIT 1", (user["id"],)).fetchone()
            if existing:
                if existing["ends_at"] <= time.time():
                    result = grade_exam(db, existing["id"])
                    self.send_json(200, {"result": result})
                    return
                self.send_json(200, {"exam": exam_public_payload(db, existing), "resumed": True})
                return
            config = db_exam_config(db)
            selected = []
            for section in SECTIONS:
                needed = config["counts"][section]
                available = db.execute("SELECT COUNT(*) FROM questions WHERE active=1 AND section=?", (section,)).fetchone()[0]
                if needed > available:
                    raise ApiError(400, f"{SECTION_LABELS[section]}题量不足：需要 {needed} 题，当前可用 {available} 题。")
                if needed:
                    selected.extend(db.execute("SELECT * FROM questions WHERE active=1 AND section=? ORDER BY RANDOM() LIMIT ?", (section, needed)).fetchall())
            # Shuffle the whole paper after sampling each category.
            import random

            random.SystemRandom().shuffle(selected)
            started = time.time()
            ends = started + config["duration_minutes"] * 60
            cursor = db.execute(
                "INSERT INTO exam_attempts(user_id,started_at,ends_at,status,config_json) VALUES(?,?,?,'in_progress',?)",
                (user["id"], started, ends, json.dumps(config, ensure_ascii=False)),
            )
            exam_id = cursor.lastrowid
            db.executemany(
                "INSERT INTO exam_items(exam_id,order_index,question_id,snapshot_json) VALUES(?,?,?,?)",
                [
                    (exam_id, index, row["id"], json.dumps(question_snapshot(row), ensure_ascii=False))
                    for index, row in enumerate(selected)
                ],
            )
            attempt = db.execute("SELECT * FROM exam_attempts WHERE id=?", (exam_id,)).fetchone()
            payload = exam_public_payload(db, attempt)
        self.send_json(201, {"exam": payload, "resumed": False})

    def exam_current(self, user: dict):
        with connect() as db:
            attempt = db.execute("SELECT * FROM exam_attempts WHERE user_id=? AND status='in_progress' ORDER BY started_at DESC LIMIT 1", (user["id"],)).fetchone()
            if attempt and attempt["ends_at"] <= time.time():
                self.send_json(200, {"result": grade_exam(db, attempt["id"])})
                return
            if not attempt:
                self.send_json(200, {"exam": None})
                return
            self.send_json(200, {"exam": exam_public_payload(db, attempt)})

    def exam_save_answer(self, user: dict):
        """将当前答案持久化到指定学生的进行中考试。"""
        data = self.read_json()
        try:
            exam_id = int(data.get("exam_id"))
            question_id = int(data.get("question_id"))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "考试或题目编号无效。") from exc
        with connect() as db:
            exam = db.execute("SELECT * FROM exam_attempts WHERE id=? AND user_id=?", (exam_id, user["id"])).fetchone()
            if not exam:
                raise ApiError(404, "考试记录不存在。")
            if exam["status"] != "in_progress":
                raise ApiError(409, "该考试已交卷。")
            if exam["ends_at"] <= time.time():
                result = grade_exam(db, exam_id)
                self.send_json(409, {"error": "考试时间已结束，已自动交卷。", "result": result})
                return
            cursor = db.execute("UPDATE exam_items SET answer_json=? WHERE exam_id=? AND question_id=?", (json.dumps(data.get("answer"), ensure_ascii=False), exam_id, question_id))
            if cursor.rowcount == 0:
                raise ApiError(404, "题目不属于这场考试。")
        self.send_json(200, {"ok": True, "saved_at": time.time()})

    def exam_submit(self, user: dict):
        """交卷并返回自动评分后的逐题复盘。"""
        data = self.read_json()
        try:
            exam_id = int(data.get("exam_id"))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "考试编号无效。") from exc
        with connect() as db:
            attempt = db.execute("SELECT * FROM exam_attempts WHERE id=? AND user_id=?", (exam_id, user["id"])).fetchone()
            if not attempt:
                raise ApiError(404, "考试记录不存在。")
            result = grade_exam(db, exam_id)
        self.send_json(200, {"result": result})

    def exam_history(self, user: dict):
        with connect() as db:
            rows = db.execute("SELECT id,started_at,submitted_at,score,max_score,status,config_json FROM exam_attempts WHERE user_id=? ORDER BY started_at DESC LIMIT 30", (user["id"],)).fetchall()
        self.send_json(200, {"exams": [{"id": row["id"], "started_at": row["started_at"], "submitted_at": row["submitted_at"], "score": row["score"], "max_score": row["max_score"], "status": row["status"], "name": json.loads(row["config_json"]).get("name", "模拟考试")} for row in rows]})

    def exam_review(self, path: str, user: dict):
        try:
            exam_id = int(path.rsplit("/", 1)[1])
        except ValueError as exc:
            raise ApiError(400, "考试编号无效。") from exc
        with connect() as db:
            attempt = db.execute("SELECT * FROM exam_attempts WHERE id=? AND user_id=?", (exam_id, user["id"])).fetchone()
            if not attempt:
                raise ApiError(404, "考试记录不存在。")
            if attempt["status"] != "submitted":
                raise ApiError(409, "考试尚未交卷。")
            result = exam_result(db, attempt)
        self.send_json(200, {"result": result})

    def admin_users(self, user: dict):
        self.require_admin()
        with connect() as db:
            rows = db.execute("SELECT id,username,display_name,role,enabled,created_at FROM users ORDER BY role,username").fetchall()
        self.send_json(200, {"users": [dict(row) for row in rows]})

    def admin_create_user(self, user: dict):
        self.require_admin()
        data = self.read_json()
        username = str(data.get("username", "")).strip()
        display_name = str(data.get("display_name") or username).strip()
        password = str(data.get("password", ""))
        self._validate_user_fields(username, display_name, password)
        salt, digest = hash_password(password)
        try:
            with connect() as db:
                cursor = db.execute("INSERT INTO users(username,display_name,password_salt,password_hash,role,created_at) VALUES(?,?,?,?, 'student',?)", (username, display_name, salt, digest, time.time()))
                audit(db, user["id"], "创建学生账号", username)
                user_id = cursor.lastrowid
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ApiError(409, "该用户名已存在。") from exc
            raise
        self.send_json(201, {"user": {"id": user_id, "username": username, "display_name": display_name, "role": "student", "enabled": 1}})

    def _validate_user_fields(self, username: str, display_name: str, password: str):
        if not re_full_username(username):
            raise ApiError(400, "用户名需为 3-40 位字母、数字、下划线或短横线。")
        if not display_name or len(display_name) > 80:
            raise ApiError(400, "请填写不超过 80 个字符的显示姓名。")
        if len(password) < 8 or len(password) > 256:
            raise ApiError(400, "密码长度需在 8 至 256 个字符之间。")

    def admin_import_users(self, user: dict):
        """验证 CSV 后批量创建学生账号，并逐条反馈失败行。"""
        self.require_admin()
        data = self.read_json()
        users = data.get("users")
        if not isinstance(users, list) or not users or len(users) > 500:
            raise ApiError(400, "请上传 1 至 500 条账号记录。")
        created, errors = [], []
        for index, item in enumerate(users, 1):
            if not isinstance(item, dict):
                errors.append({"row": index, "error": "行格式无效"})
                continue
            username = str(item.get("username", "")).strip()
            display_name = str(item.get("display_name") or username).strip()
            password = str(item.get("password", ""))
            try:
                self._validate_user_fields(username, display_name, password)
                salt, digest = hash_password(password)
                with connect() as db:
                    cursor = db.execute("INSERT INTO users(username,display_name,password_salt,password_hash,role,created_at) VALUES(?,?,?,?, 'student',?)", (username, display_name, salt, digest, time.time()))
                    audit(db, user["id"], "批量创建学生账号", username)
                    created.append({"id": cursor.lastrowid, "username": username, "display_name": display_name})
            except Exception as exc:
                errors.append({"row": index, "username": username, "error": str(exc) if isinstance(exc, ApiError) else ("用户名已存在" if "UNIQUE" in str(exc) else "创建失败")})
        self.send_json(200, {"created": created, "errors": errors})

    def admin_reset_password(self, path: str, user: dict):
        self.require_admin()
        try:
            target_id = int(path.split("/")[4])
        except (ValueError, IndexError) as exc:
            raise ApiError(400, "账号编号无效。") from exc
        data = self.read_json()
        password = str(data.get("password", ""))
        if len(password) < 8 or len(password) > 256:
            raise ApiError(400, "密码长度需在 8 至 256 个字符之间。")
        salt, digest = hash_password(password)
        with connect() as db:
            row = db.execute("SELECT id,role FROM users WHERE id=?", (target_id,)).fetchone()
            if not row or row["role"] != "student":
                raise ApiError(404, "学生账号不存在。")
            db.execute("UPDATE users SET password_salt=?,password_hash=? WHERE id=?", (salt, digest, target_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
            audit(db, user["id"], "重置学生密码", str(target_id))
        self.send_json(200, {"ok": True})

    def admin_toggle_user(self, path: str, user: dict):
        self.require_admin()
        try:
            target_id = int(path.split("/")[4])
        except (ValueError, IndexError) as exc:
            raise ApiError(400, "账号编号无效。") from exc
        data = self.read_json()
        enabled = bool(data.get("enabled"))
        with connect() as db:
            row = db.execute("SELECT role FROM users WHERE id=?", (target_id,)).fetchone()
            if not row or row["role"] != "student":
                raise ApiError(404, "学生账号不存在。")
            db.execute("UPDATE users SET enabled=? WHERE id=?", (int(enabled), target_id))
            if not enabled:
                db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
            audit(db, user["id"], "启用学生账号" if enabled else "停用学生账号", str(target_id))
        self.send_json(200, {"ok": True, "enabled": enabled})

    def admin_questions(self, params, user: dict):
        self.require_admin()
        section = (params.get("section") or [""])[0]
        search = (params.get("q") or [""])[0].strip()
        try:
            page = max(1, int((params.get("page") or ["1"])[0]))
        except ValueError as exc:
            raise ApiError(400, "页码无效。") from exc
        limit = 30
        clauses, values = [], []
        if section in SECTIONS:
            clauses.append("section=?")
            values.append(section)
        if search:
            clauses.append("(stem LIKE ? OR CAST(number AS TEXT)=?)")
            values.extend((f"%{search}%", search))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with connect() as db:
            total = db.execute("SELECT COUNT(*) FROM questions" + where, values).fetchone()[0]
            rows = db.execute("SELECT * FROM questions" + where + " ORDER BY CASE section WHEN 'single' THEN 0 WHEN 'multiple' THEN 1 WHEN 'true_false' THEN 2 WHEN 'fill_blank' THEN 3 ELSE 4 END,number LIMIT ? OFFSET ?", [*values, limit, (page - 1) * limit]).fetchall()
        self.send_json(200, {"questions": [{**public_question(row, include_answer=True), "active": bool(row["active"])} for row in rows], "total": total, "page": page, "page_size": limit})

    def admin_save_question(self, user: dict):
        """新增或修改题目并记录管理员审计信息。"""
        self.require_admin()
        data = self.read_json()
        section = data.get("section")
        if section not in SECTIONS:
            raise ApiError(400, "题型无效。")
        try:
            number = int(data.get("number"))
            source_page = int(data.get("source_page") or 0)
            question_id = int(data.get("id")) if data.get("id") else None
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "题号或页码无效。") from exc
        stem = str(data.get("stem", "")).strip()
        answer = str(data.get("answer", "")).strip()
        explanation = str(data.get("explanation", "")).strip()
        options = data.get("options") or []
        if number < 1 or not stem or not answer or len(stem) > 8000 or len(answer) > 2000:
            raise ApiError(400, "题号、题干和标准答案为必填项，且内容需在合理长度内。")
        if not isinstance(options, list) or len(options) > 8:
            raise ApiError(400, "选项格式无效。")
        cleaned_options = []
        for option in options:
            if not isinstance(option, dict) or not option.get("key") or not option.get("text"):
                raise ApiError(400, "每个选项都需要标号和内容。")
            cleaned_options.append({"key": str(option["key"]).strip().upper()[:2], "text": str(option["text"]).strip()[:3000]})
        now = time.time()
        try:
            with connect() as db:
                if question_id:
                    cursor = db.execute(
                        """UPDATE questions SET section=?,number=?,source_page=?,stem=?,options_json=?,answer=?,explanation=?,
                           subject=?,difficulty=?,active=1,updated_at=? WHERE id=?""",
                        (section, number, source_page, stem, json.dumps(cleaned_options, ensure_ascii=False), answer, explanation, str(data.get("subject") or "")[:120], str(data.get("difficulty") or "")[:40], now, question_id),
                    )
                    if cursor.rowcount == 0:
                        raise ApiError(404, "题目不存在。")
                    audit(db, user["id"], "修改题目", f"{section} {number}")
                else:
                    cursor = db.execute(
                        """INSERT INTO questions(section,number,source_page,stem,options_json,answer,explanation,subject,difficulty,active,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                        (section, number, source_page, stem, json.dumps(cleaned_options, ensure_ascii=False), answer, explanation, str(data.get("subject") or "")[:120], str(data.get("difficulty") or "")[:40], now, now),
                    )
                    question_id = cursor.lastrowid
                    audit(db, user["id"], "新增题目", f"{section} {number}")
        except ApiError:
            raise
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ApiError(409, "该题型下已存在这个题号。") from exc
            raise
        self.send_json(200, {"ok": True, "id": question_id})

    def admin_preview_pdf(self, user: dict):
        """解析上传 PDF 并保存待确认预览，不直接改动现有题库。"""
        self.require_admin()
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/pdf":
            raise ApiError(415, "请上传 PDF 文件。")
        raw = self.read_body(MAX_PDF_BYTES)
        if not raw.startswith(b"%PDF"):
            raise ApiError(400, "文件内容不是有效 PDF。")
        questions = parse_question_bank(raw, require_expected_counts=True)
        counts = {section: sum(question.section == section for question in questions) for section in SECTIONS}
        source_name = unquote(self.headers.get("X-File-Name", "question-bank.pdf"))[:160]
        source_hash = hashlib.sha256(raw).hexdigest()
        job_id = secrets.token_urlsafe(18)
        serialized = [question.as_dict() for question in questions]
        with connect() as db:
            existing = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
            if existing:
                conflicts = sum(
                    db.execute("SELECT 1 FROM questions WHERE section=? AND number=?", (question.section, question.number)).fetchone() is not None
                    for question in questions
                )
            else:
                conflicts = 0
            db.execute("INSERT INTO import_jobs(id,admin_id,source_name,source_sha256,questions_json,counts_json,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, user["id"], source_name, source_hash, json.dumps(serialized, ensure_ascii=False), json.dumps(counts), time.time()))
        samples = []
        for section in SECTIONS:
            samples.extend(
                {"section": item.section, "number": item.number, "page": item.page, "stem": item.stem, "options": item.options, "answer": item.answer}
                for item in questions
                if item.section == section
            )
        sample = samples[:0]
        for section in SECTIONS:
            sample.extend([item for item in samples if item["section"] == section][:3])
        self.send_json(200, {"job_id": job_id, "source_name": source_name, "sha256": source_hash, "counts": counts, "total": len(questions), "existing_questions_to_update": conflicts, "new_questions": len(questions) - conflicts, "sample": sample, "note": "请核对抽样内容及各题答案后再确认导入。导入会更新相同题型和题号的题目。"})

    def admin_commit_pdf(self, user: dict):
        """提交管理员确认过的 PDF 预览并按题型题号写入题库。"""
        self.require_admin()
        data = self.read_json()
        job_id = str(data.get("job_id", ""))
        now = time.time()
        with connect() as db:
            job = db.execute("SELECT * FROM import_jobs WHERE id=? AND admin_id=?", (job_id, user["id"])).fetchone()
            if not job:
                raise ApiError(404, "导入预览不存在或已过期。")
            if job["committed_at"]:
                raise ApiError(409, "这次导入已确认过。")
            questions = json.loads(job["questions_json"])
            for question in questions:
                db.execute(
                    """INSERT INTO questions(section,number,source_page,stem,options_json,answer,explanation,subject,difficulty,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,'','',1,?,?) ON CONFLICT(section,number) DO UPDATE SET
                       source_page=excluded.source_page,stem=excluded.stem,options_json=excluded.options_json,
                       answer=excluded.answer,explanation=excluded.explanation,active=1,updated_at=excluded.updated_at""",
                    (question["section"], question["number"], question["page"], question["stem"], json.dumps(question["options"], ensure_ascii=False), question["answer"], question.get("explanation", ""), now, now),
                )
            db.execute("UPDATE import_jobs SET committed_at=? WHERE id=?", (now, job_id))
            audit(db, user["id"], "导入 PDF 题库", f"{job['source_name']}，共 {len(questions)} 题")
            counts = json.loads(job["counts_json"])
        self.send_json(200, {"ok": True, "imported": sum(counts.values()), "counts": counts})

    def admin_save_settings(self, user: dict):
        """校验考试参数，确保题量可用后保存模板并记录审计。"""
        self.require_admin()
        data = self.read_json()
        try:
            config = validate_exam_config(data.get("config") or {})
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        with connect() as db:
            available = self._bank_counts(db)
            lacking = [f"{SECTION_LABELS[section]}可用题不足" for section in SECTIONS if config["counts"][section] > available[section]]
            if lacking:
                raise ApiError(400, "；".join(lacking) + "。")
            db.execute("INSERT INTO settings(`key`,value_json) VALUES('exam_config',?) ON CONFLICT(`key`) DO UPDATE SET value_json=excluded.value_json", (json.dumps(config, ensure_ascii=False),))
            audit(db, user["id"], "修改考试模板", json.dumps(config, ensure_ascii=False))
        self.send_json(200, {"ok": True, "config": config})

    def admin_audit(self, user: dict):
        self.require_admin()
        with connect() as db:
            rows = db.execute("SELECT a.id,u.username,a.action,a.details,a.created_at FROM admin_audit a JOIN users u ON u.id=a.admin_id ORDER BY a.created_at DESC LIMIT 100").fetchall()
        self.send_json(200, {"events": [dict(row) for row in rows]})

    def change_password(self, user: dict):
        data = self.read_json()
        old_password = str(data.get("old_password", ""))
        new_password = str(data.get("new_password", ""))
        if len(new_password) < 8 or len(new_password) > 256:
            raise ApiError(400, "新密码长度需在 8 至 256 个字符之间。")
        with connect() as db:
            row = db.execute("SELECT password_salt,password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
            if not verify_password(old_password, row["password_salt"], row["password_hash"]):
                raise ApiError(400, "当前密码不正确。")
            salt, digest = hash_password(new_password)
            db.execute("UPDATE users SET password_salt=?,password_hash=? WHERE id=?", (salt, digest, user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], user["token_hash"]))
        self.send_json(200, {"ok": True})


def re_full_username(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_-]{3,40}", value))


def create_admin(username: str):
    """完成数据库迁移和题库初始化后，交互式创建首个管理员。"""
    import getpass

    init_db()
    import_bundled_pdf_if_empty()
    with connect() as db:
        if db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
            raise SystemExit("管理员账号已存在；登录后可在管理员页面创建学生账号。")
    display_name = input("管理员显示姓名 [管理员]: ").strip() or "管理员"
    while True:
        password = getpass.getpass("设置管理员密码（至少 8 位）: ")
        confirm = getpass.getpass("再次输入密码: ")
        if password != confirm:
            print("两次密码不一致，请重试。")
        elif len(password) < 8:
            print("密码至少需要 8 位。")
        else:
            break
    salt, digest = hash_password(password)
    with connect() as db:
        db.execute("INSERT INTO users(username,display_name,password_salt,password_hash,role,created_at) VALUES(?,?,?,?, 'admin',?)", (username, display_name, salt, digest, time.time()))
    print(f"管理员 {username} 已创建。数据库：{database_label()}")


def serve(host: str, port: int):
    """应用数据库迁移、初始化题库并启动并发 HTTP 服务。"""
    configure_logging()
    LOGGER.info("服务进程 PID=%s，程序文件=%s", os.getpid(), Path(__file__).resolve())
    LOGGER.info("日志文件：%s", ROOT_DIR / "instance" / "logs" / "sifa.log")
    init_db()
    imported = import_bundled_pdf_if_empty()
    if imported:
        LOGGER.info("已从项目 PDF 导入 %s 道题。", imported)
    else:
        with connect() as db:
            count = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        LOGGER.info("题库已就绪，共 %s 道题。", count)
    server = ThreadingHTTPServer((host, port), SifaHandler)
    LOGGER.info("学习平台运行于 http://%s:%s", host if host not in ('0.0.0.0','::') else '127.0.0.1', port)
    LOGGER.info("首次使用请先执行：python app.py create-admin --username admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("正在关闭服务器……")
    finally:
        server.server_close()


def main():
    """解析启动、管理员创建及数据库迁移命令。"""
    parser = argparse.ArgumentParser(description="司法鉴定人执业能力评估学习平台")
    parser.set_defaults(host=os.environ.get("SIFA_HOST", "127.0.0.1"), port=int(os.environ.get("SIFA_PORT", "18080")))
    subparsers = parser.add_subparsers(dest="command")
    admin_parser = subparsers.add_parser("create-admin", help="创建首个管理员账号")
    admin_parser.add_argument("--username", default="admin")
    subparsers.add_parser("db-upgrade", help="执行未应用的数据库迁移")
    serve_parser = subparsers.add_parser("serve", help="启动网站服务")
    serve_parser.add_argument("--host", default=os.environ.get("SIFA_HOST", "127.0.0.1"))
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("SIFA_PORT", "18080")))
    args = parser.parse_args()
    if args.command == "create-admin":
        if not re_full_username(args.username):
            parser.error("用户名需为 3-40 位字母、数字、下划线或短横线。")
        create_admin(args.username)
    elif args.command == "db-upgrade":
        init_db()
        print(f"数据库迁移已完成：{database_label()}")
    else:
        serve(args.host, args.port)


if __name__ == "__main__":
    main()
