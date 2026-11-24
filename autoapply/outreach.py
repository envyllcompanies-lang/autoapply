"""Cold outreach to Los Angeles employers: a short, personal email with your résumé to companies hiring in the LA area.

Who gets one: employers with a Los Angeles-area posting in your field in the last 45 days, found by the bot's own job
searches. First those you applied to (the email follows up on that application), then those whose site stopped the bot
at a human check (the email is a way in that does not need one), then the others hiring for roles like yours.
Government employers, staffing marketplaces and employers that already turned you down are left out. One company, one
email (and one follow-up), ever.

How the address is found: the company's own website (linked from its job posting, or its own careers site), then a few
of its pages (home, contact, careers, about). Only role addresses the company publishes there are used: careers@, jobs@,
recruiting@, talent@, hr@, people@, hiring@ first, then info@ / hello@ / contact@. Never a guessed address, never a
person's address, never an address the site hides from address collectors, and a site's robots.txt is respected.

Sending: nothing is sent until outreach.send is true in settings.yaml. Until then the day's drafts are in your email, so
you can read exactly what would go out. Once on: at most outreach.per_day a day (15), on weekdays in Los Angeles business
hours, a few per run, one follow-up after outreach.followup_days (7) of silence, and never again after a reply, a bounce
or a 'no thanks'. Replies go to the top of your email. The log and the public database get company names and counts only.
"""
from __future__ import annotations

import html as _html
import os
import random
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib import robotparser
from urllib.parse import quote, urljoin, urlparse

import requests

from .sources import la_area

