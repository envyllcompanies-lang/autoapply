"""Probe: open real application forms on GitHub's servers, fill them exactly the way a live run does, and NEVER submit.

Writes everything needed to see what went wrong on each form into probe/out/<request id>/:
  steps.json   the fields found on each step, the answer chosen for each, what the page actually holds afterwards,
               required boxes still empty (with their HTML), and clickable things the field finder did not recognise
  stepN.html   the page after filling (so forms can be studied and replayed offline)
  stepN.jpg    screenshot
  net.json     requests the form made (method, url, status, short body for errors)
  log.txt      the same lines a live run would log

Driven by probe/request.yaml (pushing a change to it on the 'probe' branch starts a run):
  id: 3
  pick: {workable: 4, lever: 3, workday: 3, greenhouse: 2}   # jobs per site, best scores first, from the history database
  urls: []                                                   # or exact posting URLs
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import yaml

from . import submit as sub, auth, mailbox, render
from .brain import Brain
from .sources import Job
from .db import DB
from .main import UA, merge_board_file, merge_settings, ats_of, slug, Logger
from .aggregators import direct_apply_url

READBACK_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const e of document.querySelectorAll('[data-aa]')) {
    out.push({ id: e.dataset.aa, tag: e.tagName, type: e.getAttribute('type') || '', value: (e.value || '').slice(0, 120),
               checked: !!e.checked, pressed: e.getAttribute('aria-pressed'), achecked: e.getAttribute('aria-checked'),
               text: e.tagName === 'BUTTON' ? clean(e.innerText).slice(0, 40) : '',
               cls: String(e.className || '').slice(0, 100) });
  }
  return out;
}
"""

