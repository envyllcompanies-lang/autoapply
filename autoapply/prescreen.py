"""Look before applying: is the posting still up, and is it a match?

Two checks, both done right before a job would be applied to and neither needing a browser:

  preflight()  asks the employer's own public feed (Workday, Greenhouse, Lever, Ashby) whether the posting still exists
               and reads its full text. A posting the feed no longer lists is skipped without spending run time on it.
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


def gone(url: str) -> bool:
    """True when the posting's own address answers 404 or 410: the listing was taken down (aggregator links go stale)."""
    try:
        r = requests.get(url, headers=UA, timeout=12, allow_redirects=True, stream=True)
        code = r.status_code
        r.close()
        return code in (404, 410)
    except Exception:
        return False


_ASHBY: dict = {}       # employer -> {posting id: posting} for this run (Ashby's public feed lists a whole board at a time)


def _ashby_board(org: str):
    """Every posting of one employer on Ashby, by id (read once a run). None when the feed could not be read."""
    if org not in _ASHBY:
        got = None
        try:
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", headers=UA, timeout=15)
            if r.status_code in (404, 410):
                got = {}                                   # the employer's whole board is gone
            elif r.ok:
                got = {str(j.get("id")): j for j in (r.json().get("jobs") or []) if isinstance(j, dict)}
        except Exception:
            got = None
        _ASHBY[org] = got
    return _ASHBY[org]


def preflight(url: str, deep: bool = True) -> dict:
    """Before a browser is opened: is the posting still up, and what does it say? Read from the employer's own public feed
    (Workday, Greenhouse, Lever, Ashby), which answers in a fraction of a second. Returns {'closed': bool, 'description': str};
    'closed' is only True when the employer's feed itself says the job is gone."""
    out = {"closed": False, "description": "", "company_name": ""}
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
                    data = r.json()
                    info = data.get("jobPostingInfo") or {}
                    if info.get("canApply") is False or info.get("posted") is False:
                        out["closed"] = True
                    out["description"] = _text(info.get("jobDescription", ""))[:9000]
                    out["company_name"] = str((data.get("hiringOrganization") or {}).get("name") or "").strip()[:80]
            return out
        if "greenhouse.io" in host:
            m = re.search(r"/([\w-]+)/jobs/(\d+)", u.path)
            if m:
                r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}", headers=UA, timeout=15)
                if r.status_code in (404, 410):
                    out["closed"] = True
                elif r.ok:
                    data = r.json()
                    out["description"] = _text(data.get("content", ""))[:9000]
                    out["company_name"] = str(data.get("company_name") or "").strip()[:80]
            return out
        if "ashbyhq.com" in host:
            m = re.search(r"^/([\w.%-]+)/([0-9a-f-]{36})", u.path)
            if m:
                board = _ashby_board(m.group(1))
                if board is not None:                      # (None: the feed could not be read, so nothing is concluded)
                    j = board.get(m.group(2))
                    if j is None:
                        out["closed"] = True
                    else:
                        out["description"] = str(j.get("descriptionPlain") or _text(j.get("descriptionHtml", "")))[:9000]
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
    """The candidate's side of the match check: education, jobs, projects and skills from profile.yaml. No name, no
    contact details. About 3,400 characters: enough to judge by, and long enough that a provider which caches a repeated
    prompt opening does so (they need about a thousand tokens)."""
    def text(b):
        return b.get("text", "") if isinstance(b, dict) else str(b)
    L = []
    if profile.get("summary"):
        L.append(f"Summary: {' '.join(str(profile['summary']).split())[:300]}")
    for e in profile.get("education", []) or []:
        L.append(f"Education: {e.get('degree', '')}, {e.get('school', '')} ({e.get('date', '')})")
    for g in profile.get("skills", []) or []:
        L.append(f"Skills ({g.get('group', '')}): {', '.join(g.get('items', []))}")
    for n, r in enumerate(profile.get("experience", []) or []):
        bullets = " ".join(text(b) for b in (r.get("bullets") or []))[:600 if n == 0 else 400]
        L.append(f"Job: {r.get('title', '')} at {r.get('company', '')} [{r.get('dates', '')}]. {bullets}")
    for p in profile.get("projects", []) or []:            # (last: the cap, if it bites, takes project detail, not skills or jobs)
        what = " ".join(x for x in (str(p.get("tagline") or ""), text((p.get("bullets") or [""])[0])) if x)[:220]
        L.append(f"Project: {p.get('name') or p.get('title', '')}" + (f". {what}" if what else ""))
    return "\n".join(L)[:3400]


