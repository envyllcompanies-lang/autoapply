"""Generic application-form driver: find the form, read every field, answer from your facts, fill, submit, verify."""
from __future__ import annotations

import re
import time
from pathlib import Path

EXTRACT_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 1200);
  const visible = el => {
    if (el.type === 'file') return true;             // usually hidden behind a styled button
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelOf = el => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => document.getElementById(i)?.innerText || '').join(' ');
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
    if (!t) {
      // the nearest question text BEFORE this box in its block: never an answer option's <label> (it holds its own input)
      // and never a label that belongs to a later box (Lever cards put several questions in one block)
      const SEL = 'label, legend, .application-label, [class*="question-label"], [class*="field-label"], [class*="questionLabel"]';
      let p = el.parentElement;
      for (let i = 0; i < 5 && p && !t; i++, p = p.parentElement) {
        const before = [...p.querySelectorAll(SEL)].filter(l => !l.contains(el) && !l.querySelector('input, textarea, select') &&
                         (l.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) && clean(l.innerText).length > 1);
        if (before.length) t = before[before.length - 1].innerText;
      }
    }
    const ph = /^(type|enter|start typing|select|choose|search|your answer|answer here|write)( your| an?| here)?\b/i.test(el.placeholder || '') ? '' : (el.placeholder || '');
    return clean(t || ph || el.name || '');
  };
  const questionOf = (members) => {           // text of the smallest container holding a whole radio/checkbox group
    const grp = members[0].closest('[role="radiogroup"],[role="group"],fieldset');
    if (grp && grp.getAttribute('aria-labelledby')) {
      const t = clean(grp.getAttribute('aria-labelledby').split(/\s+/).map(i => document.getElementById(i)?.innerText || '').join(' '));
      if (t.length > 3) return t + (members.some(m => m.required || m.getAttribute('aria-required') === 'true') && !/[*\u2731]/.test(t) ? ' *' : '');
    }
    let p = members[0].parentElement;
    while (p && !members.every(m => p.contains(m))) p = p.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      const lg = p.querySelector('legend'); if (lg) return clean(lg.innerText);
      let txt = p.innerText || '';
      members.forEach(m => { txt = txt.replace(labelOf(m), ''); });
      txt = clean(txt);
      if (txt.length > 3) return txt;
    }
    return '';
  };

  const BTN_WORDS = /^(attach|upload|browse|choose|select|add)\b/i;
  const STRIP = /\b(attach|dropbox|google drive|enter manually|paste|upload( a)?( file)?|browse|choose( a)? file|no file chosen|or drag and drop( here)?|drag and drop|drop (your )?files? here|remove|replace|accepted file types?:[^\n]*|(max(imum)?|file) size[^\n]*|pdf, doc[^\n]*)\b/gi;
  const fileLabelOf = el => {
    const txt = x => clean(x && x.innerText);
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && txt(l) && !BTN_WORDS.test(txt(l))) return txt(l); }
    if (el.getAttribute('aria-labelledby')) {
      const t = clean(el.getAttribute('aria-labelledby').split(/\s+/).map(i => txt(document.getElementById(i))).join(' '));
      if (t && !BTN_WORDS.test(t)) return t;
    }
    if (el.getAttribute('aria-label') && !BTN_WORDS.test(el.getAttribute('aria-label'))) return clean(el.getAttribute('aria-label'));
    if (el.id) for (const k of ['upload-label-' + el.id, el.id + '-label', el.id + '_label', 'label-' + el.id, el.id + 'Label']) {
      const x = document.getElementById(k); if (x && txt(x)) return txt(x);
    }
    let p = el.parentElement;
    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {       // the smallest box around the input that has its own words
      const t = clean((p.innerText || '').replace(STRIP, ' '));
      if (t.length > 2 && t.length <= 90) return t;
      if (t.length > 90) break;
    }
    return clean(el.name || el.id || '');
  };

  // A stable address for an element (its own id / name), so it can be found again after the site redraws that part of the
  // page: Workday replaces the name and address boxes when the state or country changes, and markers set here are lost.
  const q = s => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const one = s => { try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; } };
  const selOf = el => {
    const tagn = el.tagName.toLowerCase();
    if ((el.type === 'radio' || el.type === 'checkbox') && el.name) {
      const s = `input[type="${el.type}"][name="${q(el.name)}"]` + (el.type === 'radio' && el.value ? `[value="${q(el.value)}"]` : '');
      if (one(s)) return s;
    }
    if (el.id && !/^[a-z0-9]{5,6}$/.test(el.id) && one(`[id="${q(el.id)}"]`)) return `[id="${q(el.id)}"]`;
    const aid = el.getAttribute('data-automation-id');
    if (aid && one(`${tagn}[data-automation-id="${q(aid)}"]`)) return `${tagn}[data-automation-id="${q(aid)}"]`;
    if (el.name && one(`${tagn}[name="${q(el.name)}"]`)) return `${tagn}[name="${q(el.name)}"]`;
    const box = el.closest('[data-automation-id^="formField-"]');
    const k = box && box.getAttribute('data-automation-id');
    if (k && k !== 'formField-') {
      const s = `[data-automation-id="${q(k)}"] ${tagn}` + (el.type ? `[type="${el.type}"]` : '');
      if (one(s)) return s;
    }
    if (el.id && one(`[id="${q(el.id)}"]`)) return `[id="${q(el.id)}"]`;
    return '';
  };
  const keyOf = el => { const b = el.closest('[data-automation-id^="formField-"]'); return b ? b.getAttribute('data-automation-id') : ''; };
  // Workday: only the application itself, never the header (language picker, account menu) or the footer
  const flow = document.querySelector('[data-automation-id="applyFlowPage"]');
  const root = flow || document;
  const chrome = el => !!el.closest('header, nav, footer, [data-automation-id="header"], [data-automation-id="utilityButtonBar"], ' +
                                    '[data-automation-id="footerContainer"], [class*="iti__country"], [class*="iti__search"], ' +
                                    '[class*="iti__dropdown"], [class*="iti__selected"], [class*="iti__flag"]');
  const HONEY = /for robots|robots only|leave (this )?(field |box )?(blank|empty)|do not (fill|enter|complete)|honeypot|if you are (a )?human/i;
  const fieldQuestion = el => {             // Workday: <div data-automation-id="formField-..."><fieldset><legend>question</legend> ... <button>
    const box = el.closest('[data-automation-id^="formField"]');
    if (!box) return '';
    const l = box.querySelector('legend, label');
    const t = clean(l && !l.contains(el) ? l.innerText : '');
    return t;
  };
  let n = window.__aa || 0; const tag = el => { if (!el.dataset.aa) el.dataset.aa = 'f' + (n++); window.__aa = n; return el.dataset.aa; };
  const fields = [], groups = {};
  const els = root.querySelectorAll('input, textarea, select');
  for (const el of els) {
    if (chrome(el)) continue;
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    const choice = type === 'radio' || type === 'checkbox';
    // Workable-style choices: the real <input> is hidden (aria-hidden, zero size) inside a visible role="radio" / <label> wrapper
    const shown = choice ? (el.closest('[role="radio"],[role="checkbox"],label') || el) : el;
    if (el.disabled || el.name === 'g-recaptcha-response' || (el.parentElement && el.parentElement.closest('[aria-hidden="true"]'))) continue;
    if (el.getAttribute('aria-hidden') === 'true' && !choice) continue;
    if (!visible(el) && !(choice && visible(shown))) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio' || type === 'checkbox') {
      const g = el.name || tag(el);
      (groups[g] = groups[g] || { type, members: [], required: false }).members.push(el);
      groups[g].required ||= required;
      continue;
    }
    if (type !== 'file' && !el.id && !el.name && el.parentElement && el.parentElement.querySelector(':scope > button[aria-haspopup="listbox"]')) continue;
    const lab0 = el.getAttribute('data-automation-id') === 'phone-sms-opt-in' ? 'I agree to receive text messages (SMS) at this number' : labelOf(el);
    if (HONEY.test(lab0) || HONEY.test(el.getAttribute('aria-label') || '') || HONEY.test(el.placeholder || '')) continue;   // spam traps
    const f = { id: tag(el), required, label: lab0, maxlength: (el.maxLength > 0 && el.maxLength < 100000) ? el.maxLength : null,
                sel: selOf(el), key: keyOf(el), has_value: !!(el.value && type !== 'file') };
    const dsec = el.getAttribute('data-automation-id') || '';
    if (/^dateSection(Day|Year)-input$/.test(dsec)) continue;                 // Workday date: handled as one field (the month box)
    if (dsec === 'dateSectionMonth-input') {
      const dq = fieldQuestion(el) || 'Date';
      const wrap = el.closest('[data-automation-id="dateInputWrapper"]') || el.parentElement;
      fields.push({ id: tag(el), kind: 'wddate', label: dq, required: /[*\u2731]/.test(dq), sel: selOf(el), key: keyOf(el),
                    maxlength: null, hasDay: !!(wrap && wrap.querySelector('[data-automation-id="dateSectionDay-input"]')) });
      continue;
    }
    if (el.getAttribute('data-uxi-widget-type') === 'selectinput') {           // Workday search-and-pick box
      f.kind = 'wdprompt';
      const pq = fieldQuestion(el); if (pq) f.label = pq;
      const mbox = el.closest('[data-automation-id="multiSelectContainer"]');
      f.selected = mbox ? [...mbox.querySelectorAll('[data-automation-id="selectedItem"]')].map(x => clean(x.innerText)).filter(Boolean) : [];
      if (/[*\u2731]/.test(f.label) || el.getAttribute('aria-required') === 'true') f.required = true;
    }
    else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') f.kind = 'combobox';
    else if (el.tagName === 'SELECT') {
      f.kind = 'select';
      f.options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--)/i.test(t));
    } else if (el.tagName === 'TEXTAREA') f.kind = 'textarea';
    else if (type === 'file') {
      f.kind = 'file'; f.accept = el.accept || ''; f.label = fileLabelOf(el);
      const up = el.closest('[data-automation-id="attachments-FileUpload"]');
      if (up) {                                  // Workday: the upload box is named by its section ('Resume/CV')
        const grp = up.closest('[role="group"]');
        const h = grp && grp.querySelector('h4, h3');
        const own = up.getAttribute('aria-labelledby') && document.getElementById(up.getAttribute('aria-labelledby'));
        if (h && clean(h.innerText)) f.label = clean(h.innerText) + (own && /[*\u2731]/.test(own.innerText) ? ' *' : '');
        f.has_file = [...up.querySelectorAll('[data-automation-id="file-upload-item-name"]')].map(x => clean(x.innerText)).filter(Boolean);
      }
      f.hint = [el.id, el.name, el.getAttribute('data-qa'), el.getAttribute('data-testid'), el.getAttribute('data-automation-id')].filter(Boolean).join(' ');
      f.elid = el.id || '';
    }
    else f.kind = ['email', 'tel', 'url', 'number', 'date', 'password'].includes(type) ? type : 'text';
    if (/[*\u2731]/.test(f.label)) f.required = true;
    fields.push(f);
  }
  for (const [name, g] of Object.entries(groups)) {
    const isOn = m => !!m.checked || m.getAttribute('aria-checked') === 'true';
    const optLabel = m => m.getAttribute('data-automation-id') === 'phone-sms-opt-in' ? 'I agree to receive text messages (SMS) at this number' : (labelOf(m) || m.value);
    const opts = g.members.map(m => ({ id: tag(m), label: optLabel(m), sel: selOf(m) }));
    const gq = fieldQuestion(g.members[0]) || questionOf(g.members);
    const gbox = g.members[0].closest('[aria-required="true"]');
    if (g.type === 'checkbox' && g.members.length === 1)
      fields.push({ id: opts[0].id, kind: 'checkbox_single', label: opts[0].label, question: gq, required: g.required,
                    sel: opts[0].sel, key: keyOf(g.members[0]), checked: isOn(g.members[0]) });
    else
      fields.push({ id: 'g_' + opts[0].id, kind: g.type === 'radio' ? 'radio' : 'checkbox_group',
                    label: gq || name, required: g.required || /[*\u2731]/.test(gq) || !!gbox, key: keyOf(g.members[0]),
                    options: opts.map(o => o.label), option_ids: opts.map(o => o.id), option_sels: opts.map(o => o.sel),
                    checked: g.members.map((m, i) => isOn(m) ? opts[i].label : null).filter(Boolean) });
  }
  // custom dropdowns drawn as buttons (Workday)
  for (const b of root.querySelectorAll('button[aria-haspopup="listbox"]')) {
    if (b.disabled || b.closest('[aria-hidden="true"]') || !visible(b) || chrome(b)) continue;
    const bq = fieldQuestion(b);
    const own = labelOf(b);
    const lab = bq || own;
    const req = /[*\u2731]/.test(lab) || /required/i.test(b.getAttribute('aria-label') || '') || b.getAttribute('aria-required') === 'true';
    fields.push({ id: tag(b), kind: 'combobox', label: lab + (req && !/[*\u2731]/.test(lab) ? ' *' : ''), required: req, maxlength: null,
                  sel: selOf(b), key: keyOf(b), button: true, cur: clean(b.innerText) });
  }
  // Ashby-style Yes/No answered with two plain <button>s instead of radio inputs
  const btnGroups = new Map();
  for (const b of root.querySelectorAll('button')) {
    if ((b.getAttribute('type') || '') === 'submit' || b.disabled || b.closest('[aria-hidden="true"]') || !visible(b) || chrome(b)) continue;
    if (!/^(yes|no)$/i.test((b.innerText || '').trim())) continue;
    const par = b.parentElement;
    if (!btnGroups.has(par)) btnGroups.set(par, []);
    btnGroups.get(par).push(b);
  }
  for (const [par, bs] of btnGroups) {
    if (bs.length !== 2) continue;
    let yq = '', c = par;
    for (let i = 0; i < 4 && c && !yq; i++, c = c.parentElement) {
      const txt = clean(c.innerText).replace(/\s*Yes\s*No\s*$/i, '').trim();
      if (txt.length > 3) yq = txt;
    }
    const ord = bs.map(b => ({ id: tag(b), label: clean(b.innerText) }));
    fields.push({ id: 'g_' + ord[0].id, kind: 'radio', label: yq, required: /[*\u2731]/.test(yq),
                  options: ord.map(o => o.label), option_ids: ord.map(o => o.id) });
  }
  return fields;
}
"""

SUCCESS_RE = re.compile(
    r"(thank(s| you) for (applying|your application|submitting)|application (has been |was )?(submitted|received|sent|complete)"
    r"|we('ve| have) (successfully )?received your (application|resume|r\u00e9sum\u00e9)|successfully (submitted|applied)|your application is in"
    r"|you('ve| have) (successfully )?applied|we got your application|your application has been (sent|filed|recorded))", re.I)
BLOCKERS = [
    ('iframe[src*="recaptcha/api2/bframe"]', "reCAPTCHA challenge"),
    ('iframe[src*="recaptcha/api2/anchor"]', "reCAPTCHA checkbox"),
    ('iframe[src*="hcaptcha.com"]', "hCaptcha"),
    ('iframe[src*="challenges.cloudflare.com"]', "Cloudflare Turnstile"),
    ('input[type="password"]', "login required"),
]


def _blockers():
    return [b for b in BLOCKERS if not (ACCOUNTS_ENABLED and b[1] == "login required")]


class Blocked(Exception):
    pass


# Greenhouse is supported. Its normal `gh_jid` query parameter identifies the job and must never be treated as an unsupported board.
UNSUPPORTED_ALWAYS = re.compile(r"linkedin\.com|indeed\.com|glassdoor\.com|ziprecruiter\.com|smartrecruiters\.com/oneclick", re.I)
UNSUPPORTED_NEEDS_ACCOUNT = re.compile(r"myworkdayjobs|\.workday\.com|icims\.com|taleo\.net|successfactors|oraclecloud\.com|"
                                       r"ultipro\.com|ukg\.com|paylocity|paycomonline|brassring|adp\.com", re.I)
ACCOUNTS_ENABLED = False          # set by main when the ACCOUNT_PASSWORD secret exists
MAX_APPLY_SECONDS = 480           # main sets this from search.max_minutes_per_job: no single site may eat the run's free minutes


class _Unsup:
    def search(self, url):
        return UNSUPPORTED_ALWAYS.search(url) or (None if ACCOUNTS_ENABLED else UNSUPPORTED_NEEDS_ACCOUNT.search(url))


UNSUPPORTED = _Unsup()
APPLY_BTN = re.compile(r"^\s*(apply( now| here| today| online| for this (job|position|role)| to this job)?|"
                       r"apply (on|at|via) (the )?(company|employer)('s)? (site|website|page)|i'?m interested|apply externally)\s*[>\u2192]?\s*$", re.I)


def _looks_like_form(page) -> bool:
    try:
        return page.locator('form input[type=file]:visible, form input[type=email]:visible, input[type=file]').count() > 0 \
            and page.locator("form input:visible, form textarea:visible").count() >= 3
    except Exception:
        return False


def resolve_fast(url: str) -> str | None:
    """Plain HTTP fetch (no browser): many job-board pages already contain the link to the employer's application system."""
    import requests
    from .aggregators import find_job_link
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                                     "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"}, timeout=15, allow_redirects=True)
        if UNSUPPORTED.search(r.url):
            raise Blocked(f"unsupported application site ({re.search(r'//([^/]+)', r.url).group(1)})")
        return find_job_link(r.text[:600000], r.url)
    except Blocked:
        raise
    except Exception:
        return None


