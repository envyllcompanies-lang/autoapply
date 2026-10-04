"""Job discovery from public ATS job-board APIs (no auth, no scraping of HTML)."""
from __future__ import annotations

import html
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

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
            description=_strip_html(j.get("content", "")),
            extra={"company_name": j["company_name"]} if j.get("company_name") else {}))
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


SEARCH: dict = {}          # set by main: the search settings (title/location gates, workday_queries, ...)
KNOWN: dict = {}           # set by main: job key -> status for jobs already in the database (their detail pages need no second look)
DETAIL_CAP = 80            # detail pages read per big board per run


def _needs_detail(job: "Job", gate: dict) -> bool:
    """Only postings that could still be wanted, and that the database has not already decided, get their detail page read."""
    st = KNOWN.get(job.key)
    if st and st not in ("queued", "new"):
        return False
    if SEARCH and prefilter(job, gate):
        return False
    keys = (SEARCH or {}).get("_title_keys")
    return not keys or any(k in job.title.lower() for k in keys)

WD_ROLES = ["operations", "coordinator", "analyst", "associate", "supply chain"]
WD_PLACES = ["Denver", "Los Angeles", "New York", "Remote"]
_MANY_LOCS = re.compile(r"^\s*(\d+|multiple|various)\s+locations?\s*$", re.I)


_WD_GATES: dict = {}
_WD_LOCK = threading.Lock()


def _wd_gate(url: str) -> threading.BoundedSemaphore:
    """Workday rate-limits per data centre (wd1, wd5, ...): at most 3 requests at a time to each one."""
    m = re.search(r"\.(wd\d+)\.myworkdayjobs\.com", url)
    key = m.group(1) if m else "wd"
    with _WD_LOCK:
        return _WD_GATES.setdefault(key, threading.BoundedSemaphore(3))


def _wd_request(method: str, url: str, **kw) -> requests.Response:
    """GET/POST with polite retries on 'Too Many Requests' (429) and temporary errors, honouring Retry-After."""
    kw.setdefault("timeout", TIMEOUT)
    r = None
    for attempt in range(4):
        with _wd_gate(url):
            r = (requests.post if method == "POST" else requests.get)(url, **kw)
        if r.status_code not in (429, 502, 503, 504) or attempt == 3:
            break
        try:
            wait = float((getattr(r, "headers", None) or {}).get("Retry-After") or 0)
        except (TypeError, ValueError):
            wait = 0
        time.sleep(min(wait or 3 * 2 ** attempt, 20) + random.uniform(0, 1.5))
    r.raise_for_status()
    return r


def _wd_post(api: str, offset: int, text: str):
    return _wd_request("POST", f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": text},
                       headers={**UA, "Content-Type": "application/json"}).json()


def workday(spec: str) -> list[Job]:
    """spec = 'tenant/wd5/SiteName' (optionally '/Display Name'). Uses Workday's public career-site JSON.

    A small career site is read in full. A big one (thousands of postings) is searched instead: role word x city word
    (search.workday_queries / workday_places), so the postings you could actually want are not buried under the rest."""
    parts = spec.split("/")
    tenant, wd, site = parts[0], parts[1], parts[2]
    display = parts[3] if len(parts) > 3 else ""
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    s = SEARCH or {}
    seen: set[str] = set()
    posts: list[dict] = []

    def take(data) -> int:
        n = 0
        for p in data.get("jobPostings") or []:
            path = p.get("externalPath", "")
            if path and path not in seen:
                seen.add(path)
                posts.append(p)
                n += 1
        return n

    first = _wd_post(api, 0, "")              # a failure here is the board's error; later failures only cost coverage
    take(first)
    total = int(first.get("total") or 0)      # Workday reports the total on the first page only
    full_pages = int(s.get("workday_full_pages", 10))
    try:
        if total <= 20 * full_pages:
            offset = 20
            while offset < total:
                if not take(_wd_post(api, offset, "")):
                    break
                offset += 20
        else:
            roles = s.get("workday_queries") or WD_ROLES
            places = s.get("workday_places") or WD_PLACES
            pages = int(s.get("workday_pages", 2))
            for role in roles:
                for place in places:
                    for page in range(pages):
                        data = _wd_post(api, page * 20, f"{role} {place}".strip())
                        take(data)
                        if len(data.get("jobPostings") or []) < 20:
                            break
    except requests.RequestException:
        pass

    out, details = [], 0
    for p in posts:
        path = p.get("externalPath", "")
        job = Job(source="workday", company=tenant, job_id=path.rsplit("_", 1)[-1] or path, title=p.get("title", ""),
                  location=p.get("locationsText", "") or "", url=f"{base}/{site}{path}", apply_url=f"{base}/{site}{path}",
                  description=p.get("title", ""), extra={"company_name": display} if display else {})
        many = bool(_MANY_LOCS.match(job.location))
        gate = {**s, "locations_include": [], "_onsite_ok": []} if many else s      # '2 Locations': the real cities are in the detail page
        if details < DETAIL_CAP and _needs_detail(job, gate):
            try:
                d = _wd_request("GET", f"{api}{path}", headers=UA).json().get("jobPostingInfo", {})
                job.description = _strip_html(d.get("jobDescription", "")) or job.description
                places = [x for x in [d.get("location")] + list(d.get("additionalLocations") or []) if x]
                if places and (many or not job.location):
                    job.location = "; ".join(dict.fromkeys(places))
                details += 1
            except Exception:
                pass
        out.append(job)
    return out


