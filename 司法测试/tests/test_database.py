import unittest

from database import MysqlCursor, _mysql_sql


class MysqlSqlCompatibilityTests(unittest.TestCase):
    def test_mysql_cursor_supports_row_iteration(self):
        class FakeCursor:
            def __init__(self):
                self.rows = iter([{"section": "single"}, {"section": "multiple"}])

            def fetchone(self):
                return next(self.rows, None)

        rows = list(MysqlCursor(FakeCursor()))
        self.assertEqual([row["section"] for row in rows], ["single", "multiple"])

    def test_qmark_parameters_are_converted_without_touching_literals(self):
        sql = _mysql_sql("SELECT * FROM questions WHERE stem LIKE ? AND note='literal ?' AND id=?")
        self.assertEqual(sql, "SELECT * FROM questions WHERE stem LIKE %s AND note='literal ?' AND id=%s")

    def test_common_sqlite_upsert_and_random_order_are_converted(self):
        sql = _mysql_sql(
            "INSERT OR IGNORE INTO t(a) VALUES(?) ON CONFLICT(a) DO UPDATE SET b=excluded.b ORDER BY RANDOM()"
        )
        self.assertEqual(
            sql,
            "INSERT IGNORE INTO t(a) VALUES(%s) ON DUPLICATE KEY UPDATE b=VALUES(b) ORDER BY RAND()",
        )

    def test_sqlite_text_cast_is_converted(self):
        self.assertEqual(_mysql_sql("SELECT CAST(number AS TEXT) FROM questions"), "SELECT CAST(number AS CHAR) FROM questions")


if __name__ == "__main__":
    unittest.main()
