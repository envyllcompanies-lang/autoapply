"""Employers' own job boards found in Common Crawl's open web index (index.commoncrawl.org), so the bot reads boards
that neither your company lists nor the daily snapshot (job-board-data) cover.

Common Crawl publishes a new index about once a month. When one appears, the pages it archived under Greenhouse, Lever
and Ashby are listed (only the addresses, a few requests in a row, with pauses: the index server is busy and answers 503
when pressed), each board name in them that the bot does not know yet is looked up on that system's public job feed,
and boards with open jobs are added to discovered_boards.json, which every search reads. Nothing is applied to here.
On 2026-10-27 one index page of 5 named 39 Greenhouse boards with 1,230 open jobs that the snapshot did not have.
Only Greenhouse by default (search.ccboards_sites): Lever and Ashby stop most automated applications at a human check,
so their boards would mostly add to your apply-by-hand list.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

INDEX = "https://index.commoncrawl.org"
UA = {"User-Agent": "autoapply-board-discovery (personal job search; reads public job feeds)"}
PREFIXES = {"greenhouse": ("job-boards.greenhouse.io/", "boards.greenhouse.io/"), "lever": ("jobs.lever.co/",),
            "ashby": ("jobs.ashbyhq.com/",)}
TOKEN_RX = {"greenhouse": re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)"),
            "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
            "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)")}
NOT_A_BOARD = {"embed", "jobs", "www", "careers", "apply", "app", "api", "j", "static", "assets", "robots.txt", "favicon.ico", ""}


def _get(url: str, params: dict | None = None, tries: int = 4, timeout: int = 60, pause: float = 2.0):
    """One request to the index, retried with growing pauses on 503 / 429 / a dropped connection. None when it never answers."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code not in (429, 500, 502, 503, 504):
                return None
        except requests.RequestException:
            pass
        time.sleep(pause * (2 ** i))
    return None


def latest_index() -> str:
    r = _get(f"{INDEX}/collinfo.json")
    try:
        return (r.json() or [{}])[0].get("id", "") if r is not None else ""
    except Exception:
        return ""


def board_names(index: str, ats: str, deadline: float, log=print) -> set:
    """Board names under one system's addresses in this index (lower-case), as far as the time allows."""
    out: set = set()
    for prefix in PREFIXES[ats]:
        api = f"{INDEX}/{index}-index"
        r = _get(api, {"url": prefix, "matchType": "prefix", "showNumPages": "true"})
        try:
            pages = int(r.json().get("pages", 0)) if r is not None else 0
        except Exception:
            pages = 0
        for page in range(pages):
            if time.time() > deadline:
                log(f"  ccboards: time is up after page {page} of {pages} for {prefix} (the rest next index)")
                return out
            r = _get(api, {"url": prefix, "matchType": "prefix", "output": "json", "fl": "url", "page": page})
            if r is None:
                continue
            for m in TOKEN_RX[ats].finditer(r.text):
                t = m.group(1).lower()
                if t not in NOT_A_BOARD and len(t) > 1:
                    out.add(t)
            time.sleep(1.5)                    # one request at a time, with a pause: the index is shared by everyone
    return out


def _openings(ats: str, token: str) -> int:
    """Open jobs on this board's public feed (0 when it is gone or empty)."""
    try:
        if ats == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", headers=UA, timeout=15)
            return len(r.json().get("jobs") or []) if r.ok else 0
        if ats == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"}, headers=UA, timeout=15)
            d = r.json() if r.ok else []
            return len(d) if isinstance(d, list) else 0
        if ats == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", headers=UA, timeout=15)
            return len((r.json() or {}).get("jobs") or []) if r.ok else 0
    except Exception:
        return 0
    return 0


def discover(base: Path, known: dict, log=print, index: str = "", minutes: float = 6.0, max_new: int = 600,
             sites: tuple = ("greenhouse",)) -> dict:
    """{ats: [new board names with open jobs]} from this Common Crawl index, written into discovered_boards.json.
    known: {ats: set of board names already read (your lists, earlier discoveries, the daily snapshot)}."""
    index = index or latest_index()
    if not index:
        log("  ccboards: Common Crawl's index list could not be read (tried again next run)")
        return {}
    deadline = time.time() + minutes * 60
    path = Path(base) / "discovered_boards.json"
    try:
        saved = json.loads(path.read_text())
    except Exception:
        saved = {}
    found: dict = {}
    for ats in [a for a in PREFIXES if a in sites]:
        names = board_names(index, ats, deadline, log)
        have = {str(x).lower() for x in (known.get(ats) or ())} | {str(x).lower() for x in saved.get(ats, [])}
        fresh = sorted(names - have)[:max_new]
        if not fresh:
            continue
        with ThreadPoolExecutor(8) as ex:
            counts = list(ex.map(lambda t: _openings(ats, t), fresh))
        live = [t for t, n in zip(fresh, counts) if n > 0]
        found[ats] = live
        log(f"  ccboards: {ats}: {len(names)} boards in Common Crawl's {index}, {len(fresh)} new to the bot, "
            f"{len(live)} with open jobs ({sum(counts):,} jobs) added")
        if live:
            lst = saved.setdefault(ats, [])
            lst += [t for t in live if t not in lst]
            path.write_text(json.dumps(saved, indent=1))
    return found
