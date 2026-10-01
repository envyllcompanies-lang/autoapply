"""CAPTCHA provider boundary.

The adapter records challenge metadata and exposes a stable provider interface, but it does
not request, poll for, or inject CAPTCHA-solving tokens. Human verification remains a hard
boundary for unattended applications.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


class CaptchaDisabled(RuntimeError):
    """Raised when an application reaches a CAPTCHA that this runner cannot complete."""


@dataclass(frozen=True)
class CaptchaChallenge:
    kind: str
    site_key: str | None
    page_url: str


@dataclass(frozen=True)
class CaptchaResult:
    status: str
    provider: str
    challenge: CaptchaChallenge
    reason: str


class CaptchaProvider:
    name = "disabled"

    def solve(self, challenge: CaptchaChallenge) -> CaptchaResult:
        raise CaptchaDisabled("CAPTCHA solving is disabled; manual verification is required")


class TwoCaptchaProvider(CaptchaProvider):
    """Configuration-compatible 2Captcha adapter with solving deliberately unavailable."""

    name = "twocaptcha"

    def __init__(self, api_key_env: str = "TWOCAPTCHA_API_KEY"):
        self.api_key_env = api_key_env

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self.api_key_env))

    def solve(self, challenge: CaptchaChallenge) -> CaptchaResult:
        # Deliberately no 2Captcha API request, polling, token retrieval, or token injection.
        raise CaptchaDisabled(
            "2Captcha provider is configured for metadata only; CAPTCHA solving is unavailable"
        )


def provider_from_settings(settings: Mapping[str, object] | None) -> CaptchaProvider:
    cfg = dict((settings or {}).get("captcha") or {})
    provider = str(cfg.get("provider") or "disabled").lower()
    if provider == "twocaptcha":
        return TwoCaptchaProvider(str(cfg.get("api_key_env") or "TWOCAPTCHA_API_KEY"))
    return CaptchaProvider()


def challenge_from_page(page, page_url: str | None = None) -> CaptchaChallenge | None:
    """Extract non-secret challenge metadata from the current DOM."""
    url = page_url or getattr(page, "url", "") or ""
    try:
        for selector, kind in (
            ("iframe[src*='recaptcha/api2/']", "recaptcha_v2"),
            ("iframe[src*='hcaptcha.com']", "hcaptcha"),
            ("iframe[src*='challenges.cloudflare.com']", "turnstile"),
        ):
            frame = page.locator(selector).first
            if not frame.count():
                continue
            src = frame.get_attribute("src") or ""
            site_key = None
            for attr in ("data-sitekey", "data-site-key"):
                node = page.locator(f"[{attr}]").first
                if node.count():
                    site_key = node.get_attribute(attr)
                    if site_key:
                        break
            return CaptchaChallenge(kind=kind, site_key=site_key, page_url=url)
    except Exception:
        return None
    return None
