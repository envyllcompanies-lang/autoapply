"""SQLite log of every job seen and what happened to it. Guarantees we never apply twice."""
from __future__ import annotations

import sqlite3
from datetime import datetime, date

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    source TEXT, company TEXT, title TEXT, location TEXT,
    url TEXT, apply_url TEXT,
    first_seen TEXT,
    status TEXT,          -- filtered | low_score | queued | applied | dry_run | blocked | skipped | failed | unconfirmed
    score INTEGER,
    reason TEXT,
    resume_path TEXT, cover_path TEXT, screenshot TEXT,
    attempts INTEGER DEFAULT 0,
    updated TEXT
);
"""

FINAL = ("applied", "filtered", "low_score", "blocked", "skipped", "unconfirmed")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        try:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN fit INTEGER")     # LinkedIn-style match % (prescreen.py)
        except sqlite3.OperationalError:
            pass
        # a submit that showed no confirmation may still have gone through: never retry it automatically
        self.conn.execute("UPDATE jobs SET status='unconfirmed' WHERE status='failed' AND reason LIKE 'no confirmation after submit%'")
        self.conn.commit()

    def requeue_if_new_version(self, version: str) -> int:
        """Jobs skipped/blocked only because of a bug that a newer version fixed get another chance (once per version)."""
        row = self.conn.execute("SELECT v FROM meta WHERE k='requeue'").fetchone()
        if row and row[0] == version:
            return 0
        cur = self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=0 WHERE status IN ('skipped','blocked') AND ("
            " reason LIKE 'can''t truthfully answer%' OR reason LIKE 'no application form%' OR reason LIKE 'could not find the employer%'"
            " OR reason LIKE 'login required%' OR reason LIKE 'unsupported application site%' OR reason LIKE 'not a real application form%'"
            " OR reason LIKE 'form requires a cover letter%' OR reason LIKE 'account:%')")
        # submits the site bounced back with 'X is required' (e.g. the résumé upload didn't register): nothing was sent
        cur2 = self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=0 WHERE status='unconfirmed' AND reason LIKE 'no confirmation after submit; page errors:%'"
            " AND lower(reason) LIKE '%required%' AND lower(reason) NOT LIKE '%thank%'")
        # Workable submits that sat on the form (a YES/NO question the bot could not see, or 'Submitting…' held by a hidden
        # check) and got no confirmation email: almost certainly never sent. One more try; the inbox is checked first.
        cur3 = self.conn.execute(
            "UPDATE jobs SET status='queued', reason='recheck-inbox: ' || reason WHERE status='unconfirmed' AND attempts <= 1"
            " AND (apply_url LIKE '%workable.com%' OR source LIKE '%workab%') AND reason LIKE 'no confirmation after submit; page ends:%'")
        # jobs listed 'by hand' only because their whole site or employer was paused: the bot tries them itself now
        cur4 = self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=0 WHERE status='manual' AND (reason LIKE 'apply by hand: % stopped the bot at a human check on its last%'"
            " OR reason LIKE 'apply by hand: %application already stopped at a human check%')")
        cur5 = self.conn.execute("UPDATE jobs SET status='queued', attempts=0 WHERE status='blocked' AND reason LIKE '%Password must include%'")
        # parked only because their site was paused, or stopped by things the bot now handles (Workday accounts, start
        # pages, footer buttons): the bot does them itself. Real human checks (hCaptcha, emailed codes) stay as they are.
        cur6 = self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=0 WHERE status='blocked' AND (reason LIKE 'account:%' OR reason LIKE 'not a real application form%' OR reason LIKE 'no Next or Submit%'"
            " OR reason LIKE 'no application form found%')")
        self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('requeue', ?)", (version,))
        self.conn.commit()
        return cur.rowcount + cur2.rowcount + cur3.rowcount + cur4.rowcount + cur5.rowcount + cur6.rowcount

    def meta_get(self, k: str, default: str = "") -> str:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def meta_set(self, k: str, v: str):
        self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))
        self.conn.commit()

    def seen(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone() is not None

    def get(self, key: str):
        return self.conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()

    def add(self, job, status: str, **kw):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT OR IGNORE INTO jobs (key, source, company, title, location, url, apply_url, first_seen, status, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job.key, job.source, job.company, job.title, job.location, job.url, job.apply_url, now, status, now))
        self.update(job.key, status=status, **kw)

    def update(self, key: str, **kw):
        kw["updated"] = datetime.now().isoformat(timespec="seconds")
        cols = ", ".join(f"{k}=?" for k in kw)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE key=?", (*kw.values(), key))
        self.conn.commit()

    def applied_today(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('applied','unconfirmed') AND updated LIKE ?",
            (date.today().isoformat() + "%",)).fetchone()[0]

    def retryable(self, max_attempts: int):
        """Scored-high jobs that failed transiently and deserve another go."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','failed','dry_run') AND attempts < ? ORDER BY score DESC",
            (max_attempts,)).fetchall()

    def since(self, iso_ts: str):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE updated >= ? ORDER BY status, score DESC", (iso_ts,)).fetchall()
