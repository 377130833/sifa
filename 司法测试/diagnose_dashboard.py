import traceback

from database import connect, database_label

queries = [
    ("total", "SELECT COUNT(*) FROM questions WHERE active=1", ()),
    ("done", "SELECT COUNT(*) FROM practice_progress p JOIN questions q ON q.id=p.question_id WHERE p.user_id=? AND q.active=1", ()),
    ("wrong", "SELECT COUNT(*) FROM practice_progress WHERE user_id=? AND incorrect_count>0", ()),
    ("favorites", "SELECT COUNT(*) FROM favorites WHERE user_id=?", ()),
    ("exams", "SELECT COUNT(*) FROM exam_attempts WHERE user_id=? AND status='submitted'", ()),
    ("bank", "SELECT section,COUNT(*) AS amount FROM questions WHERE active=1 GROUP BY section", ()),
    ("last", "SELECT id,started_at,submitted_at,score,max_score FROM exam_attempts WHERE user_id=? AND status='submitted' ORDER BY submitted_at DESC LIMIT 1", ()),
]

print(f"Database: {database_label()}")
with connect() as db:
    user = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if user is None:
        raise RuntimeError("No user available for dashboard diagnosis")
    user_id = user["id"]
    print(f"Testing with user id: {user_id}")
    for name, sql, params in queries:
        try:
            actual_params = (user_id,) if "?" in sql else params
            row = db.execute(sql, actual_params).fetchone()
            print(f"OK {name}: {dict(row) if row else row}")
        except Exception:
            print(f"FAILED {name}: {sql}")
            traceback.print_exc()
            break
