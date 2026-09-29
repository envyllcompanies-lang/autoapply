"""Turn tailored Markdown into clean, ATS-readable PDFs using the same Chromium we drive."""
from __future__ import annotations

import html
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
.letter { font-size: 11pt; line-height: 1.5; white-space: pre-wrap; }
"""


def _pdf(browser, body_html: str, out: Path) -> Path:
    page = browser.new_page()
    page.set_content(f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body_html}</body></html>")
    page.pdf(path=str(out), format="Letter", print_background=True)
    page.close()
    return out


def resume_pdf(browser, md_text: str, out: Path) -> Path:
    return _pdf(browser, markdown.markdown(md_text, extensions=["sane_lists"]), out)


def letter_pdf(browser, text: str, out: Path) -> Path:
    return _pdf(browser, f"<div class='letter'>{html.escape(text)}</div>", out)
