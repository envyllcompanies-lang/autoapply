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
                        r"(sent|sending) (you )?(an )?e-?mail|activate your account|verification (code|link|e-?mail)|"
                        r"(sent|emailed) (you )?(a|an|the) (\d[- ]digit )?(one.?time )?(code|passcode)|enter the (\d[- ]digit |one.?time )?code", re.I)
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


def long_password(pw: str, n: int = 14) -> str:
    """The same password made longer the same way every time, for sites that insist on 12+ characters."""
    out = strong_password(pw)
    while out and len(out) < n:
        out += "Zq7!"
    return out


class Accounts:
    def __init__(self, cfg: dict, base):
        a = cfg.get("accounts") or {}
        self.email = a.get("email") or (cfg.get("facts") or {}).get("account_email") or (cfg.get("facts") or {}).get("email", "")
        self._base_pw = os.environ.get("ACCOUNT_PASSWORD") or a.get("password") or ""
        self.password = strong_password(self._base_pw)
        self.enabled = bool(a.get("enabled", True)) and bool(self.password) and bool(self.email)
        self.create_in_dry_run = bool(a.get("create_in_dry_run", False))
        self.path = base / "accounts.json"
        try:
            self.known = json.loads(self.path.read_text())
        except Exception:
            self.known = {}

    @staticmethod
    def host(url: str) -> str:
        u = urlparse(url)
        return ((u.hostname or "") + (f":{u.port}" if u.port and u.port not in (80, 443) else "")).lower()

    def use(self, host: str, long: bool | None = None):
        """Set the password for this site: the usual one, or the longer form on a site that demanded 12+ characters
        (which form a site got is remembered in accounts.json; the password itself never is)."""
        rec = self.known.get(host) if isinstance(self.known.get(host), dict) else {}
        if long is None:
            long = bool(rec.get("long_pw")) or bool(getattr(self, "_long", {}).get(host))
        if long:
            self.__dict__.setdefault("_long", {})[host] = True
        base = getattr(self, "_base_pw", "") or self.password
        self.password = long_password(base) if long else strong_password(base)

    def remember(self, host: str, state: str = "created"):
        self.known[host] = {"email": self.email, "state": state, "at": time.strftime("%Y-%m-%d"),
                            **({"long_pw": True} if getattr(self, "_long", {}).get(host) else {})}
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


CODE_INPUT = ("input[autocomplete=one-time-code], input[name*=code i], input[id*=code i], input[name*=otp i], input[id*=otp i], "
              "input[name*=pin i], input[id*=pin i], input[name*=token i], input[aria-label*=code i], input[placeholder*=code i], "
              "input[aria-label*=digit i], input[inputmode=numeric][maxlength='1'], input[maxlength='1']")


def _code_inputs(page) -> list:
    """The visible box(es) a site wants a one-time code typed into: one box, or a row of one-character boxes."""
    try:
        boxes = [b for b in page.locator(CODE_INPUT).locator("visible=true").all()
                 if (b.get_attribute("type") or "text").lower() not in ("password", "email", "hidden", "checkbox", "radio")]
    except Exception:
        return []
    singles = [b for b in boxes if (b.get_attribute("maxlength") or "") == "1"]
    # group one-character boxes by the closest container that holds two or more of them, and use the biggest group
    # (a digit row), so a lone 'middle initial' box elsewhere on the page is never typed into
    _grp = "e => { let p = e.parentElement; while (p && p.querySelectorAll('input[maxlength=\"1\"]').length < 2) p = p.parentElement;" \
           " if (!p) return ''; const path = []; for (let q = p; q; q = q.parentElement) path.push(q.tagName + [...(q.parentElement ? q.parentElement.children : [])].indexOf(q)); return path.join('/'); }"
    groups: dict = {}
    try:
        for b in singles:
            groups.setdefault(b.evaluate(_grp), []).append(b)
        singles = max(groups.values(), key=len) if groups else []
    except Exception:
        singles = []
    return singles if len(singles) >= 4 else [b for b in boxes if b not in singles][:1]