def resolve_apply_url(page, url: str, log=print) -> str:
    """Follow a job-board / aggregator link to the employer's real application page (max 3 hops)."""
    from .aggregators import board_of
    fast = resolve_fast(url)
    if fast:
        return fast
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    for _ in range(3):
        page.wait_for_timeout(1500)
        cur = page.url
        if UNSUPPORTED.search(cur):
            raise Blocked(f"unsupported application site ({re.search(r'//([^/]+)', cur).group(1)})")
        if board_of(cur) or _looks_like_form(page):
            return cur
        btn = page.locator("a:visible, button:visible").filter(has_text=APPLY_BTN).first
        if not btn.count():
            break
        href = btn.get_attribute("href") if btn.evaluate("e => e.tagName") == "A" else None
        if href and href.startswith(("http://", "https://")):
            page.goto(href, wait_until="domcontentloaded", timeout=30000)
            continue
        try:
            with page.context.expect_page(timeout=4000) as pi:
                btn.click()
            newp = pi.value
            newp.wait_for_load_state("domcontentloaded")
            newp.wait_for_timeout(1500)
            final = newp.url
            newp.close()
            if UNSUPPORTED.search(final):
                raise Blocked(f"unsupported application site ({re.search(r'//([^/]+)', final).group(1)})")
            return final
        except Blocked:
            raise
        except Exception:
            page.wait_for_timeout(2000)      # no popup: the click navigated this page
    cur = page.url
    if UNSUPPORTED.search(cur):
        raise Blocked(f"unsupported application site ({re.search(r'//([^/]+)', cur).group(1)})")
    if board_of(cur) or _looks_like_form(page):
        return cur
    raise Blocked("could not find the employer's application page")


AI_BAN = [re.compile(p, re.I) for p in (
    r"(do not|don'?t|please do not|must not|may not|not permitted to|not allowed to|prohibited from|refrain from|avoid|"
    r"cannot|can'?t) (use|using|utili[sz]e|utili[sz]ing|rely on|relying on|submit|submitting)\b[^.\n]{0,70}\b"
    r"(ai|a\.i\.|artificial intelligence|chatgpt|gpt|llms?|generative|large language model)",
    r"\b(ai|a\.i\.|artificial intelligence|chatgpt|generative ai)\b[^.\n]{0,60}\b(is|are|will be) "
    r"(not (permitted|allowed|accepted)|prohibited|forbidden|unacceptable|not welcome)",
    r"(ai|chatgpt|llm)[- ]generated[^.\n]{0,70}(disqualif|reject|not (be )?(considered|accepted|reviewed))",
    r"\bno (ai|chatgpt|llm)[- ]?(generated|written|assisted|use)",
    r"(must|should) be (entirely |solely |completely |strictly )?(your own|written by you|in your own words)[^.\n]{0,60}"
    r"(without|no|not)[^.\n]{0,20}\b(ai|chatgpt|assistance)",
)]


class Unanswerable(Exception):
    pass


class NotSubmitted(Exception):
    """The site rejected the form (validation errors still showing): nothing was sent, safe to retry later."""


class Unconfirmed(Exception):
    """Submit was clicked but no confirmation appeared: it may or may not have gone through, so it is never retried."""
    t0: float = 0.0


def guard_ai_policy(page, job):
    """Never submit AI-written answers to an employer that says it doesn't want them."""
    text = (job.description or "") + "\n" + page.inner_text("body")
    for rx in AI_BAN:
        m = rx.search(text)
        if m:
            raise Unanswerable("posting restricts AI-assisted applications: \"" + " ".join(m.group(0).split())[:90] + "\"")


def _blocker(page) -> str | None:
    """What stands in the way on this page: a human check (reCAPTCHA, hCaptcha, Cloudflare Turnstile) or a login wall.
    A human check ends the application. The bot never solves one, clicks through one, or hands one to a solving service."""
    for sel, name in _blockers():
        for el in page.locator(sel).all():
            try:
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box["width"] > 30 and box["height"] > 30:
                        return name if name == "login required" else f"{name} (a human check: the bot stops here)"
            except Exception:
                pass
    return None


