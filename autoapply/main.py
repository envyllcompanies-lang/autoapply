"""autoapply: discover -> filter -> score -> tailor -> apply -> report. Free: no paid APIs."""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests
import yaml

from .db import DB
from .brain import Brain
from .sources import discover, prefilter
from .aggregators import discover_aggregators, load_boards, remember_board, canon_key
from . import render, submit as sub, auth, sources

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


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


def run(cfg_path: str, dry_run: bool = False, limit: int | None = None):
    cfg_file = Path(cfg_path).resolve()
    base = cfg_file.parent
    cfg = yaml.safe_load(cfg_file.read_text())
    s = cfg.get("search", {})
    dry_run = dry_run or cfg.get("dry_run", False)
    today = date.today().isoformat()
    log = Logger(base / "logs" / f"{today}.log")
    run_start = datetime.now().isoformat(timespec="seconds")
    log(f"=== autoapply run ({'DRY RUN' if dry_run else 'LIVE'}) ===")

    db = DB(str(base / cfg.get("db", "applications.db")))
    profile = yaml.safe_load((base / cfg.get("profile_file", "profile.yaml")).read_text())
    brain = Brain(cfg, profile, base, log)

    # 1. discover
    log("Discovering jobs…")
    companies = {k: list(v or []) for k, v in (cfg.get("companies", {}) or {}).items()}
    for ats, toks in load_boards(base).items():          # boards found earlier by following aggregator links
        companies.setdefault(ats, [])
        companies[ats] += [t for t in toks if t not in companies[ats]]
    sources.SEARCH = cfg.get("search", {}) or {}
    jobs = discover(companies, log)
    jobs += discover_aggregators(cfg, base, log)
    by_key = {j.key: j for j in jobs}

    # 2. filter + score only what we've never seen
    fresh = [j for j in jobs if not db.seen(j.key)]
    log(f"{len(jobs)} open roles, {len(fresh)} new")
    scored = 0
    max_score = s.get("max_scored_per_run", 100000)
    for j in fresh:
        if (why := prefilter(j, s)):
            db.add(j, "filtered", reason=why)
            continue
        if scored >= max_score:
            break  # leave the rest unseen; they'll be scored next run
        try:
            score, reason = brain.score(j)
        except Exception as e:
            log(f"  ! scoring failed for {j.title} @ {j.company}: {e}")
            continue
        scored += 1
        ok = score >= s.get("min_score", 70)
        db.add(j, "queued" if ok else "low_score", score=score, reason=reason)
        log(f"  {'✓' if ok else '·'} {score:3d}  {j.title} @ {j.company} — {reason}")

    # 3. apply, best matches first, within today's cap
    cap = s.get("daily_cap", 25) - db.applied_today()
    if s.get("per_run_cap"):
        cap = min(cap, int(s["per_run_cap"]))
    if limit is not None:
        cap = min(cap, limit)
    queue = db.retryable(s.get("max_attempts", 2))
    log(f"{len(queue)} jobs queued; applying to up to {max(cap, 0)} today")
    if cap <= 0 or not queue:
        return finish(cfg, db, run_start, log, base, today)

    from playwright.sync_api import sync_playwright
    out_root = base / "applications" / today
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.get("headless", True))
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800}, locale="en-US")
        acc = auth.Accounts(cfg, base)
        sub.ACCOUNTS_ENABLED = acc.enabled
        log(f"Accounts: {'ON (' + acc.email + ')' if acc.enabled else 'off (no ACCOUNT_PASSWORD secret), login sites are skipped'}")
        done = 0
        dead = set()          # (company, title) already skipped/blocked this run
        for row in queue:
            if done >= cap:
                break
            job = by_key.get(row["key"])
            if job is None:
                db.update(row["key"], status="skipped", reason="posting no longer listed")
                continue
            ck = (job.company.lower(), job.title.strip().lower())
            if ck in dead:
                db.update(job.key, status="skipped", reason="same role at same company already skipped this run")
                continue
            if db.conn.execute("SELECT 1 FROM jobs WHERE key != ? AND status='applied' AND lower(company)=? AND lower(title)=?",
                               (job.key, ck[0], ck[1])).fetchone():
                db.update(job.key, status="skipped", reason="already applied to this role at this company")
                continue
            log(f"→ {job.title} @ {job.company} (score {row['score']})")
            d = out_root / f"{slug(job.company)}-{slug(job.title)}"
            d.mkdir(parents=True, exist_ok=True)
            page = None
            try:
                page = ctx.new_page()
                if job.source.startswith("agg-"):          # follow the aggregator link to the employer's own form
                    job.apply_url = sub.resolve_apply_url(page, job.apply_url, log)
                    remember_board(base, job.apply_url)
                    dup = db.conn.execute(
                        "SELECT 1 FROM jobs WHERE key != ? AND status IN ('applied','dry_run') AND (apply_url=? OR key=?)",
                        (job.key, job.apply_url, canon_key(job.apply_url) or "")).fetchone()
                    if dup:
                        db.update(job.key, status="skipped", reason="same posting already handled", attempts=row["attempts"] + 1)
                        log("    ✗ skipped: already applied to this posting via another listing")
                        continue
                    db.update(job.key, apply_url=job.apply_url)
                sub.open_form(page, job.apply_url)   # check for blockers before spending tokens on tailoring
                if brain.writer and cfg.get("respect_ai_policies", True):
                    sub.guard_ai_policy(page, job)   # skip employers that say no AI-assisted applications
                resume_md, letter = brain.tailor(job)
                (d / "resume.md").write_text(resume_md)
                name = slug(cfg.get("facts", {}).get("full_name", "resume")).replace("-", "_") or "resume"
                files = {"RESUME": render.resume_pdf(browser, resume_md, d / f"{name}_resume.pdf"),
                         "_LETTER_MAKER": (lambda txt, _d=d, _n=name: render.letter_pdf(browser, txt, _d / f"{_n}_cover_letter.pdf"))}
                result = sub.apply(page, job, brain, letter, files, d / "form.png", dry_run, log, acc)
                status = "applied" if result == "confirmed" else "dry_run"
                db.update(job.key, status=status, reason=result, resume_path=str(files["RESUME"]),
                          cover_path=str(files.get("COVER_LETTER", "")), screenshot=str(d / "form.png"),
                          attempts=row["attempts"] + (0 if dry_run else 1))
                log(f"    ✓ {status}")
                done += 1
            except sub.Blocked as e:
                db.update(job.key, status="blocked", reason=str(e), attempts=row["attempts"] + 1)
                dead.add(ck)
                log(f"    ✗ blocked: {e}")
            except sub.Unanswerable as e:
                db.update(job.key, status="skipped", reason=str(e), attempts=row["attempts"] + 1)
                dead.add(ck)
                log(f"    ✗ skipped: {e}")
            except Exception as e:
                db.update(job.key, status="failed", reason=str(e).splitlines()[0][:300],
                          attempts=row["attempts"] + 1)
                log(f"    ✗ failed: {str(e).splitlines()[0][:200]}")
                if page:
                    try:
                        page.screenshot(path=str(d / "error.png"), full_page=True)
                    except Exception:
                        pass
            finally:
                if page:
                    page.close()
            if not dry_run:
                time.sleep(random.uniform(*s.get("delay_seconds", [20, 60])))
        browser.close()
    return finish(cfg, db, run_start, log, base, today)


def finish(cfg, db, run_start, log, base, today):
    rows = db.since(run_start)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["status"], []).append(r)
    order = ["applied", "dry_run", "blocked", "skipped", "failed", "queued", "low_score", "filtered"]
    counts = ", ".join(f"{len(groups[k])} {k}" for k in order if k in groups) or "nothing new"
    lines = [f"# autoapply — {today}", "", f"**{counts}**", ""]
    for k in order[:6]:
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
    rp.write_text("\n".join(lines))
    log(f"Summary: {counts}. Report: {rp}")

    hook = (cfg.get("notify") or {}).get("webhook_url")
    if hook:
        applied = groups.get("applied", [])
        text = f"autoapply {today}: {counts}" + "".join(
            f"\n• {r['title']} @ {r['company']}" for r in applied[:15])
        try:
            requests.post(hook, json={"text": text, "content": text}, timeout=10)
        except Exception as e:
            log(f"  ! notify failed: {e}")
    return groups


def main():
    ap = argparse.ArgumentParser(prog="autoapply", description="Hands-off job applier")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="fill forms and screenshot them, but never submit")
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
