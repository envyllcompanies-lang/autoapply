"""Generic application-form driver: find the form, read every field, answer from your facts, fill, submit, verify."""
from __future__ import annotations

import re
import time
from pathlib import Path

EXTRACT_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 400);
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
      let p = el.parentElement;
      for (let i = 0; i < 4 && p && !t; i++, p = p.parentElement) {
        const l = p.querySelector('label, legend');
        if (l && !l.contains(el)) t = l.innerText;
      }
    }
    return clean(t || el.placeholder || el.name || '');
  };
  const questionOf = (members) => {           // text of the smallest container holding a whole radio/checkbox group
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

  let n = window.__aa || 0; const tag = el => { if (!el.dataset.aa) el.dataset.aa = 'f' + (n++); window.__aa = n; return el.dataset.aa; };
  const fields = [], groups = {};
  const els = document.querySelectorAll('input, textarea, select');
  for (const el of els) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (el.disabled || el.name === 'g-recaptcha-response' || el.closest('[aria-hidden="true"]')) continue;
    if (!visible(el)) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio' || type === 'checkbox') {
      const g = el.name || tag(el);
      (groups[g] = groups[g] || { type, members: [], required: false }).members.push(el);
      groups[g].required ||= required;
      continue;
    }
    const f = { id: tag(el), required, label: labelOf(el), maxlength: (el.maxLength > 0 && el.maxLength < 100000) ? el.maxLength : null };
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') f.kind = 'combobox';
    else if (el.tagName === 'SELECT') {
      f.kind = 'select';
      f.options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--)/i.test(t));
    } else if (el.tagName === 'TEXTAREA') f.kind = 'textarea';
    else if (type === 'file') {
      f.kind = 'file'; f.accept = el.accept || ''; f.label = fileLabelOf(el);
      f.hint = [el.id, el.name, el.getAttribute('data-qa'), el.getAttribute('data-testid'), el.getAttribute('data-automation-id')].filter(Boolean).join(' ');
      f.elid = el.id || '';
    }
    else f.kind = ['email', 'tel', 'url', 'number', 'date'].includes(type) ? type : 'text';
    if (/\*/.test(f.label)) f.required = true;
    fields.push(f);
  }
  for (const [name, g] of Object.entries(groups)) {
    const opts = g.members.map(m => ({ id: tag(m), label: labelOf(m) || m.value }));
    const q = questionOf(g.members);
    if (g.type === 'checkbox' && g.members.length === 1)
      fields.push({ id: opts[0].id, kind: 'checkbox_single', label: opts[0].label, question: q, required: g.required });
    else
      fields.push({ id: 'g_' + opts[0].id, kind: g.type === 'radio' ? 'radio' : 'checkbox_group',
                    label: q || name, required: g.required || /\*/.test(q),
                    options: opts.map(o => o.label), option_ids: opts.map(o => o.id) });
  }
  // custom dropdowns drawn as buttons (Workday)
  for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
    if (b.disabled || b.closest('[aria-hidden="true"]') || !visible(b)) continue;
    const lab = labelOf(b);
    fields.push({ id: tag(b), kind: 'combobox', label: lab, required: /\*/.test(lab) || b.getAttribute('aria-required') === 'true', maxlength: null });
  }
  // Ashby-style Yes/No answered with two plain <button>s instead of radio inputs
  const btnGroups = new Map();
  for (const b of document.querySelectorAll('button')) {
    if ((b.getAttribute('type') || '') === 'submit' || b.disabled || b.closest('[aria-hidden="true"]') || !visible(b)) continue;
    if (!/^(yes|no)$/i.test((b.innerText || '').trim())) continue;
    const par = b.parentElement;
    if (!btnGroups.has(par)) btnGroups.set(par, []);
    btnGroups.get(par).push(b);
  }
  for (const [par, bs] of btnGroups) {
    if (bs.length !== 2) continue;
    let q = '', c = par;
    for (let i = 0; i < 4 && c && !q; i++, c = c.parentElement) {
      const txt = clean(c.innerText).replace(/\s*Yes\s*No\s*$/i, '').trim();
      if (txt.length > 3) q = txt;
    }
    const ord = bs.map(b => ({ id: tag(b), label: clean(b.innerText) }));
    fields.push({ id: 'g_' + ord[0].id, kind: 'radio', label: q, required: /\*/.test(q),
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


UNSUPPORTED_ALWAYS = re.compile(r"linkedin\.com|indeed\.com|glassdoor\.com|ziprecruiter\.com|ashbyhq\.com|smartrecruiters\.com/oneclick", re.I)
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
    for sel, name in _blockers():
        for el in page.locator(sel).all():
            try:
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box["width"] > 30 and box["height"] > 30:
                        return name
            except Exception:
                pass
    return None


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
OPEN_BTN = re.compile(r"^\s*(apply( now| here| today| online| for (this|the) (job|position|role)| to (this|the) (job|position|role))?"
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
                href = el.get_attribute("href") if role == "link" else None
                if href and href.startswith(("http://", "https://")):
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


def open_form(page, url: str):
    """Open the page and get to the actual application form, whichever way the link is built: the form itself, a description page
    with an Apply button (same tab, new tab, or a link), a form embedded in an iframe, or a listing that needs '/apply' added."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    _dismiss_cookies(page)
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
                _dismiss_cookies(page)
                break
        if not moved:
            break
    if (b := _blocker(page)):
        raise Blocked(b)
    if not _has_form(page):
        raise Blocked("no application form found on page")


_OPT_SELECTORS = ('[role="listbox"] [role="option"]', '[class*="select__menu"] [role="option"]',
                  '[class*="menu"] [role="option"]', '[role="option"]', 'ul[role="listbox"] li')


def _visible_options(page, el=None) -> list[str]:
    """Texts of the options of the dropdown that is open right now (never a hidden phone-country list)."""
    ids = []
    if el is not None:
        ids = (el.get_attribute("aria-controls") or el.get_attribute("aria-owns") or "").split()
    for i in ids:
        got = [t.strip() for t in page.locator(f'[id="{i}"] [role="option"]').locator("visible=true").all_inner_texts() if t.strip()]
        if got:
            return got[:80]
    for sel in _OPT_SELECTORS:
        got = [t.strip() for t in page.locator(sel).locator("visible=true").all_inner_texts() if t.strip()]
        if got:
            return got[:80]
    return []


def extract(page) -> list[dict]:
    fields = page.evaluate(EXTRACT_JS)
    for f in fields:  # comboboxes only reveal options when opened
        if f["kind"] == "combobox":
            try:
                el = page.locator(f'[data-aa="{f["id"]}"]')
                el.click()
                page.wait_for_timeout(500)
                f["options"] = _visible_options(page, el)
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception:
                f["options"] = []
    return fields


def _norm(s):
    return re.sub(r"\W+", " ", str(s)).strip().lower()


def _pick(options: list[str], want) -> int | None:
    w = _norm(want)
    for i, o in enumerate(options):
        if _norm(o) == w:
            return i
    for i, o in enumerate(options):
        if w and (w in _norm(o) or _norm(o) in w):
            return i
    return None


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


def fill(page, fields: list[dict], answers: dict, files: dict[str, Path], log=print, deadline: float | None = None):
    by_id = {f["id"]: f for f in fields}
    for fid, val in answers.items():
        f = by_id.get(fid)
        if f is None or val is None or val == "":
            continue
        if deadline and time.time() > deadline:
            raise RuntimeError(f"took longer than {MAX_APPLY_SECONDS // 60} minutes on this site")
        try:
            kind = f["kind"]
            el = page.locator(f'[data-aa="{fid}"]') if not fid.startswith("g_") else None
            if kind == "text" and re.search(r"location|city|where are you (based|located)|currently (live|located)", f.get("label", ""), re.I):
                _fill_location(page, el, str(val), log)
            elif kind in ("text", "textarea", "email", "tel", "url", "number", "date"):
                el.fill(str(val))
            elif kind == "select":
                try:
                    el.select_option(label=str(val))
                except Exception:
                    opts = el.locator("option").all_inner_texts()
                    i = _pick(opts, val)
                    if i is not None:
                        el.select_option(index=i)
            elif kind == "combobox":
                el.click()
                page.wait_for_timeout(400)
                want = _norm(val)
                if f.get("options"):                       # a fixed list: click the option that matches
                    opts = page.locator('[role="option"]').locator("visible=true")
                    texts = opts.all_inner_texts()
                    i = next((k for k, t in enumerate(texts) if _norm(t) == want), None)
                    if i is None:
                        i = next((k for k, t in enumerate(texts) if want and (want in _norm(t) or _norm(t) in want)), None)
                    if i is None:
                        el.fill(str(val))
                        page.wait_for_timeout(500)
                        page.keyboard.press("Enter")
                    else:
                        opts.nth(i).click()
                else:                                      # search-as-you-type (places): type, take the first suggestion
                    el.fill("")
                    el.press_sequentially(str(val), delay=50)
                    page.wait_for_timeout(1300)
                    opts = page.locator('[role="option"]').locator("visible=true")
                    if opts.count():
                        opts.first.click()
                    else:
                        page.keyboard.press("Enter")
            elif kind == "radio":
                i = _pick(f["options"], val)
                if i is None:
                    log(f"      ! no option matched {str(val)[:40]!r} for '{f.get('label','')[:50]}'")
                else:
                    loc = page.locator(f'[data-aa="{f["option_ids"][i]}"]')
                    if loc.evaluate("e => e.tagName") == "BUTTON":
                        loc.click()
                    else:
                        loc.check(force=True)
            elif kind == "checkbox_group":
                for v in (val if isinstance(val, list) else [val]):
                    i = _pick(f["options"], v)
                    if i is None:
                        log(f"      ! no option matched {str(v)[:40]!r} for '{f.get('label','')[:50]}'")
                    else:
                        page.locator(f'[data-aa="{f["option_ids"][i]}"]').check(force=True)
            elif kind == "checkbox_single":
                if val is True or str(val).lower() in ("true", "yes"):
                    el.check(force=True)
            elif kind == "file":
                path = files.get(str(val).upper())
                if path:
                    _upload(page, _file_input(page, f), path, f, log)
        except Exception as e:
            log(f"      ! could not fill '{f.get('label','')[:50]}': {str(e).splitlines()[0]}")


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


def verify(page, fields: list[dict], answers: dict, log=print):
    """After filling, re-read the page and say which answered fields didn't actually take."""
    for f in fields:
        val = answers.get(f["id"])
        if val in (None, "") or f["kind"] not in ("text", "textarea", "email", "tel", "url", "number", "select"):
            continue
        try:
            got = page.locator(f'[data-aa="{f["id"]}"]').input_value()
        except Exception:
            continue
        if not str(got).strip():
            log(f"      ! NOT FILLED: '{f.get('label','')[:60]}' (wanted {str(val)[:40]!r})")


NEXT_RX = re.compile(r"^\s*(next|continue|save\s*(and|&)\s*(continue|next)|next step|review( (and|&) submit| application)?|proceed|"
                     r"continue to .{2,40}|go to (next|review).{0,20})\s*[>\u2192]?\s*$", re.I)
FINAL_RX = re.compile(r"^\s*(submit( (your )?application)?|send( my)? application|finish|complete( application)?|apply( now)?)\s*[>\u2192]?\s*$", re.I)
LANDING_RX = re.compile(r"^\s*(apply manually|start (your )?application|apply( now)?|continue application)\s*$", re.I)
MAX_STEPS = 16


def _find_advance(page):
    """(button, 'submit'|'next') for the visible primary action of the current step, or (None, None)."""
    loc = page.locator("button, input[type=submit], input[type=button], [role=button]").locator("visible=true")
    nxt = final = None
    for i in range(min(loc.count(), 60)):
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
        errs = page.locator('[aria-invalid="true"], [role="alert"], .error, .field-error, [class*="error"]').locator("visible=true").all_inner_texts()
    except Exception:
        return []
    return [e.strip()[:80] for e in errs if e.strip()][:4]


def _landing(page) -> bool:
    """Click 'Apply Manually' / 'Apply' style buttons on a page that has no form yet."""
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=LANDING_RX).locator("visible=true")
        if loc.count():
            loc.first.click(force=True)
            page.wait_for_timeout(2000)
            return True
    return False


SECURITY_TEXT = re.compile(r"security code|verification code|enter the (\d|six|eight|8|6)[- ]?(character|digit)? ?code", re.I)


def _security_prompt(page) -> bool:
    """A site that answers a submit with 'enter the code we emailed you' is running its own human check."""
    try:
        if page.locator('input[id^="security-input"]').locator("visible=true").count() >= 4:
            return True
        one = page.locator('input[autocomplete="one-time-code"], input[name*="security" i], input[id*="security" i]')
        return one.locator("visible=true").count() > 0 and bool(SECURITY_TEXT.search(page.inner_text("body")))
    except Exception:
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


def submit(page, timeout_ms: int = 20000, btn=None, on_click=None) -> str:
    before_url = page.url
    before_hits = len(SUCCESS_RE.findall(page.inner_text("body")))
    if btn is None:
        btn = page.locator("button[type=submit], input[type=submit]").filter(visible=True).last
        if not btn.count():
            btn = page.get_by_role("button", name=re.compile(r"submit|send application|apply", re.I)).last
    t_click = time.time()
    if on_click:
        on_click()                       # write-ahead: from here on, a crash or timeout must never lead to a second submit
    btn.click()
    waited = 0
    try:
        while waited < timeout_ms:
            page.wait_for_timeout(1000)
            waited += 1000
            body = page.inner_text("body")
            url_says_done = page.url != before_url and re.search(r"thank|success|submitted|confirmation", page.url, re.I)
            if len(SUCCESS_RE.findall(body)) > before_hits or url_says_done:
                return "confirmed"
            if _security_prompt(page):
                raise Blocked("the site asked for an emailed security code (its own human check): left for you to finish by hand")
            if (b := _blocker(page)):
                raise Blocked(f"{b} after submit")
            if waited >= 6000 and waited % 3000 == 0:
                _rejected(page, before_url, btn)          # the form bounced the submit with 'X is required': stop waiting
        _rejected(page, before_url, btn)
        errs = _page_errors(page)
        tail = " ".join(page.inner_text("body").split())[-220:]
        msg = "no confirmation after submit" + (f"; page errors: {errs}" if errs else "") + f"; page ends: {tail!r}"
    except (Blocked, Unconfirmed, NotSubmitted):
        raise
    except Exception as ex:              # page closed / navigated away after the click: the submit may have gone through
        msg = f"no confirmation after submit (page error after click: {str(ex).splitlines()[0][:120]})"
    e = Unconfirmed(msg)
    e.t0 = t_click
    raise e


def apply(page, job, brain, cover_letter: str, files: dict[str, Path], shot: Path, dry_run: bool, log=print, accounts=None, on_click=None) -> str:
    """Expects open_form(page, job.apply_url) to have been called already. Handles one-page forms and multi-step wizards
    (with account creation / sign-in when the site needs it)."""
    from . import auth
    start_url = page.url
    total, uploaded, prev_sig, stuck, answered = 0, False, None, 0, {}
    limit_at = time.time() + MAX_APPLY_SECONDS
    for step in range(1, MAX_STEPS + 1):
        if time.time() > limit_at:
            raise RuntimeError(f"took longer than {MAX_APPLY_SECONDS // 60} minutes on this site")
        page.wait_for_timeout(600)
        if step > 1 and SUCCESS_RE.search(page.inner_text("body")) and _few_inputs(page):
            return "confirmed"
        if (b := _blocker(page)):
            raise Blocked(b)
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
        if len(fields) < 2 and _landing(page):            # description / "Apply Manually" page: get to the form
            fields = extract(page)
        sig = tuple(sorted(f.get("label", "") for f in fields))
        stuck = stuck + 1 if sig == prev_sig else 0
        if stuck >= 2:
            raise Unanswerable(f"stuck on step {step}; page says: {_page_errors(page) or 'nothing'}")
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
                raise Blocked(f"not a real application form ({total} fields, no résumé upload)")
            raise Blocked("no Next or Submit button found on this step")
        if kind == "submit":
            if total < 4 or not uploaded:
                raise Blocked(f"not a real application form ({total} fields, no résumé upload)")
            page.screenshot(path=str(shot), full_page=True)
            if dry_run:
                return "dry_run"
            try:
                try:
                    result = submit(page, btn=btn, on_click=on_click)
                except NotSubmitted as e:
                    res_fields = [f for f in fields if plan["answers"].get(f["id"]) == "RESUME"]
                    if not (res_fields and re.search(r"resume|r\u00e9sum\u00e9|\bcv\b|attach|upload|file", str(e), re.I)):
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
                    result = submit(page, btn=btn2, on_click=on_click)
            except (Unconfirmed, NotSubmitted):
                page.screenshot(path=str(shot.with_name("after_submit.png")), full_page=True)
                raise
            page.screenshot(path=str(shot.with_name("confirmation.png")), full_page=True)
            return result
        btn.click(force=True)                                 # Next / Save and Continue
        page.wait_for_timeout(1500)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    raise Blocked(f"more than {MAX_STEPS} steps; giving up")
