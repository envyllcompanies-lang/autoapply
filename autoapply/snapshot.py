"""What the page looked like when an application stopped.

Every application that does not go through leaves a short text picture of the page it stopped on in
logs/snapshots.log: the page's headings, questions, boxes (what kind, whether required, whether filled, whether the
site marked them wrong), buttons, open lists and error messages. That is what is needed to see why a form would not go
on and to rebuild the page as a test, without screenshots.

Your answers are not saved. Boxes are recorded as filled/empty, never with their contents; lists and yes/no questions as
answered/not answered, never with which choice; a page that shows your answers back to you (Workday's Review step) is
recorded as headings, buttons and error messages only. Your email address, phone number, name, street address, city, zip
code and profile links are blanked out of any text the page itself shows. The file is plain text, is kept with the run
logs, and only its most recent part is kept (it never grows past a few hundred kilobytes).
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

OUTLINE_JS = r"""(maxLines) => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (el, n) => ((el.innerText || el.textContent || '') + '').replace(/\s+/g, ' ').trim().slice(0, n || 110);
  const aid = el => el.getAttribute('data-automation-id') || '';
  const isOn = e => !!e.checked || e.getAttribute('aria-checked') === 'true';
  const EMPTY = /^\s*(select( one)?|choose( one)?|please select|-+|none( selected)?)?\s*\.?\s*$/i;
  const out = [];
  const seen = new Set();
  const key = t => t.replace(/[*\u2731\s]+$/, '').slice(0, 90);
  const seenText = { has: t => seen.has(key(t)), add: t => seen.add(key(t)) };      // a question is printed once (label or text)
  const push = s => { if (out.length < maxLines) out.push(s); };
  const alerts = el => { for (const e of el.querySelectorAll('[role="alert"], [data-automation-id*="rror"]')) { const t = vis(e) ? txt(e, 200) : '';
                           if (t && !seenText.has(t)) { seenText.add(t); push('! ' + t); } } };
  const walk = el => {
    if (out.length >= maxLines) return;
    const tag = el.tagName;
    if (!tag || /^(SCRIPT|STYLE|NOSCRIPT|SVG|HEAD|META|LINK|IFRAME)$/i.test(tag)) {
      if (tag === 'IFRAME' && vis(el)) push('<iframe ' + (el.getAttribute('src') || '').split('?')[0].slice(0, 80) + '>');
      return;
    }
    const type = (el.getAttribute('type') || '').toLowerCase();
    const hiddenOk = tag === 'INPUT' && (type === 'file' || type === 'radio' || type === 'checkbox');
    const cs = getComputedStyle(el);
    if (!hiddenOk && (cs.display === 'none' || cs.visibility === 'hidden')) return;
    const shown = hiddenOk || vis(el);            // a zero-size wrapper (display: contents) still has visible children
    const a = aid(el), role = el.getAttribute('role') || '';
    if (a === 'applyFlowReviewPage') {            // this page shows every answer back: only its headings and complaints
      push('{applyFlowReviewPage} (shows your answers back; not recorded)');
      for (const h of el.querySelectorAll('h2, h3, h4')) if (vis(h)) push('# ' + txt(h, 80));
      alerts(el);
      return;
    }
    if (/^H[1-5]$/.test(tag)) { if (shown) push('# ' + txt(el)); return; }
    if (tag === 'LABEL' || tag === 'LEGEND') { const t = txt(el); if (t && !seenText.has(t)) { seenText.add(t); push('label: ' + t); } if (tag === 'LABEL') return; }
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      if (type === 'hidden' || !shown) return;
      const bits = [tag.toLowerCase() + (type ? ':' + type : '')];
      if (el.id) bits.push('#' + el.id.slice(0, 60)); else if (el.name) bits.push('name=' + el.name.slice(0, 40));
      if (a) bits.push('{' + a + '}');
      if (el.required || el.getAttribute('aria-required') === 'true') bits.push('required');
      if (el.getAttribute('aria-invalid') === 'true') bits.push('INVALID');
      if (type === 'radio' || type === 'checkbox') {
        // whether the question is answered, never which option: the options of one question share a box
        const box = el.closest('fieldset, [role="radiogroup"], [data-automation-id^="formField-"]') || el.closest('[role="group"]') || el.parentElement;
        const any = box ? [...box.querySelectorAll('input[type="radio"], input[type="checkbox"]')].some(isOn) : isOn(el);
        bits.push(any ? 'question-answered' : 'question-unanswered');
      }
      else if (type === 'file') bits.push(el.files && el.files.length ? 'has-file' : 'no-file');
      else if (type !== 'password') bits.push(el.value ? 'filled' : 'empty');
      if (tag === 'SELECT') bits.push('options=' + el.options.length);
      push('  <' + bits.join(' ') + '>');
      return;
    }
    if (a === 'selectedItem' || a === 'selectedItemList') { if (shown && txt(el, 5)) push('    (a choice is selected)'); return; }
    if (a === 'promptAriaInstruction') return;        // reads '1 item selected, <the choice>': the line above already says so
    if (tag === 'BUTTON' || role === 'button' || (tag === 'A' && (role === 'button' || a))) {
      if (!shown) return;
      const pop = el.getAttribute('aria-haspopup');
      const inForm = !el.closest('header, nav, footer, [data-automation-id="header"], [data-automation-id="footerContainer"]');
      const bits = [];
      if (pop === 'listbox' && inForm) bits.push('[dropdown] ' + (EMPTY.test(txt(el, 60)) ? 'nothing chosen' : 'chosen'));   // never which
      else if (/^(yes|no)$/i.test(txt(el, 6))) bits.push('[button] ' + txt(el, 6));
      else bits.push('[' + (tag === 'A' ? 'link' : 'button') + '] ' + txt(el, 60));
      if (el.id && pop === 'listbox') bits.push('#' + el.id.slice(0, 60));
      if (a) bits.push('{' + a + '}');
      if (pop && pop !== 'listbox') bits.push('opens-' + pop);
      if (el.getAttribute('aria-invalid') === 'true') bits.push('INVALID');
      if (el.disabled) bits.push('disabled');
      push('  ' + bits.join(' '));
      return;
    }
    if (role === 'alert' || /rror|alert/i.test(a)) { const t = txt(el, 200); if (t && !seenText.has(t)) { seenText.add(t); push('! ' + t + (a ? ' {' + a + '}' : '')); } return; }
    if (role === 'option') { push('    (choice) ' + txt(el, 60)); return; }
    if (a && /^(applyFlow|progressBarActiveStep|formField-|multiSelectContainer|errorContainer|alreadyApplied|signInContent)/.test(a))
      push((a.startsWith('formField-') ? ' ' : '') + '{' + a + '}' + (a === 'progressBarActiveStep' ? ' ' + txt(el, 70) : ''));
    else if (role === 'radiogroup' || role === 'group' || role === 'dialog') { if (el.getAttribute('aria-invalid') === 'true') push(' (group INVALID)'); if (role === 'dialog') push('(dialog)'); }
    if (!el.children.length) { const t = shown ? txt(el, 140) : ''; if (t.length > 25 && !seenText.has(t)) { seenText.add(t); push('  text: ' + t); } return; }
    for (const c of el.children) walk(c);
  };
  walk(document.body);
  return out.join('\n');
}"""

MAX_PER_RUN = 30
MAX_FILE_BYTES = 300_000
_count = [0]


def path(base) -> Path:
    return Path(base) / "logs" / "snapshots.log"


def outline(page, max_lines: int = 170) -> str:
    try:
        return page.evaluate(OUTLINE_JS, max_lines)
    except Exception as e:
        return f"(the page could not be read: {str(e).splitlines()[0][:100]})"


def scrub(text: str, facts: dict | None = None) -> str:
    """Blank out the applicant's own details in page text."""
    facts = facts or {}
    text = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "<email>", text)
    text = re.sub(r"\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b", "<phone>", text)
    for k in ("linkedin", "github", "website"):                       # profile links, with or without https:// and www.
        v = re.sub(r"^https?://(www\.)?", "", str(facts.get(k) or "").strip(), flags=re.I).rstrip("/")
        if len(v) >= 6:
            text = re.sub(r"(https?://)?(www\.)?" + re.escape(v) + r"/?", f"<{k}>", text, flags=re.I)
    text = re.sub(r"[\w.()-]+\.(pdf|docx?|rtf|odt|txt)\b", "<file>", text, flags=re.I)      # an uploaded file's name (it has your name in it)
    for k in ("full_name", "last_name", "first_name", "address_line1", "preferred_name", "middle_name"):
        v = str(facts.get(k) or "").strip()
        if len(v) >= 3:
            text = re.sub(re.escape(v), f"<{k.split('_')[0]}>", text, flags=re.I)
    for k in ("city", "zip"):                                           # whole words only (a city called 'Troy' must not blank 'Troyes')
        v = str(facts.get(k) or "").strip()
        if len(v) >= 3:
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", f"<{k}>", text, flags=re.I)
    return text


def save(base: Path, page, job, status: str, reason: str, facts: dict | None = None, stage: str = "") -> bool:
    """Append one snapshot to today's snapshot file. Never raises."""
    try:
        if page is None or _count[0] >= MAX_PER_RUN:
            return False
        _count[0] += 1
        try:
            url = page.url.split("?")[0][:200]
        except Exception:
            url = "?"
        body = scrub(outline(page), facts)
        head = (f"=== {datetime.now():%Y-%m-%d %H:%M:%S}  {status}  {getattr(job, 'title', '')} @ {getattr(job, 'company', '')}\n"
                f"url: {url}\n" + (f"stage: {stage}\n" if stage else "") + f"why: {scrub(str(reason), facts)[:400]}\n")
        p = path(base)
        p.parent.mkdir(parents=True, exist_ok=True)
        old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        new = old + head + body[:7000] + "\n\n"
        if len(new.encode("utf-8", "replace")) > MAX_FILE_BYTES:          # keep the newest snapshots only
            new = new[-MAX_FILE_BYTES:]
            cut = new.find("\n=== ")
            new = new[cut + 1:] if cut >= 0 else new
        p.write_text(new, encoding="utf-8")
        return True
    except Exception:
        return False
