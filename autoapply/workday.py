"""Workday applications, step by step.

Workday is where this bot's applications actually go through, so it has its own driver instead of the generic
"find a form and guess" loop. Workday names every part of its pages (data-automation-id), and those names are the same
at every employer, so the driver always knows where it is:

    job page  ->  Start Your Application  ->  Create Account / Sign In  ->  My Information  ->  My Experience
              ->  Application Questions  ->  Voluntary Disclosures  ->  Self Identify  ->  Review  ->  submitted

For each step it reads the fields, answers them from your facts, fills them, presses Next and then *looks at what
happened*: the progress bar moved on, or Workday sent the page back with complaints. A page that is sent back is repaired
(only the fields Workday flagged are redone, a required Work Experience / Education block is added) and tried again; if it
still will not go, the application stops with Workday's own words in the log.

Nothing here gets around a human check: a CAPTCHA anywhere in the flow stops the application.
"""
from __future__ import annotations

import re
import time

STATE_JS = r"""() => {
  const q = s => document.querySelector(s);
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(), st = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const txt = el => ((el && el.innerText) || '').replace(/\s+/g, ' ').trim();
  const aid = n => q('[data-automation-id="' + n + '"]');
  const out = { url: location.href };
  out.error = vis(aid('errorContainer')) ? (txt(aid('errorContainer')).slice(0, 200) || 'error page') : '';
  out.already = vis(aid('alreadyApplied')) ? (txt(aid('alreadyApplied')).slice(0, 120) || 'already applied') : '';
  out.start = vis(aid('applyAdventurePage'));
  const man = aid('applyManually');
  out.manual = man ? (man.getAttribute('href') || 'click') : '';
  out.applyButton = vis(aid('adventureButton'));
  const flow = aid('applyFlowPage');
  out.flow = vis(flow);
  const steps = [...document.querySelectorAll('[data-automation-id="progressBar"] > li')];
  const stepName = li => { const l = [...li.querySelectorAll('label')]; return txt(l[l.length - 1] || li); };
  out.steps = steps.map(stepName);
  out.index = steps.findIndex(li => li.getAttribute('data-automation-id') === 'progressBarActiveStep');
  out.name = out.index >= 0 ? out.steps[out.index] : '';
  out.done = steps.filter(li => li.getAttribute('data-automation-id') === 'progressBarCompletedStep').length;
  const pg = flow && flow.querySelector('[data-automation-id^="applyFlow"][data-automation-id$="Page"]');
  out.page = pg ? pg.getAttribute('data-automation-id') : '';
  out.passwords = [...document.querySelectorAll('input[type=password]')].filter(vis).length;
  out.signin = vis(aid('signInContent')) || vis(aid('signInSubmitButton')) || vis(aid('createAccountSubmitButton'));
  out.emailButton = vis(aid('SignInWithEmailButton')) || vis(aid('signInWithEmailButton'));
  const next = aid('pageFooterNextButton') || aid('bottom-navigation-next-button');
  out.next = vis(next) ? txt(next) : '';
  out.heading = txt(flow ? flow.querySelector('h3') : q('main h2, h1')).slice(0, 80);
  out.inputs = [...(flow || document).querySelectorAll('input, textarea, select, button[aria-haspopup="listbox"]')].filter(vis).length;
  return out;
}"""

ERRORS_JS = r"""() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(), st = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const txt = el => ((el && el.innerText) || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('[data-automation-id="applyFlowPage"]') || document;
  const msgs = [], fields = [];
  const add = t => { t = (t || '').slice(0, 170); if (t && !msgs.includes(t)) msgs.push(t); };
  for (const el of root.querySelectorAll('[aria-invalid="true"]')) {
    const box = el.closest('[data-automation-id^="formField-"]') || el.closest('fieldset') || el.parentElement;
    if (!vis(box) && !vis(el)) continue;
    const key = (box && box.getAttribute && box.getAttribute('data-automation-id')) || '';
    const lab = (txt(box && box.querySelector('legend, label')) || el.getAttribute('aria-label') || '').replace(/\*+\s*$/, '').trim().slice(0, 90);
    if (!fields.some(f => f.key === key && f.label === lab)) fields.push({ key, label: lab });
    for (const id of ((el.getAttribute('aria-describedby') || '') + ' ' + (el.getAttribute('aria-errormessage') || '')).split(/\s+/)) {
      const d = id && document.getElementById(id);
      const t = d && vis(d) ? txt(d) : '';
      if (t && /error|required|invalid|must|enter|select|missing|valid/i.test(t)) add(t);
    }
  }
  const SEL = '[data-automation-id="errorBanner"], [data-automation-id="errorHeading"], [data-automation-id="errorMessage"], ' +
              '[data-automation-id="inputAlert"], [data-automation-id*="rror"], [role="alert"]';
  for (const el of root.querySelectorAll(SEL)) {
    if (!vis(el)) continue;
    const t = txt(el);
    if (!t || /successfully uploaded|items? selected|uploading/i.test(t)) continue;
    add(t);
  }
  return { fields, messages: msgs.slice(0, 8) };
}"""