def _type_code(page, boxes: list, code: str):
    """Type the code (one character per box when the site splits it up), then press the verify/continue button."""
    if len(boxes) >= 4:
        boxes[0].click()
        for ch, b in zip(code, boxes):
            b.fill(ch)
        if len(code) > len(boxes):
            boxes[-1].type(code[len(boxes):])
    else:
        boxes[0].fill(code)
    page.wait_for_timeout(600)
    if not _click_named(page, re.compile(r"^\s*(verify|confirm|submit|continue|next|activate|sign in|log ?in)( (code|e-?mail|account))?\s*$", re.I),
                        roles=("button",)):
        if not _submit_auth(page, "signin"):
            boxes[-1].press("Enter")


def _verify_email(page, acc: Accounts, since: float, log, timeout: int = 150):
    if not mailbox.configured():
        why = mailbox.disabled_reason() or "IMAP_USER / IMAP_PASS not set"
        raise AuthBlocked(f"account needs email verification but the inbox can't be read ({why}): fix the app password and it will work")
    log("      account: waiting for the verification email…")
    site = acc.host(page.url)
    res = mailbox.wait_for_verification(since_ts=since, host_hint=site.split(".")[0], timeout=timeout, log=log, kind="verify", site_host=site)
    if not res:
        raise AuthBlocked("verification email did not arrive in time")
    code_box = _code_inputs(page)
    if res.get("code") and code_box:
        # the page is waiting for a code and the email has one: type it in, even when the email also has a link
        log("      account: typing the emailed code")
        _type_code(page, code_box, res["code"])
        _settle(page)
    elif res.get("link") and re.search(r"myworkdayjobs\.com/.*/(activate|verify)|redirect=", res["link"], re.I):
        # Workday's verify link signs you in and goes straight to the application ('...?redirect=.../apply/applyManually'):
        # follow it in this tab so the bot lands on the form
        log("      account: opening the verify link (it leads straight to the application)")
        page.goto(res["link"], wait_until="domcontentloaded", timeout=45000)
        _settle(page, 3000)
    elif res.get("link"):
        log("      account: opening the verification link")
        p2 = page.context.new_page()
        try:
            p2.goto(res["link"], wait_until="domcontentloaded", timeout=45000)
            _settle(p2, 2000)
            # some links land on 'click to confirm' instead of confirming on open
            if _click_named(p2, re.compile(r"^\s*(verify|confirm|activate)( (my )?(e-?mail|account))?\s*$", re.I)):
                _settle(p2, 1500)
        finally:
            p2.close()
        # the original tab may still say 'check your email': reload so it sees the verified account
        try:
            if VERIFY_MSG.search(page.inner_text("body")):
                page.reload(wait_until="domcontentloaded", timeout=45000)
                _settle(page, 1500)
        except Exception:
            pass
    elif res.get("code"):
        box = code_box or page.locator("input[type=text], input[type=tel], input[type=number]").locator("visible=true").all()
        if not box:
            raise AuthBlocked("got a verification code but found no place to type it")
        log("      account: typing the emailed code")
        _type_code(page, box, res["code"])
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
    if is_auth_page(page) and (BAD_LOGIN.search(body) or _form_errors(page)):
        # an account with this email exists with another password: reset it through the inbox, then sign in again
        if _reset_password(page, acc, log, url_after):
            if is_auth_page(page):
                if page.locator("input[type=password]").locator("visible=true").count() >= 2:
                    _click_named(page, re.compile(r"sign in|log ?in|already have", re.I))
                    _settle(page, 1500)
                return _sign_in(page, acc, log, url_after)
            return "signed_in"
        raise AuthBlocked("sign-in was refused and the password could not be reset through your email")
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


RESETS: dict = {}          # host -> password resets done this run (any site)
FORGOT_RX = re.compile(r"forgot(ten)? (your |my )?password|reset (your |my )?password|can.?t (sign|log) ?in|trouble (signing|logging) in|"
                       r"need help (signing|logging) in|password help", re.I)