def _place(*parts) -> str:
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def workable(slug: str) -> list[Job]:
    """apply.workable.com public widget feed (no key)."""
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    data = r.json()
    cname = data.get("name") or ""
    for j in data.get("jobs", []) or []:
        code = j.get("shortcode") or j.get("code")
        if not code:
            continue
        loc = _place(j.get("city"), j.get("state"), j.get("country"))
        if j.get("telecommuting"):
            loc = f"{loc} (Remote)".strip()
        url = j.get("url") or f"https://apply.workable.com/j/{code}"
        out.append(Job(source="workable", company=slug, job_id=str(code), title=j.get("title", ""), location=loc, url=url,
                       apply_url=j.get("application_url") or url.rstrip("/") + "/apply",
                       description=_strip_html(j.get("description", "")), extra={"company_name": cname} if cname else {}))
    return out


def bamboohr(slug: str) -> list[Job]:
    """{slug}.bamboohr.com public careers list (no key)."""
    base = f"https://{slug}.bamboohr.com"
    r = requests.get(f"{base}/careers/list", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out, details = [], 0
    for j in (r.json().get("result") or []):
        jid = j.get("id")
        if jid is None:
            continue
        loc_d = j.get("location") or {}
        loc = _place(loc_d.get("city"), loc_d.get("state")) if isinstance(loc_d, dict) else str(loc_d)
        if j.get("isRemote"):
            loc = f"{loc} (Remote)".strip()
        job = Job(source="bamboohr", company=slug, job_id=str(jid), title=j.get("jobOpeningName", ""), location=loc,
                  url=f"{base}/careers/{jid}", apply_url=f"{base}/careers/{jid}", description=j.get("jobOpeningName", ""))
        if details < 40 and _needs_detail(job, SEARCH):
            try:
                d = requests.get(f"{base}/careers/{jid}/detail", headers=UA, timeout=TIMEOUT).json()
                info = (d.get("result") or {}).get("jobOpening") or {}
                job.description = _strip_html(info.get("description", "")) or job.description
                if info.get("compensation"):
                    job.description += f"\nSalary: {info['compensation']}"
                details += 1
            except Exception:
                pass
        out.append(job)
    return out


def recruitee(slug: str) -> list[Job]:
    """{slug}.recruitee.com public careers API (no key)."""
    r = requests.get(f"https://{slug}.recruitee.com/api/offers/", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for o in r.json().get("offers", []) or []:
        url = o.get("careers_url") or ""
        if not url:
            continue
        loc = o.get("location") or _place(o.get("city"), o.get("state_code"), o.get("country_code"))
        if o.get("remote"):
            loc = f"{loc} (Remote)".strip()
        out.append(Job(source="recruitee", company=slug, job_id=str(o.get("id") or o.get("slug")), title=o.get("title", ""),
                       location=loc, url=url, apply_url=o.get("careers_apply_url") or url.rstrip("/") + "/c/new",
                       description=_strip_html((o.get("description") or "") + "\n" + (o.get("requirements") or "")),
                       extra={"company_name": o["company_name"]} if o.get("company_name") else {}))
    return out


def breezy(slug: str) -> list[Job]:
    """{slug}.breezy.hr public JSON feed (no key)."""
    r = requests.get(f"https://{slug}.breezy.hr/json", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json() or []:
        if not isinstance(j, dict) or not j.get("url"):
            continue
        loc = j.get("location") or {}
        name = loc.get("name") if isinstance(loc, dict) else str(loc)
        if isinstance(loc, dict) and loc.get("is_remote"):
            name = f"{name or ''} (Remote)".strip()
        co = (j.get("company") or {}).get("name") if isinstance(j.get("company"), dict) else ""
        out.append(Job(source="breezy", company=slug, job_id=str(j.get("id") or j.get("friendly_id")), title=j.get("name", ""),
                       location=name or "", url=j["url"], apply_url=j["url"], description=_strip_html(j.get("description", "") or ""),
                       extra={"company_name": co} if co else {}))
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday, "workable": workable,
            "bamboohr": bamboohr, "recruitee": recruitee, "breezy": breezy}

STATE_FILE = "boards_state.json"
DEAD_AFTER = 2                 # consecutive 'no such board' answers before a board is left alone
NO_SUCH_BOARD = (404, 410, 422)
RETRY_DEAD_DAYS = 21


def dedupe_boards(source: str, names) -> list[str]:
    """Same board listed twice (any case; for Workday, with or without a display name) is fetched once."""
    keep: dict[str, str] = {}
    for n in names or []:
        n = str(n).strip()
        if not n:
            continue
        k = "/".join(n.split("/")[:3]).lower() if source == "workday" else n.lower()
        if k not in keep or (source == "workday" and n.count("/") > keep[k].count("/")):
            keep[k] = n
    return list(keep.values())


def _load_state(base) -> dict:
    try:
        return json.loads((Path(base) / STATE_FILE).read_text())
    except Exception:
        return {}


def discover(companies: dict[str, list[str]], log=print, base=None, workers: int = 16) -> list[Job]:
    """Fetch every company board in parallel. Boards that 404 twice in a row are skipped for a few weeks (boards_state.json)."""
    state = _load_state(base) if base else {}
    now = time.time()
    tasks = []
    for source, names in (companies or {}).items():
        if source not in FETCHERS:
            log(f"  ! unknown source '{source}', skipping")
            continue
        for name in dedupe_boards(source, names):
            st = (state.get(source) or {}).get(name) or {}
            if st.get("dead", 0) >= DEAD_AFTER and now - st.get("last", 0) < RETRY_DEAD_DAYS * 86400:
                continue
            tasks.append((source, name))

    def one(t):
        source, name = t
        for attempt in (1, 2):
            try:
                return t, FETCHERS[source](name), None
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code in NO_SUCH_BOARD or attempt == 2 or code in (400, 401, 403):
                    return t, None, e
            except Exception as e:  # timeouts etc.: one retry
                if attempt == 2:
                    return t, None, e
            time.sleep(1.5)
        return t, None, RuntimeError("failed")

    jobs: list[Job] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, tasks))
    per_ats: dict[str, list[int]] = {}
    missing: dict[str, list[str]] = {}
    errors: list[str] = []
    for (source, name), found, err in results:
        st = state.setdefault(source, {}).setdefault(name, {})
        if err is None:
            st.update(dead=0, last=now, roles=len(found))
            jobs.extend(found)
            per_ats.setdefault(source, []).append(len(found))
            if len(found) >= 100:
                log(f"  {source}/{name}: {len(found)} open roles")
        else:
            code = err.response.status_code if isinstance(err, requests.HTTPError) and err.response is not None else 0
            if code in NO_SUCH_BOARD:
                st.update(dead=st.get("dead", 0) + 1, last=now)
                missing.setdefault(source, []).append(name)
            else:
                errors.append(f"{source}/{name}: {str(err)[:90]}")
    for source, counts in per_ats.items():
        log(f"  {source}: {sum(1 for c in counts if c)} of {len(counts)} boards have openings ({sum(counts)} roles)")
    for source, names in missing.items():
        log(f"  {source}: {len(names)} board names not found (skipped from now on): {', '.join(names[:30])}{' …' if len(names) > 30 else ''}")
    for e in errors[:12]:
        log(f"  ! {e}")
    if len(errors) > 12:
        log(f"  ! …and {len(errors) - 12} more board errors")
    if base:
        try:
            (Path(base) / STATE_FILE).write_text(json.dumps(state, indent=0, sort_keys=True))
        except Exception:
            pass
    return jobs


