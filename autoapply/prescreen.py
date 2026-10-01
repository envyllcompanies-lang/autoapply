"""LinkedIn-style match pre-screen: before applying, compare the actual posting with your résumé and rate the fit 0-100,
the way LinkedIn's 'top applicant' / match signal does, with a one-line reason.

For the best-scoring queued jobs that have not been screened yet, it:
  1. fetches the full posting text from the employer's own public feed (Workday, Greenhouse, Lever JSON) when the job
     list only had a title,
  2. re-runs the entry-level / pay check against that full text,
  3. asks the free writer for {"fit": 0-100, "why": "..."} using a short résumé digest (no personal details),
and stores the result in the 'fit' column. Jobs under search.min_fit are set aside as low matches; the rest are applied
to best match first. Every job is screened once.
"""
from __future__ import annotations

import html
import json
import re
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


def fetch_description(url: str) -> str:
    """The posting's full text from the employer's public JSON feed ('' when the site is not one of these)."""
    try:
        u = urlparse(url)
        host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
        if "myworkdayjobs.com" in host:
            tenant = host.split(".")[0]
            if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
                parts = parts[1:]                          # /en-US/<site>/job/...
            if len(parts) >= 3 and "job" in parts:
                site = parts[0]
                rest = "/".join(parts[parts.index("job"):])
                r = requests.get(f"https://{host}/wday/cxs/{tenant}/{site}/{rest}", headers=UA, timeout=20)
                if r.ok:
                    info = r.json().get("jobPostingInfo") or {}
                    return _text(info.get("jobDescription", ""))[:9000]
        elif "greenhouse.io" in host:
            m = re.search(r"/([\w-]+)/jobs/(\d+)", u.path)
            if m:
                r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}", headers=UA, timeout=20)
                if r.ok:
                    return _text(r.json().get("content", ""))[:9000]
        elif "lever.co" in host:
            if len(parts) >= 2:
                r = requests.get(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}", headers=UA, timeout=20)
                if r.ok:
                    d = r.json()
                    lists = " ".join(f"{x.get('text', '')}: {_text(x.get('content', ''))}" for x in d.get("lists") or [])
                    return (str(d.get("descriptionPlain") or "") + "\n" + lists + "\n" + str(d.get("additionalPlain") or ""))[:9000]
        # any other site: the posting page's own text (BambooHR, Breezy, Workable and most career pages render it server-side)
        r = requests.get(url, headers={**UA, "Accept": "text/html"}, timeout=20)
        if r.ok and "html" in r.headers.get("content-type", ""):
            txt = _text(r.text)
            if len(txt) > 600:
                return txt[:9000]
    except Exception:
        pass
    return ""


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
    return "\n".join(L)[:3500]


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
            f"{(job.description or '')[:3500]}")
    try:
        out = w._complete([{"role": "system", "content": PROMPT}, {"role": "user", "content": user}], 300, log, temperature=0.0)
    except Exception as e:
        log(f"      match check unavailable: {str(e)[:80]}")
        return None, ""
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
        fit = int(d.get("fit"))
        return max(0, min(100, fit)), str(d.get("why", ""))[:160]
    except Exception:
        n = re.search(r"\b(\d{1,3})\b", out or "")
        return (min(100, int(n.group(1))) if n else None), ""


def run(db, brain, by_key: dict, row_job, s: dict, level_out, log=print, site_rank=None) -> list[tuple]:
    """Screen up to search.prescreen_per_run unscreened queued jobs (best score first). Returns [(fit, why, row)] screened."""
    try:
        db.conn.execute("ALTER TABLE jobs ADD COLUMN fit INTEGER")
        db.conn.commit()
    except Exception:
        pass
    n = int(s.get("prescreen_per_run", 15))
    min_fit = int(s.get("min_fit", 55))
    db.conn.execute("UPDATE jobs SET status='low_score' WHERE status='queued' AND fit > 0 AND fit < ?", (min_fit,))   # cutoff raised
    db.conn.execute("UPDATE jobs SET status='queued' WHERE status='low_score' AND fit >= ? AND reason LIKE 'match %'",
                    (min_fit,))                                                                            # cutoff lowered
    db.conn.commit()
    rows = db.conn.execute("SELECT * FROM jobs WHERE status='queued' AND fit IS NULL ORDER BY score DESC LIMIT ?", (n * 4,)).fetchall()
    if site_rank:
        rows = sorted(rows, key=lambda r: (site_rank(r), -(r["score"] or 0)))      # forms the bot fills best first
    done, out = 0, []
    for row in rows:
        if done >= n:
            break
        job = by_key.get(row["key"]) or row_job(row)
        if job is None:
            continue
        if len(job.description or "") < 400:
            desc = fetch_description(job.apply_url or job.url)
            if desc:
                job.description = desc
        why_out = level_out(job, s)
        if why_out:
            db.update(row["key"], status="filtered", reason=why_out, fit=0)
            log(f"  match check: {job.title} @ {job.company}: {why_out[:90]}")
            continue
        if len(job.description or "") < 300:
            db.update(row["key"], status="low_score", fit=-1, reason="posting text could not be read for the match check")
            continue
        fit, why = screen(brain, job, log)
        done += 1
        if fit is None:
            break                                          # writer out of quota: try again next run
        if fit < min_fit:
            db.update(row["key"], status="low_score", fit=fit, reason=f"match {fit}%: {why}")
        else:
            db.update(row["key"], fit=fit, reason=f"match {fit}%: {why} | " + str(row["reason"] or "")[:200])
        out.append((fit, why, row))
        log(f"  match {fit:3d}%  {job.title} @ {job.company} — {why[:100]}")
    return out
