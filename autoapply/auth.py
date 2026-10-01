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
# (not 'already have an account?': that is the sign-in link every sign-up page shows)
EXISTS_MSG = re.compile(r"already (exists|registered|in use|taken|associated)|(account|user) (with|for) (this|that|the) e-?mail (address )?(already|exists)|"
                        r"e-?mail (address )?(is )?already|is taken", re.I)
BAD_LOGIN = re.compile(r"incorrect|invalid|not recogni[sz]ed|failed|wrong|unable to (sign|log)|no account", re.I)
UNVERIFIED_MSG = re.compile(r"not (yet )?(been )?(verified|activated|confirmed)|verify your (e-?mail|account) (before|to|first)|"
                            r"account (is |has )?not (yet )?(been )?(active|activated|verified|confirmed)|confirm your e-?mail( address)? (before|to|first)|"
                            r"resend (the |a )?(verification|activation|confirmation)", re.I)


def _body(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _form_errors(page) -> list[str]:
    try:
        errs = page.locator('[role="alert"], [aria-live="assertive"], .error, [class*="error" i], [data-automation-id*="error" i]') \
            .locator("visible=true").all_inner_texts()
    except Exception:
        return []
    return [" ".join(e.split())[:100] for e in errs if e.strip()][:3]


def strong_password(pw: str) -> str:
    """A password every job site accepts (Workday: 8+ characters with an uppercase letter, a lowercase letter, a number and a
    special character), made the same way every time from ACCOUNT_PASSWORD so you always know it: your saved password,
    then 'Aa1!', then 'Job' if it is still shorter than 8 characters. A password that already qualifies is used as is."""
    if not pw:
        return pw
    ok = len(pw) >= 8 and re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)
    if ok:
        return pw
    out = pw + "Aa1!"
    if len(out) < 8:
        out += "Job"
    return out


class Accounts:
    def __init__(self, cfg: dict, base):
        a = cfg.get("accounts") or {}
        self.email = a.get("email") or (cfg.get("facts") or {}).get("account_email") or (cfg.get("facts") or {}).get("email", "")
        self.password = strong_password(os.environ.get("ACCOUNT_PASSWORD") or a.get("password") or "")
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


def _verify_email(page, acc: Accounts, since: float, log, timeout: int = 150):
    if not mailbox.configured():
        why = mailbox.disabled_reason() or "IMAP_USER / IMAP_PASS not set"
        raise AuthBlocked(f"account needs email verification but the inbox can't be read ({why}): fix the app password and it will work")
    log("      account: waiting for the verification email…")
    res = mailbox.wait_for_verification(since_ts=since, host_hint=acc.host(page.url).split(".")[0], timeout=timeout, log=log)
    if not res:
        raise AuthBlocked("verification email did not arrive in time")
    if res.get("link") and re.search(r"myworkdayjobs\.com/.*/(activate|verify)|redirect=", res["link"], re.I):
        # Workday's verify link signs you in and goes straight to the application ('...?redirect=.../apply/applyManually'):
        # follow it in this tab so the bot lands on the form
        log("      account: opening the verify link (it leads straight to the application)")
        page.goto(res["link"], wait_until="domcontentloaded", timeout=45000)
        _settle(page, 3000)
    elif res.get("link"):
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


def _sign_in(page, acc: Accounts, log, url_after: str | None = None):
    t0 = time.time()
    _fill_credentials(page, acc)
    if not _submit_auth(page, "signin"):
        raise AuthBlocked("could not find the sign-in button")
    _settle(page)
    body = _body(page)
    if UNVERIFIED_MSG.search(body):
        # made in an earlier run whose verification email was never read: get a fresh email and finish it now
        log("      account: it exists but was never verified; asking the site to send the email again")
        _click_named(page, re.compile(r"resend|send (it |the email )?again|send (a )?new|verify( my)? (email|account)|activate", re.I))
        _settle(page, 1500)
        _verify_email(page, acc, t0 - 30, log)
        acc.remember(acc.host(page.url), "verified")
        if url_after:
            page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
            _settle(page, 2000)
        return "verified"
    if is_auth_page(page) and BAD_LOGIN.search(body):
        raise AuthBlocked("sign-in was refused: an account with this email probably exists with a different password "
                          "(sign in by hand once, or reset that site's password to your ACCOUNT_PASSWORD)")
    return "signed_in"


def _create(page, acc: Accounts, log, start_url=None):
    t0 = time.time()
    host = acc.host(page.url)
    _fill_credentials(page, acc)
    _tick_agreements(page)
    if not _submit_auth(page, "create"):
        raise AuthBlocked("could not find the create-account button")
    _settle(page)
    body = _body(page)
    if EXISTS_MSG.search(body):
        acc.remember(host, "exists")
        log("      account: already exists, signing in instead")
        return "exists"
    if VERIFY_MSG.search(body):
        acc.remember(host, "created")
        _verify_email(page, acc, t0, log)
        acc.remember(host, "verified")
        if start_url:
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)   # come back and sign in
            _settle(page, 2000)
        return "verified"
    if page.locator("input[type=password]").locator("visible=true").count() >= 2:
        errs = _form_errors(page)
        if errs:                                  # still on the create form with complaints: the account was not made
            raise AuthBlocked(f"the site did not accept the new account: {'; '.join(errs)}")
    acc.remember(host, "created")
    return "created"


