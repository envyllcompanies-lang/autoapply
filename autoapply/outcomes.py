"""What employers answer: interview invitations, assessments, offers and rejections, read from the applicant inbox.

Each answer is matched to the application it is about and kept on that job (columns outcome / outcome_at). New interview
invitations, offers and assessments go to the top of your email ('needs you'); rejections are only counted. The log and
the public reports get counts only: who wrote and what they said stay in your inbox and your email.

The 30-day scoreboard (applications, interviews, assessments, rejections, by place) shows which part of the search is
working, so the settings can follow what actually gets interviews.
"""
from __future__ import annotations

import html as _html
import re
import time
from datetime import datetime

KINDS = ("offer", "interview", "assessment", "rejection")

# ---------------------------------------------------------------------------------------------------- reading an email
OFFER_RX = re.compile(r"\boffer letter\b|pleased to (extend|offer you)|extend(ing)? (you )?an? (formal |verbal |written )?offer|"
                      r"offer of employment|\bjob offer\b|contingent offer|congratulations[^.]{0,80}\b(offer|join our team|welcome aboard)", re.I)
REJECT_RX = re.compile(
    r"\bunfortunately\b|regret to (inform|let you know)|not (be )?moving forward|will not be (moving|proceeding)|"
    r"decided (not )?to (move|proceed|go) (forward )?with (other|another|a different|candidates)|"
    r"(move|moving|proceed|proceeding|go|going) forward with (other|another|a different) (candidate|applicant)|"
    r"pursue other (candidates|applicants)|other candidates (whose|who)|no longer (being )?considered|not (been )?selected|"
    r"were not selected|(position|role|requisition|job|opening)[^.]{0,40}\b(has been|was|is now|have been) (filled|closed|cancell?ed)|"
    r"filled the (position|role)|"
    r"not a (match|fit) (for|at) this time|decided to go in a different direction|not able to offer you|unable to offer you|"
    r"will not be offering|not to (move|proceed) your (application|candidacy)", re.I)
ASSESS_RX = re.compile(
    r"\bassessment\b|hirevue|codility|hackerrank|criteria ?corp|criteria cognitive|\bccat\b|wonderlic|pymetrics|harver|\bshl\b|"
    r"talogy|testgorilla|vervoe|plum\.io|\bskills? test\b|online test|aptitude test|one[- ]way video|video interview|spark ?hire|"
    r"\bwillo\b|myinterview|vidcruiter|modern hire|knockri|record (your|a) (video|response)|"
    r"complete (the|your|an|a) (following |short |brief |online )?(assessment|evaluation|exercise|questionnaire|test)", re.I)
INTERVIEW_RX = re.compile(
    r"\binterview(s|ing)?\b|phone screen|screening call|intro(ductory)? call|(schedule|set up|book|arrange|coordinate|find) (a|an|your|some)? ?"
    r"(time|call|interview|phone screen|conversation|chat|meeting)|(like|love|want) to (invite|schedule|set up|speak|talk|chat|connect|meet)"
    r"( with)? you|invite you to|next (round|step in (the|our) (process|hiring))|your availability|availability (for|this|next|to)|"
    r"calendly\.com|goodtime\.io|cal\.com/|youcanbook\.me|calendar\.app\.google|/calendar/appointments|outlook\.office365\.com/(owa|book)|"
    r"(select|pick|choose|book) a (time|slot)", re.I)
# an interview phrase in a sentence like 'if selected, we will contact you to schedule an interview' is not an invitation
CONDITIONAL_RX = re.compile(r"\bif\b|should (you|your|we)|will (be )?(contact|reach|review|be in touch|let you know)|may (contact|reach)|"
                            r"in the event|once we|after (we|reviewing|review)|selected candidates|qualified candidates|candidates "
                            r"(who|whose|that)|if selected|tips|prepare for|webinar|workshop|how to|guide", re.I)
NOT_EMPLOYER_MAIL = re.compile(r"job alert|jobs? for you|recommended (jobs|for you)|newsletter|webinar|new jobs? (match|posted|near)|"
                               r"verify your|verification code|security code|password|sign[- ]in|one-time|unsubscribe from (job )?alerts", re.I)