# Every country except the United States, plus regions and big foreign cities. Used on locations and titles.
_COUNTRIES = (
    r"afghanistan|albania|algeria|andorra|angola|antigua|argentina|armenia|australia|austria|azerbaijan|bahamas|bahrain|bangladesh|"
    r"barbados|belarus|belgium|belize|benin|bhutan|bolivia|bosnia|botswana|brazil|brasil|brunei|bulgaria|burkina faso|burundi|"
    r"cabo verde|cape verde|cambodia|cameroon|canada|central african republic|chad|chile|china|colombia|comoros|congo|costa rica|"
    r"cote d.?ivoire|côte d.?ivoire|ivory coast|croatia|cuba|cyprus|czech republic|czechia|denmark|djibouti|dominica|"
    r"dominican republic|ecuador|egypt|el salvador|equatorial guinea|eritrea|estonia|eswatini|swaziland|ethiopia|fiji|finland|france|"
    r"gabon|gambia|germany|deutschland|ghana|greece|grenada|guatemala|guinea|guinea-bissau|guyana|haiti|honduras|hungary|iceland|india|"
    r"indonesia|iran|iraq|ireland|israel|italy|jamaica|japan|jordan|kazakhstan|kenya|kiribati|kosovo|kuwait|kyrgyzstan|laos|latvia|"
    r"lebanon|lesotho|liberia|libya|liechtenstein|lithuania|luxembourg|madagascar|malawi|malaysia|maldives|mali|malta|marshall islands|"
    r"mauritania|mauritius|mexico|méxico|micronesia|moldova|monaco|mongolia|montenegro|morocco|mozambique|myanmar|namibia|nauru|nepal|"
    r"netherlands|holland|new zealand|nicaragua|niger|nigeria|north korea|north macedonia|macedonia|norway|oman|pakistan|"
    r"palau|palestine|panama|papua new guinea|paraguay|peru|philippines|poland|portugal|qatar|romania|russia|rwanda|saint kitts|"
    r"saint lucia|saint vincent|san marino|sao tome|são tomé|saudi arabia|saudi|senegal|serbia|seychelles|sierra leone|"
    r"singapore|slovakia|slovenia|solomon islands|somalia|south africa|south korea|korea|south sudan|spain|españa|sri lanka|sudan|"
    r"suriname|sweden|switzerland|syria|taiwan|tajikistan|tanzania|thailand|timor-leste|east timor|togo|tonga|trinidad|tobago|tunisia|"
    r"turkey|türkiye|turkiye|turkmenistan|tuvalu|uganda|ukraine|united arab emirates|uae|u\.a\.e|united kingdom|uk|u\.k|england|"
    r"scotland|wales|northern ireland|great britain|britain|uruguay|uzbekistan|vanuatu|vatican|venezuela|vietnam|viet nam|yemen|zambia|"
    r"zimbabwe|hong kong|macau|macao|greenland|bermuda|cayman islands|curacao|curaçao|aruba|bonaire|anguilla|gibraltar|isle of man"
)
_REGIONS = (r"emea|apac|apj|latam|latin america|south america|central america|caribbean|europe|european union|eu|asia|asia pacific|africa|"
            r"middle east|mena|gcc|oceania|nordics|nordic|dach|benelux|anz|balkans|scandinavia")
