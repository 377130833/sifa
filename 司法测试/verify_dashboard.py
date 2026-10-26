import json

from app import SifaHandler
from database import connect

handler = object.__new__(SifaHandler)
response = {}
handler.send_json = lambda status, data: response.update(status=status, data=data)

with connect() as db:
    users = db.execute("SELECT id,username FROM users ORDER BY id").fetchall()

for user in users:
    handler.dashboard({"id": user["id"]})
    assert response["status"] == 200
    print(user["username"], json.dumps(response["data"], ensure_ascii=False))
