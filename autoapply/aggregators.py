"""Extra job sources: free public job-board APIs and feeds (no LinkedIn/Indeed logins, no HTML scraping of search pages).

Every fetcher returns Jobs whose apply_url may point at an intermediary page; submit.resolve_apply_url follows it to the
employer's real application form when the job is about to be applied to."""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from .sources import Job, _strip_html, prefilter, discover as discover_boards, norm_location, foreign_place, _remote_ish

UA = {"User-Agent": "Mozilla/5.0 (compatible; autoapply/1.0; personal job search)"}
TIMEOUT = 25
TTL_HOURS = {"linkedin": 2, "jooble": 3, "remotive": 6, "remoteok": 1, "himalayas": 2, "jicy": 2, "wwr": 2, "themuse": 3, "adzuna": 3,
             "workablejobs": 3, "arbeitnow": 3, "workingnomads": 3, "githublists": 3, "builtin": 3, "careerjet": 3, "localfeed": 1}

ATS_RX = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    "workable": re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)/(?:j|jobs)/"),
    "bamboohr": re.compile(r"([A-Za-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)"),
    "recruitee": re.compile(r"([A-Za-z0-9-]+)\.recruitee\.com/(?:o|l)/"),
    "breezy": re.compile(r"([A-Za-z0-9-]+)\.breezy\.hr/p/"),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?!oneclick)([A-Za-z0-9_-]+)/\d"),
}
_NOT_A_BOARD = ("embed", "jobs", "www", "careers", "apply", "app", "api", "j", "static", "assets")

# a link to ONE job on a supported application system (used to skip the browser when an aggregator page already contains it)
JOB_LINK_RX = re.compile(
    r"https?://(?:(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=[\w-]+&(?:amp;)?token=\d+|[\w-]+/jobs/\d+)"
    r"|jobs\.lever\.co/[\w-]+/[0-9a-f-]{36}"
    r"|apply\.workable\.com/(?:[\w-]+/)?j/\w+"
    r"|[\w-]+\.bamboohr\.com/careers/\d+"
    r"|[\w-]+\.recruitee\.com/o/[\w-]+"
    r"|[\w-]+\.breezy\.hr/p/[\w-]+"
    r"|[\w-]+\.wd\d+\.myworkdayjobs\.com/[^\s\"'<>\\]+)", re.I)


def board_of(url: str):
    """('greenhouse', 'airbnb') when the URL is a job on a supported ATS board, else None."""
    for ats, rx in ATS_RX.items():
        m = rx.search(url or "")
        if m and m.group(1).lower() not in _NOT_A_BOARD:
            return ats, m.group(1)
    return None


def find_job_link(text: str, final_url: str = "") -> str | None:
    """First link to a single job on a supported application system found in a page's HTML (or the URL it landed on)."""
    for src in (final_url, text or ""):
        m = JOB_LINK_RX.search(src or "")
        if m:
            return html_unescape(m.group(0)).rstrip("\\.,;)")
    return None


def html_unescape(u: str) -> str:
    import html as _h
    return _h.unescape(u)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "unknown").lower()).strip("-")[:40] or "unknown"


def _job(src, company, jid, title, location, apply_url, desc, url="", **extra) -> Job:
    return Job(source=f"agg-{src}", company=_slug(company), job_id=str(jid), title=(title or "").strip(),
               location=(location or "Remote").strip(), url=url or apply_url, apply_url=apply_url,
               description=_strip_html(desc)[:6000], extra={"company_name": company, **extra})