SECTIONS_JS = r"""() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(), st = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const txt = el => ((el && el.innerText) || '').replace(/\s+/g, ' ').trim();
  const out = [];
  let n = 0;
  for (const g of document.querySelectorAll('[data-automation-id="applyFlowPage"] [role="group"][aria-labelledby]')) {
    const add = [...g.querySelectorAll('[data-automation-id="add-button"], [data-automation-id="Add"]')].find(vis);
    if (!add) continue;
    const h = document.getElementById(g.getAttribute('aria-labelledby'));
    const rows = [...g.querySelectorAll('input, textarea, button[aria-haspopup="listbox"]')].filter(vis).length;
    const id = 'sec' + (n++);
    add.setAttribute('data-aa-add', id);
    out.push({ id, name: txt(h).replace(/\*+\s*$/, '').trim(), required: /\*/.test(txt(h)), rows });
  }
  return out;
}"""

NEXT = '[data-automation-id="pageFooterNextButton"], [data-automation-id="bottom-navigation-next-button"]'
CLOSED = re.compile(r"doesn.t exist|does not exist|no longer (available|accepting|open)|has been (filled|closed|removed)|not found|expired", re.I)
MARKS = ('[data-automation-id="applyFlowPage"], [data-automation-id="applyAdventurePage"], [data-automation-id="adventureButton"], '
         '[data-automation-id="jobPostingHeader"], [data-automation-id="alreadyApplied"], [data-automation-id="errorContainer"]')


def is_workday(page) -> bool:
    try:
        if "myworkdayjobs.com" in page.url:
            return True
        return page.locator(MARKS).count() > 0
    except Exception:
        return False


def posting_url(url: str) -> str:
    """The job posting's own address: no '?query' and no '/apply/...' on the end."""
    return re.sub(r"/apply(/.*)?$", "", (url or "").split("?")[0].split("#")[0]).rstrip("/")


def kind(st: dict) -> str:
    """Which Workday page this is: closed | error | already | auth | form | flow | start | job | unknown."""
    if not st:
        return "unknown"
    in_form = bool(st.get("flow") and (st.get("next") or st.get("page")))
    if st.get("error") and not in_form:               # (a complaint box inside a form step is not an error page)
        return "closed" if CLOSED.search(st["error"]) else "error"
    if st.get("flow"):
        if st.get("passwords") or st.get("signin") or st.get("emailButton"):
            return "auth"
        if st.get("next") or st.get("page"):
            return "form"
        return "flow"                       # the application frame is up but its page has not been drawn yet
    if st.get("passwords") or st.get("signin"):
        return "auth"
    if st.get("start"):
        return "start"
    if st.get("already") and re.search(r"appl", st["already"], re.I):       # 'You applied ...', not a note about something else
        return "already"
    if st.get("applyButton"):
        return "job"
    return "unknown"


def read_state(page, max_ms: int = 9000) -> dict:
    """Workday draws its pages with JavaScript after they 'load': wait until two looks in a row see the same page."""
    last, st, waited = None, {}, 0
    while waited <= max_ms:
        try:
            st = page.evaluate(STATE_JS)
        except Exception:
            st = {}                                             # mid-navigation
        k = kind(st)
        sig = (k, st.get("index"), st.get("page"), st.get("inputs"), st.get("passwords"), st.get("next")) if st else None
        # a step whose boxes have not been drawn yet looks settled for a moment: give it time (only Review has none)
        drawn = k not in ("unknown", "flow") and not (k == "form" and not st.get("inputs"))
        if st and sig == last and (drawn or waited >= 4500):
            return st
        last = sig
        page.wait_for_timeout(450)
        waited += 450
    return st


