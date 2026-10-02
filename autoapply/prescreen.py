"""Look before applying: is the posting still up, and is it a match?

Two checks, both done right before a job would be applied to and neither needing a browser:

  preflight()  asks the employer's own public feed (Workday, Greenhouse, Lever) whether the posting still exists and
               reads its full text. A posting the feed no longer lists is skipped without spending run time on it.
  gate()       compares that full text with your résumé, the way LinkedIn's match signal does, and gives a 0-100 fit with
               a one-line reason. Jobs under search.min_fit are set aside; the rest are applied to. The result is stored
               in the 'fit' column, so every job is only ever checked once.

The match check uses the free writer's smaller models and a short résumé digest (no personal details), so the best
model's daily allowance is left for the application answers themselves.
"""
from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import urlparse

import requests

from .sources import Job

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
      "Accept": "application/json"}


def _text(h: str) -> str:
    h = re.sub(r"<(script|style)\b.*?</\1>", " ", h or "", flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"[ \t]+", " ", html.unescape(h)).strip()


def preflight(url: str, deep: bool = True) -> dict:
    """Before a browser is opened: is the posting still up, and what does it say? Read from the employer's own public feed
    (Workday, Greenhouse, Lever), which answers in a fraction of a second. Returns {'closed': bool, 'description': str};
    'closed' is only True when the employer's feed itself says the job is gone."""
    out = {"closed": False, "description": ""}
    try:
        u = urlparse(url)
        host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
        if "myworkdayjobs.com" in host:
            tenant = host.split(".")[0]
            if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
                parts = parts[1:]                          # /en-US/<site>/job/...
            if "apply" in parts:
                parts = parts[:parts.index("apply")]
            if len(parts) >= 3 and "job" in parts:
                site = parts[0]
                rest = "/".join(parts[parts.index("job"):])
                r = requests.get(f"https://{host}/wday/cxs/{tenant}/{site}/{rest}", headers=UA, timeout=15)
                if r.status_code in (404, 410):
                    out["closed"] = True
                elif r.ok:
                    info = r.json().get("jobPostingInfo") or {}
                    if info.get("canApply") is False or info.get("posted") is False:
                        out["closed"] = True
                    out["description"] = _text(info.get("jobDescription", ""))[:9000]
            return out
        if "greenhouse.io" in host:
            m = re.search(r"/([\w-]+)/jobs/(\d+)", u.path)
            if m:
                r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}", headers=UA, timeout=15)
                if r.status_code in (404, 410):
                    out["closed"] = True
                elif r.ok:
                    out["description"] = _text(r.json().get("content", ""))[:9000]
            return out
        if "lever.co" in host:
            if len(parts) >= 2:
                r = requests.get(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}", headers=UA, timeout=15)
                if r.status_code in (404, 410):
                    out["closed"] = True
                elif r.ok:
                    d = r.json()
                    lists = " ".join(f"{x.get('text', '')}: {_text(x.get('content', ''))}" for x in d.get("lists") or [])
                    out["description"] = (str(d.get("descriptionPlain") or "") + "\n" + lists + "\n" + str(d.get("additionalPlain") or ""))[:9000]
            return out
        if not deep:
            return out
        # any other site: the posting page's own text (BambooHR, Breezy, Workable and most career pages render it server-side)
        r = requests.get(url, headers={**UA, "Accept": "text/html"}, timeout=15)
        if r.ok and "html" in r.headers.get("content-type", ""):
            txt = _text(r.text)
            if len(txt) > 600:
                out["description"] = txt[:9000]
    except Exception:
        pass
    return out


def fetch_description(url: str) -> str:
    """The posting's full text from the employer's public feed or page ('' when it cannot be read)."""
    return preflight(url)["description"]


def _digest(profile: dict) -> str:
    L = []
    for e in profile.get("education", []) or []:
        L.append(f"Education: {e.get('degree', '')}, {e.get('school', '')} ({e.get('date', '')})")
    for r in profile.get("experience", []) or []:
        bullets = " ".join((b.get("text", "") if isinstance(b, dict) else str(b)) for b in (r.get("bullets") or []))[:400]
        L.append(f"Job: {r.get('title', '')} at {r.get('company', '')} [{r.get('dates', '')}]. {bullets}")
    for p in profile.get("projects", []) or []:
        L.append(f"Project: {p.get('name') or p.get('title', '')}")
    for g in profile.get("skills", []) or []:
        L.append(f"Skills ({g.get('group', '')}): {', '.join(g.get('items', []))}")
    return "\n".join(L)[:2600]


