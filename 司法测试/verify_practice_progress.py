import json
import traceback

from app import SifaHandler
from database import connect

with connect() as db:
    users = db.execute("SELECT id,username FROM users ORDER BY id").fetchall()

for user in users:
    handler = object.__new__(SifaHandler)
    response = {}

    def capture(status, body, content_type, extra_headers=None):
        response.update(status=status, body=body, content_type=content_type)

    handler._send_bytes = capture
    try:
        handler.practice_progress({"id": user["id"]})
        payload = json.loads(response["body"])
        print(user["username"], response["status"], json.dumps(payload, ensure_ascii=False))
    except Exception:
        print(user["username"], "FAILED")
        traceback.print_exc()
