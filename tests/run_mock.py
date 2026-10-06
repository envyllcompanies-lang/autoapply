"""End-to-end test of the real pipeline (scoring, résumé assembly, form answering, free-LLM writer, submit)
against local mock application forms and a mock OpenAI-compatible LLM server. No internet, no keys, no cost."""
import copy, functools, http.server, json, os, re, shutil, sqlite3, sys, threading, urllib.parse
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
work = ROOT / "work"; shutil.rmtree(work, ignore_errors=True); (work / "site").mkdir(parents=True)

SUBS, CALLS, HITS, MATCHED, BATCHES = [], {}, {}, [], []
CLEAN = ("Capitol Edge is the clearest example: I built it alone, about 6,000 lines of production code, and I keep it "
         "running. I like ops work where the process is the product, and this role looks like that.")
FAB = "I raised revenue 45% at Otto's and ran it all in Rippling. This role is the same kind of work."
LETTER = ("Dear Acme team,\n\nCapitol Edge started as a solo build and now runs in production, roughly 6,000 lines of code "
          "that I wrote and maintain. The same habits, documenting the process and fixing what breaks, are what I'd "
          "bring to this role.\n\nBrian")

def reply(msgs):
    first, last = msgs[1]["content"], msgs[-1]["content"]
    if msgs[0]["content"].startswith("You screen job postings"):          # the résumé-vs-posting match check (one or several postings per call)
        posts = re.findall(r"POSTING (\d+)\n([^\n]*)\n(.*?)(?=\n\nPOSTING \d+\n|\Z)", first, re.S)
        MATCHED.extend(t for _n, t, _b in posts)
        BATCHES.append(len(posts))
        return json.dumps([{"n": int(n), "fit": 40, "why": "needs a nursing licence the candidate does not have"} if "MARK_LOW" in body else
                           {"n": int(n), "fit": 86, "why": "operations and analysis background fits"} for n, _t, body in posts])

    if "Multiple-choice question on the application form" in first: return "1"
    if "Application form field (short text)" in first: return "N/A"
    if last.startswith("Revise."): return CLEAN
    if "Write my cover letter" in first: return LETTER
    q = first.split("Application question:\n")[1]
    if "failed" in q: return "CANNOT_ANSWER"
    return FAB if "MARK_FAB" in first else CLEAN

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(work / "site"), **k)
    def log_message(self, *a): pass
    def do_GET(self):
        HITS[self.path.split("?")[0]] = HITS.get(self.path.split("?")[0], 0) + 1
        if self.path.startswith("/thanks.html"):
            SUBS.append({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
        super().do_GET()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prov = self.path.split("/")[1]; CALLS[prov] = CALLS.get(prov, 0) + 1
        if prov == "modelfix" and body.get("model") == "old":
            self.send_response(404); self.end_headers(); self.wfile.write(b'{"error":{"message":"This model models/old is no longer available"}}'); return
        if prov == "empty":
            out = json.dumps({"choices": [{"message": {"content": ""}}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(out); return
        if prov == "bad":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"bad key"}'); return
        out = json.dumps({"choices": [{"message": {"content": reply(body["messages"])}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(out)

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def sel(id_, label, opts):
    o = "".join(f"<option>{x}</option>" for x in ["Select..."] + opts)
    return f'<div><label for="{id_}">{label}</label><select id="{id_}" name="{id_}">{o}</select></div>'
def radio(name, legend, opts=("Yes", "No")):
    r = "".join(f'<label><input type="radio" name="{name}" value="{x}"{" required" if i == 0 else ""}> {x}</label>' for i, x in enumerate(opts))
    return f"<fieldset><legend>{legend}</legend>{r}</fieldset>"

def page(tag, essay="Why do you want to work at Acme? (max 400 characters) *", top="", extra="", action="thanks.html"):
    (work / "site" / f"{tag}.html").write_text(f"""<!doctype html><html><body><h1>Operations Associate — Acme</h1>{top}
<form id="application-form" action="{action}" method="get"><input type="hidden" name="tag" value="{tag}">
<div><label for="first_name">First Name *</label><input id="first_name" name="first_name" required></div>
<div><label for="last_name">Last Name *</label><input id="last_name" name="last_name" required></div>
<div><label for="email">Email *</label><input id="email" type="email" name="email" required></div>
<div><label for="phone">Phone</label><input id="phone" type="tel" name="phone"></div>
<div><label>Resume/CV *</label><button type="button">Attach</button><input type="file" id="resume" name="resume" style="display:none" required></div>
<div><label>Cover Letter *</label><input type="file" required id="cover" name="cover" style="opacity:0;width:1px"></div>
{sel("auth", "Are you legally authorized to work in the United States? *", ["Yes", "No"])}
{radio("spons", "Will you now or in the future require visa sponsorship? *")}
<div><label id="loc-l">Location (City) *</label><input role="combobox" aria-labelledby="loc-l" aria-autocomplete="list" id="loc" name="loc" required
  onfocus="document.getElementById('lb').style.display='block'"><ul role="listbox" id="lb" style="display:none">
  <li role="option" onclick="loc.value=this.innerText;lb.style.display='none'">Denver, Colorado, United States</li>
  <li role="option" onclick="loc.value=this.innerText;lb.style.display='none'">Glenwood Springs, Colorado, United States</li></ul></div>
<div><label for="why">{essay}</label><textarea id="why" name="why" maxlength="400" required></textarea></div>
<div><label for="sql">How many years of experience do you have with SQL? *</label><input type="number" id="sql" name="sql" required></div>
{radio("uspers", "Are you a U.S. citizen, lawful permanent resident, or protected individual? *")}
{sel("citstat", "What is your citizenship status?", ["U.S. Citizen", "Permanent Resident (Green Card)", "Visa Holder"])}
{radio("aiuse", "Did you use AI tools to help complete this application? *")}
<div><label><input type="checkbox" name="aicert"> I certify that I did not use AI to complete this application</label></div>
{sel("gender", "Gender", ["Male", "Female", "Decline To Self Identify"])}
{sel("hisp", "Hispanic/Latino", ["Yes", "No", "Decline To Self Identify"])}
{sel("trans", "Do you identify as transgender?", ["Yes", "No", "Decline to self identify"])}
{sel("orient", "Sexual Orientation", ["Heterosexual/Straight", "Gay/Lesbian", "Bisexual", "I prefer not to say"])}
{sel("vet", "Veteran Status", ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran", "I don't wish to answer"])}
{sel("disab", "Disability Status", ["Yes, I have a disability, or have had one in the past", "No, I do not have a disability and have not had one in the past", "I do not want to answer"])}
<div><label for="cl">Current location *</label><input id="cl" name="cl" type="text" required autocomplete="off"
  oninput="document.getElementById('cll').style.display='block'" onblur="setTimeout(function(){{if(!cl.dataset.ok)cl.value=''}},400)">
  <ul id="cll" class="dropdown-results" style="display:none"><li onclick="cl.value=this.innerText;cl.dataset.ok=1;cll.style.display='none'">Glenwood Springs, CO, USA</li></ul></div>
<div><label for="addr">Street Address *</label><input id="addr" name="addr" required></div>
<div><label for="zip">Zip / Postal Code *</label><input id="zip" name="zip" required></div>
<div><label for="cob">Country of Birth</label><input id="cob" name="cob"></div>
{sel("trav", "What percentage of travel are you comfortable with?", ["None", "0-10%", "10-25%", "25-50%", "50-75%", "75-100%"])}
<div><label for="sal">Desired annual salary (USD) *</label><input type="number" id="sal" name="sal" required></div>
{extra}
<div><label><input type="checkbox" name="consent" required> I agree to the privacy policy *</label></div>
<button type="submit">Submit Application</button></form></body></html>""")

RELOC = sel("reloc", "Are you willing to relocate for this role? *", ["Yes", "No"])
page("main"); page("fab"); page("reloc_no", extra=RELOC); page("reloc_yes", extra=RELOC); page("unknown", extra='<div><label for="fc">Favorite color? *</label><input id="fc" name="fc" required></div>')
page("aggform")
(work / "site" / "listing.html").write_text('<!doctype html><html><body><h1>Operations Coordinator at Zeta Corp</h1><p>Great job.</p><a href="aggform.html">Apply Now</a></body></html>')
(work / "site" / "remoteok").mkdir(exist_ok=True)
(work / "site" / "remoteok" / "api").write_text(json.dumps([
    {"legal": "remoteok terms"},
    {"id": "agg1", "position": "Operations Coordinator", "company": "Zeta Corp", "location": "Remote US", "apply_url": "http://127.0.0.1:8765/listing.html",
     "description": "<p>Remote. You will drive process improvement using Excel and SQL, coordinate vendor work. 2 years of experience.</p>"},
    {"id": "agg2", "position": "Operations Coordinator II", "company": "Zeta Corp", "location": "Remote US", "apply_url": "http://127.0.0.1:8765/listing.html",
     "description": "<p>Remote. Same posting listed twice. Excel, SQL, vendor coordination, process improvement.</p>"}]))
page("fail", essay="Tell me about a time you failed or had a conflict at work. *")
page("noai", top="<p>Please do not use AI tools to write your application.</p>")
page("captcha", top='<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x" width="304" height="78"></iframe>')
page("sec", action="secure.html")                      # answers the submit with an emailed-security-code prompt
(work / "site" / "secure.html").write_text("<!doctype html><html><body><h2>Security code</h2><p>Enter the 8-character code that was sent to your email.</p>"
    + "".join(f'<input id="security-input-{i}" maxlength="1" style="width:30px">' for i in range(8))
    + "<button type='button' onclick=\"if([...document.querySelectorAll('input')].map(i=>i.value).join('')==='AB12CD34')document.body.innerHTML='<h1>Thank you for applying</h1><p>Your application has been submitted.</p>'\">Verify</button></body></html>")
page("unclearA", action="unclear_done.html"); page("unclearB", action="unclear_done.html")   # submit shows no confirmation
(work / "site" / "unclear_done.html").write_text("<!doctype html><html><body><p>Working on it...</p></body></html>")
for i in range(1, 5): page(f"cap{i}")
page("stub")
for i in range(1, 6): page(f"gate{i}", top='<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x" width="304" height="78"></iframe>')
page("gateok")

(work / "site" / "wizard.html").write_text("""<!doctype html><html><body><h1>Program Coordinator — Acme</h1>
<div id="auth-create" style="display:none"><h2>Create Account</h2>
 <div><label for="ce">Email Address *</label><input id="ce" type="email"></div>
 <div><label for="cp">Password *</label><input id="cp" type="password"></div>
 <div><label for="cv">Verify New Password *</label><input id="cv" type="password"></div>
 <label><input type="checkbox" id="agree"> I agree to the terms and privacy notice</label>
 <button id="create" type="button">Create Account</button></div>
<div id="auth-wait" style="display:none"><p>Please check your email to verify your account.</p></div>
<div id="auth-signin" style="display:none"><h2>Sign In</h2>
 <div><label for="se">Email Address *</label><input id="se" type="email"></div>
 <div><label for="sp">Password *</label><input id="sp" type="password"></div>
 <button id="signin" type="button">Sign In</button><p id="autherr"></p></div>
<div id="s1" style="display:none"><h2>My Information</h2>
 <div><label for="first_name">First Name *</label><input id="first_name" required></div>
 <div><label for="last_name">Last Name *</label><input id="last_name" required></div>
 <div><label for="email">Email *</label><input id="email" type="email" required></div>
 <div><label for="phone">Phone *</label><input id="phone" type="tel" required></div>
 <div><label>Resume/CV *</label><input type="file" id="resume" style="display:none" required></div>
 <div><label id="hear-l">How did you hear about us? *</label>
  <button type="button" id="hear" aria-haspopup="listbox" aria-labelledby="hear-l">Select One</button>
  <ul role="listbox" id="hearlist" style="display:none"><li role="option">LinkedIn</li><li role="option">Job board</li><li role="option">Referral</li></ul></div>
 <button type="button" id="n1">Save and Continue</button></div>
<div id="s2" style="display:none"><h2>Application Questions</h2>
 <div><label for="auth">Are you legally authorized to work in the United States? *</label><select id="auth"><option>Select...</option><option>Yes</option><option>No</option></select></div>
 <fieldset><legend>Will you now or in the future require visa sponsorship? *</legend><label><input type="radio" name="spons" value="Yes"> Yes</label><label><input type="radio" name="spons" value="No"> No</label></fieldset>
 <div><label for="why">Why do you want to work at Acme? (max 400 characters) *</label><textarea id="why" maxlength="400" required></textarea></div>
 <button type="button" id="n2">Save and Continue</button></div>
<div id="s3" style="display:none"><h2>Voluntary Disclosures</h2>
 <div><label for="gender">Gender</label><select id="gender"><option>Select...</option><option>Male</option><option>Female</option><option>Decline to self-identify</option></select></div>
 <div><label for="vet">Veteran Status</label><select id="vet"><option>Select...</option><option>I am a protected veteran</option><option>I am not a protected veteran</option><option>I decline to self-identify</option></select></div>
 <button type="button" id="n3">Save and Continue</button></div>
<div id="s4" style="display:none"><h2>Review</h2><p>Please review your application.</p><button type="button" id="submitbtn">Submit</button></div>
<script>
const S=(k,v)=>v===undefined?localStorage.getItem(k):localStorage.setItem(k,v), $=id=>document.getElementById(id);
const ALL=['auth-create','auth-wait','auth-signin','s1','s2','s3','s4'];
function show(id){ALL.forEach(x=>$(x).style.display=x===id?'block':'none');}
if(location.search.includes('verify=1')){S('verified','1');document.body.innerHTML='<p>Email verified.</p>';}
else{ if(S('signed'))show('s1'); else if(S('verified'))show('auth-signin'); else if(S('acct'))show('auth-wait'); else show('auth-create'); }
$('create').onclick=()=>{if(!$('ce').value||$('cp').value!==$('cv').value||!$('agree').checked)return;S('acct',$('ce').value);S('pw',$('cp').value);show('auth-wait');};
$('signin').onclick=()=>{if($('sp').value===S('pw')&&$('se').value===S('acct')){S('signed','1');show('s1');}else $('autherr').innerText='Incorrect email or password';};
$('hear').onclick=()=>{$('hearlist').style.display='block';};
document.querySelectorAll('#hearlist li').forEach(li=>li.onclick=()=>{$('hear').innerText=li.innerText;$('hearlist').style.display='none';});
const need=(ids)=>ids.every(i=>$(i).value&&!/^Select/.test($(i).value));
$('n1').onclick=()=>{if(need(['first_name','last_name','email','phone'])&&$('hear').innerText!=='Select One')show('s2');};
$('n2').onclick=()=>{if(need(['auth','why'])&&document.querySelector('input[name=spons]:checked'))show('s3');};
$('n3').onclick=()=>show('s4');
$('submitbtn').onclick=()=>{const p=new URLSearchParams({tag:'wizard',first_name:$('first_name').value,email:$('email').value,phone:$('phone').value,
 hear:$('hear').innerText,auth:$('auth').value,spons:document.querySelector('input[name=spons]:checked').value,why:$('why').value,gender:$('gender').value,vet:$('vet').value});
 location.href='thanks.html?'+p.toString();};
</script></body></html>""")
(work / "site" / "thanks.html").write_text("<!doctype html><html><body><h1>Thank you for applying!</h1><p>We've received your application.</p></body></html>")

page("lowfit")
sys.path.insert(0, str(ROOT)); import wd_mock
WD_SRV, WD_BASE, WD_STATE = wd_mock.serve()
import autoapply.writer as _W; _W._USAGE_FILE = work / "usage.json"
from autoapply import main as M
from autoapply.sources import Job
B = "http://127.0.0.1:8765/"; GOOD = "Remote. You will drive process improvement using Excel and SQL, coordinate vendor work and build dashboards. 2 years of experience."
def J(i, title, page_, desc=GOOD, loc="Remote", co="acme", src="greenhouse"): return Job(src, co, str(i), title, loc, B + page_, B + page_, desc)
M.discover = lambda companies, log=print, base=None: [
    J(1, "Operations Associate", "main.html"),
    J(2, "Operations Manager", "main.html", "Requires 8+ years of experience in operations. Security clearance required."),
    J(3, "Operations Analyst", "captcha.html", loc="Denver, CO", co="captchaco"),
    J(4, "Business Analyst", "unknown.html"),
    J(5, "Business Analyst", "fail.html", co="failco"),              # essay the writer won't do -> your fallback_answer
    J(6, "Supply Chain Analyst", "noai.html", GOOD + " Please do not use AI tools to write your application."),
    J(7, "Logistics Coordinator", "fab.html", GOOD + " MARK_FAB", co="fabco"),
    J(8, "Senior Staff Engineer", "main.html", ""),
    J(9, "Operations Coordinator", "reloc_no.html", loc="Chicago, United States"),
    J(10, "Project Coordinator", "reloc_yes.html", loc="New York, NY"),
    J(11, "Operations Associate", "main.html", GOOD + " Pay range: $45,000 - $55,000 per year."),
    J(12, "Program Coordinator", "wizard.html", co="wizco"),
    J(13, "Operations Associate", "sec.html", co="secco"),                 # emailed security code after Submit -> read from the inbox, typed, confirmed
    J(14, "Operations Analyst", "unclearA.html", co="confirmco"),          # no confirmation page, but the company emails one
    J(15, "Operations Analyst", "unclearB.html", co="unconfco"),           # no confirmation anywhere
    J(16, "Operations Associate", "cap1.html", co="capco"),
    J(17, "Operations Coordinator", "cap2.html", co="capco"),
    J(18, "Project Coordinator", "cap3.html", co="capco"),
    J(19, "Program Coordinator", "cap4.html", co="capco"),                 # 4th at one employer: over the cap of 3
    J(21, "Operations Analyst", "lowfit.html", GOOD + " " + "Day-to-day duties are listed below. " * 12 + "MARK_LOW", co="lowco"),   # poor match: never applied to
    Job("workday", "wdco", "R-200", "Operations Analyst", "Remote", wd_mock.job_url(WD_BASE, "acme", "R-200"), wd_mock.job_url(WD_BASE, "acme", "R-200"), GOOD),
]

cfg = yaml.safe_load((ROOT.parent / "config.yaml").read_text())
cfg["aggregators"] = {"enabled": True, "sources": ["remoteok"], "base_urls": {"remoteok": B + "remoteok"}, "queries": [], "locations": [],
                      "jobboard": {"enabled": False}}          # (the real 1.4M-job snapshot is not part of this offline test)
cfg["facts"]["city"] = "Glenwood Springs"; cfg["search"]["delay_seconds"] = [0, 0]
cfg["search"]["per_run_cap"] = 50; cfg["search"]["max_per_company"] = 3; cfg["search"]["min_fit"] = 70
cfg["writer"]["providers"] = [
    {"name": "modelfix", "base_url": B + "modelfix", "model": ["old", "new"], "api_key_env": "TESTKEY", "rpm": 1000, "rpd": 1},
    {"name": "bad", "base_url": B + "bad", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000},
    {"name": "empty", "base_url": B + "empty", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000},
    {"name": "capped", "base_url": B + "capped", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000, "rpd": 2},
    {"name": "good", "base_url": B + "good", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000}]
os.environ["TESTKEY"] = "x"; os.environ["ACCOUNT_PASSWORD"] = "Test-Pass-123!"
import autoapply.mailbox as _MB
_MB.configured = lambda: True
_MB.wait_for_verification = lambda **k: {"link": B + "wizard.html?verify=1", "code": "AB12CD34" if k.get("require_code") else None}
_MB.find_confirmation = lambda name, since, timeout=90, log=print: "Thanks for applying to Acme!" if name.lower().startswith("confirmco") else None
_MB.scan_confirmations = lambda companies, since_ts, n=60: {}
MAILS = []
M.notify.send_email = lambda cfg, subject, text, log=print: MAILS.append((subject, text)) or True
cfg["accounts"] = {"enabled": True, "email": "delgado@alumni.usc.edu", "create_in_dry_run": False}
for f in ("profile.yaml", "about_me.md", "briandelgado_resume.pdf"): shutil.copy(ROOT.parent / f, work / f)
cfg["cover_letters"] = True          # the cover-letter scenarios need them on; a separate check below covers "off"
cfg["writer"]["cover_letter_min_words"] = 0

# 1) a leftover template placeholder must stop the run
prof = (work / "profile.yaml").read_text(); (work / "profile.yaml").write_text(prof + "\n# <TODO fill me>\nnote: '<TODO fill me>'\n")
(work / "config.yaml").write_text(yaml.safe_dump(cfg))
try:
    M.run(str(work / "config.yaml")); print("FAIL: placeholders not refused"); sys.exit(1)
except SystemExit as e:
    print("PLACEHOLDER GUARD OK ->", str(e).splitlines()[1].strip())
(work / "profile.yaml").write_text(prof)

# 2) real run. A queued Workday posting that this run's search does not return must still be tried (big sites only show the newest hits)
from autoapply.db import DB as _DB
_d = _DB(str(work / "applications.db"))
_d.add(Job("workday", "stubco", "R-77", "Operations Associate", "Remote", B + "stub.html", B + "stub.html", ""), "queued", score=80, reason="earlier run")
_d.conn.close()
M.run(str(work / "config.yaml"), dry_run="--dry" in sys.argv)
db = sqlite3.connect(work / "applications.db")
rows = {r[0].split(":")[-1]: r[1:] for r in db.execute("select key,status,score,reason from jobs")}
print("\nDB:"); [print("  ", k, v) for k, v in sorted(rows.items())]
exp = {"1": "applied", "2": "filtered", "3": "blocked", "4": "applied", "5": "applied", "6": "skipped", "7": "applied", "8": "filtered", "9": "filtered", "10": "applied", "11": "filtered", "agg1": "applied", "agg2": "filtered", "12": "applied",
       "13": "applied", "14": "applied", "15": "unconfirmed", "16": "applied", "17": "applied", "18": "applied", "19": "skipped", "21": "low_score",
       "R-200": "applied", "R-77": "applied"}
if "--dry" in sys.argv: exp.update({"1": "dry_run", "4": "dry_run", "5": "dry_run", "7": "dry_run", "10": "dry_run", "agg1": "dry_run", "12": "dry_run", "14": "dry_run", "15": "dry_run",
                                    "16": "dry_run", "17": "dry_run", "18": "dry_run", "19": "dry_run", "13": "dry_run", "R-200": "dry_run", "R-77": "dry_run"})
problems = [f"job {k}: {rows[k][0]} != {v}" for k, v in exp.items() if rows[k][0] != v]
if "--dry" not in sys.argv:
    if not (rows["14"][2] or "").startswith("confirmed by email"): problems.append(f"emailed confirmation not used: {rows['14'][2]!r}")
    if "over" not in (rows["19"][2] or "") and "already applied to 3 roles" not in (rows["19"][2] or ""): problems.append(f"per-company cap reason: {rows['19'][2]!r}")
    if not (rows["21"][2] or "").startswith("match 40%"): problems.append(f"low-match job reason: {rows['21'][2]!r}")
    if any(s.get("tag") == "lowfit" for s in SUBS): problems.append("a job under the match bar was applied to")
    if not (rows["1"][2] or "").startswith("confirmed") : problems.append(f"applied job reason: {rows['1'][2]!r}")
    wd = [s for s in WD_STATE.submitted if s["job"].endswith("R-200")]
    if len(wd) != 1 or wd[0]["data"].get("name--legalName--firstName") != "Brian" or wd[0]["data"].get("files") != ["briandelgado_resume.pdf"]:
        problems.append(f"the Workday application was not sent once with your details: {[ (s['job'], s['data'].get('name--legalName--firstName'), s['data'].get('files')) for s in wd ]}")
    if not any(n > 1 for n in BATCHES): problems.append(f"match checks were not asked several postings at a time: {BATCHES}")
    if any(t.startswith("Program Coordinator at capco") for t in MATCHED): problems.append("a match check was spent on a posting over its employer's limit of applications")
    if not MAILS: problems.append("no summary email was sent")
    else:
        subj, body = MAILS[-1]
        for want in ("APPLIED", "EXCEPTIONS", "SUBMITTED BUT NOT CONFIRMED", "reCAPTCHA", "BEST MATCHES CHECKED", "match 86%"):
            if want.lower() not in body.lower(): problems.append(f"summary email lacks {want!r}")
        if "applied" not in subj: problems.append(f"summary subject: {subj!r}")
        if "TESTKEY: the provider does not accept the key" not in body: problems.append("the summary email does not say that a provider rejected its key")
        print("\nSUMMARY EMAIL SUBJECT:", subj)

if "--dry" not in sys.argv:
    by = {s["tag"]: s for s in SUBS}
    main, fab = by.get("main", {}), by.get("fab", {})
    wz = by.get("wizard", {})
    for k, v in {"first_name": "Brian", "email": "delgado@alumni.usc.edu", "hear": "Job board", "auth": "Yes", "spons": "No",
                 "gender": "Male", "vet": "I am not a protected veteran"}.items():
        if wz.get(k) != v: problems.append(f"multi-page wizard {k}={wz.get(k)!r} (want {v!r})")
    if not wz.get("why"): problems.append("wizard essay empty")
    if "127.0.0.1:8765" not in json.loads((work / "accounts.json").read_text()): problems.append("account was not remembered")
    if by.get("reloc_yes", {}).get("reloc") != "Yes": problems.append(f"NYC relocation answer: {by.get('reloc_yes')}")
    if "reloc_no" in by: problems.append("answered a relocation question for a city you didn't approve")
    want = {"first_name": "Brian", "last_name": "Delgado-Ortega", "email": "delgado@alumni.usc.edu", "phone": "(970) 366-8832",
            "auth": "Yes", "spons": "No", "loc": "Glenwood Springs, Colorado, United States", "sql": "2", "uspers": "Yes",
            "citstat": "Permanent Resident (Green Card)", "aiuse": "Yes", "gender": "Male", "hisp": "Yes",
            "addr": "63 Cherry Ct", "cl": "Glenwood Springs, CO, USA", "zip": "81647", "cob": "Mexico", "trav": "25-50%", "sal": "75000",
            "trans": "No", "orient": "Heterosexual/Straight", "vet": "I am not a protected veteran", "consent": "on"}
    problems += [f"main.{k}={main.get(k)!r} expected {v!r}" for k, v in want.items() if main.get(k) != v]
    if not main.get("disab", "").startswith("No, I do not have"): problems.append(f"disab={main.get('disab')!r}")
    if "stub" not in by: problems.append("a queued posting missing from this run's search was not attempted")
    if "aggform" not in by or by["aggform"].get("first_name") != "Brian": problems.append("aggregator job was not followed to its form and applied")
    if "aicert" in main: problems.append("ticked the 'I did not use AI' checkbox (must never)")
    if not main.get("why") or len(main["why"]) > 400 or "6,000" not in main["why"]: problems.append(f"why={main.get('why')!r}")
    if "45" in fab.get("why", "45") or "Rippling" in fab.get("why", "Rippling"): problems.append(f"fabricated number/tool survived: {fab.get('why')!r}")
    if any(w in (main.get("why", "") + fab.get("why", "")) for w in ("3.07", "Spanish", "first-generation")): problems.append("volunteered GPA/Spanish/background")
    if not main.get("resume", "").endswith("_resume.pdf") or not main.get("cover", "").endswith("_cover_letter.pdf"): problems.append("file uploads missing")
    if (CALLS.get("modelfix"), CALLS.get("bad"), CALLS.get("empty"), CALLS.get("capped")) != (2, 1, 3, 2) or CALLS.get("good", 0) < 2: problems.append(f"provider fallback wrong: {CALLS}")
    print("\nWRITER CALLS:", CALLS)
    print("ESSAY (main):", main.get("why")); print("ESSAY (fab, after revision):", fab.get("why"))
    d = next((work / "applications").glob("*/acme-operations-associate"))
    print("\n--- resume.md (first 30 lines) ---\n" + "\n".join((d / "resume.md").read_text().splitlines()[:30]))
    print("\n--- cover_letter.txt ---\n" + ((d / "cover_letter.txt").read_text() if (d / "cover_letter.txt").exists() else "(none written: form did not require one)"))
# 4) no site is paused or skipped: every job on a site whose human check stops the bot is still tried, and from then on
#    that site's jobs wait behind the sites where applications go through
cfg2 = copy.deepcopy(cfg); cfg2["db"] = "gate.db"; cfg2["aggregators"] = {"enabled": False}
(work / "config2.yaml").write_text(yaml.safe_dump(cfg2))
M.discover = lambda companies, log=print, base=None: [J(f"g{i}", "Operations Associate", f"gate{i}.html", co=f"gateco{i}", src="lever") for i in range(1, 6)] + \
    [J("ok", "Operations Associate", "gateok.html", co="okco", src="workable")]
n_mail = len(MAILS)
M.run(str(work / "config2.yaml"), dry_run="--dry" in sys.argv)
gdb = sqlite3.connect(work / "gate.db"); gdb.row_factory = sqlite3.Row
g = {r[0].split(":")[-1]: r[1:] for r in gdb.execute("select key,status,score,reason from jobs")}
if "--dry" not in sys.argv:
    blocked = [k for k in g if k.startswith("g") and g[k][0] == "blocked"]
    if len(blocked) != 5: problems.append(f"every human-check job should still be tried and recorded as blocked: {len(blocked)} of 5: {g}")
    for k in blocked:
        if not HITS.get(f"/gate{k[1:]}.html"): problems.append(f"blocked job {k} was never tried")
    if g.get("ok", ("",))[0] != "applied": problems.append(f"the job on the working site did not go through: {g.get('ok')}")
    health = M.site_health(M.DB(str(work / "gate.db")))
    lever_row = gdb.execute("select * from jobs where source='lever'").fetchone(); ok_row = gdb.execute("select * from jobs where source='workable'").fetchone()
    if not (M.site_rank(lever_row, health) == 3 and M.site_rank(ok_row, health) == 0):
        problems.append(f"measured ranking: lever {M.site_rank(lever_row, health)} (want 3), workable {M.site_rank(ok_row, health)} (want 0); health {health}")
    if len(MAILS) <= n_mail or "KEEP ENDING AT A HUMAN CHECK" not in MAILS[-1][1] or "lever" not in MAILS[-1][1]: problems.append("summary email does not mention the site that keeps ending at a human check")
    snaps = (work / "logs" / "snapshots.log").read_text() if (work / "logs" / "snapshots.log").exists() else ""
    if "reCAPTCHA" not in snaps or "=== " not in snaps: problems.append("no snapshot was saved for the blocked applications")
    if "delgado@alumni.usc.edu" in snaps or "366-8832" in snaps: problems.append("a snapshot contains your email address or phone number")

# 3) one stuck site cannot use up the run: the per-application time cap stops both the wizard loop and the field filler
import time as _t
from playwright.sync_api import sync_playwright as _sp
import autoapply.submit as _S
with _sp() as _p:
    _b = _p.chromium.launch(headless=True); _pg = _b.new_page(); _pg.goto(B + "main.html")
    _old = _S.MAX_APPLY_SECONDS; _S.MAX_APPLY_SECONDS = -1
    try:
        _S.apply(_pg, None, None, "", {}, work / "x.png", True)
        problems.append("apply() ignored the per-application time cap")
    except RuntimeError as e:
        if "took longer" not in str(e): problems.append(f"time cap message: {e}")
    finally:
        _S.MAX_APPLY_SECONDS = _old
    _f = _S.extract(_pg)
    try:
        _S.fill(_pg, _f, {_f[0]["id"]: "x"}, {}, print, deadline=_t.time() - 1)
        problems.append("fill() ignored its deadline")
    except RuntimeError:
        pass
    _b.close()

print("\nRESULT:", "ALL AS EXPECTED" if not problems else "PROBLEMS: " + "; ".join(problems))
sys.exit(1 if problems else 0)
