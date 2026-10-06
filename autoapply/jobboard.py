"""Daily snapshot of ~1.4 million open jobs from 20,000+ company career sites (Workday, Greenhouse, Lever, Ashby, BambooHR,
iCIMS, Paylocity), published by the open-source job-board-aggregator project at github.com/Feashliaa/job-board-data
(data CC BY-NC 4.0: fine for a personal job search). Each row has title, company, location, the site it is on, an
estimated level (entry/mid/senior/intern), a salary estimate, the direct posting link and when it was first seen.

The bot looks at it whenever the snapshot has changed (once a day): ~75 MB of gzipped JSON, a few seconds on GitHub's
servers. It keeps rows that pass the usual title and location filters and hands them to the normal scoring and apply steps.
Postings have no description here; the posting itself is read (and re-checked for level and pay) right before applying.

The snapshot's level is only an estimate from the title: 'mid' is what it calls everything that is not plainly entry or
senior, and most coordinator / analyst / associate postings sit there. So 'mid' rows are read too, and the bot's own level
rules (level.py), the posting's real text and the résumé match decide. Reading only 'entry' rows missed about 95% of the
postings that pass your filters.
"""
from __future__ import annotations

import gzip
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import level
from .sources import Job, prefilter, level_title

REPO = "https://github.com/Feashliaa/job-board-data"
ATS_SOURCE = {"Workday": "workday", "Greenhouse": "greenhouse", "Lever": "lever", "Ashby": "ashby", "BambooHR": "bamboohr",
              "iCIMS": "icims", "Paylocity": "paylocity"}
LEVELS = ("entry", "mid")
SITES = ("Workday", "Greenhouse", "Lever", "BambooHR", "Ashby")      # sites the bot can fill


def remote_version(timeout: int = 40) -> str:
    """The snapshot's current version (its latest commit), read without downloading it. '' when it cannot be read."""
    try:
        out = subprocess.run(["git", "ls-remote", REPO, "HEAD"], check=True, timeout=timeout, capture_output=True, text=True).stdout
        return (out.split() or [""])[0]
    except Exception:
        return ""


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


def _posted(raw) -> str:
    """The snapshot's 'first seen' as a plain date-time ('' when it has none)."""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        return ""


def rows_to_jobs(rows, search: dict, known_urls: set, known_keys: set, levels=LEVELS, sites=SITES, max_age: int = 30,
                 max_level: int = 1, cap: int = 6000, out: list | None = None, tally: dict | None = None) -> list[Job]:
    """Turn snapshot rows into Jobs: the wanted sites and levels, new enough, passing the title/location filters and the
    title-level rules, and not already in the database (by its address, or by the key the same posting has when it comes
    straight from the employer's board, so a posting found both ways is one posting)."""
    from .aggregators import canon_key
    out = [] if out is None else out
    tally = tally if tally is not None else {}
    inc = [str(x).lower() for x in (search.get("titles_include") or [])]
    since = datetime.now(timezone.utc) - timedelta(days=max_age)
    sites, levels = set(sites), set(levels)
    for r in rows:
        tally["rows"] = tally.get("rows", 0) + 1
        lvl = r.get("skill_level")
        if r.get("ats") not in sites or lvl not in levels:
            continue
        tally["level"] = tally.get("level", 0) + 1
        title = str(r.get("title") or "").strip()
        if inc and not any(k in level_title(title).lower() for k in inc):      # the quick test first: most rows end here
            continue
        url = (r.get("url") or "").strip()
        if not url or url in known_urls:
            continue
        try:
            if datetime.fromisoformat(str(r.get("first_seen", "")).replace("Z", "+00:00")) < since:
                continue
        except Exception:
            pass
        src = ATS_SOURCE.get(r["ats"], "web")
        company = str(r.get("company") or "")
        ck = canon_key(url)
        if ck and ck.split(":", 1)[0] == src:
            _, company, job_id = ck.split(":", 2)          # the same key the employer's own board gives this posting
        else:
            job_id = "jb-" + _id(url)
        legacy = f"{src}:{r.get('company') or ''}:jb-{_id(url)}"       # how rows from the snapshot were keyed before
        key = f"{src}:{company}:{job_id}"
        if key in known_keys or legacy in known_keys:
            continue
        # (the snapshot's salary figures are estimates from similar jobs, not this posting's pay: not used)
        desc = f"{title} at {r.get('company', '')}. Location: {r.get('location', '')}." + (" Entry level." if lvl == "entry" else "")
        j = Job(source=src, company=company, job_id=job_id, title=title, location=str(r.get("location") or ""), url=url,
                apply_url=url, description=desc, extra={"posted": _posted(r.get("first_seen")), "snapshot_level": lvl})
        if prefilter(j, search):
            continue
        lv = level.classify(title)
        if not lv.eligible or lv.level > max_level:
            continue                                        # a title the level rules drop anyway: not worth a row in the history
        known_keys.add(key)
        out.append(j)
        if len(out) >= cap:
            break
    return out


def discover_jobboard(cfg: dict, base: Path, log=print, known_urls: set | None = None, known_keys: set | None = None,
                      state: dict | None = None) -> list[Job]:
    """state: {'version': <the snapshot version that was last read in full>}. When the snapshot has not changed since,
    nothing is downloaded or read again; after a full read the dict holds the new version for the caller to save."""
    agg = cfg.get("aggregators") or {}
    a = agg.get("jobboard") or {}
    if a.get("enabled", True) is False or (agg.get("enabled") is False and "jobboard" not in agg):
        return []                              # switched off (or all aggregators are, and this one was not asked for by name)
    search = cfg.get("search") or {}
    levels = set(a.get("levels", LEVELS))
    sites = set(a.get("sites", SITES))
    max_age = int(a.get("max_age_days", 30))
    cap = int(a.get("max_jobs", 6000))
    state = state if state is not None else {}
    version = remote_version()
    if version and state.get("version") == version:
        log("  agg/jobboard: today's snapshot was already read (it changes once a day)")
        return []
    dest = Path(base) / "logs" / "jobboard"
    if not _fetch(dest, log):
        return []
    known_urls = known_urls or set()
    known_keys = set(known_keys or ())
    out: list[Job] = []
    tally: dict = {}
    for f in sorted((dest / "data" / "chunks").glob("*.json.gz")):
        try:
            rows = json.load(gzip.open(f))
        except Exception:
            continue
        rows_to_jobs(rows, search, known_urls, known_keys, levels, sites, max_age, int(search.get("max_level", 1)), cap, out, tally)
        if len(out) >= cap:
            break
    log(f"  agg/jobboard: {tally.get('rows', 0):,} jobs in today's snapshot, {tally.get('level', 0):,} at your level on sites the bot "
        f"can fill, {len(out)} new ones pass your title/location filters" + (f" (the first {cap}; the rest next time)" if len(out) >= cap else ""))
    if version and len(out) < cap:
        state["version"] = version                 # read in full: no need to look again until it changes
    return out
