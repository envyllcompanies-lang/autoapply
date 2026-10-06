"""Save jobs read from LinkedIn's PUBLIC no-login search (the pages anyone sees in a browser) into feeds/linkedin.json.

Run this on a home computer: LinkedIn refuses GitHub's servers, but answers an ordinary home connection at low volume. It reads
plain public pages at a polite pace and stops when LinkedIn rate-limits, blocks or asks for a login: nothing is done to get
around that. An empty or blocked read never overwrites an earlier good feed. The cloud bot then reads feeds/ (see
aggregators.localfeed) and applies on the employer's own form.

    python -m autoapply.exporter            # read and save feeds/linkedin.json
    python -m autoapply.exporter --push     # ...and commit + push the file to GitHub
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from . import aggregators


def export(cfg: dict, outdir: Path, log=print) -> int:
    """Read LinkedIn's public search through the existing reader and save what it found. Returns how many jobs were saved."""
    jobs = aggregators.linkedin(cfg)
    if not jobs:
        log("LinkedIn returned no jobs (blocked, rate-limited or nothing matched): the saved feed is left as it was")
        return 0
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    rows = [{"title": j.title, "company": (j.extra or {}).get("company_name") or j.company, "location": j.location,
             "apply_url": j.apply_url, "url": j.url, "id": j.job_id, "posted": datetime.now().date().isoformat()} for j in jobs if j.apply_url]
    (outdir / "linkedin.json").write_text(json.dumps({"exported_at": datetime.now().isoformat(timespec="seconds"),
                                                      "source": "linkedin-public", "jobs": rows}, indent=1))
    log(f"saved {len(rows)} LinkedIn jobs to {outdir / 'linkedin.json'}")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--push", action="store_true", help="commit and push feeds/linkedin.json")
    args = ap.parse_args()
    base = Path(__file__).resolve().parent.parent
    from .main import merge_settings
    cfg = merge_settings(yaml.safe_load((base / args.config).read_text()), base)
    n = export(cfg, base / "feeds")
    if n and args.push:
        def git(*a):
            return subprocess.run(["git", *a], cwd=base, capture_output=True, text=True)
        git("add", "feeds/linkedin.json")
        if git("diff", "--cached", "--quiet").returncode:
            git("commit", "-m", f"LinkedIn public listings {datetime.now():%Y-%m-%d %H:%M}")
            git("pull", "--rebase", "origin", "main")
            r = git("push", "origin", "main")
            print("pushed" if r.returncode == 0 else f"push failed: {r.stderr[-200:]}")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
