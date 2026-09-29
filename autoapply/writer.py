"""Free LLM writer for open-ended application questions.

Talks to any OpenAI-compatible /chat/completions endpoint (Groq, Gemini, GitHub Models, OpenRouter, a local
Ollama...) using a chain of providers, so when one free tier is out of quota the next one takes over.

It only ever writes from your résumé, about_me.md and stories. Three hard checks run on every answer:
  1. Numbers must come from your facts (no invented metrics); otherwise the answer is discarded.
  2. Length limits from the form are enforced.
  3. The model may reply CANNOT_ANSWER when a question needs something about you it hasn't been given; the job is
     then skipped instead of guessed.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

# Phrases that make application answers read like boilerplate (and like AI). Soft-linted: we ask for a rewrite once.
BANNED = [
    "delve", "tapestry", "testament to", "realm of", "in today's", "fast-paced", "passionate about", "i am writing to",
    "i'm writing to", "excited to apply", "thrilled", "unwavering", "cutting-edge", "synergy", "game-changer",
    "spearhead", "leverage", "navigate the", "landscape", "it is important to note", "in conclusion", "furthermore",
    "moreover", "seamless", "holistic", "dynamic environment", "proven track record", "results-driven",
    "detail-oriented", "team player", "hit the ground running", "i am confident that", "deeply committed",
]

TRUTH_RULES = """RULES (non-negotiable):
- Use ONLY the facts in the FACTS section. Never invent employers, titles, dates, numbers, results, tools, degrees,
  stories, feelings about the company, or people. Company details may come only from the job description.
- Never name software, platforms or certifications that are not listed in FACTS, not even to say "I'd learn it".
- If the question needs a personal story or fact that is not in FACTS, reply with exactly: CANNOT_ANSWER
- Never mention immigration status, race, gender, sexuality, disability, GPA or grades. Mention personal background
  (first-generation, upbringing) or Spanish ONLY if the question itself asks about it.
- Describe past work as "project" or "operation", never as a business that was founded or scaled. Use verbs like led,
  owned, coordinated, developed, recommended, organized, improved. Never inflate titles, seniority or team size.
- At Roaring Fork Property Group the applicant was a crew member (Property Management Crew): he performed maintenance,
  inspections and readiness checks and helped write SOPs across 70+ estates. Never say he managed, led, ran or oversaw
  operations, properties, estates or people there.