def _email_code_prompt(page) -> bool:
    """True for an explicit email/OTP verification screen, not a CAPTCHA or ordinary form field."""
    try:
        body = " ".join(page.inner_text("body").split())
        code_box = page.locator(
            "input[autocomplete=one-time-code], input[name*=otp i], input[id*=otp i], "
            "input[name*=verification i], input[id*=verification i], input[name*=code i], "
            "input[id*=code i], input[placeholder*=code i], input[aria-label*=code i], "
            "input[inputmode=numeric][maxlength='1'], input[maxlength='1']"
        ).locator("visible=true")
        n_code = 0
        for i in range(min(code_box.count(), 12)):
            hint = " ".join(filter(None, (code_box.nth(i).get_attribute(a) for a in ("name", "id", "placeholder", "aria-label", "autocomplete")))).lower()
            if re.search(r"postal|zip|post ?code|country|area ?code|dial|promo|coupon|referr|discount|job ?code|req", hint):
                continue                         # an ordinary form box that merely has 'code' in its name
            n_code += 1
        if not n_code:
            return False
        return bool(re.search(
            r"verification code|one[- ]time (code|password|passcode|pin)|"
            r"enter (the )?(code|passcode|otp)|check (your )?(email|inbox)|"
            r"(we|we've|we have|the site) (sent|emailed|e-?mailed) (you )?(a |an )?"
            r"(code|passcode|one[- ]time|verification)|"
            r"code (was|has been|is) sent (to|via|by) (your )?e-?mail",
            body, re.I))
    except Exception:
        return False


def _code_after_submit(page, body: str) -> bool:
    """After Submit was clicked the site shows boxes for a code it emailed ('Enter the 8-character code that was sent to
    your email') instead of a confirmation. Only used to stop waiting and say so; the bot does not enter this code."""
    try:
        if not re.search(r"security code|verification code|enter the [\w-]{0,14} ?code|code (that )?(was|has been|is) sent to your e-?mail|"
                         r"(sent|emailed) (you )?a (security |verification )?code", " ".join(body.split()), re.I):
            return False
        boxes = page.locator("input[id^='security-input'], input[autocomplete=one-time-code], input[name*=security i], input[id*=security i], "
                             "input[name*=verification i], input[id*=verification i], input[maxlength='1']").locator("visible=true")
        return boxes.count() > 0
    except Exception:
        return False


def _few_inputs(page) -> bool:
    """A real confirmation page has (almost) no fields left; a mid-form 'thank you' heading does not count."""
    try:
        return page.locator("input:not([type]), input[type=text], input[type=email], input[type=tel], input[type=file], textarea, select") \
            .locator("visible=true").count() < 2
    except Exception:
        return True


def _has_form(page) -> bool:
    if page.locator("input[type=email], input[type=text], textarea, input[type=file], input[type=password]").count() >= 2:
        return True
    return ACCOUNTS_ENABLED and page.get_by_role("button", name=re.compile(r"apply manually", re.I)).count() > 0


COOKIE_BTN = re.compile(r"^\s*(accept( all)?( cookies)?|allow all( cookies)?|i accept|agree|got it|ok(ay)?|close|dismiss)\s*$", re.I)
OPEN_BTN = re.compile(r"^\s*(apply( now| here| today| online)?( (for|to)( this| the)? (job|position|role|opening|vacancy))?( now)?"
                      r"|start( your)? application|begin application|i'?m interested|apply manually|continue to application)\s*[>\u2192]?\s*$", re.I)
KNOWN_FRAMES = re.compile(r"greenhouse\.io|lever\.co|workable\.com|bamboohr\.com|recruitee\.com|breezy\.hr|smartrecruiters|icims|jobvite|applytojob|"
                          r"paylocity|ultipro|myworkdayjobs|teamtailor|personio|rippling|gusto|comeet|jazzhr|zohorecruit|freshteam", re.I)


def _dismiss_cookies(page):
    try:
        for role in ("button", "link"):
            b = page.get_by_role(role, name=COOKIE_BTN).locator("visible=true")
            if b.count():
                b.first.click(timeout=2000)
                page.wait_for_timeout(400)
                return
    except Exception:
        pass


def _rewrite_ats_url(url: str) -> str | None:
    """Listing URLs that show the description first, where the form lives at a known sub-path."""
    if re.search(r"jobs\.lever\.co/[\w-]+/[0-9a-f-]{36}/?(\?.*)?$", url, re.I):
        return re.sub(r"/?(\?.*)?$", "/apply", url, count=1)
    if re.search(r"apply\.workable\.com/(?:[\w-]+/)?j/\w+/?$", url, re.I):
        return url.rstrip("/") + "/apply/"
    if re.search(r"\.bamboohr\.com/careers/\d+/?$", url, re.I):
        return url  # the page itself carries an Apply button handled below
    return None