_FOREIGN_CITIES = (
    r"toronto|vancouver|montreal|montréal|ottawa|calgary|edmonton|winnipeg|mississauga|quebec|"
    r"london|paris|berlin|munich|münchen|hamburg|frankfurt|amsterdam|dublin|madrid|barcelona|lisbon|rome|milan|zurich|zürich|geneva|"
    r"vienna|prague|warsaw|krakow|kraków|wroclaw|budapest|bucharest|sofia|athens|istanbul|stockholm|oslo|copenhagen|helsinki|brussels|"
    r"tel aviv|dubai|abu dhabi|doha|riyadh|jeddah|cairo|lagos|nairobi|johannesburg|cape town|mumbai|bangalore|bengaluru|hyderabad|pune|"
    r"chennai|delhi|new delhi|gurgaon|gurugram|noida|kolkata|karachi|lahore|islamabad|dhaka|colombo|kathmandu|kuala lumpur|jakarta|manila|"
    r"cebu|bangkok|ho chi minh|hanoi|shanghai|beijing|shenzhen|guangzhou|taipei|seoul|tokyo|osaka|sydney|melbourne|brisbane|perth|"
    r"auckland|wellington|mexico city|guadalajara|monterrey|bogota|bogotá|medellin|medellín|lima|santiago|buenos aires|"
    r"sao paulo|são paulo|rio de janeiro|montevideo|quito|caracas|kyiv|kiev|lviv|minsk|moscow|tbilisi|yerevan|baku|almaty|tashkent|"
    r"belgrade|zagreb|ljubljana|bratislava|vilnius|riga|tallinn|reykjavik|valletta|nicosia"
)
FOREIGN = re.compile(rf"(?<![a-z])({_COUNTRIES}|{_REGIONS}|{_FOREIGN_CITIES}|jp|kr)(?![a-z])", re.I)