UA = {"User-Agent": "Mozilla/5.0 (compatible; personal job-search assistant)", "Accept": "text/html,application/xhtml+xml"}
TIMEOUT = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
    domain TEXT PRIMARY KEY,       -- one company per website ('pending:<employer>' until the website is known)
    co TEXT,                       -- the employer's key (its name squashed), so it is never picked twice
    company TEXT, email TEXT,
    kind TEXT,                     -- applied | hand | hiring
    role TEXT, job_key TEXT, city TEXT,
    status TEXT,                   -- new | no_site | no_email | ready | sent | followed_up | replied | bounced | opted_out | skipped
    reason TEXT,
    found_at TEXT, sent_at TEXT, followup_at TEXT, replied_at TEXT,
    msg_id TEXT, subject TEXT,
    tries INTEGER DEFAULT 0,
    updated TEXT
);
"""
DONE = ("sent", "followed_up", "replied", "bounced", "opted_out")

# hosts that are job systems, job boards, social sites or page furniture: never the employer's own website
NOT_EMPLOYER_HOST = re.compile(
    r"greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkdaysite\.com|workday\.com|bamboohr\.com|smartrecruiters\.com|"
    r"workable\.com|icims\.com|jobvite\.com|recruitee\.com|breezy\.hr|paylocity\.com|paycomonline|ultipro|ukg\.|adp\.com|taleo|"
    r"successfactors|oraclecloud|jazzhr|applytojob|rippling|dover\.com|wellfound|linkedin|indeed|glassdoor|ziprecruiter|builtin|"
    r"themuse|simplify|jobgether|careerjet|jooble|adzuna|google\.|goo\.gl|facebook|fb\.com|twitter|x\.com|instagram|youtube|youtu\.be|"
    r"tiktok|threads\.net|reddit|medium\.com|github\.com|apple\.com|microsoft\.com|w3\.org|schema\.org|cloudflare|gstatic|"
    r"googleapis|jsdelivr|unpkg|cdn|onetrust|cookiebot|cookielaw|hubspot|hsforms|typeform|calendly|vimeo|wix\.com|squarespace\.com|"
    r"wordpress\.(com|org)|shopify\.com|\.gov$|\.mil$|e-verify|bit\.ly|lnkd\.in|t\.co$|eeoc|apple\.co|spotify|soundcloud|"
    r"pinterest|tumblr|flickr|glassdoor|crunchbase|bloomberg|wikipedia|amazonaws|azure|office\.com|outlook\.com|mailchimp|"
    r"sendgrid|recaptcha|hcaptcha|stripe\.com|paypal|trustpilot|yelp|bbb\.org|instagr\.am|wa\.me|whatsapp|t\.me|discord", re.I)
# employers left alone: job marketplaces and staffing middlemen (their inbox is not a hiring manager's)
SKIP_EMPLOYER = re.compile(r"jobgether|lensa|talentify|jobot|hireology|recruit(ing|ers)? ?(inc|llc|group)?$|staffing|"
                           r"talent ?community|crowdstaffing|toptal|upwork|fiverr", re.I)
_SUFFIX = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "group", "holdings", "technologies", "technology",
           "tech", "labs", "usa", "us", "international", "global", "services", "solutions", "systems", "enterprises", "partners",
           "industries", "incorporated", "llp", "lp", "plc", "pbc", "careers", "jobs", "and"}
EMAIL_RX = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
JOB_LOCAL = re.compile(r"jobs?|careers?|recruit|talent|hiring|^hr$|^hr[._-]|[._-]hr$|humanresources|human[._-]resources|people|"
                       r"employment|resumes?|apply|join(us)?$|workwithus|opportunit", re.I)
GENERIC_LOCAL = re.compile(r"^(info|hello|contact|contactus|office|inquiries|inquiry|admin|team|general|mail|connect|hi|hey)$", re.I)
BAD_LOCAL = re.compile(r"no-?reply|donotreply|privacy|legal|press|media|^pr$|sales|support|help|billing|invoice|accounts?|^ar$|^ap$|"
                       r"payable|receivable|abuse|webmaster|security|marketing|partner|investor|^ir$|compliance|donat|event|ticket|"
                       r"order|return|service|bids?|rfp|vendor|procure|purchas|dmca|ethics|whistle|report|feedback|advertis|"
                       r"sponsor|affiliate|newsletter|unsubscribe|example|test|user|name|email|your|wholesale|booking|reserv", re.I)
PAGE_HINT = re.compile(r"contact|career|jobs|join|about|team|work-with-us|employment|hiring|company", re.I)


# ---------------------------------------------------------------------------------------------------------- helpers
def reg_domain(host: str) -> str:
    """'careers.acme.com' -> 'acme.com' ('acme.co.uk' kept whole)."""
    h = (host or "").lower().split(":")[0].strip(".")
    if h.startswith("www."):
        h = h[4:]
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _SUFFIX]


def _squash(name: str) -> str:
    return "".join(_words(name))


def name_fits_domain(name: str, slug: str, domain: str) -> bool:
    """'Relativity Space' / slug 'relativity' fits relativityspace.com; 'IMA Financial Group' does not fit imacorp.com
    by name alone (the website's own title decides then)."""
    label = re.sub(r"[^a-z0-9]", "", reg_domain(domain).split(".")[0])
    keys = {k for k in (_squash(name), re.sub(r"[^a-z0-9]", "", (slug or "").lower())) if len(k) >= 4}
    return bool(label) and any(k in label or (len(label) >= 5 and label in k) for k in keys)


def _site_title(page: str) -> str:
    bits = re.findall(r"(?is)<title[^>]*>(.*?)</title>|<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']+)", page or "")
    return _html.unescape(" ".join(x for pair in bits for x in pair if x)).lower()


def title_fits(name: str, page: str) -> bool:
    """The website calls itself by the employer's name: its <title> or og:site_name holds the name's main words, or the
    name written as one word ('Matchgroup' for 'Match Group')."""
    words = [w for w in _words(name) if len(w) >= 3][:2]
    t = _site_title(page)
    if words and all(re.search(r"(?<![a-z0-9])" + re.escape(w), t) for w in words):
        return True
    flat = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return len(flat) >= 5 and flat in re.sub(r"[^a-z0-9]", "", t)


class Fetcher:
    """Polite page reads: a site's robots.txt is respected, a few pages per site, a short timeout."""

    def __init__(self, get=None):
        self._get = get or (lambda url: requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True))
        self._robots: dict = {}
        self._pages: dict = {}
        self.calls = 0

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self._get(root + "/robots.txt")
                self.calls += 1
                rp.parse(r.text.splitlines() if getattr(r, "status_code", 200) == 200 else [])
            except Exception:
                rp.parse([])
            self._robots[root] = rp
        try:
            return self._robots[root].can_fetch(UA["User-Agent"], url)
        except Exception:
            return True

    def page(self, url: str) -> tuple[str, str]:
        """(final url, html) or ('', '') when not allowed or not readable. Each page is read once per run."""
        if url not in self._pages:
            self._pages[url] = self._read(url)
            if len(self._pages) > 300:
                self._pages.pop(next(iter(self._pages)))
        return self._pages[url]

    def _read(self, url: str) -> tuple[str, str]:
        if not self.allowed(url):
            return "", ""
        try:
            r = self._get(url)
            self.calls += 1
            if getattr(r, "status_code", 200) >= 400:
                return "", ""
            ctype = (getattr(r, "headers", {}) or {}).get("Content-Type", "text/html")
            if "html" not in ctype and "text" not in ctype:
                return "", ""
            return str(getattr(r, "url", url) or url), r.text[:2000000]
        except Exception:
            return "", ""


def _links(page: str, base: str) -> list[tuple[str, str]]:
    """(absolute url, link text) for each <a href> on a page."""
    out = []
    for m in re.finditer(r"(?is)<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", page or ""):
        href = _html.unescape(m.group(1)).strip()
        if href.lower().startswith(("javascript:", "tel:")):
            continue
        out.append((href if href.lower().startswith("mailto:") else urljoin(base, href), re.sub(r"<[^>]+>", " ", m.group(2))[:80]))
    return out


