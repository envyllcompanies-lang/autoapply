"""Turn tailored Markdown into clean, ATS-readable PDFs using the same Chromium we drive."""
from __future__ import annotations

import html
import re
from pathlib import Path

import markdown

CSS = """
@page { size: Letter; margin: 0.55in 0.6in; }
body { font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 10.3pt; line-height: 1.32; color: #111; }
h1 { font-size: 20pt; margin: 0 0 2px; letter-spacing: .3px; }
h1 + p { margin: 0 0 10px; color: #444; }
h2 { font-size: 10.5pt; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #999;
     padding-bottom: 2px; margin: 12px 0 5px; }
h3 { font-size: 10.5pt; margin: 7px 0 1px; }
p { margin: 2px 0 5px; }
ul { margin: 2px 0 5px 16px; padding: 0; }
li { margin: 1px 0; }
.letter { font-size: 11.5pt; line-height: 1.45; }
.letter .head { margin-bottom: 22px; } .letter .head b { font-size: 15pt; letter-spacing: .3px; }
.letter .head div { color: #444; font-size: 10pt; } .letter .date { margin-bottom: 16px; }
.letter p { margin: 0 0 12px; white-space: pre-line; }
"""


def _pdf(browser, body_html: str, out: Path) -> Path:
    page = browser.new_page()
    page.set_content(f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body_html}</body></html>")
    page.pdf(path=str(out), format="Letter", print_background=True)
    page.close()
    return out


def resume_pdf(browser, md_text: str, out: Path) -> Path:
    return _pdf(browser, markdown.markdown(md_text, extensions=["sane_lists"]), out)


def letter_pdf(browser, text: str, out: Path, name: str = "", contact: str = "", today: str = "") -> Path:
    """Full business-letter layout: letterhead, date, paragraphs with space between them."""
    import datetime
    today = today or datetime.date.today().strftime("%B %-d, %Y")
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    head = f"<div class='head'><b>{html.escape(name)}</b><div>{html.escape(contact)}</div></div>" if name else ""
    body = "".join(f"<p>{html.escape(p.strip())}</p>" for p in paras)
    css_page = "<style>@page { margin: 0.75in 0.9in; }</style>"
    return _pdf(browser, css_page + f"<div class='letter'>{head}<div class='date'>{today}</div>{body}</div>", out)
