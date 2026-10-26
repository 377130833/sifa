import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import app
import database


class ApiSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.old_db_path = database.DB_PATH
        cls.old_db_backend = database.DB_BACKEND
        database.DB_BACKEND = "sqlite"
        database.DB_PATH = Path(cls.temp_dir.name) / "test.sqlite3"
        database.init_db()
        with database.connect() as db:
            now = 1_800_000_000
            for username, role in (("admin", "admin"), ("student", "student")):
                salt, digest = database.hash_password("TestPassword123!")
                db.execute(
                    "INSERT INTO users(username,display_name,password_salt,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                    (username, username, salt, digest, role, now),
                )
            db.execute(
                "INSERT INTO questions(section,number,source_page,stem,options_json,answer,created_at,updated_at) VALUES('single',1,1,'测试题','[{\"key\":\"A\",\"text\":\"正确\"},{\"key\":\"B\",\"text\":\"错误\"}]','A',?,?)",
                (now, now),
            )
            config = {
                "name": "接口测试", "duration_minutes": 30,
                "counts": {"single": 1, "multiple": 0, "true_false": 0, "fill_blank": 0},
                "points": {"single": 1, "multiple": 1, "true_false": 1, "fill_blank": 1},
                "wrong_penalties": {"single": 0, "multiple": 0, "true_false": 0, "fill_blank": 0},
                "multiple_partial_credit": False, "fill_partial_credit": False,
                "official_verified": False, "official_reference": "",
            }
            db.execute("UPDATE settings SET value_json=? WHERE key='exam_config'", (json.dumps(config),))
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.SifaHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        database.DB_PATH = cls.old_db_path
        database.DB_BACKEND = cls.old_db_backend
        cls.temp_dir.cleanup()

    def login(self, username):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request = urllib.request.Request(
            self.base_url + "/api/login",
            data=json.dumps({"username": username, "password": "TestPassword123!"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request) as response:
            data = json.loads(response.read())
        return opener, data["user"]["csrf_token"]

    def request(self, opener, path, method="GET", body=None, csrf=None):
        headers = {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        request = urllib.request.Request(self.base_url + path, data=encoded, headers=headers, method=method)
        try:
            with opener.open(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_student_cannot_manage_and_answers_only_show_after_submission(self):
        student, csrf = self.login("student")
        status, _ = self.request(student, "/api/admin/settings")
        self.assertEqual(status, 403)

        status, practice = self.request(student, "/api/practice/question?section=single")
        self.assertEqual(status, 200)
        self.assertNotIn("answer", practice["question"])
        status, feedback = self.request(
            student, "/api/practice/answer", "POST",
            {"question_id": practice["question"]["id"], "answer": "A"}, csrf,
        )
        self.assertEqual(status, 200)
        self.assertEqual(feedback["correct_answer"], "A")

        status, exam = self.request(student, "/api/exams/start", "POST", {}, csrf)
        self.assertEqual(status, 201)
        self.assertNotIn("answer", exam["exam"]["items"][0])
        status, result = self.request(student, "/api/exams/submit", "POST", {"exam_id": exam["exam"]["id"]}, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(result["result"]["items"][0]["correct_answer"], "A")
        self.assertEqual(result["result"]["score"], 0)

    def test_invalid_exam_settings_return_client_error(self):
        admin, csrf = self.login("admin")
        status, data = self.request(admin, "/api/admin/settings", "POST", {"config": {"duration_minutes": 0}}, csrf)
        self.assertEqual(status, 400)
        self.assertIn("时长", data["error"])

    def test_dashboard_returns_question_bank_summary(self):
        student, _ = self.login("student")
        status, dashboard = self.request(student, "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["total_questions"], 1)
        self.assertEqual(dashboard["bank"]["single"], 1)
        self.assertEqual(dashboard["completed_questions"], 0)

    def test_unhandled_get_error_is_logged_with_traceback(self):
        student, _ = self.login("student")
        log_directory = Path(self.temp_dir.name) / "logs"
        app.configure_logging(log_directory)

        def raise_diagnostic_error(handler, path, params, user):
            raise RuntimeError("test diagnostic marker")

        try:
            with patch.object(app.SifaHandler, "route_get", raise_diagnostic_error):
                status, response = self.request(student, "/api/bank")
            self.assertEqual(status, 500)
            self.assertEqual(response["error"], "服务器处理请求时发生错误。")
            log_text = (log_directory / "sifa.log").read_text(encoding="utf-8")
            self.assertIn("Traceback (most recent call last)", log_text)
            self.assertIn("test diagnostic marker", log_text)
            self.assertIn("/api/bank", log_text)
        finally:
            for handler in app.LOGGER.handlers[:]:
                app.LOGGER.removeHandler(handler)
                handler.close()

    def test_admin_configures_penalty_and_exam_uses_saved_snapshot(self):
        admin, admin_csrf = self.login("admin")
        status, settings = self.request(admin, "/api/admin/settings")
        self.assertEqual(status, 200)
        config = settings["config"]
        config["wrong_penalties"]["single"] = 0.5
        status, saved = self.request(admin, "/api/admin/settings", "POST", {"config": config}, admin_csrf)
        self.assertEqual(status, 200)
        self.assertEqual(saved["config"]["wrong_penalties"]["single"], 0.5)

        student, csrf = self.login("student")
        status, exam = self.request(student, "/api/exams/start", "POST", {}, csrf)
        self.assertEqual(status, 201)
        item = exam["exam"]["items"][0]
        status, _ = self.request(
            student, "/api/exams/answer", "POST",
            {"exam_id": exam["exam"]["id"], "question_id": item["id"], "answer": "B"}, csrf,
        )
        self.assertEqual(status, 200)
        status, result = self.request(student, "/api/exams/submit", "POST", {"exam_id": exam["exam"]["id"]}, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(result["result"]["score"], -0.5)

        status, _ = self.request(student, "/api/admin/settings", "POST", {"config": config}, csrf)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