# ------------------------------------------------------------------------------------------------- the company's site
def domain_from_posting(job_url: str, name: str, slug: str, fetch: Fetcher) -> tuple[str, str]:
    """The employer's own website from its posting: the posting itself on the employer's site, else the site its job page
    links to (a Greenhouse or Lever job page links the company's site in its header). ('', why) when not found."""
    host = urlparse(job_url or "").netloc
    if host and not NOT_EMPLOYER_HOST.search(host):
        return reg_domain(host), "the posting is on its own site"
    if not job_url:
        return "", "no posting address"
    final, page = fetch.page(job_url)
    if not page:
        return "", "the job page could not be read"
    count: dict = {}
    for url, _t in _links(page, final or job_url):
        h = urlparse(url).netloc
        if not h or NOT_EMPLOYER_HOST.search(h):
            continue
        d = reg_domain(h)
        if d and not NOT_EMPLOYER_HOST.search(d):
            count[d] = count.get(d, 0) + 1
    for d in sorted(count, key=lambda d: -count[d]):
        if name_fits_domain(name, slug, d):
            return d, "linked from its job page"
    for d in sorted(count, key=lambda d: -count[d])[:2]:
        _f, home = fetch.page(f"https://{d}/")
        if home and title_fits(name, home):
            return d, "linked from its job page (its site carries its name)"
    return "", "its job page links no website of its own"


def domain_from_name(name: str, slug: str, fetch: Fetcher, suggest=None) -> tuple[str, str]:
    """A company-name lookup (Clearbit's free name-to-website service), accepted only for an exact, unambiguous name:
    two words or more (or one long one), the same name, and a website that calls itself by it. A short one-word name
    ('Vast', 'Relativity') is never looked up this way: too many companies share it."""
    try:
        rows = suggest(name) if suggest else requests.get(
            "https://autocomplete.clearbit.com/v1/companies/suggest?query=" + quote(name), headers=UA, timeout=TIMEOUT).json()
    except Exception:
        return "", "the name lookup failed"
    for r in (rows or [])[:3]:
        d = reg_domain(str(r.get("domain") or ""))
        found = str(r.get("name") or "")
        if not d or NOT_EMPLOYER_HOST.search(d) or _squash(found) != _squash(name):
            continue
        if len(_words(found)) < 2:
            return "", "a one-word name is shared by too many companies to look up safely"
        _f, home = fetch.page(f"https://{d}/")
        if home and (title_fits(found, home) or title_fits(name, home)):
            return d, "found by its name"
    return "", "no website matches the name exactly"


def proper_name(domain: str, fallback: str, fetch: Fetcher, suggest=None) -> str:
    """How the company writes its own name ('Rexford Industrial', not 'Rexfordindustrial'), for the greeting: the name
    the name-to-website service holds for this very website, else the site's own og:site_name when it is the same name,
    else the name the bot already had."""
    label = reg_domain(domain).split(".")[0]
    try:
        rows = suggest(label) if suggest else requests.get(
            "https://autocomplete.clearbit.com/v1/companies/suggest?query=" + quote(label), headers=UA, timeout=TIMEOUT).json()
    except Exception:
        rows = []
    for r in (rows or [])[:5]:
        if reg_domain(str(r.get("domain") or "")) == reg_domain(domain) and str(r.get("name") or "").strip():
            got = str(r["name"]).strip()
            if 2 <= len(got) <= 60:
                return got
    _f, home = fetch.page(f"https://{domain}/")
    m = re.search(r"(?is)<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']{2,60})", home or "")
    names = [_html.unescape(m.group(1)).strip()] if m else []
    t = re.search(r"(?is)<title[^>]*>(.*?)</title>", home or "")
    if t:                  # the site's own title, its part naming the company: 'K2 Space | Satellites...' -> 'K2 Space'
        names += [x.strip() for x in re.split(r"\s+[|\u2013\u2014\u2022\u00b7-]\s+|:\s", _html.unescape(t.group(1)).strip()) if x.strip()][:3]
    for got in names:
        if 2 <= len(got) <= 60 and _squash(got) and (_squash(got) in _squash(fallback) or _squash(fallback) in _squash(got)):
            return got
    return fallback