def read_errors(page) -> dict:
    try:
        return page.evaluate(ERRORS_JS)
    except Exception:
        return {"fields": [], "messages": []}


def open_posting(page, url: str):
    """Load a Workday posting (or its application) and stop early when Workday says the job is gone."""
    from . import submit as S
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    st = read_state(page)
    S._dismiss_cookies(page)
    k = kind(st)
    if k == "closed":
        raise S.Blocked("posting closed: the employer's page says the job is no longer available")
    if k == "error":
        raise S.Blocked(f"Workday shows an error page: {st.get('error', '')[:120]}")
    if k == "unknown":
        try:
            if S.CLOSED_RX.search(page.inner_text("body")[:3000]):
                raise S.Blocked("posting closed: the employer's page says the job is no longer available")
        except S.Blocked:
            raise
        except Exception:
            pass
    return st


def _click(page, selector: str, timeout: int = 5000) -> bool:
    loc = page.locator(selector).locator("visible=true")
    try:
        if not loc.count():
            return False
        try:
            loc.first.click(timeout=timeout)
        except Exception:
            loc.first.click(timeout=timeout, force=True)
        return True
    except Exception:
        return False


def _wait_drawn(page, max_s: float = 25.0) -> str:
    """The application frame is up but its page has not been drawn yet (Workday is still loading the step, often for half a
    minute right after signing in). Wait until it draws the step, a sign-in form or an error. Returns the kind of page."""
    t_end = time.time() + max_s
    k = "flow"
    while time.time() < t_end:
        page.wait_for_timeout(700)
        try:
            k = kind(page.evaluate(STATE_JS))
        except Exception:
            continue                                    # navigating
        if k != "flow":
            break
    return k


def _toward_form(page, st: dict, k: str, manual: str, moves: int, log) -> None:
    """One move from a job page / start page / half-drawn page toward the application form."""
    if k == "start":
        href = st.get("manual") or ""
        if href.startswith(("http://", "https://")):
            page.goto(href, wait_until="domcontentloaded", timeout=45000)
        elif href.startswith("/"):
            from urllib.parse import urljoin
            page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=45000)
        elif not _click(page, '[data-automation-id="applyManually"]') and not _click(page, '[data-automation-id="useMyLastApplication"]'):
            if manual:
                page.goto(manual, wait_until="domcontentloaded", timeout=45000)
        return
    if k == "flow" and moves < 4:
        _wait_drawn(page, 20)                           # the form is still being drawn: wait for it, not a fixed moment
        return
    if manual and (k != "job" or moves > 1 or "/apply" not in page.url):
        if moves == 1:
            log("      (opening Workday's application form directly)")
        page.goto(manual, wait_until="domcontentloaded", timeout=45000)
        return
    if not _click(page, '[data-automation-id="adventureButton"]') and manual:
        page.goto(manual, wait_until="domcontentloaded", timeout=45000)


def _match_flagged(fields: list[dict], errs: dict) -> set:
    """Ids of the extracted fields Workday complained about (by its field name, else by the label text)."""
    from .submit import _norm
    keys = {e.get("key") for e in errs.get("fields", []) if e.get("key") and e.get("key") != "formField-"}
    labels = [_norm(e.get("label", "")) for e in errs.get("fields", []) if e.get("label")]
    text = _norm(" ".join(errs.get("messages", [])))
    out = set()
    for f in fields:
        lab = _norm(re.sub(r"[*✱]", "", f.get("label", "")))
        if f.get("key") and f["key"] in keys:
            out.add(f["id"])
        elif lab and (any(lab == x or (len(lab) > 6 and (lab in x or x in lab)) for x in labels) or (len(lab) > 6 and lab in text)):
            out.add(f["id"])
    return out


NUMBER_MSG = re.compile(r"number entered is too (large|long|big)|must be (a |an )?(whole )?number|not a valid number|"
                        r"enter a (valid )?(whole )?number|numeric (values?|characters?) only|only (contain )?numbers", re.I)


