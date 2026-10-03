"""Checks for the 2026-09-29-o fixes: free-tier writer limits, multiple-choice fallback, résumé upload that really registers
(including a 'Resume/CV is required' bounce), a form that rejects the submit, and accounts left unverified by earlier runs.
Run on its own:  python tests/fix_checks.py      (also run by tests/unit_checks.py)"""
from __future__ import annotations

import functools
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import autoapply.writer as W                                              # noqa: E402
from autoapply.brain import Brain                                        # noqa: E402
from autoapply.sources import Job                                         # noqa: E402

CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
PROFILE = yaml.safe_load((ROOT / "profile.yaml").read_text())
problems: list[str] = []
TMP = Path(tempfile.mkdtemp())
W._USAGE_FILE = TMP / "usage.json"


def check(cond, msg):
    if not cond:
        problems.append(msg)


class Resp:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code, self._d, self.headers = status, data, headers or {}
        self.text = text or (json.dumps(data) if data is not None else "")

    def json(self):
        return self._d


def ok(content, tokens=1000):
    return Resp(200, {"choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": {"total_tokens": tokens}})


# ------------------------------------------------------------------------------------------------ writer
def writer_checks():
    os.environ["GROQ_API_KEY"] = "x"
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ["GITHUB_ACTIONS"] = "true"
    try:
        w = W.Writer(CFG, PROFILE, ROOT)
    finally:
        os.environ.pop("GITHUB_ACTIONS", None)
    names = [p["name"] for p in w.providers]
    check(names and all(n.startswith("groq") for n in names), f"on GitHub with only a Groq key, only Groq models should be used: {names}")
    check("ollama" not in names and not any(n.startswith("gemini") for n in names), "keyless / local-only providers must be left out")
    check(len(w.system) < 19000, f"writer prompt too big for Groq's free per-minute limit: {len(w.system)} chars")
    check("OFF" not in w.status_line() and "groq" in w.status_line(), w.status_line())

    # daily limit on the first model -> set aside, next model answers; the next request skips the exhausted model at once
    seen = []
    daily = Resp(429, text='{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` on tokens per day (TPD): '
                            'Limit 200000, Used 199900, Requested 5000. Please try again in 1h2m3.5s."}}')
    def post(url, headers=None, json=None, timeout=None):
        seen.append(json["model"])
        return daily if json["model"] == "openai/gpt-oss-120b" else ok("Plain answer from my background.")
    old = requests.post
    requests.post = post
    try:
        out = w._complete([{"role": "user", "content": "q"}], 500, log=lambda *a: None)
        check(out == "Plain answer from my background.", f"fallback to the next free model failed: {out!r}")
        check(w.limiters["groq"].exhausted() and 3700 < w.limiters["groq"].cooling() <= 3724, "daily-limited model not set aside until it resets")
        seen.clear()
        w._complete([{"role": "user", "content": "q"}], 500, log=lambda *a: None)
        check("openai/gpt-oss-120b" not in seen, f"an exhausted model was asked again: {seen}")
    finally:
        requests.post = old

    # per-minute limit with a short wait -> same model after the wait (no provider change)
    W._save_usage({"_day": time.strftime("%Y-%m-%d")})
    w2 = W.Writer(CFG, PROFILE, ROOT)
    w2.providers = [p for p in w2.providers if p["name"] == "groq-llama"]
    calls, slept = [], []
    def post2(url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return Resp(429, text='{"error":{"message":"Rate limit reached on tokens per minute (TPM): Limit 8000. Please try again in 4.2s."}}')
        return ok("Second try worked.")
    old_sleep = W.time.sleep
    W.time.sleep = lambda s: slept.append(s)
    requests.post = post2
    try:
        out = w2._complete([{"role": "user", "content": "q"}], 500, log=lambda *a: None)
        check(out == "Second try worked." and len(calls) == 2 and slept and 4 < slept[0] < 6, f"short per-minute wait not handled: {out!r} {calls} {slept}")
    finally:
        requests.post = old
        W.time.sleep = old_sleep

    # 413 too large -> next model, not dead
    W._save_usage({"_day": time.strftime("%Y-%m-%d")})
    w3 = W.Writer(CFG, PROFILE, ROOT)
    def post3(url, headers=None, json=None, timeout=None):
        return Resp(413, text="Request too large") if json["model"] == "openai/gpt-oss-120b" else ok("Smaller model answered.")
    requests.post = post3
    try:
        out = w3._complete([{"role": "user", "content": "q"}], 500, log=lambda *a: None)
        check(out == "Smaller model answered." and "groq" not in w3.dead, f"413 handling wrong: {out!r} dead={w3.dead}")
    finally:
        requests.post = old

    # nothing thrown away: an answer with a number that isn't in the facts is revised once, then used
    job = Job("greenhouse", "acme", "1", "Operations Coordinator", "Denver, CO", "", "", "Coordinate vendors and schedules.")
    replies = iter(["I cut costs by 47% at Roaring Fork Property Group.", "I cut costs by 47% at Roaring Fork Property Group."])
    requests.post = lambda url, headers=None, json=None, timeout=None: ok(next(replies))
    try:
        got = w3.answer(job, "Acme", "Why are you a fit for this role?", None, log=lambda *a: None)
        check(bool(got) and "Roaring Fork" in got, f"an imperfect answer must still be used, got {got!r}")
    finally:
        requests.post = old

    # multiple-choice pick
    requests.post = lambda url, headers=None, json=None, timeout=None: ok("2")
    try:
        got = w3.choose(job, "Acme", "Which best describes your current employment?", ["Employed full-time", "Not currently employed", "Student"])
        check(got == "Not currently employed", f"choose() picked {got!r}")
        requests.post = lambda url, headers=None, json=None, timeout=None: ok("1, 3")
        got = w3.choose(job, "Acme", "Which tools have you used?", ["Excel", "SAP", "SQL"], multi=True)
        check(got == ["Excel", "SQL"], f"choose(multi) picked {got!r}")
        requests.post = lambda url, headers=None, json=None, timeout=None: ok("0")
        check(w3.choose(job, "Acme", "Pick one", ["A", "B"]) is None, "choose() must return None for 0")
    finally:
        requests.post = old

    # retry-after parsing
    check(abs(W._retry_seconds(Resp(429, text="try again in 9m59.5s")) - 599.5) < 0.01, "retry time 9m59.5s")
    check(abs(W._retry_seconds(Resp(429, text="try again in 590ms")) - 0.59) < 0.001, "retry time 590ms")
    check(W._retry_seconds(Resp(429, text="nope", headers={"retry-after": "7"})) == 7, "retry-after header")
    print("ok  writer: only keyed Groq models on GitHub, daily limits set aside, minute limits waited out, answers never discarded, multiple-choice picks")


# ------------------------------------------------------------------------------------------------ form answers
def answer_checks():
    cfg = json.loads(json.dumps(CFG))
    b = Brain(cfg, PROFILE, ROOT, lambda *a: None)
    b.writer = None
    job = Job("greenhouse", "acme", "1", "Operations Coordinator", "Denver, CO", "", "", "")
    b._job = job
    # résumé box recognised by id/name, by container words, or as the only plain upload box
    f1 = [{"id": "a", "kind": "file", "label": "Attach", "hint": "resume", "required": True},
          {"id": "b", "kind": "file", "label": "Attach", "hint": "cover_letter", "required": False}]
    plan = b.map_fields(job, f1, "")
    check(plan["answers"].get("a") == "RESUME" and plan["answers"].get("b") != "RESUME", f"résumé by id: {plan}")
    b.facts.setdefault("linkedin", "https://www.linkedin.com/in/example")
    fl = [{"id": "li", "kind": "radio", "label": "Do you have a personal LinkedIn profile? ✱", "required": True, "options": ["Yes", "No"]}]
    plan = b.map_fields(job, fl, "")
    check(plan["answers"].get("li") == "Yes" and not plan["unanswerable_required"], f"has-LinkedIn question: {plan}")
    f2 = [{"id": "a", "kind": "file", "label": "Upload", "hint": "", "required": True},
          {"id": "t", "kind": "text", "label": "First Name", "required": True}]
    plan = b.map_fields(job, f2, "")
    check(plan["answers"].get("a") == "RESUME" and "a" not in plan["unanswerable_required"], f"the only upload box must get the résumé: {plan}")
    f3 = [{"id": "x", "kind": "file", "label": "Autofill application from resume", "hint": "", "required": False},
          {"id": "y", "kind": "file", "label": "Resume/CV*", "hint": "resume", "required": True}]
    plan = b.map_fields(job, f3, "")
    check(plan["answers"].get("y") == "RESUME" and "x" not in plan["answers"], f"autofill box must not get a second résumé: {plan}")
    f4 = [{"id": "p", "kind": "file", "label": "Transcript", "hint": "", "required": False}]
    check(b.map_fields(job, f4, "")["answers"] == {}, "a transcript box must not get the résumé")

    # a required dropdown no rule covers -> the writer picks from the options; never for demographics or waivers
    class _Pick:
        providers = [1]
        def ready(self): return True
        def choose(self, job, company, q, opts, multi=False, log=print): return [opts[-1]] if multi else opts[-1]
        def short_answer(self, job, company, q, max_chars=150, log=print): return "Glenwood Springs, CO"
    b.writer = _Pick()
    odd = {"id": "q", "kind": "select", "label": "Which of our three service lines interests you most?*", "options": ["Retail", "Wholesale", "Logistics"], "required": True}
    plan = b.map_fields(job, [odd], "")
    check(plan["answers"].get("q") == "Logistics" and not plan["unanswerable_required"], f"writer fallback for an odd dropdown: {plan}")
    demo = {"id": "g", "kind": "select", "label": "Gender identity (optional survey)", "options": ["Man", "Woman", "Non-binary"], "required": True}
    check(b._llm_fill(demo) is None, "demographic questions must never be answered by the writer")
    legal = {"id": "l", "kind": "radio", "label": "Do you agree to the arbitration agreement?", "options": ["Yes", "No"], "required": True}
    check(b._llm_fill(legal) is None, "legal waivers must never be answered by the writer")
    short = {"id": "s", "kind": "text", "label": "Hometown", "required": True}
    check(b._llm_fill(short) == "Glenwood Springs, CO", "short required text box not answered")
    b.writer = None
    print("ok  answers: résumé box always found, odd required questions answered from your facts, demographics/waivers never guessed")


# ------------------------------------------------------------------------------------------------ browser: uploads, rejected submits, accounts
GH_FORM = """<!doctype html><html><body><h1>Treasury Operations Associate</h1>
<form id=f onsubmit="return false">
 <div class=field><label for=first_name>First Name<span>*</span></label><input id=first_name required></div>
 <div class=field><label for=last_name>Last Name<span>*</span></label><input id=last_name required></div>
 <div class=field><label for=email>Email<span>*</span></label><input id=email type=email required></div>
 <div class=field><label for=phone>Phone</label><input id=phone type=tel></div>
 <div class="file-upload"><div id="upload-label-resume" class="label">Resume/CV<span>*</span></div>
   <div class="file-upload__wrapper"><div class=buttons><button type=button id=attach>Attach</button><button type=button>Dropbox</button>
   <button type=button>Google Drive</button><button type=button>Enter manually</button></div>
   <input id=resume type=file class="visually-hidden" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)" accept=".pdf"></div>
   <div id=chip></div><p class=helper>Accepted file types: pdf, doc, docx, txt, rtf</p><p id=err class=error></p></div>
 <div class="file-upload"><div id="upload-label-cover_letter" class="label">Cover Letter</div>
   <div class="file-upload__wrapper"><button type=button>Attach</button><input id=cover_letter type=file style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)"></div></div>
 <button type=submit id=go>Submit application</button>
</form>
<script>
const MODE = new URLSearchParams(location.search).get('mode') || 'normal';
let has = false, attachClicked = false, submits = 0;
document.getElementById('attach').onclick = () => { attachClicked = true; document.getElementById('resume').click(); };
document.getElementById('resume').addEventListener('change', e => {
  const f = e.target.files[0];
  if (!f) return;
  if (MODE === 'chooser_only' && !attachClicked) { e.target.value = ''; return; }   // widget ignores files it didn't ask for
  has = true; document.getElementById('chip').innerText = f.name; document.getElementById('err').innerText = '';
});
document.getElementById('go').onclick = () => {
  submits += 1;
  if (MODE === 'forget_once' && submits === 1) { has = false; document.getElementById('chip').innerText = ''; }
  if (!has) { document.getElementById('err').innerText = 'Resume/CV is required.'; document.getElementById('upload-label-resume').className = 'label error'; return; }
  document.body.innerHTML = '<h1>Thank you for applying!</h1><p>We have received your application.</p>';
};
</script></body></html>"""

ACCOUNT_PAGES = {
    "signin.html": """<!doctype html><html><body><h1>Sign In</h1><input type=email id=e><input type=password id=p>
<button id=b>Sign In</button><p id=m></p><a href="#" id=resend style="display:none">Resend verification email</a>
<script>
document.getElementById('b').onclick = () => {
  if (localStorage.getItem('verified') === '1') { location.href = 'form.html'; return; }
  document.getElementById('m').innerText = 'Your account has not been verified. Please verify your email before signing in.';
  document.getElementById('resend').style.display = 'inline';
};
document.getElementById('resend').onclick = () => { localStorage.setItem('resent', '1'); };
</script></body></html>""",
    "verify.html": "<html><body><script>localStorage.setItem('verified','1')</script><p>Verified</p></body></html>",
    "form.html": "<html><body><h1>Application</h1><input type=text id=a><input type=text id=b></body></html>",
}


def _serve(files: dict) -> tuple:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        (d / name).write_text(body)

    class Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Q, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def browser_checks():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("skip browser checks (no playwright)")
        return
    from autoapply import submit as S, auth as A, mailbox as MB
    srv, base = _serve({"gh.html": GH_FORM, **ACCOUNT_PAGES})
    pdf = ROOT / "briandelgado_resume.pdf"
    cfg = json.loads(json.dumps(CFG))
    cfg["cover_letters"] = False
    brain = Brain(cfg, PROFILE, ROOT, lambda *a: None)
    brain.writer = None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context()
            for mode, want in (("normal", "confirmed"), ("chooser_only", "confirmed"), ("forget_once", "confirmed")):
                page = ctx.new_page()
                page.goto(f"{base}/gh.html?mode={mode}")
                logs, clicks = [], []
                job = Job("greenhouse", "lithic", str(len(mode)), "Treasury Operations Associate", "New York, NY", f"{base}/gh.html", f"{base}/gh.html", "x")
                shot = TMP / f"{mode}.png"
                try:
                    got = S.apply(page, job, brain, "", {"RESUME": pdf, "_LETTER_MAKER": lambda t: None}, shot, False, logs.append,
                                  None, on_click=lambda: clicks.append(1))
                except Exception as e:
                    got = f"{type(e).__name__}: {e}"
                check(got == want, f"Greenhouse-style upload ({mode}): got {got!r}; log: {' | '.join(logs)[-400:]}")
                if mode == "forget_once":
                    check(len(clicks) == 2, f"résumé-missing bounce should lead to exactly one more submit, clicks={len(clicks)}")
                page.close()

            # a label that is only a <div id="upload-label-resume"> is still read as the résumé box
            page = ctx.new_page()
            page.goto(f"{base}/gh.html")
            fields = S.extract(page)
            files = [f for f in fields if f["kind"] == "file"]
            check(files and "Resume" in files[0]["label"] and files[0].get("hint", "").startswith("resume"), f"file label/hint: {files}")
            page.close()

            # a form that rejects the submit (validation error, still on the form) is NOT counted as sent
            page = ctx.new_page()
            page.set_content("<form onsubmit='return false'><input id=a type=text><input id=b type=email><input id=c type=file>"
                             "<p class=error id=e></p><button id=s type=submit>Submit application</button></form>"
                             "<script>document.getElementById('s').onclick=()=>{document.getElementById('e').innerText='Email is required.'}</script>")
            try:
                S.submit(page, timeout_ms=4000, on_click=lambda: None)
                check(False, "rejected form reported as submitted")
            except S.NotSubmitted:
                pass
            except Exception as e:
                check(False, f"rejected form raised {type(e).__name__} instead of NotSubmitted")
            page.close()

            # an account left unverified by an earlier run: resend, read the email, verify, sign in
            page = ctx.new_page()
            page.goto(f"{base}/signin.html")
            acc = A.Accounts({"accounts": {"email": "delgado@alumni.usc.edu"}}, TMP)
            acc.password = "Pw-123456!"
            acc.enabled = True
            acc.known[acc.host(page.url)] = {"state": "created"}
            olds = (MB.configured, MB.wait_for_verification)
            MB.configured = lambda: True
            MB.wait_for_verification = lambda **k: {"link": f"{base}/verify.html", "code": None}
            logs = []
            try:
                A.handle(page, acc, logs.append, url_after=f"{base}/signin.html")
                check(not A.is_auth_page(page), f"unverified account not recovered; log: {logs}")
            except A.AuthBlocked as e:
                check(False, f"unverified account: {e}; log: {logs}")
            finally:
                MB.configured, MB.wait_for_verification = olds
            page.close()
            # Mid-application email OTP: the code is requested on a later wizard step,
            # not during account creation. The browser must stay on the application and resume.
            page = ctx.new_page()
            page.set_content("""<!doctype html><html><body>
              <div id="step1"><h1>Application step 1</h1>
                <label>First Name <input id="first"></label>
                <label>Last Name <input id="last"></label>
                <label>Email <input id="email" type="email"></label>
                <label>Resume/CV <input id="resume" type="file"></label>
                <button id="next" type="button">Next</button>
              </div>
              <div id="step2" style="display:none">
                <h1>Verify your application</h1>
                <p>We sent a verification code to your email. Enter the code to continue.</p>
                <input id="otp" name="verification_code" autocomplete="one-time-code" inputmode="numeric">
                <button id="verify" type="button">Verify code</button>
              </div>
              <div id="done" style="display:none"><h1>Thank you for applying!</h1></div>
              <script>
                next.onclick = () => { step1.style.display='none'; step2.style.display='block'; };
                verify.onclick = () => {
                  if (otp.value === '731204') { step2.style.display='none'; done.style.display='block'; }
                };
              </script>
            </body></html>""")
            old_configured, old_wait = MB.configured, MB.wait_for_verification
            old_gh = getattr(MB, "wait_for_greenhouse_code", None)
            MB.configured = lambda: True
            MB.wait_for_verification = lambda **k: (_ for _ in ()).throw(AssertionError("Greenhouse must not use the generic mailbox verifier"))
            MB.wait_for_greenhouse_code = lambda **k: {"link": None, "code": "731204", "provider": "greenhouse"}
            logs = []
            try:
                job = Job("greenhouse", "otpco", "otp-1", "Treasury Operations Associate", "Denver, CO",
                          "http://example.test/job", "http://example.test/apply", "x")
                got = S.apply(page, job, brain, "", {"RESUME": pdf, "_LETTER_MAKER": lambda t: None},
                              TMP / "midstep_otp.png", False, logs.append, None)
                check(got == "confirmed", f"mid-application emailed OTP did not resume: {got!r}; log: {' | '.join(logs)[-500:]}")
                check(any("application requested a verification code" in x for x in logs),
                      "mid-application OTP path never polled the mailbox")
            except Exception as e:
                check(False, f"mid-application OTP crashed: {type(e).__name__}: {e}")
            finally:
                MB.configured, MB.wait_for_verification = old_configured, old_wait
                if old_gh is not None:
                    MB.wait_for_greenhouse_code = old_gh
                page.close()

            # Greenhouse post-submit email security code: the first Submit reveals the code page,
            # then the code must be entered and the application must be submitted again.
            page = ctx.new_page()
            page.set_content("""<!doctype html><html><body>
              <div id="form"><h1>Application</h1><input id="name" value="Test"><button id="submit" type="button">Submit Application</button></div>
              <div id="verify" style="display:none">
                <h1>Greenhouse Recruiting</h1>
                <p>Copy and paste this code into the security code field on your application:</p>
                <input id="otp" name="verification_code" autocomplete="one-time-code">
                <p>After you enter the code, resubmit your application.</p>
                <button id="submit2" type="button">Submit Application</button>
              </div>
              <div id="done" style="display:none"><h1>Thank you for applying!</h1><p>Application submitted successfully.</p></div>
              <footer>© 2026 Greenhouse</footer>
              <script>
                let submits = 0;
                submit.onclick = () => {
                  submits++;
                  form.style.display='none';
                  verify.style.display='block';
                };
                submit2.onclick = () => {
                  submits++;
                  if (otp.value === 'I49GcNQ9') {
                    verify.style.display='none'; done.style.display='block';
                  }
                };
                window.getSubmitCount = () => submits;
              </script>
            </body></html>""")
            old_configured, old_wai = MB.configured, MB.wait_for_verification
            old_gh = getattr(MB, "wait_for_greenhouse_code", None)
            MB.configured = lambda: True
            MB.wait_for_verification = lambda **k: (_ for _ in ()).throw(AssertionError("Greenhouse must not use the generic mailbox verifier"))
            MB.wait_for_greenhouse_code = lambda **k: {"link": None, "code": "I49GcNQ9", "provider": "greenhouse"}
            logs = []
            try:
                import autoapply.submit as S
                got = S.submit(page, timeout_ms=7000, on_click=lambda: None)
                count = page.evaluate("window.getSubmitCount()")
                check(got == "confirmed", f"Greenhouse security-code resubmit did not complete: {got!r}; log: {' | '.join(logs)[-700:]}")
                check(count == 2, f"Greenhouse security-code flow did not resubmit exactly once after code entry: {count}")
                check(page.locator("#done").is_visible(), "Greenhouse security-code resubmit did not reach confirmation")
                check(any("resubmitting application" in x for x in logs),
                      "Greenhouse security-code path never logged the required resubmit")
            except Exception as e:
                check(False, f"Greenhouse security-code resubmit crashed: {type(e).__name__}: {e}")
            finally:
                MB.configured, MB.wait_for_verification = old_configured, old_wai
                if old_gh is not None:
                    MB.wait_for_greenhouse_code = old_gh
                page.close()

            # Regression: the security-code input can disappear immediately after
            # acceptance while Greenhouse still requires a second Submit Application click.
            page = ctx.new_page()
            page.set_content("""<!doctype html><html><body>
              <div id="form"><button id="submit" type="button">Submit Application</button></div>
              <div id="verify" style="display:none"><h1>Greenhouse Recruiting</h1>
                <p>Enter the security code we emailed you. After you enter the code, resubmit your application.</p>
                <input id="otp" name="verification_code" autocomplete="one-time-code">
              </div>
              <div id="done" style="display:none"><h1>Application submitted successfully.</h1></div>
              <script>
                let submits = 0;
                submit.onclick = () => {
                  submits++;
                  if (submits === 1) {
                    form.style.display='none'; verify.style.display='block';
                    setTimeout(() => { otp.remove(); verify.innerHTML += '<button id="submit2" type="button">Submit Application</button>'; submit2.onclick=() => { submits++; verify.style.display='none'; done.style.display='block'; }; }, 50);
                  }
                };
                window.getSubmitCount = () => submits;
              </script>
            </body></html>""")
            old_configured, old_wait = MB.configured, MB.wait_for_verification
            MB.configured = lambda: True
            MB.wait_for_verification = lambda **k: {"link": None, "code": "731204"}
            logs = []
            try:
                got = S.submit(page, timeout_ms=9000, on_click=lambda: None)
                check(got == "confirmed", f"Greenhouse disappearing-code resubmit failed: {got!r}; log: {' | '.join(logs)[-700:]}")
                check(page.evaluate("window.getSubmitCount()") == 2, "Greenhouse disappearing-code path did not submit twice")
            except Exception as e:
                check(False, f"Greenhouse disappearing-code regression crashed: {type(e).__name__}: {e}")
            finally:
                MB.configured, MB.wait_for_verification = old_configured, old_wait
                page.close()

            b.close()
    finally:
        srv.shutdown()
    print("ok  browser: résumé upload verified (plain, widget-only, forgotten-once), rejected submit not counted, unverified account recovered")


def run_all() -> list[str]:
    for fn in (writer_checks, answer_checks, browser_checks):
        try:
            fn()
        except Exception as e:
            import traceback
            problems.append(f"{fn.__name__} crashed: {type(e).__name__}: {e}\n{traceback.format_exc()[-700:]}")
    return problems


if __name__ == "__main__":
    out = run_all()
    print("RESULT:", "ALL AS EXPECTED" if not out else "PROBLEMS:\n  - " + "\n  - ".join(out))
    sys.exit(1 if out else 0)