def best_address(found: dict, domain: str) -> str:
    """Of the addresses a site publishes, the one for job seekers: careers@ / jobs@ / recruiting@ ... first, then info@ /
    hello@ / contact@. Only addresses on the company's own domain; a person's address is never used."""
    def ok(e):
        local, _, dom = e.partition("@")
        if reg_domain(dom) != domain or BAD_LOCAL.search(local):
            return False
        return bool(JOB_LOCAL.search(local) or GENERIC_LOCAL.match(local))      # a person's address never qualifies
    emails = [e for e in found if ok(e)]
    jobs = [e for e in emails if JOB_LOCAL.search(e.split("@")[0])]
    if jobs:
        return sorted(jobs, key=lambda e: (-found[e], len(e)))[0]
    generic = [e for e in emails if GENERIC_LOCAL.match(e.split("@")[0])]
    return sorted(generic, key=lambda e: (-found[e], ["info", "hello", "contact", "contactus"].index(e.split("@")[0].lower())
                                           if e.split("@")[0].lower() in ("info", "hello", "contact", "contactus") else 9))[0] if generic else ""


def emails_on(page: str) -> list[str]:
    """Plain addresses a page shows (mailto links and visible text). Addresses a site hides from collectors (Cloudflare's
    email protection and similar) are left hidden."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page or "")
    found = [m.group(0).rstrip(".") for m in EMAIL_RX.finditer(_html.unescape(re.sub(r"%40", "@", text)))]
    return [e.lower() for e in found if not re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", e, re.I)]


def find_address(domain: str, fetch: Fetcher, max_pages: int = 5) -> tuple[str, str]:
    """(address, page it was on) from the company's own pages, or ('', why)."""
    found: dict = {}
    where: dict = {}
    final, home = fetch.page(f"https://{domain}/")
    if not home:
        final, home = fetch.page(f"https://www.{domain}/")
    if not home:
        return "", "its website could not be read"
    pages = [(final, home)]
    seen = {final.rstrip("/")}
    cands = []
    for url, text in _links(home, final):
        if url.lower().startswith("mailto:"):
            continue
        h = urlparse(url).netloc
        if reg_domain(h) == domain and (PAGE_HINT.search(urlparse(url).path) or PAGE_HINT.search(text)):
            cands.append(url.split("?")[0].rstrip("/"))
    for path in ("/careers", "/contact", "/contact-us", "/about"):
        cands.append(f"https://{urlparse(final).netloc or domain}{path}")
    for url in dict.fromkeys(cands):
        if len(pages) >= max_pages:
            break
        if url in seen:
            continue
        seen.add(url)
        f, p = fetch.page(url)
        if p:
            pages.append((f, p))
    for f, p in pages:
        for url, _t in _links(p, f):
            if url.lower().startswith("mailto:"):
                e = url[7:].split("?")[0].strip().lower()
                if EMAIL_RX.fullmatch(e):
                    found[e] = found.get(e, 0) + 2
                    where.setdefault(e, f)
        for e in emails_on(p):
            found[e] = found.get(e, 0) + 1
            where.setdefault(e, f)
    best = best_address(found, domain)
    if best:
        return best, where.get(best, "")
    return "", "no hiring or general address on its site (it may use a contact form)"


# ------------------------------------------------------------------------------------------------------- the email
DEFAULT_PITCH = ("I'm a USC Industrial and Systems Engineering graduate (B.S., 2025) with hands-on operations experience. At "
                 "Roaring Fork Property Group I wrote the standard operating procedures and coordinated vendors and inspection "
                 "schedules across 70+ properties, and for a construction company I built the QuickBooks and Excel tracking for "
                 "invoicing and payroll. I work comfortably in Excel, SQL and Python, and I'm bilingual in English and Spanish.")


def _where(city: str) -> str:
    """' in Torrance, CA' from a posting's location ('US-CA-Torrance', 'CA - Torrance', 'Rockville, MD or Torrance, CA')."""
    parts = [p for p in re.split(r"\s*(?:;|\||/|\bor\b|\n)\s*", city or "") if p.strip()]
    c = next((p for p in parts if la_area(p)), parts[0] if parts else "")
    c = re.sub(r"\s*\(.*?\)|,?\s*(united states( of america)?|usa)\b.*$", "", c, flags=re.I).strip(" ,-")
    m = re.match(r"^(?:us-)?([a-z]{2})\s*-\s*(.+)$", c, re.I)
    if m:
        c = f"{m.group(2).strip()}, {m.group(1).upper()}"
    return f" in {c}" if c and len(c) < 40 and not re.search(r"remote|hybrid|location", c, re.I) else ""


