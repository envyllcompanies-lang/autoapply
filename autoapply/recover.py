"""Find applications that were submitted but never finished because the employer emailed a security code.

Greenhouse emails 'Security code for your application to <Company>' when Submit is clicked. If no application to that company
ever reached 'applied' here, the company's Greenhouse board is added to the boards the bot searches, so its jobs come back
through the normal path (which now types the emailed code and waits for the success page)."""
from __future__ import annotations

import re
import time
import urllib.request

from . import mailbox
from .aggregators import load_boards, remember_board

SUBJECT = re.compile(r"security code for your application (?:to|for|at)\s+(.+?)\s*$", re.I)
_NOISE = re.compile(r"\b(all jobs|jobs|careers|career|inc|llc|ltd|limited|usa|us|corp|corporation|co|company|the)\b\.?", re.I)


def companies_from_subjects(subjects) -> list[str]:
    out = []
    for s in subjects:
        m = SUBJECT.search(s or "")
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def slugs(name: str) -> list[str]:
    """Greenhouse board names a company is likely to use: 'The Trade Desk' -> thetradedesk, tradedesk, trade-desk."""
    clean = re.sub(r"[^a-z0-9 ]", " ", _NOISE.sub(" ", name.lower() + " ")).split()
    full = re.sub(r"[^a-z0-9]", "", name.lower())
    out = []
    for c in ("".join(clean), "".join(w for w in clean[:2]), "-".join(clean), clean[0] if clean else "", full):
        if c and c not in out:
            out.append(c)
    return out


def _board_exists(slug: str) -> bool:
    try:
        with urllib.request.urlopen(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def _done(db, name: str) -> bool:
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    for r in db.conn.execute("SELECT company FROM jobs WHERE status IN ('applied','unconfirmed')"):
        c = re.sub(r"[^a-z0-9]", "", (r[0] or "").lower())
        if c and key and (c in key or key in c):
            return True
    return False


def recover(db, base, log, days: int = 14, lister=None, exists=_board_exists) -> int:
    """Add the Greenhouse boards of companies that emailed a code for an application that never finished. Returns boards added."""
    last = db.meta_get("code_recovery", "")
    if last and time.time() - float(last) < 6 * 3600:
        return 0
    try:
        subjects = [m["subject"] for m in (lister or (lambda: mailbox._recent(time.time() - days * 86400, 250, ("INBOX", "[Gmail]/Spam"))))()]
    except Exception as e:
        log(f"Code recovery: inbox not read ({str(e)[:80]})")
        return 0
    db.meta_set("code_recovery", str(time.time()))
    have = {t.lower() for t in load_boards(base).get("greenhouse", [])}
    added = 0
    for name in companies_from_subjects(subjects):
        if _done(db, name):
            continue
        for slug in slugs(name):
            if slug in have:
                break
            if exists(slug):
                remember_board(base, f"https://boards.greenhouse.io/{slug}/jobs/1")
                log(f"Code recovery: '{name}' emailed a security code but was never finished: searching its board '{slug}'")
                added += 1
                break
    if added:
        db.meta_set("discovered_at", "")          # search right away
    return added
