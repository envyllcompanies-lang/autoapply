"""Generic application-form driver: find the form, read every field, answer from your facts, fill, submit, verify."""
from __future__ import annotations

import re
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

  let n = 0; const tag = el => { if (!el.dataset.aa) el.dataset.aa = 'f' + (n++); return el.dataset.aa; };
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
    else if (type === 'file') { f.kind = 'file'; f.accept = el.accept || ''; }
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
    r"(thank(s| you) for (applying|your application|submitting)|application (has been |was )?(submitted|received)"
    r"|we('ve| have) received your application|successfully submitted|your application is in)", re.I)
BLOCKERS = [
    ('iframe[src*="recaptcha/api2/bframe"]', "reCAPTCHA challenge"),
    ('iframe[src*="recaptcha/api2/anchor"]', "reCAPTCHA checkbox"),
    ('iframe[src*="hcaptcha.com"]', "hCaptcha"),
    ('iframe[src*="challenges.cloudflare.com"]', "Cloudflare Turnstile"),
    ('input[type="password"]', "login required"),
]


class Blocked(Exception):
    pass


UNSUPPORTED = re.compile(r"myworkdayjobs|\.workday\.com|icims\.com|taleo\.net|linkedin\.com|indeed\.com|glassdoor\.com|"
                         r"ziprecruiter\.com|successfactors|oraclecloud\.com|ultipro\.com|ukg\.com|paylocity|paycomonline|"
                         r"brassring|smartrecruiters\.com/oneclick|adp\.com", re.I)
APPLY_BTN = re.compile(r"^\s*(apply( now| here| today| online| for this (job|position|role)| to this job)?|"
                       r"apply (on|at|via) (the )?(company|employer)('s)? (site|website|page)|i'?m interested|apply externally)\s*[>\u2192]?\s*$", re.I)


def _looks_like_form(page) -> bool:
    try:
        return page.locator('form input[type=file]:visible, form input[type=email]:visible, input[type=file]').count() > 0 \
            and page.locator("form input:visible, form textarea:visible").count() >= 3
    except Exception:
        return False


def resolve_apply_url(page, url: str, log=print) -> str:
    """Follow a job-board / aggregator link to the employer's real application page (max 3 hops)."""
    from .aggregators import board_of
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


def guard_ai_policy(page, job):
    """Never submit AI-written answers to an employer that says it doesn't want them."""
    text = (job.description or "") + "\n" + page.inner_text("body")
    for rx in AI_BAN:
        m = rx.search(text)
        if m:
            raise Unanswerable("posting restricts AI-assisted applications: \"" + " ".join(m.group(0).split())[:90] + "\"")


def _blocker(page) -> str | None:
    for sel, name in BLOCKERS:
        for el in page.locator(sel).all():
            try:
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box["width"] > 30 and box["height"] > 30:
                        return name
            except Exception:
                pass
    return None


def _has_form(page) -> bool:
    return page.locator("input[type=email], input[type=text], textarea, input[type=file]").count() >= 2


def open_form(page, url: str):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    if not _has_form(page):
        # Some boards show the description first with an "Apply" button.
        btn = page.get_by_role("button", name=re.compile(r"^\s*apply", re.I)).or_(
            page.get_by_role("link", name=re.compile(r"^\s*apply", re.I))).first
        if btn.count():
            btn.click()
            page.wait_for_timeout(2500)
    if (b := _blocker(page)):
        raise Blocked(b)
    if not _has_form(page):
        raise Blocked("no application form found on page")


def extract(page) -> list[dict]:
    fields = page.evaluate(EXTRACT_JS)
    for f in fields:  # comboboxes only reveal options when opened
        if f["kind"] == "combobox":
            try:
                el = page.locator(f'[data-aa="{f["id"]}"]')
                el.click()
                page.wait_for_timeout(400)
                f["options"] = [t.strip() for t in page.locator('[role="option"]').all_inner_texts() if t.strip()][:80]
                page.keyboard.press("Escape")
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
    log("      ! location box stayed empty (site wants a suggestion picked)")


def fill(page, fields: list[dict], answers: dict, files: dict[str, Path], log=print):
    by_id = {f["id"]: f for f in fields}
    for fid, val in answers.items():
        f = by_id.get(fid)
        if f is None or val is None or val == "":
            continue
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
                el.fill(str(val))
                page.wait_for_timeout(600)
                opt = page.locator('[role="option"]').filter(has_text=re.compile(re.escape(str(val)), re.I)).first
                if opt.count():
                    opt.click()
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
                    el.set_input_files(str(path))
        except Exception as e:
            log(f"      ! could not fill '{f.get('label','')[:50]}': {str(e).splitlines()[0]}")


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


def submit(page, timeout_ms: int = 20000) -> str:
    before_url = page.url
    before_hits = len(SUCCESS_RE.findall(page.inner_text("body")))
    btn = page.locator("button[type=submit], input[type=submit]").filter(visible=True).last
    if not btn.count():
        btn = page.get_by_role("button", name=re.compile(r"submit|send application|apply", re.I)).last
    btn.click()
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(1000)
        waited += 1000
        body = page.inner_text("body")
        url_says_done = page.url != before_url and re.search(r"thank|confirm|success", page.url, re.I)
        if len(SUCCESS_RE.findall(body)) > before_hits or url_says_done:
            return "confirmed"
        if (b := _blocker(page)):
            raise Blocked(f"{b} after submit")
    errs = page.locator('[aria-invalid="true"], .error, .field-error, [class*="error"]').all_inner_texts()
    errs = [e.strip() for e in errs if e.strip()][:3]
    raise RuntimeError("no confirmation after submit" + (f"; page errors: {errs}" if errs else ""))


def apply(page, job, brain, cover_letter: str, files: dict[str, Path], shot: Path, dry_run: bool, log=print) -> str:
    """Expects open_form(page, job.apply_url) to have been called already."""
    fields = extract(page)
    log(f"      form has {len(fields)} fields ({sum(f['required'] for f in fields)} required)")
    plan = brain.map_fields(job, fields, cover_letter)
    missing = [by["label"][:60] for by in fields if by["id"] in set(plan["unanswerable_required"])]
    if missing:
        for f in fields:
            if f["id"] in set(plan["unanswerable_required"]):
                log(f"      ? unanswered: {f['label'][:90]!r} kind={f['kind']} options={[o[:30] for o in (f.get('options') or [])][:6]}")
        raise Unanswerable("can't truthfully answer required: " + "; ".join(missing))
    fill(page, fields, plan["answers"], files, log)
    verify(page, fields, plan["answers"], log)
    page.screenshot(path=str(shot), full_page=True)
    if dry_run:
        return "dry_run"
    result = submit(page)
    page.screenshot(path=str(shot.with_name("confirmation.png")), full_page=True)
    return result
