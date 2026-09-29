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
print("RESULT:", "ALL AS EXPECTED" if not problems else "PROBLEMS: " + "; ".join(problems))
sys.exit(1 if problems else 0)