JOB_CONTEXT = re.compile(r"application|apply|applied|candidacy|candidate|position|role|opening|opportunit|interview|hiring|recruit|"
                         r"talent|career|resume|résumé", re.I)
# the Gmail search that narrows a month of mail to the emails that could be one of these answers
GMAIL_QUERY = ('interview OR interviews OR unfortunately OR regret OR "moving forward" OR "move forward" OR "other candidates" OR '
               'assessment OR hirevue OR codility OR hackerrank OR "phone screen" OR availability OR schedule OR offer OR '
               '"next steps" OR selected OR filled')


def plain(body: str) -> str:
    """The readable text of an email body (HTML tags, styles and scripts removed)."""
    t = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", body or "")
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", t)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t\r\f\v]+", " ", t).strip()


def _strong_interview(text: str) -> bool:
    """An interview phrase that is not in a conditional or a tip ('if selected, we will schedule an interview')."""
    for m in INTERVIEW_RX.finditer(text):
        before = text[max(0, m.start() - 110): m.start()]
        sentence = re.split(r"[.!?\n]\s", before)[-1]
        if not CONDITIONAL_RX.search(sentence + " " + text[m.end(): m.end() + 30]):
            return True
    return False


def classify(subject: str, body: str) -> str | None:
    """'offer', 'interview', 'assessment', 'rejection', or None (a confirmation, an alert, anything else)."""
    subj = subject or ""
    text = plain(body)[:4000]
    if NOT_EMPLOYER_MAIL.search(subj):
        return None
    both = f"{subj}\n{text}"
    job = bool(JOB_CONTEXT.search(both))       # (the email must also name an employer you applied to: see track())
    if OFFER_RX.search(both) and not REJECT_RX.search(text):
        return "offer"
    if job and REJECT_RX.search(both):
        return "rejection"
    if ASSESS_RX.search(subj) or (ASSESS_RX.search(text) and re.search(r"\b(complete|take|invite|invitation|link|deadline|"
                                                                       r"within|by)\b", text, re.I)):
        return "assessment"
    if re.search(r"\binterview", subj, re.I) and not CONDITIONAL_RX.search(subj):
        return "interview"
    if job and _strong_interview(text):
        return "interview"
    return None


# ---------------------------------------------------------------------------------------------- matching and keeping
def _ensure_columns(db):
    for col in ("outcome", "outcome_at"):
        try:
            db.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
        except Exception:
            pass


def _best_application(cands: list, subject: str, text: str, ts: float):
    """Of your applications to this employer, the one this email is about: the role it names, else the latest before it."""
    before = [r for r in cands if str(r["t"] or "") <= datetime.fromtimestamp(ts).isoformat(timespec="seconds")] or cands
    for r in before:
        title = (r["title"] or "").lower()
        core = re.sub(r"\s*[-–(,|].*$", "", title).strip()
        if len(core) >= 6 and core in f"{subject}\n{text[:1500]}".lower():
            return r
    return max(before, key=lambda r: str(r["t"] or ""))