# ------------------------------------------------------------------------------------------------ Workday
def _wd(scope, aid: str):
    return scope.locator(f'[data-automation-id="{aid}"]').locator("visible=true")


def _wd_press(page, scope, aid: str, label_rx) -> bool:
    """Workday covers its buttons with an invisible click-catcher: click that, else the button itself."""
    for loc in (scope.get_by_role("button", name=label_rx).locator("visible=true"), _wd(scope, aid)):
        try:
            if loc.count():
                loc.last.click(force=True, timeout=5000)
                return True
        except Exception:
            continue
    return False


def _wd_scope(page):
    dlg = page.locator('[role="dialog"]').filter(has=page.locator("input[type=password]")).locator("visible=true")
    return dlg.last if dlg.count() else page


def _wd_reset_password(page, acc: Accounts, log, url_after: str | None) -> bool:
    """Workday 'Forgot your password?': ask for the reset email, open its link from your inbox, set the password to
    ACCOUNT_PASSWORD, and come back to the application. True when it worked."""
    host = acc.host(page.url)
    t0 = time.time()
    log(f"      account: sign-in refused on {host}; resetting the password through your email")
    link = _wd(page, "forgotPasswordLink")
    try:
        if link.count():
            link.first.click(force=True)
        elif not _click_named(page, re.compile(r"forgot (your )?password", re.I), roles=("link", "button")):
            log("      account: no 'Forgot your password?' link")
            return False
        _settle(page, 2000)
        box = page.locator('input[data-automation-id="email"], input[type=email], input[type=text]').locator("visible=true")
        if not box.count():
            return False
        box.first.fill(acc.email)
        if not _wd_press(page, page, "resetPasswordSubmitButton", re.compile(r"^\s*(reset password|submit|send|continue|reset)\s*$", re.I)):
            return False
        _settle(page, 2000)
        if not mailbox.configured():
            return False
        res = mailbox.wait_for_verification(since_ts=t0 - 30, host_hint=host.split(".")[0], timeout=90, log=log)
        if not res or not res.get("link"):
            log("      account: the password-reset email did not arrive in time")
            return False
        page.goto(res["link"], wait_until="domcontentloaded", timeout=45000)
        _settle(page, 2500)
        pws = page.locator("input[type=password]").locator("visible=true")
        if pws.count() < 1:
            log("      account: the reset link did not show a new-password form")
            return False
        for i in range(pws.count()):
            pws.nth(i).fill(acc.password)
        _wd_press(page, page, "resetPasswordSubmitButton", re.compile(r"^\s*(reset password|change password|save|submit|update|continue)\s*$", re.I))
        _settle(page, 2500)
        log("      account: password reset; signing in")
        if url_after:
            page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
            _settle(page, 2500)
        return True
    except Exception as e:
        log(f"      account: password reset failed ({str(e)[:80]})")
        return False