# clickable/typable things the field finder did not tag: shows what kinds of controls a site uses that we miss
UNTAGGED_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const sel = '[role=radio],[role=radiogroup],[role=checkbox],[role=switch],[role=combobox],[role=listbox],[role=option],' +
              '[role=button],[contenteditable=true],button,input,textarea,select,[aria-pressed],[aria-checked],[data-ui]';
  const out = []; const seen = new Set();
  for (const e of document.querySelectorAll(sel)) {
    if (e.dataset.aa || !vis(e)) continue;
    const t = e.getAttribute('type') || '';
    if (['hidden', 'submit'].includes(t)) continue;
    let box = e.parentElement; for (let i = 0; i < 3 && box && clean(box.innerText).length < 15; i++) box = box.parentElement;
    const key = e.tagName + (e.getAttribute('role') || '') + clean(e.innerText).slice(0, 30) + (box ? clean(box.innerText).slice(0, 60) : '');
    if (seen.has(key)) continue; seen.add(key);
    out.push({ tag: e.tagName, role: e.getAttribute('role'), type: t, text: clean(e.innerText).slice(0, 60),
               aria: e.getAttribute('aria-label'), dataui: e.getAttribute('data-ui'), name: e.getAttribute('name'),
               context: box ? clean(box.innerText).slice(0, 160) : '', html: e.outerHTML.slice(0, 400) });
    if (out.length >= 60) break;
  }
  return out;
}
"""

EMPTY_REQUIRED_JS = r"""
(ids) => ids.map(id => {
  const e = document.querySelector(`[data-aa="${id}"]`);
  if (!e) return { id, html: '(element gone)' };
  let box = e; for (let i = 0; i < 4 && box.parentElement && box.outerHTML.length < 1500; i++) box = box.parentElement;
  return { id, html: box.outerHTML.slice(0, 3500) };
})
"""


def _slim(html: str) -> str:
    """The page without scripts, styles and inline SVG (Lever pages are megabytes of them), capped at 900 KB."""
    html = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>", "", html, flags=re.S | re.I)
    return html[:900000]


def _pick_jobs(db: DB, req: dict) -> list[dict]:
    rows = []
    for u in req.get("urls") or []:
        r = db.conn.execute("SELECT * FROM jobs WHERE apply_url=? OR url=? LIMIT 1", (u, u)).fetchone()
        rows.append(dict(r) if r else {"key": "probe:" + u, "source": "web", "company": "(url)", "title": "(url)",
                                        "location": "", "url": u, "apply_url": u, "score": 0})
    want = {k.lower(): int(v) for k, v in (req.get("pick") or {}).items()}
    if want:
        got = {k: 0 for k in want}
        seen_co = set()
        for r in db.conn.execute("SELECT * FROM jobs WHERE status IN ('queued','unconfirmed','blocked','manual','failed') "
                                 "ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, score DESC"):
            r = dict(r)
            job = _job(r)
            a = ats_of(job)
            a = "workday" if "myworkdayjobs" in (r.get("apply_url") or "") else a
            if a in want and got[a] < want[a] and (r["company"] or "").lower() not in seen_co:
                got[a] += 1
                seen_co.add((r["company"] or "").lower())
                rows.append(r)
    return rows


def _job(r: dict) -> Job:
    return Job(source=r.get("source") or "web", company=r.get("company") or "", job_id=str(r.get("key", "")).split(":")[-1],
               title=r.get("title") or "", location=r.get("location") or "", url=r.get("url") or r.get("apply_url") or "",
               apply_url=r.get("apply_url") or r.get("url") or "", description="")


def main():
    base = Path(".").resolve()
    req = yaml.safe_load((base / "probe" / "request.yaml").read_text()) or {}
    out_root = base / "probe" / "out" / str(req.get("id", "x"))
    out_root.mkdir(parents=True, exist_ok=True)
    log = Logger(out_root / "log.txt")
    cfg = merge_settings(yaml.safe_load((base / "config.yaml").read_text()), base)
    merge_board_file(cfg, base)
    cfg.setdefault("accounts", {})["create_in_dry_run"] = bool(req.get("create_accounts", True))
    profile = yaml.safe_load((base / cfg.get("profile_file", "profile.yaml")).read_text())
    brain = Brain(cfg, profile, base, log)
    mailbox.preflight(log)
    try:                                          # which Groq models this key may use (names only)
        import requests
        r = requests.get("https://api.groq.com/openai/v1/models", timeout=20,
                         headers={"Authorization": f"Bearer {os.environ.get('GROQ_API_KEY', '')}"})
        ids = sorted(m.get("id", "") for m in (r.json().get("data") or []) if m.get("active", True))
        log(f"Groq models open to this key: {', '.join(ids) or r.status_code}")
    except Exception as e:
        log(f"Groq model list failed: {e}")
    db = DB(str(base / cfg.get("db", "applications.db")))
    rows = _pick_jobs(db, req)
    log(f"=== probe {req.get('id')}: {len(rows)} forms, fill only, nothing is submitted ===")
    sub.MAX_APPLY_SECONDS = int(req.get("max_seconds_per_job", 420))

    from playwright.sync_api import sync_playwright
    summary = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800}, locale="en-US")
        ctx.set_default_timeout(10000)
        ctx.set_default_navigation_timeout(30000)
        acc = auth.Accounts(cfg, base)
        sub.ACCOUNTS_ENABLED = acc.enabled
        pw = acc.password or ""
        rules = {"8+ characters": len(pw) >= 8, "uppercase": bool(re.search(r"[A-Z]", pw)), "lowercase": bool(re.search(r"[a-z]", pw)),
                 "number": bool(re.search(r"\d", pw)), "special character": bool(re.search(r"[^A-Za-z0-9]", pw))}
        missing = [k for k, ok in rules.items() if not ok]
        log("ACCOUNT_PASSWORD meets Workday's usual rules" if not missing else f"ACCOUNT_PASSWORD is missing: {', '.join(missing)}")
        log(f"Accounts: {'ON' if acc.enabled else 'OFF'} (password set: {bool(acc.password)}, email set: {bool(acc.email)}, create in probe: {acc.create_in_dry_run})")
        for n, r in enumerate(rows, 1):
            job = _job(r)
            ats = ats_of(job)
            d = out_root / f"{n:02d}-{slug(ats)[:14]}-{slug(job.company)[:20]}-{slug(job.title)[:30]}"
            d.mkdir(parents=True, exist_ok=True)
            log(f"→ [{ats}] {job.title} @ {job.company}  {job.apply_url}")
            steps, net = [], []
            page = ctx.new_page()

            def on_resp(resp, _net=net):
                try:
                    rq = resp.request
                    if rq.resource_type in ("xhr", "fetch", "document") and (rq.method != "GET" or resp.status >= 400):
                        body = ""
                        if resp.status >= 400 or rq.method != "GET":
                            try:
                                body = resp.text()[:600]
                            except Exception:
                                pass
                        _net.append({"m": rq.method, "url": rq.url[:200], "status": resp.status, "body": body})
                except Exception:
                    pass
            page.on("response", on_resp)

            real_fill = sub.fill

            def spy_fill(pg, fields, answers, files, log=log, deadline=None, _steps=steps, _d=d):
                real_fill(pg, fields, answers, files, log, deadline)
                i = len(_steps) + 1
                try:
                    pg.wait_for_timeout(800)
                    back = {x["id"]: x for x in pg.evaluate(READBACK_JS)}
                    empty = []
                    for f in fields:
                        if not f.get("required"):
                            continue
                        ids = f.get("option_ids") or [f["id"]]
                        vals = [back.get(x, {}) for x in ids]
                        filled = any(v.get("value") or v.get("checked") or v.get("pressed") == "true" or v.get("achecked") == "true"
                                     for v in vals) if f["kind"] != "file" else bool(answers.get(f["id"]))
                        if not filled:
                            empty.append(f["id"] if not f.get("option_ids") else f["option_ids"][0])
                    steps.append({"step": i, "url": pg.url, "fields": fields, "answers": {k: (v if not isinstance(v, str) else v[:300]) for k, v in answers.items()},
                                  "readback": back, "required_still_empty": pg.evaluate(EMPTY_REQUIRED_JS, empty),
                                  "untagged_controls": pg.evaluate(UNTAGGED_JS)})
                    (_d / f"step{i}.html").write_text(_slim(pg.content()))
                    pg.screenshot(path=str(_d / f"step{i}.jpg"), full_page=True, type="jpeg", quality=45)
                except Exception as e:
                    steps.append({"step": i, "error": f"probe capture failed: {e}"})
            sub.fill = spy_fill
            result = "?"
            try:
                if job.source.startswith("agg-"):
                    try:
                        job.apply_url = sub.resolve_apply_url(page, job.apply_url, log)
                    except sub.Blocked as e:
                        direct = direct_apply_url(job, log) if "could not find the employer" in str(e) else None
                        if not direct:
                            raise
                        job.apply_url = direct
                sub.open_form(page, job.apply_url)
                try:
                    job.description = page.inner_text("body")[:8000]
                except Exception:
                    pass
                (d / "landing.html").write_text(_slim(page.content()))
                page.screenshot(path=str(d / "landing.jpg"), full_page=False, type="jpeg", quality=45)
                files = {"RESUME": base / str(cfg.get("resume_file") or "briandelgado_resume.pdf"),
                         "_LETTER_MAKER": (lambda txt, _d=d: render.letter_pdf(browser, txt, _d / "cover_letter.pdf",
                                                                               name=profile.get("name", ""), contact=profile.get("contact_line", "")))}
                resume_md, letter = brain.tailor(job)
                result = sub.apply(page, job, brain, letter, files, d / "final.png", True, log, acc)
                log(f"    = {result} (form filled; not submitted)")
            except Exception as e:
                result = f"{type(e).__name__}: {str(e)[:300]}"
                log(f"    ✗ {result}")
                try:
                    (d / "at_error.html").write_text(_slim(page.content()))
                    page.screenshot(path=str(d / "at_error.jpg"), full_page=True, type="jpeg", quality=45)
                except Exception:
                    pass
                if not isinstance(e, (sub.Blocked, sub.Unanswerable, sub.Unconfirmed, sub.NotSubmitted)):
                    (d / "traceback.txt").write_text(traceback.format_exc())
            finally:
                sub.fill = real_fill
                for f in d.glob("*.png"):          # the apply() screenshots are big PNGs: keep a small JPEG instead
                    try:
                        from PIL import Image
                        Image.open(f).convert("RGB").save(f.with_suffix(".jpg"), quality=45)
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass
                (d / "steps.json").write_text(json.dumps(steps, indent=1, default=str)[:3_000_000])
                (d / "net.json").write_text(json.dumps(net[-150:], indent=1))
                summary.append({"n": n, "ats": ats, "company": job.company, "title": job.title, "url": job.apply_url, "result": result,
                                "steps": len(steps), "empty_required": sum(len(s.get("required_still_empty") or []) for s in steps)})
                try:
                    page.close()
                except Exception:
                    pass
        browser.close()
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1))
    log("=== probe done ===")
    for s in summary:
        log(f"  {s['n']:2d} {s['ats']:12} {s['company'][:18]:18} {s['title'][:40]:40} steps={s['steps']} empty_req={s['empty_required']} | {s['result'][:120]}")


if __name__ == "__main__":
    main()
