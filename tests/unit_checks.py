"""Fast checks of the writer's truthfulness lint. Run: python tests/unit_checks.py"""
import sys, yaml
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from autoapply.writer import Writer

cfg = yaml.safe_load((ROOT / "config.yaml").read_text()); prof = yaml.safe_load((ROOT / "profile.yaml").read_text())
w = Writer(cfg, prof, ROOT); w.first = "Brian"; w._ctx = "Ramp Product Operations Specialist Juno"
JD = "You will coordinate launch readiness in Linear and Retool with the product team."
bad = {
    "cut off": "Dear Ramp team,\n\nMy background in operations aligns closely with the need to stabilize projects, remove roadblocks",
    "managed estates": "At Roaring Fork Property Group, I managed operations across 70+ luxury estates. It was steady work.\n\nBrian",
    "linear claim": "I focus on visibility, whether I am triaging issues or coordinating launch readiness in Linear.\n\nBrian",
    "retool claim": "I have built dashboards using Retool for the team.\n\nBrian",
}
good = {
    "ok": "At Roaring Fork Property Group I performed inspections and readiness checks across 70+ luxury estates and helped write SOPs.\n\nBrian",
    "linear regression": "I studied linear programming and used SQL to analyze data in Excel.\n\nBrian",
}
problems = []
for k, t in bad.items():
    if not w._issues(t, 2200, JD):
        problems.append(f"missed: {k}")
for k, t in good.items():
    got = w._issues(t, 2200, JD)
    if got:
        problems.append(f"false alarm on {k}: {got}")
# LinkedIn public-page parsers (markup as LinkedIn serves it to logged-out visitors)
from autoapply.aggregators import _linkedin_cards, _linkedin_detail, canon_key, board_of
frag = ('<li><div class="base-card" data-entity-urn="urn:li:jobPosting:3801234567"><a class="base-card__full-link" '
        'href="https://www.linkedin.com/jobs/view/operations-analyst-at-acme-3801234567?position=1"></a>'
        '<h3 class="base-search-card__title">\n Operations Analyst \n</h3><h4 class="base-search-card__subtitle"><a class="hidden-nested-link">Acme Inc</a></h4>'
        '<span class="job-search-card__location"> New York, NY </span></div></li>')
cards = _linkedin_cards(frag)
if cards != [("3801234567", "Operations Analyst", "Acme Inc", "New York, NY")]:
    problems.append(f"linkedin cards parse wrong: {cards}")
page_ext = ('<div class="show-more-less-html__markup"><p>Run vendor <b>workflows</b>.</p></div>'
            '<code id="applyUrl"><!--"https://www.linkedin.com/jobs/view/externalApply/3801234567?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F555&urlHash=x"--></code>')
desc, ext = _linkedin_detail(page_ext)
if "vendor workflows" not in desc.replace("\n", " ") or ext != "https://boards.greenhouse.io/acme/jobs/555":
    problems.append(f"linkedin detail parse wrong: {desc!r} {ext!r}")
if _linkedin_detail('<div class="show-more-less-html__markup">x</div>')[1] != "":
    problems.append("easy-apply-only job should have no employer url")
if canon_key("https://boards.greenhouse.io/acme/jobs/555") != "greenhouse:acme:555" or board_of("https://jobs.lever.co/foo/abc")[1] != "foo":
    problems.append("board/canon detection wrong")

# a reply cut off by the token limit must be retried with a bigger budget, not accepted
import requests, os
from autoapply.writer import _Limiter
calls = []
class R:
    status_code = 200; headers = {}; text = ""
    def __init__(self, n): self.n = n
    def json(self):
        full = self.n >= 1000
        return {"choices": [{"finish_reason": "stop" if full else "length",
                             "message": {"content": "A full answer." if full else "A cut off answ"}}]}
def fake_post(url, headers=None, json=None, timeout=None):
    calls.append(json["max_tokens"]); return R(json["max_tokens"])
requests.post = fake_post
os.environ["K"] = "x"
prov = {"name": "t", "base_url": "http://x", "model": "m", "api_key_env": "K"}
w.limiters["t"] = _Limiter(None, None, None, "")
out = w._chat(prov, [{"role": "user", "content": "hi"}], 500)
if out != "A full answer." or calls != [500, 1000]:
    problems.append(f"truncation retry wrong: {out!r} {calls}")
# ---- Ashby-style Yes/No buttons are seen and clickable ----
def _button_group_test():
    from playwright.sync_api import sync_playwright
    from autoapply.submit import EXTRACT_JS, fill, verify
    html = """<form><div><label>Full Name*</label><input type=text></div>
    <div><label>Will you now or in the future require Notion to sponsor an immigration case?*</label>
    <div><button type=button onclick="this.dataset.hit=1">Yes</button><button type=button onclick="this.dataset.hit=1">No</button></div></div>
    <div><label>Note</label><textarea></textarea></div>
    <button type=submit>Submit</button></form>"""
    with sync_playwright() as pw:
        b = pw.chromium.launch(); pg = b.new_page(); pg.set_content(html)
        fields = pg.evaluate(EXTRACT_JS)
        radios = [f for f in fields if f["kind"] == "radio"]
        assert len(radios) == 1 and radios[0]["options"] == ["Yes", "No"], fields
        assert "sponsor" in radios[0]["label"] and radios[0]["required"], radios[0]
        tx = [f for f in fields if f["kind"] == "textarea"][0]
        logs = []
        fill(pg, fields, {radios[0]["id"]: "No", tx["id"]: "hi"}, {}, logs.append)
        verify(pg, fields, {tx["id"]: "hi"}, logs.append)
        assert not logs, logs
        no_id = radios[0]["option_ids"][1]
        assert pg.locator(f'[data-aa="{no_id}"]').evaluate("e => e.dataset.hit === '1'"), "No button not clicked"
        b.close()
    print("ok  yes/no button group extracted and clicked")

try:
    _button_group_test()
except ImportError:
    print("skip button group test (no playwright)")
except AssertionError as e:
    problems.append("button group: " + str(e)[:200])

# ---- verification-email parsing ----
def _mail_test():
    from autoapply import mailbox
    raw = (b"From: no-reply@myworkday.com\r\nTo: delgado@alumni.usc.edu\r\nSubject: Verify your email address\r\n"
           b"Date: Mon, 28 Sep 2026 21:00:00 -0600\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b"<p>Welcome!</p><a href=\"https://wd5.myworkday.com/acme/verify?token=abc123&amp;x=1\">Verify</a>"
           b"<a href=\"https://acme.com/unsubscribe\">unsubscribe</a>")
    m = mailbox.parse_message(raw)
    assert m["link"] == "https://wd5.myworkday.com/acme/verify?token=abc123&x=1", m
    code = mailbox.parse_message(b"Subject: Your verification code\r\n\r\nYour code is 482913. It expires soon.")
    assert code["code"] == "482913", code
    print("ok  verification email parsing")

try:
    _mail_test()
except AssertionError as e:
    problems.append("mail parse: " + str(e)[:200])

print("RESULT:", "ALL AS EXPECTED" if not problems else "PROBLEMS: " + "; ".join(problems))
sys.exit(1 if problems else 0)