def _workday(page, acc: Accounts, log, url_after: str | None):
    """Workday's own sign-up / sign-in: email + password + verify password + privacy box, or the Sign In pop-up."""
    host = acc.host(page.url)
    t0 = time.time()
    tried_create = False
    for _ in range(5):
        if not is_auth_page(page):
            return
        scope = _wd_scope(page)
        n_pw = scope.locator("input[type=password]").locator("visible=true").count()
        if n_pw >= 2 and host not in acc.known and not tried_create:
            tried_create = True
            log(f"      account: creating one on {host} with {acc.email}")
            for aid, val in (("email", acc.email), ("password", acc.password), ("verifyPassword", acc.password)):
                box = _wd(scope, aid)
                if box.count():
                    box.first.fill(val)
            if not _wd(scope, "email").count():
                _fill_credentials(page, acc, scope)
            ck = _wd(scope, "createAccountCheckbox")
            if ck.count() and not ck.first.is_checked():
                ck.first.check(force=True)
            _tick_agreements(page)
            if not _wd_press(page, scope, "createAccountSubmitButton", re.compile(r"^\s*create account\s*$", re.I)):
                raise AuthBlocked("could not find Workday's Create Account button")
            _settle(page, 2500)
            body = _body(page)
            if not is_auth_page(page):
                acc.remember(host, "created")
                log("      account: created")
                return
            if VERIFY_MSG.search(body) and not EXISTS_MSG.search(body):
                acc.remember(host, "created")
                _verify_email(page, acc, t0, log)
                acc.remember(host, "verified")
                if url_after and (is_auth_page(page) or "/apply" not in page.url):
                    page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
                    _settle(page, 2000)
                continue
            if EXISTS_MSG.search(body):
                acc.remember(host, "exists")
                log("      account: Workday says one already exists for this email: signing in")
                continue
            errs = _form_errors(page)
            alert = page.locator('[data-automation-id="errorMessage"], [role="alert"], [data-automation-id="inputAlert"]').locator("visible=true")
            try:
                errs += [t.strip() for t in alert.all_inner_texts() if t.strip()][:4]
            except Exception:
                pass
            log(f"      account: Workday did not create it; page says: {'; '.join(errs)[:220] or 'nothing'}")
            if errs:
                raise AuthBlocked(f"Workday did not accept the new account: {'; '.join(errs)[:200]}")
            continue
        if n_pw == 1 and host not in acc.known and not tried_create:
            # a Sign In page first (no account here yet): go to Create Account
            link = _wd(page, "createAccountLink")
            if link.count():
                link.first.click(force=True)
                _settle(page, 2000)
                continue
            if _click_named(page, re.compile(r"^\s*create account\s*$", re.I), roles=("button", "link")):
                _settle(page, 2000)
                continue
        # sign in
        if n_pw >= 2:                                   # on the sign-up form: open the Sign In pop-up / page
            link = _wd(page, "signInLink")
            if link.count():
                link.first.click(force=True)
            else:
                _click_named(page, re.compile(r"^\s*sign in\s*$", re.I), roles=("button", "link"), scope=page.locator("form, main").first)
            _settle(page, 2000)
            scope = _wd_scope(page)
        log(f"      account: signing in on {host}")
        em, pw = _wd(scope, "email"), _wd(scope, "password")
        if em.count() and pw.count():
            em.first.fill(acc.email)
            pw.first.fill(acc.password)
        else:
            _fill_credentials(page, acc, scope)
        if not _wd_press(page, scope, "signInSubmitButton", re.compile(r"^\s*sign in\s*$", re.I)):
            raise AuthBlocked("could not find Workday's Sign In button")
        _settle(page, 2500)
        body = _body(page)
        if not is_auth_page(page):
            acc.remember(host, "signed_in")
            return
        if UNVERIFIED_MSG.search(body):
            log("      account: it exists but was never verified; asking Workday to send the email again")
            _click_named(page, re.compile(r"resend|send (it |the email )?again|verify", re.I))
            _settle(page, 1500)
            _verify_email(page, acc, t0 - 30, log)
            acc.remember(host, "verified")
            if url_after and (is_auth_page(page) or "/apply" not in page.url):
                page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
                _settle(page, 2000)
            continue
        state = (acc.known.get(host) or {}).get("state") if isinstance(acc.known.get(host), dict) else acc.known.get(host)
        if BAD_LOGIN.search(body) and state in ("created", "exists") and not getattr(acc, "_wd_verify_tried", {}).get(host):
            # a new Workday account often can't sign in until its 'verify your email' link is opened: open it, then try again
            acc.__dict__.setdefault("_wd_verify_tried", {})[host] = True
            log("      account: sign-in refused; checking the inbox for this site's verify-your-email link")
            try:
                _verify_email(page, acc, t0 - 2 * 86400, log, 60)
                acc.remember(host, "verified")
                if url_after and (is_auth_page(page) or "/apply" not in page.url):
                    page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
                    _settle(page, 2500)
                continue
            except AuthBlocked as e:
                log(f"      account: {e}")
        if BAD_LOGIN.search(body) and not getattr(acc, "_wd_reset_tried", {}).get(host):
            # the account exists with some other password: reset it to ACCOUNT_PASSWORD through your own inbox, then sign in
            acc.__dict__.setdefault("_wd_reset_tried", {})[host] = True
            if _wd_reset_password(page, acc, log, url_after):
                acc.remember(host, "reset")
                continue
        if BAD_LOGIN.search(body):
            raise AuthBlocked("Workday refused the sign-in: an account with this email exists with a different password "
                              "(reset that site's password to your ACCOUNT_PASSWORD once and it will work from then on)")
    if is_auth_page(page):
        raise AuthBlocked("still on Workday's sign-in screen after trying to sign in or create an account")


def handle(page, acc: Accounts, log=print, url_after: str | None = None):
    """Get past a login / create-account screen. Raises AuthBlocked when it cannot."""
    if "myworkdayjobs.com" in page.url or page.locator('[data-automation-id="createAccountSubmitButton"], [data-automation-id="signInSubmitButton"]').count():
        return _workday(page, acc, log, url_after)
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
                    _sign_in(page, acc, log, url_after)
            continue
        # a sign-in form (one password box)
        if known:
            log(f"      account: signing in on {host}")
            _sign_in(page, acc, log, url_after)
            continue
        if _click_named(page, re.compile(r"create( an)? account|register|sign up|new (user|candidate)", re.I)):
            _settle(page, 1500)
            continue
        log(f"      account: signing in on {host} (no 'create account' link found)")
        if _sign_in(page, acc, log, url_after) == "signed_in" and not is_auth_page(page):
            acc.remember(host, "signed_in")
    if is_auth_page(page):
        raise AuthBlocked("still on the login screen after trying to sign in or create an account")
