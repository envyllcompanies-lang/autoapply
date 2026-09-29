"""Read verification emails from the applicant inbox over IMAP (a Gmail app password, read-only use).

Secrets (GitHub Actions):  IMAP_USER, IMAP_PASS   optional: IMAP_HOST (default imap.gmail.com)
The bot only ever looks for the newest messages that look like "verify your email / your code is ...".
"""
from __future__ import annotations

import email
import html
import imaplib
import os
import re
import time
from email.utils import parsedate_to_datetime

LINK_RX = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
GOOD_LINK = re.compile(r"verif|confirm|activate|validate|registration|token|account", re.I)
BAD_LINK = re.compile(r"unsubscribe|privacy|terms|facebook|twitter|linkedin\.com/company|instagram|\.(png|jpg|gif)\b", re.I)
HINT = re.compile(r"verif|confirm|activate|validate|welcome|one.?time|passcode|security code|your code|registration", re.I)
CODE_RX = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def configured() -> bool:
    return bool(os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASS"))


def _body(msg) -> str:
    parts = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() in ("text/plain", "text/html"):
            try:
                raw = p.get_payload(decode=True) or b""
                parts.append(raw.decode(p.get_content_charset() or "utf-8", "replace"))
            except Exception:
                continue
    return html.unescape("\n".join(parts))


def parse_message(raw: bytes) -> dict:
    """{'subject','from','to','ts','link','code'} for one raw email (link/code may be None)."""
    msg = email.message_from_bytes(raw)
    subj = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    body = _body(msg)
    try:
        ts = parsedate_to_datetime(msg.get("Date")).timestamp()
    except Exception:
        ts = time.time()
    link = None
    links = [u.rstrip(".,;") for u in LINK_RX.findall(body) if not BAD_LINK.search(u)]
    for u in links:
        if GOOD_LINK.search(u):
            link = u
            break
    if not link and links and HINT.search(subj):
        link = links[0]
    code = None
    if re.search(r"code|passcode|otp|one.?time|pin", subj + " " + body[:1500], re.I):
        m = CODE_RX.search(re.sub(r"<[^>]+>", " ", body))
        code = m.group(1) if m else None
    return {"subject": subj, "from": msg.get("From", ""), "to": msg.get("To", ""), "ts": ts, "link": link, "code": code,
            "hint": bool(HINT.search(subj)) or bool(link) or bool(code)}


def wait_for_verification(since_ts: float, host_hint: str = "", timeout: int = 150, log=print) -> dict | None:
    """Poll the inbox until a fresh verification email arrives. Returns {'link':..., 'code':...} or None."""
    user, pw = os.environ["IMAP_USER"], os.environ["IMAP_PASS"]
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    deadline = time.time() + timeout
    hint = (host_hint or "").lower()
    while time.time() < deadline:
        try:
            imap = imaplib.IMAP4_SSL(host)
            imap.login(user, pw)
            imap.select("INBOX")
            _, data = imap.search(None, "ALL")
            ids = data[0].split()[-12:]
            for i in reversed(ids):
                _, d = imap.fetch(i, "(RFC822)")
                info = parse_message(d[0][1])
                if info["ts"] < since_ts - 90 or not info["hint"]:
                    continue
                if hint and hint not in (info["from"] + info["subject"]).lower() and hint not in (info["link"] or "").lower():
                    # sender/link do not mention the site: still accept a clear verification message
                    if not re.search(r"verif|confirm|activate", info["subject"], re.I):
                        continue
                imap.logout()
                log(f"      mail: found '{info['subject'][:60]}'")
                return {"link": info["link"], "code": info["code"]}
            imap.logout()
        except Exception as e:
            log(f"      mail: {str(e)[:80]}")
        time.sleep(6)
    return None