def track(db, company_name, log=print, notes: list | None = None, every_minutes: float = 60, first_days: int = 30,
          search=None, now: float | None = None) -> dict:
    """Read new employer answers and keep each on its application. company_name(row) gives the employer's display name;
    search(since_ts, gmail_query) returns parsed emails (mailbox.search by default). Returns {kind: count} of the new ones.
    notes gets one line for each new interview, offer or assessment (for the 'needs you' part of your email)."""
    from . import mailbox
    _ensure_columns(db)
    now = now or time.time()
    last = db.meta_get("outcomes_scanned_at", "")
    try:
        last_ts = datetime.fromisoformat(last).timestamp() if last else 0.0
    except ValueError:
        last_ts = 0.0
    if last_ts and now - last_ts < every_minutes * 60:
        return {}
    since = (last_ts - 3600) if last_ts else now - first_days * 86400
    cutoff = datetime.fromtimestamp(now - 120 * 86400).isoformat(timespec="seconds")
    rows = db.conn.execute(
        "SELECT key, company, title, location, status, outcome, outcome_at, followup, COALESCE(NULLIF(submitted_at, ''), updated) AS t "
        "FROM jobs WHERE status IN ('applied', 'unconfirmed', 'manual') AND COALESCE(NULLIF(submitted_at, ''), updated) >= ?",
        (cutoff,)).fetchall()
    if not rows:
        db.meta_set("outcomes_scanned_at", datetime.fromtimestamp(now).isoformat(timespec="seconds"))
        return {}
    try:
        msgs = (search or mailbox.search)(since, GMAIL_QUERY, ("INBOX", "[Gmail]/Spam"))
    except Exception as e:
        log(f"  employer replies: the inbox could not be read ({str(e)[:80]}); tried again next run")
        return {}
    by_co: dict = {}
    for r in rows:
        by_co.setdefault(company_name(r), []).append(r)
    # the longest word of each employer's name: a quick test before the careful one (names_employer)
    key_word = {name: max(re.findall(r"[a-z0-9]{3,}", name.lower()) or [""], key=len) for name in by_co if name}
    found: dict = {}
    for m in sorted(msgs, key=lambda m: m["ts"]):
        kind = classify(m.get("subject", ""), m.get("body", ""))
        if not kind:
            continue
        text = plain(m.get("body", ""))
        hay = f"{m.get('subject', '')} {m.get('from', '')} {(m.get('body') or '')[:1500]}".lower()
        cands = [r for name, rs in by_co.items() if key_word.get(name) and key_word[name] in hay
                 and mailbox.names_employer(name, m) for r in rs]
        if not cands:
            continue
        r = _best_application(cands, m.get("subject", ""), text, m["ts"])
        at = datetime.fromtimestamp(m["ts"]).isoformat(timespec="seconds")
        if str(r["outcome_at"] or "") >= at and (r["outcome"] or ""):
            continue                                    # this answer (or a later one) is already kept
        db.conn.execute("UPDATE jobs SET outcome=?, outcome_at=? WHERE key=?", (kind, at, r["key"]))
        found[kind] = found.get(kind, 0) + 1
        if kind == "assessment" and (r["followup"] or "") == (m.get("subject") or ""):
            continue                                    # already in your email as 'asks for something more'
        if notes is not None and kind != "rejection":
            what = {"interview": "INTERVIEW: they want to talk to you. Reply with your availability",
                    "offer": "OFFER: read it and decide",
                    "assessment": "ASSESSMENT: they sent a test or a recorded interview to complete"}[kind]
            when = datetime.fromtimestamp(m["ts"]).strftime("%b %d")
            notes.append(f"{what} — {r['title']} @ {company_name(r)} (email of {when}: \"{(m.get('subject') or '')[:90]}\")")
    db.conn.commit()
    db.meta_set("outcomes_scanned_at", datetime.fromtimestamp(now).isoformat(timespec="seconds"))
    if found:
        log("Employer replies: " + ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in found.items()) + " (details in your email)")
    return found


def scoreboard(db, days: int = 30, place_of=None) -> list[str]:
    """Applications and what came of them in the last `days`, overall and by place (Los Angeles, remote, elsewhere)."""
    _ensure_columns(db)
    cutoff = datetime.fromtimestamp(time.time() - days * 86400).isoformat(timespec="seconds")
    rows = db.conn.execute("SELECT location, outcome FROM jobs WHERE status IN ('applied', 'unconfirmed') AND "
                           "COALESCE(NULLIF(submitted_at, ''), updated) >= ?", (cutoff,)).fetchall()
    if not rows:
        return []
    groups: dict = {"all": [0, {}]}
    for r in rows:
        for g in ("all", place_of(r["location"] or "") if place_of else None):
            if not g:
                continue
            e = groups.setdefault(g, [0, {}])
            e[0] += 1
            if r["outcome"]:
                e[1][r["outcome"]] = e[1].get(r["outcome"], 0) + 1
    names = {"all": "all", "la": "Los Angeles", "remote": "remote", "other": "elsewhere"}
    out = []
    for g in ("all", "la", "remote", "other"):
        if g not in groups:
            continue
        n, o = groups[g]
        out.append(f"{names[g]}: {n} applications, {o.get('interview', 0)} interviews, {o.get('assessment', 0)} assessments, "
                   f"{o.get('offer', 0)} offers, {o.get('rejection', 0)} rejections")
    return out
