"""Случайный участник дня: независим от ИИ, результат неизменен в пределах даты."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
import re
import time

MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")


def clock_time(text):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("Время нужно в формате ЧЧ:ММ, например 12:00 (Москва).")
    return text


class DailyGame:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS participants(
                chat INTEGER, uid INTEGER, name TEXT, username TEXT DEFAULT '',
                active INTEGER DEFAULT 1, opted_out INTEGER DEFAULT 0, seen REAL,
                PRIMARY KEY(chat,uid));
            CREATE TABLE IF NOT EXISTS daily_draws(
                chat INTEGER, day TEXT, uid INTEGER, name TEXT, announced INTEGER DEFAULT 0,
                participants INTEGER, PRIMARY KEY(chat,day));
            CREATE TABLE IF NOT EXISTS sent_memes(
                chat INTEGER, url TEXT, ts REAL, PRIMARY KEY(chat,url));
        """)

    def observe(self, chat, user, active=True):
        if chat >= 0 or not user or user.get("is_bot") or not user.get("id"):
            return
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)[:80] or "Участник"
        with self.db:
            self.db.execute("""INSERT INTO participants(chat,uid,name,username,active,seen)
                VALUES(?,?,?,?,?,?) ON CONFLICT(chat,uid) DO UPDATE SET
                name=excluded.name,username=excluded.username,active=excluded.active,seen=excluded.seen""",
                (chat, user["id"], name, user.get("username", "")[:80], int(active), time.time()))

    def opt_out(self, chat, uid, enabled):
        with self.db:
            self.db.execute("UPDATE participants SET opted_out=? WHERE chat=? AND uid=?", (int(enabled), chat, uid))

    def candidates(self, chat):
        return [dict(row) for row in self.db.execute("""SELECT p.uid,p.name,p.username FROM participants p
            WHERE p.chat=? AND p.active=1 AND p.opted_out=0 AND NOT EXISTS
            (SELECT 1 FROM noroast n WHERE n.chat=p.chat AND n.uid=p.uid) ORDER BY p.uid""", (chat,))]

    def result(self, chat, day):
        row = self.db.execute("SELECT * FROM daily_draws WHERE chat=? AND day=?", (chat, day)).fetchone()
        return dict(row) if row else None

    def choose(self, chat, day):
        with self.db:
            existing = self.result(chat, day)
            if existing:
                return existing
            candidates = self.candidates(chat)
            if len(candidates) < 2:
                return None
            chosen = random.SystemRandom().choice(candidates)
            self.db.execute("INSERT OR IGNORE INTO daily_draws(chat,day,uid,name,participants) VALUES(?,?,?,?,?)",
                            (chat, day, chosen["uid"], chosen["name"], len(candidates)))
            return self.result(chat, day)

    def mark_announced(self, chat, day):
        with self.db:
            self.db.execute("UPDATE daily_draws SET announced=1 WHERE chat=? AND day=?", (chat, day))

    def wins(self, chat, uid):
        return self.db.execute("SELECT COUNT(*) FROM daily_draws WHERE chat=? AND uid=?", (chat, uid)).fetchone()[0]

    def stats(self, chat):
        return [dict(row) for row in self.db.execute("""SELECT d.uid,
            COALESCE(p.name,MAX(d.name)) AS name, COUNT(*) AS wins FROM daily_draws d
            LEFT JOIN participants p ON p.chat=d.chat AND p.uid=d.uid WHERE d.chat=?
            GROUP BY d.uid ORDER BY wins DESC,d.uid LIMIT 30""", (chat,))]

    def recent_memes(self, chat):
        return [row[0] for row in self.db.execute("SELECT url FROM sent_memes WHERE chat=? ORDER BY ts DESC LIMIT 100", (chat,))]

    def record_meme(self, chat, url):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sent_memes VALUES(?,?,?)", (chat, url, time.time()))
            self.db.execute("DELETE FROM sent_memes WHERE chat=? AND url NOT IN (SELECT url FROM sent_memes WHERE chat=? ORDER BY ts DESC LIMIT 100)", (chat, chat))


def local_now():
    return datetime.now(MOSCOW)