def _number_fields(fields: list[dict], errs: dict) -> set:
    """Ids of the text boxes that Workday reads as numbers, going by its complaint. A pay box that looks like any other
    text box keeps only the digits typed into it, so '$70,000 - $80,000' becomes 7000080000 and Workday answers 'The number
    entered is too large'. Its complaint names the question; with no name, a single flagged text box is the one."""
    from .submit import _norm
    msgs = [m for m in errs.get("messages", []) if NUMBER_MSG.search(m)]
    if not msgs:
        return set()
    text = _norm(" ".join(msgs))
    cands = [f for f in fields if f.get("kind") in ("text", "textarea")]
    labs = {f["id"]: _norm(re.sub(r"[*✱]", "", f.get("label", ""))) for f in cands}
    out = {i for i, lab in labs.items() if len(lab) > 6 and lab in text}
    if not out:
        flagged = _match_flagged(cands, {"fields": errs.get("fields", []), "messages": []})
        if len(flagged) == 1:
            out = flagged
    return out


def _ident(f: dict) -> str:
    """What stays the same about a field from one look at the page to the next."""
    return f.get("sel") or (f.get("option_sels") or [""])[0] or f"{f.get('key')}|{f.get('label')}"


def _add_required_sections(page, errs: dict, log) -> bool:
    """Workday only shows the boxes of Work Experience / Education / Languages after 'Add'. When it says such a section is
    required (in its heading, or in its complaint after Next), add one block so the bot can fill it."""
    text = " ".join(errs.get("messages", []) + [e.get("label", "") for e in errs.get("fields", [])]).lower()
    added = False
    for _ in range(4):                                  # one at a time: adding a block redraws the page
        try:
            secs = page.evaluate(SECTIONS_JS)
        except Exception:
            break
        todo = [s for s in secs if not s.get("rows") and
                (s.get("required") or (len(s.get("name") or "") > 3 and (s.get("name") or "").lower() in text))]
        if not todo or not _click(page, f'[data-aa-add="{todo[0]["id"]}"]'):
            break
        log(f"      (Workday requires '{todo[0].get('name')}': adding a block for it)")
        page.wait_for_timeout(1200)
        added = True
    return added


def _advance(page, st0: dict, timeout_s: float = 20.0):
    """Press Workday's footer button and see what happens: ('moved', state) when the application went on to another
    page, ('errors', complaints) when Workday sent this page back, ('same', {}) when nothing visibly changed."""
    if not _click(page, NEXT):
        return "nobutton", {}
    k0 = kind(st0)
    t_end, t0 = time.time() + timeout_s, time.time()
    while time.time() < t_end:
        page.wait_for_timeout(500)
        try:
            st = page.evaluate(STATE_JS)
        except Exception:
            continue                                            # navigating
        if kind(st) != k0 or st.get("index") != st0.get("index") or st.get("page") != st0.get("page") or st.get("name") != st0.get("name"):
            return "moved", st
        if time.time() - t0 >= 1.5:                             # give Workday a moment to answer before reading complaints
            errs = read_errors(page)
            if errs["fields"] or errs["messages"]:
                page.wait_for_timeout(600)                      # the same page with complaints: make sure it is not mid-move
                try:
                    st = page.evaluate(STATE_JS)
                except Exception:
                    continue
                if st.get("index") == st0.get("index") and st.get("page") == st0.get("page"):
                    return "errors", read_errors(page)
                return "moved", st
    return "same", {}