def _get(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------------------------- fetchers
def remoteok(cfg, base="https://remoteok.com"):
    out = []
    for j in _get(base + "/api").json():
        if not isinstance(j, dict) or not j.get("position"):
            continue
        desc = j.get("description", "")
        if j.get("salary_max"):
            desc += f"\nSalary: ${j.get('salary_min', 0)} - ${j['salary_max']}"
        out.append(_job("remoteok", j.get("company"), j.get("id") or j.get("slug"), j["position"], j.get("location") or "Remote",
                        j.get("apply_url") or j.get("url") or "", desc, j.get("url", "")))
    return out


def remotive(cfg, base="https://remotive.com"):
    out = []
    for cat in ("business", "management-finance", "customer-support", "all-others", "product"):
        try:
            data = _get(base + "/api/remote-jobs", params={"category": cat, "limit": 200}).json()
        except Exception:
            continue
        for j in data.get("jobs", []):
            out.append(_job("remotive", j.get("company_name"), j.get("id"), j.get("title"),
                            "Remote " + (j.get("candidate_required_location") or ""), j.get("url", ""),
                            j.get("description", "") + (f"\nSalary: {j['salary']}" if j.get("salary") else "")))
    return out


def jicy(cfg, base="https://jobicy.com"):
    out = []
    for j in _get(base + "/api/v2/remote-jobs", params={"count": 100, "geo": "usa"}).json().get("jobs", []):
        sal = f"\nSalary: ${j['annualSalaryMin']} - ${j['annualSalaryMax']}" if j.get("annualSalaryMin") and j.get("annualSalaryMax") else ""
        out.append(_job("jicy", j.get("companyName"), j.get("id"), j.get("jobTitle"), "Remote " + str(j.get("jobGeo") or ""),
                        j.get("url", ""), (j.get("jobDescription") or j.get("jobExcerpt") or "") + sal))
    return out


def himalayas(cfg, base="https://himalayas.app"):
    out = []
    for j in _get(base + "/jobs/api", params={"limit": 100}).json().get("jobs", []):
        locs = j.get("locationRestrictions") or []
        loc = "Remote " + (", ".join(locs) if isinstance(locs, list) else str(locs))
        sal = f"\nSalary: ${j['minSalary']} - ${j['maxSalary']}" if j.get("minSalary") and j.get("maxSalary") else ""
        out.append(_job("himalayas", j.get("companyName"), j.get("guid") or j.get("title"), j.get("title"), loc,
                        j.get("applicationLink") or j.get("guid") or "", (j.get("description") or j.get("excerpt") or "") + sal))
    return out


def wwr(cfg, base="https://weworkremotely.com"):
    out = []
    for feed in ("remote-management-and-finance-jobs", "remote-customer-support-jobs", "remote-product-jobs", "remote-jobs"):
        try:
            root = ET.fromstring(_get(f"{base}/categories/{feed}.rss").content)
        except Exception:
            continue
        for it in root.iter("item"):
            t = (it.findtext("title") or "")
            company, _, title = t.partition(": ")
            if not title:
                company, title = "", t
            link = it.findtext("link") or ""
            out.append(_job("wwr", company, link.rsplit("/", 1)[-1], title, "Remote " + (it.findtext("region") or ""), link,
                            it.findtext("description") or ""))
    return out


def themuse(cfg, base="https://www.themuse.com"):
    a = cfg.get("aggregators", {}) or {}
    out = []
    for cat in ("Business Operations", "Project Management", "Management", "Data and Analytics", "Real Estate",
                "Manufacturing and Warehouse", "Construction", "Operations"):
        for loc in a.get("locations", []) + ["Flexible / Remote"]:
            for page in (1, 2):
                try:
                    data = _get(base + "/api/public/jobs", params=[("page", page), ("category", cat), ("location", loc),
                                                                    ("level", "Entry Level"), ("level", "Mid Level")]).json()
                except Exception:
                    break
                for j in data.get("results", []):
                    locs = ", ".join(x.get("name", "") for x in j.get("locations", [])) or "Remote"
                    out.append(_job("themuse", (j.get("company") or {}).get("name"), j.get("id"), j.get("name"), locs,
                                    (j.get("refs") or {}).get("landing_page", ""), j.get("contents", "")))
                if page >= data.get("page_count", 1):
                    break
    return out


def adzuna(cfg, base="https://api.adzuna.com"):
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and key):
        return []
    a = cfg.get("aggregators", {}) or {}
    sc = cfg.get("scoring", {}) or {}
    out = []
    for q in a.get("queries", []):
        for where in a.get("locations", []) + [""]:
            try:
                data = _get(f"{base}/v1/api/jobs/us/search/1", params={
                    "app_id": app_id, "app_key": key, "results_per_page": 50, "what": q, "where": where,
                    "max_days_old": 7, "salary_min": int(sc.get("salary_floor", 0) or 0) - 5000 or "",
                    "content-type": "application/json"}).json()
            except Exception:
                continue
            for j in data.get("results", []):
                sal = f"\nSalary: ${int(j['salary_min'])} - ${int(j['salary_max'])}" if j.get("salary_min") and j.get("salary_max") else ""
                out.append(_job("adzuna", (j.get("company") or {}).get("display_name"), j.get("id"), j.get("title"),
                                (j.get("location") or {}).get("display_name", ""), j.get("redirect_url", ""),
                                (j.get("description") or "") + sal))
    return out