PREFER = ("groq-qwen", "groq-20b")        # the smaller free models do the match check; the best one is kept for answers

PROMPT = ("You screen job postings for one candidate, the way LinkedIn's job-match feature does. Using the candidate's background "
          "and each posting, rate how well the candidate fits on a 0-100 scale: 85+ strong (meets the required qualifications, "
          "relevant experience), 65-84 good (meets most, close stretch), 45-64 weak (several required things missing), under 45 "
          "poor (needs experience, licenses or skills the candidate clearly lacks). Also score UNDER 60 when the job is not a "
          "salaried professional office role a new business graduate would want: retail store, restaurant, hospitality, "
          "warehouse floor, call center, commission sales, hands-on construction or trades labor, hands-on maintenance or "
          "janitorial work, freelance or gig work, or pay stated under $60,000 a year. Office roles in construction, real "
          "estate, property and facilities (project coordination, project engineering, operations, administration, finance) "
          "are wanted and are judged like any other office role. Judge only what is written. The message holds one or more "
          "postings, each under a line 'POSTING <number>'. Rate each posting on its own, against the candidate only, never "
          "against the other postings. Reply with JSON only, a list with one object per posting in the same order: "
          '[{"n": <posting number>, "fit": <integer>, "why": "<one short sentence naming the main match or gap>"}]')

BATCH = 3            # postings rated in one call. The candidate's side of the prompt is half of a single check's cost; in a call
                     # for three postings it is paid for once, which stretches the day's free allowance by about half again.
LONG = 3200          # postings up to this length are sent whole; longer ones are cut to the parts that say what the job is
_KEEP_HDR = re.compile(r"responsibilit|what you.ll (do|be doing)|what you will do|the role|about (the|this) (role|job|position|opportunity)|"
                       r"role overview|position (summary|overview|description)|job (summary|description|duties|purpose)|duties|overview|"
                       r"requirement|qualification|what you.ll need|what you need|what we.re looking for|who you are|about you|you have|"
                       r"must.haves?|skills|experience|education|what you.ll bring|what you bring|ideal candidate|minimum|basic|preferred|"
                       r"in this role|your (impact|role|day)|a day in|the (job|position|opportunity)|what you.ll (own|work on|accomplish)|"
                       r"key (duties|accountabilities)|essential (functions|duties)|you are|we.re looking for|nice to have|bonus points", re.I)
_DROP_HDR = re.compile(r"^about\b|who we are|our (mission|story|values|culture|company|team|commitment)|benefits|perks|what we offer|"
                       r"why (join|work|you.ll love)|equal (opportunity|employment)|\beeo\b|diversity|accommodation|privacy|notice|"
                       r"how to apply|application process|disclaimer|e-verify|working conditions|additional information", re.I)


def _is_header(line: str) -> bool:
    """A short line that titles a section of a posting ('Responsibilities', 'What you'll bring:', 'ABOUT US')."""
    if not line or len(line) > 70 or len(line.split()) > 9:
        return False
    if line.endswith((".", ",", ";")) or re.match(r"^[\-•*·\d]", line):
        return False
    return line.endswith(":") or line.isupper() or line.istitle() or bool(_KEEP_HDR.search(line) or _DROP_HDR.search(line)) and len(line.split()) <= 6


