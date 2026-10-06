"""Emailed verification codes: finding them in emails and typing them in. Run: python tests/code_checks.py (needs no private config)"""
import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from email.message import EmailMessage
from autoapply import mailbox as M
cases = [
 ("Your verification code", "Hi Brian,\nYour verification code is 482913. It expires in 10 minutes.\n123 Main St, Denver CO 80202", "482913"),
 ("Verify your email", "<p>Enter this code:</p><p><strong>731 204</strong></p>", "731204"),  # spaced digits: see below
 ("Your one-time passcode", "<div>Use the code below</div><div>X7K2QF</div><p>© 2026 Acme</p>", "X7K2QF"),
 ("Welcome to Acme careers", "Thanks for creating an account in 2026. Click https://acme.com/verify?t=abc", None),
 ("Your code: 5521", "body", "5521"),
 ("Security code for your application to CharterUp", "Hi Brian,\n\nCopy and paste this code into the security code field on your application:\n\nXk3A9bQ2\n\nAfter you enter the code, resubmit your application.", "Xk3A9bQ2"),
 ("Security code for your application to CharterUp", "Copy and paste this code into the security code field on your application: aB3dE7gH", "aB3dE7gH"),
 ("Security code", "Order #99881231 placed. Your security code:\n\n  904417", "904417"),
]
bad=0
for subj, body, want in cases:
    got = M.find_code(subj, body)
    ok = got == want or (want is None and subj=="Verify your email")
    print("OK " if ok else "BAD", subj, "->", got)
    bad += not ok
m = EmailMessage(); m["Subject"]="Welcome to Acme careers"; m["Date"]="Thu, 01 Oct 2026 19:00:00 +0000"
m.set_content("Confirm your account: https://acme.com/account/verify?token=abc")
info = M.parse_message(bytes(m)); print(info["link"], info["code"], info["hint"])
bad_mail = bad

# ---- a code email for ANOTHER company must never be used for this application (back-to-back applications)
import time as _t  # noqa: E402
_now = _t.time()


def _mail(company, code, age=0):
    return {"subject": f"Security code for your application to {company}", "from": "no-reply@us.greenhouse-mail.io", "body": "Copy and paste this code",
            "ts": _now - age, "hint": True, "code": code, "link": None, "folder": "INBOX"}


_old_recent = M._recent
M._DISABLED = ""
try:
    M._recent = lambda since, n=12, folders=("INBOX",): iter([_mail("Acme Logistics", "AAAA1111", 2)])
    got = M.wait_for_verification(since_ts=_now - 5, host_hint="vardaspace|Varda Space Industries", timeout=1, require_code=True, log=lambda *a: None)
    print("OK " if got is None else "BAD", "another company's fresh code ->", got)
    bad_mail += got is not None
    M._recent = lambda since, n=12, folders=("INBOX",): iter([_mail("Acme Logistics", "AAAA1111", 2), _mail("Varda Space Industries", "BBBB2222", 1)])
    got = M.wait_for_verification(since_ts=_now - 5, host_hint="vardaspace|Varda Space Industries", timeout=1, require_code=True, log=lambda *a: None)
    ok = bool(got and got["code"] == "BBBB2222")
    print("OK " if ok else "BAD", "this company's code among others ->", got)
    bad_mail += not ok
    M._recent = lambda since, n=12, folders=("INBOX",): iter([_mail("Strive Health", "CCCC3333", 1)])
    got = M.wait_for_verification(since_ts=_now - 5, host_hint="strivehealth", timeout=1, require_code=True, log=lambda *a: None)
    ok = bool(got and got["code"] == "CCCC3333")
    print("OK " if ok else "BAD", "slug matches the name in the subject ->", got)
    bad_mail += not ok
    M._recent = lambda since, n=12, folders=("INBOX",): iter([_mail("Octus", "DDDD4444", 1)])
    got = M.wait_for_verification(since_ts=_now - 5, host_hint="", timeout=1, require_code=True, log=lambda *a: None)
    ok = bool(got and got["code"] == "DDDD4444")
    print("OK " if ok else "BAD", "no company known: a fresh code is still used ->", got)
    bad_mail += not ok
finally:
    M._recent = _old_recent

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skip browser checks (no playwright)"); sys.exit(bad_mail)
from autoapply import auth as A  # noqa: E402
SPLIT = """<p>We sent a 6-digit code to your email. Enter the code.</p>
<label>Middle initial <input maxlength="1" id="mi"></label>
<div>""" + "".join(f'<input maxlength="1" inputmode="numeric" class="d">' for _ in range(6)) + """</div>
<button onclick="document.body.dataset.got=[...document.querySelectorAll('.d')].map(x=>x.value).join('')">Verify</button>"""
ONE = """<p>Check your email for a verification code</p><input name="email" type="email"><input id="otp-code" type="text">
<button onclick="document.body.dataset.got=document.getElementById('otp-code').value">Continue</button>"""
bad = 0
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page()
    for html, code in ((SPLIT, "482913"), (ONE, "X7K2QF")):
        pg.set_content(html)
        assert A.VERIFY_MSG.search(pg.inner_text("body")), "page not recognised"
        boxes = A._code_inputs(pg)
        A._type_code(pg, boxes, code)
        got = pg.evaluate("document.body.dataset.got")
        mi = pg.evaluate("(document.getElementById('mi')||{value:''}).value")
        ok = got == code and mi == ""
        print("OK " if ok else "BAD", len(boxes), "boxes ->", got, "| middle initial untouched:", mi == "")
        bad += not ok
    b.close()
# --- recovery of applications that were submitted but left at the emailed code
import tempfile, pathlib
from autoapply import recover as R
from autoapply.db import DB
_d = pathlib.Path(tempfile.mkdtemp()); _db = DB(str(_d / "t.db"))
_db.conn.execute("INSERT INTO jobs (key,company,status) VALUES ('k1','doordashusa','applied')"); _db.conn.commit()
_log = []
_n = R.recover(_db, _d, _log.append, lister=lambda: [{"subject": "Security code for your application to The Trade Desk"},
        {"subject": "Security code for your application to DoorDash USA"}, {"subject": "Welcome"}],
        exists=lambda slug: slug == "thetradedesk")
_ok = _n == 1 and "thetradedesk" in R.load_boards(_d).get("greenhouse", [])
print("OK " if _ok else "BAD", "recovery adds the unfinished company's board only ->", _n, R.slugs("Prolific Academic Ltd")[:3])
bad += not _ok
sys.exit(bad + bad_mail)