def _linkedin_cards(fragment: str):
    """(job_id, title, company, location) from LinkedIn's public guest search fragment."""
    out = []
    for m in re.finditer(r"<li>(.*?)</li>", fragment, re.S):
        card = m.group(1)
        idm = re.search(r"/jobs/view/[^\"?]*?-?(\d{6,})[?\"]", card) or re.search(r"data-entity-urn=\"urn:li:jobPosting:(\d+)\"", card)
        title = re.search(r"base-search-card__title[^>]*>\s*(.*?)\s*<", card, re.S)
        comp = re.search(r"base-search-card__subtitle[^>]*>(?:\s*<a[^>]*>)?\s*(.*?)\s*<", card, re.S)
        loc = re.search(r"job-search-card__location[^>]*>\s*(.*?)\s*<", card, re.S)
        if idm and title:
            out.append((idm.group(1), _strip_html(title.group(1)), _strip_html(comp.group(1)) if comp else "",
                        _strip_html(loc.group(1)) if loc else ""))
    return out


def _linkedin_detail(page_html: str):
    """(description, employer apply url or '') from a public guest job posting page. Easy-Apply-only jobs give ''."""
    from urllib.parse import unquote
    d = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page_html, re.S)
    desc = _strip_html(d.group(1)) if d else ""
    ext = re.search(r'externalApply[^"\s<>]*?[?&]url=([^"&\s<>]+)', page_html)
    if ext:
        return desc, unquote(ext.group(1))
    m = re.search(r'id="applyUrl"[^>]*>\s*(?:<!--)?\s*"?(https?://[^"\s<>-]+[^"\s<>]*)', page_html)
    if m and "linkedin.com" not in m.group(1):
        return desc, m.group(1)
    return desc, ""


def linkedin(cfg, base="https://www.linkedin.com"):
    """LinkedIn's PUBLIC guest job search (no login, no account). Only postings that hand off to the employer's own site
    are kept; Easy-Apply-only postings need a login and are left alone."""
    a = cfg.get("aggregators", {}) or {}
    qs = (a.get("linkedin_queries") or a.get("queries") or [])[:8]
    locs = a.get("locations", []) + ["United States"]
    out, seen, details, fails = [], set(), 0, 0
    for q in qs:
        for loc in locs:
            if fails >= 3:            # LinkedIn is refusing this network: give up fast
                return out
            try:
                r = requests.get(base + "/jobs-guest/jobs/api/seeMoreJobPostings/search", headers=UA, timeout=10,
                                 params={"keywords": q, "location": loc, "f_TPR": "r172800", "start": 0})
                r.raise_for_status()
                frag = r.text
                fails = 0
            except Exception:
                fails += 1
                continue
            time.sleep(1.0)
            for jid, title, company, jloc in _linkedin_cards(frag):
                if jid in seen or details >= int(a.get("linkedin_max_details", 80)):
                    continue
                seen.add(jid)
                probe = Job("agg-linkedin", _slug(company), jid, title, jloc or loc, "", "", "")
                if prefilter(probe, cfg.get("search", {}) or {}):
                    continue                      # not a title/location fit: don't spend a request on its description
                try:
                    detail = _get(f"{base}/jobs-guest/jobs/api/jobPosting/{jid}").text
                except Exception:
                    continue
                details += 1
                time.sleep(1.0)
                desc, ext = _linkedin_detail(detail)
                if ext:
                    out.append(_job("linkedin", company, jid, title, jloc or loc, ext, desc, f"https://www.linkedin.com/jobs/view/{jid}"))
    return out


def jooble(cfg, base="https://jooble.org"):
    key = os.environ.get("JOOBLE_API_KEY")
    if not key:
        return []
    a = cfg.get("aggregators", {}) or {}
    out = []
    for q in a.get("queries", []):
        for where in a.get("locations", []) + ["Remote"]:
            try:
                r = requests.post(f"{base}/api/{key}", json={"keywords": q, "location": where, "page": 1},
                                  headers=UA, timeout=TIMEOUT)
                r.raise_for_status()
            except Exception:
                continue
            for j in r.json().get("jobs", []):
                out.append(_job("jooble", j.get("company"), j.get("id") or j.get("link"), j.get("title"), j.get("location"),
                                j.get("link", ""), (j.get("snippet") or "") + (f"\nSalary: {j['salary']}" if j.get("salary") else "")))
    return out