def compose(row, cfg: dict) -> tuple[str, str]:
    """(subject, text) of the first email to this company. Deterministic: the draft in your email is what is sent."""
    o = cfg.get("outreach") or {}
    f = cfg.get("facts") or {}
    company = row["company"]
    role = (row["role"] or "").strip()
    role = re.sub(r"\s*[\(\[][^)\]]*(req|id|#)[^)\]]*[\)\]]|\s+-\s+\d{3,}.*$", "", role, flags=re.I).strip()
    where = _where(row["city"] or "")
    if row["kind"] == "applied" and role:
        subject = f"Following up on my application: {role}"
        opener = f"I recently applied for the {role} role{where} and wanted to introduce myself directly."
    elif role:
        subject = f"{role}: USC Industrial & Systems Engineering graduate"
        opener = f"I saw that {company} is hiring for the {role} role{where}, and I wanted to reach out directly."
    else:
        subject = "Operations and project coordination: USC engineering graduate in Los Angeles"
        opener = f"I'm reaching out to ask whether {company} has openings in operations, project coordination or analysis."
    home_la = la_area(f"{f.get('city', '')}, {f.get('state', '')}")
    available = o.get("availability") or ("I'm based in the Los Angeles area and can start right away, full-time or contract-to-hire."
                                          if home_la else "I'm focused on roles in Los Angeles and can start right away, "
                                                          "full-time or contract-to-hire.")
    name = str(f.get("full_name") or "").strip()
    contact = " | ".join(x for x in (str(f.get("phone") or "").strip(), str(o.get("from_email") or os.environ.get("IMAP_USER") or
                                                                         f.get("email") or "").strip()) if x)
    lines = [f"Hi {company} team,", "", opener, "", str(o.get("pitch") or DEFAULT_PITCH).strip(), "",
             f"{available} My résumé is attached; if there's a fit, I'd welcome a short call.", "", "Thank you,", name]
    if contact:
        lines.append(contact)
    if o.get("include_linkedin") and f.get("linkedin"):
        lines.append(str(f["linkedin"]))
    lines += ["", "(If this isn't the right inbox or the timing is off, just let me know and I won't follow up.)"]
    return subject, "\n".join(lines)


def compose_followup(row, cfg: dict, first_text: str) -> tuple[str, str]:
    f = cfg.get("facts") or {}
    role = (row["role"] or "").strip()
    about = f"the {role} role" if role else "openings on your team"
    name = str(f.get("full_name") or "").strip()
    sent = (row["sent_at"] or "")[:10]
    body = [f"Hi {row['company']} team,", "",
            f"I wanted to follow up on my note below about {about}. I'm still very interested and can start right away; "
            "my résumé is attached again for convenience.", "", "Thank you,", name, "",
            f"On {sent}, I wrote:"] + ["> " + x for x in first_text.splitlines()]
    subj = row["subject"] or ""
    return (subj if subj.lower().startswith("re:") else f"Re: {subj}"), "\n".join(body)


def _send(cfg: dict, to: str, subject: str, text: str, attach: Path | None, msg_id: str, reply_to_id: str = "") -> None:
    user, pw = os.environ["IMAP_USER"], os.environ["IMAP_PASS"]
    f = cfg.get("facts") or {}
    msg = EmailMessage()
    msg["From"] = f"{f.get('full_name', '')} <{user}>" if f.get("full_name") else user
    msg["To"], msg["Subject"], msg["Date"], msg["Message-ID"] = to, subject, formatdate(localtime=False), msg_id
    if reply_to_id:
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = reply_to_id
    msg.set_content(text)
    if attach and attach.is_file():
        msg.add_attachment(attach.read_bytes(), maintype="application", subtype="pdf", filename=attach.name)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


# ------------------------------------------------------------------------------------------------------ the pipeline
def ensure(db):
    db.conn.executescript(SCHEMA)
    db.conn.commit()


def norm_co(name: str) -> str:
    """The same employer key main uses ('Acme, Inc.' and 'acme' are one employer)."""
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|the|group|holdings)\b", " ", n)
    return "".join(n.split())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def select_targets(db, cfg: dict, company_name, days: int = 45) -> int:
    """Add the Los Angeles employers worth an email (once a day). Returns how many were added."""
    from .sources import GOV_NAME_RX, GOV_URL_RX
    ensure(db)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    have = {r[0] for r in db.conn.execute("SELECT co FROM outreach WHERE co IS NOT NULL")} | \
           {norm_co(r[0]) for r in db.conn.execute("SELECT company FROM outreach")}
    try:
        no = {norm_co(r[0]) for r in db.conn.execute("SELECT company FROM jobs WHERE outcome = 'rejection'")}
    except Exception:
        no = set()
    rows = db.conn.execute(
        "SELECT key, company, title, location, status, fit, reason, url, apply_url, first_seen, "
        "COALESCE(NULLIF(submitted_at, ''), updated) AS t FROM jobs WHERE first_seen >= ? AND status IN "
        "('applied', 'unconfirmed', 'manual', 'queued', 'low_score', 'skipped', 'blocked')", (cutoff,)).fetchall()
    best: dict = {}
    rank = {"applied": 0, "hand": 1, "hiring": 2}
    for r in rows:
        if not la_area(r["location"] or ""):
            continue
        fit = r["fit"] or 0
        if r["status"] in ("applied", "unconfirmed"):
            kind = "applied"
        elif r["status"] == "manual":
            kind = "hand"
        elif fit >= 45 or (r["status"] == "queued" and fit <= 0):
            kind = "hiring"
        else:
            continue
        name = company_name(r)
        co = norm_co(r["company"] or "")
        if not co or co in have or co in no or SKIP_EMPLOYER.search(f"{r['company']} {name}") or GOV_NAME_RX.search(name) \
                or GOV_URL_RX.search(f"{r['url'] or ''} {r['apply_url'] or ''}"):
            continue
        cur = best.get(co)
        score = (rank[kind], -fit)        # the closest tie to you first; of equals, the most recent posting
        if cur is None or score < cur[0] or (score == cur[0] and str(r["t"] or "") > str(cur[1]["t"] or "")):
            best[co] = (score, r, kind, name)
    added = 0
    for co, (_s, r, kind, name) in best.items():
        key = f"pending:{co}"                 # the website is not known yet: keyed by the employer until it is
        db.conn.execute("INSERT OR IGNORE INTO outreach (domain, co, company, kind, role, job_key, city, status, found_at, updated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
                        (key, co, name, kind, r["title"], r["key"], r["location"], _now(), _now()))
        added += 1
    db.conn.commit()
    return added


