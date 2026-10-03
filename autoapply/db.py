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

# Statuses that end a job's life in the queue. 'unconfirmed' = Submit was clicked and no confirmation was seen: it is never
# submitted again; later runs only look in the inbox for the employer's confirmation email.
FINAL = ("applied", "filtered", "low_score", "blocked", "skipped", "unconfirmed")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        for col, kind in (("fit", "INTEGER"),          # match % of your résumé against the posting (prescreen.py)
                          ("stage", "TEXT"),           # how far the last attempt got: 'account', 'My Information', 'Review' ...
                          ("submitted_at", "TEXT"),    # when Submit was clicked; set BEFORE the click, never cleared by a retry rule
                          ("followup", "TEXT")):       # an email from the employer asking for something more
            try:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {kind}")
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
        # set aside only because their posting text could not be read ahead of time: it is now read from the page itself
        cur7 = self.conn.execute("UPDATE jobs SET status='queued', fit=NULL WHERE status='low_score' AND fit = -1")
        # Workday applications that got stuck part-way under the old page-by-page guessing (a date box, a pop-up list, a
        # block behind 'Add') or ran out of time. The Workday driver handles those pages now, and Submit was never clicked.
        cur8 = self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=0 WHERE status IN ('skipped','failed') AND apply_url LIKE '%myworkdayjobs.com%'"
            " AND (submitted_at IS NULL OR submitted_at = '') AND (status='failed' OR reason LIKE 'stuck on step%')")
        # Greenhouse's old driver stopped after the site emailed a security code. The new driver can read the
        # Greenhouse message, enter the code, and resubmit. Those old attempts explicitly said nothing was sent, so they
        # are safe to retry; reset attempts so the normal retry ceiling does not hide them.
        cur9 = self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=0 WHERE status='blocked' AND "
            "reason LIKE 'after Submit the site asked for a security code it emailed; the bot does not enter that one,%'")
        # a job whose Submit was clicked is never queued again by any of the rules above, except the inbox re-check
        self.conn.execute("UPDATE jobs SET status='unconfirmed' WHERE status='queued' AND submitted_at IS NOT NULL AND submitted_at != ''"
                          " AND reason NOT LIKE 'recheck-inbox%'")
        self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('requeue', ?)", (version,))
        self.conn.commit()
        return sum(c.rowcount for c in (cur, cur2, cur3, cur4, cur5, cur6, cur7, cur8))

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
        """Scored-high jobs that failed transiently and deserve another go. A job whose Submit button was ever clicked is
        never among them, whatever its status says: the only way back in is the inbox re-check of a submit that provably
        never reached the employer (reason 'recheck-inbox: ...')."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','failed','dry_run') AND attempts < ?"
            " AND (submitted_at IS NULL OR submitted_at = '' OR reason LIKE 'recheck-inbox%') ORDER BY score DESC",
            (max_attempts,)).fetchall()

    def mark_submit(self, key: str, attempts: int):
        """Write-ahead record of a Submit click: from here on a crash, a timeout or a cancelled run must never lead to a
        second submit of the same application."""
        self.update(key, status="unconfirmed", reason="submit clicked; outcome not yet known", attempts=attempts,
                    submitted_at=datetime.now().isoformat(timespec="seconds"))

    def since(self, iso_ts: str):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE updated >= ? ORDER BY status, score DESC", (iso_ts,)).fetchall()