def workablejobs(cfg, base="https://jobs.workable.com"):
    """Workable's own public job search (thousands of small and mid-size employers; no key)."""
    a = cfg.get("aggregators", {}) or {}
    out, seen = [], set()
    for q in (a.get("queries") or [])[:12]:
        for where in (a.get("locations") or []) + [""]:
            params = {"query": q}
            if where:
                params["location"] = where
            else:
                params["workplace"] = "remote"
            try:
                data = _get(base + "/api/v1/jobs", params=params).json()
            except Exception:
                continue
            for j in data.get("jobs", []) or []:
                if j.get("id") in seen or j.get("state", "published") != "published":
                    continue
                seen.add(j.get("id"))
                loc = j.get("locations")
                loc = ", ".join(loc) if isinstance(loc, list) else (loc or "")
                where = j.get("location") if isinstance(j.get("location"), dict) else {}
                country = (where.get("countryName") or "").strip()
                if country and not re.search(r"united states|^usa?$|^u\.s", country, re.I):
                    continue                    # a remote job open only to people in another country
                if country and country.lower() not in loc.lower():
                    loc = ", ".join(x for x in (where.get("city"), where.get("subregion"), country) if x) or loc
                if str(j.get("workplace", "")).lower() == "remote" and "remote" not in loc.lower():
                    loc = f"{loc} (Remote)".strip()
                comp = j.get("company") or {}
                out.append(_job("workablejobs", comp.get("title"), j.get("id"), j.get("title"), loc, j.get("url", ""),
                                j.get("description", "") + "\n" + (j.get("requirementsSection") or ""), j.get("url", "")))
    return out


def arbeitnow(cfg, base="https://www.arbeitnow.com"):
    """arbeitnow.com public job-board API (no key). Each listing's url leads to the employer's own application page."""
    out = []
    for page in (1, 2, 3):
        try:
            data = _get(base + "/api/job-board-api", params={"page": page}).json()
        except Exception:
            break
        for j in data.get("data", []) or []:
            if not j.get("title"):
                continue
            loc = j.get("location") or ("Remote" if j.get("remote") else "")
            out.append(_job("arbeitnow", j.get("company_name"), j.get("slug"), j["title"], loc, j.get("url") or "", j.get("description", ""),
                            j.get("url", "")))
        if not (data.get("links") or {}).get("next"):
            break
    return out


def workingnomads(cfg, base="https://www.workingnomads.com"):
    """workingnomads.com public remote-jobs feed (no key). Every listing is remote."""
    out = []
    try:
        rows = _get(base + "/api/exposed_jobs/").json()
    except Exception:
        return out
    for j in rows if isinstance(rows, list) else []:
        if not j.get("title") or not j.get("url"):
            continue
        loc = j.get("location") or "Remote"
        out.append(_job("workingnomads", j.get("company_name"), j.get("url").rstrip("/").split("/")[-1], j["title"],
                        loc if re.search(r"remote|anywhere", loc, re.I) else "Remote, " + loc, j["url"], j.get("description", ""), j["url"]))
    return out


DEFAULT_CITIES = ("denver", "boulder", "colorado", "los angeles", "pasadena", "santa monica", "culver city", "burbank", "glendale", "long beach",
                  "irvine", "el segundo", "orange county", "southern california", "new york", "nyc", "brooklyn", "manhattan")
GITHUB_LISTS = [{"repo": "SimplifyJobs/New-Grad-Positions", "branch": "dev", "path": ".github/scripts/listings.json"}]


def githublists(cfg, base="https://raw.githubusercontent.com"):
    """Public GitHub repos that publish new-grad / entry-level postings as JSON (Simplify's format: active, is_visible, url, locations).
    Keeps open postings in Denver, Los Angeles, New York or remote (US) whose apply link goes straight to an application system the bot
    completes. Government employers are dropped later by prefilter, for every source."""
    a = (cfg.get("aggregators", {}) or {}).get("github_lists") or {}
    if a.get("enabled", True) is False:
        return []
    only_ats = a.get("only_supported_ats", True)
    cities = [c.lower() for c in (a.get("locations") or DEFAULT_CITIES)]
    out = []
    for r in a.get("repos") or GITHUB_LISTS:
        try:
            rows = _get(f"{base}/{r['repo']}/{r.get('branch', 'dev')}/{r.get('path', '.github/scripts/listings.json')}").json()
        except Exception:
            continue
        for j in rows if isinstance(rows, list) else []:
            url = (j.get("url") or "").strip()
            if not j.get("active") or j.get("is_visible") is False or not j.get("title") or not url.startswith("http"):
                continue
            if only_ats and not (board_of(url) or "myworkdayjobs.com" in url or "smartrecruiters.com" in url):
                continue
            locs = [str(x) for x in (j.get("locations") or [])]
            keep = None
            for loc in locs:
                low = norm_location(loc)
                if any(c in low for c in cities) or (_remote_ish(loc) and not foreign_place(loc)):
                    keep = loc
                    break
            if keep is None:
                continue
            company = j.get("company_name") or ""
            out.append(_job("githublist", company, j.get("id") or url, j["title"], "; ".join(locs), url,
                            f"{j['title']} at {company}. Entry level. {j.get('category', '')}.", url))
    return out