def resolve(db, log, limit: int = 8, budget_s: float = 90, fetch: Fetcher | None = None, suggest=None) -> int:
    """Find the website and the published hiring address of up to `limit` new targets. Returns how many got an address."""
    ensure(db)
    fetch = fetch or Fetcher()
    t0 = time.time()
    got = 0
    order = "CASE kind WHEN 'applied' THEN 0 WHEN 'hand' THEN 1 ELSE 2 END"
    for r in db.conn.execute(f"SELECT * FROM outreach WHERE status = 'new' AND tries < 2 ORDER BY {order}, found_at LIMIT ?",
                             (limit,)).fetchall():
        if time.time() - t0 > budget_s:
            break
        job = db.conn.execute("SELECT company, url, apply_url FROM jobs WHERE key = ?", (r["job_key"],)).fetchone()
        slug = (job["company"] if job else "") or ""
        domain, how = "", ""
        for u in ((job["url"], job["apply_url"]) if job else ()):
            if u and not domain:
                domain, how = domain_from_posting(u, r["company"], slug, fetch)
        if not domain:
            domain, how = domain_from_name(r["company"], slug, fetch, suggest)
        if not domain:
            db.conn.execute("UPDATE outreach SET status = 'no_site', reason = ?, tries = tries + 1, updated = ? WHERE domain = ?",
                            (how[:120], _now(), r["domain"]))
            continue
        if db.conn.execute("SELECT 1 FROM outreach WHERE domain = ?", (domain,)).fetchone():
            db.conn.execute("DELETE FROM outreach WHERE domain = ?", (r["domain"],))      # the same company, already listed
            continue
        email, where = find_address(domain, fetch)
        status = "ready" if email else "no_email"
        name = proper_name(domain, r["company"], fetch, suggest) if email else r["company"]
        db.conn.execute("UPDATE outreach SET domain = ?, email = ?, company = ?, status = ?, reason = ?, tries = tries + 1, "
                        "updated = ? WHERE domain = ?", (domain, email, name, status, (how if email else where)[:120], _now(), r["domain"]))
        got += bool(email)
        log(f"  outreach: {name} ({domain}): {'a hiring address found' if email else where}")
    db.conn.commit()
    return got


def _la_business_hours(now_utc: datetime | None = None) -> bool:
    """Weekdays, 8:30am to 4:30pm in Los Angeles."""
    from zoneinfo import ZoneInfo
    t = (now_utc or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo("America/Los_Angeles"))
    return t.weekday() < 5 and (8 * 60 + 30) <= t.hour * 60 + t.minute <= (16 * 60 + 30)


def _resume(cfg: dict, base: Path) -> Path | None:
    p = base / str(cfg.get("resume_file") or "")
    if cfg.get("resume_file") and p.is_file():
        return p
    found = sorted(base.glob("*resume*.pdf"))
    return found[0] if found else None


