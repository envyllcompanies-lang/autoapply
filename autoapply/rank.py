"""Which waiting posting gets the next résumé-match check.

The match check (prescreen.gate) decides what is applied to, but the free writer only allows a few hundred checks a day while
thousands of postings pass the title and location filters. This module puts the waiting postings in order, so the checks go
to the ones most likely to pass. It learns from the bot's own history: which title words and which employers passed or failed
the match check before. That is blended with the keyword score and with how new the posting is. No network and no model: one
pass over the database at the start of a run, and it gets sharper every day as more postings are checked.

A title word the history has never seen counts as mildly bad news: the postings checked so far were the ones closest to
what you asked for, so a title made of words they never contained ('Junior Trader', 'Junior Tester') is further from it
than a familiar one, however well 'junior' has done.

Measured on the first 713 checked postings (5-fold, held out): the best-ranked 30% passed 56% of the time, against 47% when
ranked by keyword score alone and 38% for a random pick.
"""
from __future__ import annotations

import re
from datetime import datetime

W_COMPANY = 0.35        # how much an employer's own track record counts next to the title's
W_SCORE = 0.2           # how much the keyword score counts next to the learned odds
MIN_SEEN = 3            # a title word or employer needs this many checked postings before it counts
SMOOTH = 2.0            # pulls small samples toward the overall pass rate
K_BASE = 4.0
UNSEEN_W = 5.0          # weight of a title word the history has never seen...
UNSEEN_RATE = 0.5       # ...which counts as half the overall pass rate
LOW_SCORE = 30          # keyword scores under this were never match-checked before: a little is taken off per point below it
LOW_SCORE_STEP = 0.3
_FILLER = frozenset("of and the to in for an at on with or de la by as is ii iii iv - &".split())

_CO_NOISE = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|the|group|holdings)\b")


def norm_company(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return "".join(_CO_NOISE.sub(" ", n).split())


def title_tokens(title: str) -> set:
    """Words and word pairs of a job title ('operations analyst' counts as a thing of its own). Text in brackets is a
    location or a note, not part of the role."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())
    words = [w for w in re.findall(r"[a-z&]+", t) if len(w) > 1 or w == "&"]
    return set(words) | {" ".join(words[i:i + 2]) for i in range(len(words) - 1)}


class Prior:
    """Pass rates of the match check by title word and by employer, from postings that were already checked."""

    def __init__(self, checked, min_fit: int = 60):
        """checked: rows or dicts with 'title', 'company' and 'fit' (only fit > 0 counts)."""
        self.n_tok: dict = {}
        self.p_tok: dict = {}
        self.n_co: dict = {}
        self.p_co: dict = {}
        total = passed = 0
        for r in checked:
            try:
                fit = r["fit"] or 0
            except (KeyError, IndexError):
                fit = 0
            if fit <= 0:
                continue
            ok = 1 if fit >= min_fit else 0
            total += 1
            passed += ok
            for w in title_tokens(r["title"]):
                self.n_tok[w] = self.n_tok.get(w, 0) + 1
                self.p_tok[w] = self.p_tok.get(w, 0) + ok
            co = norm_company(r["company"])
            if co:
                self.n_co[co] = self.n_co.get(co, 0) + 1
                self.p_co[co] = self.p_co.get(co, 0) + ok
        self.total = total
        self.base = (passed / total) if total else 0.35

    def title(self, title: str) -> float:
        num, den = K_BASE * self.base, K_BASE
        for w in title_tokens(title):
            n = self.n_tok.get(w, 0)
            if n >= MIN_SEEN:
                rate = (self.p_tok.get(w, 0) + SMOOTH * self.base) / (n + SMOOTH)
                wt = min(n, 25) * (1.6 if " " in w else 1.0)
                num += wt * rate
                den += wt
            elif self.total and " " not in w and len(w) > 2 and w not in _FILLER:
                num += UNSEEN_W * UNSEEN_RATE * self.base          # a word no checked posting had: mildly bad news
                den += UNSEEN_W
        return num / den

    def company(self, company: str):
        co = norm_company(company)
        n = self.n_co.get(co, 0)
        if n < MIN_SEEN:
            return None
        return (self.p_co.get(co, 0) + SMOOTH * self.base) / (n + SMOOTH)

    def p(self, title: str, company: str = "") -> float:
        """Estimated chance (0-1) that this posting passes the match check."""
        t = self.title(title)
        c = self.company(company)
        return t if c is None else (1 - W_COMPANY) * t + W_COMPANY * c


def build(db, min_fit: int = 60) -> Prior:
    return Prior(db.conn.execute("SELECT title, company, fit FROM jobs WHERE fit > 0").fetchall(), min_fit)


def _age_days(row, now: datetime) -> float | None:
    for col in ("posted", "first_seen"):
        try:
            v = row[col]
        except (KeyError, IndexError):
            v = None
        if not v:
            continue
        try:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)
            return max(0.0, (now - t).total_seconds() / 86400)
        except Exception:
            continue
    return None


def freshness(row, now: datetime | None = None) -> float:
    """A few points for new postings and a few off for old ones: a new posting is more likely to be open still, and
    applying in its first days is when an application gets read."""
    age = _age_days(row, now or datetime.now())
    if age is None:
        return 0.0
    if age <= 2:
        return 6.0
    if age <= 7:
        return 3.0
    if age <= 14:
        return 0.0
    if age <= 30:
        return -4.0
    return -8.0


def value(prior: Prior, row, now: datetime | None = None) -> float:
    """0-100 for a posting that has not been match-checked yet: higher = check it sooner."""
    try:
        score = float(row["score"] or 0)
    except (KeyError, IndexError):
        score = 0.0
    p = prior.p(row["title"] or "", row["company"] or "")
    low = max(0.0, LOW_SCORE - score) * LOW_SCORE_STEP
    return max(0.0, min(100.0, 100 * ((1 - W_SCORE) * p + W_SCORE * 0.6 * min(score, 100) / 100) + freshness(row, now) - low))