BUILTIN_SITES = {"colorado": "https://www.builtincolorado.com", "nyc": "https://www.builtinnyc.com", "la": "https://www.builtinla.com"}
_BUILTIN_LEVEL = re.compile(r"\|(Entry level|Junior|Mid level|Senior level|Expert/Leader|Internship)\|", re.I)


def builtin(cfg, base=None):
    """Built In's regional job boards (Colorado, NYC, LA): public listing pages, no login. Reads the plain page only; a page that
    fails or is blocked gives no jobs (nothing is done to get around a block). Keeps entry-level and junior cards. Each job's
    Apply link is Built In's own redirect to the employer's application page."""
    a = (cfg.get("aggregators", {}) or {}).get("builtin") or {}
    if a.get("enabled", True) is False:
        return []
    regions = a.get("regions") or list(BUILTIN_SITES)
    cats = a.get("categories") or ["operations", "project-management"]
    pages = int(a.get("pages", 2))
    out, seen = [], set()
    for region in regions:
        site = BUILTIN_SITES.get(region)
        if not site:
            continue
        for cat in cats:
            for pg in range(1, pages + 1):
                try:
                    page = _get(f"{site}/jobs/{cat}", params={"page": pg}).text
                except Exception:
                    break                                   # failing or blocked: stop for this board, no workaround
                got = 0
                for chunk in re.split(r'(?=<div id="job-card-\d+")', page)[1:]:
                    m = re.match(r'<div id="job-card-(\d+)"', chunk)
                    href = re.search(r'href="(/job/[^"]+/\d+)"', chunk)
                    title = re.search(r'data-id="job-card-title"[^>]*>(.*?)</a>', chunk, re.S)
                    comp = re.search(r'data-id="company-title"[^>]*><span>(.*?)</span>', chunk, re.S)
                    if not (m and href and title and comp) or m.group(1) in seen:
                        continue
                    got += 1
                    seen.add(m.group(1))
                    text = html_unescape(re.sub(r"\s+", " ", re.sub(r"(\|\s*)+", "|", re.sub(r"<[^>]+>", "|", re.sub(r"<script.*?</script>", "", chunk, flags=re.S)))))
                    lv = _BUILTIN_LEVEL.search(text)
                    if lv and lv.group(1).lower() not in ("entry level", "junior"):
                        continue
                    parts = [x.strip() for x in text.split("|") if x.strip()]
                    work = next((x for x in parts if re.fullmatch(r"(In-Office|Hybrid|Remote( or Hybrid)?)", x, re.I)), "")
                    where = next((x for x in parts[parts.index(work) + 1:] if work and not re.search(r"\d|Annually|level", x)), "") if work in parts else ""
                    loc = f"{where} ({work})".strip() if where else (work or region)
                    url = site + href.group(1)
                    out.append(_job("builtin", html_unescape(comp.group(1)), m.group(1), html_unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip(),
                                    loc, url + "?handler=ApplyRedirect", "", url))
                if not got:
                    break
    return out


def careerjet(cfg, base="https://search.api.careerjet.net"):
    """Careerjet's free job-search API (v4): Basic auth with CAREERJET_API_KEY from a free publisher account. Idle without the key.
    Their rules ask for the user's IP and user agent on every call; set aggregators.careerjet.user_ip, else this machine's address is used."""
    key = os.environ.get("CAREERJET_API_KEY")
    if not key:
        return []
    a = cfg.get("aggregators", {}) or {}
    c = a.get("careerjet") or {}
    if c.get("enabled", True) is False:
        return []
    import base64
    ip = c.get("user_ip")
    if not ip:
        try:
            ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
        except Exception:
            ip = "127.0.0.1"
    hdr = {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode(), **UA}
    qs = (a.get("queries") or [])[: int(c.get("max_queries", 6))]
    locs = (a.get("locations") or []) + ["Remote"]
    out, seen = [], set()
    for q in qs:
        for loc in locs:
            try:
                r = requests.get(base + "/v4/query", headers=hdr, timeout=TIMEOUT,
                                 params={"locale_code": "en_US", "keywords": q, "location": loc, "page_size": 50, "page": 1,
                                         "sort": "date", "user_ip": ip, "user_agent": UA["User-Agent"]})
                r.raise_for_status()
                data = r.json()
            except Exception:
                continue
            for j in data.get("jobs", []) if isinstance(data, dict) else []:
                url = j.get("url") or ""
                if not url or url in seen or not j.get("title"):
                    continue
                seen.add(url)
                out.append(_job("careerjet", j.get("company"), url.rstrip("/").split("/")[-1], j["title"], j.get("locations") or loc, url,
                                (j.get("description") or "") + (f"\nSalary: {j['salary']}" if j.get("salary") else ""), url))
    return out