def _open_by_button(page) -> bool:
    """Click an Apply-style button/link. A link with a real address is followed in this tab; a popup's address is loaded here too."""
    for role in ("link", "button"):
        loc = page.get_by_role(role, name=OPEN_BTN).locator("visible=true")
        for i in range(min(loc.count(), 3)):
            el = loc.nth(i)
            try:
                href = el.get_attribute("href")          # Workday's Apply is an <a role="button" href=".../apply">
                if href and href.startswith("/") and not href.startswith("//"):
                    from urllib.parse import urljoin
                    href = urljoin(page.url, href)
                if href and href.startswith(("http://", "https://")) and href.split("#")[0] != page.url.split("#")[0]:
                    page.goto(href, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    return True
                if href and href.startswith(("mailto:", "tel:")):
                    continue
                before = page.url
                try:
                    with page.context.expect_page(timeout=3500) as pi:
                        el.click(timeout=5000)
                    newp = pi.value
                    newp.wait_for_load_state("domcontentloaded")
                    target = newp.url
                    newp.close()
                    if target and target not in ("about:blank", before):
                        page.goto(target, wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    page.wait_for_timeout(1200)          # no popup: the click changed this page
                page.wait_for_timeout(2500)
                return True
            except Exception:
                continue
    return False


def _open_embedded(page, url: str) -> bool:
    """A form embedded in an iframe (company career pages embedding Greenhouse, Lever, Workable ...): load the iframe itself."""
    try:
        for fr in page.locator("iframe[src]").all():
            src = fr.get_attribute("src") or ""
            if not src.startswith(("http://", "https://")) or re.search(r"recaptcha|hcaptcha|challenges\.cloudflare|doubleclick|youtube|vimeo|google", src, re.I):
                continue
            if KNOWN_FRAMES.search(src):
                page.goto(src, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                return True
        for f in page.frames[1:]:               # any other frame that itself holds a form
            try:
                if f.url.startswith("http") and f.locator("input[type=file], input[type=email]").count() and \
                        f.locator("input:visible, textarea:visible").count() >= 3 and not re.search(r"recaptcha|hcaptcha|challenges", f.url, re.I):
                    page.goto(f.url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    m = re.search(r"job-boards\.greenhouse\.io/(?:embed/)?([^/?#]+)/jobs/(\d+)", url)
    gh = re.search(r"[?&]gh_jid=(\d+)", page.url)
    board = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)", page.content()[:400000])
    if m and "greenhouse.io" not in page.url:
        page.goto(f"https://job-boards.greenhouse.io/embed/job_app?for={m.group(1)}&token={m.group(2)}", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        return True
    if gh and board and "greenhouse.io" not in page.url:
        page.goto(f"https://job-boards.greenhouse.io/embed/job_app?for={board.group(1)}&token={gh.group(1)}", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        return True
    return False


def open_form(page, url: str) -> str:
    """Open the page and get to the actual application form, whichever way the link is built: the form itself, a description page
    with an Apply button (same tab, new tab, or a link), a form embedded in an iframe, or a listing that needs '/apply' added.
    Returns the text of the page as it first loaded (the posting itself, when the link leads to a description page)."""
    if "myworkdayjobs.com" in url:
        from . import workday
        workday.open_posting(page, url)        # the Workday driver finds its own way from the posting to the form
        return _body_text(page)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    _dismiss_cookies(page)
    landing = _body_text(page)                 # the posting as first shown, before an Apply button leads away from it
    tried = set()
    for _ in range(4):
        if _has_form(page):
            break
        if (b := _blocker(page)):
            raise Blocked(b)
        moved = False
        for name, step in (("rewrite", None), ("button", _open_by_button), ("frame", None)):
            if name in tried:
                continue
            tried.add(name) if name != "button" else None
            try:
                if name == "rewrite":
                    alt = _rewrite_ats_url(page.url)
                    if alt and alt != page.url:
                        page.goto(alt, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2500)
                        moved = True
                elif name == "button":
                    moved = _open_by_button(page)
                else:
                    moved = _open_embedded(page, url)
            except Blocked:
                raise
            except Exception:
                moved = False
            if moved:
                if "myworkdayjobs.com" in page.url:
                    _settle(page)
                _dismiss_cookies(page)
                break
        if not moved:
            break
    if (b := _blocker(page)):
        raise Blocked(b)
    if not _has_form(page):
        try:
            if CLOSED_RX.search(page.inner_text("body")[:3000]):
                raise Blocked("posting closed: the employer's page says the job is no longer available")
        except Blocked:
            raise
        except Exception:
            pass
        raise Blocked("no application form found on page")
    return landing


def _body_text(page, limit: int = 12000) -> str:
    try:
        return page.inner_text("body")[:limit]
    except Exception:
        return ""


PLACEHOLDER = re.compile(r"^[\s\-\u2013\u2014]*((please )?(select|choose|pick)( (one|an? [a-z]+|option|from (the )?list))?|none( selected)?|"
                         r"not selected|no selection)?[\s\-\u2013\u2014.\u2026:]*$", re.I)

_OPTIONS_JS = r"""(ids) => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  // never the 'already selected' pills of a Workday search box, a phone-country list, the site's language menu, or anything hidden
  const bad = e => !!e.closest('[data-automation-id="selectedItemList"], [data-automation-id="selectedItem"], ' +
                               '[data-automation-id="multiselectInputContainer"], [data-automation-id="header"], ' +
                               '[data-automation-id="utilityMenuDropdown"], .iti, [class*="iti__"], [aria-hidden="true"]');
  const pack = els => {
    els = els.filter(e => vis(e) && !bad(e));
    els = els.filter(e => !els.some(o => o !== e && o.contains(e)));          // the outermost match only
    window.__aaOpt = window.__aaOpt || 0;
    return els.slice(0, 150).map(e => {
      const id = 'o' + (window.__aaOpt++); e.setAttribute('data-aa-opt', id);
      return { id, text: ((e.getAttribute('data-automation-label') || e.innerText || '') + '').replace(/\s+/g, ' ').trim() };
    }).filter(o => o.text);
  };
  for (const id of ids) {
    const box = document.getElementById(id);
    if (box) { const got = pack([...box.querySelectorAll('[role="option"]')]); if (got.length) return got; }
  }
  const WD = '[data-automation-id="activeListContainer"] ';
  for (const sel of [WD + '[role="option"], ' + WD + '[data-automation-id="promptOption"], ' + WD + '[data-automation-id="promptLeafNode"]',
                     '[role="listbox"] [role="option"]', '[class*="select__menu"] [role="option"]', '[class*="menu"] [role="option"]',
                     '[data-automation-id="promptOption"], [data-automation-id="promptLeafNode"], [role="option"]', 'ul[role="listbox"] li']) {
    const got = pack([...document.querySelectorAll(sel)]);
    if (got.length) return got;
  }
  return [];
}"""

_SAFE_POINT_JS = """() => {
  const W = innerWidth, H = innerHeight;
  const pts = [[W - 6, H - 6], [6, H - 6], [W - 6, Math.round(H / 2)], [6, Math.round(H / 2)], [Math.round(W / 2), 4], [5, 5]];
  for (const [x, y] of pts) {
    const e = document.elementFromPoint(x, y);
    if (e && !e.closest('a, button, input, select, textarea, label, [role=button], [role=option], [role=listbox], [role=link], [role=menuitem]')) return [x, y];
  }
  return null;
}"""


def _loc(page, f: dict, fid: str | None = None):
    """The element of a field (or of one option of a radio / checkbox group): by this pass's marker when it is still there,
    else by the element's own stable address. Sites like Workday redraw parts of a page while it is being filled in (the
    name and address boxes after the state is picked) and the marker is lost with the old element."""
    fid = fid or f["id"]
    loc = page.locator(f'[data-aa="{fid}"]')
    try:
        if loc.count():
            return loc.first
    except Exception:
        pass
    sel = f.get("sel") if fid == f["id"] else dict(zip(f.get("option_ids") or [], f.get("option_sels") or [])).get(fid)
    if sel:
        try:
            alt = page.locator(sel)
            if alt.count():
                return alt.first
        except Exception:
            pass
    return loc.first


def _options(page, el=None) -> list[dict]:
    """[{'id', 'text'}] for the choices of the list that is open right now; each can be clicked with _click_option."""
    ids = []
    if el is not None:
        try:
            ids = (el.get_attribute("aria-controls", timeout=800) or el.get_attribute("aria-owns", timeout=800) or "").split()
        except Exception:
            ids = []
    try:
        return page.evaluate(_OPTIONS_JS, ids)
    except Exception:
        return []


def _visible_options(page, el=None) -> list[str]:
    """Texts of the options of the dropdown that is open right now (never a hidden phone-country list)."""
    return [o["text"] for o in _options(page, el)][:80]


def _click_option(page, opt: dict) -> bool:
    loc = page.locator(f'[data-aa-opt="{opt["id"]}"]').first
    for kw in ({"timeout": 3000}, {"timeout": 2000, "force": True}):
        try:
            loc.click(**kw)
            return True
        except Exception:
            continue
    try:
        loc.evaluate("e => e.click()")
        return True
    except Exception:
        return False


def _list_open(page) -> bool:
    try:
        return bool(page.evaluate(_OPTIONS_JS, []))
    except Exception:
        return False


def _close_list(page):
    """Shut whatever list is open (Escape; if it stays, a click on an empty part of the page)."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        if _list_open(page):
            pt = page.evaluate(_SAFE_POINT_JS)
            if pt:
                page.mouse.click(pt[0], pt[1])
                page.wait_for_timeout(200)
    except Exception:
        pass


def _already_answered(f: dict) -> bool:
    """The form already holds a value for this field (kept from your candidate profile or an earlier visit)."""
    k = f["kind"]
    if k == "combobox":
        return bool(f.get("cur")) and not PLACEHOLDER.match(f["cur"])
    if k == "wdprompt":
        return bool(f.get("selected"))
    if k in ("radio", "checkbox_group"):
        return bool(f.get("checked"))
    if k == "file":
        return bool(f.get("has_file"))
    return bool(f.get("has_value"))


def extract(page) -> list[dict]:
    fields = page.evaluate(EXTRACT_JS)
    for f in fields:  # comboboxes only reveal options when opened
        if f["kind"] != "combobox":
            continue
        if f.get("button") and _already_answered(f):
            f["options"] = []          # already answered: left shut (opening Workday's Country list redraws the address boxes)
            continue
        try:
            el = _loc(page, f)
            el.click(timeout=3000)
            page.wait_for_timeout(450)
            f["options"] = _visible_options(page, el)
        except Exception:
            f["options"] = []
        _close_list(page)
    return fields


def _norm(s):
    return re.sub(r"\W+", " ", str(s)).strip().lower()


def _has_words(hay: str, needle: str) -> bool:
    """needle occurs in hay as whole words (both already passed through _norm): 'male' is not in 'female', 'no' not in 'now'."""
    return bool(needle) and re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay) is not None


def _match_option(texts: list[str], want) -> int | None:
    """Index of the option that IS the wanted value: the same text; else an option that starts with it ('No' ->
    'No, I will not ...'); else one that contains it as whole words; else one the wanted value starts with ('United
    States' for 'United States of America'). A short value ('No', 'Yes') never matches by sitting somewhere inside a
    longer sentence: a wrong answer on an application is worse than none."""
    w = _norm(want)
    if not w:
        return None
    norm = [_norm(t) for t in texts]
    for i, o in enumerate(norm):
        if o == w:
            return i
    for i, o in enumerate(norm):
        if o.startswith(w + " "):
            return i
    if len(w) > 3:
        for i, o in enumerate(norm):
            if _has_words(o, w):
                return i
    for i, o in enumerate(norm):
        if len(o) > 3 and w.startswith(o + " "):
            return i
    return None


def _pick(options: list[str], want) -> int | None:
    return _match_option(options, want)


def _autocomplete(el, f) -> bool:
    """A location box that wants a suggestion picked (Lever, Greenhouse, most career sites) - everything except a plain
    address-form 'City' box (Workday, BambooHR), which is just typed."""
    if re.match(r"^\W*city\W*\*?\W*$", f.get("label", ""), re.I):
        try:
            return bool(el.evaluate("e => e.getAttribute('role') === 'combobox' || !!e.getAttribute('aria-autocomplete')"))
        except Exception:
            return False
    return True


WD_HEAR_ORDER = [r"job ?board|job ?site|online job|job posting|job search", r"linkedin", r"indeed", r"glassdoor",
                 r"internet|online|website|web ?site|career ?site|careers? (page|website)|company website", r"other"]
WD_HEAR_SEARCH = ["LinkedIn", "Job Board", "Indeed", "Website", "Other"]

_PROMPT_STATE_JS = """e => {
  const box = e.closest('[data-automation-id="multiSelectContainer"]') || e.parentElement;
  if (!box) return [];
  const pills = [...box.querySelectorAll('[data-automation-id="selectedItem"]')].map(x => (x.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean);
  if (pills.length) return pills;
  const note = box.querySelector('[data-automation-id="promptAriaInstruction"]');
  const m = note && /([1-9]\\d*) items? selected(?:,\\s*(.*))?/i.exec(note.innerText || '');
  return m ? [(m[2] || 'selected').trim()] : [];
}"""


_STOP = {"of", "and", "the", "in", "for", "at", "a", "an", "&"}


def _search_terms(val: str) -> list[str]:
    """What to type into a Workday search box for a wanted value: the value itself, then shorter forms of it (lists differ
    between employers: 'Industrial and Systems Engineering' may only exist as 'Industrial Engineering'), then 'Other'."""
    val = (val or "").strip()
    if not val:
        return []
    words = [w for w in re.split(r"[^A-Za-z0-9+#.]+", re.sub(r"\(.*?\)", " ", val)) if w]
    key = [w for w in words if w.lower() not in _STOP]
    out = [val]
    if len(key) >= 2:
        out += [" ".join(key), f"{key[0]} {key[-1]}", " ".join(key[-2:])]
    out.append("Other")
    return list(dict.fromkeys(x for x in out if len(x) >= 3))


def _wd_prompt(page, f: dict, el, val: str, log) -> bool:
    """Workday's search-and-pick box ('How Did You Hear About Us?', 'Field of Study', 'School'). The choices open in a
    pop-up that is separate from the form, sometimes as a list of groups that open into sub-lists, so: read the pop-up
    only (never the 'already selected' chips of this or another box), pick, follow sub-lists, and check the box now shows
    a selection. If browsing finds nothing, search by typing. The pop-up is always shut again before returning."""
    label = f.get("label", "")
    want = _norm(val)
    hear = bool(re.search(r"hear|source|learn about|find (out|us)|referr", label, re.I))
    # a school has to be THE school: 'University of California' is not 'University of Southern California'
    school = bool(re.search(r"school|university|college|institution", label, re.I))

    def chosen() -> list[str]:
        try:
            return _loc(page, f).evaluate(_PROMPT_STATE_JS, timeout=1500)
        except Exception:
            return []

    def match(texts: list[str], seen: set) -> int | None:
        pick = next((i for i, t in enumerate(texts) if _norm(t) == want and t not in seen), None)
        if pick is None and hear:
            for pat in WD_HEAR_ORDER:
                pick = next((i for i, t in enumerate(texts) if re.search(pat, t, re.I) and t not in seen), None)
                if pick is not None:
                    break
        if pick is None and len(want) > 3:            # the wanted text inside a longer entry: '... (USC)', 'LinkedIn Jobs'
            pick = next((i for i, t in enumerate(texts) if t not in seen and _has_words(_norm(t), want)), None)
        if pick is None and want and not hear and not school:
            # a shorter entry that is part of the wanted text: 'Industrial Engineering' for 'Industrial and Systems Engineering'
            ww = set(want.split()) - _STOP
            for i, t in enumerate(texts):
                tw = set(_norm(t).split()) - _STOP
                if t not in seen and len(tw) >= 2 and tw <= ww:
                    pick = i
                    break
        return pick

    def right(h: str) -> bool:
        return _norm(h) == want or (len(want) > 3 and _has_words(_norm(h), want))

    have = chosen()
    if have and (hear or any(right(h) for h in have)):
        return True                                   # already holds an answer (Workday keeps it from your profile)

    def picked() -> bool:
        """The box now holds a choice that this call made (not one that was simply there before). For a school that has
        to be the school itself or 'Other': a look-alike that Workday may have taken on its own does not count."""
        now = chosen()
        if not now:
            return False
        if any(right(h) for h in now):
            return True
        if school:
            return now != have and any(_norm(h) == "other" for h in now)
        return now != have

    def open_list():
        try:
            el.click(timeout=3000)
        except Exception:
            _close_list(page)
            _loc(page, f).click(timeout=3000, force=True)
        for _ in range(5):
            page.wait_for_timeout(350)
            if _options(page):
                return

    ok = False
    try:
        open_list()
        seen: set = set()
        for _ in range(5):                            # browse: a choice, or a group that opens a sub-list
            opts = _options(page)
            texts = [o["text"] for o in opts]
            pick = match(texts, seen)
            if pick is None:
                break
            seen.add(texts[pick])
            _click_option(page, opts[pick])
            page.wait_for_timeout(700)
            if picked():
                ok = True
                break
        if not ok:                                    # search: type it, Enter, take the match
            for term in (WD_HEAR_SEARCH if hear else [val, "Other"] if school else _search_terms(val)):
                box = _loc(page, f)
                try:
                    _close_list(page)                 # the last search's pop-up would swallow the click
                    box.click(timeout=2000)
                    box.fill("", timeout=2000)
                    box.press_sequentially(str(term), delay=40)
                    box.press("Enter")
                except Exception:
                    continue
                page.wait_for_timeout(1400)
                if picked():                          # a single exact match is taken by Workday straight away
                    ok = True
                    break
                opts = _options(page)
                texts = [o["text"] for o in opts]
                w = _norm(term)
                pick = next((i for i, t in enumerate(texts) if _norm(t) == w), None)
                if pick is None:
                    pick = match(texts, set())
                if pick is None and hear:             # any job board will do for 'how did you hear'; nothing else is guessed
                    pick = next((i for i, t in enumerate(texts) if w in _norm(t)), None)
                if pick is not None:
                    _click_option(page, opts[pick])
                    page.wait_for_timeout(700)
                    if picked():
                        ok = True
                        break
    finally:
        _close_list(page)
    if not ok and picked():
        ok = True
    if not ok:
        log(f"      ! could not pick an option in '{label[:50]}' (wanted {str(val)[:40]!r}): left as it was")
    return ok


def _fill_location(page, el, val: str, log):
    """Autocomplete location boxes (Lever etc.) clear themselves unless a suggestion is picked, so pick one."""
    city = val.split(",")[0].strip()
    for attempt in dict.fromkeys([val, city, "Colorado"]):
        el.click()
        el.fill("")
        el.press_sequentially(attempt, delay=60)
        page.wait_for_timeout(1500)
        opt = page.locator('[role="option"], .dropdown-results li, .pac-item, ul[role="listbox"] li, '
                           '[class*="autocomplete"] li, [class*="suggestion"]').filter(visible=True).first
        if opt.count():
            opt.click()
        else:
            el.press("Tab")
        page.wait_for_timeout(500)
        if el.input_value().strip():
            return
    try:        # last resort: set the text directly and fill Lever's hidden 'selectedLocation'
        el.evaluate("""(e, v) => {
            const set = (n, x) => { const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                d.set.call(n, x); n.dispatchEvent(new Event('input', {bubbles: true})); n.dispatchEvent(new Event('change', {bubbles: true})); };
            set(e, v);
            const h = e.form && e.form.querySelector('input[name="selectedLocation"]');
            if (h) set(h, JSON.stringify({name: v}));
        }""", val)
    except Exception:
        pass
    if not el.input_value().strip():
        log("      ! location box stayed empty (site wants a suggestion picked)")


_DATE_READ_JS = """e => {
  const w = e.closest('[data-automation-id="dateInputWrapper"]') || (e.parentElement && e.parentElement.parentElement);
  const g = k => { const x = w && w.querySelector('[data-automation-id="dateSection' + k + '-input"]');
                   return x ? ((x.value || x.getAttribute('aria-valuenow') || '') + '') : null; };
  return { m: g('Month'), d: g('Day'), y: g('Year') };
}"""


def _set_text(el, val: str):
    try:
        if el.input_value(timeout=1500).strip() == val.strip():
            return                                    # already right: typing it again only makes some sites redraw the page
    except Exception:
        pass
    el.fill(val, timeout=5000)


def _set_date(page, f: dict, el, val):
    """Workday's MM / DD / YYYY boxes. Typing the digits into the month box moves along by itself; if the boxes do not
    show the date afterwards, each box is typed on its own."""
    sv = str(val).strip()
    m_iso = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$", sv)                # 2026-11-01, 2026-11
    m_us = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$", sv)             # 11/1/2026
    m_my = re.match(r"^(\d{1,2})[/.\-](\d{4})$", sv)                           # 11/2026
    if m_iso:
        yyyy, mm, dd = m_iso.group(1), m_iso.group(2), m_iso.group(3) or "01"
    elif m_us:
        mm, dd, yyyy = m_us.groups()
    elif m_my:
        mm, yyyy, dd = m_my.group(1), m_my.group(2), "01"
    else:
        digits = re.sub(r"\D", "", sv)
        if len(digits) < 6:
            raise RuntimeError(f"not a date: {val!r}")
        mm, yyyy = digits[:2], digits[-4:]
        dd = digits[2:4] if len(digits) == 8 else "01"
    mm, dd = mm.zfill(2), dd.zfill(2)
    if not (1 <= int(mm) <= 12 and 1 <= int(dd) <= 31):
        raise RuntimeError(f"not a date: {val!r}")
    has_day = bool(f.get("hasDay"))

    def ok() -> bool | None:
        try:
            r = el.evaluate(_DATE_READ_JS, timeout=1500)
        except Exception:
            return None
        if r.get("m") is None or r.get("y") is None:
            return None                               # cannot read the boxes: assume the typing worked, as before
        return (r["m"].lstrip("0") == mm.lstrip("0") and r["y"] == yyyy
                and (not has_day or r.get("d") is None or r["d"].lstrip("0") == dd.lstrip("0")))

    if ok():
        return
    el.focus(timeout=3000)                            # the visible 'MM' label sits on top: focus the box, then type
    page.keyboard.type(mm + (dd if has_day else "") + yyyy, delay=60)
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)
    if ok() is not False:
        return
    wrap = el.locator("xpath=ancestor::*[@data-automation-id='dateInputWrapper'][1]")
    for part, d in (("Month", mm), ("Day", dd if has_day else ""), ("Year", yyyy)):
        box = wrap.locator(f'[data-automation-id="dateSection{part}-input"]')
        if not d or not box.count():
            continue
        box.first.focus(timeout=2000)
        page.keyboard.press("Backspace")
        page.keyboard.type(d, delay=60)
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)
    if ok() is False:
        raise RuntimeError("the date boxes did not take the date")


def _set_dropdown(page, f: dict, el, val, log) -> bool:
    """Pick a value in a dropdown: a Workday button that opens a list, a search-as-you-type box, or a styled select."""
    val = str(val)
    try:
        is_btn = bool(el.evaluate("e => e.tagName === 'BUTTON'", timeout=1500))
    except Exception:
        is_btn = bool(f.get("button"))

    def shown() -> str:
        try:                                          # looked up afresh each time: picking a value can make the site redraw the box
            return str(_loc(page, f).evaluate("e => e.tagName === 'BUTTON' ? e.innerText : (e.value || '')", timeout=1500) or "").strip()
        except Exception:
            return ""

    s0 = shown()
    if val != "@highest" and s0 and not PLACEHOLDER.match(s0) and _match_option([s0], val) is not None:
        return True                                   # already set (Workday presets Country to the United States)
    if _list_open(page):
        _close_list(page)                             # a list left open by the previous box covers this one
    try:
        el.click(timeout=4000)
    except Exception:
        el.scroll_into_view_if_needed(timeout=3000)
        el.click(force=True, timeout=4000)
    page.wait_for_timeout(400)
    opts = _options(page, el)
    if is_btn and not opts:
        page.wait_for_timeout(700)                    # a Workday list whose choices load when opened
        opts = _options(page, el)
    texts = [o["text"] for o in opts]
    known = [o for o in (f.get("options") or []) if not PLACEHOLDER.match(o)]
    i = _best_option(texts, val) if texts else None
    if i is None and val == "@highest":
        pass                                          # no choices could be read: nothing to pick the top of
    elif i is None and (is_btn or known):
        # not among the loaded choices (long lists load as you scroll): type it so the list jumps or filters to it
        if is_btn:
            page.keyboard.type(val, delay=40)
        else:
            el.fill("", timeout=3000)
            el.press_sequentially(val, delay=40)
        page.wait_for_timeout(900)
        opts = _options(page, el)
        texts = [o["text"] for o in opts]
        i = _match_option(texts, val) if texts else None
        if i is None and not is_btn:
            page.keyboard.press("Enter")              # a search box: Enter takes its top suggestion
        # (a list of fixed choices is never answered with 'whatever is highlighted': nothing picked is reported instead)
    elif i is None:                                   # search-as-you-type (places): type, take the first suggestion
        el.fill("", timeout=3000)
        el.press_sequentially(val, delay=50)
        page.wait_for_timeout(1300)
        opts = _options(page, el)
        if opts:
            i = 0
        else:
            page.keyboard.press("Enter")
    if i is not None:
        _click_option(page, opts[i])
        page.wait_for_timeout(250)
    if _list_open(page):
        _close_list(page)
    if is_btn:
        s1 = shown()
        if not s1 or PLACEHOLDER.match(s1):
            log(f"      ! nothing was picked in '{f.get('label', '')[:50]}' (wanted {val[:30]!r}; choices: {texts[:6]})")
            return False
    return True


def _fill_one(page, f: dict, val, files: dict, log):
    kind, fid = f["kind"], f["id"]
    el = _loc(page, f) if not fid.startswith("g_") else None
    workday = str(f.get("key") or "").startswith("formField")
    if kind == "text" and not workday and re.search(r"location|city|where are you (based|located)|currently (live|located)", f.get("label", ""), re.I) \
            and _autocomplete(el, f):
        _fill_location(page, el, str(val), log)
    elif kind == "wddate":
        _set_date(page, f, el, val)
    elif kind == "wdprompt":
        _wd_prompt(page, f, el, str(val), log)
    elif kind in ("text", "textarea", "email", "tel", "url", "number", "date"):
        _set_text(el, str(val))
    elif kind == "select":
        try:
            el.select_option(label=str(val), timeout=4000)
        except Exception:
            opts = el.locator("option").all_inner_texts()
            i = _pick(opts, val)
            if i is not None:
                el.select_option(index=i, timeout=4000)
    elif kind == "combobox":
        _set_dropdown(page, f, el, val, log)
    elif kind == "radio":
        i = _pick(f["options"], val)
        if i is None:
            log(f"      ! no option matched {str(val)[:40]!r} for '{f.get('label','')[:50]}'")
        else:
            _choose(page, f, f["option_ids"][i])
    elif kind == "checkbox_group":
        for v in (val if isinstance(val, list) else [val]):
            i = _pick(f["options"], v)
            if i is None:
                log(f"      ! no option matched {str(v)[:40]!r} for '{f.get('label','')[:50]}'")
            else:
                _choose(page, f, f["option_ids"][i])
    elif kind == "checkbox_single":
        if val is True or str(val).lower() in ("true", "yes"):
            _choose(page, f)
    elif kind == "file":
        path = files.get(str(val).upper())
        if path:
            name = Path(path).name
            if any(name == n or name in n for n in (f.get("has_file") or [])):
                return                                 # the site already holds this file from an earlier visit
            _upload(page, _file_input(page, f), path, f, log)


def fill(page, fields: list[dict], answers: dict, files: dict[str, Path], log=print, deadline: float | None = None) -> list[str]:
    """Fill every answered field. Returns the ids of the fields that could not be filled."""
    by_id = {f["id"]: f for f in fields}
    misses, failed = 0, []
    for fid, val in answers.items():
        f = by_id.get(fid)
        if f is None or val is None or val == "":
            continue
        if deadline and time.time() > deadline:
            raise RuntimeError(f"took longer than {MAX_APPLY_SECONDS // 60} minutes on this site")
        if misses >= 2:                      # something (a list left open, a pop-up) covers the form: clear it, stop waiting long
            _clear_overlays(page)
            if misses >= 4:
                log("      ! the form stopped responding to input on this page; moving on")
                break
        try:
            _fill_one(page, f, val, files, log)
            misses = 0
        except Exception as e:
            failed.append(fid)
            log(f"      ! could not fill '{f.get('label','')[:50]}': {str(e).splitlines()[0]}")
            if "Timeout" in str(e):
                misses += 1
    return failed


def _best_option(texts: list[str], val: str):
    """Index of the option for a wanted value; '@highest' means the top level of a scale (5 - Fluent, Native, Expert)."""
    real = [(i, t) for i, t in enumerate(texts) if t and not re.match(r"^\s*(select one|select|choose|--)", t, re.I)]
    if not real:
        return None
    if val == "@highest":
        for pat in (r"native|bilingual", r"fluent", r"expert|advanced", r"proficient"):
            got = next((i for i, t in real if re.search(pat, t, re.I)), None)
            if got is not None:
                return got
        return real[-1][0]
    j = _match_option([t for _i, t in real], val)
    return real[j][0] if j is not None else None


def _clear_overlays(page):
    """Close whatever is open on top of the form (a dropdown list, a search-and-pick pop-up)."""
    try:
        for _ in range(2):
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        pt = page.evaluate(_SAFE_POINT_JS)
        if pt:
            page.mouse.click(pt[0], pt[1])
        page.wait_for_timeout(300)
    except Exception:
        pass


_CHECKED_JS = "e => !!e.checked || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-pressed') === 'true'"


def _choose(page, f: dict, fid: str | None = None):
    """Select a radio / checkbox option the way a person would, and make sure it took: the box itself when it can be seen,
    else what the site draws in its place (its <label>, a role="radio" wrapper), else a scripted click. Workday and
    Workable hide the real <input> and only react to a click on the label. The element is looked up afresh for every
    step, because ticking a box can make the site redraw the whole question."""
    def loc():
        return _loc(page, f, fid)

    tag = loc().evaluate("e => e.tagName", timeout=4000)
    if tag == "BUTTON":
        loc().click(timeout=4000)
        return

    def on() -> bool:
        try:
            return bool(loc().evaluate(_CHECKED_JS, timeout=1500))
        except Exception:
            return False

    if on():
        return
    seen = loc().evaluate("e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; }", timeout=1500)
    if seen:
        try:
            loc().check(force=True, timeout=2500)
        except Exception:
            pass
        if on():
            return
    marked = loc().evaluate("""e => {
        let w = e.id ? document.querySelector('label[for="' + CSS.escape(e.id) + '"]') : null;
        if (!w) w = e.closest('[role="radio"],[role="checkbox"],label');
        if (!w) return false;
        w.setAttribute('data-aa-click', '1'); return true; }""", timeout=1500)
    if marked:
        try:
            page.locator('[data-aa-click="1"]').first.click(timeout=4000, position={"x": 6, "y": 6})
        except Exception:
            pass
        finally:
            page.evaluate("() => document.querySelectorAll('[data-aa-click]').forEach(x => x.removeAttribute('data-aa-click'))")
        page.wait_for_timeout(200)
        if on():
            return
    try:
        loc().evaluate("e => e.click()", timeout=1500)
    except Exception:
        pass
    page.wait_for_timeout(150)
    if not on():
        loc().check(force=True, timeout=2500)         # raises with the browser's own reason when nothing works


def _file_input(page, f):
    """The input for this file field, even if the site re-drew it after the first upload (the marker attribute is then gone)."""
    loc = page.locator(f'[data-aa="{f["id"]}"]')
    try:
        if loc.count():
            return loc
    except Exception:
        pass
    if f.get("elid"):
        loc = page.locator(f'input[type=file][id="{f["elid"]}"]')
        if loc.count():
            return loc
    if f.get("sel"):
        alt = page.locator(f["sel"])
        try:
            if alt.count():
                return alt.first
        except Exception:
            pass
    return page.locator(f'[data-aa="{f["id"]}"]')


def _file_shown(page, name: str) -> bool:
    try:
        return page.get_by_text(name, exact=False).locator("visible=true").count() > 0
    except Exception:
        return False


def _registered(page, el, name: str) -> bool:
    """The site shows the file name (styled upload widgets), or a plain visible file input holds the file."""
    if _file_shown(page, name):
        return True
    try:
        return bool(el.count()) and el.evaluate(
            "e => !!(e.files && e.files.length) && e.offsetWidth > 30 && e.offsetHeight > 10 && "
            "getComputedStyle(e).opacity !== '0' && getComputedStyle(e).visibility !== 'hidden'")
    except Exception:
        return False


def _via_chooser(page, el, path) -> bool:
    """Open the site's own file picker (its Attach / Upload button, or the input itself) and hand it the file."""
    targets = []
    try:
        iid = el.get_attribute("id") if el.count() else None
        if iid:
            targets.append(page.locator(f'label[for="{iid}"]').locator("visible=true"))
        box = el.locator("xpath=ancestor::*[.//button or .//*[@role='button'] or .//label][1]")
        for role in ("button", "link"):
            targets.append(box.get_by_role(role, name=re.compile(r"^\s*(attach|upload|browse|choose|select|add)\b", re.I)).locator("visible=true"))
    except Exception:
        pass
    try:
        if el.count():
            el.evaluate("e => { e.value = ''; }")       # picking the same file again must still count as a change
    except Exception:
        pass
    for t in targets:
        try:
            if t.count():
                with page.expect_file_chooser(timeout=3000) as fc:
                    t.first.click()
                fc.value.set_files(str(path))
                return True
        except Exception:
            continue
    try:
        with page.expect_file_chooser(timeout=3000) as fc:
            el.evaluate("e => { e.value = ''; e.click(); }")
        fc.value.set_files(str(path))
        return True
    except Exception:
        return False


def _upload(page, el, path, f, log, force_chooser: bool = False):
    """Attach a file and make sure the site really took it. Styled upload boxes (Greenhouse, Lever, Workday) show the file
    name once they have it; if one doesn't, the site's own Attach button is used. A plain form field that simply holds the
    file is left holding it."""
    name = Path(path).name
    if not force_chooser:
        el.set_input_files(str(path))
        for _ in range(6):
            page.wait_for_timeout(500)
            if _registered(page, el, name):
                return True
    ok = _via_chooser(page, el, path)
    if ok:
        for _ in range(6):
            page.wait_for_timeout(500)
            if _registered(page, el, name):
                if not force_chooser:
                    log(f"      (upload for '{f.get('label','')[:40]}' needed the site's own Attach button)")
                return True
    try:                                              # never leave the field emptier than we found it
        if el.count() and not el.evaluate("e => !!(e.files && e.files.length)"):
            el.set_input_files(str(path))
    except Exception:
        pass
    if force_chooser:
        log(f"      ! could not confirm the re-attached file for '{f.get('label','')[:40]}'")
    return False


def _wait_uploads(page, limit_s: int = 20):
    """Uploads go to the site's storage in the background: don't press Submit while one is still running."""
    end = time.time() + limit_s
    rx = re.compile(r"\buploading\b|upload in progress|processing (your )?(file|resume|r\u00e9sum\u00e9)|parsing (your )?(resume|r\u00e9sum\u00e9)", re.I)
    while time.time() < end:
        try:
            if not page.get_by_text(rx).locator("visible=true").count():
                break
        except Exception:
            break
        page.wait_for_timeout(700)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass


def verify(page, fields: list[dict], answers: dict, log=print) -> list[str]:
    """After filling, re-read the page and say which answered fields didn't actually take. Returns their ids."""
    empty = []
    for f in fields:
        val = answers.get(f["id"])
        if val in (None, "") or f["kind"] not in ("text", "textarea", "email", "tel", "url", "number", "select"):
            continue
        try:
            got = _loc(page, f).input_value(timeout=1200)
        except Exception:
            continue
        if not str(got).strip():
            empty.append(f["id"])
            log(f"      ! NOT FILLED: '{f.get('label','')[:60]}' (wanted {str(val)[:40]!r})")
    return empty


NEXT_RX = re.compile(r"^\s*(next|continue|save\s*(and|&)\s*(continue|next)|next step|review( (and|&) submit| application)?|proceed|"
                     r"continue to .{2,40}|go to (next|review).{0,20})\s*[>\u2192]?\s*$", re.I)
FINAL_RX = re.compile(r"^\s*(submit( (your )?application)?|send( my)? application|finish|complete( application)?|apply( now)?)\s*[>\u2192]?\s*$", re.I)
LANDING_RX = re.compile(r"^\s*(apply manually|start (your )?application|apply( now)?|continue application|"
                        r"(sign in|continue|apply|sign up) with (e-?mail|your e-?mail)( address)?)\s*$", re.I)
CLOSED_RX = re.compile(r"page you are looking for (doesn.t|does not) exist|(job|position|posting|requisition) (is )?(no longer|not) (available|open|accepting)|"
                       r"no longer accepting applications|this job (has been|was) (closed|filled|removed)|job not found|position has been filled", re.I)
MAX_STEPS = 32


def _find_advance(page):
    """(button, 'submit'|'next') for the visible primary action of the current step, or (None, None)."""
    # Workday's footer button: one button whose text is Next / Save and Continue, or Submit on the Review page
    for aid in ("bottom-navigation-next-button", "pageFooterNextButton"):
        wd = page.locator(f'button[data-automation-id="{aid}"]').locator("visible=true")
        try:
            if wd.count():
                txt = (wd.first.inner_text() or "").strip()
                return wd.first, ("submit" if re.search(r"submit", txt, re.I) else "next")
        except Exception:
            pass
    loc = page.locator("button, input[type=submit], input[type=button], [role=button]").locator("visible=true")
    nxt = final = None
    for i in range(min(loc.count(), 200)):
        el = loc.nth(i)
        try:
            txt = (el.inner_text() or el.get_attribute("value") or el.get_attribute("aria-label") or "").strip()
        except Exception:
            continue
        if not txt or len(txt) > 45:
            continue
        if FINAL_RX.match(txt):
            final = el
        elif NEXT_RX.match(txt):
            nxt = el
    if final is not None:
        return final, "submit"
    if nxt is not None:
        return nxt, "next"
    return None, None


def _page_errors(page) -> list[str]:
    try:
        errs = page.locator('[data-automation-id="errorMessage"], [data-automation-id="inputError"], [data-automation-id*="rror" i], '
                            '[aria-invalid="true"], [role="alert"], .error, .field-error, [class*="error"]').locator("visible=true").all_inner_texts()
    except Exception:
        return []
    return [e.strip()[:80] for e in errs if e.strip()][:4]


def _invalid_labels(page) -> str:
    """Labels of the fields the site flagged as wrong or missing (so the log says what to fix)."""
    try:
        out = page.evaluate("""() => {
          const els = [...document.querySelectorAll('[aria-invalid="true"], [data-automation-id*="rror" i]')];
          const names = els.map(el => {
            const box = el.closest('[data-automation-id^="formField"], fieldset, .field, [role=group]') || el.parentElement;
            const lab = box && (box.querySelector('label, legend') || {}).innerText;
            return ((el.getAttribute('aria-label') || lab || el.innerText || '') + '').trim().slice(0, 60);
          }).filter(Boolean);
          return [...new Set(names)].slice(0, 6);
        }""")
        return "; ".join(out)
    except Exception:
        return ""


def _visible_buttons(page) -> str:
    """The first visible buttons/links on a page, to explain in the log why the bot could not go on."""
    try:
        txt = page.locator("button, a[role=button], [data-automation-id]").locator("visible=true").evaluate_all(
            "els => els.slice(0, 40).map(e => (e.getAttribute('data-automation-id') || '') + ':' + (e.innerText || '').trim().slice(0, 30))"
            ".filter(t => t.length > 2)")
        return "; ".join(dict.fromkeys(txt))[:300]
    except Exception:
        return "?"


def _submits_a_form(btn) -> bool:
    """The button is the submit control of a <form> that holds fields (as opposed to an 'Apply' button on a description
    page, which only opens the form). When it cannot be told, it is treated as a real submit button."""
    try:
        return bool(btn.evaluate("""e => {
            const f = e.closest('form');
            if (!f) return false;
            const t = (e.getAttribute('type') || (e.tagName === 'BUTTON' ? 'submit' : '')).toLowerCase();
            const n = [...f.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select')]
                        .filter(x => x.offsetWidth > 0 || x.type === 'file').length;
            return (t === 'submit' || e.tagName === 'INPUT') && n >= 2; }""", timeout=1500))
    except Exception:
        return True


def _form_ready(fields) -> bool:
    return len(fields) >= 3 or any(f["kind"] in ("file", "password", "email") for f in fields)


def _to_form(page, fields, accounts, log):
    """From a description page / 'Start your application' page to the form itself. Run before the account step and
    again after it (some sites drop you back on the posting once you have signed in)."""
    from . import auth
    for _k in range(3):
        if _form_ready(fields) or not _landing(page):
            break
        _settle(page)
        if accounts and accounts.enabled and auth.is_auth_page(page):
            return extract(page)                   # the caller signs in, then calls this again
        fields = extract(page)
    return fields


def _landing(page) -> bool:
    """Click 'Apply Manually' / 'Apply' style buttons on a page that has no form yet."""
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=LANDING_RX).locator("visible=true")
        if loc.count():
            loc.first.click(force=True)
            page.wait_for_timeout(2000)
            return True
    return False



_REJECT_RX = re.compile(r"required|invalid|please (enter|select|provide|upload|choose|attach|complete|fill)|must |missing|"
                        r"can.t be blank|cannot be blank|is not valid", re.I)


def _rejected(page, before_url, btn):
    """Raise NotSubmitted when the form is plainly still there, unsent, with 'X is required'-style errors on it."""
    errs = _page_errors(page)
    if errs and page.url == before_url and not _few_inputs(page) and _still_visible(btn) and _REJECT_RX.search(" ".join(errs)) \
            and not SUCCESS_RE.search(page.inner_text("body")):
        raise NotSubmitted(f"the form was rejected, not sent; page errors: {errs}")


def _still_visible(btn) -> bool:
    try:
        return btn is not None and btn.count() > 0 and btn.first.is_visible()
    except Exception:
        return False


_DONE_URL = re.compile(r"thank|success|submitted|confirmation", re.I)
_HUMAN_WORDS = re.compile(r"captcha|re?captcha|hcaptcha|turnstile|prove (you'?re|you are) human|are you a robot|human verification|"
                          r"cloudflare.{0,30}(challenge|verify)", re.I)


def submit(page, timeout_ms: int = 20000, btn=None, on_click=None, mail_hint: str = "", log=print) -> str:
    from . import auth
    before_url = page.url
    before_text = page.inner_text("body")
    before_hits = len(SUCCESS_RE.findall(before_text))
    # words that are already there before the click prove nothing afterwards: a job called 'Customer Success Associate'
    # has 'success' in its address, and most forms say 'protected by reCAPTCHA' in their small print
    url_words_before = {w.lower() for w in _DONE_URL.findall(before_url)}
    human_words_before = bool(_HUMAN_WORDS.search(" ".join(before_text.split())))
    site_host = _host(before_url)
    if btn is None:
        btn = page.locator("button[type=submit], input[type=submit]").filter(visible=True).last
        if not btn.count():
            btn = page.get_by_role("button", name=re.compile(r"submit|send application|apply", re.I)).last
    # Can the button be pressed at all? Checked without pressing it, so a button that is covered or disabled ends as
    # "not sent" (tried again later) and not as "clicked, outcome unknown" (never tried again).
    force = False
    try:
        btn.click(trial=True, timeout=8000)
    except Exception as ex:
        if not _own_cover(btn):
            raise NotSubmitted(f"the Submit button could not be pressed ({str(ex).splitlines()[0][:110]}): nothing was sent")
        force = True                     # Workday's own invisible click-catcher lies over the button: a click there is the click
    t_click = time.time()
    replies: list = []

    def _on_resp(resp):
        try:
            rq = resp.request
            if rq.method != "GET" and rq.resource_type in ("xhr", "fetch", "document") and not re.search(
                    r"google|analytics|segment|sentry|datadog|hotjar|clarity|doubleclick|facebook|linkedin\.com/px|bat\.bing", rq.url, re.I):
                body = ""
                try:
                    body = " ".join(resp.text().split())[:300]
                except Exception:
                    pass
                replies.append((rq.method, resp.status, rq.url.split("?")[0][-90:], body, _host(rq.url)))
        except Exception:
            pass
    try:
        page.on("response", _on_resp)
    except Exception:
        pass
    if on_click:
        on_click()                       # write-ahead: from here on, a crash or timeout must never lead to a second submit
    btn.click(force=True) if force else btn.click()
    waited = 0
    code_tries = 0
    try:
        while waited < timeout_ms:
            page.wait_for_timeout(1000)
            waited += 1000
            body = page.inner_text("body")
            url_says_done = page.url != before_url and bool({w.lower() for w in _DONE_URL.findall(page.url)} - url_words_before)
            if len(SUCCESS_RE.findall(body)) > before_hits or url_says_done:
                return "confirmed"
            if waited >= 2000 and _code_after_submit(page, body):
                # Not a step of the form: the site answered the Submit click by asking for a code it emailed. Waiting here
                # used to end as "submitted but not confirmed", which was wrong (nothing was sent) and cost two minutes.
                if code_tries or not auth.mailbox.configured():
                    raise NotSubmitted("after Submit the site asked for an emailed code and it was not accepted (or the inbox could not be read); "
                                       "nothing was sent: tried again next run")
                code_tries += 1
                log("      email: the site asked for a security code after Submit; reading it from the application inbox")
                res = auth.mailbox.wait_for_verification(since_ts=t_click - 5, host_hint=mail_hint, timeout=180, log=log,
                                                         require_code=True, site_host=site_host)
                boxes = auth._code_inputs(page) if res and res.get("code") else []
                if not boxes:
                    raise NotSubmitted("after Submit the site asked for an emailed code that did not arrive in the inbox in time; nothing was sent: tried again next run")
                auth._type_code(page, boxes, res["code"])
                page.wait_for_timeout(3000)
                if len(SUCCESS_RE.findall(page.inner_text("body"))) <= before_hits and not _code_after_submit(page, page.inner_text("body")) \
                        and _still_visible(btn):
                    log("      email: code accepted; pressing Submit again as the site asks")
                    try:
                        btn.click(force=force)
                    except Exception:
                        pass
                waited = 0                          # the code is in: wait again for the confirmation
                continue
            body_l = " ".join(body.split())
            if not human_words_before and _HUMAN_WORDS.search(body_l) and not _email_code_prompt(page):
                raise Blocked("the submit is held by an anti-bot/human verification challenge")
            if (b := _blocker(page)):
                raise Blocked(f"{b} after submit")
            if waited >= 6000 and waited % 3000 == 0:
                _rejected(page, before_url, btn)          # the form bounced the submit with 'X is required': stop waiting
        _rejected(page, before_url, btn)
        errs = _page_errors(page)
        tail = " ".join(page.inner_text("body").split())[-220:]
        said = _server_said(replies, site_host)
        if replies and all(_cf_challenge(r[2]) for r in replies):
            raise Blocked("Cloudflare's human check held the submit (the form only talked to the check, not the employer)")
        if said.startswith("REJECTED"):
            raise NotSubmitted(f"the site's server refused the application: {said[9:]}")
        if not replies and re.search(r"submitting|sending|please wait|processing", tail, re.I):
            raise Blocked("the form stayed on 'Submitting…' and never contacted the employer (an invisible human check held it)")
        msg = "no confirmation after submit" + (f"; page errors: {errs}" if errs else "") + (f"; server said: {said}" if said else "") + f"; page ends: {tail!r}"
    except (Blocked, Unconfirmed, NotSubmitted):
        raise
    except Exception as ex:              # page closed / navigated away after the click: the submit may have gone through
        msg = f"no confirmation after submit (page error after click: {str(ex).splitlines()[0][:120]})"
    finally:
        try:
            page.remove_listener("response", _on_resp)
        except Exception:
            pass
    e = Unconfirmed(msg)
    e.t0 = t_click
    raise e


def _own_cover(btn) -> bool:
    """Workday lays an invisible click-catcher ('click_filter') over some of its buttons. That is the button's own
    cover, not a pop-up in the way: a click on it presses the button."""
    try:
        return bool(btn.evaluate("""e => {
            const r = e.getBoundingClientRect();
            const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return !!t && t !== e && !e.contains(t) && t.getAttribute('data-automation-id') === 'click_filter'
                   && !!e.parentElement && e.parentElement.contains(t); }""", timeout=1500))
    except Exception:
        return False


def _cf_challenge(url: str) -> bool:
    """Cloudflare Turnstile / challenge-platform traffic ('/cdn-cgi/challenge-platform/...', ids like '...-1.2.1.1-...')."""
    return bool(re.search(r"cdn-cgi/challenge|challenges\.cloudflare|turnstile|-\d\.\d\.\d\.\d-", url or "", re.I))


def _host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _server_said(replies, site_host: str = "") -> str:
    """What the site's own server answered to the submit. 'REJECTED ...' only when it refused (a 4xx with a reason) and
    accepted nothing else: if any other request to the same site went through after the click, the application may have
    been sent, and it must then never be treated as 'not sent' (which would let it be submitted again)."""
    if not replies:
        return ""

    def own(r) -> bool:                               # the form's own site, not a tracker or widget on another one
        h = r[4] if len(r) > 4 else ""
        return not site_host or not h or h == site_host or h.split(".")[-2:] == site_host.split(".")[-2:]
    mine = [r for r in replies if own(r)]
    bad = [r for r in mine if 400 <= r[1] < 500 and r[1] not in (401, 403, 404)]
    took = [r for r in mine if 200 <= r[1] < 400]
    if bad:
        m, st, url, body = bad[-1][:4]
        if re.search(r"captcha|recaptcha|hcaptcha|turnstile|bot|challenge", body, re.I):
            return f"{st} human check: {body[:160]}"
        if not took:
            return f"REJECTED {m} {url} -> {st}: {body[:220]}"
        return f"{m} {url} -> {st} (but another request to the site was accepted): {body[:120]}"
    return "; ".join(f"{r[0]} {r[2]} -> {r[1]}" for r in replies[-3:])


def _settle(page, max_ms: int = 9000):
    """Wait until a page that draws itself with JavaScript (Workday) stops adding fields: the same number of inputs and
    buttons twice in a row."""
    try:
        page.wait_for_load_state("networkidle", timeout=min(max_ms, 6000))
    except Exception:
        pass
    last, same, waited = -1, 0, 0
    while waited < max_ms:
        try:
            n = page.locator("input, textarea, select, button").locator("visible=true").count()
        except Exception:
            n = -2
        if n == last and n > 0:
            same += 1
            if same >= 2:
                return
        else:
            same = 0
        last = n
        page.wait_for_timeout(500)
        waited += 500


def apply(page, job, brain, cover_letter: str, files: dict[str, Path], shot: Path, dry_run: bool, log=print, accounts=None,
          on_click=None, on_stage=None) -> str:
    """Expects open_form(page, job.apply_url) to have been called already. Workday applications go to the Workday driver;
    everything else is handled by the generic loop (one-page forms and multi-step wizards, with account creation /
    sign-in when the site needs it). on_stage(name) is told each stage reached, so a failure records where it stopped."""
    from . import workday, writer as _w
    if workday.is_workday(page):
        _w.DEADLINE[0] = time.time() + MAX_APPLY_SECONDS - 45
        return workday.apply(page, job, brain, cover_letter, files, shot, dry_run, log, accounts, on_click, on_stage)
    return _apply_generic(page, job, brain, cover_letter, files, shot, dry_run, log, accounts, on_click, on_stage)


def _apply_generic(page, job, brain, cover_letter: str, files: dict[str, Path], shot: Path, dry_run: bool, log=print, accounts=None,
                   on_click=None, on_stage=None) -> str:
    from . import auth, workday, writer as _w
    start_url = page.url
    total, uploaded, prev_sig, stuck, answered, code_tries, opened = 0, False, None, 0, {}, 0, False
    # Correlate inbox codes with the current application action, not just account creation.
    mail_since = time.time() - 45
    limit_at = time.time() + MAX_APPLY_SECONDS
    _w.DEADLINE[0] = limit_at - 45            # the writer never makes the whole application run out of time
    for step in range(1, MAX_STEPS + 1):
        if time.time() > limit_at:
            raise RuntimeError(f"took longer than {MAX_APPLY_SECONDS // 60} minutes on this site")
        page.wait_for_timeout(600)
        _settle(page)
        if workday.is_workday(page):               # an employer's own career page that hands over to Workday part-way
            _w.DEADLINE[0] = limit_at - 45
            return workday.apply(page, job, brain, cover_letter, files, shot, dry_run, log, accounts, on_click, on_stage)
        if step > 1 and SUCCESS_RE.search(page.inner_text("body")) and _few_inputs(page):
            return "confirmed"
        if (b := _blocker(page)):
            raise Blocked(b)
        if on_stage:
            on_stage(f"page {step}")
        # Any wizard step may request a code sent to the applicant's inbox.
        if _email_code_prompt(page):
            if not auth.mailbox.configured():
                raise Blocked("email verification code requested, but the application inbox is not configured")
            try:
                log("      email: application requested a verification code; checking the inbox")
                res = auth.mailbox.wait_for_verification(
                    since_ts=mail_since,
                    host_hint="|".join(x for x in (getattr(job, "company", ""), (getattr(job, "extra", None) or {}).get("company_name", "")) if x),
                    timeout=min(120, max(30, int(limit_at - time.time()))),
                    log=log,
                    require_code=True,
                )
                if not res or not res.get("code"):
                    raise Blocked("email verification code did not arrive in time")
                boxes = auth._code_inputs(page)
                if not boxes:
                    raise Blocked("received an email verification code but found no code input on the application page")
                log("      email: typing the application verification code")
                auth._type_code(page, boxes, res["code"])
                page.wait_for_timeout(1000)
                _settle(page)
                code_tries += 1
                if code_tries > 2:
                    raise Blocked("the emailed verification code was not accepted")
                mail_since = time.time() - 5
                if SUCCESS_RE.search(page.inner_text("body")) and _few_inputs(page):
                    return "confirmed"
                continue
            except Blocked:
                raise
            except Exception as e:
                raise Blocked(f"email verification code could not be completed: {str(e)[:160]}")

        if accounts and accounts.enabled and auth.is_auth_page(page):
            if dry_run and not accounts.create_in_dry_run:
                page.screenshot(path=str(shot), full_page=True)
                log("      (dry run stops at the account screen; a live run creates/signs in here)")
                return "dry_run"
            try:
                auth.handle(page, accounts, log, url_after=start_url)
            except auth.AuthBlocked as e:
                raise Blocked(f"account: {e}")
            page.wait_for_timeout(1200)
            if (b := _blocker(page)):
                raise Blocked(b)
        fields = extract(page)
        if len(fields) < 2:                                # still drawing (Workday) or a description page with an Apply button
            page.wait_for_timeout(2500)
            _settle(page)
            fields = extract(page)
        if step == 1 or not _form_ready(fields):
            fields = _to_form(page, fields, accounts, log)
        if accounts and accounts.enabled and any(f["kind"] == "password" for f in fields):
            log("      (account page: creating the account / signing in)")
            if dry_run and not accounts.create_in_dry_run:
                page.screenshot(path=str(shot), full_page=True)
                return "dry_run"
            try:
                auth.handle(page, accounts, log, url_after=start_url)
                page.wait_for_timeout(1500)
                _settle(page)
                if auth.is_auth_page(page):          # just created: Workday wants the new account signed into (or verified)
                    auth.handle(page, accounts, log, url_after=start_url)
            except auth.AuthBlocked as e:
                raise Blocked(f"account: {e}")
            page.wait_for_timeout(1200)
            _settle(page)
            if (b := _blocker(page)):
                raise Blocked(b)
            fields = extract(page)
            if not _form_ready(fields):              # signed in, but back on the posting: go to the form again
                fields = _to_form(page, fields, accounts, log)
        if any(f["kind"] == "password" for f in fields):
            raise Blocked("account: the site needs an account and accounts are off (no ACCOUNT_PASSWORD) or sign-in did not finish")

        sig = tuple(sorted(f.get("label", "") for f in fields))
        stuck = stuck + 1 if sig == prev_sig else 0
        if stuck >= 2:
            raise Unanswerable(f"stuck on step {step}; page says: {_page_errors(page) or 'nothing'}; "
                               f"fields flagged: {_invalid_labels(page) or 'none'}; buttons: {_visible_buttons(page)[:160]}")
        prev_sig = sig
        log(f"      step {step}: {len(fields)} fields ({sum(f['required'] for f in fields)} required)")
        total += len(fields)
        plan = brain.map_fields(job, fields, cover_letter)
        missing = [by["label"][:60] for by in fields if by["id"] in set(plan["unanswerable_required"])]
        if missing:
            for f in fields:
                if f["id"] in set(plan["unanswerable_required"]):
                    log(f"      ? unanswered: {f['label'][:90]!r} kind={f['kind']} options={[o[:30] for o in (f.get('options') or [])][:6]}")
            raise Unanswerable("can't truthfully answer required: " + "; ".join(missing))
        if "COVER_LETTER" in plan["answers"].values() and "COVER_LETTER" not in files:
            need = [f for f in fields if plan["answers"].get(f["id"]) == "COVER_LETTER" and f.get("required")]
            letter_txt = brain.lazy_letter(job, required=bool(need))   # a full one-page letter, written only when a form has a cover-letter upload
            if letter_txt:
                (shot.parent / "cover_letter.txt").write_text(letter_txt)
                files["COVER_LETTER"] = files["_LETTER_MAKER"](letter_txt)
            else:
                if need:
                    raise Unanswerable("form requires a cover letter, the writer couldn't write one and there is no cover_letter_template")
                plan["answers"] = {k: v for k, v in plan["answers"].items() if v != "COVER_LETTER"}   # optional: apply without one
                log("      (no cover letter could be written; it is optional, applying with the résumé only)")
        fill(page, fields, plan["answers"], files, log, deadline=limit_at)
        if any(f["kind"] == "file" and plan["answers"].get(f["id"]) for f in fields):
            _wait_uploads(page)
        verify(page, fields, plan["answers"], log)
        uploaded = uploaded or any(f["kind"] == "file" for f in fields)
        btn, kind = _find_advance(page)
        page.screenshot(path=str(shot if step == 1 else shot.with_name(f"step{step}.png")), full_page=True)
        if kind is None:
            if total < 4 or not uploaded:
                raise Blocked(f"not a real application form ({total} fields, no résumé upload); page shows: {_visible_buttons(page)}")
            raise Blocked(f"no Next or Submit button found on this step; page shows: {_visible_buttons(page)}")
        n_filled = sum(1 for v in plan["answers"].values() if v not in (None, ""))
        if kind == "submit" and (total < 4 or not uploaded) and re.match(r"^\s*apply\b", (btn.inner_text() or ""), re.I) and step < 4 \
                and not opened and n_filled < 2 and not _submits_a_form(btn):
            # an 'Apply' button on a description page that only opens the form. Never taken for a button that submits the
            # fields just filled in: that would send a tiny application with no record of the click.
            log("      (that 'Apply' button opens the form; clicking it)")
            opened = True
            btn.click(force=True)
            page.wait_for_timeout(2500)
            total = 0
            prev_sig = None
            continue
        if kind == "submit":
            if total < 4 or not uploaded:
                raise Blocked(f"not a real application form ({total} fields, no résumé upload); page shows: {_visible_buttons(page)}")
            page.screenshot(path=str(shot), full_page=True)
            if dry_run:
                return "dry_run"
            try:
                mail_since = time.time() - 5
                try:
                    result = submit(page, btn=btn, on_click=on_click, mail_hint="|".join(x for x in (getattr(job, "company", ""), (getattr(job, "extra", None) or {}).get("company_name", "")) if x), log=log)
                except NotSubmitted as e:
                    res_fields = [f for f in fields if plan["answers"].get(f["id"]) == "RESUME"]
                    if not (res_fields and re.search(r"resume|r\u00e9sum\u00e9|\bcv\b|attach|upload|\bfiles?\b", str(e), re.I)):
                        raise
                    # the form says the résumé is missing, so nothing was sent: attach it with the site's own button, submit once more
                    log("      the form says the résumé is missing: attaching it again with the site's own button and submitting once more")
                    page.screenshot(path=str(shot.with_name("resume_missing.png")), full_page=True)
                    for f in res_fields:
                        _upload(page, _file_input(page, f), files["RESUME"], f, log, force_chooser=True)
                    _wait_uploads(page)
                    btn2, kind2 = _find_advance(page)
                    if kind2 != "submit":
                        raise
                    result = submit(page, btn=btn2, on_click=on_click, mail_hint="|".join(x for x in (getattr(job, "company", ""), (getattr(job, "extra", None) or {}).get("company_name", "")) if x), log=log)
            except (Unconfirmed, NotSubmitted):
                page.screenshot(path=str(shot.with_name("after_submit.png")), full_page=True)
                raise
            page.screenshot(path=str(shot.with_name("confirmation.png")), full_page=True)
            return result
        mail_since = time.time() - 5                  # this click may trigger an email OTP on the next step
        btn.click(force=True)                                 # Next / Save and Continue
        page.wait_for_timeout(1500)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    raise Blocked(f"more than {MAX_STEPS} steps; giving up")