SEND_RX = re.compile(r"^\s*(send|submit|reset( password)?|continue|next|request( reset)?( link)?|e-?mail me|send (reset )?(link|e-?mail|instructions))\b.{0,25}$", re.I)
SAVE_RX = re.compile(r"^\s*(reset( password)?|change password|set password|save|submit|update( password)?|continue|confirm)\b.{0,20}$", re.I)


def _reset_password(page, acc: Accounts, log, url_after: str | None) -> bool:
    """Any site: 'Forgot password?' -> your email -> the reset email's link (or code) -> new password = ACCOUNT_PASSWORD
    -> back to the application. True when it worked."""
    host = acc.host(page.url)
    if RESETS.get(host, 0) >= 2:
        return False
    RESETS[host] = RESETS.get(host, 0) + 1
    if "myworkdayjobs.com" in page.url:
        return _wd_reset_password(page, acc, log, url_after)
    if not mailbox.configured():
        return False
    t0 = time.time()
    log(f"      account: sign-in refused on {host}; resetting the password through your email")
    try:
        if not _click_named(page, FORGOT_RX, roles=("link", "button")):
            log("      account: no 'Forgot password?' link on this site")
            return False
        _settle(page, 2000)
        box = page.locator("input[type=email], input[name*=mail i], input[id*=mail i], input[name*=user i], input[type=text]").locator("visible=true")
        if not box.count():
            log("      account: the reset page has no email box")
            return False
        box.first.fill(acc.email)
        if not _click_named(page, SEND_RX):
            box.first.press("Enter")
        _settle(page, 2000)
        res = mailbox.wait_for_verification(since_ts=t0 - 30, host_hint=host.split(".")[0], timeout=120, log=log, kind="reset", site_host=host)
        if not res:
            log("      account: the password-reset email did not arrive in time")
            return False
        if res.get("link"):
            page.goto(res["link"], wait_until="domcontentloaded", timeout=45000)
            _settle(page, 2500)
        elif res.get("code"):
            cb = page.locator("input[autocomplete=one-time-code], input[name*=code i], input[id*=code i], input[name*=token i]").locator("visible=true")
            if not cb.count():
                cb = page.locator("input[type=text], input[type=tel], input[type=number]").locator("visible=true")
            if not cb.count():
                log("      account: got a reset code but found no box for it")
                return False
            cb.first.fill(res["code"])
        pws = page.locator("input[type=password]").locator("visible=true")
        if not pws.count():
            if res.get("code"):                      # code first, then the new-password page
                _click_named(page, SEND_RX) or _click_named(page, SAVE_RX)
                _settle(page, 2000)
                pws = page.locator("input[type=password]").locator("visible=true")
            if not pws.count():
                log("      account: the reset did not show a new-password form")
                return False
        for i in range(pws.count()):
            pws.nth(i).fill(acc.password)
        if not _click_named(page, SAVE_RX):
            pws.last.press("Enter")
        _settle(page, 2500)
        if _form_errors(page) and page.locator("input[type=password]").locator("visible=true").count():
            log(f"      account: the new password was not accepted: {'; '.join(_form_errors(page))[:120]}")
            return False
        acc.remember(host, "verified")
        log("      account: password reset; signing in")
        if url_after:
            page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
            _settle(page, 2500)
        return True
    except Exception as e:
        log(f"      account: password reset failed ({str(e)[:80]})")
        return False


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


def _wd_errors(page) -> list[str]:
    """What Workday is complaining about on an account page (its red messages), in its own words."""
    out = list(_form_errors(page))
    try:
        alert = page.locator('[data-automation-id="errorMessage"], [role="alert"], [data-automation-id="inputAlert"], '
                             '[data-automation-id="errorBanner"]').locator("visible=true")
        out += [" ".join(t.split())[:140] for t in alert.all_inner_texts() if t.strip()][:4]
    except Exception:
        pass
    return list(dict.fromkeys(out))


