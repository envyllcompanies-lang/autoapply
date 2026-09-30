"""Read account-verification and confirmation emails from the applicant inbox over IMAP (a Gmail app password).

Secrets (GitHub Actions):  IMAP_USER, IMAP_PASS   optional: IMAP_HOST (default imap.gmail.com)
The bot only ever looks at the newest messages, and only for: account "verify your email" links / codes and
"we received your application" confirmations. It does not answer an employer's human-verification challenges.
"""
from __future__ import annotations

import email
import html
import imaplib
import os
import re
import time
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

LINK_RX = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
GOOD_LINK = re.compile(r"verif|confirm|activate|validate|registration|token|account", re.I)
BAD_LINK = re.compile(r"unsubscribe|privacy|terms|facebook|twitter|linkedin\.com/company|instagram|\.(png|jpg|gif)\b", re.I)
HINT = re.compile(r"verif|confirm|activate|validate|welcome|one.?time|passcode|security code|your code|registration", re.I)
CODE_RX = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
CONFIRM_SUBJECT = re.compile(r"thank(s| you) for (applying|your (application|interest))|application (received|submitted|confirmation)|"
                             r"(we(?:'ve|\u2019ve| have)? |successfully )received your (application|resume|r\u00e9sum\u00e9)|your application (to|for|with|at)\b|"
                             r"application (has been )?(received|submitted)|you('ve| have) applied", re.I)
NOT_CONFIRM = re.compile(r"security code|verification code|verify your|password|sign[- ]in|welcome to|job alert|newsletter|recommended", re.I)

_DISABLED = ""          # set to a reason once the login has been refused, so we stop hammering the server


def configured() -> bool:
    return bool(os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASS")) and not _DISABLED


def disabled_reason() -> str:
    return _DISABLED


def preflight(log=print) -> bool:
    """Try the mailbox login once at the start of a run. On a refused login, switch mail features off with a clear message."""
    global _DISABLED
    if not (os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASS")):
        _DISABLED = "IMAP_USER / IMAP_PASS are not set"
        log("MAIL OFF: IMAP_USER / IMAP_PASS are not set. No confirmation reading, no summary emails, no email-verified sign-ups.")
        return False
    try:
        imap = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"), timeout=30)
        imap.login(os.environ["IMAP_USER"], os.environ["IMAP_PASS"])
        imap.logout()
        log(f"mail: logged in to {os.environ['IMAP_USER']} OK")
        return True
    except Exception as e:
        _DISABLED = str(e)[:120]
        log(f"MAIL OFF: the inbox login for {os.environ['IMAP_USER']} was refused ({_DISABLED}). Use a Google APP PASSWORD "
            "(myaccount.google.com/apppasswords) as IMAP_PASS, and make sure IMAP is enabled in Gmail settings.")
        return False


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


def _plain_lines(body: str) -> list[str]:
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    txt = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|li|h\d|strong|b|span)>", "\n", txt)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
    return [ln.strip() for ln in txt.splitlines() if ln.strip()]


def parse_message(raw: bytes) -> dict:
    """{'subject','from','to','ts','link','code','hint','body'} for one raw email (link/code may be None)."""
    msg = email.message_from_bytes(raw)
    try:
        subj = str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:
        subj = msg.get("Subject", "") or ""
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
            "hint": bool(HINT.search(subj)) or bool(link) or bool(code), "body": body[:6000]}


def _recent(since_ts: float, n: int = 25, folders=("INBOX",)):
    """Yield parsed messages (newest first) that arrived shortly before since_ts or later."""
    user, pw = os.environ["IMAP_USER"], os.environ["IMAP_PASS"]
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    global _DISABLED
    imap = imaplib.IMAP4_SSL(host, timeout=30)
    try:
        try:
            imap.login(user, pw)
        except imaplib.IMAP4.error as e:
            _DISABLED = str(e)[:120]
            raise
        for folder in folders:
            try:
                typ, _ = imap.select(f'"{folder}"' if " " in folder or "[" in folder else folder, readonly=True)
                if typ != "OK":
                    continue
                _, data = imap.search(None, "ALL")
                ids = data[0].split()[-n:]
            except Exception:
                continue
            for i in reversed(ids):
                try:
                    _, d = imap.fetch(i, "(RFC822)")
                    info = parse_message(d[0][1])
                except Exception:
                    continue
                if info["ts"] < since_ts - 120:
                    continue
                info["folder"] = folder
                yield info
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def wait_for_verification(since_ts: float, host_hint: str = "", timeout: int = 150, log=print) -> dict | None:
    """Poll the inbox until a fresh verification email arrives. Returns {'link':..., 'code':...} or None."""
    deadline = time.time() + timeout
    hint = (host_hint or "").lower()
    while time.time() < deadline:
        try:
            for info in _recent(since_ts, 12):
                if not info["hint"]:
                    continue
                if hint and hint not in (info["from"] + info["subject"]).lower() and hint not in (info["link"] or "").lower():
                    # sender/link do not mention the site: still accept a clear verification message
                    if not re.search(r"verif|confirm|activate", info["subject"], re.I):
                        continue
                log(f"      mail: found '{info['subject'][:60]}'")
                return {"link": info["link"], "code": info["code"]}
        except Exception as e:
            log(f"      mail: {str(e)[:80]}")
        time.sleep(6)
    return None


def find_confirmation(company_hint: str, since_ts: float, timeout: int = 90, log=print) -> str | None:
    """Subject of an 'we received your application' email from this employer that arrived after since_ts, else None."""
    words = [w for w in re.split(r"[^a-z0-9]+", (company_hint or "").lower()) if len(w) > 2]
    key = re.sub(r"[^a-z0-9]", "", (company_hint or "").lower())
    deadline = time.time() + timeout
    while True:
        try:
            for info in _recent(since_ts, 20, ("INBOX",)):
                if info["ts"] < since_ts - 30:
                    continue
                subj, text = info["subject"], info["subject"] + " " + info["from"] + " " + info["body"][:1500]
                if NOT_CONFIRM.search(subj) or not (CONFIRM_SUBJECT.search(subj) or CONFIRM_SUBJECT.search(info["body"][:600])):
                    continue
                flat = re.sub(r"[^a-z0-9]", "", text.lower())
                if (key and key in flat) or (words and all(w in text.lower() for w in words[:2])):
                    return subj
        except Exception as e:
            log(f"      mail: {str(e)[:80]}")
        if time.time() >= deadline:
            return None
        time.sleep(10)


def scan_confirmations(companies: dict, since_ts: float, n: int = 60) -> dict:
    """{key: subject} for each company (key -> display name) that has sent an 'application received' email since since_ts."""
    found = {}
    try:
        msgs = [m for m in _recent(since_ts, n, ("INBOX",)) if not NOT_CONFIRM.search(m["subject"])
                and (CONFIRM_SUBJECT.search(m["subject"]) or CONFIRM_SUBJECT.search(m["body"][:600]))]
    except Exception:
        return found
    for key, name in companies.items():
        flat_key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
        words = [w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(w) > 2]
        for m in msgs:
            text = (m["subject"] + " " + m["from"] + " " + m["body"][:1500]).lower()
            flat = re.sub(r"[^a-z0-9]", "", text)
            if (flat_key and flat_key in flat) or (words and all(w in text for w in words[:2])):
                found[key] = m["subject"]
                break
    return found