PREFER = ("groq-qwen", "groq-20b")        # the smaller free models do the match check; the best one is kept for answers

PROMPT = ("You screen job postings for one candidate, the way LinkedIn's job-match feature does. Using the candidate's background "
          "and the posting, rate how well the candidate fits on a 0-100 scale: 85+ strong (meets the required qualifications, "
          "relevant experience), 65-84 good (meets most, close stretch), 45-64 weak (several required things missing), under 45 "
          "poor (needs experience, licenses or skills the candidate clearly lacks). Also score UNDER 60 when the job is not a "
          "salaried professional office role a new business graduate would want: retail store, restaurant, hospitality, "
          "warehouse floor, call center, commission sales, construction/trades, facilities maintenance, freelance or gig work, "
          "or pay stated under $60,000 a year. Judge only what is written. Reply with JSON "
          'only: {"fit": <integer>, "why": "<one short sentence naming the main match or gap>"}')


def screen(brain, job: Job, log=print) -> tuple[int | None, str]:
    w = getattr(brain, "writer", None)
    if w is None:
        return None, ""
    user = (f"CANDIDATE\n{_digest(brain.profile)}\nTarget: entry-level operations, coordination, analyst, supply chain, "
            f"project roles; 0-3 years of experience.\n\nPOSTING\n{job.title} at {job.company} ({job.location})\n"
            f"{(job.description or '')[:3200]}")
    from . import writer as _w
    old_deadline = _w.DEADLINE[0]
    _w.DEADLINE[0] = time.time() + 50                      # never sit out a long rate limit for a match check
    try:
        out = w._complete([{"role": "system", "content": PROMPT}, {"role": "user", "content": user}], 300, log, temperature=0.0,
                          prefer=PREFER)
    except Exception as e:
        log(f"      match check unavailable: {str(e)[:80]}")
        return None, ""
    finally:
        _w.DEADLINE[0] = old_deadline
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
        fit = int(d.get("fit"))
        return max(0, min(100, fit)), str(d.get("why", ""))[:160]
    except Exception:
        n = re.search(r"\b(\d{1,3})\b", out or "")
        return (min(100, int(n.group(1))) if n else None), ""


def gate(db, brain, job: Job, row, s: dict, log=print, have_page: bool = False) -> tuple[str, int | None, str]:
    """The match check for one job, right before it would be applied to. Returns (verdict, fit, why):
      'go'    apply (the match is at or above search.min_fit, or the check is switched off)
      'low'   set aside: the match is below the bar (the job is marked low_score)
      'page'  the posting text is not known yet: open the page, then ask again with have_page=True
      'later' it cannot be judged now (the free writer is out of allowance): the job stays queued for another run,
              unless its keyword score is high enough to go without a match check (search.min_score_without_match)."""
    if not s.get("prescreen", True):
        return "go", None, ""
    min_fit = int(s.get("min_fit", 70))
    try:
        fit = row["fit"]
    except (IndexError, KeyError):
        fit = None
    if fit and fit > 0:                                    # checked on an earlier run: the stored result stands
        if fit >= min_fit:
            return "go", fit, ""
        try:
            waiting = row["status"] != "low_score"         # e.g. waiting for a retry, with a match under today's bar
        except (IndexError, KeyError):
            waiting = False
        if waiting:
            old = re.sub(r"^match \d+%:.*? \| ", "", str(row["reason"] or ""))
            db.update(job.key, status="low_score", reason=f"match {fit}%: under the {min_fit}% bar | {old[:200]}")
        return "low", fit, ""
    score = (row["score"] or 0) if row is not None else 0
    fallback = "go" if score >= int(s.get("min_score_without_match", 80)) else "later"
    if len(job.description or "") < 300:
        return ("page" if not have_page else fallback), None, ""
    w = getattr(brain, "writer", None)
    if w is None or not w.ready():
        return fallback, None, ""
    fit, why = screen(brain, job, log)
    if fit is None:
        return fallback, None, ""
    old = str((row["reason"] if row is not None else "") or "")
    old = re.sub(r"^match \d+%:.*? \| ", "", old)
    if fit < min_fit:
        db.update(job.key, status="low_score", fit=fit, reason=f"match {fit}%: {why}")
        return "low", fit, why
    db.update(job.key, fit=fit, reason=f"match {fit}%: {why} | {old[:200]}")
    return "go", fit, why