def excerpt(desc: str, limit: int = 3000) -> str:
    """The part of a posting the match check reads. A short posting is read whole. A long one used to be cut after its
    first 3,200 characters, which is often the company's introduction and not the requirements: now its opening lines, its
    'responsibilities' and 'requirements' sections and then whatever else fits are kept, and 'about us', benefits and legal
    sections are left out."""
    text = (desc or "").strip()
    if len(text) <= max(limit, LONG if limit >= 3000 else limit):
        return text
    sections = [["intro", "", []]]
    for ln in (x.strip() for x in text.split("\n")):
        if not ln:
            continue
        if _is_header(ln):
            kind = "keep" if _KEEP_HDR.search(ln) else "drop" if _DROP_HDR.search(ln) else "other"
            sections.append([kind, ln, []])
        else:
            sections[-1][2].append(ln)
    keep = [x for x in sections[1:] if x[0] == "keep" and x[2]]
    if not keep:
        # no section titles to go by: the opening, and the stretch around the first talk of requirements
        m = re.search(r"qualif|requirement|experience|you have|must have|\byears\b", text[900:], re.I)
        if not m:
            return text[:limit]
        a = 900 + max(0, m.start() - 200)
        return (text[:900] + "\n…\n" + text[a:a + limit - 905])[:limit]
    parts = [" ".join(sections[0][2])[:500]] if sections[0][2] else []
    left = limit - sum(len(x) for x in parts)
    for n, (_k, head, body) in enumerate(keep):
        room = max(300, left // (len(keep) - n))
        chunk = (head + "\n" + "\n".join(body))[:room]
        parts.append(chunk)
        left -= len(chunk) + 1
        if left <= 0:
            break
    for kind, head, body in sections[1:]:
        if left < 200:
            break
        if kind == "other" and body:
            chunk = (head + "\n" + "\n".join(body))[:min(400, left)]
            parts.append(chunk)
            left -= len(chunk) + 1
    out = "\n".join(x for x in parts if x)[:limit]
    kept = sum(len("\n".join(body)) for _k, _head, body in keep)
    # (a short result is fine when it holds real duties / requirements; with next to nothing recognised, the opening as before)
    return out if (len(out) >= 1200 or kept >= 400) else text[:limit]


def _takes(fn, name: str) -> bool:
    """Does this function take an argument of this name? (A stand-in writer in a test may not know 'match'.)"""
    try:
        import inspect
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def match_ready(brain) -> bool:
    """Is there free allowance for a match check right now?"""
    w = getattr(brain, "writer", None)
    if w is None:
        return False
    return bool(w.ready(match=True) if _takes(w.ready, "match") else w.ready())


def _system(profile: dict) -> str:
    """The fixed opening of every match check: the rules and the candidate's side. It is the same in every call, and goes
    first, so a provider that caches repeated prompt openings (Groq does) counts it once."""
    return (f"{PROMPT}\n\nCANDIDATE\n{_digest(profile)}\nTarget: entry-level operations, business operations, "
            f"process improvement, project coordination, analyst, supply chain and construction/real-estate project or finance "
            f"roles; 0-3 years of experience.")


def parse_fits(out: str, n: int) -> list[tuple[int | None, str]]:
    """The model's reply for n postings as [(fit, why)], in the order asked. A posting it did not rate (or a reply that
    cannot be told apart) gives (None, ''): that posting is simply checked again later, never guessed."""
    res: list = [(None, "")] * n
    txt = out or ""
    objs: list = []
    m = re.search(r"\[.*\]", txt, re.S)
    if m:
        try:
            objs = [o for o in json.loads(m.group(0)) if isinstance(o, dict)]
        except Exception:
            objs = []
    if not objs:
        for mm in re.finditer(r"\{[^{}]*\}", txt, re.S):
            try:
                o = json.loads(mm.group(0))
            except Exception:
                continue
            if isinstance(o, dict):
                objs.append(o)
    if objs:
        numbered = all(str(o.get("n", "")).strip().isdigit() for o in objs) and len({str(o.get("n")).strip() for o in objs}) == len(objs)
        if not numbered and len(objs) != n:
            return res                                     # which rating belongs to which posting cannot be told
        for i, o in enumerate(objs):
            k = int(str(o["n"]).strip()) - 1 if numbered else i
            if 0 <= k < n:
                try:
                    res[k] = (max(0, min(100, int(float(o.get("fit"))))), str(o.get("why", ""))[:160])
                except (TypeError, ValueError):
                    pass
        return res
    if n == 1:
        num = re.search(r"\b(\d{1,3})\b", txt)
        if num:
            res[0] = (min(100, int(num.group(1))), "")
    return res


def screen_many(brain, jobs: list, log=print) -> list[tuple[int | None, str]]:
    """Rate several postings in one call (see BATCH). Returns [(fit, why)] in the same order; (None, '') for a posting the
    model did not rate or when the writer could not be reached."""
    w = getattr(brain, "writer", None)
    if w is None or not jobs:
        return [(None, "")] * len(jobs)
    limit = 3000 if len(jobs) == 1 else 2300               # a little less of each posting when several share one call
    user = "\n\n".join(f"POSTING {i}\n{j.title} at {j.company} ({j.location})\n{excerpt(j.description, limit)}" for i, j in enumerate(jobs, 1))
    from . import writer as _w
    old_deadline = _w.DEADLINE[0]
    _w.DEADLINE[0] = time.time() + 75                      # wait out a per-minute limit, never a long one
    try:
        extra = {"match": True} if _takes(w._complete, "match") else {}
        out = w._complete([{"role": "system", "content": _system(brain.profile)}, {"role": "user", "content": user}],
                          200 + 200 * len(jobs), log, temperature=0.0, prefer=PREFER, **extra)
    except Exception as e:
        log(f"      match check unavailable: {str(e)[:80]}")
        return [(None, "")] * len(jobs)
    finally:
        _w.DEADLINE[0] = old_deadline
    return parse_fits(out, len(jobs))


def screen(brain, job: Job, log=print) -> tuple[int | None, str]:
    return screen_many(brain, [job], log)[0]


def same_role_fit(db, job: Job) -> tuple[int, str] | None:
    """The match already worked out for the same title at the same employer (the same job posted in another city, or
    posted again): it is used as is instead of spending another check on it. Only for titles of two words or more:
    a bare 'Analyst' at a big employer is many different jobs."""
    title = " ".join((job.title or "").split())
    if len(title.split()) < 2:
        return None
    try:
        r = db.conn.execute("SELECT fit, reason FROM jobs WHERE fit > 0 AND key != ? AND lower(company) = lower(?)"
                            " AND lower(trim(title)) = lower(?) ORDER BY updated DESC LIMIT 1", (job.key, job.company, title)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    why = re.sub(r"^match \d+%:\s*", "", str(r["reason"] or "")).split(" | ")[0][:110]
    return int(r["fit"]), ("same role at this employer, checked before: " + why).strip(": ")


def _fallback(row, s: dict) -> str:
    """What happens to a job that cannot be match-checked right now: 'go' when its keyword score is high enough to do
    without (search.min_score_without_match), else 'later' (it stays queued)."""
    score = (row["score"] or 0) if row is not None else 0
    return "go" if score >= int(s.get("min_score_without_match", 80)) else "later"


def gate_pre(db, brain, job: Job, row, s: dict, have_page: bool = False) -> tuple[str, int | None, str]:
    """Everything about the match of one job that needs no model: a result stored earlier, the same role checked before,
    a posting whose text is not known yet, no allowance left. Returns a verdict as gate() does, or ('check', None, '') when
    the model has to be asked now (alone with screen(), or together with other postings with screen_many())."""
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
    got = same_role_fit(db, job)
    if got:
        return gate_store(db, job, row, s, got[0], got[1])
    if len(job.description or "") < 300:
        return ("page" if not have_page else _fallback(row, s)), None, ""
    if not match_ready(brain):
        return _fallback(row, s), None, ""
    return "check", None, ""


def gate_store(db, job: Job, row, s: dict, fit: int | None, why: str) -> tuple[str, int | None, str]:
    """Record the match worked out for one job and say what follows: 'go', 'low' (set aside), or, when no match could be
    worked out (fit None), the fallback of a job that cannot be checked now."""
    if fit is None:
        return _fallback(row, s), None, ""
    min_fit = int(s.get("min_fit", 70))
    old = str((row["reason"] if row is not None else "") or "")
    old = re.sub(r"^match \d+%:.*? \| ", "", old)
    if fit < min_fit:
        db.update(job.key, status="low_score", fit=fit, reason=f"match {fit}%: {why}")
        return "low", fit, why
    db.update(job.key, fit=fit, reason=f"match {fit}%: {why} | {old[:200]}")
    return "go", fit, why


def gate(db, brain, job: Job, row, s: dict, log=print, have_page: bool = False) -> tuple[str, int | None, str]:
    """The match check for one job, right before it would be applied to. Returns (verdict, fit, why):
      'go'    apply (the match is at or above search.min_fit, or the check is switched off)
      'low'   set aside: the match is below the bar (the job is marked low_score)
      'page'  the posting text is not known yet: open the page, then ask again with have_page=True
      'later' it cannot be judged now (the free writer is out of allowance): the job stays queued for another run,
              unless its keyword score is high enough to go without a match check (search.min_score_without_match)."""
    v = gate_pre(db, brain, job, row, s, have_page)
    if v[0] != "check":
        return v
    fit, why = screen(brain, job, log)
    return gate_store(db, job, row, s, fit, why)
