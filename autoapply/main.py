"""autoapply: discover -> filter -> score -> tailor -> apply -> report. Free: no paid APIs."""
from __future__ import annotations

import argparse
import calendar
import dataclasses
import json
import os
import random
import re
import sys
import time
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from pathlib import Path

import yaml

from .db import DB
from .brain import Brain
from .sources import discover, prefilter, Job
from .aggregators import direct_apply_url, discover_aggregators, load_boards, remember_board, canon_key, board_of
from . import render, submit as sub, auth, sources, mailbox, notify, level, prescreen, snapshot, selfcheck, __version__
from .ats import detect as detect_ats

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
REQUEUE_VERSION = "2026-10-04-a"
ACTIONS_OVERHEAD_MIN = 3.0          # checkout + install + history save around the Python step, per run


def merge_board_file(cfg: dict, base: Path) -> int:
    """Fold boards.yaml (the public company lists and display names) into the config. Returns how many boards it added."""
    p = base / "boards.yaml"
    if not p.exists():
        return 0
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception as e:
        print(f"! boards.yaml could not be read: {e}", flush=True)
        return 0
    names = data.pop("names", None) or {}
    if not isinstance(cfg.get("companies"), dict):
        cfg["companies"] = {}
    added = 0
    for ats, toks in data.items():
        if not isinstance(toks, list):
            continue
        cur = cfg["companies"].get(ats) or []
        cfg["companies"][ats] = cur
        have = {str(t).lower() for t in cur}
        for t in toks:
            if str(t).lower() not in have:
                cur.append(str(t))
                have.add(str(t).lower())
                added += 1
    cfg["company_names"] = {**{str(k): v for k, v in names.items()}, **(cfg.get("company_names") or {})}
    return added


TOP_MATCHES: list = []      # (fit, line) for the match checks of this run, for the summary email
FOLLOWUPS: list = []        # employers that emailed asking for something more ('incomplete application', an assessment)


