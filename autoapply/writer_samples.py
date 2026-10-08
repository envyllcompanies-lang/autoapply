"""Writer samples (probe branch only): what the free writer produces for real postings, written exactly the way a live run
asks for it. Nothing is submitted anywhere. Output: probe/out/writer-<id>/samples.md

probe/request.yaml:
  id: w1
  writer_samples: 2        # postings, the best matched ones from the history database (or list urls: [...])
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from .brain import Brain
from .db import DB
from .main import merge_settings, row_job
from . import prescreen, writer as W

QUESTIONS = [
    ("essay", "Why are you interested in this role at {company}?"),
    ("essay", "Tell us about a time you improved a process or made an operation run better. What did you do and what was the result?"),
    ("essay", "What makes you a strong fit for this position?"),
    ("essay", "Describe a project you are proud of and your role in it."),
    ("short", "What is your current city and state?"),
    ("short", "What are your salary expectations for this role?"),
    ("choose", "How many years of experience do you have with project coordination?", ["Less than 1 year", "1-2 years", "3-5 years", "More than 5 years"]),
    ("choose", "Are you comfortable working in an office setting three days a week?", ["Yes", "No"]),
]


def run(req: dict, base: Path, out_dir: Path):
    cfg = merge_settings(yaml.safe_load((base / "config.yaml").read_text()), base)
    profile = yaml.safe_load((base / "profile.yaml").read_text())
    logs: list[str] = []
    log = lambda m: logs.append(str(m))
    W._USAGE_FILE = out_dir / "usage.json"          # its own count: the live runs' file is not touched
    brain = Brain(cfg, profile, base, log)
    w = brain.writer
    used: list[str] = []
    orig = w._chat

    def chat(p, messages, max_tokens, temperature=None):
        out = orig(p, messages, max_tokens, temperature)
        used.append(f"{p['name']} ({p['model'] if isinstance(p['model'], str) else p['model'][0]})")
        return out
    w._chat = chat
    db = DB(str(base / "applications.db"))
    n = int(req.get("writer_samples") or 2)
    rows = db.conn.execute("SELECT * FROM jobs WHERE fit >= 70 AND status IN ('applied','queued') AND apply_url != '' "
                           "ORDER BY (apply_url LIKE '%myworkdayjobs%') DESC, updated DESC LIMIT 40").fetchall()
    picked, seen = [], set()
    for r in rows:
        site = "workday" if "myworkdayjobs" in (r["apply_url"] or "") else "other"
        if site in seen and len(picked) < n - 1:
            continue
        job = row_job(r)
        desc = prescreen.fetch_description(job.apply_url or job.url)
        if len(desc) < 400:
            continue
        job.description = desc
        picked.append(job)
        seen.add(site)
        if len(picked) >= n:
            break
    md = [f"# Writer samples ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})", "",
          f"Writer: {w.status_line()}", ""]
    for job in picked:
        company = brain._company_name(job)
        md += [f"## {job.title} @ {company}", f"{job.apply_url}", "", "### Cover letter", ""]
        used.clear()
        t0 = time.time()
        try:
            letter = w.cover_letter(job, company, log)
        except Exception as e:
            letter = f"(no letter: {e})"
        md += [letter or "(no letter)", "", f"_written by: {', '.join(dict.fromkeys(used)) or 'nothing'}; {time.time() - t0:.0f}s; "
               f"{len((letter or '').split())} words_", ""]
        for q in QUESTIONS:
            kind, text = q[0], q[1].format(company=company)
            used.clear()
            try:
                if kind == "essay":
                    got = w.answer(job, company, text, W.limits_from_question(text, None), log)
                elif kind == "short":
                    got = w.short_answer(job, company, text, 150, log)
                else:
                    got = w.choose(job, company, text, q[2], log=log)
            except Exception as e:
                got = f"(no answer: {e})"
            md += [f"**Q ({kind}): {text}**", "", str(got), "", f"_by: {', '.join(dict.fromkeys(used)) or 'nothing'}_", ""]
    md += ["## Writer log", "", "```", *logs[-80:], "```", "", f"Allowance used by this test: {w.allowance_line()}"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "samples.md").write_text("\n".join(md))
    print("\n".join(md))