- Never claim to use a tool because the job description mentions it. Tools come only from FACTS.
- Do not restate the question. No greeting, no sign-off, no headings, no bullet lists unless the question asks for a list.
- Output only the answer text."""

# software/platform names we never let the writer claim unless they appear in the applicant's own facts
TOOLS = ["rippling", "lattice", "windmill", "zapier", "airtable", "salesforce", "hubspot", "workday", "netsuite", "sap",
         "oracle", "tableau", "power bi", "looker", "asana", "jira", "monday.com", "smartsheet", "procore", "yardi",
         "appfolio", "buildium", "bamboohr", "gusto", "adp", "greenhouse", "lever", "slack", "snowflake", "dbt",
         "sharepoint", "confluence", "trello", "docusign", "concur", "coupa", "anaplan", "alteryx", "matlab", "sql server"]

NUM_RX = re.compile(r"\d[\d,]*(?:\.\d+)?")


class WriterUnavailable(Exception):
    pass


_USAGE_FILE = Path("logs") / "usage.json"


def _usage() -> dict:
    try:
        d = json.loads(_USAGE_FILE.read_text())
        if d.get("_day") == time.strftime("%Y-%m-%d"):
            return d
    except Exception:
        pass
    return {"_day": time.strftime("%Y-%m-%d")}


def _save_usage(u: dict):
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _USAGE_FILE.write_text(json.dumps(u))
    except Exception:
        pass


class _Limiter:
    """Sliding 60-second window on requests and (estimated) tokens, so free-tier limits are never hit."""

    def __init__(self, rpm: int | None, tpm: int | None, rpd: int | None = None, name: str = ""):
        self.rpm, self.tpm, self.rpd, self.events, self.name = rpm, tpm, rpd, [], name

    @property
    def total(self) -> int:
        """Requests made today by this provider (persisted, so loops and restarts share one daily budget)."""
        return _usage().get(self.name, 0) if self.name else getattr(self, "_t", 0)

    def exhausted(self) -> bool:
        """Daily request budget (per run; the bot runs once a day) used up."""
        return bool(self.rpd) and self.total >= self.rpd

    def delay(self, tokens: int) -> float:
        """Seconds until a request of this size would be allowed (0 = now)."""
        now = time.time()
        ev = [e for e in self.events if now - e[0] < 60]
        if self.tpm:
            tokens = min(tokens, self.tpm)
        over = (self.rpm and len(ev) >= self.rpm) or (self.tpm and sum(e[1] for e in ev) + tokens > self.tpm)
        return max(0.0, ev[0][0] + 60 - now) if over and ev else 0.0

    def wait(self, tokens: int):
        if self.tpm:
            tokens = min(tokens, self.tpm)
        while True:
            now = time.time()
            self.events = [e for e in self.events if now - e[0] < 60]
            reqs, toks = len(self.events), sum(e[1] for e in self.events)
            over = (self.rpm and reqs >= self.rpm) or (self.tpm and toks + tokens > self.tpm)
            if not over or not self.events:
                break
            time.sleep(max(0.5, self.events[0][0] + 60 - now + 0.2))
        self.events.append((time.time(), tokens))
        if self.name:
            u = _usage(); u[self.name] = u.get(self.name, 0) + 1; _save_usage(u)
        else:
            self._t = getattr(self, "_t", 0) + 1


def resume_digest(p: dict) -> str:
    L = [f"Name: {p.get('name', '')}"]
    if p.get("summary"):
        L.append(f"Summary: {' '.join(str(p['summary']).split())}")
    for sec, title in (("experience", "EXPERIENCE"), ("projects", "PROJECTS")):
        if p.get(sec):
            L.append(f"\n{title}")
        for r in p.get(sec, []) or []:
            head = r.get("title") or r.get("name", "")
            if r.get("company"):
                head += f" at {r['company']}"
            elif r.get("tagline"):
                head += f" ({r['tagline']})"
            L.append(f"- {head} [{r.get('dates', '')}]")
            for b in r.get("bullets", []):
                L.append(f"    * {b if isinstance(b, str) else b['text']}")
    for e in p.get("education", []) or []:
        L.append(f"\nEDUCATION: {e['degree']}, {e['school']} ({e.get('date', '')})")
        L += [f"    * {b}" for b in e.get("bullets", [])]
    for g in p.get("skills", []) or []:
        L.append(f"SKILLS - {g['group']}: {', '.join(g['items'])}")
    if p.get("stories"):
        L.append("\nSTORIES (true, usable for behavioral questions)")
        for s in p["stories"]:
            L.append(f"- [{s['topic']}] {' '.join(str(s['story']).split())}")
    return "\n".join(L)


class Writer:
    def __init__(self, cfg: dict, profile: dict, base: Path):
        w = cfg.get("writer", {}) or {}
        self.cfg = w
        self.name = profile.get("name", "the applicant")
        self.first = self.name.split()[0]
        self.providers = [p for p in w.get("providers", []) or []]
        self._cands = {}
        self.limiters = {p["name"]: _Limiter(p.get("rpm"), p.get("tpm"), p.get("rpd"), p["name"]) for p in self.providers}
        self.dead: set[str] = set()
        self.strikes: dict[str, int] = {}
        self.calls = 0

        def read(name, fallback=""):
            f = base / name
            return f.read_text() if name and f.exists() else fallback

        default_voice = (Path(__file__).parent / "voice_default.md").read_text()
        self.voice = read(w.get("voice_file", "voice.md"), default_voice)
        self.about = read(w.get("about_file", "about_me.md"))
        cap = w.get("max_context_chars", 8000)
        self.digest = resume_digest(profile)
        about_lines = [ln for ln in self.about.splitlines()]
        self.about = "\n".join(about_lines)[:cap]
        # facts the grounding checks trust: résumé, about_me and config facts, minus explicit "DO NOT CLAIM" lines
        trusted = "\n".join(ln for ln in about_lines if not ln.strip().upper().startswith("DO NOT CLAIM"))
        self.sources = " ".join([self.digest, trusted, str(cfg.get("facts", {}))]).lower()
        self.system = (
            f"You are ghostwriting job-application answers for {self.name}. Write in first person, as {self.first}.\n\n"
            f"{TRUTH_RULES}\n\n# VOICE\n{self.voice}\n\n# FACTS ABOUT {self.first.upper()} (only source of truth)\n"
            f"{self.digest}\n\n# EXTRA BACKGROUND (personal context, use only what is relevant)\n{self.about}")

    # ------------------------------------------------------------------ transport
    def _chat(self, p: dict, messages: list[dict], max_tokens: int) -> str:
        key = None
        if p.get("api_key_env"):
            key = os.environ.get(p["api_key_env"])
            if not key:
                raise WriterUnavailable(f"{p['name']}: ${p['api_key_env']} not set")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        est = sum(len(m["content"]) for m in messages) // 4 + max_tokens
        cands = self._cands.setdefault(p["name"], [p["model"]] if isinstance(p["model"], str) else list(p["model"]))
        discovered = False
        attempt = 0
        while attempt < 3:
            if not cands:
                if discovered or not p.get("discover"):
                    raise WriterUnavailable(f"{p['name']}: HTTP 404 no usable model")
                discovered = True
                cands.extend(self._discover(p, headers))
                continue
            payload = {"model": cands[0], "messages": messages, "max_tokens": max_tokens,
                       "temperature": p.get("temperature", 0.8), **(p.get("extra") or {})}
            self.limiters[p["name"]].wait(est)
            r = requests.post(p["base_url"].rstrip("/") + "/chat/completions", headers=headers, json=payload, timeout=120)
            if r.status_code in (400, 404) and re.search(r"model", r.text, re.I) and (len(cands) > 1 or p.get("discover")):
                cands.pop(0)                       # this model name is gone or not offered: try the next one
                continue
            if r.status_code == 429 and re.search(r"limit: 0|quota exceeded for metric.*free", r.text, re.I | re.S):
                if len(cands) > 1 or (p.get("discover") and not discovered):
                    cands.pop(0)                   # no free quota on this model: try another one
                    continue
                raise WriterUnavailable(f"{p['name']}: HTTP 403 no free quota for this model")
            attempt += 1
            if r.status_code == 200:
                choice = r.json()["choices"][0]
                text = (choice["message"].get("content") or "").strip()
                if choice.get("finish_reason") == "length" and max_tokens < 4000:
                    max_tokens = min(max_tokens * 2, 4000)   # reasoning ate the budget / answer was cut off: retry bigger
                    est = sum(len(m["content"]) for m in messages) // 4 + max_tokens
                    attempt += 1
                    continue
                if not text:                       # e.g. a reasoning model spent its whole token budget thinking
                    raise WriterUnavailable(f"{p['name']}: empty reply")
                return text
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(float(r.headers.get("retry-after", 5 * (attempt + 1))), 60))
                continue
            raise WriterUnavailable(f"{p['name']}: HTTP {r.status_code} {r.text[:120]}")
        raise WriterUnavailable(f"{p['name']}: still rate limited after retries")

    def _discover(self, p: dict, headers: dict) -> list[str]:
        """Ask the provider which models exist and pick likely free ones (stable before preview, newest first)."""
        try:
            r = requests.get(p["base_url"].rstrip("/") + "/models", headers=headers, timeout=30)
            ids = [m.get("id", "").replace("models/", "") for m in r.json().get("data", [])]
        except Exception:
            return []
        kw = str(p["discover"]).lower()
        ids = [i for i in ids if kw in i.lower() and (kw == "lite" or "lite" not in i.lower()) and not re.search(r"image|tts|live|audio|embed|robot|computer|exp|vision|thinking|customtools", i, re.I)]
        ver = lambda i: [float(x) for x in re.findall(r"\d+(?:\.\d+)?", i)] or [0]
        return sorted(ids, key=lambda i: ("preview" in i, [-v for v in ver(i)]))[:4]

    def _complete(self, messages: list[dict], max_tokens: int = 900, log=print) -> str:
        est = sum(len(m["content"]) for m in messages) // 4 + max_tokens
        live = [p for p in self.providers if p["name"] not in self.dead and not self.limiters[p["name"]].exhausted()]
        # prefer providers with quota available right now, otherwise the one that frees up soonest
        order = sorted(range(len(live)), key=lambda i: (self.limiters[live[i]["name"]].delay(est) > 5, i))
        errors = []
        for i in order:
            p = live[i]
            try:
                out = self._chat(p, messages, max_tokens)
                self.calls += 1
                return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
            except (WriterUnavailable, requests.RequestException) as e:
                errors.append(str(e))
                if "not set" in str(e) or "HTTP 40" in str(e) or ("rate limited" in str(e) and p["name"].startswith("gemini")) or isinstance(e, requests.ConnectionError):
                    self.dead.add(p["name"])          # bad key / not running: don't retry this run
                elif "empty reply" in str(e):
                    self.strikes[p["name"]] = self.strikes.get(p["name"], 0) + 1
                    if self.strikes[p["name"]] >= 3:
                        self.dead.add(p["name"])
                log(f"      writer: {str(e)[:110]}")
        raise WriterUnavailable("; ".join(errors) or "no providers configured")

    def ready(self) -> bool:
        return any(p["name"] not in self.dead and not self.limiters[p["name"]].exhausted()
                   and (not p.get("api_key_env") or os.environ.get(p["api_key_env"]))
                   for p in self.providers)

    # ------------------------------------------------------------------ checks
    def _ungrounded(self, text: str, extra: str) -> list[str]:
        src = self.sources + " " + extra.lower()
        bad = []
        for m in NUM_RX.findall(text):
            n = m.replace(",", "")
            if n.replace(".", "").isdigit() and float(n) <= 3:
                continue
            if not re.search(r"(?<![\d.])" + re.escape(n) + r"(?![\d])", src.replace(",", "")):
                bad.append(m)
        return bad

    def _issues(self, text: str, max_chars: int | None, extra: str) -> list[str]:
        issues = []
        if max_chars and len(text) > max_chars:
            issues.append(f"it is {len(text)} characters; the hard limit is {max_chars}. Shorten it")
        hits = [b for b in BANNED if b in text.lower()]
        if hits:
            issues.append("rewrite without these stock phrases: " + ", ".join(hits))
        if "—" in text:
            issues.append("do not use em dashes")
        bad = self._ungrounded(text, extra)
        if bad:
            issues.append("these numbers are not in my facts, so remove them: " + ", ".join(bad))
        if not re.search(r"[.!?\"”)]$", text.strip()) and not text.strip().endswith(self.first):
            issues.append("the text is cut off mid-sentence; write the complete answer")
        if re.search(r"\b(managed|led|oversaw|ran|directed|supervised)\b[^.]{0,45}\b(estates?|propert(y|ies)|portfolio)\b", text, re.I):
            issues.append("at Roaring Fork Property Group I was a crew member who did maintenance, inspections and SOP work; "
                          "do not say I managed, led or oversaw estates, properties or operations there")
        tools = self._unknown_tools(text, extra)
        if tools:
            issues.append("these tools are not in my facts, so do not mention them: " + ", ".join(tools))
        return issues

    _NOT_TOOLS = {"new", "san", "los", "las", "united", "north", "south", "east", "west", "the", "our", "this", "that", "their",
                  "colorado", "california", "denver", "boulder", "aspen", "america", "york", "english", "spanish", "usc", "us"}

    def _unknown_tools(self, text: str, extra: str = "") -> list[str]:
        low = text.lower()
        found = [t for t in TOOLS if re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", low)
                 and not re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", self.sources)]
        # "in Linear", "using Retool": a capitalised name that comes from the job description, not from my facts
        skip = {w.lower() for w in re.findall(r"\w+", getattr(self, "_ctx", ""))}
        for m in re.finditer(r"\b(?:in|using|with|via|through|on)\s+([A-Z][A-Za-z0-9+#.]{2,})", text):
            w = m.group(1).rstrip(".")
            if w.lower() in self._NOT_TOOLS or w.lower() in skip:
                continue
            if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", extra) and \
                    not re.search(r"(?<![\w])" + re.escape(w.lower()) + r"(?![\w])", self.sources):
                found.append(w)
        return found

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"^```\w*\n|\n```$", "", text.strip())
        text = re.sub(r"^(answer|response)\s*:\s*", "", text, flags=re.I)
        return text.strip().strip('"“”').strip()

    def _finish(self, first: str, user_msg: str, max_chars, extra: str, max_tokens: int, log) -> str | None:
        text = self._clean(first)
        if not text or text.upper().startswith("CANNOT_ANSWER"):
            return None
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": user_msg}]
        for _ in range(2):
            issues = self._issues(text, max_chars, extra)
            if not issues:
                break
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "Revise. " + "; ".join(issues) + ". Output only the revised answer."}]
            text = self._clean(self._complete(msgs, max_tokens, log))
            if text.upper().startswith("CANNOT_ANSWER"):
                return None
        if self._ungrounded(text, extra) or self._unknown_tools(text, extra):
            log("      writer: answer kept claiming numbers or tools not in your facts, discarded")
            return None
        text = text.replace(" — ", ", ").replace("—", ", ")
        if max_chars and len(text) > max_chars:
            cut = text[:max_chars]
            end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("."))
            if end < max_chars * 0.5:
                return None
            text = cut[: end + 1]
        return text

    # ------------------------------------------------------------------ public API
    def answer(self, job, company: str, question: str, max_chars: int | None = None, log=print) -> str | None:
        limit = (f"Hard limit: {max_chars} characters. Stay comfortably under it." if max_chars
                 else "Length: 70-130 words unless the question clearly asks for more or less.")
        user = (f"Role: {job.title} at {company}\nJob description (excerpt):\n{job.description[:2200]}\n\n"
                f"Application question:\n{question}\n\n{limit}\nWrite my answer.")
        extra = job.description[:2200] + " " + question
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        self._ctx = f"{company} {job.title}"
        first = self._complete(msgs, 1000, log)
        return self._finish(first, user, max_chars, extra, 1000, log)

    def cover_letter(self, job, company: str, log=print) -> str | None:
        user = (f"Role: {job.title} at {company}\nJob description (excerpt):\n{job.description[:2500]}\n\n"
                f"Write my cover letter: 150-220 words, plain text. Start with 'Dear {company} team,' "
                f"and sign off with just '{self.first}'. Open with something specific about why this role fits what I "
                "have actually done, not with 'I am writing'. Use two concrete things from my background that map to "
                "the posting. No bullet points, no headings.")
        extra = job.description[:2500]
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        self._ctx = f"{company} {job.title}"
        first = self._complete(msgs, 1600, log)
        return self._finish(first, user, 2200, extra, 1600, log)


def limits_from_question(label: str, maxlength: int | None) -> int | None:
    """Character cap from the input's maxlength, or from wording like '250 words' / '500 characters'."""
    caps = []
    if maxlength:
        caps.append(int(maxlength))
    for m in re.finditer(r"(\d{2,5})\s*(?:-\s*\d+\s*)?(characters?|chars?|words?)", label or "", re.I):
        n = int(m.group(1))
        caps.append(n if m.group(2).lower().startswith("c") else n * 6)
    return min(caps) if caps else None