def merge_settings(cfg: dict, base: Path) -> dict:
    """Fold settings.yaml (non-private tuning kept in the repository) over the private config.yaml: nested sections merge,
    lists and plain values replace. writer.drop_providers removes providers by name."""
    p = base / "settings.yaml"
    if not p.exists():
        return cfg
    try:
        over = yaml.safe_load(p.read_text()) or {}
    except Exception as e:
        print(f"! settings.yaml could not be read: {e}", flush=True)
        return cfg

    def deep(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                deep(a[k], v)
            else:
                a[k] = v
    drop = set(((over.get("writer") or {}).pop("drop_providers", None)) or [])
    deep(cfg, over)
    w = cfg.get("writer") or {}
    if drop and isinstance(w.get("providers"), list):
        w["providers"] = [x for x in w["providers"] if x.get("name") not in drop]
    return cfg


def level_out(job, s: dict) -> str | None:
    """Why this posting is above your level or not open to you (None = fine). search.max_level: 0 entry, 1 early (default)."""
    lo, hi = level.pay_range(job.description or "")
    lv = level.classify(job.title, job.description or "", lo, hi)
    if not lv.eligible:
        return "not open to you: " + "; ".join(r for r in lv.reasons if not r.startswith(("title", "requires", "pay", "level", "manager")))
    if lv.level > int(s.get("max_level", 1)):
        return "above entry level: " + lv.why()
    floor = s.get("_pay_floor")
    if hi and floor and hi < float(floor):
        return f"pays too little: tops out near ${hi:,.0f} a year (your floor is ${float(floor):,.0f})"
    return None


def norm_co(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|the|group|holdings)\b", " ", n)
    return "".join(n.split())


def stub_job(row):
    """A queued posting that this run's search did not return. Big Workday sites and the aggregators only show the newest
    results of each search, so 'not returned' does not mean 'closed': the employer's own page decides."""
    src = row["source"] or ""
    if not (src == "workday" or src.startswith("agg-")) or not (row["apply_url"] or row["url"]):
        return None
    return Job(source=src, company=row["company"], job_id=str(row["key"]).split(":")[-1], title=row["title"] or "",
               location=row["location"] or "", url=row["url"] or row["apply_url"], apply_url=row["apply_url"] or row["url"], description="")


def row_job(row):
    """A queued posting rebuilt from the history (runs that skip the job search). Its own page is read before applying."""
    if not (row["apply_url"] or row["url"]):
        return None
    return Job(source=row["source"] or "web", company=row["company"], job_id=str(row["key"]).split(":")[-1], title=row["title"] or "",
               location=row["location"] or "", url=row["url"] or row["apply_url"], apply_url=row["apply_url"] or row["url"], description="")


# How likely each application system is to let an unattended application finish, before anything has been measured:
# 0 = finishes (Workday: the only system the bot has completed again and again), 1 = no known obstacle,
# 2 = submits are often held by a human check when they come from a data-centre address.
SITE_PRIOR = {"workday": 0, "bamboohr": 1, "breezy": 1, "recruitee": 1, "smartrecruiters": 1, "icims": 1, "jobvite": 1,
              "ashby": 2, "workable": 2, "greenhouse": 2, "lever": 2}
HUMAN_CHECK = re.compile(r"captcha|turnstile|cloudflare|security code|human check|human verification|are you a robot|anti-bot", re.I)


def ats_of(job) -> str:
    """Which application system a posting lives on: workday, greenhouse, lever ... or the site's own host."""
    src = (getattr(job, "source", "") or "").lower()
    url = getattr(job, "apply_url", "") or getattr(job, "url", "") or ""
    if "myworkdayjobs.com" in url:
        return "workday"
    if src and src != "web" and not src.startswith("agg-"):
        return src
    found = board_of(url)
    if found:
        return found[0]
    return detect_ats(url).name


def _row_ats(row) -> str:
    return ats_of(SimpleNamespace(source=row["source"] or "", apply_url=row["apply_url"] or "", url=row["url"] or ""))


def site_health(db: DB, days: int = 14) -> dict:
    """What actually happened on each application system lately: {'workday': {'ok': 7, 'human': 0, 'other': 12}}.
    'ok' = applications that went through, 'human' = stopped by a human check, 'other' = everything else that was tried."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out: dict = {}
    for r in db.conn.execute("SELECT source, url, apply_url, status, reason FROM jobs WHERE attempts > 0 AND updated >= ?"
                             " AND status IN ('applied','blocked','unconfirmed','failed','skipped')", (since,)):
        site = out.setdefault(_row_ats(r), {"ok": 0, "human": 0, "other": 0})
        if r["status"] == "applied":
            site["ok"] += 1
        elif HUMAN_CHECK.search(r["reason"] or ""):
            site["human"] += 1
        else:
            site["other"] += 1
    return out


def site_rank(row, health: dict | None = None) -> int:
    """Order in which application systems are tried, from measured results rather than a fixed list: 0 = applications
    have been going through there, 1 = untested or mixed, 2 = submits are often held by a human check, 3 = every recent
    try ended at a human check. Nothing is paused or skipped: lower-ranked sites simply wait until the better ones are done."""
    ats = _row_ats(row)
    h = (health or {}).get(ats)
    if h:
        if h["ok"] and h["ok"] * 3 >= h["human"]:
            return 0
        if h["human"] >= 3 and not h["ok"]:
            return 3
    return SITE_PRIOR.get(ats, 1)


def health_lines(health: dict) -> list[str]:
    """One line per application system that keeps ending at a human check (for the summary email)."""
    return [f"{ats}: stopped by a human check on {h['human']} recent tries, none went through (tried last, after the sites that finish)"
            for ats, h in sorted(health.items()) if h["human"] >= 3 and not h["ok"]]


def site_summary(rows) -> list[str]:
    """Per application system, how this run's attempts ended: 'greenhouse: 4 blocked, 1 skipped'. Shows at a glance which
    kinds of site the bot can finish and which stop it."""
    tally: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["status"] in ("queued", "low_score", "filtered"):
            continue
        site = tally.setdefault(_row_ats(r), {})
        site[r["status"]] = site.get(r["status"], 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: -sum(kv[1].values()))[:10]
    return [f"  {name}: " + ", ".join(f"{n} {st}" for st, n in sorted(c.items(), key=lambda kv: -kv[1])) for name, c in ranked]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


class Logger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, msg: str):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        self.f.write(line + "\n")
        self.f.flush()


# ----------------------------------------------------------------------------------------- Actions-minutes budget
def _usage_file(base: Path) -> Path:
    return base / "logs" / "usage_minutes.json"


def _read_usage(base: Path) -> dict:
    try:
        data = json.loads(_usage_file(base).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_usage(base: Path, data: dict):
    p = _usage_file(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, sort_keys=True))


def _bill(data: dict, minutes: float, when: date | None = None):
    key = (when or date.today()).strftime("%Y-%m")
    cur = data.get(key) or {"minutes": 0.0, "runs": 0}
    cur["minutes"] = round(float(cur.get("minutes", 0)) + minutes, 1)
    cur["runs"] = int(cur.get("runs", 0)) + 1
    data[key] = cur


def month_used(base: Path) -> float:
    try:
        return float((_read_usage(base).get(date.today().strftime("%Y-%m")) or {}).get("minutes", 0))
    except Exception:
        return 0.0


def add_usage(base: Path, minutes: float):
    """Bill this run's minutes to the month and close its 'open run' note."""
    data = _read_usage(base)
    data.pop("open", None)
    _bill(data, minutes)
    _write_usage(base, data)


def open_run(base: Path):
    """Note that a run has started. A run that is cancelled or killed never reaches add_usage, so the next run bills what
    the dead one used (up to its last heartbeat) and starts fresh. Only on Actions: your own machine has no minutes to count."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    data = _read_usage(base)
    dead = data.pop("open", None)
    if isinstance(dead, dict) and dead.get("start"):
        try:
            start = float(dead["start"])
            _bill(data, max(0.0, (float(dead.get("beat") or start) - start) / 60) + ACTIONS_OVERHEAD_MIN, datetime.fromtimestamp(start).date())
        except Exception:
            pass
    now = time.time()
    data["open"] = {"start": now, "beat": now}
    _write_usage(base, data)


def heartbeat(base: Path):
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    data = _read_usage(base)
    if isinstance(data.get("open"), dict):
        data["open"]["beat"] = time.time()
        _write_usage(base, data)


def unlimited() -> bool:
    """The repository is public, so GitHub does not count its Actions minutes: runs are long and back to back."""
    return bool(os.environ.get("AUTOAPPLY_UNLIMITED"))


def budget_ok(cfg: dict, base: Path, log) -> bool:
    """Free private repos get 2,000 Actions minutes a month. Past the budget the run stops early instead of failing later."""
    if not os.environ.get("GITHUB_ACTIONS") or unlimited():
        return True
    budget = float((cfg.get("search") or {}).get("actions_minutes_budget", 1850))
    used = month_used(base)
    if used >= budget:
        log(f"Actions-minutes budget reached ({used:.0f} of {budget:.0f} this month): skipping this run to stay in the free tier")
        return False
    return True


def run_time_allowance(cfg: dict, base: Path, today: date | None = None) -> float:
    """Minutes of Python time this run may spend, so what is left of the month's free minutes lasts until the month
    ends. Spread evenly over the runs still to come (search.runs_per_day mirrors the workflow's cron); unused time
    from quiet runs flows to later ones. Off Actions (your own machine) the only limit is search.max_run_minutes."""
    s = cfg.get("search") or {}
    cap = float(s.get("max_run_minutes", 30))
    if unlimited():
        return float(s.get("max_run_minutes_unlimited", 50))
    if not os.environ.get("GITHUB_ACTIONS"):
        return cap
    today = today or date.today()
    budget = float(s.get("actions_minutes_budget", 1850))
    per_day = max(1, int(s.get("runs_per_day", 4)))
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    share = max(0.0, budget - month_used(base)) / max(1, days_left * per_day)
    return max(6.0, min(cap, share - ACTIONS_OVERHEAD_MIN))


def apply_deadline(t_start: float, now: float, allow_min: float, s: dict) -> float:
    """When the apply loop must stop. Finding jobs counts against the run's time, but a run whose search overran still
    gets a short window (search.min_apply_minutes) to apply, so it never finds jobs and then applies to none of them.
    No run goes past max_run_minutes + 6 in total."""
    hard = t_start + 60 * ((float(s.get("max_run_minutes_unlimited", 50)) if unlimited() else float(s.get("max_run_minutes", 30))) + 6)
    return min(hard, max(t_start + allow_min * 60, now + 60 * float(s.get("min_apply_minutes", 6))))


# ----------------------------------------------------------------------------------------- the run
def run(cfg_path: str, dry_run: bool = False, limit: int | None = None):
    t_start = time.time()
    base = Path(cfg_path).resolve().parent
    try:
        return _run(cfg_path, dry_run, limit, t_start)
    finally:            # a run that ends in an error or is cancelled still bills the minutes it used
        if os.environ.get("GITHUB_ACTIONS") and isinstance(_read_usage(base).get("open"), dict):
            add_usage(base, (time.time() - t_start) / 60 + ACTIONS_OVERHEAD_MIN)


def _run(cfg_path: str, dry_run: bool, limit: int | None, t_start: float):
    cfg_file = Path(cfg_path).resolve()
    base = cfg_file.parent
    cfg = merge_settings(yaml.safe_load(cfg_file.read_text()), base)
    merge_board_file(cfg, base)
    s = cfg.setdefault("search", {})
    # on-site/hybrid roles only where you'd live: the same list that answers relocation questions (remote roles are always fine)
    s["_onsite_ok"] = list((cfg.get("facts") or {}).get("relocation_ok_locations") or [])
    s["_pay_floor"] = (cfg.get("scoring") or {}).get("salary_floor")
    dry_run = dry_run or cfg.get("dry_run", False)
    today = date.today().isoformat()
    log = Logger(base / "logs" / f"{today}.log")
    run_start = datetime.now().isoformat(timespec="seconds")
    log(f"=== autoapply run ({'DRY RUN' if dry_run else 'LIVE'}) build {__version__} ===")
    try:
        broken = selfcheck.problems()
    except Exception as e:          # the checker itself must never be the reason a run does not start
        log(f"(code check skipped: {type(e).__name__}: {str(e)[:100]})")
        broken = []
    if broken:          # e.g. a function that was deleted but is still called: every application would fail the same way
        log("The bot's own code has a mistake in it, so this run stops before touching any job:")
        for b in broken[:15]:
            log(f"  - {b}")
        if not dry_run:
            notify.send_email(cfg, "autoapply could not start: its code needs a fix",
                              "This run stopped before applying to anything, because the bot's code has a mistake in it:\n\n"
                              + "\n".join("  - " + b for b in broken[:15]) + f"\n\n(build {__version__})", log)
        raise SystemExit("code check failed: " + broken[0])
    if not budget_ok(cfg, base, log):
        if os.environ.get("GITHUB_ACTIONS"):
            add_usage(base, ACTIONS_OVERHEAD_MIN)          # even a skipped run spends its set-up minutes
        return {}
    open_run(base)

    db = DB(str(base / cfg.get("db", "applications.db")))
    n_req = db.requeue_if_new_version(REQUEUE_VERSION)
    if n_req:
        log(f"Re-checking {n_req} jobs that were skipped by earlier bugs")
    profile = yaml.safe_load((base / cfg.get("profile_file", "profile.yaml")).read_text())
    brain = Brain(cfg, profile, base, log)
    mailbox.preflight(log)
    brain.applied_before = {r[0] for r in db.conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE status IN ('applied','unconfirmed')")}
    _resolve_old_unconfirmed(db, brain, log)
    _note_followups(db, brain, log)
    n_up = 0
    for r in db.conn.execute("SELECT key, title FROM jobs WHERE status='queued'").fetchall():
        lv = level.classify(r["title"] or "")
        if not lv.eligible or lv.level > int(s.get("max_level", 1)):
            db.update(r["key"], status="filtered", reason=("not open to you: " if not lv.eligible else "above entry level: ") + lv.why())
            n_up += 1
    if n_up:
        log(f"Dropped {n_up} queued jobs whose titles are above entry level or not open to you")

    # 1. discover
    log("Discovering jobs…")
    skip_src = {x.lower() for x in (s.get("skip_sources") or [])}
    manual_src = {x.lower() for x in (s.get("manual_sources") or [])}
    companies = {k: list(v or []) for k, v in (cfg.get("companies", {}) or {}).items()}
    for ats, toks in load_boards(base).items():          # boards found earlier by following aggregator links
        companies.setdefault(ats, [])
        companies[ats] += [t for t in toks if t not in companies[ats]]
    companies = {k: v for k, v in companies.items() if k.lower() not in skip_src or k.lower() in manual_src}
    sources.SEARCH = {**(cfg.get("search", {}) or {}),
                      "_title_keys": [str(k).lower() for k in ((cfg.get("scoring") or {}).get("title_keywords") or {})]}
    sources.KNOWN = {r["key"]: r["status"] for r in db.conn.execute("SELECT key, status FROM jobs")}
    every_h = float(s.get("discover_every_hours", 20))
    if unlimited():          # non-stop mode: minutes are not counted, so new postings are looked for every few hours
        every_h = float(s.get("discover_every_hours_unlimited", 3))
    last = db.meta_get("discovered_at", "")
    fresh_enough = False
    try:
        fresh_enough = bool(last) and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < every_h * 3600
    except Exception:
        pass
    if fresh_enough and not dry_run:
        log(f"Job search already ran at {last[11:16]} (searches run every {every_h:.0f} h): this run only applies")
        jobs = []
        searched = False
    else:
        searched = True
        jobs = discover(companies, log, base=base)
        jobs += discover_aggregators(cfg, base, log)
        if len(jobs) > 1000:
            db.meta_set("discovered_at", datetime.now().isoformat(timespec="seconds"))
    try:                       # the daily snapshot of 1M+ postings: cheap (seconds), so every run looks at it
        from .jobboard import discover_jobboard
        have = {r[0] for r in db.conn.execute("SELECT apply_url FROM jobs WHERE apply_url != ''")} | \
               {r[0] for r in db.conn.execute("SELECT url FROM jobs WHERE url != ''")} | {j.apply_url for j in jobs}
        jobs += discover_jobboard(cfg, base, log, have)
    except Exception as e:
        log(f"  agg/jobboard failed: {e}")
    by_key = {j.key: j for j in jobs}

    # 2. filter + score only what we've never seen
    fresh = [j for j in by_key.values() if not db.seen(j.key)]
    log(f"{len(by_key)} open roles, {len(fresh)} new")
    scored = n_ok = n_low = n_filt = 0
    max_score = s.get("max_scored_per_run", 100000)
    min_score = s.get("min_score", 70)
    for j in fresh:
        if (why := prefilter(j, s) or level_out(j, s)):
            db.add(j, "filtered", reason=why)
            n_filt += 1
            continue
        if scored >= max_score:
            break  # leave the rest unseen; they'll be scored next run
        try:
            score, reason = brain.score(j)
        except Exception as e:
            log(f"  ! scoring failed for {j.title} @ {j.company}: {e}")
            continue
        scored += 1
        ok = score >= min_score
        by_hand = j.source.lower() in manual_src
        db.add(j, ("manual" if by_hand else "queued") if ok else "low_score", score=score,
               reason=("apply by hand: this site blocks automated submissions. " if by_hand and ok else "") + reason)
        if ok:
            n_ok += 1
            log(f"  ✓ {score:3d}  {j.title} @ {j.company} ({j.location[:30]}) — {reason[:150]}")
        else:
            n_low += 1
    log(f"Scored {scored} new roles: {n_ok} good fits, {n_low} below the bar, {n_filt} filtered out by title/location "
        f"(finding and scoring took {(time.time() - t_start) / 60:.1f} min)")

    # 3. apply, best matches first, within today's cap
    cap = s.get("daily_cap", 25) - db.applied_today()
    if unlimited():          # non-stop mode: the day's ceiling is higher and one run may use all of it
        cap = int(s.get("daily_cap_unlimited", 120)) - db.applied_today()
    elif s.get("per_run_cap"):
        cap = min(cap, int(s["per_run_cap"]))
    if limit is not None:
        cap = min(cap, limit)

    # re-check every queued title against the current rules (rules get stricter over time)
    n_sw = 0
    for r in db.conn.execute("SELECT * FROM jobs WHERE status='queued'").fetchall():
        j = by_key.get(r["key"]) or row_job(r)
        if j is None:
            continue
        why = level_out(dataclasses.replace(j, description=""), s)
        if not why:
            try:
                why = prefilter(j, s)
            except Exception:
                why = None
        if why:
            db.update(r["key"], status="filtered", reason=str(why)[:200])
            n_sw += 1
    if n_sw:
        log(f"Queue clean-up: {n_sw} queued jobs no longer meet the rules and were removed")

    health = site_health(db)
    match_on = bool(s.get("prescreen", True))
    min_fit = int(s.get("min_fit", 70))
    no_match_bar = int(s.get("min_score_without_match", 80))
    if match_on:      # the bar may have moved since these were checked: set aside what is now under it, bring back what is now over it
        db.conn.execute("UPDATE jobs SET status='low_score' WHERE status='queued' AND fit > 0 AND fit < ?", (min_fit,))
        db.conn.execute("UPDATE jobs SET status='queued' WHERE status='low_score' AND fit >= ? AND reason LIKE 'match %'", (min_fit,))
        # the keyword bar is only a first sieve (the résumé match decides): when it is lowered, jobs that were under the old
        # one and were never match-checked come back to be checked
        db.conn.execute("UPDATE jobs SET status='queued' WHERE status='low_score' AND (fit IS NULL OR fit <= 0) AND attempts = 0"
                        " AND score >= ? AND (submitted_at IS NULL OR submitted_at = '')", (int(s.get("min_score", 70)),))
        db.conn.commit()

    def _fit(r) -> int:
        try:
            return r["fit"] or 0
        except (IndexError, KeyError):
            return 0

    def _rank(r):
        fit = _fit(r)
        return (site_rank(r, health), -(fit if fit > 0 else 0.8 * (r["score"] or 0)))
    queue = sorted(db.retryable(s.get("max_attempts", 2)), key=_rank)
    skip_sites = [x.lower() for x in s.get("skip_sites", []) or []]
    if skip_sites:
        queue = [r for r in queue if not any(x in ((r["source"] or "") + " " + (r["apply_url"] or r["url"] or "")).lower()
                                             for x in skip_sites)]
    log(f"{len(queue)} jobs queued; applying to up to {max(cap, 0)} now"
        + (f" (each is first checked against your résumé: the bar is a {min_fit}% match)" if match_on and brain.writer else ""))
    if cap <= 0 or not queue:
        return finish(cfg, db, run_start, log, base, today, t_start, dry_run)

    from playwright.sync_api import sync_playwright
    out_root = base / "applications" / today
    allow_min = run_time_allowance(cfg, base)
    deadline = apply_deadline(t_start, time.time(), allow_min, s)
    if os.environ.get("GITHUB_ACTIONS"):
        log(f"Time for this run: {(deadline - t_start) / 60:.0f} min in all ({month_used(base):.0f} of {s.get('actions_minutes_budget', 1850)} free Actions minutes used this month)")
    per_company = int(s.get("max_per_company", 3))
    sub.MAX_APPLY_SECONDS = int(float(s.get("max_minutes_per_job", 8)) * 60)
    facts = cfg.get("facts") or {}
    heartbeat(base)

    def judge(row):
        """Everything that can be decided about a queued job without opening a browser: still listed, still within today's
        rules, posting still up (the employer's own feed says so in a fraction of a second), your level and pay once the
        full text is known, and the résumé match. Returns (verdict, job): 'go', 'page' (judge again once its page is
        open), 'later' (cannot be judged now, stays queued) or 'drop' (already recorded why)."""
        job = by_key.get(row["key"]) or (stub_job(row) if searched else row_job(row))
        if job is None:
            db.update(row["key"], status="skipped", reason="posting no longer listed")
            return "drop", None
        why_out = prefilter(job, s) or level_out(job, s)       # safety net: a queued job that no longer passes today's filters is dropped
        if why_out:
            db.update(job.key, status="filtered", reason=why_out)
            return "drop", None
        url0 = job.apply_url or job.url or ""
        direct = not job.source.startswith("agg-") or bool(board_of(url0)) or "myworkdayjobs.com" in url0
        if direct and s.get("preflight", True):
            pre = prescreen.preflight(url0, deep=len(job.description or "") < 300)
            if pre["closed"]:
                db.update(job.key, status="skipped", reason="posting closed: the employer's own feed no longer lists it")
                log(f"  ✗ {job.title} @ {job.company}: the posting is closed (no time spent on it)")
                return "drop", None
            if len(pre["description"]) > len(job.description or ""):
                job.description = pre["description"]
            if len(job.description or "") >= 300 and (why_lv := level_out(job, s)):
                db.update(job.key, status="filtered", reason=why_lv)
                log(f"  ✗ {job.title} @ {job.company}: {why_lv[:120]}")
                return "drop", None
        verdict, fit, why = prescreen.gate(db, brain, job, row, s, log)
        if why:
            log(f"  match {fit:3d}%  {job.title} @ {job.company} — {why[:100]}")
            TOP_MATCHES.append((fit, f"{fit}% {job.title} @ {job.company}: {why}"))
        return ("drop" if verdict == "low" else verdict), job

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.get("headless", True))
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800}, locale="en-US")
        ctx.set_default_timeout(10000)
        ctx.set_default_navigation_timeout(30000)
        acc = auth.Accounts(cfg, base)
        sub.ACCOUNTS_ENABLED = acc.enabled
        log(f"Accounts: {'ON (' + acc.email + ')' if acc.enabled else 'off (no ACCOUNT_PASSWORD secret), login sites are skipped'}")
        done = laters = 0
        dead = set()          # (company, title) already skipped/blocked this run
        nofind: dict = {}     # job source -> listings whose real application page could not be found this run
        weak_pw: set = set()  # sites that rejected ACCOUNT_PASSWORD this run
        bad_hosts: set = set()   # career sites that would not let the bot sign in this run: their other jobs wait, untouched
        verdicts: dict = {}   # job key -> judge() result worked out ahead of time (during the pause after an application)

        def lookahead(start: int, until: float):
            """Use the pause between two applications to check the next jobs in line, so the next one is ready to go."""
            ready = 0
            for r in queue[start:start + 60]:
                if ready >= 2 or time.time() >= until:
                    break
                v = verdicts.get(r["key"])
                if v is None:
                    v = verdicts[r["key"]] = judge(r)
                if v[0] in ("go", "page"):
                    ready += 1

        for idx, row in enumerate(queue):
            if done >= cap:
                break
            if time.time() > deadline:
                log(f"Run time limit reached ({(deadline - t_start) / 60:.0f} min): the rest waits for the next run")
                break
            ck = ((row["company"] or "").lower(), (row["title"] or "").strip().lower())
            if nofind.get(row["source"] or "", 0) >= 2:
                continue          # this board keeps handing out listings with no real form: don't burn minutes on more of them now
            if ck in dead:
                db.update(row["key"], status="skipped", reason="same role at same company already skipped this run")
                continue
            co_n = norm_co(row["company"] or "")
            hist = [r for r in db.conn.execute("SELECT key, company, title FROM jobs WHERE status IN ('applied','unconfirmed') AND key != ?",
                                               (row["key"],)) if norm_co(r["company"]) == co_n]
            if any(r["title"].strip().lower() == ck[1] for r in hist):
                db.update(row["key"], status="skipped", reason="already applied to this role at this company")
                continue
            n_co = len(hist)
            if per_company and n_co >= per_company:
                db.update(row["key"], status="skipped", reason=f"already applied to {n_co} roles at this company")
                continue
            if str(row["reason"] or "").startswith("recheck-inbox"):
                # an earlier submit that never reached the employer's server: one more try, but only after looking in the
                # inbox for a confirmation. If the inbox cannot be read, it is not submitted again blind.
                if not mailbox.configured():
                    continue
                rj = by_key.get(row["key"]) or row_job(row)
                got = mailbox.find_confirmation(brain._company_name(rj), time.time() - 7 * 86400, 0, log) if rj else None
                if got:
                    db.update(row["key"], status="applied", reason=f"confirmed by email: {got[:100]}")
                    log(f"    ✓ {row['title']} @ {row['company']}: the earlier submit did go through ({got[:60]})")
                    continue
            host = (urlparse(row["apply_url"] or row["url"] or "").netloc or "").lower()
            if host and host in bad_hosts:
                continue          # the bot could not sign in to this career site a moment ago: its other jobs stay queued
            if weak_pw and "myworkdayjobs" in (row["apply_url"] or "") and "workday" in weak_pw:
                continue

            verdict, job = verdicts.pop(row["key"], None) or judge(row)
            if verdict == "drop":
                continue
            if verdict == "later":
                laters += 1
                if laters == 1:
                    log(f"  (the match check has no free allowance left right now: jobs with a keyword score under {no_match_bar} wait for a later run)")
                continue

            fit_now = _fit(db.get(job.key) or row)
            log(f"→ {job.title} @ {job.company} (score {row['score']}" + (f", match {fit_now}%" if fit_now > 0 else "") + ")")
            d = out_root / f"{slug(job.company)}-{slug(job.title)}"
            d.mkdir(parents=True, exist_ok=True)
            page = None
            clicked: list = []
            stage = [""]
            t_job = time.time()

            def note_stage(name, _k=job.key, _st=stage):
                _st[0] = str(name)[:60]
                db.update(_k, stage=_st[0])

            def snap(status, why, _job=job, _st=stage):
                snapshot.save(base, page, _job, status, why, facts, _st[0])
            try:
                page = ctx.new_page()
                if job.source.startswith("agg-"):          # follow the aggregator link to the employer's own form
                    try:
                        job.apply_url = sub.resolve_apply_url(page, job.apply_url, log)
                    except sub.Blocked as e:
                        if "could not find the employer" not in str(e):
                            raise
                        direct = direct_apply_url(job, log)     # the listing page gave no link: find the company's own posting by name
                        if not direct:
                            raise
                        job.apply_url = direct
                    remember_board(base, job.apply_url)
                    dup = db.conn.execute(
                        "SELECT 1 FROM jobs WHERE key != ? AND status IN ('applied','unconfirmed','dry_run') AND (apply_url=? OR key=?)",
                        (job.key, job.apply_url, canon_key(job.apply_url) or "")).fetchone()
                    if dup:
                        db.update(job.key, status="skipped", reason="same posting already handled", attempts=row["attempts"] + 1)
                        log("    ✗ skipped: already applied to this posting via another listing")
                        continue
                    db.update(job.key, apply_url=job.apply_url)
                    host = (urlparse(job.apply_url).netloc or "").lower()
                landing = sub.open_form(page, job.apply_url) or ""   # check for blockers before spending tokens on tailoring
                try:
                    page_txt = page.inner_text("body")[:12000]
                except Exception:
                    page_txt = ""
                if len(job.description or "") < 300:  # only the title is known (big Workday sites, stubs): read the posting itself
                    job.description = (job.description + "\n" + (landing if len(landing) > len(page_txt) else page_txt))[:8000]
                why_lv = level_out(Job(job.source, job.company, job.job_id, job.title, job.location, job.url, job.apply_url,
                                       (job.description or "") + "\n" + page_txt), s)   # the whole posting and form: years, pay
                if why_lv:
                    db.update(job.key, status="filtered", reason=why_lv)
                    log(f"    ✗ skipped: {why_lv[:140]}")
                    continue
                if verdict == "page":                 # the posting text was not known until its page was open: the match check now
                    v2, fit2, why2 = prescreen.gate(db, brain, job, row, s, log, have_page=True)
                    if why2:
                        log(f"    match {fit2}% — {why2[:110]}")
                        TOP_MATCHES.append((fit2, f"{fit2}% {job.title} @ {job.company}: {why2}"))
                    if v2 == "low":
                        log(f"    ✗ set aside: under the {min_fit}% match bar")
                        continue
                    if v2 == "later":
                        log(f"    … waits for a later run (no match check available now and its keyword score is under {no_match_bar})")
                        continue
                if brain.writer and cfg.get("respect_ai_policies", True):
                    sub.guard_ai_policy(page, job)   # skip employers that say no AI-assisted applications
                from . import writer as _w
                _w.DEADLINE[0] = time.time() + sub.MAX_APPLY_SECONDS      # cover letter + answers share one time budget
                resume_md, letter = brain.tailor(job)
                (d / "resume.md").write_text(resume_md)
                name = slug(cfg.get("facts", {}).get("full_name", "resume")).replace("-", "_") or "resume"
                fixed = base / str(cfg.get("resume_file", "") or "")
                use_fixed = bool(cfg.get("resume_file")) and fixed.is_file()
                if cfg.get("resume_file") and not use_fixed:
                    raise RuntimeError(f"resume_file {cfg.get('resume_file')} is missing: not applying with a made-up résumé")
                files = {"RESUME": (fixed if use_fixed else render.resume_pdf(browser, resume_md, d / f"{name}_resume.pdf")),
                         "_LETTER_MAKER": (lambda txt, _d=d, _n=name: render.letter_pdf(browser, txt, _d / f"{_n}_cover_letter.pdf", name=profile.get("name", ""), contact=profile.get("contact_line", "")))}
                result = sub.apply(page, job, brain, letter, files, d / "form.png", dry_run, log, acc,
                                   on_click=(None if dry_run else (lambda _k=job.key, _a=row["attempts"]: (clicked.append(1), db.mark_submit(_k, _a + 1)))),
                                   on_stage=note_stage)
                took = f"{(time.time() - t_job) / 60:.1f} min"
                if result == "already_applied":
                    db.update(job.key, status="applied", reason="the site says you already applied to this job", attempts=row["attempts"] + 1)
                    brain.applied_before.add(job.company)
                    log("    ✓ already applied earlier (recorded; nothing was sent again)")
                    continue
                status = "applied" if result == "confirmed" else "dry_run"
                db.update(job.key, status=status, reason=result, resume_path=str(files["RESUME"]),
                          cover_path=str(files.get("COVER_LETTER", "")), screenshot=str(d / "form.png"),
                          attempts=row["attempts"] + (0 if dry_run else 1))
                if status == "applied":
                    brain.applied_before.add(job.company)
                log(f"    ✓ {status} ({took})")
                done += 1
            except sub.Blocked as e:
                blocked_text = str(e)
                if re.search(r"password must include|password must (contain|have)", blocked_text, re.I):
                    # the saved ACCOUNT_PASSWORD is too weak for this site: nothing is wrong with the job, so it stays queued
                    # for the run after the password is updated, and no more time is spent on that site this run
                    db.update(job.key, status="queued", reason="waiting for a stronger ACCOUNT_PASSWORD (8+ chars, upper, lower, number, symbol)")
                    weak_pw.add(ats_of(job))
                    log(f"    ✗ {ats_of(job)} rejected the saved account password as too weak: its jobs wait until it is updated")
                    continue
                if blocked_text.startswith("posting closed"):
                    db.update(job.key, status="skipped", reason=blocked_text)
                    log("    ✗ skipped: the posting is closed")
                    continue
                snap("blocked", blocked_text)
                db.update(job.key, status="blocked", reason=blocked_text, attempts=row["attempts"] + 1)
                dead.add(ck)
                if "could not find the employer" in blocked_text:
                    nofind[job.source] = nofind.get(job.source, 0) + 1
                if blocked_text.startswith("account:") and host:
                    bad_hosts.add(host)
                log(f"    ✗ blocked: {e}")
            except sub.Unconfirmed as e:
                snap("unconfirmed", str(e))
                got = None
                if mailbox.configured():
                    got = mailbox.find_confirmation(brain._company_name(job), getattr(e, "t0", 0) or time.time() - 120, 75, log)
                if got:
                    db.update(job.key, status="applied", reason=f"confirmed by email: {got[:100]}", attempts=row["attempts"] + 1)
                    brain.applied_before.add(job.company)
                    log(f"    ✓ applied (the company's confirmation email arrived: {got[:70]!r})")
                    done += 1
                else:
                    db.update(job.key, status="unconfirmed", reason=str(e)[:400], attempts=row["attempts"] + 1)
                    dead.add(ck)
                    log(f"    ? submitted but NOT confirmed (will not retry; checked the inbox too): {str(e)[:260]}")
            except sub.NotSubmitted as e:
                snap("not sent", str(e))
                # the site bounced the submit with its own error: nothing was sent, so this one may be tried again
                db.update(job.key, status="failed", reason=str(e)[:300], attempts=row["attempts"] + 1, submitted_at="")
                log(f"    ✗ not sent (the form rejected it): {str(e)[:220]}")
            except sub.Unanswerable as e:
                snap("skipped", str(e))
                db.update(job.key, status="skipped", reason=str(e), attempts=row["attempts"] + 1)
                dead.add(ck)
                log(f"    ✗ skipped: {e}")
            except Exception as e:
                snap("failed", str(e).splitlines()[0] if str(e) else type(e).__name__)
                if clicked:          # submit was already clicked: it may have gone through, so never retry it
                    db.update(job.key, status="unconfirmed", reason=("error after submit click: " + str(e).splitlines()[0])[:300],
                              attempts=row["attempts"] + 1)
                    dead.add(ck)
                    log(f"    ? submit clicked, then an error: not retrying ({str(e).splitlines()[0][:150]})")
                else:
                    db.update(job.key, status="failed", reason=(str(e).splitlines()[0] if str(e) else type(e).__name__)[:300],
                              attempts=row["attempts"] + 1)
                    log(f"    ✗ failed: {(str(e).splitlines()[0] if str(e) else type(e).__name__)[:200]}")
                if page:
                    try:
                        page.screenshot(path=str(d / "error.png"), full_page=True)
                    except Exception:
                        pass
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
            heartbeat(base)
            if not dry_run:
                pause = random.uniform(*s.get("delay_seconds", [3, 10]))
                t_pause = time.time()
                if pause > 0.5:
                    lookahead(idx + 1, t_pause + pause)      # the pause is not wasted: the next jobs in line are checked meanwhile
                rest = pause - (time.time() - t_pause)
                if rest > 0:
                    time.sleep(rest)
        browser.close()
    return finish(cfg, db, run_start, log, base, today, t_start, dry_run)


def _resolve_old_unconfirmed(db: DB, brain: Brain, log):
    """Submits from earlier runs that showed no confirmation: if the company has since emailed 'we received your
    application', they count as applied."""
    if not mailbox.configured():
        return
    cutoff = datetime.fromtimestamp(time.time() - 5 * 86400).isoformat(timespec="seconds")
    rows = db.conn.execute("SELECT key, company, title, updated FROM jobs WHERE status='unconfirmed' AND updated >= ?", (cutoff,)).fetchall()
    if not rows:
        return
    names = {r["key"]: brain._company_name(type("J", (), {"company": r["company"], "extra": {}})()) for r in rows}
    found = mailbox.scan_confirmations(names, time.time() - 5 * 86400)
    for key, subj in found.items():
        db.update(key, status="applied", reason=f"confirmed by email: {subj[:100]}")
        log(f"  ✓ earlier unconfirmed submit is confirmed by the company's email: {subj[:70]!r}")


def _note_followups(db: DB, brain: Brain, log):
    """An employer that answers an application with 'incomplete application' or 'please complete the assessment' does not
    treat it as finished. The bot cannot do those steps, so you are told about each one, once."""
    if not mailbox.configured():
        return
    cutoff = datetime.fromtimestamp(time.time() - 7 * 86400).isoformat(timespec="seconds")
    rows = db.conn.execute("SELECT key, company, title, followup FROM jobs WHERE status IN ('applied','unconfirmed') AND updated >= ?",
                           (cutoff,)).fetchall()
    if not rows:
        return
    names = {r["key"]: brain._company_name(type("J", (), {"company": r["company"], "extra": {}})()) for r in rows}
    by = {r["key"]: r for r in rows}
    for key, subj in mailbox.scan_followups(names, time.time() - 7 * 86400).items():
        if (by[key]["followup"] or "") == subj:
            continue                                        # already told
        db.conn.execute("UPDATE jobs SET followup=? WHERE key=?", (subj, key))      # (not through update(): 'updated' stays as it was)
        db.conn.commit()
        FOLLOWUPS.append(f"{by[key]['title']} @ {names[key]}: the employer emailed \"{subj[:100]}\"")
        log(f"  ! {names[key]} asks for something more on '{by[key]['title']}': {subj[:80]!r}")


def finish(cfg, db, run_start, log, base, today, t_start=None, dry_run=False):
    rows = db.since(run_start)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["status"], []).append(r)
    order = ["applied", "unconfirmed", "blocked", "skipped", "failed", "dry_run", "queued", "low_score", "filtered"]
    counts = ", ".join(f"{len(groups[k])} {k}" for k in order if k in groups) or "nothing new"
    manual = notify.manual_rows(rows)
    lines = [f"# autoapply — {today}", "", f"**{counts}**", ""]
    if manual:
        lines += [f"## Exceptions not submitted automatically ({len(manual)})", "", "| Score | Role | Company | Why |", "|---:|---|---|---|"]
        for r in manual:
            lines.append(f"| {r['score'] or ''} | [{r['title']}]({r['apply_url'] or r['url']}) | {r['company']} | {(r['reason'] or '').replace('|', '/')[:140]} |")
        lines.append("")
    for k in order[:7]:
        if k not in groups:
            continue
        lines += [f"## {k.replace('_', ' ').title()} ({len(groups[k])})", "",
                  "| Score | Role | Company | Note |", "|---:|---|---|---|"]
        for r in groups[k]:
            note = (r["reason"] or "").replace("|", "/")[:140]
            lines.append(f"| {r['score'] or ''} | [{r['title']}]({r['url']}) | {r['company']} | {note} |")
        lines.append("")
    rp = base / "reports" / f"{today}.md"
    rp.parent.mkdir(exist_ok=True)
    prior = rp.read_text() + "\n\n---\n\n" if rp.exists() else ""
    rp.write_text(prior + "\n".join(lines))
    log(f"Summary: {counts}. Report: {rp}")
    for line in site_summary(rows):
        log(f"  by site{line}")

    run_url = ""
    if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    s = cfg.get("search") or {}
    footer = f"build {__version__} · {len(db.retryable(s.get('max_attempts', 2)))} good fits still waiting for a later run"
    if os.environ.get("GITHUB_ACTIONS"):
        footer += f" · {month_used(base):.0f} of {s.get('actions_minutes_budget', 1850)} free Actions minutes used this month (before this run)"
    by_site = site_summary(rows)
    if TOP_MATCHES:
        top = [line for _fit, line in sorted(TOP_MATCHES, key=lambda x: -x[0])[:6]]
        footer = "BEST MATCHES CHECKED THIS RUN\n" + "\n".join("- " + x for x in top) + "\n\n" + footer
    text = notify.build_text(today, counts, groups, manual, run_url, footer, health_lines(site_health(db)), by_site, needs=list(FOLLOWUPS))
    applied_n = len(groups.get("applied", []))
    n = cfg.get("notify") or {}
    # An email whenever something happened, and at least one a day even when nothing did, so silence never means "broken".
    daily_check_in = db.meta_get("last_email_day") != today
    if not dry_run and (applied_n or manual or FOLLOWUPS or groups.get("unconfirmed") or daily_check_in or not n.get("only_if_activity", True)):
        subject = (f"autoapply: {applied_n} applied" + (f", {len(FOLLOWUPS)} need you" if FOLLOWUPS else "")
                   + (f", {len(manual)} not sent" if manual else "") + ("" if applied_n or manual or FOLLOWUPS else " (running, nothing new to send)"))
        if notify.send_email(cfg, subject, text, log):
            db.meta_set("last_email_day", today)
    notify.send_webhook(cfg, text, log)
    if t_start is not None and os.environ.get("GITHUB_ACTIONS"):
        add_usage(base, (time.time() - t_start) / 60 + ACTIONS_OVERHEAD_MIN)
    return groups


def main():
    ap = argparse.ArgumentParser(prog="autoapply", description="Hands-off job applier")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="fill forms and screenshot them, but never click submit")
    ap.add_argument("--limit", type=int, help="max applications this run (overrides daily cap if lower)")
    ap.add_argument("--loop", action="store_true", help="run forever: check for new postings every search.loop_minutes")
    a = ap.parse_args()
    if not a.loop:
        run(a.config, a.dry_run, a.limit)
        return
    while True:   # 24/7 mode: a crash or network blip never stops the loop
        cfg = yaml.safe_load(Path(a.config).read_text())
        s = cfg.get("search", {})
        try:
            run(a.config, a.dry_run, a.limit)
        except SystemExit as e:
            print(f"config problem, stopping: {e}", flush=True)
            raise
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            print(f"run failed ({type(e).__name__}: {str(e)[:200]}); retrying next cycle", flush=True)
        mins = float(s.get("loop_minutes", 20))
        time.sleep(mins * 60 * random.uniform(0.85, 1.15))


if __name__ == "__main__":
    sys.exit(main())
