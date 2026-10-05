"""Checks for the Workday driver (autoapply/workday.py).

1. Real pages: the fixtures in tests/fixtures/workday are Workday application pages the bot captured on real employer
   sites on 2026-10-01 (personal details replaced). The driver must recognise each page and read its fields.
2. Whole applications against tests/wd_mock.py, a stand-in Workday site that behaves like the real one where it hurts:
   pop-up lists, redrawn boxes, label-only checkboxes, verify-your-email, forgot-password, required blocks behind 'Add',
   pages sent back with complaints.

Run on its own:  python tests/workday_checks.py      (also run by tests/unit_checks.py). No internet, nothing is sent."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import wd_mock                                                            # noqa: E402
import autoapply.writer as W                                              # noqa: E402
from autoapply.brain import Brain                                        # noqa: E402
from autoapply.sources import Job                                         # noqa: E402
from autoapply import submit as S, auth as A, mailbox as MB, workday as WD   # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "workday"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
PROFILE = yaml.safe_load((ROOT / "profile.yaml").read_text())
problems: list[str] = []
TMP = Path(tempfile.mkdtemp())
W._USAGE_FILE = TMP / "usage.json"


def check(cond, msg):
    if not cond:
        problems.append(msg)


def helper_checks():
    check(WD.posting_url("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver-CO/Ops_R-1/apply/applyManually?source=x")
          == "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver-CO/Ops_R-1", "posting_url should drop /apply and the query")
    check(WD.kind({}) == "unknown" and WD.kind({"error": "The page you are looking for doesn't exist."}) == "closed", "kind(): closed page")
    check(WD.kind({"flow": True, "signin": True}) == "auth" and WD.kind({"flow": True, "next": "Next"}) == "form"
          and WD.kind({"flow": True}) == "flow" and WD.kind({"start": True}) == "start" and WD.kind({"applyButton": True}) == "job"
          and WD.kind({"already": "You applied"}) == "already", "kind(): page types")
    terms = S._search_terms("Industrial and Systems Engineering")
    check(terms[0] == "Industrial and Systems Engineering" and "Industrial Engineering" in terms and terms[-1] == "Other", f"search terms: {terms}")
    fields = [{"id": "a", "label": "First Name*", "key": "formField-legalName--firstName"},
              {"id": "b", "label": "How Did You Hear About Us?*", "key": "formField-source"},
              {"id": "c", "label": "City*", "key": "formField-city"}]
    got = WD._match_flagged(fields, {"fields": [{"key": "formField-source", "label": "How Did You Hear About Us?"}],
                                     "messages": ["Error: The field First Name is required and must have a value."]})
    check(got == {"a", "b"}, f"flagged fields: {got}")
    mail_checks()
    # matching a wanted value to the choices of a list: whole words, and a short value never by sitting inside a sentence
    sponsor = ["Yes, I will now or in the future require sponsorship", "No, I will not now or in the future require sponsorship"]
    for want, opts, exp in (("No", sponsor, 1), ("Yes", sponsor, 0), ("Male", ["Female", "Male"], 1), ("Male", ["Female", "Non-binary"], None),
                            ("Colorado", ["California", "Colorado (CO)"], 1), ("United States of America", ["Canada", "United States"], 1),
                            ("Bachelor's Degree", ["Associate's Degree", "Bachelor's Degree"], 1), ("No", ["Not sure", "Unknown"], None),
                            ("I am not a protected veteran", ["I am a protected veteran", "I am not a protected veteran"], 1)):
        check(S._match_option(opts, want) == exp, f"_match_option({want!r}, {opts}) = {S._match_option(opts, want)}, expected {exp}")
    check(S._best_option(["Select One", "Female", "Male"], "Male") == 2 and S._best_option(["Select One", "Female"], "Male") is None, "_best_option picked a look-alike")
    check(all(S.PLACEHOLDER.match(x) for x in ("Select One", "Select...", "Select an option", "-- Select --", "Choose one", "", "None selected"))
          and not any(S.PLACEHOLDER.match(x) for x in ("Colorado", "Selective Service: registered", "No", "Mobile")), "placeholder texts of an unanswered list")
    check(S._server_said([("POST", 422, "/apply", "email is required", "jobs.example.com")], "jobs.example.com").startswith("REJECTED")
          and not S._server_said([("POST", 200, "/apply", "", "jobs.example.com"), ("POST", 429, "/track", "slow down", "jobs.example.com")], "jobs.example.com").startswith("REJECTED")
          and not S._server_said([("POST", 400, "/collect", "bad", "tracker.example.net")], "jobs.example.com").startswith("REJECTED"),
          "'the site refused it' must mean the form's own site refused and accepted nothing else")
    print("ok  Workday helpers: page types, posting address, search terms, matching Workday's complaints to fields, "
          "picking the right email (this employer's, verify vs. reset)")


def mail_checks():
    """The inbox holds several employers' emails at once. The bot must take this employer's, and never mistake a
    password-reset email for a verify-your-account email (or the other way round)."""
    import types
    now = time.time()

    def msg(sender, subject, link, age_s):
        return {"from": sender, "subject": subject, "link": link, "code": None, "hint": True, "ts": now - age_s, "body": ""}
    saks_verify = msg("Saks <saks@myworkday.com>", "Verify your candidate account", "https://saks.wd1.myworkdayjobs.com/Saks/activate/abc?redirect=%2Fapply", 30)
    br_verify = msg("BlackRock <blackrock@myworkday.com>", "Verify your candidate account",
                    "https://blackrock.wd1.myworkdayjobs.com/BlackRock/activate/xyz?redirect=%2Fapply", 60)
    br_reset = msg("BlackRock <blackrock@myworkday.com>", "Reset your password", "https://blackrock.wd1.myworkdayjobs.com/BlackRock/passwordreset/r1", 20)
    generic_old = msg("Careers <no-reply@careers-mail.example>", "Please verify your email", "https://careers-mail.example/v/1", 3 * 3600)
    generic_new = msg("Careers <no-reply@careers-mail.example>", "Please verify your email", "https://careers-mail.example/v/2", 15)
    old_recent, old_time = MB._recent, MB.time
    MB.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)
    try:
        def ask(inbox, **k):
            MB._recent = lambda since, n=12, folders=(): iter(inbox)
            return MB.wait_for_verification(since_ts=now - 2 * 86400, timeout=0.3, log=lambda *a: None, **k)
        host = "blackrock.wd1.myworkdayjobs.com"
        got = ask([br_reset, saks_verify, br_verify], host_hint="blackrock", kind="verify", site_host=host)
        check(got and got["link"] == br_verify["link"], f"verify email: should take this employer's verify link, got {got}")
        got = ask([saks_verify, br_reset, br_verify], host_hint="blackrock", kind="reset", site_host=host)
        check(got and got["link"] == br_reset["link"], f"reset email: should take this employer's reset link, got {got}")
        got = ask([saks_verify], host_hint="blackrock", kind="verify", site_host=host)
        check(got is None, f"another employer's Workday email was taken for this one's: {got}")
        got = ask([generic_old], host_hint="acme", kind="verify", site_host="careers.acme.com")
        check(got is None, f"an hours-old email that does not name the site was accepted: {got}")
        other = msg("PCG <noreply@mail.example>", "Verify your candidate account", "https://click.mail.example/t/abc", 85)
        got = ask([other], host_hint="avalonbay", kind="verify", site_host="avalonbay.wd5.myworkdayjobs.com")
        check(got is None, f"an email sent 85 seconds ago for another employer was taken for this one's: {got}")
        got = ask([generic_new], host_hint="acme", kind="verify", site_host="careers.acme.com")
        check(got and got["link"] == generic_new["link"], f"a just-arrived verification email from a generic sender should be accepted, got {got}")
    finally:
        MB._recent, MB.time = old_recent, old_time
    check(MB._mentions("ms", {"from": "Morgan Stanley <ms@myworkday.com>", "subject": "Verify your candidate account", "link": None})
          and not MB._mentions("ms", {"from": "Forms <forms@example.com>", "subject": "Your items", "link": "https://example.com/terms"}),
          "a two-letter site name must match as a whole word only")


def fixture_checks(ctx):
    """Real Workday pages: what kind of page it is, and what is on it."""
    page = ctx.new_page()

    def load(name):
        page.set_content((FIX / name).read_text(encoding="utf-8"))
        return page.evaluate(WD.STATE_JS)

    st = load("my_information.html")
    check(WD.kind(st) == "form" and st["name"] == "My Information" and st["index"] == 0 and len(st["steps"]) == 5 and st["next"] == "Next"
          and st["page"] == "applyFlowMyInfoPage", f"My Information state: {st}")
    fields = S.extract(page)
    by = {f["label"].rstrip("*").strip(): f for f in fields}
    src = by.get("How Did You Hear About Us?") or {}
    check(src.get("kind") == "wdprompt" and src.get("sel") == '[id="source--source"]' and src.get("selected") == ["Indeed.com"]
          and src.get("key") == "formField-source", f"'How did you hear' box: {src}")
    for lab, cur in (("Country", "United States of America"), ("State", "Colorado"), ("Phone Device Type", "Mobile")):
        f = by.get(lab) or {}
        check(f.get("kind") == "combobox" and f.get("button") and f.get("cur") == cur and f.get("options") == [] and S._already_answered(f),
              f"dropdown {lab}: {f}")
    check(sum(1 for f in fields if f["label"].startswith(("Country*", "State*", "Phone Device Type*"))) == 3, "the value holders next to dropdowns were read as fields")
    fn = by.get("First Name") or {}
    check(fn.get("kind") == "text" and fn.get("sel") == '[id="name--legalName--firstName"]' and fn.get("has_value"), f"First Name: {fn}")
    rad = next((f for f in fields if f["kind"] == "radio"), {})
    check(rad.get("option_sels") == ['input[type="radio"][name="candidateIsPreviousWorker"][value="true"]',
                                     'input[type="radio"][name="candidateIsPreviousWorker"][value="false"]'] and rad.get("checked") == ["No"]
          and rad.get("required"), f"previous-worker question: {rad}")
    check(not any(f["label"] in ("", "English") or "Sign In" in f["label"] for f in fields), "header controls (language, account) were read as form fields")
    check(any("text messages" in f["label"] for f in fields), "the SMS opt-in box has no usable label")
    pc = by.get("Country Phone Code") or {}
    check(pc.get("selected") == ["United States of America (+1)"], f"phone code box: {pc}")
    check(page.evaluate(S._OPTIONS_JS, []) == [], "the 'already selected' chips were read as choices of an open list")

    st = load("my_experience.html")
    check(WD.kind(st) == "form" and st["name"] == "My Experience" and st["next"] == "Save and Continue" and st["done"] == 1, f"My Experience state: {st}")
    fields = S.extract(page)
    up = next((f for f in fields if f["kind"] == "file"), {})
    check(up.get("label", "").startswith("Resume/Cover Letter") and up.get("has_file") == ["jordansample_resume.pdf"], f"résumé box: {up}")
    # the real résumé box is headed 'Resume/Cover Letter': the résumé must go into it (not a cover letter, not nothing)
    cfg0 = json.loads(json.dumps(CFG))
    cfg0["cover_letters"] = False
    b0 = Brain(cfg0, PROFILE, ROOT, lambda *a: None)
    b0.writer = None
    plan = b0.map_fields(Job("workday", "x", "1", "Operations Analyst", "", "u", "u", ""), fields, "")
    check(plan["answers"].get(up.get("id")) == "RESUME", f"the 'Resume/Cover Letter' box should get the résumé, got {plan['answers'].get(up.get('id'))!r}")
    cfg0["cover_letters"] = True
    b1 = Brain(cfg0, PROFILE, ROOT, lambda *a: None)
    b1.writer = None
    check(b1.map_fields(Job("workday", "x", "1", "Operations Analyst", "", "u", "u", ""), fields, "")["answers"].get(up.get("id")) == "RESUME",
          "with cover letters switched on, the 'Resume/Cover Letter' box must still get the résumé")
    secs = page.evaluate(WD.SECTIONS_JS)
    check([s["name"] for s in secs] == ["Work Experience", "Education", "Languages", "Websites/Portfolio"] and not any(s["required"] or s["rows"] for s in secs),
          f"sections behind 'Add': {secs}")

    st = load("application_questions.html")
    check(WD.kind(st) == "form" and st["name"] == "Application Questions" and st["page"] == "applyFlowPrimaryQuestionsPage", f"Application Questions state: {st}")
    fields = page.evaluate(S.EXTRACT_JS)
    dds = [f for f in fields if f["kind"] == "combobox"]
    check(len(dds) == 6 and all(f["cur"] == "Select One" and f["required"] and f["sel"].startswith('[id="primaryQuestionnaire--') for f in dds), f"question dropdowns: {dds}")
    check(any(f["kind"] == "textarea" and "name(s) of the" in f["label"] for f in fields) and any(f["kind"] == "text" and "salary expectations" in f["label"] for f in fields),
          "the text questions were not read")
    check(len(fields) == 8, f"Application Questions should have 8 fields, got {len(fields)}: {[f['label'][:30] for f in fields]}")

    st = load("start.html")
    check(WD.kind(st) == "start" and st["manual"].endswith("/apply/applyManually"), f"Start Your Application: {st}")
    st = load("signin_landing.html")
    check(WD.kind(st) == "auth" and st["emailButton"] and not st["passwords"] and st["name"] == "Create Account/Sign In" and len(st["steps"]) == 8, f"sign-in landing: {st}")
    check(page.evaluate(S._OPTIONS_JS, []) == [] and not S.extract(page), "the site's language menu in the page header was read as part of the application")
    st = load("closed.html")
    check(WD.kind(st) == "closed", f"closed posting: {st}")

    # failure snapshots say what is on a page (questions, which boxes are empty or marked wrong), never the answers
    from autoapply import snapshot as SN
    for name in ("my_information.html", "my_experience.html"):
        load(name)
        o = SN.scrub(SN.outline(page), {"full_name": "Jordan Sample", "first_name": "Jordan", "last_name": "Sample"})
        shown = [w for w in ("Indeed", "Colorado", "United States of America", "Mobile", "(+1)", "Jordan", "jordan", "Sample", "@", ".pdf") if w in o]
        check(not shown, f"the snapshot of {name} shows an answer or a personal detail: {shown}")
    load("my_information.html")
    o = SN.outline(page)
    check("How Did You Hear About Us?" in o and "[dropdown] chosen" in o and "question-answered" in o and "required filled" in o
          and "{pageFooterNextButton}" in o and "(a choice is selected)" in o, f"the snapshot lost what is needed to debug a page: {o[:600]}")
    page.set_content('<div data-automation-id="applyFlowPage"><div data-automation-id="applyFlowReviewPage"><h3>Voluntary Disclosures</h3>'
                     '<p>I am not a protected veteran, as shown on your review page</p><div role="alert">Error: Postal Code is required.</div></div>'
                     '<button data-automation-id="pageFooterNextButton">Submit</button></div>')
    o = SN.outline(page)
    check("protected veteran" not in o and "Postal Code is required" in o and "Submit" in o, f"the Review page's snapshot must hold complaints and buttons, not your answers: {o}")
    page.close()
    print("ok  real Workday pages: page type, step, fields, dropdown values, sections behind 'Add' all read correctly; "
          "failure snapshots hold no answers or personal details")


def flow_checks(p):
    cfg = json.loads(json.dumps(CFG))
    cfg["cover_letters"] = False
    brain = Brain(cfg, PROFILE, ROOT, lambda *a: None)
    brain.writer = None
    srv, base, state = wd_mock.serve()
    now = {"tenant": ""}
    olds = (MB.configured, MB.wait_for_verification)
    MB.configured = lambda: True

    def wait(**k):
        box = [m for m in state.outbox if m["tenant"] == now["tenant"]]
        return {"link": box[-1]["link"], "code": None} if box else None
    MB.wait_for_verification = wait
    pdf = ROOT / "briandelgado_resume.pdf"
    b = p.chromium.launch()

    def run(tenant, req="R-100", dry=False, ctx=None, acc=None, seed=False):
        now["tenant"] = tenant
        own = ctx is None
        if own:
            ctx = b.new_context(viewport={"width": 1280, "height": 1800})
            ctx.set_default_timeout(10000)
        if acc is None:
            acc = A.Accounts({"accounts": {"email": "delgado@alumni.usc.edu"}}, Path(tempfile.mkdtemp()))
            acc.password, acc.enabled = "Pw-123456!x", True
        url = wd_mock.job_url(base, tenant, req)
        job = Job("workday", tenant, req, "Operations Analyst", "Denver, CO", url, url, "Entry-level operations analyst. 0-2 years.")
        logs, stages, clicks = [], [], []
        page = ctx.new_page()
        t0 = time.time()
        try:
            if seed:
                page.goto(url + "?seed=1")
            S.open_form(page, url) if "myworkdayjobs.com" in url else WD.open_posting(page, url)
            got = S.apply(page, job, brain, "", {"RESUME": pdf, "_LETTER_MAKER": lambda t: None}, TMP / f"{tenant}.png", dry, logs.append, acc,
                          on_click=lambda: clicks.append(1), on_stage=stages.append)
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        took = time.time() - t0
        page.close()
        sent = [s for s in state.submitted if s["tenant"] == tenant and s["job"].endswith(req)]
        out = {"got": got, "logs": logs, "stages": stages, "clicks": clicks, "took": took, "data": sent[-1]["data"] if sent else None, "ctx": ctx, "acc": acc}
        if own:
            out["ctx"] = None
            ctx.close()
        return out

    def tail(r):
        return " | ".join(r["logs"])[-600:]

    try:
        # -- a plain application, new account
        r = run("acme")
        d = r["data"] or {}
        check(r["got"] == "confirmed" and len(r["clicks"]) == 1, f"plain Workday application: {r['got']}; log: {tail(r)}")
        F = CFG["facts"]                                       # expected values come from your own facts, not from this file
        want = {"name--legalName--firstName": F["first_name"], "name--legalName--lastName": F["last_name"], "address--city": F["city"],
                "address--countryRegion": "Colorado", "address--postalCode": str(F["zip"]), "phoneNumber--phoneType": "Mobile",
                "candidateIsPreviousWorker": "false", "primaryQuestionnaire--q1": "Yes", "primaryQuestionnaire--q2": "No",
                "primaryQuestionnaire--q3": "Bachelor's Degree", "primaryQuestionnaire--q4": "No", "personalInfoUS--veteranStatus": "I am not a protected veteran",
                "termsAndConditions--acceptTermsAndAgreements": True, "disab": 1, "files": ["briandelgado_resume.pdf"]}
        check(all(d.get(k) == v for k, v in want.items()), f"submitted values: { {k: d.get(k) for k, v in want.items() if d.get(k) != v} }")
        check((d.get("source--source") or [""])[0] in ("LinkedIn", "Indeed.com", "Glassdoor"), f"'how did you hear' should be a job board from the sub-list: {d.get('source--source')}")
        check(d.get("phoneNumber--countryPhoneCode") == ["United States of America (+1)"], f"phone code changed: {d.get('phoneNumber--countryPhoneCode')}")
        dt = d.get("selfIdentifiedDisabilityData--dateSignedOn") or {}
        check(dt.get("m") == time.strftime("%m") and dt.get("d") == time.strftime("%d") and dt.get("y") == time.strftime("%Y"), f"signature date: {dt}")
        check(r["stages"] == ["account", "My Information", "My Experience", "Application Questions", "Voluntary Disclosures", "Self Identify", "Review"], f"stages: {r['stages']}")
        check(not any("could not" in x or "NOT FILLED" in x or "(try " in x for x in r["logs"]), f"the plain flow should need no repairs: {tail(r)}")
        check(r["took"] < 75, f"a plain Workday application took {r['took']:.0f}s (it should be well under 75s)")
        check(not d.get("workRows") and not d.get("eduRows"), "optional Work Experience / Education blocks should be left to the résumé")

        # -- a second job at the same employer in the same browser: already signed in, everything remembered, no second résumé copy
        ctx = b.new_context(viewport={"width": 1280, "height": 1800})
        ctx.set_default_timeout(10000)
        r1 = run("acme2", ctx=ctx)
        r2 = run("acme2", req="R-101", ctx=ctx, acc=r1["acc"])
        check(r1["got"] == "confirmed" and r2["got"] == "confirmed", f"second job at the same employer: {r1['got']} / {r2['got']}; log: {tail(r2)}")
        check((r2["data"] or {}).get("files") == ["briandelgado_resume.pdf"], f"résumé uploaded twice: {(r2['data'] or {}).get('files')}")
        check("account" not in r2["stages"] and r2["took"] < r1["took"], f"second application should skip the account step and be quicker ({r1['took']:.0f}s then {r2['took']:.0f}s)")
        ctx.close()

        # -- accounts: verify-by-email, 'sign in with email' first, an account that exists with another password
        r = run("verifyco")
        check(r["got"] == "confirmed" and any("verification" in x for x in r["logs"]), f"verify-your-email account: {r['got']}; log: {tail(r)}")
        r = run("emailfirst")
        check(r["got"] == "confirmed", f"'Sign in with email' landing: {r['got']}; log: {tail(r)}")
        r = run("brock")
        check(r["got"] == "confirmed" and any("verify link" in x for x in r["logs"]),
              f"account that must be verified, with the sign-in choice page coming back after every step: {r['got']}; log: {tail(r)}")
        check((r["data"] or {}).get("files") == ["briandelgado_resume.pdf"], f"a résumé box headed 'Resume/Cover Letter' did not get the résumé: {(r['data'] or {}).get('files')}")
        r = run("resetco", seed=True)
        check(r["got"] == "confirmed" and any("password reset" in x for x in r["logs"]), f"existing account, other password: {r['got']}; log: {tail(r)}")
        r = run("quietco", seed=True)
        check(r["got"] == "confirmed" and any("password reset" in x for x in r["logs"]),
              f"sign-in refused with no message at all: the password must still be reset: {r['got']}; log: {tail(r)}")

        # -- required blocks behind 'Add' (marked in the heading, or only said after Next)
        for tenant in ("strict", "strict2"):
            r = run(tenant)
            d = r["data"] or {}
            check(r["got"] == "confirmed", f"{tenant}: {r['got']}; log: {tail(r)}")
            exp = (PROFILE.get("experience") or [{}])[0]
            check(d.get("workExperience-1--jobTitle") == exp.get("title") and d.get("workExperience-1--companyName") == exp.get("company"),
                  f"{tenant}: work experience should be your real most recent job, got {d.get('workExperience-1--jobTitle')!r} at {d.get('workExperience-1--companyName')!r}")
            check((d.get("workExperience-1--startDate") or {}).get("y", "").isdigit(), f"{tenant}: work dates missing: {d.get('workExperience-1--startDate')}")
            check(d.get("education-1--school") == ["University of Southern California"] and d.get("education-1--degree") == "Bachelor's Degree",
                  f"{tenant}: education: {d.get('education-1--school')} / {d.get('education-1--degree')}")
            check(d.get("education-1--fieldOfStudy") == ["Industrial Engineering"], f"{tenant}: field of study should fall back to the closest listed one: {d.get('education-1--fieldOfStudy')}")
            check(not d.get("education-1--gradeAverage"), f"{tenant}: an optional GPA box should be left empty")

        # -- your school is not in the employer's list: 'Other', never a school with a similar name
        r = run("strict3")
        d = r["data"] or {}
        check(r["got"] == "confirmed" and d.get("education-1--school") == ["Other"],
              f"school missing from the list: expected 'Other', got {d.get('education-1--school')} ({r['got']}); log: {tail(r)}")

        # -- a page sent back by the server with only a banner; an empty phone-code box
        r = run("bounce")
        check(r["got"] == "confirmed" and any("(try 2)" in x and "Something went wrong" in x for x in r["logs"]), f"page sent back once: {r['got']}; log: {tail(r)}")
        r = run("nophone")
        check(r["got"] == "confirmed" and (r["data"] or {}).get("phoneNumber--countryPhoneCode") == ["United States of America (+1)"],
              f"empty phone code: {r['got']} {(r['data'] or {}).get('phoneNumber--countryPhoneCode')}")

        # -- stops that are not failures
        r = run("closed")
        check(r["got"].startswith("Blocked: posting closed"), f"closed posting: {r['got']}")
        r = run("applied")
        check(r["got"] == "already_applied", f"already applied: {r['got']}")
        r = run("dryco", dry=True)
        check(r["got"] == "dry_run" and not r["clicks"] and r["data"] is None and r["stages"][-1:] == ["account"], f"dry run must stop at the account screen: {r['got']} {r['stages']}")

        # -- the per-application time cap also stops a Workday application
        old = S.MAX_APPLY_SECONDS
        S.MAX_APPLY_SECONDS = -1
        try:
            r = run("capco")
            check("took longer" in r["got"] and r["data"] is None, f"time cap: {r['got']}")
        finally:
            S.MAX_APPLY_SECONDS = old
    finally:
        MB.configured, MB.wait_for_verification = olds
        b.close()
        srv.shutdown()
    print("ok  Workday applications end to end: accounts (new, verify by email, forgot password), pop-up lists, redrawn pages, "
          "required blocks, pages sent back, second job at one employer, closed / already applied / dry run / time cap")


def run_all() -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("skip Workday checks (no playwright)")
        return problems
    for fn in (helper_checks,):
        try:
            fn()
        except Exception as e:
            problems.append(f"{fn.__name__} crashed: {type(e).__name__}: {e}")
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1280, "height": 1800})
        try:
            fixture_checks(ctx)
        except Exception as e:
            import traceback
            problems.append(f"fixture_checks crashed: {type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")
        b.close()
        try:
            flow_checks(p)
        except Exception as e:
            import traceback
            problems.append(f"flow_checks crashed: {type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")
    return problems


if __name__ == "__main__":
    run_all()
    print("RESULT:", "ALL AS EXPECTED" if not problems else "PROBLEMS:\n  - " + "\n  - ".join(problems))
    sys.exit(1 if problems else 0)