def _wd_wait_answer(page, body_before: str, n_pw: int, max_ms: int = 6000) -> str:
    """After Create Account / Sign In / Reset was pressed, Workday answers a moment later: the boxes go away, another form
    is drawn, or a message appears. Wait for that instead of reading the page while the request is still on its way
    (which reads as 'Workday said nothing'). Returns the page text."""
    waited = 0
    while waited <= max_ms:
        try:
            n = page.locator("input[type=password]").locator("visible=true").count()
            body = _body(page)
        except Exception:
            n, body = n_pw, body_before                 # in the middle of loading the next page
        if n != n_pw or " ".join(body.split()) != " ".join((body_before or "").split()):
            page.wait_for_timeout(600)                  # let it finish drawing
            return _body(page)
        page.wait_for_timeout(500)
        waited += 500
    return _body(page)


def _wd_scope(page):
    dlg = page.locator('[role="dialog"]').filter(has=page.locator("input[type=password]")).locator("visible=true")
    return dlg.last if dlg.count() else page


def _wd_reset_password(page, acc: Accounts, log, url_after: str | None) -> bool:
    """Workday 'Forgot your password?': ask for the reset email, open its link from your inbox, set the password to
    ACCOUNT_PASSWORD, and come back to the application. True when it worked."""
    host = acc.host(page.url)
    t0 = time.time()
    log(f"      account: sign-in refused on {host}; resetting the password through your email")
    if not mailbox.configured():
        log("      account: the inbox cannot be read, so a reset email could not be used")
        return False
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
            log("      account: Workday's 'Forgot password' page has no email box")
            return False
        box.first.fill(acc.email)
        if not _wd_press(page, page, "resetPasswordSubmitButton", re.compile(r"^\s*(reset password|submit|send|continue|reset)\s*$", re.I)):
            log("      account: no button to send the reset email was found")
            return False
        _settle(page, 2000)
        res = mailbox.wait_for_verification(since_ts=t0 - 30, host_hint=host.split(".")[0], timeout=120, log=log, kind="reset", site_host=host)
        if not res or not res.get("link"):
            said = "; ".join(_wd_errors(page))[:140]
            log("      account: the password-reset email did not arrive in time" + (f" (Workday says: {said})" if said else ""))
            return False
        page.goto(res["link"], wait_until="domcontentloaded", timeout=45000)
        _settle(page, 2500)
        pws = page.locator("input[type=password]").locator("visible=true")
        if pws.count() < 1:
            said = "; ".join(_wd_errors(page))[:140]
            log("      account: the reset link did not show a new-password form" + (f" (Workday says: {said})" if said else ""))
            return False
        n_new = pws.count()
        for i in range(n_new):
            pws.nth(i).fill(acc.password)
        before = _body(page)
        if not _wd_press(page, page, "resetPasswordSubmitButton", re.compile(r"^\s*(reset password|change password|save|submit|update|continue)\s*$", re.I)):
            pws.last.press("Enter")
        _settle(page, 2500)
        _wd_wait_answer(page, before, n_new)
        # Did Workday take the new password? If the new-password boxes are still there with a complaint, it did not
        # ('must not match a previous password', 'link expired'): say what it said instead of signing in with a wrong password.
        errs = _wd_errors(page)
        still = page.locator("input[type=password]").locator("visible=true").count()
        if errs and still >= max(2, n_new):
            log(f"      account: Workday did not accept the new password: {'; '.join(errs)[:180]}")
            return False
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
    acc.use(host)
    t0 = time.time()
    tried_create = False
    for _ in range(9):
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
            before = _body(page)
            acc.__dict__.setdefault("_made_at", {})[host] = time.time()       # its verification email can only come after this
            if not _wd_press(page, scope, "createAccountSubmitButton", re.compile(r"^\s*create account\s*$", re.I)):
                raise AuthBlocked("could not find Workday's Create Account button")
            _settle(page, 2500)
            body = _wd_wait_answer(page, before, n_pw)
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
            errs = _wd_errors(page)
            log(f"      account: Workday did not create it; page says: {'; '.join(errs)[:220] or 'nothing'}")
            need = re.search(r"minimum of (\d+) characters", " ".join(errs), re.I)
            if need and int(need.group(1)) > len(acc.password) and int(need.group(1)) <= 14:
                log(f"      account: this site wants {need.group(1)}+ characters: using the longer form of your password here")
                acc.use(host, long=True)
                tried_create = False
                continue
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
        before = _body(page)
        n_before = page.locator("input[type=password]").locator("visible=true").count()
        if not _wd_press(page, scope, "signInSubmitButton", re.compile(r"^\s*sign in\s*$", re.I)):
            raise AuthBlocked("could not find Workday's Sign In button")
        _settle(page, 2500)
        body = _wd_wait_answer(page, before, n_before)
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
        # Several employers refuse a sign-in without saying anything at all: the page simply stays on Sign In. That is a refusal
        # too (wrong password, or an account not verified yet), so the same recovery runs: the verify link, then a reset.
        if state in ("created", "exists") and not getattr(acc, "_wd_verify_tried", {}).get(host):
            # a new Workday account often can't sign in until its 'verify your email' link is opened: open it, then try again
            acc.__dict__.setdefault("_wd_verify_tried", {})[host] = True
            log("      account: sign-in refused; checking the inbox for this site's verify-your-email link")
            try:
                # (made a moment ago: its email may still be on its way. Found already there: only an email that is
                #  already in the inbox can help, so one look is enough)
                made = getattr(acc, "_made_at", {}).get(host)
                if made and time.time() - made < 600:      # made a moment ago in this run: only an email from after that
                    _verify_email(page, acc, made, log, 60)
                else:
                    _verify_email(page, acc, t0 - 2 * 86400, log, 60 if state == "created" else 10)
                acc.remember(host, "verified")
                if url_after and (is_auth_page(page) or "/apply" not in page.url):
                    page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
                    _settle(page, 2500)
                continue
            except AuthBlocked as e:
                log(f"      account: {e}")
        if not getattr(acc, "_wd_reset_tried", {}).get(host):
            # the account exists with some other password: reset it to ACCOUNT_PASSWORD through your own inbox, then sign in
            acc.__dict__.setdefault("_wd_reset_tried", {})[host] = True
            if _wd_reset_password(page, acc, log, url_after):
                acc.remember(host, "reset")
                continue
            # No reset email comes when no account exists for this email on this site (an old record in accounts.json may be
            # wrong): forget the record and make the account instead.
            if not getattr(acc, "_wd_create_fallback", {}).get(host):
                acc.__dict__.setdefault("_wd_create_fallback", {})[host] = True
                log(f"      account: no reset email came, so this email probably has no account on {host}: creating one")
                acc.known.pop(host, None)
                tried_create = False
                if url_after:                              # the reset attempt left the page on 'Forgot password': go back to the application
                    page.goto(url_after, wait_until="domcontentloaded", timeout=45000)
                    _settle(page, 2500)
                    try:                                   # wait for the sign-in / sign-up form to be drawn before looking at it
                        page.wait_for_selector("input[type=password]", state="visible", timeout=20000)
                    except Exception:
                        pass
                continue
        if True:
            said = "; ".join(_wd_errors(page))[:140]
            raise AuthBlocked("Workday refused the sign-in and the password could not be reset through your email"
                              + (f" (Workday says: {said})" if said else "")
                              + ": reset that site's password to your ACCOUNT_PASSWORD once and it will work from then on")
    if is_auth_page(page):
        raise AuthBlocked("still on Workday's sign-in screen after trying to sign in or create an account")


def handle(page, acc: Accounts, log=print, url_after: str | None = None):
    """Get past a login / create-account screen. Raises AuthBlocked when it cannot."""
    if "myworkdayjobs.com" in page.url or page.locator('[data-automation-id="createAccountSubmitButton"], [data-automation-id="signInSubmitButton"]').count():
        return _workday(page, acc, log, url_after)
    host = acc.host(page.url)
    acc.use(host)
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
