import json
import secrets
import sqlite3
import time

TTL_SECONDS = 600  # предложение действует 10 минут


class ActionStore:
    """Предложения изменений, ждущие подтверждения. Это же и журнал: записи не удаляются.
    Выполнить предложение можно только один раз: claim() атомарно переводит pending -> running."""

    def __init__(self, path=":memory:", ttl=TTL_SECONDS):
        self.ttl = ttl
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY, kind TEXT, payload TEXT, preview TEXT, status TEXT,
            created REAL, expires REAL, finished REAL, result TEXT)""")
        self.db.commit()
        self.new_ids = []

    def create(self, kind, payload, preview):
        aid = secrets.token_hex(4)
        now = time.time()
        self.db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
                        (aid, kind, json.dumps(payload, ensure_ascii=False), preview, "pending", now, now + self.ttl, None, None))
        self.db.commit()
        self.new_ids.append(aid)
        return aid

    def pop_new(self):
        ids, self.new_ids = self.new_ids, []
        return ids

    def get(self, aid):
        row = self.db.execute("SELECT id, kind, payload, preview, status, created, expires, finished, result "
                              "FROM actions WHERE id=?", (aid,)).fetchone()
        if not row:
            return None
        keys = ("id", "kind", "payload", "preview", "status", "created", "expires", "finished", "result")
        d = dict(zip(keys, row))
        d["payload"] = json.loads(d["payload"])
        return d

    def claim(self, aid):
        """Забрать предложение на выполнение. Вернёт (kind, payload) ровно один раз, иначе None."""
        cur = self.db.execute("UPDATE actions SET status='running' WHERE id=? AND status='pending' AND expires>?",
                              (aid, time.time()))
        self.db.commit()
        if cur.rowcount != 1:
            self.db.execute("UPDATE actions SET status='expired', finished=? WHERE id=? AND status='pending'", (time.time(), aid))
            self.db.commit()
            return None
        a = self.get(aid)
        return a["kind"], a["payload"]

    def cancel(self, aid):
        cur = self.db.execute("UPDATE actions SET status='cancelled', finished=? WHERE id=? AND status='pending'", (time.time(), aid))
        self.db.commit()
        return cur.rowcount == 1

    def finish(self, aid, ok, result):
        self.db.execute("UPDATE actions SET status=?, finished=?, result=? WHERE id=?",
                        ("done" if ok else "failed", time.time(), result, aid))
        self.db.commit()

    def recent(self, limit=10):
        rows = self.db.execute("SELECT id, kind, status, created, result FROM actions ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("id", "kind", "status", "created", "result"), r)) for r in rows]