def check_replies(db, log, notes: list, search=None, every_minutes: float = 60) -> dict:
    """Replies, bounces and 'no thanks' answers to the emails sent (at most once an hour, new mail only). A reply goes to
    the top of your email."""
    from . import mailbox
    rows = db.conn.execute("SELECT * FROM outreach WHERE status IN ('sent', 'followed_up') AND sent_at >= ?",
                           ((datetime.now() - timedelta(days=45)).isoformat(timespec="seconds"),)).fetchall()
    if not rows:
        return {}
    last = db.meta_get("outreach_replies_at", "")
    try:
        last_ts = datetime.fromisoformat(last).timestamp() if last else 0.0
    except ValueError:
        last_ts = 0.0
    if search is None and last_ts and time.time() - last_ts < every_minutes * 60:
        return {}
    since = max(min(datetime.fromisoformat(r["sent_at"]).timestamp() for r in rows), last_ts - 3 * 3600)
    doms = sorted({r["domain"] for r in rows})
    q = "from:mailer-daemon OR from:postmaster OR subject:re OR " + " OR ".join(f"from:{d}" for d in doms[:60])
    try:
        msgs = (search or mailbox.search)(since, q, ("INBOX", "[Gmail]/Spam"))
    except Exception as e:
        log(f"  outreach: replies could not be read ({str(e)[:80]})")
        return {}
    out: dict = {}
    settled: set = set()                  # companies whose answer this pass already recorded
    for m in sorted(msgs, key=lambda m: m["ts"]):
        frm = (m.get("from") or "").lower()
        body = m.get("body") or ""
        refs = f"{m.get('in_reply_to', '')} {m.get('references', '')}"
        for r in rows:
            if r["domain"] in settled:
                continue
            if re.search(r"mailer-daemon|postmaster", frm):
                if r["email"] and r["email"].lower() in body.lower():
                    db.conn.execute("UPDATE outreach SET status = 'bounced', updated = ? WHERE domain = ?", (_now(), r["domain"]))
                    out["bounced"] = out.get("bounced", 0) + 1
                    settled.add(r["domain"])
                continue
            fdom = re.search(r"@([\w.-]+)", frm)
            subj = m.get("subject") or ""
            ours = (r["msg_id"] and r["msg_id"] in refs) or (
                fdom and reg_domain(fdom.group(1)) == r["domain"] and re.match(r"\s*(re|aw|sv)\s*:", subj, re.I)
                and m["ts"] >= datetime.fromisoformat(r["sent_at"]).timestamp())
            if not ours:
                continue
            if m.get("auto") or re.search(r"out of (the )?office|automatic reply|auto[- ]?reply|autoreply|received your (email|message)|"
                                          r"thank you for (contacting|reaching out|your (email|message))", subj, re.I):
                continue                          # an automatic answer: the follow-up still goes
            from .outcomes import plain
            text = plain(body)[:1500]
            stop = re.search(r"\b(unsubscribe|remove me|not interested|no thanks|no, thank|stop (emailing|contacting)|do not contact|"
                             r"don't contact|take me off)\b", text, re.I)
            status = "opted_out" if stop else "replied"
            db.conn.execute("UPDATE outreach SET status = ?, replied_at = ?, updated = ? WHERE domain = ?",
                            (status, datetime.fromtimestamp(m["ts"]).isoformat(timespec="seconds"), _now(), r["domain"]))
            out[status] = out.get(status, 0) + 1
            settled.add(r["domain"])
            if status == "replied":
                notes.append(f"REPLY to your outreach from {r['company']}: \"{subj[:90]}\". Answer it from your inbox")
    db.conn.commit()
    db.meta_set("outreach_replies_at", _now())
    if out:
        log("  outreach replies: " + ", ".join(f"{n} {k}" for k, n in out.items()))
    return out