def apply(page, job, brain, cover_letter: str, files: dict, shot, dry_run: bool, log=print, accounts=None, on_click=None, on_stage=None) -> str:
    """Drive one Workday application from wherever the browser is to the confirmation. Returns 'confirmed', 'dry_run' or
    'already_applied'; raises the same exceptions as the generic form driver."""
    from . import submit as S, auth
    t_end = time.time() + S.MAX_APPLY_SECONDS
    src = getattr(job, "apply_url", "") or ""
    if "myworkdayjobs.com" not in src:                 # an employer's own career page that handed over to Workday
        src = page.url
    home = posting_url(src)
    manual = home + "/apply/applyManually" if "/job/" in home and "myworkdayjobs.com" in home else ""
    resume_box = resume_in = False                     # a résumé box was shown / the résumé is in it
    email_last = False                                 # the last thing done was pressing 'Sign in with email'
    blank_auth = strays = 0
    visits: dict[tuple, int] = {}            # step -> how many times it has been filled in
    moves = auth_tries = email_clicks = total = flow_waits = 0
    flagged: dict[tuple, dict] = {}          # step -> Workday's complaints from the last try
    known: dict[tuple, set] = {}             # step -> the fields that were there on the last try
    stage = ""
    for _turn in range(45):
        if time.time() > t_end:
            raise RuntimeError(f"took longer than {S.MAX_APPLY_SECONDS // 60} minutes on this site")
        st = read_state(page)
        k = kind(st)
        if k == "closed":
            raise S.Blocked("posting closed: the employer's page says the job is no longer available")
        if k == "error":
            raise S.Blocked(f"Workday shows an error page: {st.get('error', '')[:120]}")
        if k == "already":
            log("      Workday says you already applied to this job")
            return "already_applied"
        b = S._blocker(page)
        if b and not (k == "auth" and b == "login required"):
            raise S.Blocked(b)
        if "myworkdayjobs.com" in home and "myworkdayjobs.com" in page.url and S._host(page.url) != S._host(home):
            strays += 1                                # on another employer's Workday site (a link led there): go back
            if strays > 2 or not manual:
                raise S.Blocked("ended up on another employer's Workday site: stopped before filling anything in there")
            page.goto(manual, wait_until="domcontentloaded", timeout=45000)
            continue

        if k == "auth":
            if stage != "account" and on_stage:
                on_stage("account")
            stage = "account"
            if not (accounts and accounts.enabled):
                raise S.Blocked("account: Workday needs an account and accounts are off (no ACCOUNT_PASSWORD) or sign-in did not finish")
            if dry_run and not accounts.create_in_dry_run:
                page.screenshot(path=str(shot), full_page=True)
                log("      (dry run stops at the account screen; a live run creates/signs in here)")
                return "dry_run"
            if st.get("emailButton") and (not st.get("passwords") or not email_last):
                # the page (or pop-up over the sign-in form) that offers Google / Apple / email: choose email. It comes
                # back after every step of the account set-up, so these clicks are not counted as sign-in tries.
                email_last = True
                email_clicks += 1
                if email_clicks > 6:
                    raise S.Blocked(f"account: Workday keeps returning to its 'Sign in with email' page; page shows: {S._visible_buttons(page)}")
                _click(page, '[data-automation-id="SignInWithEmailButton"], [data-automation-id="signInWithEmailButton"]')
                page.wait_for_timeout(1500)
                continue
            email_last = False
            if not st.get("passwords") and not auth.VERIFY_MSG.search(auth._body(page)):
                # Workday's sign-in page with nothing drawn in it yet (seen right after an account was made)
                blank_auth += 1
                if blank_auth > 4:
                    raise S.Blocked(f"account: Workday's sign-in page stayed empty; page shows: {S._visible_buttons(page)}")
                page.wait_for_timeout(2500)
                if blank_auth >= 2 and manual:
                    page.goto(manual, wait_until="domcontentloaded", timeout=45000)
                continue
            auth_tries += 1
            if auth_tries > 5:
                raise S.Blocked(f"account: still at Workday's sign-in after {auth_tries - 1} tries; page shows: {S._visible_buttons(page)}")
            if auth_tries == 1:
                log("      (account page: creating the account / signing in)")
            try:
                if not st.get("passwords") and auth.VERIFY_MSG.search(auth._body(page)):
                    auth._verify_email(page, accounts, time.time() - 900, log)      # 'check your email' notice with no boxes
                    if manual:
                        page.goto(manual, wait_until="domcontentloaded", timeout=45000)
                else:
                    auth.handle(page, accounts, log, url_after=manual or home)
            except auth.AuthBlocked as e:
                raise S.Blocked(f"account: {e}")
            continue

        if k == "flow":
            # Signed in and inside the application, but the step itself has not been drawn. That is Workday being slow (it
            # has taken half a minute), not a wrong page: wait for it, load the form afresh once, wait again, then give up.
            flow_waits += 1
            if flow_waits > 5:
                raise S.Blocked(f"not a real application form: Workday's application page stayed empty for about two minutes; page shows: {S._visible_buttons(page)}")
            if flow_waits == 4 and manual:
                log("      (Workday has not drawn the form yet: loading it again)")
                page.goto(manual, wait_until="domcontentloaded", timeout=45000)
                continue
            _wait_drawn(page, 20)
            continue
        if k != "form":
            moves += 1
            if moves > 5:
                raise S.Blocked(f"not a real application form: could not reach Workday's form; page shows: {S._visible_buttons(page)}")
            _toward_form(page, st, k, manual, moves, log)
            continue

        # ---------------------------------------------------------------- a step of the application
        moves = 0                                      # (the allowance for finding the form starts afresh after each step)
        name = st.get("name") or st.get("heading") or st.get("page") or "application"
        if name != stage and on_stage:
            on_stage(name)
        stage = name
        sid = (st.get("index"), st.get("page") or "", name)      # two pages may share a name ('Application Questions' 1 and 2)
        visits[sid] = visits.get(sid, 0) + 1
        review = bool(re.search(r"\bsubmit\b", st.get("next", ""), re.I)) or st.get("page") == "applyFlowReviewPage"
        if review:
            btn = page.locator(NEXT).locator("visible=true").first
            if not (st.get("done") or total >= 4):
                raise S.Blocked(f"not a real application form: Workday shows Submit before any step was filled in ({total} fields)")
            if resume_box and not resume_in:
                raise RuntimeError("the résumé did not get attached (Workday's upload box never showed the file): not submitting without it")
            page.screenshot(path=str(shot), full_page=True)
            if dry_run:
                log(f"      {name}: ready to submit (dry run stops here)")
                return "dry_run"
            log(f"      {name}: submitting")
            try:
                result = S.submit(page, timeout_ms=30000, btn=btn, on_click=on_click)
            except (S.Unconfirmed, S.NotSubmitted):
                try:
                    page.screenshot(path=str(shot.with_name("after_submit.png")), full_page=True)
                except Exception:
                    pass
                raise
            try:
                page.screenshot(path=str(shot.with_name("confirmation.png")), full_page=True)
            except Exception:
                pass
            return result

        if visits[sid] > 3:
            errs = flagged.get(sid) or read_errors(page)
            raise S.Unanswerable(
                f"stuck on Workday's '{name}' page; Workday says: {errs.get('messages') or 'nothing'}; "
                f"fields flagged: {[e.get('label') for e in errs.get('fields', [])][:6] or 'none'}")

        t0 = time.time()
        errs = flagged.get(sid) or {}
        if visits[sid] > 1 and not (errs.get("fields") or errs.get("messages")):
            errs = read_errors(page)                 # back on a page it already filled in: whatever Workday shows now
        if visits[sid] == 1 or errs.get("fields") or errs.get("messages"):
            _add_required_sections(page, errs, log)
        fields = S.extract(page)
        plan = brain.map_fields(job, fields, cover_letter)
        need = set(plan["unanswerable_required"])
        missing = [f for f in fields if f["id"] in need and not S._already_answered(f)]
        if missing:
            for f in missing:
                log(f"      ? unanswered: {f['label'][:90]!r} kind={f['kind']} options={[o[:30] for o in (f.get('options') or [])][:6]}")
            raise S.Unanswerable("can't truthfully answer required: " + "; ".join(f["label"][:60] for f in missing))
        answers = dict(plan["answers"])
        if "COVER_LETTER" in answers.values() and "COVER_LETTER" not in files:
            want_letter = [f for f in fields if answers.get(f["id"]) == "COVER_LETTER" and f.get("required")]
            letter_txt = brain.lazy_letter(job, required=bool(want_letter))
            if letter_txt:
                (shot.parent / "cover_letter.txt").write_text(letter_txt)
                files["COVER_LETTER"] = files["_LETTER_MAKER"](letter_txt)
            else:
                if want_letter:
                    raise S.Unanswerable("form requires a cover letter, the writer couldn't write one and there is no cover_letter_template")
                answers = {i: v for i, v in answers.items() if v != "COVER_LETTER"}
        n_req = sum(1 for f in fields if f.get("required"))
        if visits[sid] == 1:
            total += len(fields)
            log(f"      {st.get('index', 0) + 1}/{len(st.get('steps') or []) or '?'} {name}: {len(fields)} fields ({n_req} required)")
        else:
            # Workday sent this page back: redo only what it complained about, plus required fields that are still empty
            redo = _match_flagged(fields, errs)
            redo |= {f["id"] for f in fields if f.get("required") and not S._already_answered(f)}
            redo |= {f["id"] for f in fields if _ident(f) not in known.get(sid, set())}       # a block that was just added
            numeric = _number_fields(fields, errs)
            if numeric:
                # a box Workday reads as a number got words or a range: answer it again as the number box it is
                for f in fields:
                    if f["id"] in numeric:
                        f["kind"] = "number"
                again = brain.map_fields(job, fields, cover_letter)["answers"]
                for i in numeric:
                    plan["answers"][i] = answers[i] = again.get(i)
                redo |= numeric
            log(f"      {name} (try {visits[sid]}): Workday said {errs.get('messages') or 'nothing'}; "
                f"redoing {[f['label'][:30] for f in fields if f['id'] in redo][:8] or 'the required fields'}")
            if redo:
                answers = {i: v for i, v in answers.items() if i in redo}
            # An optional box Workday calls invalid: empty it when the bot has no answer for it (a leftover value), and also
            # when Workday has now refused the bot's answer twice (an employer's 'LinkedIn' box that Workday checks as a
            # Facebook address accepts no LinkedIn address, and an empty optional box is always accepted).
            refused_txt = S._norm(" ".join(m for m in errs.get("messages", []) if re.search(r"\binvalid\b|not (a )?valid|valid (url|link|username|address)", m, re.I)))
            for f in fields:
                if f["id"] in redo and not f.get("required") and f["kind"] in ("text", "url", "tel", "email", "number") and f.get("has_value"):
                    lab = S._norm(re.sub(r"[*✱]", "", f.get("label", "")))
                    twice = visits[sid] >= 3 and bool(refused_txt) and len(lab) > 6 and lab in refused_txt
                    if plan["answers"].get(f["id"]) in (None, "") or twice:
                        try:
                            S._loc(page, f).fill("", timeout=3000)
                        except Exception:
                            pass
                        if twice:
                            answers.pop(f["id"], None)
                            log(f"      (Workday refused the answer in the optional box '{f.get('label', '')[:50]}' twice: left empty)")
        known[sid] = {_ident(f) for f in fields}
        # lists first: picking a country or state makes Workday redraw (and empty) the name and address boxes
        kinds = {f["id"]: f["kind"] for f in fields}
        answers = dict(sorted(answers.items(), key=lambda kv: 0 if kinds.get(kv[0]) == "combobox" else 1))
        S.fill(page, fields, answers, files, log, deadline=t_end)
        if any(f["kind"] == "file" and answers.get(f["id"]) for f in fields):
            S._wait_uploads(page)
        if any(f["kind"] == "file" and plan["answers"].get(f["id"]) == "RESUME" for f in fields):
            resume_box = True
            try:
                resume_in = resume_in or page.locator('[data-automation-id="file-upload-item-name"]').count() > 0
            except Exception:
                pass
            if not resume_in:
                log("      ! the résumé does not show in Workday's upload box")
        S.verify(page, fields, answers, log)
        if S._list_open(page):
            S._close_list(page)
        try:
            page.screenshot(path=str(shot if st.get("index", 0) == 0 else shot.with_name(f"step{st.get('index', 0) + 1}.png")), full_page=True)
        except Exception:
            pass
        st_now = read_state(page, 3000)
        if kind(st_now) == "form" and (st_now.get("index"), st_now.get("page")) != (st.get("index"), st.get("page")):
            continue                                   # the page moved on by itself meanwhile: Next belongs to the step just filled, not this one
        what, info = _advance(page, st_now if kind(st_now) == "form" else st)
        took = time.time() - t0
        if what == "moved":
            flagged.pop(sid, None)
            if took > 45:
                log(f"      ({name} took {took:.0f}s)")
            continue
        if what == "nobutton":
            raise S.Blocked(f"no Next or Submit button found on Workday's '{name}' page; page shows: {S._visible_buttons(page)}")
        if what == "errors":
            flagged[sid] = info
        else:
            flagged[sid] = read_errors(page)
            if not flagged[sid]["fields"] and not flagged[sid]["messages"]:
                flagged[sid]["messages"] = ["the page did not move on after Next"]
    raise S.Blocked("more than 45 turns on one Workday application; giving up")