_US_STATES = (
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|"
    r"kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    r"new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
    r"south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|district of columbia|"
    r"puerto rico|guam|american samoa|virgin islands|northern mariana|new england|pacific northwest|midwest|east coast|west coast|"
    r"denver|boulder|aspen|los angeles|nyc|brooklyn|manhattan|queens|bronx|chicago|seattle|san francisco|boston|atlanta|dallas|houston|"
    r"austin|phoenix|philadelphia|miami|salt lake city|minneapolis|detroit|nashville|charlotte|raleigh|pittsburgh|baltimore|jersey city"
)
_US_WORDS = re.compile(rf"united states|(?<![a-z])usa(?![a-z])|(?<![a-z.])u\.s\.?(a\.?)?(?![a-z])|(?<![a-z])us(?![a-z])|north america|"
                       rf"(?<![a-z])americas(?![a-z])|nationwide|(?<![a-z])({_US_STATES})(?![a-z])", re.I)
_US_ABBR = re.compile(r",\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|"
                      r"pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc|pr)(?![a-z])", re.I)
_CA_PROVINCE_ABBR = re.compile(r",\s*(on|bc|ab|qc|mb|sk|ns|nb|nl|pe|pei|yt|nt|nu)(?![a-z])", re.I)
REMOTE_RX = re.compile(r"remote|telecommut|work from home|(?<![a-z])wfh(?![a-z])|anywhere|virtual|distributed|home[- ]based|nationwide", re.I)


def us_place(text: str) -> bool:
    """True when the text names the United States, a US state or territory, or a US city ('Denver, CO', 'Remote - US')."""
    low = (text or "").lower()
    if _US_WORDS.search(low):
        return True
    for m in _US_ABBR.finditer(low):
        if m.group(1).lower() == "ca" and (_CA_PROVINCE_ABBR.search(low) or "canada" in low):
            continue                                   # 'Toronto, ON, CA' is Canada
        return True
    return False


def foreign_place(text: str) -> bool:
    """True when the text is tied to another country and not also to the US ('Belize (Remote)', 'Remote - EMEA')."""
    return bool(FOREIGN.search(text or "")) and not us_place(text)


def _remote_ish(loc: str) -> bool:
    low = (loc or "").lower().strip()
    return bool(REMOTE_RX.search(low)) or bool(re.fullmatch(r"(united states( of america)?|usa|u\.s\.a?\.?|us)(\s*\(.*\))?", low))


LICENSE_RX = re.compile(r"\(p\.?e\.?\)|(?<![\w.])p\.e\.|professional engineer|(?<![\w-])pe (license|licensed|required|stamp)|(?<![\w-])licensed(?![\w-])|"
                        r"(?<![\w-])(cpa|rn|lpn)(?![\w-])|attorney|(?<![\w-])counsel(?![\w-])|physician|dynamics 365|systems? administrator|sysadmin|"
                        r"erp analyst|devops|(?<![\w-])sdlc(?![\w-])|(?<![\w-])ai engineer|machine learning|data engineer|structural engineer|"
                        r"(?<![\w-])programmer(?![\w-])|salesforce (admin|administrator|developer)|netsuite (admin|administrator|developer)|"
                        r"workday (business )?analyst|workday consultant|sap (consultant|analyst)|hris analyst", re.I)

