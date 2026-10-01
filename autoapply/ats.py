"""Application-system intelligence.

The browser engine is deliberately generic, but navigation is more reliable when we can
identify a known ATS early. This module contains *classification*, not challenge bypasses:
CAPTCHA/Turnstile/human verification remains a hard boundary.

The classifier is intentionally URL-first and cheap. DOM evidence can be supplied by callers
when the URL is a custom employer domain.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
import re


@dataclass(frozen=True)
class ATS:
    name: str
    confidence: float
    kind: str
    priority: int


_PATTERNS = (
    ("workday", re.compile(r"(?:myworkdayjobs\.com|workday\.com)", re.I), 1.0, 0),
    ("greenhouse", re.compile(r"(?:greenhouse\.io|job-boards\.greenhouse\.io)", re.I), 1.0, 2),
    ("lever", re.compile(r"(?:jobs\.lever\.co|jobs\.lever\.co|lever\.co)", re.I), 1.0, 1),
    ("workable", re.compile(r"(?:workable\.com|apply\.workable\.com)", re.I), 1.0, 2),
    ("bamboohr", re.compile(r"(?:bamboohr\.com)", re.I), 0.95, 1),
    ("breezy", re.compile(r"(?:breezy\.hr)", re.I), 0.95, 1),
    ("recruitee", re.compile(r"(?:recruitee\.com)", re.I), 0.95, 1),
    ("ashby", re.compile(r"(?:ashbyhq\.com)", re.I), 0.95, 1),
    ("icims", re.compile(r"(?:icims\.com)", re.I), 0.9, 1),
    ("taleo", re.compile(r"(?:taleo\.net|oraclecloud\.com)", re.I), 0.85, 2),
    ("smartrecruiters", re.compile(r"(?:smartrecruiters\.com)", re.I), 0.95, 1),
    ("jobvite", re.compile(r"(?:jobvite\.com)", re.I), 0.9, 1),
    ("successfactors", re.compile(r"(?:successfactors\.com)", re.I), 0.9, 2),
    ("phenom", re.compile(r"(?:phenompeople\.com)", re.I), 0.9, 2),
)


def detect(url: str, page_text: str = "") -> ATS:
    """Classify a posting/application host without opening another request."""
    u = url or ""
    for name, rx, confidence, priority in _PATTERNS:
        if rx.search(u):
            return ATS(name, confidence, "known", priority)

    text = (page_text or "").lower()
    # Cheap DOM fingerprints for employer-hosted ATS pages.
    fingerprints = (
        ("workday", ("data-automation-id", "myworkdayjobs", "workday")),
        ("greenhouse", ("greenhouse", "application_questions")),
        ("lever", ("lever", "selectedLocation")),
        ("workable", ("workable", "job_application")),
        ("bamboohr", ("bamboohr",)),
        ("smartrecruiters", ("smartrecruiters",)),
    )
    for name, needles in fingerprints:
        if sum(1 for n in needles if n in text) >= min(2, len(needles)):
            return ATS(name, 0.75, "dom", 1)
    host = (urlparse(u).hostname or "").lower()
    return ATS(host or "web", 0.35, "generic", 1)


def is_human_gate(text: str) -> bool:
    """True only for an explicit anti-bot/human-verification boundary."""
    return bool(re.search(
        r"captcha|hcaptcha|re?captcha|turnstile|cloudflare.*(verify|human|challenge)|"
        r"prove you.?re human|are you a robot|human verification|human check|"
        r"its own human check|security code.*(human|robot|verification)",
        text or "", re.I,
    ))


def priority(url: str, page_text: str = "") -> int:
    return detect(url, page_text).priority
