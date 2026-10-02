"""End-of-run summary: an email to the applicant address (over the same Gmail app password the inbox reader uses) plus a
exception list for postings the bot could not or should not submit itself."""
from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from email.message import EmailMessage

import requests

MANUAL_STATUSES = ("blocked", "unconfirmed", "failed")
# reasons that are not worth reporting as an exception
NOISE = ("no application form found", "not a real application form", "posting no longer listed", "same role at same company",
         "already applied", "same posting already handled", "could not find the employer")


def manual_rows(rows, min_score: int = 55, limit: int = 25):
    """Best-scoring exceptions worth reporting; the unattended runner does not create a human-work queue."""
    out = []
    for r in rows:
        if r["status"] not in MANUAL_STATUSES or (r["score"] or 0) < min_score:
            continue
        why = (r["reason"] or "")
        if r["status"] != "manual" and any(n in why for n in NOISE):
            continue
        out.append(r)
    out.sort(key=lambda r: -(r["score"] or 0))
    return out[:limit]


def _link(r):
    return r["apply_url"] or r["url"] or ""


def why_lines(groups: dict, limit: int = 6) -> list[str]:
    """One line per common reason postings did not go through, so a quiet run explains itself."""
    tally: dict[tuple, int] = {}
    for status in ("blocked", "failed", "skipped", "unconfirmed"):
        for r in groups.get(status, []):
            why = re.sub(r"\s+", " ", (r["reason"] or "")).strip()
            if not why or any(n in why for n in NOISE):
                continue
            key = (status, why[:90])
            tally[key] = tally.get(key, 0) + 1
    top = sorted(tally.items(), key=lambda kv: -kv[1])[:limit]
    return [f"  - {status} x{n}: {why}" for (status, why), n in top]


def _fit(r) -> str:
    try:
        return f", match {r['fit']}%" if r["fit"] and r["fit"] > 0 else ""
    except (KeyError, IndexError, TypeError):
        return ""


def _stage(r) -> str:
    try:
        return f" (stopped at: {r['stage']})" if r["stage"] else ""
    except (KeyError, IndexError, TypeError):
        return ""


def build_text(today: str, counts: str, groups: dict, manual: list, run_url: str = "", footer: str = "", paused: list | None = None,
               by_site: list | None = None, needs: list | None = None) -> str:
    lines = [f"autoapply {today}: {counts}", ""]
    if needs:
        lines.append(f"NEEDS YOU ({len(needs)}): the employer asked for something the bot cannot do")
        lines += [f"  - {x}" for x in needs] + [""]
    applied = groups.get("applied", [])
    if applied:
        lines.append(f"APPLIED ({len(applied)})")
        lines += [f"  - {r['title']} @ {r['company']}  (score {r['score']}{_fit(r)})\n    {r['url']}" for r in applied[:40]]
        lines.append("")
    unc = groups.get("unconfirmed", [])
    if unc:
        lines.append(f"SUBMITTED BUT NOT CONFIRMED ({len(unc)}): check this inbox for each company's reply before applying again")
        lines += [f"  - {r['title']} @ {r['company']}\n    {r['url']}" for r in unc[:15]]
        lines.append("")
    if manual:
        lines.append(f"EXCEPTIONS ({len(manual)}): these were not submitted automatically")
        for r in manual:
            lines.append(f"  - [{r['score']}] {r['title']} @ {r['company']}\n    {_link(r)}\n    why: {(r['reason'] or '')[:150]}{_stage(r)}")
        lines.append("")
    if not applied and not manual:
        lines.append("No applications were submitted this run. The bot is running; this is what happened instead:")
    why = why_lines(groups)
    if why:
        lines += (["WHY OTHERS DID NOT GO THROUGH"] if (applied or manual) else []) + why + [""]
    elif not applied and not manual:
        lines += ["  (nothing new was found to apply to)", ""]
    if by_site:
        lines += ["BY SITE (this run)"] + by_site + [""]
    if paused:
        lines += ["SITES THAT KEEP ENDING AT A HUMAN CHECK (still tried, but after the sites that finish)"]
        lines += [f"  - {x}" for x in paused] + [""]
    if run_url:
        lines += [f"Run log and résumé PDFs: {run_url}"]
    if footer:
        lines += [footer]
    return "\n".join(lines)


def build_html(text: str) -> str:
    body = html.escape(text).replace("\n", "<br>")
    return f"<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.45'>{body}</div>"


def send_email(cfg: dict, subject: str, text: str, log=print) -> bool:
    n = cfg.get("notify") or {}
    if n.get("email", True) is False:
        return False
    user, pw = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASS")
    to = n.get("email_to") or (cfg.get("accounts") or {}).get("email") or (cfg.get("facts") or {}).get("account_email") or user
    if not (user and pw and to):
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(text)
    msg.add_alternative(build_html(text), subtype="html")
    try:
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, context=ssl.create_default_context(), timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        log(f"Summary emailed to {to}")
        return True
    except Exception as e:
        log(f"  ! summary email failed: {str(e)[:120]}")
        return False


def send_webhook(cfg: dict, text: str, log=print):
    hook = (cfg.get("notify") or {}).get("webhook_url")
    if not hook:
        return
    try:
        requests.post(hook, json={"text": text[:1800], "content": text[:1800]}, timeout=10)
    except Exception as e:
        log(f"  ! notify failed: {e}")
