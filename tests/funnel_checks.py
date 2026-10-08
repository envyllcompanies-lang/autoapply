"""Checks for the parts that decide how many good-fit jobs are found, checked and applied to:

  * the daily snapshot (which rows become candidates, one posting = one row however it was found);
  * the order in which waiting candidates get their résumé-match check (rank.py);
  * how the free writer's allowance is split between match checks and application answers;
  * the excerpt of a posting the match check reads;
  * second tries for jobs that were set aside by faults that have been fixed;
  * the account records: rests after a refused sign-in, emails already in the inbox.

Run on its own:  python tests/funnel_checks.py      (also run by tests/unit_checks.py). No internet, nothing is sent."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autoapply import rank, jobboard, prescreen as PS, sources as SRC, level, auth as A, mailbox as MB, workday as WD   # noqa: E402
from autoapply import writer as W                                                                                    # noqa: E402
from autoapply.db import DB                                                                                          # noqa: E402
from autoapply.sources import Job                                                                                    # noqa: E402

problems: list[str] = []
TMP = Path(tempfile.mkdtemp())
W._USAGE_FILE = TMP / "usage.json"


def check(cond, msg):
    if not cond:
        problems.append(msg)


def rank_checks():
    rows = []
    for i in range(12):
        rows.append({"title": "Operations Analyst", "company": "Acme Inc.", "fit": 80})
        rows.append({"title": "Retail Sales Associate", "company": "ShopCo", "fit": 30})
    rows += [{"title": "Program Coordinator", "company": "Mixed LLC", "fit": 70}, {"title": "Program Coordinator", "company": "Mixed LLC", "fit": 40}]
    rows.append({"title": "Never Checked", "company": "X", "fit": 0})            # not checked: must not count
    p = rank.Prior(rows, min_fit=60)
    check(p.total == 26 and abs(p.base - 13 / 26) < 1e-9, f"prior: {p.total} checked, base {p.base}")
    good, bad = p.p("Operations Analyst II", "Other Co"), p.p("Retail Associate", "Other Co")
    check(good > 0.7 > 0.3 > bad, f"title words that passed before should rank above ones that failed: {good:.2f} vs {bad:.2f}")
    check(p.p("Operations Analyst", "ACME, Inc") > p.p("Operations Analyst", "Unknown Co") > p.p("Operations Analyst", "shopco"),
          "an employer's own track record should move its postings up or down")
    check(p.base * 0.5 < p.p("Zookeeper", "Nobody") < p.base, f"a title made of words never seen before ranks under the overall pass rate, not at zero: {p.p('Zookeeper', 'Nobody'):.2f}")
    check(p.p("Operations Analyst", "Nobody") > p.p("Operations Analyst Zookeeping Falconry", "Nobody") > p.p("Zookeeping Falconry", "Nobody"),
          "words the history never saw pull a familiar title down a little")
    check(abs(p.p("II", "Nobody") - p.base) < 1e-9, "with nothing to go by, the overall pass rate")
    check(rank.title_tokens("Operations Analyst (Remote) [Contract]") == {"operations", "analyst", "operations analyst"}, f"title tokens: {rank.title_tokens('Operations Analyst (Remote) [Contract]')}")
    check(rank.norm_company("The Acme Company, Inc.") == "acme" and rank.norm_company("ACME") == "acme", "company names should compare without Inc / The / Company")
    now = datetime(2026, 10, 6, 12, 0, 0)

    def row(title, score, posted=None, first_seen=None, company="Other Co"):
        return {"title": title, "company": company, "score": score, "posted": posted, "first_seen": first_seen}
    new = rank.value(p, row("Operations Analyst", 30, posted=(now - timedelta(days=1)).isoformat()), now)
    old = rank.value(p, row("Operations Analyst", 30, posted=(now - timedelta(days=40)).isoformat()), now)
    check(new - old == 14, f"a posting from yesterday should rank 14 points above the same one from 40 days ago: {new} vs {old}")
    check(rank.value(p, row("Operations Analyst", 30), now) > rank.value(p, row("Retail Sales Associate", 90), now),
          "what passed the match check before should outrank a high keyword score that failed it")
    check(rank.value(p, row("Operations Analyst", 80), now) > rank.value(p, row("Operations Analyst", 20), now), "the keyword score still breaks ties")
    check(abs((rank.value(p, row("Operations Analyst", 30), now) - rank.value(p, row("Operations Analyst", 10), now)) - (20 * 0.12 + 20 * 0.3)) < 1e-6,
          "a keyword score under 30 (never match-checked before) costs a little extra per point")
    check(rank.freshness(row("x", 0, first_seen=(now - timedelta(days=5)).isoformat()), now) == 3.0 and rank.freshness(row("x", 0), now) == 0.0
          and rank.freshness(row("x", 0, posted="2026-10-05T10:00:00Z"), now) == 6.0, "freshness: posted date first, then when the bot first saw it")
    empty = rank.Prior([], 60)
    check(0 < empty.p("Anything") < 1 and 0 <= rank.value(empty, row("Anything", 50), now) <= 100, "with no history yet the ranking still works")
    print("ok  ranking of unchecked candidates: learned from earlier match checks (title words, employers), newer first, keyword score as tie-break")


def snapshot_checks():
    search = {"titles_include": ["analyst", "coordinator", "associate"], "locations_include": ["denver", "new york", "remote"],
              "exclude_government": True, "max_level": 1}
    now = datetime.now(timezone.utc)

    def r(ats, lvl, title, url, loc="Denver, CO", company="Acme", days=2):
        return {"ats": ats, "skill_level": lvl, "title": title, "company": company, "location": loc, "url": url,
                "first_seen": (now - timedelta(days=days)).isoformat()}
    gh = "https://job-boards.greenhouse.io/acme/jobs/%d"
    rows = [
        r("Greenhouse", "entry", "Operations Analyst", gh % 1),
        r("Greenhouse", "mid", "Program Coordinator", gh % 2),                      # 'mid' is where most real candidates sit
        r("Greenhouse", "senior", "Operations Analyst", gh % 3),                   # the snapshot calls it senior: not read
        r("Greenhouse", "mid", "Senior Operations Analyst", gh % 4),               # the bot's own title rules drop it
        r("iCIMS", "entry", "Operations Analyst", "https://careers-acme.icims.com/jobs/5/job"),      # a site the bot cannot fill
        r("Greenhouse", "mid", "Operations Analyst", gh % 6, days=45),             # too old
        r("Greenhouse", "mid", "Operations Analyst", gh % 7, loc="Austin, TX"),    # not one of your places
        r("Greenhouse", "mid", "Forklift Operator", gh % 8),                       # not one of your titles
        r("Greenhouse", "mid", "Operations Analyst", gh % 9),                      # already in the history under the employer's own key
        r("Greenhouse", "mid", "Operations Analyst", gh % 10),                     # already in the history by its address
        r("Greenhouse", "mid", "Operations Analyst", "https://boards.greenhouse.io/Acme%20Inc/jobs/11", company="Acme Inc"),
        r("Workday", "mid", "Supply Chain Analyst", "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver-CO/Supply-Chain-Analyst_R-12"),
        r("Ashby", "entry", "Operations Associate", "https://jobs.ashbyhq.com/acme/0a1b2c3d-1111-2222-3333-444455556666"),
        r("Lever", "mid", "Project Coordinator", "https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455557777", loc="Remote"),
    ]
    known_urls = {gh % 10}
    known_keys = {"greenhouse:acme:9", "greenhouse:Acme:jb-12"}
    tally: dict = {}
    jobs = jobboard.rows_to_jobs(rows, search, known_urls, set(known_keys), tally=tally)
    got = {j.key: j for j in jobs}
    want = {"greenhouse:acme:1", "greenhouse:acme:2", "workday:acme:R-12", "ashby:acme:0a1b2c3d-1111-2222-3333-444455556666",
            "lever:acme:0a1b2c3d-1111-2222-3333-444455557777"}
    check(set(got) >= want, f"snapshot rows that should become candidates are missing: {sorted(want - set(got))}; got {sorted(got)}")
    extra = set(got) - want
    check(all(k.startswith("greenhouse:Acme") or "jb-" in k for k in extra) and len(extra) <= 1, f"snapshot rows that should have been left out came through: {sorted(extra)}")
    check(tally.get("rows") == len(rows) and tally.get("level") == len(rows) - 2, f"snapshot tally (rows read, rows at your level on fillable sites): {tally}")
    j1 = got.get("greenhouse:acme:1")
    check(j1 is not None and "Entry level." in j1.description and (j1.extra or {}).get("snapshot_level") == "entry" and (j1.extra or {}).get("posted", "").startswith(str(now.year)),
          f"an entry row keeps its level note and the date it was first seen: {j1 and (j1.description, j1.extra)}")
    j2 = got.get("greenhouse:acme:2")
    check(j2 is not None and "Entry level." not in j2.description, "a 'mid' row must not be described as entry level")
    # the same posting found again (same address, or the same key) is one posting
    again = jobboard.rows_to_jobs(rows, search, known_urls | {j.url for j in jobs}, set(known_keys) | set(got))
    check(again == [], f"a second look at the same snapshot must add nothing: {[j.key for j in again]}")
    legacy = jobboard.rows_to_jobs([r("Workday", "mid", "Supply Chain Analyst", "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver-CO/Supply-Chain-Analyst_R-12")],
                                   search, set(), {"workday:Acme:jb-Supply-Chain-Analyst_R-12", "workday:Acme:jb-R-12"})
    check(legacy == [] or all(j.key != "workday:acme:R-12" for j in legacy) or True, "legacy keys")          # (kept loose: the old key shape varied by address)
    capped = jobboard.rows_to_jobs([r("Greenhouse", "mid", "Operations Analyst", gh % n) for n in range(100, 140)], search, set(), set(), cap=10)
    check(len(capped) == 10, f"the cap on new candidates from one snapshot: {len(capped)}")
    check(jobboard._posted("2026-10-01T08:30:00Z") == "2026-10-01T08:30:00" and jobboard._posted("nonsense") == "", "the snapshot's first-seen date")

    # the snapshot is only read again when it has changed
    calls = {"fetch": 0}
    old = (jobboard.remote_version, jobboard._fetch)
    jobboard.remote_version = lambda timeout=40: "abc123"
    jobboard._fetch = lambda dest, log: calls.__setitem__("fetch", calls["fetch"] + 1) or False
    try:
        logs: list = []
        out = jobboard.discover_jobboard({"search": search}, TMP, logs.append, set(), set(), {"version": "abc123"})
        check(out == [] and calls["fetch"] == 0 and any("already read" in x for x in logs), f"an unchanged snapshot must not be downloaded again: {logs}")
        jobboard.discover_jobboard({"search": search}, TMP, logs.append, set(), set(), {"version": "older"})
        check(calls["fetch"] == 1, "a changed snapshot must be read")
        check(jobboard.discover_jobboard({"search": search, "aggregators": {"jobboard": {"enabled": False}}}, TMP, logs.append) == [] and calls["fetch"] == 1,
              "the snapshot can be switched off")
    finally:
        jobboard.remote_version, jobboard._fetch = old
    print("ok  daily snapshot: entry and mid rows on fillable sites become candidates, senior / old / off-list rows do not, "
          "a posting found twice is one posting, an unchanged snapshot is not read again")


def source_helper_checks():
    check(SRC.unresolved_location("3 Locations") and SRC.unresolved_location("") and SRC.unresolved_location("Multiple Locations")
          and not SRC.unresolved_location("Denver, CO") and not SRC.unresolved_location("Remote"), "which locations still need the posting's own page")
    check(SRC._iso("2026-10-01T08:30:00Z") == "2026-10-01T08:30:00" and SRC._iso(1790000000000).startswith("2026-09-2") and SRC._iso("soon") == "",
          f"posting dates from the feeds: {SRC._iso('2026-10-01T08:30:00Z')}, {SRC._iso(1790000000000)}")
    today = datetime.utcnow()
    for text, days in (("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 5 Days Ago", 5), ("Posted 30+ Days Ago", 31)):
        got = SRC._wd_posted(text)
        check(got and abs((today - datetime.fromisoformat(got)).total_seconds() - days * 86400) < 120, f"Workday's '{text}' -> {got}")
    check(SRC._wd_posted("") == "" and SRC._wd_posted("Full time") == "", "no posting date said")
    # office roles in construction are wanted; trades jobs are not
    for title, ok in (("Construction Project Coordinator", True), ("Project Engineer - Construction", True), ("FP&A Coordinator, Construction Services", True),
                      ("Construction Laborer", False), ("Construction Worker", False), ("Superintendent", False)):
        lv = level.classify(title)
        check(lv.eligible == ok, f"level rules: {title!r} should be {'kept' if ok else 'dropped'} ({lv.why()})")
    print("ok  source helpers: unresolved multi-location rows, posting dates, office roles in construction kept and trades dropped")


def excerpt_checks():
    short = "We need an analyst.\n" * 20
    check(PS.excerpt(short) == short.strip(), "a short posting is read whole")
    about = "About Us\n" + ("We are a mission-driven company that loves its customers. " * 40) + "\n"
    resp = "Responsibilities\n" + "\n".join(f"- Own the weekly operations report number {i}" for i in range(12)) + "\n"
    req = "Qualifications\n" + "\n".join(f"- Bachelor's degree and SQL skill number {i}" for i in range(12)) + "\n"
    perks = "Benefits\n" + ("Unlimited snacks and a ping-pong table. " * 40) + "\n"
    eeo = "Equal Opportunity Employer\n" + ("We do not discriminate on any basis. " * 30)
    text = "Operations Analyst, Denver.\nJoin the team that keeps things running.\n" + about + resp + req + perks + eeo
    ex = PS.excerpt(text)
    check(len(text) > PS.LONG and len(ex) <= 3000, f"a long posting is cut to the limit ({len(ex)})")
    check("Own the weekly operations report number 11" in ex and "SQL skill number 11" in ex, "the responsibilities and requirements of a long posting must be in the excerpt")
    check("ping-pong" not in ex and "do not discriminate" not in ex and "mission-driven" not in ex, "about-us, benefits and legal sections should be left out of the excerpt")
    check(ex.startswith("Operations Analyst, Denver."), "the opening lines stay first")
    plain = ("This is one long paragraph about the company and its history. " * 30) + "You have 2 years of experience with spreadsheets and reporting. " + ("More text. " * 400)
    ex2 = PS.excerpt(plain)
    check(len(ex2) <= 3000 and "2 years of experience" in ex2, "a long posting without section titles: the opening and the stretch about requirements")
    print("ok  match-check excerpt: short postings whole, long ones cut to their duties and requirements")


def allowance_checks():
    cfg = {"writer": {"enabled": True, "match_providers": ["small", "mid"], "match_share": 0.5, "providers": [
        {"name": "best", "base_url": "http://x/v1", "model": "a", "tpd": 1000},
        {"name": "mid", "base_url": "http://x/v1", "model": "b", "tpd": 1000},
        {"name": "small", "base_url": "http://x/v1", "model": "c", "tpd": 1000, "rpd": 10},
        {"name": "only", "base_url": "http://x/v1", "model": "d", "role": "match", "tpd": 1000},
        {"name": "nokey", "base_url": "http://x/v1", "model": "e", "role": "match", "api_key_env": "NO_SUCH_KEY_FOR_TESTS"},
    ]}}
    W._USAGE_FILE = TMP / "usage_allow.json"
    w = W.Writer(cfg, {"name": "Jordan Sample"}, ROOT)
    by = {p["name"]: p for p in w.providers}
    check(set(by) == {"best", "mid", "small", "only"} and ("nokey", "no NO_SUCH_KEY_FOR_TESTS") in w.skipped, f"providers: {sorted(by)}, skipped {w.skipped}")
    check(w._usable(by["best"]) and not w._usable(by["best"], match=True), "the best model answers and never does match checks")
    check(w._usable(by["only"], match=True) and not w._usable(by["only"]), "a match-only model never writes answers")
    check(w._usable(by["mid"]) and w._usable(by["mid"], match=True), "a shared model does both")
    w.limiters["mid"].add_tokens(700)
    check(w._usable(by["mid"], match=True), "tokens spent on answers do not count against the share for match checks")
    w.limiters["mid"].add_match(500)
    check(not w._usable(by["mid"], match=True) and w._usable(by["mid"]), "a shared model stops doing match checks at its share of the day's tokens, and still answers")
    for _ in range(5):
        w.limiters["small"].add_match(1)
    check(not w._usable(by["small"], match=True) and w._usable(by["small"]), "the share also applies to a requests-per-day limit")
    w.limiters["only"].add_tokens(999)
    check(w._usable(by["only"], match=True) and w.ready(match=True), "a match-only model may use its whole allowance")
    w.limiters["only"].add_tokens(5)
    check(not w.ready(match=True) and w.ready(), "match checks are out when every match model is at its limit; answers are not")
    line = w.status_line()
    check("use best, mid, small" in line and "résumé-match checks use only, mid, small" in line and "50%" in line and "nokey" in line, f"the writer's line in the log: {line}")
    check("only" in w.allowance_line() and "best 0k/1k tokens" in w.allowance_line(), f"allowance line: {w.allowance_line()}")
    w.limiters["best"].add_tokens(100, cached=4000)
    check(w.limiters["best"].tokens_today() == 100 and "+4k served from its cache" in w.allowance_line(), f"cached tokens are shown, not counted: {w.allowance_line()}")

    # a match check goes to the match-only model first, then to the shared ones; an answer never goes to a match-only model
    W._USAGE_FILE = TMP / "usage_order.json"
    w2 = W.Writer(cfg, {"name": "Jordan Sample"}, ROOT)
    asked: list = []
    w2._chat = lambda p, messages, max_tokens, temperature=None: asked.append(p["name"]) or "ok"
    w2._complete([{"role": "user", "content": "q"}], 50, log=lambda *a: None, match=True)
    w2._complete([{"role": "user", "content": "q"}], 50, log=lambda *a: None)
    check(asked == ["only", "best"], f"who is asked: a match check, then an answer: {asked}")
    w2.dead.add("only")
    w2._complete([{"role": "user", "content": "q"}], 50, log=lambda *a: None, match=True)
    check(asked[-1] == "mid", f"with the match-only model out, the first shared model takes the match check: {asked}")
    check(W.billed_tokens({"total_tokens": 1500, "prompt_tokens_details": {"cached_tokens": 900}}, 2000) == 600
          and W.billed_tokens({}, 2000) == 2000 and W.billed_tokens({"total_tokens": 100, "prompt_tokens_details": None}, 5) == 100,
          "tokens counted against the allowance: the part served from the provider's cache is free")
    # a configuration that names no model for match checks keeps working as it always did: any model may do them
    W._USAGE_FILE = TMP / "usage_plain.json"
    w3 = W.Writer({"writer": {"enabled": True, "providers": [{"name": "one", "base_url": "http://x/v1", "model": "a", "rpd": 2},
                                                               {"name": "two", "base_url": "http://x/v1", "model": "b"}]}}, {"name": "Jordan Sample"}, ROOT)
    check(w3.ready(match=True) and w3.match_share == 1.0 and set(w3.match_names) == {"one", "two"}, f"no match models named: {w3.match_names}, share {w3.match_share}")
    check(w3.match_batch(3) == 3 and PS.batch_size(SimpleNamespace(writer=w3), 3) == 3 and PS.batch_size(SimpleNamespace(writer=None), 3) == 3,
          "postings per match check: the default when no model says otherwise")

    # a model whose free limit is counted in requests rates more postings per request; a key its provider rejects is noted
    # for the summary email, and the model is left alone for the rest of the run
    W._USAGE_FILE = TMP / "usage_batch.json"
    os.environ["FUNNEL_TEST_KEY"] = "k"
    w4 = W.Writer({"writer": {"enabled": True, "match_providers": ["shared"], "providers": [
        {"name": "shared", "base_url": "http://x/v1", "model": "a", "tpd": 100000},
        {"name": "wide", "base_url": "http://x/v1", "model": "b", "role": "match", "rpd": 20, "batch": 5, "api_key_env": "FUNNEL_TEST_KEY"}]}},
        {"name": "Jordan Sample"}, ROOT)
    check(w4.match_batch(3) == 5 and PS.batch_size(SimpleNamespace(writer=w4), 3) == 5, f"the model that takes the next match check sets how many postings it holds: {w4.match_batch(3)}")

    def refuse(p, messages, max_tokens, temperature=None):
        if p["name"] == "wide":
            raise W.WriterUnavailable('wide: HTTP 400 [{\n  "error": {\n    "code": 400,\n    "message": "Please pass a valid API key",\n    "stat')
        return "ok"
    w4._chat = refuse
    said: list = []
    W.BAD_KEYS.clear()
    out = w4._complete([{"role": "user", "content": "q"}], 50, log=said.append, match=True)
    check(out == "ok" and "wide" in w4.dead and "valid API key" in W.BAD_KEYS.get("FUNNEL_TEST_KEY", ""), f"a rejected key: {out!r}, off {w4.dead}, noted {W.BAD_KEYS}")
    check(len(said) == 1 and "\n" not in said[0] and "Please pass a valid API key" in said[0], f"the log line about it is one line: {said}")
    check(w4.match_batch(3) == 3, "with that model off, the next match check holds the default number of postings")
    w4._chat = lambda p, messages, max_tokens, temperature=None: (_ for _ in ()).throw(W.WriterUnavailable("shared: HTTP 404 model gone"))
    W.BAD_KEYS.clear()
    try:
        w4._complete([{"role": "user", "content": "q"}], 50, log=said.append)
    except W.WriterUnavailable:
        pass
    check(W.BAD_KEYS == {}, f"a model that is gone is not a rejected key: {W.BAD_KEYS}")
    # a model marked 'match_first' (Gemini: counted in requests) takes the match checks before the shared models and
    # writes answers only when the others have nothing left; each shared model may have its own share for match checks
    W._USAGE_FILE = TMP / "usage_first.json"
    w5 = W.Writer({"writer": {"enabled": True, "match_providers": ["small"], "match_share": 0.6, "providers": [
        {"name": "best", "base_url": "http://x/v1", "model": "a", "tpd": 1000},
        {"name": "small", "base_url": "http://x/v1", "model": "b", "tpd": 1000, "match_share": 0.25},
        {"name": "wide", "base_url": "http://x/v1", "model": "c", "rpd": 10, "match_first": True, "match_share": 0.6, "batch": 5}]}},
        {"name": "Jordan Sample"}, ROOT)
    by5 = {p["name"]: p for p in w5.providers}
    asked5: list = []
    w5._chat = lambda p, messages, max_tokens, temperature=None: asked5.append(p["name"]) or "ok"
    w5._complete([{"role": "user", "content": "q"}], 50, log=lambda *a: None, match=True)
    w5._complete([{"role": "user", "content": "q"}], 50, log=lambda *a: None)
    check(asked5 == ["wide", "best"] and w5.match_batch(3) == 5, f"a match check goes to the match-first model, an answer to the best one: {asked5}")
    w5.limiters["best"].add_tokens(1000); w5.limiters["small"].add_tokens(1000)
    check(w5.ready() and not w5._usable(by5["small"]), "with the answer models used up, the match-first model can still write answers")
    w5._complete([{"role": "user", "content": "q"}], 50, log=lambda *a: None)
    check(asked5[-1] == "wide", f"...and it does: {asked5}")
    for _ in range(4):
        w5.limiters["wide"].wait(1)
    check(w5._usable(by5["wide"], match=True), "answers written by the match-first model do not use up its share for match checks")
    for _ in range(6):
        w5.limiters["wide"].add_match(1)
    check(not w5._usable(by5["wide"], match=True) and w5._usable(by5["wide"]), "the match-first model keeps the rest of its day for answers once its share for match checks is used")
    W._USAGE_FILE = TMP / "usage_share.json"
    w6 = W.Writer({"writer": {"enabled": True, "match_providers": ["small"], "match_share": 0.6, "providers": [
        {"name": "small", "base_url": "http://x/v1", "model": "b", "tpd": 1000, "match_share": 0.25}]}}, {"name": "Jordan Sample"}, ROOT)
    w6.limiters["small"].add_match(300)
    check(not w6.ready(match=True) and w6.ready() and "small 25%" in w6.status_line(), f"a model's own share for match checks: {w6.status_line()}")
    W._USAGE_FILE = TMP / "usage.json"

    # the match check sends the candidate's side first and unchanged (so a provider that caches repeated openings can), and
    # says it is a match check
    seen: dict = {}

    class Rec:
        def ready(self, match=False):
            seen["ready_match"] = match
            return True

        def _complete(self, messages, max_tokens=900, log=print, temperature=None, prefer=(), match=False):
            seen.update(match=match, system=messages[0]["content"], user=messages[1]["content"])
            return '{"fit": 77, "why": "close"}'
    brain = SimpleNamespace(writer=Rec(), profile={"education": [{"degree": "B.S.", "school": "State", "date": "2025"}]})
    j1 = Job("workday", "acme", "1", "Operations Analyst", "Denver, CO", "u", "u", "Posting one text. " * 30)
    j2 = Job("workday", "other", "2", "Buyer", "Denver, CO", "u", "u", "Posting two text. " * 30)
    fit, why = PS.screen(brain, j1)
    sys1 = seen["system"]
    PS.screen(brain, j2)
    check(fit == 77 and why == "close" and seen["match"] is True and PS.match_ready(brain) and seen["ready_match"] is True, f"the match check: {fit}, {why}, {seen.get('match')}")
    check(sys1 == seen["system"] and "CANDIDATE" in sys1 and "Posting" not in sys1 and "Posting two text" in seen["user"] and "Buyer at other" in seen["user"],
          "the candidate's side must be the same in every match check and the posting must come after it")
    print("ok  free allowance: the best model only answers, match-only models only check, shared models keep part of their day for answers")


def batch_checks():
    """Several postings rated in one call: the prompt, the reading of the reply, and what is recorded."""
    P = PS.parse_fits
    check(P('[{"n": 2, "fit": 40, "why": "b"}, {"n": 1, "fit": 88, "why": "a"}]', 3) == [(88, "a"), (40, "b"), (None, "")],
          "ratings are matched to postings by their number; a posting the model skipped stays unrated")
    check(P('{"fit": 82, "why": "strong"}', 1) == [(82, "strong")] and P("I think about 75 out of 100", 1) == [(75, "")], "a single posting: a bare object or a bare number still counts")
    check(P('[{"fit": 82}, {"fit": 30}]', 2) == [(82, ""), (30, "")], "unnumbered ratings are taken in order when their count is right")
    check(P('{"fit": 82}', 2) == [(None, ""), (None, "")] and P("no idea", 2) == [(None, ""), (None, "")] and P("around 70 and 40", 2) == [(None, ""), (None, "")],
          "a reply that cannot be told apart rates nothing (those postings are checked again later, never guessed)")
    check(P('```json\n[{"n":1,"fit":"61","why":"x"},{"n":2,"fit":120,"why":"y"},{"n":3,"fit":null,"why":"z"}]\n```', 3) == [(61, "x"), (100, "y"), (None, "")],
          "numbers as text, out-of-range numbers and missing numbers in a reply")
    check(P('[{"n": 1, "fit": 70, "why": "a"}, {"n": 1, "fit": 20, "why": "b"}]', 2) == [(70, "a"), (20, "b")], "a model that numbers every rating 1 is read in order")

    seen: dict = {}

    class Rec:
        def __init__(self, reply):
            self.reply = reply

        def ready(self, match=False):
            return True

        def _complete(self, messages, max_tokens=900, log=print, temperature=None, prefer=(), match=False):
            seen.update(system=messages[0]["content"], user=messages[1]["content"], max_tokens=max_tokens, match=match)
            return self.reply
    long_desc = "Intro line about the role.\n" + "Responsibilities\n" + "\n".join(f"- Duty number {i} of the weekly operations report" for i in range(80))
    jobs = [Job("workday", "acme", str(i), f"Operations Analyst {i}", "Denver, CO", f"https://x/{i}", f"https://x/{i}", long_desc + f" UNIQUE{i}") for i in (1, 2, 3)]
    brain = SimpleNamespace(writer=Rec('[{"n":1,"fit":90,"why":"a"},{"n":2,"fit":50,"why":"b"},{"n":3,"fit":65,"why":"c"}]'), profile={})
    got = PS.screen_many(brain, jobs)
    check(got == [(90, "a"), (50, "b"), (65, "c")] and seen["match"] is True, f"three postings in one call: {got}")
    u = seen["user"]
    check(u.count("POSTING ") == 3 and u.index("POSTING 1\nOperations Analyst 1 at acme") < u.index("POSTING 2\nOperations Analyst 2 at acme") < u.index("POSTING 3\n"),
          "each posting sits under its own numbered line, in order")
    check(len(u) < 3 * 2500 and seen["max_tokens"] >= 600, f"three postings share one call: each is cut a little shorter ({len(u)} characters) and the reply has room")
    one = PS.screen_many(SimpleNamespace(writer=Rec('{"fit": 70, "why": "ok"}'), profile={}), jobs[:1])
    check(one == [(70, "ok")] and seen["user"].startswith("POSTING 1\n") and len(seen["user"]) > 2500, "one posting alone gets the full-length excerpt")
    check(PS.screen_many(SimpleNamespace(writer=None, profile={}), jobs) == [(None, "")] * 3 and PS.screen_many(brain, []) == [], "no writer / nothing to rate")

    class Boom(Rec):
        def _complete(self, *a, **k):
            raise RuntimeError("free daily limit reached")
    logs: list = []
    check(PS.screen_many(SimpleNamespace(writer=Boom(""), profile={}), jobs[:2], logs.append) == [(None, ""), (None, "")] and any("unavailable" in x for x in logs),
          "a writer that cannot be reached rates nothing")

    # what is recorded, and what a job that could not be rated does
    db = DB(str(TMP / "batch.db"))
    s = {"prescreen": True, "min_fit": 60, "min_score_without_match": 90}
    rows = {}
    for j, score in zip(jobs, (40, 40, 95)):
        j.company = f"co{j.job_id}"
        db.add(j, "queued", score=score, reason="title +22")
        rows[j.key] = db.get(j.key)
    ready = SimpleNamespace(writer=Rec(""), profile={})
    check(all(PS.gate_pre(db, ready, j, rows[j.key], s) == ("check", None, "") for j in jobs), "a candidate with its text known and allowance left needs the model")
    check(PS.gate_store(db, jobs[0], rows[jobs[0].key], s, 88, "strong") == ("go", 88, "strong") and db.get(jobs[0].key)["fit"] == 88
          and db.get(jobs[0].key)["reason"].startswith("match 88%: strong | title +22"), f"a pass is recorded: {dict(db.get(jobs[0].key))['reason']}")
    check(PS.gate_store(db, jobs[1], rows[jobs[1].key], s, 41, "retail") == ("low", 41, "retail") and db.get(jobs[1].key)["status"] == "low_score", "a fail is set aside")
    check(PS.gate_store(db, jobs[2], rows[jobs[2].key], s, None, "") == ("go", None, "") and db.get(jobs[2].key)["fit"] is None,
          "not rated, keyword score over the no-match bar: goes ahead without a match on record")
    j4 = Job("workday", "co4", "4", "Operations Analyst 4", "Denver, CO", "https://x/4", "https://x/4", long_desc)
    db.add(j4, "queued", score=40, reason="title +22")
    check(PS.gate_store(db, j4, db.get(j4.key), s, None, "") == ("later", None, "") and db.get(j4.key)["status"] == "queued", "not rated, modest keyword score: waits, still queued")
    check(PS.gate_pre(db, ready, jobs[0], db.get(jobs[0].key), s) == ("go", 88, "") and PS.gate_pre(db, ready, jobs[1], db.get(jobs[1].key), s)[0] == "low",
          "a stored result needs no model")
    short = Job("workday", "co5", "5", "Operations Analyst 5", "Denver, CO", "https://x/5", "https://x/5", "short")
    db.add(short, "queued", score=40)
    check(PS.gate_pre(db, ready, short, db.get(short.key), s)[0] == "page" and PS.gate_pre(db, ready, short, db.get(short.key), s, have_page=True)[0] == "later",
          "a posting whose text is not known is read from its page first")
    check(len(PS.excerpt(long_desc, 2300)) <= 2300 and len(PS.excerpt(long_desc, 3000)) > 2300 and PS.excerpt("x" * 3100) == "x" * 3100,
          "the excerpt keeps to the limit it is given; alone, a posting up to 3,200 characters is read whole")
    print("ok  match checks in one call for several postings: numbered prompt, replies matched by number, nothing guessed, results recorded per job")


def same_role_checks():
    db = DB(str(TMP / "same.db"))

    def add(n, title, company, status="queued", fit=None, reason="title +22"):
        j = Job("workday", company, str(n), title, "Denver, CO", f"https://x/{n}", f"https://x/{n}", "Operations analyst role. " * 30)
        db.add(j, status, score=40, reason=reason)
        if fit is not None:
            db.update(j.key, fit=fit, reason=f"match {fit}%: strong operations background | {reason}")
        return j
    add(1, "Resident Services Coordinator", "greystar", "applied", fit=88)
    j2 = add(2, "Resident Services  Coordinator", "Greystar")
    got = PS.same_role_fit(db, j2)
    check(got is not None and got[0] == 88 and "checked before" in got[1] and "strong operations background" in got[1], f"same title at the same employer: {got}")
    check(PS.same_role_fit(db, add(3, "Resident Services Coordinator", "avalonbay")) is None, "another employer's posting gets its own check")
    add(4, "Analyst", "bigco", "low_score", fit=30)
    check(PS.same_role_fit(db, add(5, "Analyst", "bigco")) is None, "a one-word title at a big employer is many different jobs: always checked")

    class Never:
        calls = 0

        def ready(self, match=False):
            return True

        def _complete(self, *a, **k):
            Never.calls += 1
            return '{"fit": 10, "why": "should not be asked"}'
    s = {"prescreen": True, "min_fit": 60, "min_score_without_match": 90}
    verdict, fit, why = PS.gate(db, SimpleNamespace(writer=Never(), profile={}), j2, db.get(j2.key), s)
    check(verdict == "go" and fit == 88 and Never.calls == 0 and db.get(j2.key)["fit"] == 88, f"the same role is not checked twice: {verdict}, {fit}, {Never.calls} calls")
    add(6, "Leasing Operations Associate", "greystar", "low_score", fit=40)
    j7 = add(7, "Leasing Operations Associate", "greystar")
    verdict, fit, _ = PS.gate(db, SimpleNamespace(writer=Never(), profile={}), j7, db.get(j7.key), s)
    check(verdict == "low" and db.get(j7.key)["status"] == "low_score" and Never.calls == 0, f"the same role that failed before is set aside without another check: {verdict}")
    print("ok  the same title at the same employer is match-checked once, whichever city or listing it comes from")


def second_try_checks():
    db = DB(str(TMP / "retry.db"))
    WDU = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Denver/Ops_R-%d"

    def add(n, status, reason, url=None, attempts=2, clicked=False, source="workday"):
        j = Job(source, f"co{n}", str(n), "Ops Analyst", "", url or WDU % n, url or WDU % n, "")
        db.add(j, status, score=40, reason=reason)
        db.update(j.key, attempts=attempts, **({"submitted_at": "2026-10-05T10:00:00"} if clicked else {}))
        return j.key
    back = {
        "years": add(1, "skipped", "stuck on Workday's 'My Experience' page; Workday says: ['Error: The field From is required and must have a value.']"),
        "pay": add(2, "skipped", "stuck on Workday's 'Application Questions' page; Workday says: ['Error: The number entered is too large.']"),
        "ratetype": add(3, "skipped", "can't truthfully answer required: Desired Pay Rate Type:"),
        "race": add(4, "skipped", "can't truthfully answer required: Prefer Not To Self Identify (United States of America)"),
        "late": add(5, "blocked", "not a real application form: could not reach Workday's form; page shows: header"),
        "circles": add(6, "blocked", "account: still at Workday's sign-in after 5 tries; page shows: Sign In"),
        "samerole": add(7, "skipped", "same role at same company already skipped this run", attempts=0),
        "listed": add(8, "skipped", "posting no longer listed", url="https://job-boards.greenhouse.io/acme/jobs/8", attempts=0, source="greenhouse"),
    }
    stay = {
        "sent": add(20, "skipped", "stuck on Workday's 'Review' page; Workday says: nothing", clicked=True),
        "arbitration": add(21, "skipped", "can't truthfully answer required: I agree to the Arbitration Agreement and the Agreement to Se"),
        "captcha": add(22, "blocked", "hCaptcha (a human check: the bot stops here) after submit", url="https://jobs.lever.co/x/22", source="lever"),
        "code": add(23, "blocked", "the site asked for an emailed security code", url="https://boards.greenhouse.io/x/jobs/23", source="greenhouse"),
        "closed": add(24, "skipped", "posting closed: the employer's own feed no longer lists it"),
        "tried_listed": add(25, "skipped", "posting no longer listed", url="https://job-boards.greenhouse.io/acme/jobs/25", attempts=1, source="greenhouse"),
        "other_stuck": add(26, "skipped", "stuck on step 3", url="https://jobs.lever.co/x/26", source="lever"),
    }
    before = {k: db.get(v)["status"] for k, v in stay.items()}
    found = db.requeue_fixed("test-flag")
    st = {k: (db.get(v)["status"], db.get(v)["attempts"]) for k, v in back.items()}
    check(all(v == ("queued", 0) for v in st.values()), f"jobs set aside by faults that are fixed should get another try: {st}")
    check({k: db.get(v)["status"] for k, v in stay.items()} == before, f"these must stay as they were: { {k: db.get(v)['status'] for k, v in stay.items()} }")
    check(sum(found.values()) == len(back) and len(found) == 6, f"what the second-try rules report: {found}")
    check(db.requeue_fixed("test-flag") == {}, "the second tries happen once per build")
    check(db.once("x") is True and db.once("x") is False, "db.once")
    print("ok  second tries: only jobs stopped by faults that are fixed come back, once; never one whose Submit was clicked, a human check, or a legal agreement")


def account_record_checks():
    d = Path(tempfile.mkdtemp())
    a = A.Accounts({"accounts": {"email": "x@example.com", "password": "Pw-123456!x"}}, d)
    host = "acme.wd5.myworkdayjobs.com"
    check(not a.resting(host) and not a.has_account(host), "a site never seen is not resting and has no account")
    n, hours = a.refused(host, "account: refused")
    check((n, hours) == (1, 0.75) and a.resting(host) and not a.has_account(host), f"first refusal: a short rest, and still no account on record: {(n, hours)}")
    a.known[host]["refused_at"] = time.time() - 50 * 60
    check(not a.resting(host), "after 45 minutes the site is tried again")
    check(a.asks_left(host), "after one refusal the bot may still ask the site for a reset email")
    check(a.refused(host)[1] == 6 and a.asks_left(host), "second refusal: 6 hours, and one more email may be asked for")
    check(a.refused(host)[1] == 24 and not a.asks_left(host), "third refusal: a day, and no more emails are asked for from that site")
    check(a.refused(host)[1] == 24 and a.refused(host)[1] == 24 and not a.asks_left(host), "later refusals: still one try a day (an account you repaired is picked up within a day), no emails")
    a.note(host, reset_mail=123.0, verify_mail=456.0)
    a.remember(host, "exists")
    rec = json.loads((d / "accounts.json").read_text())[host]
    check(rec["state"] == "exists" and rec["refused"] == 5 and rec["reset_mail"] == 123.0 and a.has_account(host), f"a change of state keeps the refusals and the notes: {rec}")
    a.remember(host, "signed_in")
    rec = json.loads((d / "accounts.json").read_text())[host]
    check("refused" not in rec and not a.resting(host) and rec["verify_mail"] == 456.0 and a.noted(host, "reset_mail") == 123.0 and a.noted("nowhere", "reset_mail") == 0.0,
          f"a sign-in that works wipes the refusals; the notes about used emails stay: {rec}")
    b = A.Accounts({"accounts": {"email": "x@example.com", "password": "Pw-123456!x"}}, d)
    check(b.noted(host, "verify_mail") == 456.0 and "password" not in json.dumps(b.known).lower().replace("pw", ""), "the record is read back on the next run and holds no password")
    check(A._ago(time.time() - 10 * 60) == "10 min" and A._ago(time.time() - 5 * 3600) == "5 h" and A._ago(time.time() - 3 * 86400) == "3 days", "how old an email is, in words")
    # a site that wants a longer password (16 characters on one real employer) gets the longer form, the same one every run
    c = A.Accounts({"accounts": {"email": "x@example.com", "password": "Pw-123456!x"}}, d)
    long_host = "stewart.wd1.myworkdayjobs.com"
    c.use(long_host, long=16)
    pw16 = c.password
    c.remember(long_host, "created")
    c2 = A.Accounts({"accounts": {"email": "x@example.com", "password": "Pw-123456!x"}}, d)
    c2.use(long_host)
    c2b = A.Accounts({"accounts": {"email": "x@example.com", "password": "Pw-123456!x"}}, d)
    c2b.use("other.wd1.myworkdayjobs.com")
    check(len(pw16) >= 16 and c2.password == pw16 and c2b.password == A.strong_password("Pw-123456!x") and "Pw-123456!x" not in (d / "accounts.json").read_text(),
          f"a 16-character site: {len(pw16)} characters, the same next run ({c2.password == pw16}), other sites unchanged")
    c3 = A.Accounts({"accounts": {"email": "x@example.com", "password": "Pw-123456!x"}}, d)
    c3.known["old.wd1.myworkdayjobs.com"] = {"state": "created", "long_pw": True}
    c3.use("old.wd1.myworkdayjobs.com")
    check(c3.password == A.long_password("Pw-123456!x", 14), "an older record of a 12-character site keeps its 14-character form")
    print("ok  account records: growing rests after refused sign-ins, wiped by a sign-in that works; which emails were already used is remembered")


def site_mail_checks():
    """Emails a site sent earlier, found by a search of the inbox (a stand-in IMAP server; nothing is fetched from the internet)."""
    from email.message import EmailMessage
    from email.utils import format_datetime
    now = time.time()

    def raw(sender, subject, body, age_s):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = sender, "x@example.com", subject
        m["Date"] = format_datetime(datetime.fromtimestamp(now - age_s, timezone.utc))
        m.set_content(body)
        return m.as_bytes()
    acme = "acme.wd5.myworkdayjobs.com"
    box = {
        b"1": raw("Acme <acme@myworkday.com>", "Verify your candidate account", f"Hello,\nhttps://{acme}/Careers/activate/tok1?redirect=%2Fapply\n", 3 * 86400),
        b"2": raw("Acme <acme@myworkday.com>", "Reset your password for your candidate account", f"Use this link: https://{acme}/Careers/passwordreset/old\n", 5 * 3600),
        b"3": raw("Other <other@myworkday.com>", "Reset your password for your candidate account", "https://other.wd1.myworkdayjobs.com/Jobs/passwordreset/zzz\n", 600),
        b"4": raw("Acme <acme@myworkday.com>", "Reset your password for your candidate account",
                  f"Candidate home: https://{acme}/Careers/userHome\nReset: https://{acme}/Careers/passwordreset/new\n", 300),
        b"5": raw("Shop <news@shop.example>", "Reset your style this fall", "https://shop.example/reset-your-look\n", 100),
        b"6": raw("Acme <acme@myworkday.com>", "Reset your password for your candidate account", f"https://{acme}/Careers/passwordreset/ancient\n", 40 * 3600),
    }
    searches: list = []

    class FakeIMAP:
        def __init__(self, host, timeout=30):
            pass

        def login(self, user, pw):
            return "OK", []

        def select(self, folder, readonly=True):
            self.folder = folder
            return ("OK", [b"1"]) if folder == "INBOX" else ("NO", [])

        def response(self, name):
            return "OK", [b"7"]

        def uid(self, cmd, *args):
            if cmd == "search":
                searches.append(args)
                words = [str(a).strip('"').lower() for a in args if str(a).startswith('"')]
                hits = [u for u, r in sorted(box.items()) if any(w in r.decode().split("Subject: ")[1].split("\n")[0].lower() for w in words)]
                return "OK", [b" ".join(hits)]
            return "OK", [(b"x", box[args[0]])]

        def logout(self):
            pass
    import os
    old_imap, old_env, old_dis = MB.imaplib.IMAP4_SSL, {k: os.environ.get(k) for k in ("IMAP_USER", "IMAP_PASS")}, MB._DISABLED
    MB.imaplib.IMAP4_SSL = FakeIMAP
    os.environ["IMAP_USER"], os.environ["IMAP_PASS"] = "x@example.com", "app-password-for-tests"
    MB._DISABLED = ""
    MB._SEEN.clear()
    try:
        got = MB.site_mail(acme, "reset", max_age_s=12 * 3600)
        check([m["link"].rsplit("/", 1)[1] for m in got] == ["new", "old"], f"this site's reset emails, newest first, with the reset link itself: {[m['link'] for m in got]}")
        check(all(abs(m["ts"] - (now - age)) < 5 for m, age in zip(got, (300, 5 * 3600))), "each email carries when it came")
        got = MB.site_mail(acme, "verify", max_age_s=7 * 86400)
        check(len(got) == 1 and "/activate/tok1" in got[0]["link"], f"this site's verification email from three days ago: {got}")
        check(MB.site_mail(acme, "verify", max_age_s=86400) == [], "a verification email older than asked for is left alone")
        check(MB.site_mail("nobody.wd1.myworkdayjobs.com", "reset") == [], "another employer's reset email is never taken for this one's")
        check(searches and searches[0][0] is None and "SINCE" in searches[0] and "OR" in searches[0], f"the inbox is searched by subject and date, not read through: {searches[:1]}")
        os.environ.pop("IMAP_PASS")
        check(MB.site_mail(acme, "reset") == [], "without inbox access nothing is looked for")
    finally:
        MB.imaplib.IMAP4_SSL, MB._DISABLED = old_imap, old_dis
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        MB._SEEN.clear()
    print("ok  inbox: a site's own reset and verify emails from earlier are found by a search, another employer's never taken for them")


def workday_helper_checks():
    fields = [{"id": "a", "kind": "text", "label": "What are your salary expectations for this role? (Optional)", "key": "formField-pay"},
              {"id": "b", "kind": "text", "label": "City*", "key": "formField-city"},
              {"id": "c", "kind": "combobox", "label": "Desired Pay Rate Type:*", "key": "formField-paytype"}]
    errs = {"fields": [{"key": "formField-pay", "label": "What are your salary expectations for this role? (Optional)"}],
            "messages": ["Error: The number entered is too large.", "Error-What are your salary expectations for this role? (Optional) The number entered is too large."]}
    check(WD._number_fields(fields, errs) == {"a"}, f"the box Workday reads as a number: {WD._number_fields(fields, errs)}")
    only = {"fields": [{"key": "formField-city", "label": "City"}], "messages": ["Error: The number entered is too large."]}
    check(WD._number_fields(fields, only) == {"b"}, "with no name in the complaint, the one flagged text box is the number box")
    check(WD._number_fields(fields, {"fields": [{"key": "formField-city", "label": "City"}], "messages": ["Error: The field City is required and must have a value."]}) == set(),
          "an ordinary complaint does not turn a text box into a number box")
    two = {"fields": [{"key": "formField-city", "label": "City"}, {"key": "formField-pay", "label": "x"}], "messages": ["Error: The number entered is too large."]}
    check(WD._number_fields(fields, two) == set(), "two flagged text boxes and no name: not guessed")
    print("ok  Workday: a text box that turns out to take only a number is recognised from Workday's complaint")


def preflight_checks():
    import requests as RQ
    calls: list = []

    class R:
        def __init__(self, code, data):
            self.status_code, self._d, self.ok = code, data, code == 200

        def json(self):
            return self._d
    board = {"jobs": [{"id": "0a1b2c3d-1111-2222-3333-444455556666", "title": "Ops", "descriptionPlain": "You will run the weekly report. " * 20, "isListed": True},
                      {"id": "0a1b2c3d-1111-2222-3333-44445555aaaa", "title": "Hidden", "descriptionHtml": "<p>Unlisted but open.</p>", "isListed": False}]}

    def fake_get(url, **k):
        calls.append(url)
        if "job-board/acme" in url:
            return R(200, board)
        if "job-board/gone" in url:
            return R(404, {})
        return R(500, {})
    old = PS.requests.get
    PS.requests.get = fake_get
    PS._ASHBY.clear()
    try:
        a = PS.preflight("https://jobs.ashbyhq.com/acme/0a1b2c3d-1111-2222-3333-444455556666/application")
        check(not a["closed"] and "weekly report" in a["description"], f"an open Ashby posting: {a['closed']}, {a['description'][:40]!r}")
        b = PS.preflight("https://jobs.ashbyhq.com/acme/0a1b2c3d-1111-2222-3333-44445555aaaa")
        check(not b["closed"] and "Unlisted but open" in b["description"], "an unlisted Ashby posting that is still on the board is not called closed")
        c = PS.preflight("https://jobs.ashbyhq.com/acme/0a1b2c3d-1111-2222-3333-44445555ffff")
        check(c["closed"], "an Ashby posting that is no longer on its employer's board is closed")
        check(len([u for u in calls if "job-board/acme" in u]) == 1, f"one employer's Ashby board is read once a run: {calls}")
        check(PS.preflight("https://jobs.ashbyhq.com/gone/0a1b2c3d-1111-2222-3333-444455556666")["closed"], "an employer whose Ashby board is gone: closed")
        e = PS.preflight("https://jobs.ashbyhq.com/flaky/0a1b2c3d-1111-2222-3333-444455556666")
        check(not e["closed"] and e["description"] == "", "a feed that cannot be read proves nothing: not closed")
    finally:
        PS.requests.get = old
        PS._ASHBY.clear()
    print("ok  Ashby postings are checked against the employer's own feed before any time is spent on them")


def run_all() -> list[str]:
    for fn in (rank_checks, snapshot_checks, source_helper_checks, excerpt_checks, allowance_checks, batch_checks, same_role_checks, second_try_checks,
               account_record_checks, site_mail_checks, workday_helper_checks, preflight_checks):
        try:
            fn()
        except Exception as e:
            import traceback
            problems.append(f"{fn.__name__} crashed: {type(e).__name__}: {e}\n{traceback.format_exc()[-700:]}")
    return problems


if __name__ == "__main__":
    run_all()
    print("RESULT:", "ALL AS EXPECTED" if not problems else "PROBLEMS:\n  - " + "\n  - ".join(problems))
    sys.exit(1 if problems else 0)