def run(cfg: dict, db, company_name, base: Path, log=print, notes: list | None = None, dry_run: bool = False,
        fetch: Fetcher | None = None) -> dict:
    """One run's outreach: new targets (once a day), a few addresses looked up, replies read, and (when outreach.send is
    on) a few emails sent. Returns {'report': [lines for your email], 'drafts': [(to, subject, text)]}."""
    o = cfg.get("outreach") or {}
    if not o.get("enabled", True):
        return {}
    ensure(db)
    notes = notes if notes is not None else []
    today = datetime.now().date().isoformat()
    if db.meta_get("outreach_targets_day") != today:
        n = select_targets(db, cfg, company_name, int(o.get("lookback_days", 45)))
        db.meta_set("outreach_targets_day", today)
        if n:
            log(f"Outreach: {n} Los Angeles employers added to look up")
    resolve(db, log, int(o.get("lookups_per_run", 8)), float(o.get("lookup_seconds", 90)), fetch)
    # names still written the way a job-board address spells them ('K2spacecorporation'): the company's own spelling
    for r in db.conn.execute("SELECT domain, company FROM outreach WHERE status = 'ready' AND company NOT LIKE '% %' "
                             "AND length(company) > 9").fetchall()[:5]:
        better = proper_name(r["domain"], r["company"], fetch or Fetcher())
        if better != r["company"]:
            db.conn.execute("UPDATE outreach SET company = ? WHERE domain = ?", (better, r["domain"]))
    db.conn.commit()
    from . import mailbox
    if mailbox.configured():
        check_replies(db, log, notes)
    send_on = bool(o.get("send")) and not dry_run and mailbox.configured()
    per_day, per_run = int(o.get("per_day", 15)), int(o.get("per_run", 3))
    sent_today = db.conn.execute("SELECT COUNT(*) FROM outreach WHERE substr(sent_at, 1, 10) = ? OR substr(followup_at, 1, 10) = ?",
                                 (today, today)).fetchone()[0]
    order = "CASE kind WHEN 'applied' THEN 0 WHEN 'hand' THEN 1 ELSE 2 END, updated DESC"
    ready = db.conn.execute(f"SELECT * FROM outreach WHERE status = 'ready' ORDER BY {order}").fetchall()
    resume = _resume(cfg, base)
    done_now = 0
    try:                                  # employers that answered an application since: no cold email, no follow-up
        answered = {norm_co(c): k for c, k in db.conn.execute("SELECT company, outcome FROM jobs WHERE outcome IS NOT NULL "
                                                                "AND outcome != ''")}
    except Exception:
        answered = {}
    if send_on and _la_business_hours() and resume:
        due = db.conn.execute("SELECT * FROM outreach WHERE status = 'sent' AND sent_at <= ?",
                              ((datetime.now() - timedelta(days=int(o.get("followup_days", 7)))).isoformat(timespec="seconds"),)).fetchall()
        for r in list(due) + list(ready):
            if done_now >= per_run or sent_today + done_now >= per_day:
                break
            if r["co"] and r["co"] in answered:
                why = {"rejection": "they turned down your application", "interview": "they invited you to interview",
                       "offer": "they made you an offer"}.get(answered[r["co"]], "they answered your application")
                db.conn.execute("UPDATE outreach SET status = 'skipped', reason = ?, updated = ? WHERE domain = ?", (why, _now(), r["domain"]))
                db.conn.commit()
                continue
            try:
                if r["status"] == "sent":
                    subj0, text0 = compose(r, cfg)
                    subj, text = compose_followup(r, cfg, text0)
                    _send(cfg, r["email"], subj, text, resume, make_msgid(domain=(os.environ.get("IMAP_USER") or "x@x").split("@")[-1]),
                          reply_to_id=r["msg_id"] or "")
                    db.conn.execute("UPDATE outreach SET status = 'followed_up', followup_at = ?, updated = ? WHERE domain = ?",
                                    (_now(), _now(), r["domain"]))
                    log(f"  outreach: follow-up sent to {r['company']}")
                else:
                    subj, text = compose(r, cfg)
                    mid = make_msgid(domain=(os.environ.get("IMAP_USER") or "x@x").split("@")[-1])
                    _send(cfg, r["email"], subj, text, resume, mid)
                    db.conn.execute("UPDATE outreach SET status = 'sent', sent_at = ?, msg_id = ?, subject = ?, updated = ? "
                                    "WHERE domain = ?", (_now(), mid, subj, _now(), r["domain"]))
                    log(f"  outreach: email sent to {r['company']} ({r['domain']})")
                db.conn.commit()
                done_now += 1
                time.sleep(random.uniform(20, 45))
            except Exception as e:
                log(f"  ! outreach to {r['company']} failed: {str(e)[:100]}")
                db.conn.execute("UPDATE outreach SET reason = ?, updated = ? WHERE domain = ?", (f"send failed: {str(e)[:80]}", _now(), r["domain"]))
                db.conn.commit()
                break
    counts = {k: v for k, v in db.conn.execute("SELECT status, COUNT(*) FROM outreach GROUP BY status")}
    report = []
    sent_total = sum(counts.get(k, 0) for k in DONE)
    if send_on:
        report.append(f"Sent today: {sent_today + done_now} (at most {per_day} a day, weekdays 8:30am-4:30pm Los Angeles time). "
                      f"In all: {sent_total} companies emailed, {counts.get('replied', 0)} replied, {counts.get('bounced', 0)} "
                      f"bounced, {counts.get('opted_out', 0)} said no thanks; {counts.get('ready', 0)} more ready.")
    else:
        report.append(f"OFF until you turn it on: {counts.get('ready', 0)} Los Angeles employers have a published hiring address and "
                      f"a draft ready ({counts.get('new', 0)} still to look up, {counts.get('no_email', 0) + counts.get('no_site', 0)} "
                      f"have no public address). Below is exactly what would be sent. To start (at most {per_day} a day, weekdays "
                      f"in LA business hours), set outreach.send: true in settings.yaml.")
    drafts = []
    if not send_on:
        for r in ready[: int(o.get("preview", 3))]:
            subj, text = compose(r, cfg)
            drafts.append((r["email"], subj, text))
    return {"report": report, "drafts": drafts, "sent": done_now}
