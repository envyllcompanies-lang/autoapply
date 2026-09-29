"""Job discovery from public ATS job-board APIs (no auth, no scraping of HTML)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import requests

TIMEOUT = 20
UA = {"User-Agent": "autoapply/1.0 (personal job search)"}


@dataclass
class Job:
    source: str          # greenhouse | lever | ashby
    company: str
    job_id: str
    title: str
    location: str
    url: str             # public posting page
    apply_url: str       # page that holds the application form
    description: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.job_id}"


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def greenhouse(token: str) -> list[Job]:
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        params={"content": "true"}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        url = j.get("absolute_url") or f"https://job-boards.greenhouse.io/{token}/jobs/{j['id']}"
        out.append(Job(
            source="greenhouse", company=token, job_id=str(j["id"]),
            title=j.get("title", ""), location=(j.get("location") or {}).get("name", ""),
            url=url, apply_url=f"https://job-boards.greenhouse.io/{token}/jobs/{j['id']}",
            description=_strip_html(j.get("content", ""))))
    return out


def lever(company: str) -> list[Job]:
    r = requests.get(f"https://api.lever.co/v0/postings/{company}",
                     params={"mode": "json"}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        desc = j.get("descriptionPlain") or _strip_html(j.get("description", ""))
        for lst in j.get("lists", []):
            desc += f"\n\n{lst.get('text','')}\n{_strip_html(lst.get('content',''))}"
        desc += "\n\n" + (j.get("additionalPlain") or "")
        out.append(Job(
            source="lever", company=company, job_id=j["id"], title=j.get("text", ""),
            location=cats.get("location", "") or ", ".join(cats.get("allLocations", []) or []),
            url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl") or j.get("hostedUrl", "") + "/apply",
            description=desc.strip(), extra={"workplace": j.get("workplaceType")}))
    return out


def ashby(org: str) -> list[Job]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}",
                     params={"includeCompensation": "true"}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "")
        if j.get("isRemote"):
            loc = f"{loc} (Remote)".strip()
        job_url = j.get("jobUrl", "")
        out.append(Job(
            source="ashby", company=org, job_id=j["id"], title=j.get("title", ""),
            location=loc, url=job_url,
            apply_url=j.get("applyUrl") or (job_url.rstrip("/") + "/application"),
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
            extra={"comp": (j.get("compensation") or {}).get("compensationTierSummary")}))
    return out


SEARCH: dict = {}          # set by main: lets the Workday fetcher skip descriptions for jobs that fail the title/location gate


def workday(spec: str) -> list[Job]:
    """spec = 'tenant/wd5/SiteName' (optionally '/Display Name'). Uses Workday's public career-site JSON."""
    parts = spec.split("/")
    tenant, wd, site = parts[0], parts[1], parts[2]
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    out, offset, details = [], 0, 0
    for _ in range(12):
        r = requests.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                          headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        posts = data.get("jobPostings") or []
        if not posts:
            break
        for p in posts:
            path = p.get("externalPath", "")
            if not path:
                continue
            job = Job(source="workday", company=tenant, job_id=path.rsplit("_", 1)[-1] or path, title=p.get("title", ""),
                      location=p.get("locationsText", ""), url=f"{base}/{site}{path}", apply_url=f"{base}/{site}{path}",
                      description=p.get("title", ""))
            if details < 60 and not (SEARCH and prefilter(job, SEARCH)):
                try:
                    d = requests.get(f"{api}{path}", headers=UA, timeout=TIMEOUT).json().get("jobPostingInfo", {})
                    job.description = _strip_html(d.get("jobDescription", "")) or job.description
                    if d.get("location") and "location" not in job.location.lower():
                        job.location = d["location"] if job.location.lower().startswith(("2 loc", "3 loc", "multiple")) else job.location
                    details += 1
                except Exception:
                    pass
            out.append(job)
        offset += 20
        if offset >= int(data.get("total", 0) or 0):
            break
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday}


def discover(companies: dict[str, list[str]], log=print) -> list[Job]:
    jobs: list[Job] = []
    for source, names in (companies or {}).items():
        fetch = FETCHERS.get(source)
        if not fetch:
            log(f"  ! unknown source '{source}', skipping")
            continue
        for name in names or []:
            try:
                found = fetch(name)
                log(f"  {source}/{name}: {len(found)} open roles")
                jobs.extend(found)
            except Exception as e:  # one bad board shouldn't kill the run
                log(f"  ! {source}/{name}: {e}")
    return jobs


FOREIGN = re.compile(
    r"\b(india|korea|japan|china|singapore|australia|new zealand|canada|toronto|mexico|brazil|latam|latin america|emea|apac|europe|"
    r"germany|france|paris|london|uk|united kingdom|ireland|dublin|spain|italy|netherlands|poland|israel|dubai|uae|philippines|"
    r"indonesia|vietnam|taiwan|hong kong|argentina|colombia|chile|sweden|switzerland|denmark|portugal|romania)\b", re.I)


def prefilter(job: Job, search: dict) -> str | None:
    """Cheap keyword gate before spending tokens. Returns a rejection reason or None."""
    t = job.title.lower()
    loc = job.location.lower()
    inc = [s.lower() for s in search.get("titles_include", [])]
    exc = [s.lower() for s in search.get("titles_exclude", [])]
    locs = [s.lower() for s in search.get("locations_include", [])]
    if inc and not any(k in t for k in inc):
        return "title not in include list"
    if any(k in t for k in exc):
        return "title matched exclude list"
    if locs and not any(k in loc for k in locs):
        return f"location '{job.location}' not allowed"
    if search.get("us_remote_only", True):
        # "remote" postings tied to another country need work authorization there
        if FOREIGN.search(t + " " + loc) and not re.search(r"united states|\bus\b|u\.s\.|usa|denver|new york|california|colorado|los angeles", loc):
            return "remote role tied to another country"
    return None