def localfeed(cfg, base=None):
    """Jobs saved as JSON files in the repo's feeds/ folder: written by autoapply.exporter on a home computer, which can read
    LinkedIn's public no-login search where GitHub's servers are refused. Entries older than max_age_days (default 10) are dropped."""
    from datetime import date, datetime, timedelta
    a = (cfg.get("aggregators", {}) or {}).get("localfeed") or {}
    if a.get("enabled", True) is False:
        return []
    folder = Path(a.get("dir") or (Path(__file__).resolve().parent.parent / "feeds"))
    if not folder.is_dir():
        return []
    oldest = date.today() - timedelta(days=int(a.get("max_age_days", 10)))
    out = []
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        for j in data.get("jobs", []) if isinstance(data, dict) else []:
            if not (j.get("title") and j.get("apply_url")):
                continue
            try:
                if datetime.fromisoformat(str(j.get("posted") or "")[:10]).date() < oldest:
                    continue
            except Exception:
                pass
            out.append(_job("localfeed", j.get("company"), j.get("id") or j["apply_url"], j["title"], j.get("location"), j["apply_url"],
                            j.get("description", ""), j.get("url") or j["apply_url"]))
    return out


ALWAYS_ON = ("workablejobs", "arbeitnow", "workingnomads", "githublists", "builtin", "careerjet", "localfeed")      # keyless feeds that run whatever the config's own source list says
FETCHERS = {"linkedin": linkedin, "jooble": jooble, "remoteok": remoteok, "remotive": remotive, "jicy": jicy, "himalayas": himalayas, "wwr": wwr,
            "themuse": themuse, "adzuna": adzuna, "workablejobs": workablejobs, "arbeitnow": arbeitnow, "workingnomads": workingnomads, "githublists": githublists, "builtin": builtin, "careerjet": careerjet, "localfeed": localfeed}


# ---------------------------------------------------------------------------------------------- cache + entry point
def _to_dict(j: Job) -> dict:
    return dict(source=j.source, company=j.company, job_id=j.job_id, title=j.title, location=j.location, url=j.url,
                apply_url=j.apply_url, description=j.description, extra=j.extra)


def discover_aggregators(cfg: dict, base: Path, log=print) -> list[Job]:
    a = cfg.get("aggregators", {}) or {}
    if not a.get("enabled"):
        return []
    search = cfg.get("search", {}) or {}
    cache_dir = Path(base) / "logs" / "agg_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    urls = a.get("base_urls", {}) or {}
    cap = int(a.get("max_jobs_per_source", 400))
    jobs: list[Job] = []
    for name in list(dict.fromkeys(list(a.get("sources", list(FETCHERS))) + list(ALWAYS_ON))):
        fn = FETCHERS.get(name)
        if not fn:
            log(f"  ! unknown aggregator '{name}'")
            continue
        cache = cache_dir / f"{name}.json"
        ttl = float(a.get("ttl_hours", {}).get(name, TTL_HOURS.get(name, 2))) * 3600
        try:
            if cache.exists() and time.time() - cache.stat().st_mtime < ttl:
                found = [Job(**d) for d in json.loads(cache.read_text())]
                log(f"  agg/{name}: {len(found)} roles (cached)")
            else:
                raw = fn(cfg, urls[name]) if name in urls else fn(cfg)
                # keep only roles that could pass the title/location gate, so caches stay small
                found = [j for j in raw if j.apply_url and not prefilter(j, search)][:cap]
                cache.write_text(json.dumps([_to_dict(j) for j in found]))
                log(f"  agg/{name}: {len(raw)} fetched, {len(found)} pass the title/location filter")
        except Exception as e:
            log(f"  ! agg/{name}: {str(e)[:120]}")
            continue
        jobs.extend(found)
    try:
        jobs += _grow_boards(cfg, base, jobs, log)
    except Exception as e:
        log(f"  ! board discovery from aggregator listings failed: {str(e)[:100]}")
    return jobs