TECH_RX = re.compile(
    r"\b(software|firmware|asic|silicon|devops|sre|site reliability|machine learning|ml engineer|data scientist|data engineer|backend|"
    r"back-end|frontend|front-end|full[- ]?stack|platform engineer|security engineer|network|cloud|kernel|hardware|rf engineer|"
    r"mobile|ios|android|research (scientist|engineer)|architect|technical program manager|tpm|counsel|attorney|physician|nurse|clinical|"
    r"therapist|pharmac\w*|dentist|driver|cdl|welder|electrician|mechanic|barista|cook|chef|cashier|teacher|professor|"
    r"safeguards|enforcement analyst|fraud investigator|security operations|soc analyst|cyber\w*|infosec|information security|penetration)\b", re.I)
SENIOR_RX = re.compile(r"\b(iii|iv|v)\s*$|^\s*lead\b|\b(sr|snr)\b\.?|associate director|regional (manager|director)|\bvp\b", re.I)
NOT_A_JOB_RX = re.compile(r"talent (community|pool|network)|general application|future opportunit|join (our|the) (team|talent)|expression of interest|"
                          r"20(26|27|28)\s*(start|class|graduate|summer|intern|program)|summer analyst|graduate (program|scheme)|new grad program", re.I)


_STATE_ABBR = {"co": "colorado", "ny": "new york", "ca": "california"}
_CANADIAN_PROVINCE = re.compile(r"(?<![a-z])(on|bc|ab|qc|mb|sk|ns|nb|nl|pe)(?![a-z])")
_ALT_SENIOR = re.compile(r"\s*(?:/|\bor\b)\s*(?:senior|sr\.?)\s+[\w&-]+", re.I)


def norm_location(loc: str) -> str:
    """Lower-cased location text with 'CO', 'NY' and 'CA' also read as the state names, so 'Westminster, CO' matches 'colorado'."""
    low = (loc or "").lower()
    extra = []
    for tok in re.findall(r"(?<![a-z-])(co|ny|ca)(?![a-z-])", low):
        if tok == "ca" and _CANADIAN_PROVINCE.search(low):
            continue                                            # 'Toronto, ON, CA' is Canada
        extra.append(_STATE_ABBR[tok])
    return low + (" " + " ".join(extra) if extra else "")


def level_title(title: str) -> str:
    """'Operations Associate / Senior Associate' is a multi-level posting that includes the junior role: judge the junior part."""
    return _ALT_SENIOR.sub("", title or "")


def prefilter(job: Job, search: dict) -> str | None:
    """Cheap keyword gate before spending tokens. Returns a rejection reason or None."""
    title = level_title(job.title)
    t = title.lower()
    loc = norm_location(job.location)
    inc = [s.lower() for s in search.get("titles_include", [])]
    exc = [s.lower() for s in search.get("titles_exclude", [])]
    locs = [s.lower() for s in search.get("locations_include", [])]
    if inc and not any(k in t for k in inc):
        return "title not in include list"
    if any(k in t for k in exc):
        return "title matched exclude list"
    if search.get("smart_title_filter", True):
        if TECH_RX.search(t) or LICENSE_RX.search(title):
            return "technical, licensed or specialist role outside your background"
        if SENIOR_RX.search(title):
            return "senior-level title"
        if NOT_A_JOB_RX.search(t):
            return "talent-pool posting or student program, not an open role"
        if foreign_place(title):
            return "role is based in another country"
    if locs and not any(k in loc for k in locs):
        return f"location '{job.location}' not allowed"
    if search.get("us_remote_only", True) and foreign_place(job.location):
        return "role is tied to another country"
    # On-site / hybrid roles only in the places you said you'd live (facts.relocation_ok_locations, passed in by main);
    # remote and US-wide roles are always fine.
    onsite_ok = [str(x).lower() for x in (search.get("_onsite_ok") or [])]
    if onsite_ok and "*" not in onsite_ok and loc.strip() and not _remote_ish(loc):
        if not any(re.search(r"(?<![a-z])" + re.escape(x) + r"(?![a-z])", loc) for x in onsite_ok):
            return f"on-site in '{job.location[:60]}', outside the cities you'd move to"
    return None
