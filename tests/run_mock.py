"""End-to-end test of the real pipeline (scoring, résumé assembly, form answering, free-LLM writer, submit)
against local mock application forms and a mock OpenAI-compatible LLM server. No internet, no keys, no cost."""
import copy, functools, http.server, json, os, shutil, sqlite3, sys, threading, urllib.parse
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
work = ROOT / "work"; shutil.rmtree(work, ignore_errors=True); (work / "site").mkdir(parents=True)

SUBS, CALLS = [], {}
CLEAN = ("Capitol Edge is the clearest example: I built it alone, about 6,000 lines of production code, and I keep it "
         "running. I like ops work where the process is the product, and this role looks like that.")
FAB = "I raised revenue 45% at Otto's and ran it all in Rippling. This role is the same kind of work."
LETTER = ("Dear Acme team,\n\nCapitol Edge started as a solo build and now runs in production, roughly 6,000 lines of code "
          "that I wrote and maintain. The same habits, documenting the process and fixing what breaks, are what I'd "
          "bring to this role.\n\nBrian")

def reply(msgs):
    first, last = msgs[1]["content"], msgs[-1]["content"]
    if last.startswith("Revise."): return CLEAN
    if "Write my cover letter" in first: return LETTER
    q = first.split("Application question:\n")[1]
    if "failed" in q: return "CANNOT_ANSWER"
    return FAB if "MARK_FAB" in first else CLEAN

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(work / "site"), **k)
    def log_message(self, *a): pass
    def do_GET(self):
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

def page(tag, essay="Why do you want to work at Acme? (max 400 characters) *", top="", extra=""):
    (work / "site" / f"{tag}.html").write_text(f"""<!doctype html><html><body><h1>Operations Associate — Acme</h1>{top}
<form id="application-form" action="thanks.html" method="get"><input type="hidden" name="tag" value="{tag}">
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
(work / "site" / "thanks.html").write_text("<!doctype html><html><body><h1>Thank you for applying!</h1><p>We've received your application.</p></body></html>")

import autoapply.writer as _W; _W._USAGE_FILE = work / "usage.json"
from autoapply import main as M
from autoapply.sources import Job
B = "http://127.0.0.1:8765/"; GOOD = "Remote. You will drive process improvement using Excel and SQL, coordinate vendor work and build dashboards. 2 years of experience."
def J(i, title, page_, desc=GOOD, loc="Remote"): return Job("greenhouse", "acme", str(i), title, loc, B + page_, B + page_, desc)
M.discover = lambda companies, log=print: [
    J(1, "Operations Associate", "main.html"),
    J(2, "Operations Manager", "main.html", "Requires 8+ years of experience in operations. Security clearance required."),
    J(3, "Operations Analyst", "captcha.html", loc="Denver, CO"),
    J(4, "Business Analyst", "unknown.html"),
    J(5, "Business Analyst", "fail.html"),
    J(6, "Supply Chain Analyst", "noai.html", GOOD + " Please do not use AI tools to write your application."),
    J(7, "Logistics Coordinator", "fab.html", GOOD + " MARK_FAB"),
    J(8, "Senior Staff Engineer", "main.html", ""),
    J(9, "Operations Coordinator", "reloc_no.html", loc="Chicago, United States"),
    J(10, "Project Coordinator", "reloc_yes.html", loc="New York, NY"),
    J(11, "Operations Associate", "main.html", GOOD + " Pay range: $45,000 - $55,000 per year."),
]

cfg = yaml.safe_load((ROOT.parent / "config.yaml").read_text())
cfg["aggregators"] = {"enabled": True, "sources": ["remoteok"], "base_urls": {"remoteok": B + "remoteok"}, "queries": [], "locations": []}
cfg["facts"]["city"] = "Glenwood Springs"; cfg["search"]["delay_seconds"] = [0, 0]
cfg["writer"]["providers"] = [
    {"name": "modelfix", "base_url": B + "modelfix", "model": ["old", "new"], "api_key_env": "TESTKEY", "rpm": 1000, "rpd": 1},
    {"name": "bad", "base_url": B + "bad", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000},
    {"name": "empty", "base_url": B + "empty", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000},
    {"name": "capped", "base_url": B + "capped", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000, "rpd": 2},
    {"name": "good", "base_url": B + "good", "model": "m", "api_key_env": "TESTKEY", "rpm": 1000}]
os.environ["TESTKEY"] = "x"
for f in ("profile.yaml", "about_me.md"): shutil.copy(ROOT.parent / f, work / f)

# 1) a leftover template placeholder must stop the run
prof = (work / "profile.yaml").read_text(); (work / "profile.yaml").write_text(prof + "\n# <TODO fill me>\nnote: '<TODO fill me>'\n")
(work / "config.yaml").write_text(yaml.safe_dump(cfg))
try:
    M.run(str(work / "config.yaml")); print("FAIL: placeholders not refused"); sys.exit(1)
except SystemExit as e:
    print("PLACEHOLDER GUARD OK ->", str(e).splitlines()[1].strip())
(work / "profile.yaml").write_text(prof)

# 2) real run
M.run(str(work / "config.yaml"), dry_run="--dry" in sys.argv)
db = sqlite3.connect(work / "applications.db")
rows = {r[0].split(":")[-1]: r[1:] for r in db.execute("select key,status,score,reason from jobs")}
print("\nDB:"); [print("  ", k, v) for k, v in sorted(rows.items())]
exp = {"1": "applied", "2": "low_score", "3": "blocked", "4": "skipped", "5": "skipped", "6": "skipped", "7": "applied", "8": "filtered", "9": "skipped", "10": "applied", "11": "low_score", "agg1": "applied", "agg2": "skipped"}
if "--dry" in sys.argv: exp.update({"1": "dry_run", "7": "dry_run", "10": "dry_run", "agg1": "dry_run"})
problems = [f"job {k}: {rows[k][0]} != {v}" for k, v in exp.items() if rows[k][0] != v]

if "--dry" not in sys.argv:
    by = {s["tag"]: s for s in SUBS}
    main, fab = by.get("main", {}), by.get("fab", {})
    if by.get("reloc_yes", {}).get("reloc") != "Yes": problems.append(f"NYC relocation answer: {by.get('reloc_yes')}")
    if "reloc_no" in by: problems.append("answered a relocation question for a city you didn't approve")
    want = {"first_name": "Brian", "last_name": "Delgado-Ortega", "email": "delgado@alumni.usc.edu", "phone": "(970) 366-8832",
            "auth": "Yes", "spons": "No", "loc": "Glenwood Springs, Colorado, United States", "sql": "2", "uspers": "Yes",
            "citstat": "Permanent Resident (Green Card)", "aiuse": "Yes", "gender": "Male", "hisp": "Yes",
            "addr": "63 Cherry Ct", "cl": "Glenwood Springs, CO, USA", "zip": "81647", "cob": "Mexico", "trav": "25-50%", "sal": "75000",
            "trans": "No", "orient": "Heterosexual/Straight", "vet": "I am not a protected veteran", "consent": "on"}
    problems += [f"main.{k}={main.get(k)!r} expected {v!r}" for k, v in want.items() if main.get(k) != v]
    if not main.get("disab", "").startswith("No, I do not have"): problems.append(f"disab={main.get('disab')!r}")
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
print("\nRESULT:", "ALL AS EXPECTED" if not problems else "PROBLEMS: " + "; ".join(problems))
sys.exit(1 if problems else 0)