# ---------------------------------------------------------------------------------------------- growing the company list
_SUFFIX = {"inc", "llc", "corp", "corporation", "co", "company", "group", "holdings", "technologies", "technology", "labs", "ltd",
           "the", "and", "usa", "us", "services", "solutions", "systems", "international", "global"}
_PROBE_ATS = ("greenhouse", "lever", "workable", "smartrecruiters")
_PROBE_URL = {"greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
              "lever": "https://api.lever.co/v0/postings/{}?mode=json&limit=1",
              "workable": "https://apply.workable.com/api/v1/widget/accounts/{}",
              "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{}/postings?limit=1&country=us"}


def slug_variants(name: str) -> list[str]:
    words = [w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if w]
    core = [w for w in words if w not in _SUFFIX] or words
    out = []
    for cand in ("".join(words), "".join(core), "-".join(core), "-".join(words), core[0] if core and len(core) <= 2 else ""):
        if 2 < len(cand) <= 40 and cand not in out:
            out.append(cand)
    return out[:4]


def _tnorm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


# a listing whose apply link leads to the listing site's own sign-in, never to the employer's form (Built In: '?handler=ApplyRedirect'
# answers 302 to '?applyRequired=true', checked 2026-10-06): the bot makes no accounts there, so it goes to the employer directly
ACCOUNT_GATED = re.compile(r"builtin\w*\.com/job/.*[?&]handler=ApplyRedirect", re.I)


def account_gated(url: str) -> bool:
    return bool(ACCOUNT_GATED.search(url or ""))


def _same_company(name: str, display: str) -> bool:
    a, b = _tnorm(name).replace(" ", ""), _tnorm(display).replace(" ", "")
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a))))


def direct_apply_url(job, log=print, workday_boards=()) -> str | None:
    """When an aggregator listing has no usable link (JS-only pages, or a link behind the listing site's sign-in), find the same
    posting on the employer's own board: a Workday career site the bot already knows for that company (searched by the title),
    else the Workable / Greenhouse / Lever / Ashby / SmartRecruiters feeds looked up by the company's name."""
    from . import sources
    name = (job.extra or {}).get("company_name") or job.company
    want = _tnorm(job.title)
    loc = (job.location or "").lower().split(",")[0].split("(")[0].strip()

    def pick(found):
        same = [j for j in found if _tnorm(j.title) == want]
        if not same:                       # Workday often writes the place into the title: compare without it
            short = re.sub(r"\s+\b(" + re.escape(loc) + r")\b.*$", "", want) if loc else want
            same = [j for j in found if _tnorm(j.title).startswith(short) and len(short) >= 12
                    and (not loc or loc in (_tnorm(j.title) + " " + (j.location or "").lower()))]
        same.sort(key=lambda j: 0 if loc and loc in (j.location or "").lower() else 1)
        return same[0] if same else None

    for spec in workday_boards or ():
        parts = str(spec).split("/")
        if len(parts) < 3 or not any(_same_company(name, x) for x in (parts[3] if len(parts) > 3 else "", parts[0])):
            continue
        try:
            hit = pick(sources.workday_search(str(spec), job.title))
        except Exception:
            hit = None
        if hit:
            log(f"    found the employer's own posting on workday/{parts[0]}")
            return hit.apply_url
    tried = set()
    for slug in slug_variants(name) + [job.company]:
        for ats in ("workable", "greenhouse", "lever", "ashby", "smartrecruiters"):
            if (ats, slug) in tried or not slug or ats not in sources.FETCHERS:
                continue
            tried.add((ats, slug))
            try:
                hit = pick(sources.FETCHERS[ats](slug))
            except Exception:
                continue
            if hit:
                log(f"    found the employer's own posting on {ats}/{slug}")
                return hit.apply_url
    return None


def _probe(ats: str, token: str) -> bool:
    try:
        r = requests.get(_PROBE_URL[ats].format(token), headers=UA, timeout=10)
        if r.status_code != 200:
            return False
        d = r.json()
        if ats == "smartrecruiters":
            return bool(isinstance(d, dict) and d.get("totalFound"))
        return bool(d.get("jobs") if ats in ("greenhouse", "workable") and isinstance(d, dict) else d)
    except Exception:
        return False


