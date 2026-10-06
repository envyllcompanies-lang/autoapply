"""Find applications that were submitted but never finished because the employer emailed a security code.

Greenhouse emails 'Security code for your application to <Company>' when Submit is clicked. If no application to that company
ever reached 'applied' here, the company's Greenhouse board is added to the boards the bot searches, so its jobs come back
through the normal path (which now types the emailed code and waits for the success page)."""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from . import __version__, mailbox
from .aggregators import load_boards, remember_board

SUBJECT = re.compile(r"security code for your application (?:to|for|at)\s+(.+?)\s*$", re.I)
_NOISE = re.compile(r"\b(all jobs|jobs|careers|career|inc|llc|ltd|limited|usa|us|corp|corporation|co|company|the)\b\.?", re.I)


def companies_from_subjects(subjects) -> list[str]:
    """Each company named in a 'Security code for your application to <Company>' subject, once, in order."""
    found = (SUBJECT.search(s or "") for s in subjects)
    return list(dict.fromkeys(m.group(1) for m in found if m))


def _flat(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slugs(name: str) -> list[str]:
    """Greenhouse board names a company is likely to use: 'The Trade Desk' -> tradedesk, trade-desk, trade, thetradedesk."""
    words = re.sub(r"[^a-z0-9 ]", " ", _NOISE.sub(" ", name.lower())).split()      # its words, without 'Inc', 'Careers', 'The'…
    candidates = ("".join(words), "".join(words[:2]), "-".join(words), words[0] if words else "", _flat(name))
    return [c for c in dict.fromkeys(candidates) if c]


def _board_name(slug: str) -> str | None:
    """The company name a Greenhouse board shows (the same name its code emails use), or None if there is no such board."""
    try:
        with urllib.request.urlopen(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("name") or ""
    except Exception:
        return None


def _same(a: str | None, b: str | None) -> bool:
    """Names of the same company: equal, or one inside the other when both have 5+ letters ('Ramp' is not 'Trampoline').
    A missing name never matches."""
    a, b = _flat(a), _flat(b)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 5 and (a in b or b in a))


def _words(x: str | None) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", _NOISE.sub(" ", (x or "").lower())).split()


def _board_is(shown: str | None, name: str) -> bool:
    """The board belongs to the company the email names: the same name, or the board shows the start of it ('Prolific' for
    'Prolific Academic Ltd', checked 2026-10-06) when that start is a name of 6+ letters. A board called 'Trade', 'New' or
    'Star' is another company, never 'The Trade Desk', 'The New York Times' or 'Co–Star'."""
    if shown is None or not _flat(shown):
        return False
    if _flat(shown) == _flat(name):
        return True
    b, n = _words(shown), _words(name)
    return bool(b) and n[:len(b)] == b and len("".join(b)) >= 6


def _forget(db, base, slug: str) -> None:
    """Take a board that is not the company's out of the searched boards, with its jobs still waiting in the queue."""
    data = load_boards(base)
    if slug in data.get("greenhouse", []):
        data["greenhouse"].remove(slug)
        (Path(base) / "discovered_boards.json").write_text(json.dumps(data, indent=1))
    db.conn.execute("UPDATE jobs SET status='filtered', reason='board added by mistake: not the company that emailed the code'"
                    " WHERE company=? AND status='queued' AND (submitted_at IS NULL OR submitted_at='')", (slug,))
    db.conn.commit()


def _done(db, name: str) -> bool:
    """An application to this company was finished here (applied, or submitted and waiting for its confirmation email)."""
    return any(_same(r[0], name) for r in db.conn.execute("SELECT company FROM jobs WHERE status IN ('applied','unconfirmed')"))


def recover(db, base, log, days: int = 14, lister=None, board_name=_board_name, known=(), budget_s: float = 90) -> int:
    """Add the Greenhouse boards of companies that emailed a code for an application that never finished. Returns boards added.
    known: Greenhouse boards the bot already searches (config and boards.yaml). Looks up boards for at most budget_s seconds."""
    ts, _, ver = db.meta_get("code_recovery", "").partition("|")
    try:
        if ver == __version__ and time.time() - float(ts or 0) < 6 * 3600:
            return 0                           # looked within 6 hours with this same build (a new build looks again at once)
    except ValueError:
        pass                                   # an unreadable mark: look again
    try:
        subjects = lister() if lister else mailbox.recent_subjects(time.time() - days * 86400)
    except Exception as e:
        log(f"Code recovery: inbox not read ({str(e)[:80]})")
        return 0
    db.meta_set("code_recovery", f"{time.time()}|{__version__}")
    found = {str(t).lower() for t in load_boards(base).get("greenhouse", [])}
    fixed = {str(t).lower() for t in known}                # boards from config / boards.yaml: never removed here
    names: dict = {}

    def shown(slug):
        if slug not in names:
            names[slug] = board_name(slug)
        return names[slug]

    added, stop_at = 0, time.time() + budget_s
    for name in companies_from_subjects(subjects):
        if time.time() > stop_at:
            log("Code recovery: time is up for looking up boards; the rest is looked at in a later run")
            db.meta_set("code_recovery", "")
            break
        for slug in slugs(name):                           # a board an earlier build guessed wrongly for this company: undone
            if slug in found and slug not in fixed and not _board_is(shown(slug), name):
                _forget(db, base, slug)
                found.discard(slug)
                log(f"Code recovery: board '{slug}' ({(shown(slug) or 'gone').strip()}) is not '{name}': no longer searched")
        if _done(db, name):
            continue
        for slug in slugs(name):
            if not _board_is(shown(slug), name):           # no such board, or another company that happens to use that name
                continue
            if slug not in found | fixed:                  # already searched: its unfinished jobs come back through the retry rules
                remember_board(base, f"https://boards.greenhouse.io/{slug}/jobs/1")
                found.add(slug)
                log(f"Code recovery: '{name}' emailed a security code but was never finished: searching its board '{slug}'")
                added += 1
            break
    if added:
        db.meta_set("discovered_at", "")          # search right away
    return added
