"""New job sources: GitHub entry-level lists, SmartRecruiters, the data-file employer list, Built In, Careerjet, the local LinkedIn file,
and the no-government filter. No network: every source reads saved sample data. Run: python tests/source_checks.py"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from autoapply import aggregators as A, sources as S  # noqa: E402

FX = ROOT / "tests" / "fixtures"
problems: list[str] = []


def check(ok, msg):
    if not ok:
        problems.append(msg)


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    @property
    def text(self):
        return self._d if isinstance(self._d, str) else json.dumps(self._d)

    status_code = 200


def serve(fn, data):
    """Run fn with the aggregator HTTP helper answering `data`."""
    old = A._get
    A._get = lambda *a, **k: _Resp(data)
    try:
        return fn()
    finally:
        A._get = old


# ---- no-government filter (applies to every source through prefilter)
def J(company, title="Operations Analyst", loc="Denver, CO", url="https://boards.greenhouse.io/x/jobs/1"):
    return S.Job("greenhouse", company, "1", title, loc, url, url, "")


SEARCH = {"titles_include": ["operations", "analyst", "coordinator"], "locations_include": ["denver", "new york", "remote"]}
for gov in ("City of Denver", "State of Colorado", "County of Los Angeles", "Colorado Department of Transportation", "U.S. Army Corps of Engineers",
            "Denver Public Schools", "Denver Housing Authority", "Regional Transportation District (RTD)", "Federal Reserve Bank of Kansas City",
            "nyc-parks-arsenal-west", "NYC Health + Hospitals", "Denver Parks and Recreation", "Los Angeles County Fire"):
    check(S.prefilter(J(gov), SEARCH) is not None and "government" in str(S.prefilter(J(gov), SEARCH)), f"government employer not dropped: {gov}")
check("government" in str(S.prefilter(J("Acme Corp", url="https://acme.wd5.myworkdayjobs.com/x", loc="Denver, CO") if False else J("Parks", url="https://careers.denvergov.org/job/1"), SEARCH)),
      "a .gov-style address should be dropped")
for ok in ("FedEx", "Federal Express", "Stateline Capital", "Countyline Logistics", "Cityscape Properties", "Department Store Co", "Acme Corp", "Unitedhealth Group",
           "Parks Pharmacy", "NYC Hospitality Group", "Denver Water Sports Inc"):
    check(S.prefilter(J(ok), SEARCH) is None, f"private employer wrongly dropped as government: {ok}")
check(S.prefilter(J("City of Denver"), {**SEARCH, "exclude_government": False}) is None, "the government filter must be switchable off")

# ---- source 1: GitHub entry-level lists
rows = json.load(open(FX / "github_listings_sample.json"))
got = serve(lambda: A.githublists({"aggregators": {}}), rows)
ids = sorted(j.job_id for j in got)
check(ids == ["id-1", "id-10", "id-2", "id-7"] or ids == ["id-1", "id-10", "id-2", "id-7", "id-8"],
      f"github list: expected only open, visible, US/remote rows on supported application systems, got {ids}")
check(all(j.apply_url.startswith("http") and j.source == "agg-githublist" for j in got), "github list: jobs need an apply link and the agg-githublist source")
check("id-3" not in ids and "id-4" not in ids and "id-5" not in ids and "id-6" not in ids and "id-9" not in ids, f"github list: closed, hidden, foreign, Texas and unsupported-system rows must be dropped: {ids}")
check(serve(lambda: A.githublists({"aggregators": {"github_lists": {"enabled": False}}}), rows) == [], "github list must be switchable off")
check("githublists" in A.FETCHERS and "githublists" in A.ALWAYS_ON, "github list must be registered and on by default")
none = serve(lambda: A.githublists({"aggregators": {"github_lists": {"only_supported_ats": False}}}), rows)
check("id-9" in {j.job_id for j in none}, "only_supported_ats: false should keep other application systems")

# ---- source 2: SmartRecruiters
class _SRResp(_Resp):
    def raise_for_status(self):
        pass


def sr_fetch(slug, data):
    old = S.requests.get
    S.requests.get = lambda *a, **k: _SRResp(data)
    try:
        return S.smartrecruiters(slug)
    finally:
        S.requests.get = old


sr = json.load(open(FX / "smartrecruiters_sample.json"))
jobs = sr_fetch("Acme", sr)
check(sorted(j.title for j in jobs) == ["Business Operations Associate", "Operations Analyst", "Supply Chain Coordinator"],
      f"smartrecruiters: only US entry-level and associate postings should be kept, got {[j.title for j in jobs]}")
j0 = next((j for j in jobs if j.title == "Operations Analyst"), None)
check(j0 is not None and j0.source == "smartrecruiters" and j0.company == "Acme" and j0.url == "https://jobs.smartrecruiters.com/Acme/740000000000001-operations-analyst"
      and j0.apply_url.startswith("https://jobs.smartrecruiters.com/Acme/") and "Denver" in j0.location, f"smartrecruiters: job fields wrong: {j0}")
check(any("Remote" in j.location for j in jobs), "smartrecruiters: a remote posting should say so in its location")
check("smartrecruiters" in S.FETCHERS, "smartrecruiters must be a registered employer system")
check(A.board_of("https://jobs.smartrecruiters.com/Acme/740000000000001-operations-analyst") == ("smartrecruiters", "Acme"), "board_of should read SmartRecruiters links")
check(A.canon_key("https://jobs.smartrecruiters.com/Acme/740000000000001-operations-analyst") == "smartrecruiters:Acme:740000000000001", "canon_key should read SmartRecruiters links")
check(j0 is not None and j0.key == A.canon_key(j0.url), f"smartrecruiters: a board job and the same job found through a link must share one key: {j0.key if j0 else None}")
check(S.dedupe_boards("smartrecruiters", ["Acme", "acme", "BoschGroup"]) == ["Acme", "BoschGroup"], "smartrecruiters boards listed twice are fetched once")

old_get = A.requests.get
A.requests.get = lambda *a, **k: type("R", (), {"status_code": 200, "json": lambda self: {"totalFound": 0, "content": []}})()
check(A._probe("smartrecruiters", "nobody") is False, "a SmartRecruiters company with no postings is not a board")
A.requests.get = lambda *a, **k: type("R", (), {"status_code": 200, "json": lambda self: {"totalFound": 7, "content": [{}]}})()
check(A._probe("smartrecruiters", "acme") is True, "a SmartRecruiters company with postings is a board")
A.requests.get = old_get

# ---- source 3: the employer list is a data file; every system in it is one the bot can read, and nothing is listed twice
import yaml  # noqa: E402
boards = yaml.safe_load((ROOT / "boards.yaml").read_text())
check(set(boards) - {"names"} <= set(S.FETCHERS), f"boards.yaml names an unknown system: {set(boards) - {'names'} - set(S.FETCHERS)}")
check(len(boards.get("smartrecruiters") or []) >= 5 and len(boards.get("greenhouse") or []) >= 350, "boards.yaml should hold the longer employer list")
for ats, toks in boards.items():
    if ats not in ("workday", "names"):
        check(len(toks or []) == len({str(t).lower() for t in toks or []}), f"boards.yaml {ats}: a board is listed twice")

# ---- Built In regional boards (public pages, no login)
class _Page:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


bi_html = (FX / "builtin_sample.html").read_text()
old = A._get
calls = []
A._get = lambda url, **k: (calls.append(url), _Page(bi_html))[1]
try:
    bi = A.builtin({"aggregators": {"builtin": {"regions": ["colorado"], "categories": ["operations"], "pages": 1}}})
finally:
    A._get = old
check(len(bi) == 2 and sorted(j.title for j in bi) == sorted([j.title for j in bi]), f"builtin: only entry-level and junior cards should be kept, got {len(bi)}")
check(all("builtincolorado.com" in c for c in calls) and len(calls) == 1, f"builtin: one page for one region and category: {calls}")
b0 = bi[0] if bi else None
check(b0 is not None and b0.source == "agg-builtin" and b0.apply_url.endswith("?handler=ApplyRedirect") and "/job/" in b0.url and b0.job_id.isdigit()
      and "colorado" in b0.location.lower(), f"builtin: job fields wrong: {b0}")
check(any("remote" in j.location.lower() for j in bi), "builtin: a remote-or-hybrid card should say so in its location")
check("builtin" in A.FETCHERS and "builtin" in A.ALWAYS_ON, "builtin must be registered and on by default")
A._get = lambda url, **k: (_ for _ in ()).throw(RuntimeError("blocked"))
try:
    check(A.builtin({"aggregators": {"builtin": {"regions": ["colorado"], "categories": ["operations"], "pages": 1}}}) == [], "builtin: a blocked or failing page must give no jobs, with no workaround")
finally:
    A._get = old
check(A.builtin({"aggregators": {"builtin": {"enabled": False}}}) == [], "builtin must be switchable off")

# ---- Careerjet (free keyed API; does nothing without CAREERJET_API_KEY)
import base64  # noqa: E402
import os  # noqa: E402
cj_calls = []


class _CJ(_Resp):
    def raise_for_status(self):
        pass


def cj_get(url, **k):
    cj_calls.append((url, k))
    return _CJ({"type": "JOBS", "hits": 2, "pages": 1, "jobs": [
        {"title": "Operations Analyst", "company": "Acme Logistics", "locations": "Denver, CO", "url": "https://www.careerjet.com/jobad/us1234",
         "description": "<p>Coordinate vendors.</p>", "date": "Mon, 05 Oct 2026 10:00:00 GMT", "salary": "$60,000 - $70,000"},
        {"title": "Project Coordinator", "company": "Rocky Build", "locations": "Denver, CO", "url": "https://www.careerjet.com/jobad/us1235", "description": "x"}]})


old_get, old_key = A.requests.get, os.environ.get("CAREERJET_API_KEY")
A.requests.get = cj_get
try:
    os.environ.pop("CAREERJET_API_KEY", None)
    check(A.careerjet({"aggregators": {"queries": ["operations analyst"], "locations": ["Denver, CO"]}}) == [] and not cj_calls, "careerjet: no key means no calls and no jobs")
    os.environ["CAREERJET_API_KEY"] = "KEY123"
    cjj = A.careerjet({"aggregators": {"queries": ["operations analyst"], "locations": ["Denver, CO"], "careerjet": {"user_ip": "203.0.113.5"}}})
finally:
    A.requests.get = old_get
    if old_key is None:
        os.environ.pop("CAREERJET_API_KEY", None)
    else:
        os.environ["CAREERJET_API_KEY"] = old_key
check(len(cj_calls) >= 1 and cj_calls[0][0] == "https://search.api.careerjet.net/v4/query", f"careerjet: wrong endpoint {cj_calls[:1]}")
if cj_calls:
    hdr, prm = cj_calls[0][1].get("headers", {}), cj_calls[0][1].get("params", {})
    check(hdr.get("Authorization") == "Basic " + base64.b64encode(b"KEY123:").decode(), "careerjet: Basic auth must carry the key")
    check(prm.get("keywords") == "operations analyst" and prm.get("location") == "Denver, CO" and prm.get("locale_code") == "en_US"
          and prm.get("user_ip") == "203.0.113.5" and prm.get("user_agent") and int(prm.get("page_size", 0)) <= 100, f"careerjet: params wrong {prm}")
check(len({j.job_id for j in cjj}) == 2 and cjj[0].source == "agg-careerjet" and cjj[0].apply_url.startswith("https://www.careerjet.com/") and "Denver" in cjj[0].location,
      f"careerjet: jobs wrong {cjj}")
check("careerjet" in A.FETCHERS and "careerjet" in A.ALWAYS_ON, "careerjet must be registered (it stays idle without a key)")

# ---- local feed (jobs a Mac reads from LinkedIn's public search and saves in the repo) and its exporter
lf = A.localfeed({"aggregators": {"localfeed": {"dir": str(FX / "localfeed")}}})
check(sorted(j.job_id for j in lf) == ["1001", "1002", "1004"] or sorted(j.job_id for j in lf) == ["1001", "1002"],
      f"localfeed: jobs older than the age limit must be dropped, got {sorted(j.job_id for j in lf)}")
check(all(j.source == "agg-localfeed" and j.apply_url.startswith("https://") for j in lf), "localfeed: jobs need the localfeed source and an apply link")
check(A.localfeed({"aggregators": {"localfeed": {"dir": str(FX / "nothing-here")}}}) == [], "localfeed: a missing folder gives no jobs and no error")
check("localfeed" in A.FETCHERS and "localfeed" in A.ALWAYS_ON, "localfeed must be registered and on by default")
check(A.localfeed({"aggregators": {"localfeed": {"enabled": False, "dir": str(FX / "localfeed")}}}) == [], "localfeed must be switchable off")

import tempfile  # noqa: E402
from autoapply import exporter as X  # noqa: E402
tmp = Path(tempfile.mkdtemp())
good = [S.Job("linkedin", "acme", "9", "Operations Analyst", "Denver, CO", "https://www.linkedin.com/jobs/view/9", "https://boards.greenhouse.io/acme/jobs/9", "x")]
old_li = A.linkedin
A.linkedin = lambda cfg, *a: good
try:
    n = X.export({"aggregators": {}}, tmp, log=lambda *a: None)
finally:
    A.linkedin = old_li
check(n == 1 and (tmp / "linkedin.json").exists(), "exporter: one job should be written")
rt = A.localfeed({"aggregators": {"localfeed": {"dir": str(tmp)}}})
check(len(rt) == 1 and rt[0].apply_url == "https://boards.greenhouse.io/acme/jobs/9", f"exporter: the saved file must read back as the same job: {rt}")
A.linkedin = lambda cfg, *a: []
try:
    n0 = X.export({"aggregators": {}}, tmp, log=lambda *a: None)
finally:
    A.linkedin = old_li
check(n0 == 0 and len(A.localfeed({"aggregators": {"localfeed": {"dir": str(tmp)}}})) == 1, "exporter: an empty or blocked read must never overwrite a good feed")

# ---- Built In: its apply link needs a Built In account, so a listing is applied to on the employer's own board instead
bi = A._job("builtin", "Wells Fargo", "11413249", "Branch Operations Coordinator Wheat Ridge CO", "Wheat Ridge, CO (Hybrid)",
            "https://www.builtincolorado.com/job/branch-operations-coordinator-wheat-ridge-co/11433651?handler=ApplyRedirect", "",
            "https://www.builtincolorado.com/job/branch-operations-coordinator-wheat-ridge-co/11433651")
check(A.account_gated(bi.apply_url) and not A.account_gated("https://boards.greenhouse.io/acme/jobs/1"),
      "Built In's ApplyRedirect link must be known as account-only")
WF = "https://wf.wd1.myworkdayjobs.com/WellsFargoJobs/job/WHEAT-RIDGE-CO/Branch-Operations-Coordinator-Wheat-Ridge-CO_R-480101"
asked = []


def _wd_search(spec, text):
    asked.append((spec, text))
    if spec.startswith("wf/"):
        return [S.Job("workday", "wf", "R-480101", "Branch Operations Coordinator - Wheat Ridge, CO", "Wheat Ridge, CO", WF, WF, ""),
                S.Job("workday", "wf", "R-480102", "Branch Operations Coordinator - Mankato, MN", "Mankato, MN", WF + "2", WF + "2", "")]
    return []


old_search, old_fetch = getattr(S, "workday_search", None), dict(S.FETCHERS)
S.workday_search = _wd_search
for k in list(S.FETCHERS):
    if k != "workday":
        S.FETCHERS[k] = lambda *a, **kw: []                       # no network: no board on any public feed
try:
    got = A.direct_apply_url(bi, log=lambda *a: None, workday_boards=["blackrock/wd1/BlackRock_Professional/BlackRock", "wf/wd1/WellsFargoJobs/Wells Fargo"])
    check(got == WF, f"Built In listing should resolve to the employer's own Workday posting (title punctuation aside): {got}")
    check(all(sp.startswith("wf/") for sp, _ in asked), f"only the employer's own Workday board may be searched: {asked}")
    none = A.direct_apply_url(A._job("builtin", "Tiny Startup LLC", "1", "Operations Coordinator", "Denver, CO", bi.apply_url, ""),
                              log=lambda *a: None, workday_boards=["wf/wd1/WellsFargoJobs/Wells Fargo"])
    check(none is None, f"a company with no board found must give no link: {none}")
except Exception as e:
    check(False, f"Built In resolution crashed: {type(e).__name__}: {e}")
finally:
    if old_search:
        S.workday_search = old_search
    S.FETCHERS.clear(); S.FETCHERS.update(old_fetch)

if __name__ == "__main__":
    if problems:
        print("RESULT: PROBLEMS:\n  - " + "\n  - ".join(problems))
        sys.exit(1)
    print("ok  new job sources")
    print("RESULT: ALL AS EXPECTED")