def _grow_boards(cfg, base, jobs, log) -> list[Job]:
    """Boards found two ways: (1) an aggregator listing that already links to a supported application system, and (2) the company
    names on aggregator listings, tried against the Greenhouse / Lever / Workable feeds. New boards are fetched right away and
    remembered for the next run, so the company list keeps growing by itself."""
    from concurrent.futures import ThreadPoolExecutor
    a = cfg.get("aggregators", {}) or {}
    data = load_boards(base)
    known = {ats: set(map(str.lower, toks)) for ats, toks in data.items()}
    for ats, toks in (cfg.get("companies") or {}).items():
        known.setdefault(ats, set()).update(str(t).split("/")[0].lower() for t in toks or [])
    new: dict[str, list[str]] = {}
    for j in jobs:
        b = board_of(j.apply_url)
        if b and b[1].lower() not in known.get(b[0], set()):
            known.setdefault(b[0], set()).add(b[1].lower())
            new.setdefault(b[0], []).append(b[1])
    st_path = Path(base) / "boards_state.json"
    try:
        state = json.loads(st_path.read_text())
    except Exception:
        state = {}
    probed = state.setdefault("_probed", {})
    now = time.time()
    names = {}
    for j in jobs:
        nm = (j.extra or {}).get("company_name") or ""
        if nm and now - probed.get(nm.lower(), 0) > 30 * 86400:
            names.setdefault(nm.lower(), nm)
    todo = list(names.values())[: int(a.get("probe_companies_per_run", 80))]

    def try_company(nm):
        got = {}
        for ats in _PROBE_ATS:
            for tok in slug_variants(nm):
                if tok.lower() in known.get(ats, set()):
                    got = {}
                    break
                if _probe(ats, tok):
                    got[ats] = tok
                    break
        return nm, got

    with ThreadPoolExecutor(max_workers=12) as ex:
        for nm, got in ex.map(try_company, todo):
            probed[nm.lower()] = now
            for ats, tok in got.items():
                if tok.lower() not in known.get(ats, set()):
                    known.setdefault(ats, set()).add(tok.lower())
                    new.setdefault(ats, []).append(tok)
    try:
        st_path.write_text(json.dumps(state, indent=0, sort_keys=True))
    except Exception:
        pass
    if not new:
        return []
    for ats, toks in new.items():
        lst = data.setdefault(ats, [])
        lst += [t for t in toks if t not in lst]
    (Path(base) / "discovered_boards.json").write_text(json.dumps(data, indent=1))
    log(f"  found {sum(len(v) for v in new.values())} new company boards from aggregator listings: "
        + ", ".join(f"{a_}/{t}" for a_, ts in new.items() for t in ts[:8]))
    skip = {x.lower() for x in ((cfg.get("search") or {}).get("skip_sources") or ["ashby"])}
    return discover_boards({k: v for k, v in new.items() if k.lower() not in skip}, log, base=base)


# ---------------------------------------------------------------------------------------------- discovered boards
def load_boards(base: Path) -> dict:
    try:
        return json.loads((Path(base) / "discovered_boards.json").read_text())
    except Exception:
        return {}


def remember_board(base: Path, url: str):
    b = board_of(url)
    if not b:
        return
    data = load_boards(base)
    lst = data.setdefault(b[0], [])
    if b[1] not in lst:
        lst.append(b[1])
        (Path(base) / "discovered_boards.json").write_text(json.dumps(data, indent=1))


def canon_key(url: str):
    """The DB key an ATS-hosted posting would have (so the same job found twice is applied to once)."""
    for pat, ats in ((r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)(?:/jobs/|&token=)(\d+)", "greenhouse"),
                     (r"jobs\.lever\.co/([\w-]+)/([0-9a-f-]{36})", "lever"),
                     (r"jobs\.ashbyhq\.com/([\w.%-]+)/([0-9a-f-]{36})", "ashby"),
                     (r"apply\.workable\.com/([\w-]+)/j/(\w+)", "workable"),
                     (r"([\w-]+)\.bamboohr\.com/careers/(\d+)", "bamboohr"),
                     (r"jobs\.smartrecruiters\.com/([\w-]+)/(\d+)", "smartrecruiters"),
                     (r"([\w-]+)\.wd\d+\.myworkdayjobs\.com/[^?#]*/job/[^?#]*_([^/?#_]+)(?:[?#]|$)", "workday")):
        m = re.search(pat, url or "")
        if m:
            return f"{ats}:{m.group(1)}:{m.group(2)}"
    return None
