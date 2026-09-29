"""Decision layer: rule-based scoring, résumé assembly and form answering, plus a free-LLM writer for essay questions.

Résumé and letter content is only ever *selected and reordered* from what you wrote in profile.yaml. Form answers
come only from config.yaml (`facts`, `answers`) or, for open-ended prompts, from the writer (see writer.py), which is
limited to the same facts and discards anything it can't ground.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .writer import Writer, WriterUnavailable, limits_from_question

STOP = set("""a an and are as at be but by for from has have in into is it its of on or our that the their this to
we will with you your they them who what when where which while about also can more most other such than then
these those through over under between within without across per etc including include able ability work working
team teams role roles job company experience years year strong new great good""".split())

NA_RX = re.compile(
    r"current (or most recent )?(employer|company|position|job|title|role|salary|supervisor|manager)|present (employer|position)|"
    r"(previous|prior|former|last) (supervisor|manager)|supervisor'?s? (name|email|phone|title)|manager'?s? (name|email|phone)|"
    r"reason for leaving|end date|(address|line|apt|suite|unit) ?(line )?2\b|suffix|preferred (contact )?time|"
    r"(employee|staff) (id|number)|badge|referral|referred|other (name|names|language)|maiden|previous (name|address)|"
    r"linkedin|portfolio|(company|employer) (address|phone|website)|department|if (yes|applicable|other)|"
    r"professional licen[sc]e|certification number|license number", re.I)
DECLINE = re.compile(r"decline|prefer not|prefer to not|do not wish|don'?t wish|do not want|don'?t want|"
                     r"not to (say|answer|disclose|self)|choose not|rather not", re.I)
CONSENT = re.compile(r"agree|acknowledge|consent|certify|confirm|accept|privacy|terms|policy|true and (correct|accurate)", re.I)
OPTIONAL_CHECK = re.compile(r"marketing|newsletter|updates|receive|sms|text message|future (roles|opportunities)|talent (pool|community)", re.I)
AI_WORDS = re.compile(r"\b(ai|a\.i\.|chatgpt|generative|llm|artificial intelligence)\b", re.I)
PROMPT_RX = re.compile(
    r"\b(why|describe|tell us|tell me|explain|share|give (us )?an example|walk us|what (makes|excites|motivates|interests|"
    r"draws|would|do you|are you|is your|has been)|how (do|did|would|have|will)|in your own words|your (experience|approach|"
    r"interest|background|motivation)|elaborate|proud|challenge|accomplishment|achievement|tell us about)\b", re.I)

# (regex over "label question", fact keys in priority order, mode)
#   mode: None = normal | "choice" = only for option fields (select/radio/checkbox group) | "demo" = voluntary self-ID
_AI_Q = (r"\b(ai|a\.i\.|chatgpt|generative|artificial intelligence|llm)\b.{0,60}\b(use[ds]?|using|assist\w*|generat\w*|tools?|help\w*)\b|"
         r"\b(use[ds]?|using|assist\w*|help\w*)\b.{0,60}\b(ai|a\.i\.|chatgpt|generative|artificial intelligence|llm)\b")
FIELD_RULES = [
    (_AI_Q, ("ai_use_disclosure",), "choice"),
    (r"preferred (first )?name|nickname", ("preferred_name", "first_name"), None),
    (r"middle (name|initial)", ("middle_name",), None),
    (r"first name|given name|forename", ("first_name",), None),
    (r"last name|surname|family name", ("last_name",), None),
    (r"^\W*(full |legal )?name\b|your name|legal name", ("full_name",), None),
    (r"e-?mail", ("email",), None),
    (r"phone|mobile|telephone|\bcell\b", ("phone",), None),
    (r"linkedin", ("linkedin",), None),
    (r"github", ("github",), None),
    (r"portfolio|website|personal (site|url)|\burl\b", ("website", "linkedin"), None),
    (r"most recent (company|employer)|previous (company|employer)|last (company|employer)", ("most_recent_company",), None),
    (r"most recent (job )?title|previous (job )?title|last (job )?title", ("most_recent_title",), None),
    (r"current (company|employer)|present employer", ("current_company",), None),
    (r"current (job )?title", ("current_title",), None),
    (r"street|mailing address|\baddress( line)?( 1)?\b", ("address_line1",), None),
    (r"zip|postal", ("zip",), None),
    (r"country of birth|birth country|place of birth|where (were you|are you) born", ("country_of_birth",), None),
    (r"country of (citizenship|origin)|nationality|citizen of which", ("country_of_citizenship",), None),
    (r"\bcity\b|where are you (located|based)|current location|\blocation\b|where do you (live|reside)", ("location_text", "city"), None),
    (r"\bstate\b|province|region", ("state",), None),
    (r"\bcountry\b", ("country",), None),
    (r"citizen or (lawful )?permanent resident|citizen.{0,25}permanent resident|permanent resident.{0,25}citizen|"
     r"u\.?s\.? person|protected individual|export control|\bitar\b", ("us_person",), None),
    (r"u\.?s\.? citizen|citizen of the (u|united)", ("us_citizen",), None),
    (r"sponsor", ("requires_sponsorship_now_or_future",), None),
    (r"citizenship|immigration status|work status|visa status|status in the (u|united)|type of (work )?(authorization|permit)",
     ("citizenship_status",), "demo"),
    (r"clearance", ("security_clearance",), None),
    (r"authori[sz]ed to work|legally (authorized|eligible|permitted)|eligible to work|right to work|work authori|"
     r"unrestricted|work in the (u\.?s|united states)", ("authorized_to_work_in_us",), None),
    (r"note-?taker|(record|transcri)\w* (of )?(the |our )?(interview|call|meeting)", ("ai_notetaker_consent",), "choice"),
    (r"plan on working from|payroll tax|state (do you|will you) (work|reside|live)", ("state",), None),
    (r"relocat", ("willing_to_relocate",), "loc"),
    (r"\btravel", ("willing_to_travel",), "travel"),
    (r"hybrid|on-?site|in-?office|in the office|in-?person|our offices?|anchor days?|days? ?(a|per|/) ?week|commut", ("open_to_onsite",), "loc"),
    (r"remote", ("open_to_remote",), "choice"),
    (r"start date|earliest.*start|available to start|notice period|when (can|could) you start|availability", ("earliest_start_date",), None),
    (r"salary|compensation|pay expectation|desired pay|expected pay|pay range", ("salary_expectation",), "salary"),
    (r"(related|relative|friend|family).{0,60}(work|employ)|know (anyone|someone|any)|current(ly)? employees?.{0,40}(know|refer)|have you been referred|were you referred", ("know_employee",), "choice"),
    (r"\bsms\b|text messag|text you|contact (you )?(by|via) text", ("sms_consent",), "choice"),
    (r"referred by|referrer|referral (name|employee|code|email)|employee referral|name of (the )?(employee|person)", ("referral_name",), None),
    (r"how did you (hear|find|learn)|hear about (us|this|the)|where did you (hear|find|learn)|referral source", ("how_did_you_hear",), None),
    (r"(previously|ever|formerly) (been )?(employed|worked)|former employee|worked (for|at) .{0,40}before", ("previously_employed_here",), None),
    (r"18 years|over 18|at least 18|legal age|age of 18", ("over_18",), None),
    (r"currently (employed|working)", ("currently_employed",), None),
    (r"contact .{0,30}employer", ("may_contact_employer",), None),
    (r"background (check|screen|investigation)|consent to .{0,30}(check|screening)", ("background_check_consent",), None),
    (r"drug (test|screen)", ("drug_test_consent",), None),
    (r"convict|felony|misdemeanor|criminal", ("criminal_conviction",), None),
    (r"driver'?s? licen[cs]e|valid driver", ("has_drivers_license",), None),
    (r"reliable transportation", ("reliable_transportation",), None),
    (r"speak spanish|spanish.{0,20}(fluen|proficien|speak)|bilingual", ("speaks_spanish",), None),
    (r"languages? (do you )?(speak|spoken|proficien)|fluent in", ("languages",), None),
    (r"\bgpa\b|grade point", ("gpa",), None),
    (r"highest (level of )?(education|degree)|level of education|education level", ("education_level",), None),
    (r"\b(school|university|college|institution)\b", ("school",), None),
    (r"degree|diploma", ("degree",), None),
    (r"\bmajor\b|field of study|discipline", ("major",), None),
    (r"graduat", ("graduation_date",), None),
    (r"pronoun", ("pronouns",), "demo"),
    (r"transgender|\btrans\b", ("transgender",), "demo"),
    (r"gender identity|\bgender\b|\bsex\b", ("gender",), "demo"),
    (r"sexual orientation", ("sexual_orientation",), "demo"),
    (r"hispanic|latin[oax]", ("hispanic_latino",), "demo"),
    (r"\brace\b|ethnic", ("race_ethnicity",), "demo"),
    (r"veteran|military", ("veteran_status",), "demo"),
    (r"disabilit", ("disability_status",), "demo"),
]
FIELD_RULES = [(re.compile(p, re.I), k, m) for p, k, m in FIELD_RULES]

# Canonical self-ID values -> (option must-match regex, option must-NOT-match regex). Lets "not_veteran" pick
# "I am not a protected veteran" but never "I don't wish to answer".
_NOT_DECL = DECLINE.pattern + r"|answer|disclose"
DEMO_RX = {
    "gender": {"male": (r"\b(male|man)\b", _NOT_DECL + r"|female|woman|non-?binary|trans"),
               "female": (r"\b(female|woman)\b", _NOT_DECL + r"|non-?binary|trans")},
    "transgender": {"no": (r"^\s*no\b|cisgender|not transgender|do not identify", _NOT_DECL + r"|\byes\b")},
    "sexual_orientation": {"heterosexual": (r"hetero|straight", _NOT_DECL)},
    "veteran_status": {"not_veteran": (r"\bnot\b|^\s*no\b", _NOT_DECL + r"|identify as|\byes\b")},
    "disability_status": {"none": (r"\bno\b|do not have|don'?t have|not have|without", _NOT_DECL + r"|\byes\b")},
    "citizenship_status": {"permanent_resident": (r"permanent resident|green card|\blpr\b", r"non-?permanent|temporary")},
}
DEMO_TEXT = {"male": "Male", "female": "Female", "no": "No", "heterosexual": "Heterosexual", "not_veteran": "Not a protected veteran",
             "none": "No disability", "permanent_resident": "Lawful Permanent Resident"}


def _norm(s) -> str:
    return re.sub(r"\W+", " ", str(s)).strip().lower()


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#.\-]{2,}", text.lower()) if w not in STOP}


def pick_option(options: list[str], want) -> str | None:
    """Best option for a wanted value: exact, then leading word (Yes/No), then substring."""
    w = _norm(want)
    if not w:
        return None
    norm = [(_norm(o), o) for o in options]
    for n, o in norm:
        if n == w:
            return o
    first = w.split()[0]
    for n, o in norm:
        if n == first:
            return o
    for n, o in norm:
        if n.split() and n.split()[0] == first:
            return o
    for n, o in norm:
        if len(n) > 3 and (n in w or w in n):
            return o
    return None


class Brain:
    def __init__(self, cfg: dict, profile: dict, base: Path | None = None, log=print):
        self.cfg, self.profile, self._log, self._job = cfg, profile, log, None
        base = Path(base or ".")
        f = dict(cfg.get("facts", {}))
        f.setdefault("full_name", profile.get("name", ""))
        parts = f["full_name"].split()
        f.setdefault("first_name", parts[0] if parts else "")
        f.setdefault("last_name", " ".join(parts[1:]))
        if not f.get("location_text") and f.get("city"):
            f["location_text"] = ", ".join(x for x in (f.get("city"), f.get("state")) if x)
        self.facts = f
        self.sc = cfg.get("scoring", {})
        self.answers = [(re.compile(a["match"], re.I), a["answer"]) for a in cfg.get("answers", []) or []]
        self.skills = [s.lower() for g in profile.get("skills", []) or [] for s in g["items"]]
        self._refuse_unfinished_setup(base)
        w = cfg.get("writer", {}) or {}
        self.writer = Writer(cfg, profile, base) if w.get("enabled") else None
        if self.writer:
            if self.writer.ready():
                log("Writer: essay questions will be answered by " +
                    ", ".join(p["name"] for p in self.writer.providers) + " (free tiers, in that order)")
            else:
                log("Writer: no provider has a key/endpoint available, so jobs with essay questions will be skipped")

    def _refuse_unfinished_setup(self, base: Path):
        """A hands-off bot must never send template placeholders to a real employer."""
        bad = []
        blobs = [("profile.yaml", json.dumps(self.profile, ensure_ascii=False)),
                 ("config.yaml answers", json.dumps(self.cfg.get("answers", []), ensure_ascii=False))]
        about = base / (self.cfg.get("writer", {}) or {}).get("about_file", "about_me.md")
        if about.exists():
            blobs.append(("about_me.md", about.read_text()))
        for label, blob in blobs:
            bad += [f"{label}: {m}" for m in re.findall(r"<[^<>\n]{2,80}>", blob)]
        if "example.com" in str(self.facts.get("email", "")) or not self.facts.get("email"):
            bad.append("config.yaml facts.email is still the example address")
        if bad:
            raise SystemExit("Finish your setup first. These still look like template placeholders:\n  - " + "\n  - ".join(bad[:12]))

    # ------------------------------------------------------------------ 1. score
    def score(self, job) -> tuple[int, str]:
        sc = self.sc
        title, desc, loc = job.title.lower(), job.description.lower(), job.location.lower()
        parts = []

        hits = sorted(((v, k) for k, v in (sc.get("title_keywords") or {}).items() if k.lower() in title), reverse=True)
        t = 0
        if hits:
            t = min(50, hits[0][0] + 5 * min(len(hits) - 1, 2))
            parts.append(f"title '{hits[0][1]}' +{t}")

        kw_hits = [(k, v) for k, v in (sc.get("keywords") or {}).items() if k.lower() in desc]
        d = min(sc.get("keyword_cap", 35), sum(v for _, v in kw_hits))
        if kw_hits:
            names = ", ".join(k for k, _ in sorted(kw_hits, key=lambda x: -x[1])[:4])
            parts.append(f"keywords [{names}] +{d}")

        bonus = 0
        remote_job = "remote" in loc or str(job.extra.get("workplace", "")).lower() == "remote"
        for pref in sc.get("preferred_locations", []) or []:
            if pref.lower() in loc or (pref.lower() == "remote" and remote_job):
                bonus = sc.get("location_bonus", 10)
                parts.append(f"location '{pref}' +{bonus}")
                break

        pen = 0
        for k, v in (sc.get("negative_keywords") or {}).items():
            if k.lower() in desc or k.lower() in title:
                pen += v
                parts.append(f"'{k}' {v}")
        pen = max(pen, -50)

        pay = self._pay(job)
        if pay:
            floor = sc.get("salary_floor")
            if floor and pay[1] < floor:
                pen -= 40
                parts.append(f"pay tops out near ${pay[1]:,.0f} (below your ${floor:,} floor) -40")
            elif pay[1] >= sc.get("salary_target", 0) * 0.93:
                pen += 5
                parts.append(f"pay up to ${pay[1]:,.0f} +5")

        yrs = self._years_required(desc)
        ypen = 0
        max_y = sc.get("max_years_experience")
        if yrs and max_y is not None and yrs > max_y:
            ypen = -min(40, 12 * (yrs - max_y))
            parts.append(f"asks {yrs}+ yrs {ypen}")

        total = max(0, min(100, t + d + bonus + pen + ypen))
        return total, "; ".join(parts) or "no keyword matches"

    @staticmethod
    def _pay(job):
        """(low, high) annual pay from the posting text, or None. Ignores hourly figures and huge numbers."""
        text = job.description + " " + str(job.extra.get("comp") or "")
        vals = []
        for m in re.finditer(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK]\b|\d{5,6})(?![\d,])", text):
            raw = m.group(1).replace(",", "").replace(" ", "")
            v = float(raw[:-1]) * 1000 if raw[-1] in "kK" else float(raw)
            if 30000 <= v <= 500000:
                vals.append(v)
        return (min(vals), max(vals)) if vals else None

    @staticmethod
    def _years_required(desc: str) -> int:
        found = []
        for m in re.finditer(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs)[^.\n]{0,40}experience", desc):
            found.append(int(m.group(1)))
        for m in re.finditer(r"experience[^.\n]{0,25}?(\d{1,2})\s*\+?\s*(?:years?|yrs)", desc):
            found.append(int(m.group(1)))
        found = [y for y in found if 1 <= y <= 15]
        return max(found) if found else 0

    # ------------------------------------------------------------------ 2. tailor
    def _rel(self, text: str, keywords: list[str], jd_terms: set[str], jd_lower: str) -> float:
        s = len(_terms(text) & jd_terms)
        s += 3 * sum(1 for k in keywords if k.lower() in jd_lower)
        return s

    @staticmethod
    def _bullet(b):
        return (b, []) if isinstance(b, str) else (b["text"], b.get("keywords", []) or [])

    def tailor(self, job) -> tuple[str, str]:
        p = self.profile
        jd_lower = (job.title + "\n" + job.description).lower()
        jd_terms = _terms(job.title + " " + job.description)
        max_b = self.cfg.get("resume", {}).get("max_bullets_per_role", 4)
        L = [f"# {p['name']}", p.get("contact_line", ""), ""]

        summary = p.get("summary", "")
        best = -1
        for v in p.get("summary_variants", []) or []:
            r = self._rel(v["text"], v.get("keywords", []), jd_terms, jd_lower)
            if r > best and r > 0:
                best, summary = r, v["text"]
        if summary:
            L += ["## Summary", " ".join(str(summary).split()), ""]

        top_bullets = []

        def emit(item, head):
            ranked = []
            for i, b in enumerate(item.get("bullets", [])):
                text, kws = self._bullet(b)
                ranked.append((self._rel(text, kws, jd_terms, jd_lower), -i, text))
            ranked.sort(reverse=True)
            keep = ranked[: item.get("max_bullets", max_b)]
            top_bullets.extend((r, t) for r, _, t in keep)
            L.append(head)
            if item.get("url"):
                L.append(item["url"])
            L.extend(f"- {t}" for _, _, t in keep)
            L.append("")

        L.append("## Experience")
        for role in p.get("experience", []):
            head = f"### {role['title']}, {role['company']}"
            if role.get("location"):
                head += f" — {role['location']}"
            emit(role, f"{head} · {role.get('dates', '')}".rstrip(" ·"))
        if p.get("projects"):
            L.append("## Projects")
            for pr in p["projects"]:
                head = f"### {pr['name']}" + (f" — {pr['tagline']}" if pr.get("tagline") else "")
                emit(pr, f"{head} · {pr.get('dates', '')}".rstrip(" ·"))

        if p.get("education"):
            L.append("## Education")
            for e in p["education"]:
                L.append(f"### {e['degree']}, {e['school']} · {e.get('date', '')}".rstrip(" ·"))
                L += [f"- {b}" for b in e.get("bullets", [])[:3]] + [""]

        matched_skills = []
        if p.get("skills"):
            L.append("## Skills")
            for g in p["skills"]:
                items = g["items"]
                ordered = sorted(items, key=lambda s: (0 if s.lower() in jd_lower else 1))
                matched_skills += [s for s in items if s.lower() in jd_lower]
                L.append(f"**{g['group']}:** {', '.join(ordered)}  ")
            L.append("")

        self._lazy = (top_bullets, matched_skills)     # kept so a letter can be written later IF a form requires one
        self._letters = getattr(self, "_letters", {})
        return "\n".join(L).strip() + "\n", ""

    def lazy_letter(self, job) -> str:
        """Cover letters are only written for forms that require one (free-LLM letters are the riskiest thing we write)."""
        if job.key in self._letters:
            return self._letters[job.key]
        letter = None
        wcfg = self.cfg.get("writer", {}) or {}
        if self.writer and self.writer.ready() and wcfg.get("cover_letter", True):
            try:
                letter = self.writer.cover_letter(job, self._company_name(job), self._log)
            except WriterUnavailable as e:
                self._log(f"      writer unavailable for cover letter, using template ({str(e)[:80]})")
        if not letter:
            letter = self._letter(job, *self._lazy)
        self._letters[job.key] = letter.strip() + "\n"
        return self._letters[job.key]

    def _company_name(self, job) -> str:
        names = self.cfg.get("company_names", {}) or {}
        if getattr(job, "extra", None) and job.extra.get("company_name"):
            return job.extra["company_name"]
        return names.get(job.company) or re.sub(r"[-_]+", " ", job.company).title()

    def _letter(self, job, top_bullets, matched_skills) -> str:
        p, f = self.profile, self.facts
        tpl = p.get("cover_letter") or DEFAULT_LETTER
        top = [t for _, t in sorted(top_bullets, key=lambda x: -x[0])]
        h = (top + ["", ""])[:2]
        skills = ", ".join(dict.fromkeys(matched_skills[:4])) or ", ".join(
            (p.get("skills") or [{"items": []}])[0]["items"][:3])
        mapping = dict(company=self._company_name(job), role=job.title, name=f["full_name"],
                       first_name=f["first_name"], skills=skills, h1=h[0].rstrip("."), h2=h[1].rstrip("."))
        out = tpl.format_map(_Safe(mapping))
        return re.sub(r"\n[•-] *\n", "\n", out).strip() + "\n"

    # ------------------------------------------------------------------ 3. answer forms
    def map_fields(self, job, fields: list[dict], cover_letter: str) -> dict:
        self._job = job
        answers, missing = {}, []
        ctx = dict(company=self._company_name(job), role=job.title)
        for fld in fields:
            val = self._answer(fld, cover_letter, ctx)
            if val is None:
                if fld.get("required"):
                    missing.append(fld["id"])
            else:
                answers[fld["id"]] = val
        return {"answers": answers, "unanswerable_required": missing}

    def _location_ok(self) -> bool:
        """True when the job is remote or in a place listed in facts.relocation_ok_locations."""
        loc = (self._job.location if self._job else "").lower()
        if not loc or "remote" in loc:
            return True
        ok = [str(x).lower() for x in self.facts.get("relocation_ok_locations", []) or []]
        return "*" in ok or any(x in loc for x in ok)

    @staticmethod
    def _nums(text: str) -> list[float]:
        out = []
        for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?", text):
            v = float(m.group(1).replace(",", ""))
            out.append(v * 1000 if m.group(2) else v)
        return out

    def _travel_option(self, opts, kind):
        """Most open-to-travel choice up to facts.travel_max_percent (so the answer never reads as unwilling)."""
        cap = float(self.facts.get("travel_max_percent", 50))
        best, best_v = None, -1.0
        for o in opts:
            if re.search(r"\bno\b|none|never|not willing|unable", o, re.I):
                continue
            nums = [n for n in self._nums(o) if n <= 100]
            v = max(nums) if nums else None
            if v is not None and v <= cap and v > best_v:
                best, best_v = o, v
        best = best or pick_option(opts, "Yes")
        return ([best] if kind == "checkbox_group" else best) if best else None

    def _salary_option(self, opts, kind):
        """Range choice containing your target salary (or the nearest one)."""
        target = float(self.facts.get("salary_number") or 0)
        if not target:
            return None
        best, gap = None, 1e12
        for o in opts:
            nums = [n for n in self._nums(o) if n >= 1000] or [n * 1000 for n in self._nums(o) if 20 <= n <= 400]
            if not nums:
                continue
            lo, hi = min(nums), max(nums)
            d = 0 if lo <= target <= hi else min(abs(target - lo), abs(target - hi))
            if d < gap:
                best, gap = o, d
        return ([best] if kind == "checkbox_group" else best) if best else None

    def _fact(self, keys):
        return next((self.facts[k] for k in keys if self.facts.get(k) not in (None, "")), None)

    def _answer(self, f: dict, letter: str, ctx: dict):
        kind = f["kind"]
        text = f"{f.get('label', '')} {f.get('question', '')}".strip()
        low = text.lower()
        has_opts = kind in ("select", "radio", "combobox", "checkbox_group")
        opts = f.get("options") or []

        if kind == "file":
            if re.search(r"cover", low):
                return "COVER_LETTER" if f.get("required") else None
            if re.search(r"resume|résumé|cv\b|curriculum", low):
                return "RESUME"
            return None
        if kind == "textarea" and re.search(r"cover letter", low):
            return (letter or self.lazy_letter(self._job)) if f.get("required") else None

        # 1) user's own canned answers win
        for rx, ans in self.answers:
            if rx.search(low):
                ans = ans.format_map(_Safe(ctx))
                return self._resolve(f, ans, opts) if has_opts else ans

        if kind == "checkbox_single":
            if re.search(r"\bsms\b|text messag|texts? (you|me)|via text", low) and not AI_WORDS.search(low) \
                    and re.match(r"y", str(self.facts.get("sms_consent", "")), re.I):
                return True          # you allowed SMS contact
            if OPTIONAL_CHECK.search(low) or AI_WORDS.search(low):
                return None          # never tick a statement about AI use on the user's behalf
            return True if CONSENT.search(low) else None
        if kind == "number" and re.search(r"salary|compensation|pay", low):
            return self.facts.get("salary_number") or None

        if kind == "checkbox_group" and re.search(r"language", low):
            got = [o for o in opts if re.search(r"\b(english|spanish|espa[nñ]ol)\b", o, re.I)]
            if got:
                return got
        if kind in ("select", "combobox") and opts and (not f.get("label") or f["label"].lstrip().startswith("cards[")):
            got = self._by_options(opts)
            if got:
                return got
        # 2) facts
        if kind != "textarea":
            for rx, keys, mode in FIELD_RULES:
                if not rx.search(low):
                    continue
                if mode == "choice" and not has_opts:
                    continue
                if mode == "loc" and not self._location_ok():
                    return None      # relocation / in-office answers are only given for places you said yes to
                val = self._fact(keys)
                if mode == "travel" and has_opts:
                    return self._travel_option(opts, kind)
                if mode == "salary" and has_opts:
                    return self._salary_option(opts, kind)
                if mode == "demo":
                    if has_opts:
                        return self._demo_option(keys[0], val, opts, f["kind"])
                    return DEMO_TEXT.get(_norm(val).replace(" ", "_"), str(val)) if val else None
                if val is None:
                    return None
                if has_opts:
                    got = self._resolve(f, str(val), opts)
                    if got:
                        return got
                    continue         # this rule can't pick an option here; let a later rule try
                return str(val)
            if has_opts and re.search(r"experience|proficien|familiar|comfortable|skilled|knowledge", low) and any(
                    re.search(r"(?<![\w+#])" + re.escape(s) + r"(?![\w+#])", low) for s in self.skills):
                got = pick_option(opts, "Yes")
                if got:
                    return got

        if kind in ("select", "combobox") and opts:
            got = self._by_options(opts)
            if got:
                return got
        if kind in ("radio", "checkbox_group") and opts and re.search(r"hear|find out|learn about|source|how did you", low):
            got = self._by_options(opts)
            if got:
                return [got] if kind == "checkbox_group" else got

        # 2b) things that simply don't apply to you -> "N/A" (short text fields only, never essays)
        if kind in ("text", "textarea") and len(low) < 160 and NA_RX.search(low) and not re.search(r"\bwhy\b|describe|explain|tell us", low):
            return "N/A"
        if has_opts and NA_RX.search(low):
            got = next((o for o in opts if re.search(r"n/?a\b|not applicable|none of the above", o, re.I)), None)
            if got:
                return [got] if kind == "checkbox_group" else got

        # 3) open-ended prompt -> free LLM writer (required questions only, unless answer_optional)
        return self._write(f, low, kind)

    def _by_options(self, opts):
        """Recognise a question from the shape of its options (school lists, 'how did you hear' lists)."""
        schools = sum(bool(re.search(r"universit|college|institute|school of", o, re.I)) for o in opts)
        if len(opts) >= 20 and schools >= len(opts) * 0.3 and self.facts.get("school"):
            return pick_option(opts, self.facts["school"]) or next((o for o in opts if "southern california" in o.lower()), None)
        if any(re.search(r"glassdoor|linkedin|indeed|job board|friend or family|career site|ziprecruiter|builtin", o, re.I) for o in opts) \
                and any(re.search(r"friend|referr|recruit|campus|glassdoor|linkedin|indeed|job board", o, re.I) for o in opts) and len(opts) >= 4:
            for pat in (r"^linkedin", r"^indeed", r"glassdoor", r"job board|job site", r"builtin", r"^other"):
                for o in opts:
                    if re.search(pat, o, re.I):
                        return o
        return None

    def _write(self, f, low, kind):
        if not self.writer or not self.writer.ready():
            return None
        open_prompt = kind == "textarea" or (kind == "text" and len(f.get("label", "")) > 60 and PROMPT_RX.search(low))
        if not open_prompt:
            return None
        if not f.get("required") and not (self.cfg.get("writer", {}) or {}).get("answer_optional", False):
            return None
        question = f.get("label", "") + ((" " + f["question"]) if f.get("question") else "")
        try:
            return self.writer.answer(self._job, self._company_name(self._job), question.strip(),
                                      limits_from_question(question, f.get("maxlength")), self._log)
        except WriterUnavailable as e:
            self._log(f"      writer unavailable: {str(e)[:100]}")
            return None

    def _resolve(self, f, value, opts):
        if f["kind"] == "combobox" and not opts:
            return value
        if f["kind"] == "checkbox_group":
            got = pick_option(opts, value)
            return [got] if got else None
        return pick_option(opts, value)

    def _demo_option(self, fkey, val, opts, kind):
        """Voluntary self-identification: the user's canned value if set and matchable, else 'decline to answer'."""
        def decline():
            for o in opts:
                if DECLINE.search(o):
                    return [o] if kind == "checkbox_group" else o
            return None
        norm = _norm(val).replace(" ", "_") if val else ""
        if not norm or DECLINE.search(str(val)):
            return decline()
        got = None
        spec = DEMO_RX.get(fkey, {}).get(norm)
        if spec:
            must, notx = spec
            got = next((o for o in opts if re.search(must, o, re.I) and not re.search(notx, o, re.I)), None)
        else:
            got = pick_option(opts, val)
        if got is None:
            return decline()
        return [got] if kind == "checkbox_group" else got


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


DEFAULT_LETTER = """Dear {company} hiring team,

I'm applying for the {role} role. My background in {skills} lines up closely with what you're looking for.

A few things from my background that map to this role:
• {h1}
• {h2}

I'd welcome the chance to talk about how I can help {company}. Thank you for your time.

{name}
"""
