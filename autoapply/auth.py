"""Account gate for application sites that need a login (Workday, iCIMS, Taleo, ...).

Creates an account with facts.account_email + the ACCOUNT_PASSWORD secret, confirms it from the inbox, signs in.
Sites already seen are remembered in accounts.json (no passwords stored there) so the next application just signs in.
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlparse

from . import mailbox


class AuthBlocked(Exception):
    pass


VERIFY_MSG = re.compile(r"verify your (e-?mail|account)|check your (e-?mail|inbox)|confirmation (e-?mail|link)|"
                        r"(sent|sending) (you )?(an )?e-?mail|activate your account|verification (code|link|e-?mail)", re.I)
EXISTS_MSG = re.compile(r"already (exists|registered|in use|have an account)|account with this e-?mail|is taken", re.I)
BAD_LOGIN = re.compile(r"incorrect|invalid|not recogni[sz]ed|failed|wrong|unable to (sign|log)|no account", re.I)


class Accounts:
    def __init__(self, cfg: dict, base):
        a = cfg.get("accounts") or {}
        self.email = a.get("email") or (cfg.get("facts") or {}).get("account_email") or (cfg.get("facts") or {}).get("email", "")
        self.password = os.environ.get("ACCOUNT_PASSWORD") or a.get("password") or ""
        self.enabled = bool(a.get("enabled", True)) and bool(self.password) and bool(self.email)
        self.create_in_dry_run = bool(a.get("create_in_dry_run", False))
        self.path = base / "accounts.json"
        try:
            self.known = json.loads(self.path.read_text())
        except Exception:
            self.known = {}

    @staticmethod
    def host(url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def remember(self, host: str, state: str = "created"):
        self.known[host] = {"email": self.email, "state": state, "at": time.strftime("%Y-%m-%d")}
        self.path.write_text(json.dumps(self.known, indent=1, sort_keys=True))


def is_auth_page(page) -> bool:
    try:
        return page.locator("input[type=password]").locator("visible=true").count() > 0
    except Exception:
        return False


def _click_named(page, rx, roles=("button", "link"), scope=None):
    scope = scope or page
    for role in roles:
        loc = scope.get_by_role(role, name=rx).locator("visible=true")
        if loc.count():
            loc.first.click(force=True)
            return True
    return False


def _fill_credentials(page, acc: Accounts, scope=None):
    scope = scope or page
    for el in scope.locator("input[type=email], input[type=text], input:not([type])").locator("visible=true").all():
        hint = " ".join(filter(None, [el.get_attribute("type"), el.get_attribute("name"), el.get_attribute("id"),
                                      el.get_attribute("placeholder"), el.get_attribute("aria-label"),
                                      el.get_attribute("autocomplete"), el.get_attribute("data-automation-id")])).lower()
        if re.search(r"e-?mail|user ?name|login|username", hint) or el.get_attribute("type") == "email":
            el.fill(acc.email)
            break
    for el in scope.locator("input[type=password]").locator("visible=true").all():
        el.fill(acc.password)


def _tick_agreements(page, scope=None):
    scope = scope or page
    for cb in scope.locator("input[type=checkbox]").locator("visible=true").all():
        try:
            if not cb.is_checked():
                cb.check(force=True)
        except Exception:
            pass


def _submit_auth(page, kind: str):
    rx = re.compile(r"^\s*(create account|register|sign up|join|continue|submit|next)\s*$" if kind == "create"
                    else r"^\s*(sign in|log ?in|continue|submit|next)\s*$", re.I)
    sel = ('[data-automation-id="createAccountSubmitButton"], [data-automation-id="click_filter"][aria-label*="Create"]'
           if kind == "create" else '[data-automation-id="signInSubmitButton"], [data-automation-id="click_filter"][aria-label*="Sign"]')
    loc = page.locator(sel).locator("visible=true")
    if loc.count():
        loc.first.click(force=True)
        return True
    if _click_named(page, rx, roles=("button",)):
        return True
    btn = page.locator("input[type=submit], button[type=submit]").locator("visible=true")
    if btn.count():
        btn.first.click(force=True)
        return True
    return False


def _settle(page, ms=2500):
    page.wait_for_timeout(ms)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass


def _verify_email(page, acc: Accounts, since: float, log):
    if not mailbox.configured():
        raise AuthBlocked("account needs email verification but no mailbox is configured (IMAP_USER / IMAP_PASS)")
    log("      account: waiting for the verification email…")
    res = mailbox.wait_for_verification(since_ts=since, host_hint=acc.host(page.url).split(".")[0], log=log)
    if not res:
        raise AuthBlocked("verification email did not arrive in time")
    if res.get("link"):
        p2 = page.context.new_page()
        try:
            p2.goto(res["link"], wait_until="domcontentloaded", timeout=45000)
            _settle(p2, 2000)
        finally:
            p2.close()
    elif res.get("code"):
        box = page.locator("input[autocomplete=one-time-code], input[name*=code i], input[id*=code i], input[type=text], input[type=tel]").locator("visible=true")
        if not box.count():
            raise AuthBlocked("got a verification code but found no place to type it")
        box.first.fill(res["code"])
        _submit_auth(page, "signin")
        _settle(page)
    else:
        raise AuthBlocked("verification email had no link or code")


def _sign_in(page, acc: Accounts, log):
    _fill_credentials(page, acc)
    if not _submit_auth(page, "signin"):
        raise AuthBlocked("could not find the sign-in button")
    _settle(page)
    if is_auth_page(page):
        body = page.inner_text("body")
        if BAD_LOGIN.search(body):
            raise AuthBlocked("sign-in was refused (account exists with a different password?)")


def _create(page, acc: Accounts, log, start_url=None):
    t0 = time.time()
    _fill_credentials(page, acc)
    _tick_agreements(page)
    if not _submit_auth(page, "create"):
        raise AuthBlocked("could not find the create-account button")
    _settle(page)
    acc.remember(acc.host(page.url), "created")
    body = page.inner_text("body")
    if EXISTS_MSG.search(body):
        log("      account: already exists, signing in instead")
        return "exists"
    if VERIFY_MSG.search(body):
        _verify_email(page, acc, t0, log)
        if start_url:
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)   # come back and sign in
            _settle(page, 2000)
        return "verified"
    return "created"


def handle(page, acc: Accounts, log=print, url_after: str | None = None):
    """Get past a login / create-account screen. Raises AuthBlocked when it cannot."""
    host = acc.host(page.url)
    for _ in range(4):
        if not is_auth_page(page):
            return
        n_pw = page.locator("input[type=password]").locator("visible=true").count()
        known = host in acc.known
        if n_pw >= 2:                                   # a create-account form
            if known:
                if not _click_named(page, re.compile(r"sign in|log ?in|already have", re.I)):
                    raise AuthBlocked("account exists but no sign-in link found")
                _settle(page, 1500)
                continue
            log(f"      account: creating one on {host} with {acc.email}")
            out = _create(page, acc, log, url_after)
            _settle(page, 1500)
            if is_auth_page(page):                       # verified or exists: now sign in
                if page.locator("input[type=password]").locator("visible=true").count() >= 2:
                    _click_named(page, re.compile(r"sign in|log ?in|already have", re.I))
                    _settle(page, 1500)
                if is_auth_page(page) and page.locator("input[type=password]").locator("visible=true").count() == 1:
                    _sign_in(page, acc, log)
            continue
        # a sign-in form (one password box)
        if known:
            log(f"      account: signing in on {host}")
            _sign_in(page, acc, log)
            continue
        if _click_named(page, re.compile(r"create( an)? account|register|sign up|new (user|candidate)", re.I)):
            _settle(page, 1500)
            continue
        log(f"      account: signing in on {host} (first try)")
        _sign_in(page, acc, log)
        acc.remember(host, "signed_in")
    if is_auth_page(page):
        raise AuthBlocked("still on the login screen after trying to sign in or create an account")
