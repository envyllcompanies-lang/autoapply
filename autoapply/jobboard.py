"""Daily snapshot of ~1.4 million open jobs from 20,000+ company career sites (Workday, Greenhouse, Lever, Ashby, BambooHR,
iCIMS, Paylocity), published by the open-source job-board-aggregator project at github.com/Feashliaa/job-board-data
(data CC BY-NC 4.0: fine for a personal job search). Each row has title, company, location, the site it is on, an
estimated level (entry/mid/senior/intern), a salary estimate, the direct posting link and when it was first seen.

The bot downloads it once per job search (~75 MB of gzipped JSON, a few seconds on GitHub's servers), keeps entry-level
rows that pass the usual title and location filters, and hands them to the normal scoring and apply steps. Postings have
no description here; the posting page itself is read (and re-checked for level and pay) right before applying.
"""
from __future__ import annotations

import gzip
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import Job, prefilter

REPO = "https://github.com/Feashliaa/job-board-data"
ATS_SOURCE = {"Workday": "workday", "Greenhouse": "greenhouse", "Lever": "lever", "Ashby": "ashby", "BambooHR": "bamboohr",
              "iCIMS": "icims", "Paylocity": "paylocity"}


def _fetch(dest: Path, log) -> bool:
    """Shallow, sparse clone of the snapshot's data folder (refreshed when older than 20 hours)."""
    marker = dest / ".fetched"
    if marker.exists() and time.time() - marker.stat().st_mtime < 20 * 3600:
        return True
    try:
        if dest.exists():
            subprocess.run(["rm", "-rf", str(dest)], check=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", REPO, str(dest)],
                       check=True, timeout=240)
        subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone", "/data/chunks/*"],
                       check=True, timeout=300, capture_output=True)
        marker.touch()
        return True
    except Exception as e:
        log(f"  agg/jobboard: download failed ({str(e)[:120]})")
        return False


def _id(url: str) -> str:
    m = re.search(r"(?:/jobs?/|_)([A-Za-z0-9-]{5,})/?$", url or "")
    return m.group(1) if m else re.sub(r"\W+", "-", (url or "")[-60:])


def discover_jobboard(cfg: dict, base: Path, log=print, known_urls: set | None = None) -> list[Job]:
    agg = cfg.get("aggregators") or {}
    a = agg.get("jobboard") or {}
    if a.get("enabled", True) is False or (agg.get("enabled") is False and "jobboard" not in agg):
        return []                              # switched off (or all aggregators are, and this one was not asked for by name)
    search = cfg.get("search") or {}
    levels = set(a.get("levels", ["entry"]))
    sites = set(a.get("sites", ["Workday", "Greenhouse", "Lever", "BambooHR"]))   # sites the bot can fill
    max_age = int(a.get("max_age_days", 14))
    cap = int(a.get("max_jobs", 6000))
    dest = Path(base) / "logs" / "jobboard"
    if not _fetch(dest, log):
        return []
    since = datetime.now(timezone.utc) - timedelta(days=max_age)
    known_urls = known_urls or set()
    out, n_rows, n_level = [], 0, 0
    for f in sorted((dest / "data" / "chunks").glob("*.json.gz")):
        try:
            rows = json.load(gzip.open(f))
        except Exception:
            continue
        for r in rows:
            n_rows += 1
            if r.get("ats") not in sites or r.get("skill_level") not in levels:
                continue
            n_level += 1
            url = (r.get("url") or "").strip()
            if not url or url in known_urls:
                continue
            try:
                seen = datetime.fromisoformat(str(r.get("first_seen", "")).replace("Z", "+00:00"))
                if seen < since:
                    continue
            except Exception:
                pass
            src = ATS_SOURCE.get(r["ats"], "web")
            # (the snapshot's salary figures are estimates from similar jobs, not this posting's pay: not used)
            desc = f"{r.get('title', '')} at {r.get('company', '')}. Location: {r.get('location', '')}. Entry level."
            j = Job(source=src, company=str(r.get("company") or ""), job_id="jb-" + _id(url), title=str(r.get("title") or "").strip(),
                    location=str(r.get("location") or ""), url=url, apply_url=url, description=desc)
            if prefilter(j, search):
                continue
            out.append(j)
            if len(out) >= cap:
                break
        if len(out) >= cap:
            break
    log(f"  agg/jobboard: {n_rows:,} jobs in today's snapshot, {n_level:,} entry-level on sites the bot can fill, "
        f"{len(out)} pass your title/location filters")
    return out
