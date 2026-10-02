"""Offline checks for everything that does not need a browser: how questions are answered, which postings are kept,
how each job-board feed is read, the free-minutes budget, notifications, inbox parsing, the history-saving script.
Run on its own:  python tests/more_checks.py      (also run by tests/unit_checks.py)"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autoapply import mailbox, notify, sources, aggregators          # noqa: E402
from autoapply import main as M                                          # noqa: E402
from autoapply.brain import Brain                                        # noqa: E402
from autoapply.sources import Job, prefilter                             # noqa: E402

CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
PROFILE = yaml.safe_load((ROOT / "profile.yaml").read_text())
YN = ["Yes", "No"]
problems: list[str] = []


def check(cond, msg):
    if not cond:
        problems.append(msg)


# ------------------------------------------------------------------------------------------------ 1. form questions
def question_checks():
    cfg = json.loads(json.dumps(CFG))
    cfg["writer"]["enabled"] = False
    b = Brain(cfg, PROFILE, ROOT, lambda *a: None)
    b.applied_before = {"acme"}

    def ask(label, kind="select", opts=None, req=True, loc="Denver, CO", company="acme", title="Operations Coordinator"):
        b._job = Job("greenhouse", company, "1", title, loc, "", "", "")
        f = {"id": "x", "kind": kind, "label": label, "question": "", "options": opts or [], "required": req}
        return b._answer(f, "", dict(company=company.title(), role=title))

    for q in ("Do you NOT require sponsorship?", "Are you not currently authorized to work in the United States?",
              "Is there any reason you cannot work on-site?"):
        check(ask(q, "radio", YN) is None, f"negated question answered instead of skipped: {q}")
    check(ask("Resume/CV", "file", []) == "RESUME", "resume upload not answered")
    check(CFG.get("resume_file") == "briandelgado_resume.pdf" and (ROOT / CFG["resume_file"]).is_file(), "fixed resume file missing")
    class _W:                                            # cover-letter length gate
        def __init__(self, text): self.text = text
        def ready(self): return True
        def cover_letter(self, job, company, log=print): return self.text
    b.writer = _W("Dear Acme,\n\nToo short.\n\nBrian")
    b.writer.cover_letter = lambda job, c, log=print, _t=b.writer.text: None
    check(ask("Cover Letter", "file", []) == "COVER_LETTER", "optional cover-letter upload not offered a letter")
    b._letters = {}; check(b.lazy_letter(b._job) is None, "an optional letter the writer can't write must be left out")
    check(ask("Cover letter", "textarea", [], req=False) is None, "optional cover-letter box filled without a written letter")
    b._letters = {}
    got = ask("Cover letter", "textarea", [], req=True, company="acme")
    check(bool(got) and len(got.split()) >= 270 and "Dear Acme hiring team" in got and "{" not in got,
          f"a REQUIRED cover-letter box must get the full standard letter, got {str(got)[:80]!r}")
    b.writer = None
    EDU2 = ["Associate’s Degree", "Bachelor’s Degree", "Master’s Degree", "Doctoral Degree", "In Progress - degree not yet completed", "High school diploma or equivalent"]
    check(ask("What is your highest level of completed education?*", "combobox", EDU2) == "Bachelor’s Degree", "highest level of COMPLETED education not answered")
    check(ask("Will you now or in the future require sponsorship to work in the US? (incl. but not limited to H-1B, TN)", "combobox", YN) == "No", "'not limited to' wrongly treated as a negated question")
    check(ask("This role is required to be based near our New York City, NY hub. Are you open to relocation?", "combobox", YN, loc="United States") == "Yes", "relocation to a named approved city not answered")
    check(ask("Are you open to relocation to Chicago?", "combobox", YN, loc="United States") == "Yes", "relocation question for a country-level posting")
    check(ask("Our interviews require you to be on video. Can you confirm that you can keep your camera on?", "combobox", YN) == "Yes", "camera-on question not answered")
    check(ask("Applicant Privacy Statement", "checkbox_group", ["By clicking this box and submitting my application, I do not consent", "By clicking this box and submitting, I consent to the Applicant Privacy Statement"]) == ["By clicking this box and submitting, I consent to the Applicant Privacy Statement"], "privacy consent picked the wrong option")
    check(ask("Which office are you applying to?", "select", ["Denver", "New York", "San Francisco"], loc="New York, NY") == "New York", "office matching job location")
    check(ask("Please confirm you are not a robot", "checkbox_single", []) is None, "the bot must never tick an 'I am not a robot' box")
    check(ask("City", "text", []) == "New Castle", "plain City field should be just the city")
    check(ask("Do you have experience with Excel?", "radio", YN) == "Yes", "Excel experience (skill listed as 'Excel (advanced)')")
    check(ask("Do you require accommodation to complete the application process?", "radio", YN) == "No", "application accommodation")
    check(ask("Phone type", "select", ["Mobile", "Home", "Work"]) == "Mobile", "phone type")
    for q, want in (("Are you a government official or public official?", "No"), ("Are you related to a government official or politically exposed person (PEP)?", "No"),
                    ("Is any member of your immediate family a public official?", "No"), ("Are you subject to a non-compete agreement with a current or former employer?", "No"),
                    ("Are you bound by any restrictive covenants?", "No"), ("Do you currently have any other employment?", "No"), ("Do you have any conflicts of interest?", "No"),
                    ("Are you willing to sign a non-compete agreement?", "Yes"), ("Do you engage in any outside business activities?", "No"),
                    ("Are you currently debarred, suspended or excluded from participating in federal programs?", "No"), ("Are you a registered lobbyist?", "No"),
                    ("Have you ever been terminated or asked to resign from a job?", "No"),
                    ("Are you available to work weekends?", "Yes"), ("Are you willing to work overtime?", "Yes"), ("Are you comfortable working nights or holidays?", "Yes"),
                    ("Do you have experience with Salesforce?", "No"), ("Are you willing to wear a uniform?", "Yes"), ("Are you able to stand for long periods?", "Yes"),
                    ("Do you hold a PMP certification?", "No"), ("Are you willing to be on call?", "Yes")):
        check(ask(q, "radio", YN) == want, f"government/non-compete/conflict question wrong: {q} -> {ask(q, 'radio', YN)!r}")
    for q, kind, opts, want in (
            ("How many years of experience do you have with Salesforce?", "text", [], "0"), ("How many years of experience do you have with SAP?", "number", [], "0"),
            ("How many years of experience do you have with Excel?", "text", [], "4"), ("How many years of experience do you have in supply chain?", "text", [], "2"),
            ("Are you a member of a union?", "radio", YN, "No"), ("Are you currently on an F-1 visa?", "radio", YN, "No"), ("Do you need a visa to work here?", "radio", YN, "No"),
            ("Are you married?", "radio", YN, "No"), ("What is your marital status?", "select", ["Single", "Married", "Divorced"], "Single"),
            ("Do you have any dependents?", "radio", YN, "No"), ("Have you ever been arrested?", "radio", YN, "No"), ("Do you have any pending criminal charges?", "radio", YN, "No"),
            ("Have you ever filed for bankruptcy?", "radio", YN, "No"), ("Do you have any wage garnishments?", "radio", YN, "No"),
            ("Do you have a clean driving record?", "radio", YN, "Yes"), ("Have you had any DUI or moving violations in the past 3 years?", "radio", YN, "No"),
            ("Do you have a valid passport?", "radio", YN, "Yes"), ("Do you own a vehicle?", "radio", YN, "Yes"),
            ("Do you have access to a computer and reliable internet?", "radio", YN, "Yes"), ("Do you agree that all information is accurate?", "radio", YN, "Yes"),
            ("Please select your availability", "checkbox_group", ["Weekdays", "Weekends", "Evenings"], ["Weekdays", "Weekends", "Evenings"]),
            ("Age range", "select", ["18-24", "25-34", "35-44", "45+"], "18-24"), ("Do you have any scheduling restrictions?", "radio", YN, "No"),
            ("Do you identify as LGBTQ+?", "radio", YN, "No"), ("Do you speak any other languages?", "radio", YN, "Yes")):
        got = ask(q, kind, opts)
        check(got == want, f"confirmed-fact question wrong: {q} -> {got!r}, want {want!r}")
    L4 = ["Beginner", "Intermediate", "Advanced", "Expert"]
    for q, opts, want in (("Rate your proficiency in Excel", L4, "Advanced"), ("Rate your proficiency in SQL", L4, "Intermediate"), ("Rate your proficiency in Salesforce", L4, "Beginner"),
                          ("How would you rate your Python skills?", ["1", "2", "3", "4", "5"], "3"), ("How would you rate your Excel skills?", ["1", "2", "3", "4", "5"], "4"),
                          ("Proficiency in supply chain planning", ["No experience", "Basic", "Proficient", "Expert"], "Proficient")):
        check(ask(q, "select", opts) == want, f"proficiency rating wrong: {q} -> {ask(q, 'select', opts)!r}, want {want!r}")
    check(ask("Why are you leaving your current role?", "textarea", []).startswith("My most recent role"), "reason for leaving not answered")
    check(ask("This role is required to be based near our New York City, NY hub. Are you open to relocation? If not, please explain.", "combobox", YN, loc="New York, NY") == "Yes", "relocation question containing 'if not' was skipped")
    check(ask("Are you not currently authorized to work in the United States?", "radio", YN) is None, "negated authorization question must still be skipped")
    edu = ["High School", "Associate's", "Bachelor's", "Master's", "PhD"]
    table = [
        # work authorization and sponsorship
        ("Are you legally authorized to work in the United States?", "radio", YN, "Yes"),
        ("Will you now or in the future require sponsorship for employment visa status?", "radio", YN, "No"),
        ("Do you require visa sponsorship?", "select", YN, "No"),
        ("Are you a U.S. citizen?", "radio", YN, "No"),
        ("What is your current work authorization status?", "select", ["U.S. Citizen", "Permanent Resident", "H-1B", "F-1 / OPT", "Other"], "Permanent Resident"),
        ("Are you currently authorized to work in the country outlined for this job (e.g. H-1B status)", "combobox",
         ["I am authorized to work for any employer", "I am authorized to work for a specific employer only", "I am not authorized"],
         "I am authorized to work for any employer"),
        ("I am authorized to work in the United States and do not require sponsorship", "checkbox_single", [], True),
        ("I will require sponsorship to work in the United States", "checkbox_single", [], None),
        ("I am not authorized to work in the United States", "checkbox_single", [], None),
        ("Are you a U.S. person as defined by export control laws (citizen, lawful permanent resident, refugee or asylee)?", "radio", YN, "Yes"),
        ("Can you provide proof of eligibility to work in the U.S. upon hire?", "radio", YN, "Yes"),
        ("Do you hold an active security clearance?", "radio", YN, "No"),
        # employer history
        ("Have you ever been employed by Acme?", "radio", YN, "No"),
        ("Are you a current or former Acme employee?", "radio", YN, "No"),
        ("Have you previously applied to Acme?", "radio", YN, "Yes"),                 # this bot did apply to 'acme' before
        ("Have you ever interviewed at Anthropic before?*", "combobox", YN, "No"),
        ("Do you have any relatives or close friends who work at Acme?", "radio", YN, "No"),
        ("Do you have any existing family members at Koalafi? *", "combobox", YN, "No"),
        ("Were you referred by a current employee? If so, name:", "text", [], "No"),
        # where he lives / will work
        ("Do you currently reside in the Denver Metro area? *", "combobox", YN, "No"),
        ("Do you live in Colorado?", "radio", YN, "Yes"),
        ("Are you located in the United States?", "radio", YN, "Yes"),
        ("What state do you reside in?", "select", ["California", "Colorado", "New York", "Texas"], "Colorado"),
        ("Which office would you prefer to work from?", "select", ["Denver", "New York", "San Francisco", "Remote"], "Denver"),
        ("Preferred work location", "select", ["Austin", "Chicago", "Denver, CO", "Los Angeles, CA"], "Denver, CO"),
        ("Are you willing to relocate?", "select", YN, "Yes"),
        ("Are you willing to relocate?", "select", YN, None, dict(loc="Chicago, IL")),      # not a city he approved
        ("This role is hybrid (3 days in office). Are you comfortable with this?", "radio", YN, "Yes"),
        ("Are you open to remote work?", "radio", YN, "Yes"),
        ("Are you willing to travel up to 25% of the time?", "radio", YN, "Yes"),
        # pay, start date
        ("What is your desired salary?", "text", [], "$70,000 to $80,000"),
        ("Salary expectations (USD)", "number", [], "75000"),
        ("When can you start?", "text", [], "Two weeks after an offer"),
        # experience
        ("How many years of experience do you have in operations?", "text", [], "3"),
        ("Years of experience with Excel", "number", [], "4"),
        ("Do you have 3+ years of experience in supply chain?", "radio", YN, "No"),         # he has 2
        ("Do you have 2+ years of experience in supply chain?", "radio", YN, "Yes"),
        ("Do you have 5+ years of experience in operations?", "radio", YN, "No"),
        ("Do you have at least 3 years of experience in operations?", "radio", YN, "Yes"),
        ("How many years of experience do you have in supply chain?", "select", ["0-1 years", "1-3 years", "3-5 years", "5+ years"], "1-3 years"),
        ("How many years of operations experience do you have?", "select", ["Less than 1 year", "1-2 years", "3-5 years", "6+ years"], "3-5 years"),
        ("Do you have experience in project management?", "radio", YN, "Yes"),
        ("Do you have experience using Python or SQL?", "radio", YN, "Yes"),
        ("Do you have experience with Salesforce?", "radio", YN, "No"),                    # not on his résumé: truthful answer is No
        ("Have you ever been terminated or asked to resign from a job?", "radio", YN, "No"),
        # education
        ("Highest level of education completed", "select", edu, "Bachelor's"),
        ("Do you have a bachelor's degree?", "radio", YN, "Yes"),
        ("Do you have a master's degree?", "radio", YN, "No"),
        ("Are you currently a student?", "radio", YN, "No"),
        ("What is your GPA?", "text", [], "3.07"),
        ("School", "text", [], "University of Southern California"),
        ("Discipline / Major", "text", [], "Industrial and Systems Engineering"),
        # practical
        ("Are you at least 18 years of age?", "radio", YN, "Yes"),
        ("Do you have a valid driver's license?", "radio", YN, "Yes"),
        ("Are you willing to submit to a background check?", "radio", YN, "Yes"),
        ("Have you ever been convicted of a felony?", "radio", YN, "No"),
        ("Are you available to work full time?", "radio", YN, "Yes"),
        ("Are you comfortable working in a fast-paced environment?", "radio", YN, "Yes"),
        ("Are you able to lift up to 50 pounds?", "radio", YN, "Yes"),
        ("Are you able to lift 75 pounds?", "radio", YN, None),
        ("Can you work weekends and holidays?", "radio", YN, "Yes"),
        ("Do you speak Spanish?", "radio", YN, "Yes"),
        ("Please rate your Spanish proficiency", "select", ["None", "Basic", "Conversational", "Fluent", "Native"], "Native"),
        # contact / links
        ("Phone", "text", [], "(970) 366-8832"),
        ("Address Line 1", "text", [], "63 Cherry Ct"),
        ("Zip Code", "text", [], "81647"),
        ("Current company", "text", [], "N/A"),
        ("GitHub URL", "text", [], "N/A"),
        ("How did you hear about this job?", "select", ["LinkedIn", "Indeed", "Company website", "Job board", "Referral", "Other"], "Job board"),
        # self-identification
        ("Gender", "select", ["Male", "Female", "Non-binary", "Decline to self identify"], "Male"),
        ("Are you Hispanic/Latino?", "select", ["Yes", "No", "Decline to self identify"], "Yes"),
        ("Veteran status", "select", ["I am not a protected veteran", "I am a protected veteran", "I don't wish to answer"], "I am not a protected veteran"),
        ("Disability status", "select", ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"], "No, I do not have a disability"),
        ("I identify as a first-generation professional (please select one):*", "combobox", ["Yes", "No", "I don't wish to answer"], "Yes"),
        # consents and things that are never agreed to or certified for him
        ("I have read and agree to the Privacy Policy", "checkbox_single", [], True),
        ("Do you consent to receive text messages (SMS) about your application?", "checkbox_single", [], True),
        ("Please acknowledge that you've read and understand Mixpanel's Recruitment Privacy Policy*", "combobox", ["Acknowledge/Confirm"], "Acknowledge/Confirm"),
        ("Do you agree to Acme's arbitration agreement?", "checkbox_single", [], None),
        ("Are you subject to a non-compete or non-solicitation agreement?", "radio", YN, "No"),   # confirmed by Brian: none
        ("AI Policy for Application*", "combobox", YN, None),
        ("Do you certify that you did not use AI to complete this application?", "checkbox_single", [], None),
        ("Did you use AI to help with this application?", "radio", YN, "Yes"),
        ("I certify that I meet the minimum qualifications for this role", "radio", YN, "Yes"),   # entry-level posting with no license / grad degree: you meet them
    ]
    for row in table:
        label, kind, opts, want = row[:4]
        kw = row[4] if len(row) > 4 else {}
        got = ask(label, kind, opts, **kw)
        check(got == want, f"question {label[:70]!r}: got {got!r}, want {want!r}")
    print(f"ok  {len(table)} real-world application questions answered as expected")


# ------------------------------------------------------------------------------------------------ 2. keep / drop / score
def fit_checks():
    s, cfg = CFG["search"], json.loads(json.dumps(CFG))
    b = Brain({**cfg, "writer": {"enabled": False}}, PROFILE, ROOT, lambda *a: None)

    def J(title, loc="Denver, CO", desc=""):
        return Job("greenhouse", "acme", "1", title, loc, "", "", desc)

    keep = [J("Operations Associate"), J("Business Operations Analyst", "New York, NY"), J("Project Coordinator", "Remote - US"),
            J("Supply Chain Analyst II", "Los Angeles, CA"), J("Junior Business Analyst", "Remote"), J("Program Coordinator", "Denver, Colorado, United States"),
            # real postings seen while researching boards: state abbreviations, boroughs, LA suburbs, multi-level titles
            J("Contracts Specialist", "Westminster, CO"), J("Strategy & Operations Associate", "Long Island City"), J("Business Operations Manager", "Torrance, CA"),
            J("Business Operations Associate / Senior Associate", "New York, NY"), J("Analyst / Senior Analyst, Commercial Analysis", "New York, NY"),
            J("Production Scheduler", "El Segundo, California, United States"), J("Project Engineer", "Rye Brook, New York"),
            J("Operations Coordinator", "Bloomington, CA"), J("Operations Associate", "Denver, CO; Boston, MA")]
    drop = {
        "senior": J("Senior Operations Manager"), "not a fit title": J("Software Engineer"), "tech": J("Operations Data Engineer"),
        "level III": J("Operations Analyst III"), "abroad": J("Operations Coordinator", "London, UK"), "wrong city": J("Operations Coordinator", "Austin, TX"),
        "talent pool": J("Operations Associate - Future Opportunities"), "vp": J("VP, Operations"), "intern": J("Operations Intern"),
        "still senior": J("Senior Associate, Accounting & Finance", "Denver"), "canada": J("Operations Coordinator", "Toronto, ON, CA"),
        "hyphenated co": J("Operations Coordinator", "Co-Working Hub, Boston, MA"), "other state": J("Office Coordinator", "Houston, TX"),
    }
    for j in keep:
        check(prefilter(j, s) is None, f"prefilter dropped a wanted posting: {j.title} / {j.location}: {prefilter(j, s)}")
    for why, j in drop.items():
        check(prefilter(j, s) is not None, f"prefilter kept an unwanted posting ({why}): {j.title} / {j.location}")

    good = "Remote. You will improve processes using Excel and SQL, coordinate vendor work. 2 years of experience. Pay range: $65,000 - $80,000."
    sc, _ = b.score(J("Operations Coordinator", "Denver, CO", good))
    check(sc >= s["min_score"], f"a good fit scored {sc}, under the bar {s['min_score']}")
    sc, _ = b.score(J("Operations Manager", "Denver, CO", "Requires 8+ years of experience in operations."))
    check(sc < s["min_score"], f"an 8+ years posting scored {sc}")
    sc, _ = b.score(J("Operations Associate", "Denver, CO", "Pay range: $130,000 - $160,000 per year. 2 years experience."))
    check(sc < s["min_score"], f"a $130K+ posting scored {sc}")
    sc, _ = b.score(J("Operations Associate", "Denver, CO", "Security clearance required. US citizens only. Pay $70,000."))
    check(sc < s["min_score"], f"a clearance posting scored {sc}")
    b._job = J("Operations Coordinator", "Westminster, CO")
    check(b._location_ok(), "relocation questions should be answered for 'Westminster, CO' (Colorado)")
    b._job = J("Operations Coordinator", "Houston, TX")
    check(not b._location_ok(), "relocation question answered for a city you did not approve")
    sc_co, why = b.score(J("Operations Coordinator", "Westminster, CO", "Process improvement with Excel."))
    check("location" in why, f"'Westminster, CO' should earn the preferred-location bonus: {why}")
    print("ok  keep / drop / score decisions")


# ------------------------------------------------------------------------------------------------ 3. job-board feeds
class Resp:
    def __init__(self, data, status=200, content=b""):
        self._d, self.status_code, self.content, self.text = data, status, content, json.dumps(data) if not isinstance(data, str) else data

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            e = requests.HTTPError(f"HTTP {self.status_code}")
            e.response = self
            raise e


class Patch:
    """Swap requests.get / requests.post for the duration of a block."""
    def __init__(self, get=None, post=None):
        self.get, self.post = get, post

    def __enter__(self):
        self.old = (requests.get, requests.post)
        if self.get:
            requests.get = self.get
        if self.post:
            requests.post = self.post

    def __exit__(self, *a):
        requests.get, requests.post = self.old


def source_checks():
    sources.SEARCH = CFG["search"]

    # -- Workday, a small career site: Workday reports `total` on the first page only
    calls = []
    small = [{"title": f"Operations Coordinator {i}", "externalPath": f"/job/Denver-CO/Ops-{i}_R-{i}", "locationsText": "Denver, CO"} for i in range(45)]

    def post_small(url, json=None, headers=None, timeout=None):
        calls.append(json["offset"])
        return Resp({"total": 45 if json["offset"] == 0 else 0, "jobPostings": small[json["offset"]:json["offset"] + 20]})

    def get_detail(url, headers=None, timeout=None, params=None):
        return Resp({"jobPostingInfo": {"jobDescription": "<p>Coordinate vendors with Excel.</p>", "location": "Denver, CO"}})
    with Patch(get=get_detail, post=post_small):
        jobs = sources.workday("acme/wd5/External/Acme Corp")
    check(len(jobs) == 45 and calls == [0, 20, 40], f"workday small site: {len(jobs)} jobs, offsets {calls}")
    check(jobs[0].company == "acme" and jobs[0].extra.get("company_name") == "Acme Corp" and "/External/job/" in jobs[0].url, f"workday job fields: {jobs[0]}")
    check("Coordinate vendors" in jobs[0].description, "workday detail description not read")

    # -- Workday, a huge career site: searched by role x place, duplicates merged, '2 Locations' resolved from the detail page
    queries = []

    def post_big(url, json=None, headers=None, timeout=None):
        q = json["searchText"]
        queries.append((q, json["offset"]))
        if not q:
            return Resp({"total": 9000, "jobPostings": [{"title": "Warehouse Associate", "externalPath": "/job/x/Whs_R-0", "locationsText": "Reno, NV"}]})
        if json["offset"] == 0:
            n = 5 if q.startswith("operations") else 2
            posts = [{"title": f"Operations Analyst {q}", "externalPath": f"/job/x/{q.replace(' ', '-')}-{i}_R-{abs(hash(q)) % 10000}-{i}", "locationsText": "2 Locations"}
                     for i in range(n)]
            posts.append({"title": "Operations Analyst shared", "externalPath": "/job/x/shared_R-SHARED", "locationsText": "Denver, CO"})   # same in every query
            return Resp({"total": 0, "jobPostings": posts})
        return Resp({"total": 0, "jobPostings": []})

    detail_calls = []

    def get_multi(url, headers=None, timeout=None, params=None):
        detail_calls.append(url)
        return Resp({"jobPostingInfo": {"jobDescription": "<p>desc</p>", "location": "Denver, CO", "additionalLocations": ["New York, NY"]}})
    with Patch(get=get_multi, post=post_big):
        jobs = sources.workday("big/wd1/Careers")
    n_roles, n_places = len(sources.WD_ROLES), len(sources.WD_PLACES)
    texts = [q for q, o in queries if q]
    check(all(any(r in t for r in sources.WD_ROLES) and any(p in t for p in sources.WD_PLACES) for t in texts) and len(set(texts)) == n_roles * n_places,
          f"workday big site queries: {len(set(texts))} distinct (want {n_roles * n_places})")
    ids = [j.job_id for j in jobs]
    check(len(ids) == len(set(ids)) and ids.count("R-SHARED") == 1, "workday duplicates were not merged")
    multi = [j for j in jobs if j.job_id not in ("R-SHARED", "R-0")]
    check(multi and all(j.location == "Denver, CO; New York, NY" for j in multi), f"'2 Locations' not resolved: {multi[0].location if multi else None}")

    # -- the smaller feeds
    def by_url(mapping):
        def _get(url, headers=None, timeout=None, params=None):
            for k, v in mapping.items():
                if k in url:
                    return Resp(v)
            return Resp({}, 404)
        return _get
    gh = {"boards-api.greenhouse.io": {"jobs": [{"id": 7, "title": "Ops Associate", "location": {"name": "Denver, CO"}, "absolute_url": "https://acme.com/careers?gh_jid=7",
                                                  "company_name": "Acme Inc", "content": "&lt;p&gt;Run vendors.&lt;/p&gt;"}]}}
    with Patch(get=by_url(gh)):
        j = sources.greenhouse("acme")[0]
    check(j.extra.get("company_name") == "Acme Inc" and j.description == "Run vendors." and j.apply_url == "https://job-boards.greenhouse.io/acme/jobs/7", f"greenhouse: {j}")

    lv = {"api.lever.co": [{"id": "0d2b4c3a-1111-2222-3333-444455556666", "text": "Ops Coordinator", "categories": {"location": "New York"},
                             "hostedUrl": "https://jobs.lever.co/acme/0d2b4c3a", "applyUrl": "https://jobs.lever.co/acme/0d2b4c3a/apply", "descriptionPlain": "d",
                             "lists": [], "additionalPlain": ""}]}
    with Patch(get=by_url(lv)):
        j = sources.lever("acme")[0]
    check(j.title == "Ops Coordinator" and j.location == "New York" and j.apply_url.endswith("/apply"), f"lever: {j}")

    wk = {"apply.workable.com/api/v1/widget": {"name": "Acme Co", "jobs": [{"shortcode": "ABC", "title": "Coordinator", "city": "Denver", "state": "Colorado",
                                                                          "country": "United States", "telecommuting": False, "url": "https://apply.workable.com/j/ABC",
                                                                          "application_url": "https://apply.workable.com/acme/j/ABC/apply", "description": "<p>d</p>"}]}}
    with Patch(get=by_url(wk)):
        j = sources.workable("acme")[0]
    check(j.location == "Denver, Colorado, United States" and j.extra.get("company_name") == "Acme Co" and j.apply_url.endswith("/ABC/apply"), f"workable: {j}")

    bb = {"/careers/list": {"result": [{"id": 5, "jobOpeningName": "Operations Coordinator", "location": {"city": "Denver", "state": "CO"}, "isRemote": False}]},
          "/careers/5/detail": {"result": {"jobOpening": {"description": "<p>Coordinate.</p>", "compensation": "$60,000"}}}}
    with Patch(get=by_url(bb)):
        j = sources.bamboohr("acme")[0]
    check(j.url == "https://acme.bamboohr.com/careers/5" and "Salary: $60,000" in j.description, f"bamboohr: {j}")

    rc = {"recruitee.com/api/offers": {"offers": [{"id": 1, "title": "Ops", "careers_url": "https://acme.recruitee.com/o/ops", "location": "Denver, CO",
                                                    "company_name": "Acme B.V.", "description": "<p>d</p>", "requirements": "<p>r</p>"}]}}
    with Patch(get=by_url(rc)):
        j = sources.recruitee("acme")[0]
    check(j.extra.get("company_name") == "Acme B.V." and j.apply_url.endswith("/c/new"), f"recruitee: {j}")

    bz = {"breezy.hr/json": [{"id": "a", "name": "Ops", "url": "https://acme.breezy.hr/p/a", "location": {"name": "Denver, CO", "is_remote": False},
                               "company": {"name": "Acme LLC"}, "description": "<p>d</p>"}]}
    with Patch(get=by_url(bz)):
        j = sources.breezy("acme")[0]
    check(j.extra.get("company_name") == "Acme LLC" and j.location == "Denver, CO", f"breezy: {j}")

    # -- discover(): boards that do not exist are dropped after two misses; 422 counts as 'no such board'; errors do not
    tmp = Path(tempfile.mkdtemp())
    hits: dict[str, int] = {}

    def fake(name):
        hits[name] = hits.get(name, 0) + 1
        if name == "ok":
            return [Job("greenhouse", "ok", "1", "Ops", "Denver", "u", "u", "")]
        e = requests.HTTPError("x")
        e.response = Resp({}, {"gone": 404, "gone422": 422, "flaky": 500}[name])
        raise e
    old = sources.FETCHERS["greenhouse"]
    sources.FETCHERS["greenhouse"] = fake
    try:
        old_sleep, time.sleep = time.sleep, lambda *_: None
        logs = []
        for _ in range(3):
            got = sources.discover({"greenhouse": ["ok", "gone", "gone422", "flaky", "OK"]}, logs.append, base=tmp)
        check(len(got) == 1, f"discover returned {len(got)} jobs")
        check(hits["gone"] == 2 and hits["gone422"] == 2 and hits["flaky"] >= 3 and hits["ok"] == 3, f"discover dead-board memory: {hits}")
        state = json.loads((tmp / "boards_state.json").read_text())
        check(state["greenhouse"]["gone"]["dead"] == 2 and state["greenhouse"]["ok"]["dead"] == 0, f"boards_state: {state}")
    finally:
        sources.FETCHERS["greenhouse"] = old
        time.sleep = old_sleep
        shutil.rmtree(tmp, ignore_errors=True)
    check(sources.dedupe_boards("workday", ["a/wd1/X", "A/wd1/x/Acme", "b/wd5/Y"]) == ["A/wd1/x/Acme", "b/wd5/Y"], "dedupe_boards(workday)")
    check(sources.dedupe_boards("greenhouse", ["Acme", "acme", "beta"]) == ["Acme", "beta"], "dedupe_boards(greenhouse)")
    # jobs the database already decided do not get their detail page read again; titles with no wanted word are not read at all
    first_run_calls = len(detail_calls)
    check(first_run_calls == len(jobs) - 1, f"first run should read every wanted posting's detail page: {first_run_calls} of {len(jobs) - 1}")
    sources.KNOWN = {jobs[1].key: "applied", jobs[2].key: "skipped", jobs[3].key: "queued"}
    detail_calls.clear()
    try:
        with Patch(get=get_multi, post=post_big):
            sources.workday("big/wd1/Careers")
        check(len(detail_calls) == first_run_calls - 2, f"decided jobs were read again: {len(detail_calls)} calls, want {first_run_calls - 2}")
        sources.SEARCH = {**CFG["search"], "_title_keys": ["nothing-matches-this"]}
        detail_calls.clear()
        sources.KNOWN = {}
        with Patch(get=get_multi, post=post_big):
            sources.workday("big/wd1/Careers")
        check(not detail_calls, "detail pages were read for titles with no wanted keyword")
    finally:
        sources.KNOWN = {}
        sources.SEARCH = CFG["search"]
    print("ok  job-board feeds (Greenhouse, Lever, Workday small + huge, Workable, BambooHR, Recruitee, Breezy) and dead-board memory")


# ------------------------------------------------------------------------------------------------ 4. board list + budget
def board_and_budget_checks():
    data = yaml.safe_load((ROOT / "boards.yaml").read_text())
    names = data.pop("names")
    for ats, toks in data.items():
        check(ats in sources.FETCHERS, f"boards.yaml: unknown source {ats}")
        check(isinstance(toks, list) and all(isinstance(t, str) and t.strip() for t in toks), f"boards.yaml {ats}: entries must be strings")
        check(len(toks) == len({t.lower() for t in toks}) or ats == "workday", f"boards.yaml {ats}: duplicate entries")
    for spec in data["workday"]:
        parts = spec.split("/")
        check(len(parts) >= 3 and parts[1].startswith("wd") and parts[1][2:].isdigit(), f"boards.yaml workday spec looks wrong: {spec}")
    wd_keys = ["/".join(x.split("/")[:3]).lower() for x in data["workday"]]
    check(len(wd_keys) == len(set(wd_keys)), "boards.yaml workday: same site listed twice")
    check(all(isinstance(k, str) and isinstance(v, str) for k, v in names.items()), "boards.yaml names must be text")
    check(len(data["greenhouse"]) >= 150 and len(data["workday"]) >= 100, "boards.yaml is unexpectedly short")

    cfg = {"companies": {"greenhouse": ["mine", "AIRBNB"], "lever": None}, "company_names": {"casa": "My Override"}}
    added = M.merge_board_file(cfg, ROOT)
    check(added > 200 and "mine" in cfg["companies"]["greenhouse"], f"merge_board_file added {added}")
    check(sum(1 for t in cfg["companies"]["greenhouse"] if t.lower() == "airbnb") == 1, "merge_board_file duplicated a board")
    check(cfg["company_names"]["casa"] == "My Override" and cfg["company_names"]["janestreet"] == "Jane Street", "company_names precedence")
    check(M.merge_board_file({}, Path(tempfile.gettempdir()) / "nope") == 0, "merge_board_file with no boards.yaml")

    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "logs").mkdir()
        cfgs = {"search": {"actions_minutes_budget": 1850, "runs_per_day": 4, "max_run_minutes": 30}}
        os.environ.pop("GITHUB_ACTIONS", None)
        check(M.run_time_allowance(cfgs, tmp) == 30, "off Actions the allowance should be max_run_minutes")
        check(M.budget_ok(cfgs, tmp, print), "budget_ok off Actions")
        os.environ["GITHUB_ACTIONS"] = "true"
        M.add_usage(tmp, 100.0)
        check(abs(M.month_used(tmp) - 100.0) < 1e-6, "add_usage / month_used")
        mid = M.run_time_allowance(cfgs, tmp, today=date(2026, 9, 10))       # 1750 left, 21 days x 4 runs = 84 runs -> 20.8 - 3
        check(17.5 < mid < 18.2, f"allowance mid-month: {mid}")
        check(M.run_time_allowance(cfgs, tmp, today=date(2026, 9, 29)) == 30, "allowance near month end should hit the cap")
        M.add_usage(tmp, 1760.0)
        check(M.run_time_allowance(cfgs, tmp, today=date(2026, 9, 10)) == 6.0, "allowance floor")
        check(not M.budget_ok(cfgs, tmp, lambda *_: None), "budget_ok should stop a run once the month's budget is used")

        # a run that dies (cancelled, killed) is billed by the next run, up to its last heartbeat; a finished run is billed once
        t2 = Path(tempfile.mkdtemp())
        (t2 / "logs").mkdir()
        M.open_run(t2)
        u = json.loads((t2 / "logs" / "usage_minutes.json").read_text())
        check("open" in u, "open_run should note the run")
        u["open"]["beat"] = u["open"]["start"] + 5 * 60
        (t2 / "logs" / "usage_minutes.json").write_text(json.dumps(u))
        M.open_run(t2)                                                      # the next run finds the dead one
        check(abs(M.month_used(t2) - (5 + M.ACTIONS_OVERHEAD_MIN)) < 0.2, f"dead run billed {M.month_used(t2)}")
        M.heartbeat(t2)
        M.add_usage(t2, 10.0)
        u = json.loads((t2 / "logs" / "usage_minutes.json").read_text())
        check("open" not in u and abs(M.month_used(t2) - (5 + M.ACTIONS_OVERHEAD_MIN + 10.0)) < 0.2, f"finished run billing: {u}")
        M.open_run(t2)
        real = M._run
        M._run = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())     # a cancel in the middle of the run
        try:
            try:
                M.run(str(t2 / "config.yaml"))
            except KeyboardInterrupt:
                pass
        finally:
            M._run = real
        u = json.loads((t2 / "logs" / "usage_minutes.json").read_text())
        check("open" not in u and M.month_used(t2) > 5 + M.ACTIONS_OVERHEAD_MIN + 10.0 + M.ACTIONS_OVERHEAD_MIN - 0.1, f"cancelled run not billed: {u}")
        shutil.rmtree(t2, ignore_errors=True)
    finally:
        os.environ.pop("GITHUB_ACTIONS", None)
        shutil.rmtree(tmp, ignore_errors=True)

    # finding jobs must never leave a run with no time to apply; no run may outlive the workflow's timeout
    sc = CFG["search"]
    check(M.apply_deadline(0, 300, 12.4, sc) == 744.0, "deadline when finding jobs was quick")
    check(M.apply_deadline(0, 900, 12.0, sc) == 900 + 60 * sc["min_apply_minutes"], "a slow search should still leave a minimum apply window")
    check(M.apply_deadline(0, 1700, 12.0, sc) == 60 * (sc["max_run_minutes"] + 6), "the hard ceiling")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "autoapply.yml").read_text())
    run_step = next(x for x in wf["jobs"]["apply"]["steps"] if x.get("name") == "Run")
    check(sc["max_run_minutes"] + 6 + sc["max_minutes_per_job"] <= run_step["timeout-minutes"],
          "a run could outlive the workflow's Run-step timeout and never record its results")
    check(run_step["timeout-minutes"] < wf["jobs"]["apply"]["timeout-minutes"], "job timeout should exceed the Run step's")
    print("ok  boards.yaml is valid and merges; Actions-minutes budget behaves")


# ------------------------------------------------------------------------------------------------ 5. notify + inbox
def mail_checks():
    rows = [
        {"status": "blocked", "score": 70, "reason": "reCAPTCHA checkbox", "title": "Ops A", "company": "a", "url": "u1", "apply_url": "u1"},
        {"status": "blocked", "score": 80, "reason": "no application form found on page", "title": "Ops B", "company": "b", "url": "u2", "apply_url": ""},
        {"status": "manual", "score": 50, "reason": "apply by hand", "title": "Ops C", "company": "c", "url": "u3", "apply_url": ""},
        {"status": "manual", "score": 60, "reason": "apply by hand: this site blocks automated submissions. no application form found", "title": "Ops D", "company": "d", "url": "u4", "apply_url": ""},
        {"status": "failed", "score": 90, "reason": "timeout", "title": "Ops E", "company": "e", "url": "u5", "apply_url": ""},
        {"status": "skipped", "score": 99, "reason": "x", "title": "Ops F", "company": "f", "url": "u6", "apply_url": ""},
        {"status": "unconfirmed", "score": 65, "reason": "no confirmation after submit", "title": "Ops G", "company": "g", "url": "u7", "apply_url": ""},
    ]
    got = notify.manual_rows(rows)
    check([r["title"] for r in got] in (["Ops E", "Ops A", "Ops G"], ["Ops E", "Ops G", "Ops A"]), f"manual_rows picked {[r['title'] for r in got]}")
    check(len(notify.manual_rows(rows, limit=2)) == 2, "manual_rows limit")
    text = notify.build_text("2026-09-29", "2 applied", {"applied": [dict(title="Ops H", company="h", score=80, url="u8")], "unconfirmed": [rows[6]]}, got, "https://run")
    for want in ("APPLIED (1)", "SUBMITTED BUT NOT CONFIRMED", "EXCEPTIONS (3)", "Ops A", "https://run"):
        check(want in text, f"summary text lacks {want!r}")

    quiet = notify.build_text("2026-09-29", "2 blocked, 1 skipped", {"blocked": [rows[0], rows[0]], "skipped": [dict(rows[5], reason="posting restricts AI-assisted applications")]},
                              [], "", "build X · 5 good fits still waiting for a later run")
    check("The bot is running" in quiet and "blocked x2: reCAPTCHA checkbox" in quiet and "skipped x1: posting restricts" in quiet, f"quiet-run email: {quiet}")
    check(quiet.rstrip().endswith("5 good fits still waiting for a later run"), "email footer")
    nothing = notify.build_text("2026-09-29", "nothing new", {}, [], "", "")
    check("nothing new was found" in nothing, "empty-run email")
    check("same role at same company" not in notify.build_text("d", "c", {"skipped": [dict(rows[5], reason="same role at same company already skipped this run")]}, []), "noise reasons should be hidden")

    # one email whenever something happened, and a check-in once a day even when nothing did
    from datetime import datetime as _dt
    from autoapply.db import DB
    tmp = Path(tempfile.mkdtemp())
    sent: list = []
    real_send = notify.send_email
    notify.send_email = lambda cfg, subject, text, log=print: (sent.append((subject, text)), True)[1]
    try:
        (tmp / "reports").mkdir()
        db = DB(str(tmp / "a.db"))
        cfg = {"search": {"max_attempts": 2}, "notify": {}}

        def fin(day, since):
            return M.finish(cfg, db, since, lambda *a: None, tmp, day, None, False)
        fin("2026-09-29", _dt.now().isoformat(timespec="seconds"))
        check(len(sent) == 1 and "running" in sent[0][0], f"first quiet run of the day should send a check-in: {sent}")
        fin("2026-09-29", _dt.now().isoformat(timespec="seconds"))
        check(len(sent) == 1, "a second quiet run the same day should not email again")
        db.add(Job("greenhouse", "acme", "1", "Ops", "Denver", "http://u", "", ""), "applied", score=80, reason="confirmed")
        fin("2026-09-29", "2000-01-01T00:00:00")
        check(len(sent) == 2 and "1 applied" in sent[1][0] and "build " in sent[1][1], f"an application should email: {sent[-1:]}")
        check("BY SITE" in sent[1][1] and "greenhouse: 1 applied" in sent[1][1], f"per-site summary missing: {sent[1][1]}")
        fin("2026-09-30", _dt.now().isoformat(timespec="seconds"))
        check(len(sent) == 3, "the next day's first run should check in again")
    finally:
        notify.send_email = real_send
        shutil.rmtree(tmp, ignore_errors=True)

    yes = ["Thank you for applying to Acme", "We received your application", "Your application to Acme Corp", "Application received - Ops Coordinator",
           "Thanks for applying!", "You've applied to Ops Coordinator"]
    no = ["Verify your email address", "Your security code is 123456", "Job alert: 5 new roles", "Welcome to Acme careers", "Reset your password"]
    for subj in yes:
        check(bool(mailbox.CONFIRM_SUBJECT.search(subj)) and not mailbox.NOT_CONFIRM.search(subj), f"confirmation subject not recognised: {subj}")
    for subj in no:
        check(not mailbox.CONFIRM_SUBJECT.search(subj) or bool(mailbox.NOT_CONFIRM.search(subj)), f"non-confirmation treated as one: {subj}")

    now = time.time()
    msgs = [{"subject": "Your security code is 123456", "from": "no-reply@greenhouse-mail.io", "body": "code for Acme", "ts": now, "hint": True, "link": None, "code": "123456"},
            {"subject": "Thank you for applying to Beta", "from": "jobs@beta.com", "body": "Beta received it", "ts": now, "hint": False, "link": None, "code": None},
            {"subject": "Thank you for applying to Acme Corp!", "from": "no-reply@acme.com", "body": "We received your application for Ops.", "ts": now, "hint": False, "link": None, "code": None}]
    old = mailbox._recent
    mailbox._recent = lambda since_ts, n=25, folders=("INBOX",): iter(msgs)
    try:
        check(mailbox.find_confirmation("Acme Corp", now - 60, timeout=0) == "Thank you for applying to Acme Corp!", "find_confirmation missed the company's email")
        check(mailbox.find_confirmation("Gamma Inc", now - 60, timeout=0) is None, "find_confirmation matched the wrong company")
        found = mailbox.scan_confirmations({"k1": "Acme Corp", "k2": "Gamma Inc", "k3": "Beta"}, now - 60)
        check(set(found) == {"k1", "k3"}, f"scan_confirmations: {found}")
    finally:
        mailbox._recent = old
    check(not hasattr(mailbox, "security_code") and not hasattr(mailbox, "wait_for_security_code"), "the inbox reader must not read employers' security codes")
    print("ok  summary email, daily check-in, exceptions list, confirmation-email matching")


# ------------------------------------------------------------------------------------------------ 6. aggregator helpers
def aggregator_checks():
    A = aggregators
    for url, want in [("https://boards.greenhouse.io/acme/jobs/1", ("greenhouse", "acme")), ("https://job-boards.greenhouse.io/embed/job_app?for=acme&token=55", ("greenhouse", "acme")),
                      ("https://jobs.lever.co/foo/0d2b4c3a-1111-2222-3333-444455556666", ("lever", "foo")), ("https://apply.workable.com/acme/j/ABC123/", ("workable", "acme")),
                      ("https://acme.bamboohr.com/careers/12", ("bamboohr", "acme")), ("https://acme.recruitee.com/o/ops", ("recruitee", "acme")),
                      ("https://acme.breezy.hr/p/abc", ("breezy", "acme")), ("https://jobs.ashbyhq.com/ramp/1234", ("ashby", "ramp")), ("https://example.com/careers", None)]:
        check(A.board_of(url) == want, f"board_of({url}) = {A.board_of(url)}")
    check(A.canon_key("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver-CO/Ops-Coordinator_R-123456?source=x") == "workday:acme:R-123456", "canon_key workday")
    check(A.canon_key("https://boards.greenhouse.io/acme/jobs/555") == "greenhouse:acme:555", "canon_key greenhouse")
    check(A.find_job_link('<a href="https://boards.greenhouse.io/acme/jobs/777?gh_src=x">Apply</a>') == "https://boards.greenhouse.io/acme/jobs/777", "find_job_link greenhouse")
    check(A.find_job_link("no links here") is None, "find_job_link false positive")
    check("acme" in A.slug_variants("Acme Inc.") and "aloyoga" in A.slug_variants("Alo Yoga") and "renttherunway" in A.slug_variants("Rent the Runway"), "slug_variants")
    print("ok  aggregator link and board helpers")


# ------------------------------------------------------------------------------------------------ 7. workflow history script
def workflow_checks():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "autoapply.yml").read_text())
    trig = wf.get("on") or wf.get(True)
    cron = trig["schedule"][0]["cron"]
    check(cron == "7 1,13,17,21 * * *", f"workflow cron is {cron!r}")
    check(len(cron.split()[1].split(",")) == CFG["search"]["runs_per_day"], "search.runs_per_day does not match the workflow's cron times")
    steps = wf["jobs"]["apply"]["steps"]
    script = next(x["run"] for x in steps if x.get("name", "").startswith("Save history"))
    hist_line = next(ln for ln in script.splitlines() if ln.strip().startswith("HIST="))
    check("git add -f $ADD" in script and "agg_cache" not in hist_line and " logs " not in hist_line, "history step should add only small files")
    if not shutil.which("git"):
        print("skip workflow history simulation (no git)")
        return
    tmp = Path(tempfile.mkdtemp())
    try:
        def sh(cmd, cwd):
            return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
        sh("git init -q --bare -b main origin.git", tmp)
        sh("git clone -q origin.git seed", tmp)
        seed = tmp / "seed"
        sh("git config user.name t; git config user.email t@t", seed)
        (seed / ".gitignore").write_text("applications.db\nlogs/\nreports/\nconfig.yaml\n")
        (seed / "main.py").write_text("code")
        sh("git add -A && git commit -qm init && git push -q origin HEAD:main", seed)
        sh("git clone -q origin.git work", tmp)
        work = tmp / "work"
        sh("git config user.name t; git config user.email t@t", work)
        (work / "applications.db").write_bytes(b"db1")
        (work / "boards_state.json").write_text("{}")
        (work / "logs" / "agg_cache").mkdir(parents=True)
        (work / "logs" / "agg_cache" / "big.json").write_text("x" * 1000)
        (work / "logs" / "2026-09-29.log").write_text("log")
        (work / "logs" / "usage_minutes.json").write_text("{}")
        (work / "reports").mkdir()
        (work / "reports" / "2026-09-29.md").write_text("# r")
        (work / "save.sh").write_text(script)
        hook = tmp / "origin.git" / "hooks" / "pre-receive"           # reject the first push: a run that overlapped another
        hook.write_text('#!/bin/bash\nif [ ! -f "$GIT_DIR/rejected_once" ]; then touch "$GIT_DIR/rejected_once"; exit 1; fi\n')
        hook.chmod(0o755)
        r = sh("bash -e save.sh", work)
        check(r.returncode == 0, f"history script failed: {r.stderr[-300:]}")
        sh("git clone -q origin.git verify", tmp)
        listed = set(sh("git ls-files", tmp / "verify").stdout.split())
        want = {"applications.db", "boards_state.json", "logs/2026-09-29.log", "logs/usage_minutes.json", "reports/2026-09-29.md"}
        check(want <= listed, f"history script did not save {want - listed}")
        check(not any("agg_cache" in f for f in listed), "history script saved the feed cache")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ok  workflow: 4 runs a day, history saved (missing files tolerated, push race retried)")


# ------------------------------------------------------------------------------------------------ 8. publish script
FAKE_GH = r"""#!/usr/bin/env bash
echo "gh $*" >> "$FAKE_LOG"
case "$1 $2" in
  "auth status") exit 0 ;;
  "api user") echo "tester" ;;
  "repo view") exit 0 ;;
  "repo clone")
      git clone -q "$FAKE_REMOTE" "$4"
      if [ -n "${FAKE_RACE:-}" ]; then      # the bot saves its history between the clone and the push
        rm -rf "$FAKE_TMP/bot" && git clone -q "$FAKE_REMOTE" "$FAKE_TMP/bot"
        mkdir -p "$FAKE_TMP/bot/reports" && echo "run report" > "$FAKE_TMP/bot/reports/2026-09-29.md"
        (cd "$FAKE_TMP/bot" && git add -f reports && git -c user.name=bot -c user.email=b@b commit -qm "autoapply run" && git push -q origin HEAD:main)
      fi ;;
  "secret set")
      if [ "${6:-}" = "--body" ]; then printf '%s' "$7" > "$FAKE_SECRETS/$3"; else cat > "$FAKE_SECRETS/$3"; fi ;;
  "secret list") for f in "$FAKE_SECRETS"/*; do [ -e "$f" ] && printf '%s\tless than a minute ago\n' "$(basename "$f")"; done ;;
  "workflow run") : ;;
  *) echo "fake gh: unhandled $*" >&2; exit 1 ;;
esac
"""


def script_checks():
    if not (shutil.which("git") and shutil.which("bash")):
        print("skip publish-script simulation (no git or bash)")
        return
    tmp = Path(tempfile.mkdtemp())
    try:
        def sh(cmd, cwd, env=None):
            return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
        base_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        sh("git init -q --bare -b main remote.git", tmp)                        # "GitHub": older code plus the bot's saved history
        sh("git clone -q remote.git seed", tmp)
        seed = tmp / "seed"
        (seed / ".gitignore").write_text("config.yaml\napplications.db\nlogs/\nreports/\n")
        (seed / "autoapply").mkdir()
        (seed / "autoapply" / "old_module.py").write_text("old")
        (seed / "applications.db").write_bytes(b"history")
        (seed / "boards_state.json").write_text("{}")
        sh("git add -f -A && git -c user.name=t -c user.email=t@t commit -qm init && git push -q origin HEAD:main", seed, base_env)
        fake = tmp / "bin"
        fake.mkdir()
        (fake / "gh").write_text(FAKE_GH)
        (fake / "gh").chmod(0o755)
        (tmp / "secrets").mkdir()
        folder = tmp / "folder"                                                  # what you get after unzipping
        shutil.copytree(ROOT, folder, ignore=shutil.ignore_patterns("work", "__pycache__", ".git", "logs", "applications", "reports", "*.db"))
        env = {**base_env, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE": str(tmp / "remote.git"), "FAKE_LOG": str(tmp / "gh.log"),
               "FAKE_SECRETS": str(tmp / "secrets"), "FAKE_TMP": str(tmp), "IMAP_PASS": "app-pass-1"}
        for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "ACCOUNT_PASSWORD", "IMAP_USER"):
            env.pop(k, None)

        r = sh("bash update_github.sh", folder, env)
        check(r.returncode == 0, f"update_github.sh failed: {r.stdout[-300:]} {r.stderr[-300:]}")
        sh("git clone -q remote.git verify", tmp)
        listed = set(sh("git ls-files", tmp / "verify").stdout.split())
        for want in ("autoapply/main.py", "autoapply/sources.py", "boards.yaml", ".github/workflows/autoapply.yml", "README.md", "update_github.sh",
                     "applications.db", "boards_state.json"):
            check(want in listed, f"update_github.sh: {want} is not in the repo afterwards")
        check("briandelgado_resume.pdf" in listed, "update_github.sh did not push the résumé PDF the bot uploads")
        check("autoapply/old_module.py" not in listed, "update_github.sh left a deleted module behind")
        check(not (listed & {"config.yaml", "profile.yaml", "about_me.md", "voice.md"}), f"private files were pushed: {listed & {'config.yaml', 'profile.yaml', 'about_me.md', 'voice.md'}}")
        check(not any("__pycache__" in f or f.startswith("tests/work/") for f in listed), "cache or test scratch files were pushed")
        check((tmp / "verify" / "applications.db").read_bytes() == b"history", "the bot's saved history was changed")
        sec = tmp / "secrets"
        check((sec / "CONFIG_YAML").read_text() == (folder / "config.yaml").read_text(), "CONFIG_YAML secret not refreshed")
        check((sec / "IMAP_PASS").read_text() == "app-pass-1" and not (sec / "GROQ_API_KEY").exists(), "exported secrets should be set, others left alone")
        check("Still missing on GitHub" in r.stdout and "GROQ_API_KEY" in r.stdout and "IMAP_USER" in r.stdout, f"missing-secret warning: {r.stdout[-400:]}")

        head = sh("git rev-parse HEAD", tmp / "verify").stdout
        r = sh("bash update_github.sh", folder, env)
        check(r.returncode == 0 and "already up to date" in r.stdout, f"second run should change nothing: {r.stdout[-200:]}")
        sh("git pull -q", tmp / "verify")
        check(sh("git rev-parse HEAD", tmp / "verify").stdout == head, "second run made a commit")

        (folder / "boards.yaml").write_text((folder / "boards.yaml").read_text() + "\n# edited\n")   # a change, with a run saving history mid-update
        r = sh("bash update_github.sh --run", folder, {**env, "FAKE_RACE": "1"})
        check(r.returncode == 0, f"update during a race failed: {r.stdout[-300:]} {r.stderr[-300:]}")
        sh("git pull -q", tmp / "verify")
        listed = set(sh("git ls-files", tmp / "verify").stdout.split())
        check("reports/2026-09-29.md" in listed and "# edited" in (tmp / "verify" / "boards.yaml").read_text(), "the race lost either the update or the bot's report")
        check("gh workflow run autoapply.yml" in (tmp / "gh.log").read_text(), "--run did not start a run")

        # the code on GitHub has moved on (it was updated there directly): an older folder must not be put back over it
        init = tmp / "verify" / "autoapply" / "__init__.py"
        init.write_text('__version__ = "2099-01-01-aa"\n')
        (tmp / "verify" / "autoapply" / "newer_module.py").write_text("new")
        sh("git add -A && git -c user.name=t -c user.email=t@t commit -qm newer && git push -q origin HEAD:main", tmp / "verify", base_env)
        (folder / "config.yaml").write_text((folder / "config.yaml").read_text() + "\n# my edit\n")
        r = sh("bash update_github.sh", folder, env)
        check(r.returncode == 0 and "newer than this folder" in r.stdout, f"older folder vs newer GitHub code: {r.stdout[-300:]} {r.stderr[-200:]}")
        sh("git pull -q", tmp / "verify")
        check(init.read_text().strip() == '__version__ = "2099-01-01-aa"' and (tmp / "verify" / "autoapply" / "newer_module.py").exists(),
              "an older folder overwrote newer code on GitHub")
        check((sec / "CONFIG_YAML").read_text().endswith("# my edit\n"), "secrets should still be refreshed when the code is left alone")
        for a, b in (("2026-10-01-z", "2026-10-01-aa"), ("2026-10-01-aa", "2026-10-01-ab"), ("2026-09-30-zz", "2026-10-01-a")):
            o = sh("order() { echo \"$1\" | awk -F- '{ printf \"%s-%s-%s-%02d%s\", $1, $2, $3, length($4), $4 }'; }; "
                   f"printf '%s\\n%s\\n' \"$(order {b})\" \"$(order {a})\" | LC_ALL=C sort | head -1", tmp).stdout.strip()
            check(o.startswith(a[:10]) and o.endswith(a.split("-")[-1]), f"build order: {a} should come before {b} (got {o})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ok  update_github.sh: pushes code without touching history, refreshes secrets, survives a run saving mid-update, "
          "never puts older code over newer")


# ------------------------------------------------------------------------------------------------ 9. human-check pause
def gate_checks():
    """Sites are never paused or skipped; they are tried in the order the measured results suggest."""
    from autoapply.db import DB
    tmp = Path(tempfile.mkdtemp())
    try:
        db = DB(str(tmp / "g.db"))
        W = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver/Ops_R-%d"

        def add(src, n, status, reason, url=""):
            j = Job(src, f"co{n}", str(n), "Ops", "", url, url, "")
            db.add(j, status, score=80, reason=reason)
            db.update(j.key, attempts=1)
            return db.get(j.key)
        check(M.site_health(db) == {}, "no attempts yet: no measured health")
        lv = [add("lever", i, "blocked", "hCaptcha after submit") for i in range(3)]
        add("lever", 9, "blocked", "no application form found on page")
        wd = add("workday", 20, "applied", "confirmed", W % 20)
        add("workday", 21, "blocked", "not a real application form (0 fields, no résumé upload)", W % 21)
        gh = add("greenhouse", 30, "blocked", "the site asked for an emailed security code (its own human check)")
        bb = add("bamboohr", 40, "skipped", "stuck on step 3")
        h = M.site_health(db)
        check(h["lever"] == {"ok": 0, "human": 3, "other": 1} and h["workday"] == {"ok": 1, "human": 0, "other": 1}, f"site_health: {h}")
        check(M.site_rank(wd, h) == 0, "a site where applications go through is tried first")
        check(M.site_rank(lv[0], h) == 3, "a site that ended at a human check every time is tried last")
        check(M.site_rank(gh, h) == 2 and M.site_rank(bb, h) == 1, f"priors: greenhouse {M.site_rank(gh, h)}, bamboohr {M.site_rank(bb, h)}")
        check(M.site_rank(lv[0], {}) == 2 and M.site_rank(wd, {}) == 0, "with nothing measured the priors decide")
        add("lever", 50, "applied", "confirmed")
        add("lever", 51, "applied", "confirmed")
        check(M.site_rank(lv[0], M.site_health(db)) == 0, "once applications get through on a site it moves to the front")
        lines = M.health_lines(h)
        check(len(lines) == 1 and lines[0].startswith("lever:") and "3 recent tries" in lines[0], f"health_lines: {lines}")
        db.conn.execute("UPDATE jobs SET updated='2020-01-01T00:00:00'")
        check(M.site_health(db) == {}, "old results should not count")
        check(not hasattr(M, "site_paused") and not hasattr(M, "employer_blocked"), "no site or employer is paused any more")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for src, url, want in [("greenhouse", "", "greenhouse"), ("Lever", "", "lever"),
                           ("agg-themuse", "https://boards.greenhouse.io/acme/jobs/5", "greenhouse"),
                           ("agg-themuse", "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver/Ops_R-1", "workday"),
                           ("web", "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver/Ops_R-1", "workday"),
                           ("agg-remoteok", "https://careers.example.com/apply/9", "careers.example.com")]:
        check(M.ats_of(Job(src, "x", "1", "t", "", url, url, "")) == want, f"ats_of({src}, {url}) = {M.ats_of(Job(src, 'x', '1', 't', '', url, url, ''))}")
    print("ok  sites are ranked by measured results (finishers first, human-check sites last), none paused")


def match_checks():
    """The résumé match check that decides what is applied to (search.min_fit) and what waits or is set aside,
    the requeue of old Workday failures, and the submit-once record."""
    from types import SimpleNamespace
    from autoapply.db import DB
    from autoapply import prescreen as PS
    tmp = Path(tempfile.mkdtemp())
    try:
        db = DB(str(tmp / "m.db"))
        long_text = "Operations analyst role. " * 30
        s = {"prescreen": True, "min_fit": 70, "min_score_without_match": 80}

        class FakeWriter:
            def __init__(self, reply=None, up=True, boom=False):
                self.reply, self.up, self.boom, self.calls = reply, up, boom, 0

            def ready(self):
                return self.up

            def _complete(self, msgs, n, log, temperature=0.0, prefer=()):
                self.calls += 1
                if self.boom:
                    raise RuntimeError("free daily limit reached")
                return self.reply

        def job(n, score, desc=long_text, status="queued", fit=None):
            j = Job("workday", f"co{n}", str(n), "Operations Analyst", "Denver, CO", f"https://x/{n}", f"https://x/{n}", desc)
            db.add(j, status, score=score, reason="title +50")
            if fit is not None:
                db.update(j.key, fit=fit)
            return j, db.get(j.key)

        def brain(w):
            return SimpleNamespace(writer=w, profile={})
        j, r = job(1, 60)
        check(PS.gate(db, brain(FakeWriter('{"fit": 90, "why": "x"}')), j, r, {"prescreen": False}) == ("go", None, ""), "match check switched off: every queued job goes")
        w = FakeWriter('{"fit": 82, "why": "strong operations background"}')
        got = PS.gate(db, brain(w), j, r, s)
        row = db.get(j.key)
        check(got[0] == "go" and got[1] == 82 and row["fit"] == 82 and row["status"] == "queued" and row["reason"].startswith("match 82%: strong"),
              f"a match over the bar: {got} {dict(row)}")
        check(PS.gate(db, brain(w), j, row, s)[0] == "go" and w.calls == 1, "a job is only ever checked once (the stored match is used)")
        j, r = job(2, 60)
        got = PS.gate(db, brain(FakeWriter('{"fit": 55, "why": "retail role"}')), j, r, s)
        row = db.get(j.key)
        check(got[0] == "low" and row["status"] == "low_score" and row["fit"] == 55 and row["reason"] == "match 55%: retail role", f"a match under the bar: {got} {dict(row)}")
        j, r = job(3, 60, desc="short")
        check(PS.gate(db, brain(w), j, r, s)[0] == "page", "a posting whose text is not known yet must be read from its page first")
        check(PS.gate(db, brain(w), j, r, s, have_page=True)[0] == "later", "no posting text even on the page, modest keyword score: waits")
        j, r = job(4, 85, desc="short")
        check(PS.gate(db, brain(w), j, r, s, have_page=True)[0] == "go", "no posting text but a keyword score over 80: goes ahead")
        j, r = job(5, 60)
        check(PS.gate(db, brain(FakeWriter(up=False)), j, r, s)[0] == "later" and db.get(j.key)["status"] == "queued",
              "the free writer is out of allowance: the job waits for a later run and is not dropped")
        check(PS.gate(db, brain(FakeWriter(boom=True)), j, r, s)[0] == "later", "a match check that errors must not let the job through or drop it")
        check(PS.gate(db, brain(None), j, r, s)[0] == "later", "no writer at all: modest keyword score waits")
        j, r = job(6, 85)
        check(PS.gate(db, brain(FakeWriter(up=False)), j, r, s)[0] == "go", "no match check available but a keyword score over 80: goes ahead")
        j, r = job(7, 75, status="failed", fit=60)
        got = PS.gate(db, brain(w), j, r, s)
        check(got[0] == "low" and db.get(j.key)["status"] == "low_score", f"a retry whose stored match is under today's bar is set aside: {got} {db.get(j.key)['status']}")
        check(PS.gate(db, brain(FakeWriter("I think about 75 out of 100")), *job(8, 60), s)[1] == 75, "a reply that is not JSON still yields its number")

        # ---- submit-once: a job whose Submit was clicked is never queued again, whatever its status says
        j, r = job(20, 80)
        db.mark_submit(j.key, 1)
        check(db.get(j.key)["status"] == "unconfirmed" and db.get(j.key)["submitted_at"], "the Submit click must be written down before it happens")
        db.update(j.key, status="failed", reason="page crashed")
        check(j.key not in {x["key"] for x in db.retryable(3)}, "a job whose Submit was clicked came back for another try")
        db.update(j.key, status="queued", reason="recheck-inbox: no confirmation after submit")
        check(j.key in {x["key"] for x in db.retryable(3)}, "the inbox re-check is the one way back into the queue")
        j2, _ = job(21, 80)
        db.update(j2.key, status="failed", reason="the form was rejected, not sent", attempts=1, submitted_at="")
        check(j2.key in {x["key"] for x in db.retryable(3)}, "a submit the site itself rejected (nothing sent) may be tried again")

        # ---- a new build gives old Workday failures another go, but never one whose Submit was clicked
        W = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver/Ops_R-%d"

        def wd(n, status, reason, clicked=False, url=None):
            j = Job("workday", f"wd{n}", str(n), "Ops", "", url or W % n, url or W % n, "")
            db.add(j, status, score=80, reason=reason)
            db.update(j.key, attempts=2, **({"submitted_at": "2026-10-01T10:00:00"} if clicked else {}))
            return j.key
        a = wd(30, "skipped", "stuck on step 9; page says: nothing")
        b = wd(31, "failed", "took longer than 8 minutes on this site")
        c = wd(32, "unconfirmed", "submit clicked; outcome not yet known", clicked=True)
        d = wd(33, "blocked", "the site asked for an emailed security code (its own human check): left for you to finish by hand", url="https://boards.greenhouse.io/x/jobs/33")
        e = wd(34, "skipped", "stuck on step 3", url="https://jobs.lever.co/x/34")
        f = wd(35, "blocked", "account: Workday refused the sign-in")
        n = db.requeue_if_new_version("test-build-1")
        st = {k: (db.get(k)["status"], db.get(k)["attempts"]) for k in (a, b, c, d, e, f)}
        check(st[a] == ("queued", 0) and st[b] == ("queued", 0) and st[f] == ("queued", 0), f"old Workday failures should be tried again: {st}")
        check(st[c][0] == "unconfirmed" and st[d][0] == "blocked" and st[e][0] == "skipped", f"these must stay as they are: {st}")
        check(n >= 3 and db.requeue_if_new_version("test-build-1") == 0, "the requeue runs once per build")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ok  match check: 70% bar, checked once, waits when the writer is out, never lets a job through or drops it by mistake; "
          "submit-once record; old Workday failures requeued")


def open_form_checks():
    """Every common way a job link can be built must end on the real form."""
    import tempfile
    from pathlib import Path as P
    from playwright.sync_api import sync_playwright
    from autoapply import submit as S
    d = P(tempfile.mkdtemp())
    FORM = ('<form><input type=text name=a><input type=email name=b><input type=text name=c><textarea name=d></textarea>'
            '<input type=file name=f><button type=submit>Submit application</button></form>')
    (d / "form.html").write_text(f"<html><body><h1>Apply</h1>{FORM}</body></html>")
    (d / "direct.html").write_text(f"<html><body>{FORM}</body></html>")
    (d / "link.html").write_text('<html><body><h1>Ops Coordinator</h1><p>Great job</p><a href="form.html">Apply now</a></body></html>')
    (d / "popup.html").write_text('<html><body><h1>Job</h1><button onclick="window.open(\'form.html\')">Apply for this job</button></body></html>')
    (d / "reveal.html").write_text(f'<html><body><h1>Job</h1><button onclick="document.getElementById(\'f\').style.display=\'block\'">Apply</button>'
                                   f'<div id=f style="display:none">{FORM}</div></body></html>')
    (d / "iframe.html").write_text('<html><body><h1>Careers</h1><p>Join us</p><iframe src="form.html" width=800 height=900></iframe></body></html>')
    (d / "cookie.html").write_text('<html><body><div><button onclick="this.parentNode.remove()">Accept all</button></div>'
                                   '<h1>Job</h1><a href="form.html">Apply</a></body></html>')
    (d / "captcha.html").write_text('<html><body><h1>Job</h1><p>No form here and no apply button</p></body></html>')
    import functools, http.server, threading
    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context()
        for name in ("direct", "link", "popup", "reveal", "iframe", "cookie"):
            page = ctx.new_page()
            try:
                S.open_form(page, f"{base}/{name}.html")
                check(S._has_form(page), f"open_form: {name} did not end on the form")
            except Exception as e:
                check(False, f"open_form: {name} raised {type(e).__name__}: {str(e)[:80]}")
            page.close()
        page = ctx.new_page()
        try:
            S.open_form(page, f"{base}/captcha.html")
            check(False, "open_form: a page with no form must raise Blocked")
        except S.Blocked:
            pass
        page.close()
        b.close()
    srv.shutdown()
    check(S._rewrite_ats_url("https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000") == "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply", "lever /apply rewrite")
    check(S._rewrite_ats_url("https://apply.workable.com/acme/j/ABC123/") == "https://apply.workable.com/acme/j/ABC123/apply/", "workable /apply rewrite")


def direct_link_checks():
    from autoapply import aggregators as A, sources
    from autoapply.sources import Job
    old = dict(sources.FETCHERS)
    try:
        sources.FETCHERS["workable"] = lambda slug: ([Job("workable", slug, "c1", "Project Coordinator", "United States (Remote)", "u",
                                                        "https://apply.workable.com/smb-team/j/ABC/apply", "")] if slug == "smb-team" else [])
        sources.FETCHERS["greenhouse"] = lambda slug: []
        sources.FETCHERS["lever"] = lambda slug: []
        j = Job("agg-workablejobs", "smb-team", "x", "Project Coordinator ", "United States (Telecommute)", "u", "https://jobs.workable.com/view/abc", "",
                extra={"company_name": "SMB Team"})
        check(A.direct_apply_url(j, lambda *a: None) == "https://apply.workable.com/smb-team/j/ABC/apply", "employer posting not found by company name")
        j2 = Job("agg-workablejobs", "other-co", "y", "Project Coordinator", "x", "u", "https://jobs.workable.com/view/abd", "", extra={"company_name": "Other Co"})
        check(A.direct_apply_url(j2, lambda *a: None) is None, "found a posting for the wrong company")
    finally:
        sources.FETCHERS.clear(); sources.FETCHERS.update(old)


def location_checks():
    import yaml as _y
    from autoapply.sources import Job, prefilter
    c = _y.safe_load((ROOT / "config.yaml").read_text())
    sc = dict(c["search"]); sc["_onsite_ok"] = c["facts"]["relocation_ok_locations"]
    cases = [
        ("Belize (Remote)", True), ("Oman (Remote)", True), ("United Arab Emirates (Remote)", True), ("TELECOMMUTE, Mozambique (Remote)", True),
        ("TELECOMMUTE, Abu Dhabi, Abu Dhabi", True), ("Qatar (Remote)", True), ("Costa Rica (Remote)", True), ("TELECOMMUTE, Jamaica (Remote)", True),
        ("Remote - EMEA", True), ("Remote (LATAM)", True), ("Toronto, ON, CA", True), ("Remote, Canada", True), ("London, UK", True),
        ("Remote-Canada", True), ("TELECOMMUTE, Argentina", True), ("Remote, India", True), ("Vancouver, BC", True), ("Mexico City", True),
        ("Colorado (Remote)", False), ("California (Remote)", False), ("Denver, CO", False), ("Denver, Colorado, United States", False),
        ("United States (Remote)", False), ("Remote - US", False), ("Remote (US & Canada)", False), ("Remote", False), ("Anywhere", False),
        ("New York, NY", False), ("Jamaica, NY", False), ("Los Angeles, California, United States", False), ("United States", False),
        ("TELECOMMUTE, United States (Remote)", False), ("Remote - North America", False), ("New Mexico (Remote)", False), ("US-CO-Denver", False),
        ("Colorado Springs, Colorado, United States", False), ("Irvine, CA", False), ("Hybrid - New York", False), ("USA - Denver, CO", False),
        ("New York, New York, United States; Remote", False), ("Remote, US-based", False), ("United States (Telecommute)", False),
        # on-site outside the cities you'd move to
        ("Atlanta, Georgia, United States", True), ("Coppell, Texas, United States", True), ("San Francisco, CA", True), ("Washington, DC", True),
    ]
    for loc, bad in cases:
        got = prefilter(Job("greenhouse", "acme", "1", "Project Coordinator", loc, "", "", ""), sc)
        check(bool(got) == bad, f"location filter wrong for {loc!r}: {got!r}")
    titles = [("Structural Engineer (PE) - QA/QC Plan Reviewer", True), ("Operations Analyst, PE Fund Administration", False),
              ("Microsoft Dynamics 365 System Administrator", True), ("AI Engineer - SDLC Process Improvement", True), ("Exenta ERP Analyst", True),
              ("Workday Business Analyst", True), ("CNC Programmer", True), ("Operations Coordinator", False), ("Licensed Insurance Agent", True),
              ("Project Coordinator -Labor Compliance Analyst", False), ("Operations Analyst - Canada", True), ("Operations Coordinator (US & Canada)", False),
              ("Operations Associate, EMEA", True), ("Business Operations Associate", False)]
    for t, bad in titles:
        got = prefilter(Job("greenhouse", "acme", "1", t, "Denver, CO", "", "", ""), sc)
        check(bool(got) == bad, f"title filter wrong for {t!r}: {got!r}")
    from autoapply import aggregators as A
    api = {"jobs": [{"id": "a", "state": "published", "title": "Project Coordinator", "locations": ["TELECOMMUTE", "Argentina"], "workplace": "remote",
                     "location": {"city": "", "countryName": "Argentina"}, "company": {"title": "Pavago"}, "url": "u1", "description": "x"},
                    {"id": "b", "state": "published", "title": "Project Coordinator", "locations": ["TELECOMMUTE", "United States"], "workplace": "remote",
                     "location": {"city": "", "countryName": "United States"}, "company": {"title": "SMB Team"}, "url": "u2", "description": "x"}]}
    class _R:
        def json(self): return api
    old = A._get
    A._get = lambda *a, **k: _R()
    try:
        got = A.workablejobs({"aggregators": {"queries": ["project coordinator"], "locations": []}})
    finally:
        A._get = old
    check([j.job_id for j in got] == ["b"], f"Workable job board: only US-based remote jobs should stay, got {[j.job_id for j in got]}")
    print("ok  locations: other countries dropped, on-site only in your cities, remote/US-wide kept; licence/coding titles dropped")


def safety_checks():
    """Never double-submit, never a false 'applied'."""
    import re as _re
    from autoapply import submit as S
    from autoapply.main import norm_co
    from autoapply.db import DB
    check(norm_co("Acme, Inc.") == norm_co("ACME") == norm_co("The Acme Company"), "company names not normalised")
    check(not S.SUCCESS_RE.search("Thank you for your interest in Acme. Please fill out the form"), "pre-form thank-you counted as success")
    check(bool(S.SUCCESS_RE.search("Thank you for applying!")), "real confirmation missed")

    class P:                                            # a page that dies right after the click
        url = "http://x/apply"
        def inner_text(self, _sel):
            if getattr(self, "clicked", False):
                raise RuntimeError("Target page, context or browser has been closed")
            return "Apply now"
        def wait_for_timeout(self, ms): pass
        def locator(self, *a, **k): return self
        def count(self): return 1
        def filter(self, **k): return self
        @property
        def last(self): return self
        def click(self, **k): self.clicked = True
    calls = []
    try:
        S.submit(P(), on_click=lambda: calls.append(1))
        check(False, "post-click crash returned without Unconfirmed")
    except S.Unconfirmed:
        pass
    except Exception as e:
        check(False, f"post-click crash escaped as {type(e).__name__}")
    check(calls == [1], "write-ahead callback not called before click")
    import tempfile
    d = DB(tempfile.mktemp(suffix=".db"))
    from autoapply.sources import Job
    j = Job("greenhouse", "Acme", "1", "Ops", "Denver, CO", "", "", "")
    d.add(j, "unconfirmed")
    check(d.applied_today() == 1, "unconfirmed not counted against the daily cap")


def run_all() -> list[str]:
    for fn in (question_checks, fit_checks, source_checks, board_and_budget_checks, mail_checks, aggregator_checks, workflow_checks, script_checks, gate_checks, match_checks, safety_checks, direct_link_checks, open_form_checks, location_checks):
        try:
            fn()
        except Exception as e:                                        # a crash in one group must not hide the others
            import traceback
            problems.append(f"{fn.__name__} crashed: {type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")
    return problems


if __name__ == "__main__":
    out = run_all()
    print("RESULT:", "ALL AS EXPECTED" if not out else "PROBLEMS:\n  - " + "\n  - ".join(out))
    sys.exit(1 if out else 0)
