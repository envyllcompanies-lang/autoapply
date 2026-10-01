"""Decision layer: rule-based scoring, résumé assembly and form answering, plus a free-LLM writer for essay questions.

Résumé and letter content is only ever *selected and reordered* from what you wrote in profile.yaml. Form answers
come only from config.yaml (`facts`, `answers`) or, for open-ended prompts, from the writer (see writer.py), which is
limited to the same facts and discards anything it can't ground.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .sources import norm_location, level_title
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
CONSENT = re.compile(r"agree|acknowledge|consent|certify|confirm|accept|privacy|terms|policy|true and (correct|accurate)|\bunderstand\b|\battest\b|\baffirm\b|\bdeclare\b", re.I)
OPTIONAL_CHECK = re.compile(r"marketing|newsletter|updates|receive|sms|text message|future (roles|opportunities)|talent (pool|community)", re.I)
AI_WORDS = re.compile(r"\b(ai|a\.i\.|chatgpt|generative|llm|artificial intelligence)\b", re.I)
PROMPT_RX = re.compile(
    r"\b(why|describe|tell us|tell me|explain|share|give (us )?an example|walk us|what (makes|excites|motivates|interests|"
    r"draws|would|do you|are you|is your|has been)|how (do|did|would|have|will)|in your own words|your (experience|approach|"
    r"interest|background|motivation)|elaborate|proud|challenge|accomplishment|achievement|tell us about)\b", re.I)

# Things the bot never agrees to or certifies on the person's behalf (the job is skipped instead)
LEGAL_RX = re.compile(r"arbitrat|waive|jury|non-?compete|non-?solicit|indemnif|release of claims|class action|liquidated|"
                      r"invention assignment|intellectual property assignment|forfeit", re.I)
AI_POLICY_RX = re.compile(r"\bai\b.{0,25}\b(policy|guidelines?|statement)\b|\b(policy|guidelines?)\b.{0,40}\b(use of |using )?(ai|artificial intelligence)\b|"
                          r"use of (ai|artificial intelligence) (in|during) (the )?(application|hiring|interview)", re.I)
QUALIFY_CERT_RX = re.compile(r"minimum (basic )?qualifications|meet (all )?the (basic|minimum|required)|possess (all )?(of )?the required", re.I)
ACK_RX = re.compile(r"acknowledg|i agree|\b(i|we|hereby) certify\b|certify (that|the|this)|privacy (policy|notice|statement)|terms (of|and)|true and (correct|accurate|complete)|"
                    r"read and (understand|agree)|have read", re.I)
ACK_OPT_RX = re.compile(r"^\W*(i )?(acknowledge|agree|accept|confirm|understand|consent|certify|have read)|acknowledge|\b(agree|consent|accept|understand|have read|certify)\b", re.I)
ACK_NEG_RX = re.compile(r"\b(do not|don't|dont|decline|disagree|withhold|refuse|opt[- ]out|no,)\b|\bnot\b", re.I)
RESIDE_RX = re.compile(r"\b(reside|resident|residing|live in|living in|located in|located within|based in|currently (live|located|based)|"
                       r"are you (a )?local|commuting distance|do you live|metro area)\b", re.I)
HOME_TERMS = ("new castle", "garfield county", "glenwood", "carbondale", "rifle", "silt", "aspen", "roaring fork", "western slope")
OTHER_CO = re.compile(r"denver|boulder|front range|metro|colorado springs|fort collins|aurora|lakewood|littleton|golden|broomfield|"
                      r"longmont|westminster|arvada|centennial|castle rock|englewood", re.I)
FAMILY_RX = re.compile(r"(family|relatives?|spouse|partner|related).{0,40}(work|employ|at\b|with\b)|family members?|relatives?\b|"
                       r"(related|close personal).{0,40}(employee|officer|director|member)", re.I)
PRIOR_EMP_RX = re.compile(r"(ever|previously|formerly|currently)\W+(been\W+)?(employed|worked|working|contracted|interned)\W+(at|by|for|with)\b|"
                          r"(worked|employed|working)\W+(for|at|with|by)\W+.{0,60}(before|previously|prior|any other time|in the past|ever)", re.I)
YEARS_Q = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:or more\s*)?(?:years?|yrs?)\b|(?:at least|minimum(?: of)?|over|more than)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)", re.I)
_UNSET = object()

# (regex over "label question", fact keys in priority order, mode)
#   mode: None = normal | "choice" = only for option fields (select/radio/checkbox group) | "demo" = voluntary self-ID
_AI_Q = (r"\b(ai|a\.i\.|chatgpt|generative|artificial intelligence|llm)\b.{0,60}\b(use[ds]?|using|assist\w*|generat\w*|tools?|help\w*)\b|"
         r"\b(use[ds]?|using|assist\w*|help\w*)\b.{0,60}\b(ai|a\.i\.|chatgpt|generative|artificial intelligence|llm)\b")
FIELD_RULES = [
    (r"first.?generation", ("first_generation",), "choice"),
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
    (r"^\W*(city|town|city ?/ ?town|city or town)\W*$", ("city",), None),
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
     r"unrestricted|work in the (u\.?s|united states)|proof of (eligibility|work authori|identity)|verify.{0,30}(eligib|authori)", ("authorized_to_work_in_us",), None),
    (r"note-?taker|(record|transcri)\w* (of )?(the |our )?(interview|call|meeting)", ("ai_notetaker_consent",), "choice"),
    (r"plan on working from|payroll tax|state (do you|will you) (work|reside|live)", ("state",), None),
    (r"relocat", ("willing_to_relocate",), "loc"),
    (r"\btravel", ("willing_to_travel",), "travel"),
    (r"hybrid|on-?site|in-?office|in the office|in-?person|our offices?|anchor days?|days? ?(a|per|/) ?week|commut", ("open_to_onsite",), "loc"),
    (r"remote", ("open_to_remote",), "choice"),
    (r"start date|earliest.*start|available to start|notice period|when (can|could) you start|availability|next career move|when are you looking", ("earliest_start_date",), None),
    (r"salary|compensation|pay expectation|desired pay|expected pay|pay range", ("salary_expectation",), "salary"),
    (r"(related|relative|friend|family).{0,60}(work|employ)|family members?|\brelatives?\b|know (anyone|someone|any)|current(ly)? employees?.{0,40}(know|refer)|have you been referred|were you referred", ("know_employee",), "choice"),
    (r"\bsms\b|text messag|text you|contact (you )?(by|via) text|consent to (receive )?texts?\b|\btexts? from", ("sms_consent",), "choice"),
    (r"referred by|referrer|referral (name|employee|code|email)|employee referral|name of (the )?(employee|person)", ("referral_name",), None),
    (r"how did you (hear|find|learn)|hear about (us|this|the)|where did you (hear|find|learn)|referral source", ("how_did_you_hear",), None),
    (r"(previously|ever|formerly) (been )?(employed|worked)|former employee|worked (for|at) .{0,40}before|"
     r"(ever|previously|formerly|currently)\W+(been\W+)?(employed|worked|working|contracted|interned)\W+(at|by|for|with)\b|"
     r"(worked|employed|working)\W+(for|at|with|by)\W+.{0,60}(before|previously|prior|any other time|in the past|ever)", ("previously_employed_here",), "choice"),
    (r"(current|former)( or (current|former))?\W+(\w+\W+){0,3}(employee|associate|team member|contractor)\b", ("previously_employed_here",), "choice"),
    (r"18 years|over 18|at least 18|legal age|age of 18", ("over_18",), None),
    (r"(currently|presently)\W+(a\W+)?(full.?time\W+|part.?time\W+)?(student|enrolled)|are you (a|an) (full.?time |part.?time )?student|"
     r"enrolled in (a |an )?(degree|school|college|university|program)", ("currently_student",), None),
    (r"(have|hold|completed|earned|obtained)\W+(a\W+|an\W+)?(bachelor|bs\b|b\.s\.|college degree|four.year degree|4.year degree|undergraduate degree)", ("has_bachelors_degree",), "choice"),
    (r"(have|hold|completed|earned|obtained)\W+(a\W+|an\W+)?(master|graduate degree|mba\b|phd|doctora|advanced degree)", ("has_graduate_degree",), "choice"),
    (r"(available|able|willing|open)\W+(to\W+)?work\W+(full.?time|40 hours|a full)", ("available_full_time",), "choice"),
    (r"essential (job )?functions|with or without (a )?(reasonable )?accommodation", ("can_perform_essential_functions",), "choice"),
    (r"(comfortable|able|thrive|enjoy|excel|experienced)\W.{0,40}(fast.?paced|deadline|multi-?task|dynamic environment|changing priorit|ambiguity|under pressure)", ("comfortable_general",), "choice"),
    (r"currently (employed|working)", ("currently_employed",), None),
    (r"contact .{0,30}employer", ("may_contact_employer",), None),
    (r"background (check|screen|investigation)|consent to .{0,30}(check|screening)", ("background_check_consent",), None),
    (r"drug (test|screen)", ("drug_test_consent",), None),
    (r"convict|felony|misdemeanor|criminal", ("criminal_conviction",), None),
    (r"driver'?s? licen[cs]e|valid driver", ("has_drivers_license",), None),
    (r"reliable transportation", ("reliable_transportation",), None),
    (r"speak spanish|spanish.{0,40}(fluen|proficien|speak|skills|language)|bilingual|language skills", ("speaks_spanish",), None),
    (r"languages? (do you )?(speak|spoken|proficien)|fluent in", ("languages",), None),
    (r"\bgpa\b|grade point", ("gpa",), None),
    (r"highest (level of )?(\w+ )?(education|degree)|level of (\w+ )?education|education level", ("education_level",), None),
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


HUMAN_CHECK_RX = re.compile(r"not a robot|i'?m (a )?human|are you (a )?human|prove you|captcha|verify (that )?you are (a )?(human|person)|human verification", re.I)
NEG_Q_RX = re.compile(r"\b(are|do|does|did|is|will|would|can|could|have|has)\s+(you\s+|there\s+)?(not|n't)\b|\b(unable to|cannot|can't|cant|won't)\b|\b(are|is)\s+not\b", re.I)
NEG_TOPIC_RX = re.compile(r"authori[sz]|sponsor|eligible|legally|right to work|on-?site|in[- ]office|relocat|commut|reason\W.{0,30}(cannot|unable)", re.I)


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
        self.answers = [(re.compile(a["match"], re.I), a["answer"], bool(a.get("only_options")))
                        for a in cfg.get("answers", []) or []]
        self.skills_raw = [s.lower() for g in profile.get("skills", []) or [] for s in g["items"]]
        self.skills = [re.sub(r"\s*\(.*?\)", "", s.lower()).strip() for g in profile.get("skills", []) or [] for s in g["items"]]
        self.applied_before: set[str] = set()      # company slugs this bot already applied to (set by main from the database)
        self._refuse_unfinished_setup(base)
        w = cfg.get("writer", {}) or {}
        self.writer = Writer(cfg, profile, base) if w.get("enabled") else None
        if self.writer:
            log(self.writer.status_line())

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
        title, desc, loc = level_title(job.title).lower(), job.description.lower(), norm_location(job.location)
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

        # entry-level wording up, senior wording down (title only)
        adj = 0
        for k, v in (sc.get("title_adjust") or {}).items():
            if re.search(r"(?<![a-z])" + re.escape(k.lower()) + r"(?![a-z])", title):
                adj += v
        adj = max(-30, min(15, adj))
        if adj:
            parts.append(f"title level {adj:+d}")

        ceiling = None
        pay = self._pay(job)
        if pay:
            floor = sc.get("salary_floor")
            if floor and pay[1] < floor:
                pen -= 40
                parts.append(f"pay tops out near ${pay[1]:,.0f} (below your ${floor:,} floor) -40")
            elif pay[1] >= sc.get("salary_target", 0) * 0.93:
                pen += 5
                parts.append(f"pay up to ${pay[1]:,.0f} +5")
            hard_pay = sc.get("pay_min_ceiling", 115000)
            if hard_pay and pay[0] >= hard_pay:
                ceiling = 25
                parts.append(f"pay starts at ${pay[0]:,.0f}, far above your target (senior role) = skip")

        yrs = self._years_required(desc)
        ypen = 0
        max_y = sc.get("max_years_experience")
        if yrs and max_y is not None and yrs > max_y:
            ypen = -min(40, 12 * (yrs - max_y))
            parts.append(f"asks {yrs}+ yrs {ypen}")
        hard_y = sc.get("max_years_hard", 6)
        if hard_y and yrs >= hard_y:
            ceiling = 20
            parts.append(f"needs {yrs}+ years = skip")

        total = max(0, min(100, t + d + bonus + pen + ypen + adj))
        if ceiling is not None:
            total = min(total, ceiling)
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

    def lazy_letter(self, job, required: bool = False) -> str | None:
        """A full one-page letter from the writer, written only for forms that ask for one. When the writer can't produce
        one: an optional letter is left out (None); a REQUIRED one gets your standard full-length letter from config.yaml
        (cover_letter_template), so the application still goes in."""
        if job.key in self._letters:
            return self._letters[job.key]
        letter = None
        if self.writer and self.writer.ready() and (self.cfg.get("writer", {}) or {}).get("cover_letter", True):
            try:
                letter = self.writer.cover_letter(job, self._company_name(job), self._log)
            except WriterUnavailable as e:
                self._log(f"      writer unavailable for cover letter ({str(e)[:80]})")
        if not letter and required:
            letter = self.template_letter(job)
            if letter:
                self._log("      (used your standard cover letter: this form requires one and the writer couldn't write it)")
        if not letter:
            return None
        self._letters[job.key] = letter.strip() + "\n"
        return self._letters[job.key]

    def template_letter(self, job) -> str | None:
        tpl = (self.cfg.get("cover_letter_template") or "").strip()
        if not tpl:
            return None
        return tpl.format_map(_Safe(dict(company=self._company_name(job), role=level_title(job.title).strip() or "open",
                                         name=self.facts.get("full_name", ""), first_name=self.facts.get("first_name", "")))).strip()

    def _company_name(self, job) -> str:
        names = self.cfg.get("company_names", {}) or {}
        if getattr(job, "extra", None) and job.extra.get("company_name"):
            return job.extra["company_name"]
        if names.get(job.company):
            return names[job.company]
        if not hasattr(self, "_wd_names"):          # Workday boards carry their display name: 'wf/wd1/WellsFargoJobs/Wells Fargo'
            self._wd_names = {}
            for spec in ((self.cfg.get("companies") or {}).get("workday") or []):
                parts = str(spec).split("/")
                if len(parts) > 3 and parts[3].strip():
                    self._wd_names.setdefault(parts[0].lower(), parts[3].strip())
        got = self._wd_names.get(str(job.company).lower())
        if got:
            return got
        name = re.sub(r"[-_]+", " ", job.company).strip()
        return name.title() if name.islower() or name.isupper() else name

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
        ctx = dict(company=self._company_name(job), role=job.title, today=date.today().strftime("%m/%d/%Y"))
        exp_vals = self._experience_answers(fields)          # Workday 'My Experience': your real jobs, never the one applied to
        for fld in fields:
            if fld["id"] in exp_vals:
                if exp_vals[fld["id"]] is not None:
                    answers[fld["id"]] = exp_vals[fld["id"]]
                continue
            val = self._answer(fld, cover_letter, ctx)
            if val is None and fld.get("required"):
                val = self._llm_fill(fld)
            if val is None:
                if fld.get("required"):
                    missing.append(fld["id"])
            else:
                answers[fld["id"]] = val
        self._place_resume(fields, answers, missing)
        return {"answers": answers, "unanswerable_required": missing}

    _NOT_RESUME_FILE = re.compile(r"cover|transcript|portfolio|writing sample|photo|headshot|picture|certificat|licen[cs]e|passport|"
                                  r"\bid\b|identification|reference|recommendation|additional|other|supporting|work sample", re.I)
    _AUTOFILL_FILE = re.compile(r"auto-?fill|autocomplete|parse|import|quick apply|apply with", re.I)

    def _place_resume(self, fields, answers, missing):
        """Every application gets the résumé: if no upload box was recognised as the résumé by its label, the first plain
        upload box is used for it. An 'autofill from résumé' box is only used when it is the only one."""
        files_ = [f for f in fields if f["kind"] == "file"]
        if not files_:
            return
        res = [f for f in files_ if answers.get(f["id"]) == "RESUME"]
        if len(res) > 1:
            real = [f for f in res if not self._AUTOFILL_FILE.search(f.get("label", "") + " " + str(f.get("hint") or ""))]
            if real:
                for f in res:
                    if f not in real and not f.get("required"):
                        answers.pop(f["id"], None)
        if res:
            return
        cand = [f for f in files_ if f["id"] not in answers and not self._NOT_RESUME_FILE.search(f.get("label", "") + " " + str(f.get("hint") or ""))]
        if cand:
            answers[cand[0]["id"]] = "RESUME"
            if cand[0]["id"] in missing:
                missing.remove(cand[0]["id"])

    _NEVER_GUESS = re.compile(r"gender|\bsex\b|race|ethnic|hispanic|latin[oa]|veteran|disab|sexual|orientation|pronoun|transgender|"
                              r"lgbt|religio|marital|date of birth|birth ?date|social security|ssn|salary history|current salary|"
                              r"lift|password|signature|initials|full legal name", re.I)

    def _llm_fill(self, f: dict):
        """Last resort for a REQUIRED field no rule could answer: the writer picks the true option (or writes a few words)
        from your facts, so an ordinary question never ends the application. Never used for human checks, legal waivers,
        AI-policy confirmations, negated authorization questions or demographics."""
        if not (self.writer and self.writer.ready() and self._job is not None):
            return None
        kind = f["kind"]
        text = f"{f.get('label', '')} {f.get('question', '')}".strip()
        low = text.lower()
        if not text or HUMAN_CHECK_RX.search(low) or LEGAL_RX.search(low) or AI_POLICY_RX.search(low) or AI_WORDS.search(low) \
                or QUALIFY_CERT_RX.search(low) or self._NEVER_GUESS.search(low):
            return None
        if NEG_Q_RX.search(re.sub(r"(including )?(but )?not limited to|not (just|only) limited to", " ", low)) and NEG_TOPIC_RX.search(low):
            return None
        if re.search(r"relocat|commut|on-?site|in[- ]office|in person|hybrid", low) and not self._location_ok(low):
            return None
        opts = f.get("options") or []
        company = self._company_name(self._job)
        try:
            if kind in ("select", "radio", "combobox") and opts:
                got = self.writer.choose(self._job, company, text, opts, multi=False, log=self._log)
            elif kind == "checkbox_group" and opts:
                got = self.writer.choose(self._job, company, text, opts, multi=True, log=self._log)
            elif kind in ("text", "number") and len(low) < 300:
                got = self.writer.short_answer(self._job, company, text, int(f.get("maxlength") or 150), self._log)
                if got and kind == "number":
                    m = re.search(r"\d+(?:\.\d+)?", got)
                    got = m.group(0) if m else None
            else:
                return None
        except WriterUnavailable as e:
            self._log(f"      writer unavailable: {str(e)[:100]}")
            return None
        if got:
            self._log(f"      (writer answered {text[:60]!r} -> {str(got)[:50]!r})")
        return got or None

    def _location_ok(self, label: str = "") -> bool:
        """True when the job is remote or in a place listed in facts.relocation_ok_locations."""
        loc = norm_location(self._job.location if self._job else "")
        if not loc or "remote" in loc or re.fullmatch(r"(united states|usa|us|u\.s\.a?\.?|united states of america|anywhere|nationwide)( \(.*\))?", loc.strip()):
            return True
        ok = [str(x).lower() for x in self.facts.get("relocation_ok_locations", []) or []]
        lab = norm_location(label)
        return "*" in ok or any(x in loc for x in ok) or bool(lab and any(re.search(r"(?<![\w-])" + re.escape(x) + r"(?![\w-])", lab) for x in ok))

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
        if HUMAN_CHECK_RX.search(low):
            return None          # "I'm not a robot" style boxes are the site's human check: never ticked by the bot
        has_opts = kind in ("select", "radio", "combobox", "checkbox_group")
        opts = f.get("options") or []

        if kind == "file":
            both = low + " " + str(f.get("hint") or "").lower().replace("_", " ").replace("-", " ")
            if re.search(r"cover", both):
                return "COVER_LETTER" if self.cfg.get("cover_letters", False) else None   # optional ones too: a full letter helps
            if re.search(r"resume|r\u00e9sum\u00e9|\bcv\b|curriculum", both):
                return "RESUME"
            return None
        if kind == "textarea" and re.search(r"cover letter", low):
            if not self.cfg.get("cover_letters", False):
                return None          # cover letters are switched off: a form that requires one is skipped
            return letter or self.lazy_letter(self._job, required=bool(f.get("required"))) or None   # optional + no letter: left empty

        # 1) user's own canned answers win
        for rx, ans, only_opts in self.answers:
            if rx.search(low) and (has_opts or not only_opts):
                ans = ans.format_map(_Safe(ctx))
                if re.fullmatch(r"\d+(?:\.\d+)?", ans.strip()) and self._names_unlisted_tool(text):
                    ans = "0"          # 'years of experience with <a tool that is not on the résumé>': the truthful number is zero
                if has_opts and re.fullmatch(r"\d+(?:\.\d+)?", ans.strip()):
                    got = self._years_option(low, opts, float(ans), kind)     # '3+ years?' Yes/No, or a '3-5 years' bucket
                else:
                    got = self._resolve(f, ans, opts) if has_opts else ans
                if got is not None or not only_opts:
                    return got

        sp = self._special(f, low, kind, opts, has_opts)
        if sp is not _UNSET:
            return sp

        lab = str(f.get("label", "")).lower()
        if kind == "wddate":
            if re.match(r"\W*(today.s )?date\W*(signed)?\W*\*?\W*$|.*(signature|signed on)", lab):
                return date.today().strftime("%m/%d/%Y")          # the signature date on a self-identification form
            if re.search(r"start|available|begin", lab):
                v = self.facts.get("earliest_start_date_value") or ""
                return v or None
            return None
        if kind == "checkbox_single" and re.search(r"\(united states( of america)?\)\s*\*?\s*$", lab) and \
                re.search(r"american indian|asian|black|hispanic|latino|pacific islander|white|two or more|not specified|decline", lab):
            low = lab
            want = str(self.facts.get("race_ethnicity") or "")      # Workday draws race/ethnicity as one box per choice
            if want and re.search(r"hispanic|latino", want, re.I):
                return True if re.search(r"hispanic|latino", low) else ""
            if want and _norm(want).split()[0] in low:
                return True
            return True if (not want and re.search(r"not specified|decline", low)) else ""
        if kind == "checkbox_single" and re.match(r"\W*(yes, i have a disability|no, i (do not|don.t) have a disability|"
                                                  r"i (do not|don.t) (want|wish) to (answer|self.identify))", lab):
            d = str(self.facts.get("disability_status") or "").lower()      # Workday's disability form: one box per choice
            if d in ("none", "no", "not disabled", "no disability"):
                return True if re.match(r"\W*no, i", lab) else ""
            if d in ("yes", "disabled"):
                return True if re.match(r"\W*yes, i", lab) else ""
            return True if re.match(r"\W*i (do not|don.t)", lab) else ""
        if kind == "checkbox_single":
            if re.search(r"\bsms\b|text messag|texts? (you|me)|via text", low) and not AI_WORDS.search(low) \
                    and re.match(r"y", str(self.facts.get("sms_consent", "")), re.I):
                return True          # you allowed SMS contact
            stmt = self._statement_box(low)
            if stmt is not _UNSET:
                return stmt
            if OPTIONAL_CHECK.search(low) or AI_WORDS.search(low):
                return None          # never tick a statement about AI use on the user's behalf
            if LEGAL_RX.search(low) or QUALIFY_CERT_RX.search(low):
                return None          # never agree to arbitration / waivers or certify qualifications for the user
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
        if kind in ("text", "textarea") and len(low) < 40 and (re.search(r"(address|street)( line)? ?2\b|\bline 2\b", low) or
                                                               re.match(r"\W*(apt\.?|apartment|suite|unit)\b(?!.*(street|address))", low)):
            return ""                                 # no second address line
        if kind in ("text", "tel", "combobox", "wdprompt", "select") and re.search(r"country (phone )?code|phone (country )?code|dial(l?ing)? code", low) and len(low) < 40:
            if has_opts and opts:
                got = next((o for o in opts if re.search(r"united states|\busa?\b", o, re.I) and "+1" in o), None)
                if got:
                    return got
            return ""                                 # already set to United States (+1) by the country choice: leave it
        if kind == "combobox" and re.match(r"\W*(overall|speaking|writing|reading|listening|comprehension|verbal|written)\W*\*?\W*$", lab) \
                and not any(re.search(r"fluent|native|advanced|expert|proficient", o, re.I) for o in (opts or [])):
            return "@highest"                          # Workday language rows whose choices load when opened
        if kind == "combobox" and re.match(r"\W*language\W*\*?\W*$", lab) and len([o for o in (opts or []) if not re.match(r"select", o, re.I)]) == 0:
            return "English"
        if has_opts and opts and re.match(r"\W*(overall|speaking|writing|reading|listening|comprehension|verbal|written)\W*\*?\W*$", lab) \
                and any(re.search(r"fluent|native|advanced|expert|proficient", o, re.I) for o in opts):
            for pat in (r"native|bilingual", r"fluent", r"expert|advanced", r"proficient"):
                got = next((o for o in opts if re.search(pat, o, re.I)), None)
                if got:
                    return [got] if kind == "checkbox_group" else got     # Workday language rows: the language picked is English
        if kind in ("text", "tel", "number") and re.search(r"(phone )?extension\b|^\W*ext\.?\W*$", low):
            return ""                                 # no extension
        # 2) facts (Workable draws every free-text question as a textarea: short factual ones get the fact too)
        short_box = kind != "textarea" or (len(low) < 160 and not re.search(
            r"\b(why|describe|explain|tell us|walk us|example|share (a|an|your (experience|approach|story))|how (do|did|would|have|will) you)\b", low))
        if short_box:
            for rx, keys, mode in FIELD_RULES:
                if not rx.search(low):
                    continue
                if mode == "choice" and not has_opts:
                    if kind in ("text", "textarea") and keys[0] == "know_employee" and self._fact(keys):
                        return str(self._fact(keys))      # 'Were you referred by a current employee? If so, who?' -> No
                    continue
                if mode == "loc" and not self._location_ok(low):
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
                    got = self._resolve(f, str(val), opts) or self._semantic(keys[0], val, opts, f["kind"], low)
                    if got:
                        return got
                    if kind == "combobox" and mode is None and len(low) < 40 and keys[0] in ("country", "state", "city", "location", "county"):
                        # a long list the page loads as you scroll (Workday's 250 countries): type the real value and
                        # let the list find it, never settle for whatever option happened to be loaded
                        return "United States of America" if keys[0] == "country" and re.match(r"(united states|us|usa)$", str(val), re.I) else str(val)
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

        # acknowledgements / certifications that the information given is true (never legal waivers, AI or qualification claims)
        if has_opts and ACK_RX.search(low) and not (OPTIONAL_CHECK.search(low) or AI_WORDS.search(low)):
            got = next((o for o in opts if ACK_OPT_RX.search(o) and not ACK_NEG_RX.search(o)), None) or pick_option(opts, "Yes")
            if got:
                return [got] if kind == "checkbox_group" else got

        # 2b) things that simply don't apply to you -> "N/A" (short text fields only, never essays)
        if kind in ("text", "textarea") and len(low) < 160 and NA_RX.search(low) and not re.search(r"\bwhy\b|describe|explain|tell us", low):
            return "N/A"
        if has_opts and NA_RX.search(low):
            got = next((o for o in opts if re.search(r"n/?a\b|not applicable|none of the above", o, re.I)), None)
            if got:
                return [got] if kind == "checkbox_group" else got

        # 2c) last resorts so an ordinary question never ends the application
        yn = has_opts and {_norm(o) for o in opts} <= {"yes", "no", "yes i do", "no i do not"} and len(opts) == 2
        if yn and not (LEGAL_RX.search(low) or AI_WORDS.search(low) or QUALIFY_CERT_RX.search(low) or HUMAN_CHECK_RX.search(low)):
            if re.search(r"(experience|proficien|familiar|knowledge|skilled|worked|used|trained|certified|certification|licen[sc]e[ds]?)\b", low) \
                    and not re.search(r"driver|drivers", low):
                return pick_option(opts, "No")       # a tool / skill / credential that is not in the résumé: the truthful answer is No
            if re.search(r"\b(willing|able|comfortable|available|open|okay|ok|prepared|can you|will you|are you)\b", low):
                return pick_option(opts, "Yes")      # ordinary willingness / ability questions
        if has_opts and kind in ("select", "radio", "combobox") and f.get("required"):
            got = next((o for o in opts if re.search(r"decline|prefer not|choose not|do not wish|don'?t wish|not (to )?(say|answer|disclose|specify)|n/?a\b|not applicable", o, re.I)), None)
            if got:
                return got                            # unknown but demographic-style question: the 'prefer not to say' option

        # 3) open-ended prompt -> free LLM writer (required questions only, unless answer_optional)
        return self._write(f, low, kind)

    _MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

    def _month_year(self, txt: str):
        m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", txt or "")
        if not m or m.group(1).lower() not in self._MONTHS:
            return None
        return f"{self._MONTHS[m.group(1).lower()]:02d}/01/{m.group(2)}"

    def _experience_answers(self, fields) -> dict:
        """Work-history blocks (Workday 'Work Experience 1, 2 ...': Job Title, Company, Location, I currently work here, From,
        To, Role Description) filled from profile.yaml experience, newest first. Returns {field id: value or None}."""
        labs = [(f, _norm(re.sub(r"[*\u2731]", "", f.get("label", "")))) for f in fields]
        if not (any(l == "job title" for _, l in labs) and any(l in ("company", "company name", "employer") for _, l in labs)):
            return {}
        jobs = list(self.profile.get("experience") or [])
        seen: dict = {}
        out = {}
        for f, l in labs:
            key = {"job title": "title", "title": "title", "company": "company", "company name": "company", "employer": "company",
                   "location": "location", "from": "from", "start date": "from", "to": "to", "end date": "to",
                   "role description": "desc", "description": "desc", "i currently work here": "current"}.get(l)
            if not key:
                continue
            n = seen.get(key, 0)
            seen[key] = n + 1
            if n >= len(jobs):
                out[f["id"]] = None
                continue
            j = jobs[n]
            dates = str(j.get("dates", ""))
            parts = re.split(r"\s*[–—-]\s*", dates, maxsplit=1)
            now = bool(re.search(r"present|current|now", dates, re.I))
            if key == "title":
                out[f["id"]] = j.get("title") or None
            elif key == "company":
                out[f["id"]] = j.get("company") or None
            elif key == "location":
                out[f["id"]] = j.get("location") or None
            elif key == "from":
                out[f["id"]] = self._month_year(parts[0])
            elif key == "to":
                out[f["id"]] = None if now else self._month_year(parts[1] if len(parts) > 1 else "")
            elif key == "desc":
                out[f["id"]] = (" ".join((b.get("text", "") if isinstance(b, dict) else str(b)) for b in (j.get("bullets") or []))[:1900]) or None
            elif key == "current":
                out[f["id"]] = True if now else ""
        return out

    def _meets_minimums(self) -> bool:
        """True when the posting being applied to asks for no more than you have: entry/early level (at most 2 required years;
        you have 3+ years of work), a bachelor's at most, no professional license and no security clearance."""
        job = getattr(self, "_job", None)
        if job is None:
            return False
        from . import level
        from .sources import LICENSE_RX
        desc = job.description or ""
        lo, hi = level.pay_range(desc)
        lv = level.classify(job.title, desc, lo, hi)
        if not lv.eligible or lv.level > 1 or level.ADV_DEGREE_REQ.search(desc) or LICENSE_RX.search(job.title):
            return False
        if re.search(r"\b(license[d]? (required|is required)|must (be|hold)[^.]{0,30}licen[sc]e|certified (public|professional)|"
                     r"(cpa|pmp|pe|cdl|rn)\b[^.]{0,20}required|bilingual[^.]{0,30}required|fluen[ct][^.]{0,40}(required|must))", desc, re.I):
            return False
        return True

    # ------------------------------------------------------------------ special cases
    def _special(self, f, low, kind, opts, has_opts):
        """Rules that need more than a fact lookup. Returns _UNSET when none applies, None when the job must be skipped."""
        optionish = has_opts or kind == "checkbox_single"
        if (has_opts or kind in ("text", "textarea")) and re.search(
                r"are you (currently )?(subject to|bound by|a party to|under)\b.{0,80}(agreement|restriction|non-?compete|non-?solicit|covenant|contract)|"
                r"(do|does) (you|your).{0,30}(have|hold).{0,30}(non-?compete|non-?solicit|contractual restriction)", low):
            return pick_option(opts, "No") if has_opts else "No"      # you are not bound by a non-compete or similar agreement
        if kind == "checkbox_group" and opts and re.search(r"(office|location)s?\W.{0,40}(interest|prefer|consider|open to|willing)|which (office|location)", low):
            ok = [str(x).lower() for x in self.facts.get("relocation_ok_locations", []) or []]
            got = [o for o in opts if any(re.search(r"(?<![a-z])" + re.escape(x) + r"(?![a-z])", o.lower()) for x in ok)
                   or re.search(r"remote", o, re.I)]
            if got:
                return got                               # the offices in the places you would live
        if has_opts and opts and re.search(r"relocat", low) and any(re.search(r"willing to relocate", o, re.I) for o in opts):
            if self._location_ok(low):
                got = next((o for o in opts if re.match(r"\W*yes,? i am willing to relocate", o, re.I)), None) or \
                      next((o for o in opts if re.search(r"willing to relocate", o, re.I) and not re.match(r"\W*no\b", o, re.I)), None)
            else:
                got = next((o for o in opts if re.match(r"\W*no\b", o, re.I)), None)
            if got:
                return [got] if kind == "checkbox_group" else got
        if has_opts and opts and re.search(r"\breside|\bresidents?\b|\bresidency\b|live in (one|any) of", low) and \
                any(re.search(r"(do not|don.t|not) (currently )?reside|(currently )?reside in (one|any)|i (do not|don.t) live|i live in", o, re.I) for o in opts):
            home = str(self.facts.get("state") or "Colorado")
            states_txt = low + " " + " ".join(o.lower() for o in opts)
            listed = bool(re.search(r"(?<![a-z])" + re.escape(home.lower()) + r"(?![a-z])", states_txt)) or \
                bool(re.search(r"(?<![a-z])co(?![a-z])", f.get("label", "")))
            neg = re.compile(r"(do not|don.t|not) (currently )?(reside|live)|none of", re.I)
            if listed:
                got = next((o for o in opts if not neg.search(o) and re.search(r"reside|live", o, re.I) and
                            re.search(re.escape(home), o, re.I)), None) or \
                      next((o for o in opts if not neg.search(o) and re.search(r"reside|live", o, re.I)), None)
            else:
                got = next((o for o in opts if neg.search(o)), None)       # you live in Colorado, which is not on their list
            if got:
                return [got] if kind == "checkbox_group" else got
        if has_opts and NEG_Q_RX.search(re.sub(r"(including )?(but )?not limited to|not (just|only) limited to", " ", low)) and NEG_TOPIC_RX.search(low):
            return None                      # negated yes/no question about authorization / location: too easy to answer backwards
        if optionish and re.search(r"investigative consumer report|consumer report|background (check|screening|investigation)|"
                                   r"criminal (background|history) check|employment verification", low) and \
                re.search(r"understand|agree|authori[sz]e|consent|acknowledge", low) and not LEGAL_RX.search(low):
            if kind == "checkbox_single":
                return True
            got = next((o for o in opts if re.search(r"^\W*(yes|i agree|i understand|i consent|i authori|agree)", o, re.I)), None)
            if got:
                return [got] if kind == "checkbox_group" else got   # a background check you have said you are fine with
        if optionish and LEGAL_RX.search(low):
            return None                      # arbitration / waiver / non-compete: never agreed to automatically
        if optionish and AI_POLICY_RX.search(low) and not re.search(r"did you|have you|do you use|will you use", low):
            return None                      # AI-in-the-application policies: the writer is AI, so never confirm them
        if has_opts and QUALIFY_CERT_RX.search(low):
            if self._meets_minimums():
                return pick_option(opts, "Yes")  # entry-level posting, no license / graduate degree / clearance: you meet them
            return None                      # otherwise never certify qualifications for the user
        if kind in ("text", "textarea") and ACK_RX.search(low) and re.search(r"true|accurate|correct|complete|truthful", low) \
                and not (LEGAL_RX.search(low) or AI_WORDS.search(low)):
            return self.facts.get("full_name")   # 'I certify my answers are true' typed-signature box
        if kind in ("text", "textarea", "number") and re.search(r"salary|compensation|pay\b|rate|expect", low) and \
                re.search(r"per month|monthly|a month|/ ?month", low):
            nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]{3,}", str(self.facts.get("salary_expectation", "")))]
            if len(nums) >= 2:
                lo, hi = round(nums[0] / 12 / 100) * 100, round(nums[1] / 12 / 100) * 100
                return f"{lo:.0f}" if kind == "number" else f"${lo:,.0f} to ${hi:,.0f} per month"
        if (has_opts or kind == "text") and re.search(r"(current|existing|active)\W+(\[?[\w&.\]-]+\]?\W+)?(member(ship)?|customer|client|subscriber|"
                                                       r"student|patient)\b|are you (a|an) (member|customer|client|subscriber)", low):
            return pick_option(opts, "No") if has_opts else "No"      # not a member / customer of the employer
        if (has_opts or kind == "text") and re.search(r"(ever|previously)\W+(been\W+)?(interviewed|interview)\b|interviewed (at|with|for)\b", low):
            return pick_option(opts, "No") if has_opts else "No"
        if (has_opts or kind == "text") and re.search(r"(ever|previously)\W+applied|applied (to|for|at|with)\W.{0,60}(before|previously|in the past)", low):
            company = getattr(self._job, "company", "") if self._job else ""
            yes = company in self.applied_before
            return pick_option(opts, "Yes" if yes else "No") if has_opts else ("Yes" if yes else "No")
        if has_opts and re.search(r"camera|webcam|on video|video (interview|call)|video on", low) and re.search(r"\b(on|enabled?|able|can you|will you|confirm|comfortable)\b", low):
            return pick_option(opts, "Yes")          # ordinary interview logistics
        if has_opts and (m := re.search(r"lift\w*\W+(?:up to |at least |over |up to a? ?)?(\d+)\s*(?:lbs?|pounds)", low)):
            cap = float(self.facts.get("can_lift_lbs") or 0)
            yes = pick_option(opts, "Yes")
            return yes if (cap and float(m.group(1)) <= cap and yes) else None
        if has_opts and RESIDE_RX.search(low) and not re.search(r"relocat|willing|open to|hybrid|on-?site", low):
            return self._reside(low, opts, kind)
        if kind == "checkbox_group" and opts and re.search(r"availab|which (days|shifts)|days? (can|are|do) you|shifts? (can|are|do) you|when (can|are) you", low):
            return list(opts)                # open availability: every day / shift offered
        if has_opts and kind in ("select", "radio", "combobox") and re.search(
                r"\brate\b|rating|proficien(cy|t) (level|in|with)|(level|degree) of (proficiency|expertise|experience|knowledge|skill|familiarity)|how (proficient|skilled|experienced|comfortable|familiar)|skill level|experience level|familiarity (with|level)|self.?assess|expertise", low) \
                and not re.search(r"years?|language|spanish|english|fluen|verbal|written|speaking|reading|writing", low):
            got = self._rate(low, opts)
            if got:
                return got
        if has_opts and re.search(r"age (range|group|bracket)|how old|your age\b", low):
            for o in opts:
                m = re.match(r"\D*(\d{2})\s*(?:-|to|–|and)\s*(\d{2})", o)
                if m and int(m.group(1)) <= 22 <= int(m.group(2)):
                    return o
                m = re.match(r"\D*(\d{2})\s*\+", o)
                if m and int(m.group(1)) <= 22:
                    return o
        if has_opts and re.search(r"which (office|location|city|hub|site)\b|(office|location|city) (are you|you are|you.re) (applying|interested)", low) \
                and not re.search(r"prefer|first choice|top", low):
            jl = norm_location(self._job.location if self._job else "")
            got = next((o for o in opts if len(o) > 2 and jl and (o.lower().split(",")[0].strip() in jl)), None)
            return ([got] if kind == "checkbox_group" else got) if got else None
        if has_opts and re.search(r"(top|first|preferred?|preference)\W+(\w+\W+)?(office|location|city)|which (office|location|city)\W.{0,30}(prefer|interest)", low):
            pref = ["denver", "boulder", "colorado", "los angeles", "new york", "remote"]
            for p in pref:
                got = next((o for o in opts if p in o.lower()), None)
                if got:
                    return [got] if kind == "checkbox_group" else got
            return None
        return _UNSET

    _GENERIC_DOMAIN = re.compile(
        r"operations?|project|program|management|supply|procure|logistic|propert|facilit|maintenance|data|analy|excel|sql|python|quickbooks|"
        r"customer|professional|relevant|related|work|this (field|role|position|industry)|the (field|role|industry)|similar|business|process|"
        r"administrative|office|coordinat|vendor|inventory|planning|finance|accounting|bookkeeping|hospitality|construction|real estate", re.I)

    def _names_unlisted_tool(self, text: str) -> bool:
        """True for 'years of experience with Salesforce / SAP / Tableau ...' when that named tool is not in the profile skills."""
        m = re.search(r"\b(?:with|using|in|on)\s+([A-Za-z0-9][A-Za-z0-9+#./&\- ]{1,40})", text)
        if not m:
            return False
        cand = re.split(r"[?,;:()]| and | or ", m.group(1))[0].strip()
        if not cand or self._GENERIC_DOMAIN.search(cand):
            return False
        c = cand.lower()
        if any(c in s or s in c for s in self.skills if len(s) > 2):
            return False
        return bool(re.search(r"[A-Z]", cand)) or len(cand.split()) == 1

    _COURSE_TERMS = ("supply chain", "optimization", "simulation", "probability", "statistic", "regression", "data analysis", "project management",
                     "accounting", "economics", "systems engineering", "requirements", "workflow", "process improvement", "sop", "scheduling", "budget",
                     "microsoft project", "quickbooks", "google workspace", "powerpoint", "word", "sql", "python", "excel")

    def _skill_level(self, low: str) -> int:
        """0 none, 1 beginner, 2 intermediate, 3 advanced: from the skills and coursework in the profile only."""
        best = 0
        for raw, plain in zip(self.skills_raw, self.skills):
            if len(plain) > 1 and re.search(r"(?<![\w+#])" + re.escape(plain) + r"(?![\w+#])", low):
                best = max(best, 3 if "advanced" in raw else 2)
        if best == 0 and any(t in low for t in self._COURSE_TERMS):
            best = 2
        return best

    _RANK = ((0, r"\b(none|no experience|not familiar|never|no knowledge)\b"), (1, r"\b(beginner|basic|novice|entry|limited|fundamental|elementary|some)\b"),
             (2, r"\b(intermediate|moderate|working|competent|proficient|average|good|solid)\b"), (3, r"\b(advanced|strong|high|very good|skilled)\b"),
             (4, r"\b(expert|master|exceptional|highest|guru)\b"))

    def _rate(self, low: str, opts: list[str]):
        lvl = self._skill_level(low)
        ranked = []
        for o in opts:
            r = next((k for k, rx in self._RANK if re.search(rx, o, re.I)), None)
            ranked.append(r)
        if opts and all(r is not None for r in ranked):
            want = [o for o, r in zip(opts, ranked) if r == lvl]
            if want:
                return want[0]
            below = [(r, o) for o, r in zip(opts, ranked) if r < lvl]
            return max(below)[1] if below else opts[0]
        nums = []
        for o in opts:
            m = re.match(r"\s*(\d+)", o)
            nums.append(int(m.group(1)) if m else None)
        if opts and all(n is not None for n in nums):
            order = sorted(range(len(opts)), key=lambda i: nums[i])
            pos = {0: 0.0, 1: 0.25, 2: 0.5, 3: 0.75}[lvl]
            return opts[order[round(pos * (len(opts) - 1))]]
        if len(opts) >= 3:      # unlabeled ordered scale (Entry / Mid / Senior, Somewhat / Very ...): the same position, lowest first
            return opts[round({0: 0.0, 1: 0.25, 2: 0.5, 3: 0.75}[lvl] * (len(opts) - 1))]
        return None

    def _statement_box(self, low):
        """A tick-box that states something about the applicant. True only when the statement is true for them."""
        if re.search(r"authori[sz]ed to work|eligible to work|legally (authorized|permitted|eligible)|right to work|work authori", low):
            if re.search(r"\bnot (authori|eligible|permitted)|unauthori", low):
                return None
            if re.search(r"(will|do|would)\W+(now\W+or\W+in\W+the\W+future\W+)?require\W+(\w+\W+)?sponsor|need\W+(\w+\W+)?sponsor", low) \
                    and not re.search(r"(not|n't|no|without)\W+(\w+\W+){0,3}(require|need|sponsor)", low):
                return None
            return True if re.match(r"y", str(self.facts.get("authorized_to_work_in_us", "")), re.I) else None
        if re.search(r"(do|will|does)\W+not\W+(now\W+or\W+in\W+the\W+future\W+)?require\W+(\w+\W+)?sponsor|without (visa )?sponsorship", low):
            return True if re.match(r"n", str(self.facts.get("requires_sponsorship_now_or_future", "")), re.I) else None
        if re.search(r"\b(at least|over|older than)\W+18\b|18 years", low) and re.match(r"y", str(self.facts.get("over_18", "")), re.I):
            return True
        return _UNSET

    def _years_option(self, low, opts, have: float, kind):
        """A canned 'years of X' number turned into the right choice: Yes/No for 'N+ years?' questions, a range for buckets."""
        yes, no = pick_option(opts, "Yes"), pick_option(opts, "No")
        if yes and no and len(opts) <= 3:
            m = YEARS_Q.search(low)
            if not m:
                return None
            need = float(m.group(1) or m.group(2))
            return yes if have >= need else no
        got = self._bucket(opts, have)
        if got is None:
            got = self._resolve({"kind": kind}, str(int(have) if have == int(have) else have), opts)
        return [got] if (got and kind == "checkbox_group" and not isinstance(got, list)) else got

    @staticmethod
    def _bucket(opts, have: float):
        """The option whose year range contains `have` ('1-3 years', '5+', 'less than 1'); the lower range when two touch."""
        best, best_hi = None, None
        for o in opts:
            t = o.lower().replace("–", "-").replace("—", "-")
            if (m := re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", t)):
                lo, hi = float(m.group(1)), float(m.group(2))
            elif (m := re.search(r"(?:less than|under|fewer than|up to)\s*(\d+(?:\.\d+)?)", t)):
                lo, hi = 0.0, float(m.group(1)) - 0.01
            elif (m := re.search(r"(\d+(?:\.\d+)?)\s*\+|(\d+(?:\.\d+)?)\s*(?:or more|and (?:up|over|above))|(?:more than|over|at least)\s*(\d+(?:\.\d+)?)", t)):
                lo, hi = float(next(g for g in m.groups() if g)), 1e9
            elif re.search(r"\bnone\b|no experience|\b0\b", t):
                lo, hi = 0.0, 0.0
            else:
                continue
            if lo <= have <= hi and (best_hi is None or hi < best_hi):
                best, best_hi = o, hi
        return best

    def _reside(self, low, opts, kind):
        """Answers 'do you live in <place>' from where the person actually lives (New Castle, CO)."""
        yes, no = pick_option(opts, "Yes"), pick_option(opts, "No")
        city = str(self.facts.get("city", "")).lower()
        state = str(self.facts.get("state", "")).lower()
        home = [h for h in HOME_TERMS + ((city,) if city else ())]
        if yes and no:
            if any(h in low for h in home):
                return yes
            if re.search(r"united states|\bu\.?s\.?a?\b|america", low) and not OTHER_CO.search(low):
                return yes
            if state and re.search(rf"\b{re.escape(state)}\b|\bco\b", low) and not OTHER_CO.search(low):
                return yes
            return no
        for term in home + ([state] if state else []):
            got = next((o for o in opts if term in o.lower()), None)
            if got:
                return [got] if kind == "checkbox_group" else got
        got = next((o for o in opts if re.search(r"\bother\b|remote|none of|not listed|outside", o, re.I)), None)
        return ([got] if kind == "checkbox_group" else got) if got else None

    def _semantic(self, key, val, opts, kind, low):
        """Pick the option that MEANS the wanted answer when the options are sentences rather than Yes/No."""
        def out(o):
            return [o] if (o and kind == "checkbox_group") else o
        neg = re.compile(r"\b(not|no|never|cannot|can't|unable|don't|do not|won't|will not|neither|none)\b", re.I)
        want_yes = bool(re.match(r"y", str(val), re.I))
        if key == "authorized_to_work_in_us" and want_yes:
            best, bs = None, -99
            for o in opts:
                if not re.search(r"authori[sz]ed|eligible|permitted|permanent resident|citizen|green card|right to work", o, re.I):
                    continue
                sc = -2 if neg.search(o) else 2
                if re.search(r"specific employer|only for|h-?1b|visa|sponsor|temporary|limited|\bopt\b|\bcpt\b|\btn\b|e-?3", o, re.I):
                    sc -= 3
                if re.search(r"any employer|permanent|unrestricted|without (restriction|sponsorship)|green card|lawful", o, re.I):
                    sc += 2
                if sc > bs:
                    best, bs = o, sc
            return out(best) if bs > 0 else None
        if key == "requires_sponsorship_now_or_future" and not want_yes:
            best = next((o for o in opts if re.search(r"sponsor|visa|immigration", o, re.I) and neg.search(o)), None)
            return out(best)
        if key == "speaks_spanish" and want_yes and re.search(r"spanish|bilingual|language", low):
            order = [r"native", r"fully|full professional|fluent", r"advanced|professional working|proficient", r"conversational|intermediate"]
            for pat in order:
                got = next((o for o in opts if re.search(pat, o, re.I) and not re.search(r"\bnot\b|\bno\b|basic|beginner|limited|elementary", o, re.I)), None)
                if got:
                    return out(got)
            return None
        if key == "earliest_start_date":
            for pat in (r"2 weeks|two weeks|14 days|1-2 weeks|within 2|2-4", r"immediate|asap|right away|as soon|now\b|currently available",
                        r"30 days|1 month|one month|4 weeks|within (1|one|30)"):
                got = next((o for o in opts if re.search(pat, o, re.I)), None)
                if got:
                    return out(got)
            return None
        if key == "how_did_you_hear":
            for pat in (r"^linkedin", r"^indeed", r"glassdoor", r"job board|job site|job posting|jobs? website", r"company (career|web)|careers? (site|page)",
                        r"internet|online|search|website", r"^other"):
                got = next((o for o in opts if re.search(pat, o, re.I)), None)
                if got:
                    return out(got)
            return None
        return None

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
        open_prompt = kind == "textarea" or (kind == "text" and len(f.get("label", "")) > 60 and PROMPT_RX.search(low))
        if not open_prompt:
            return None
        if not f.get("required") and not (self.cfg.get("writer", {}) or {}).get("answer_optional", False):
            return None
        question = f.get("label", "") + ((" " + f["question"]) if f.get("question") else "")
        got = None
        if self.writer and self.writer.ready():
            try:
                got = self.writer.answer(self._job, self._company_name(self._job), question.strip(),
                                         limits_from_question(question, f.get("maxlength")), self._log)
            except WriterUnavailable as e:
                self._log(f"      writer unavailable: {str(e)[:100]}")
        if not got and f.get("required"):
            got = self._fallback_text(f)
        return got

    def _fallback_text(self, f):
        """Plain background-based answer used when the writer fails, so a required essay never skips the job."""
        txt = (self.cfg.get("fallback_answer") or "").strip()
        if not txt:
            return None
        mx = f.get("maxlength")
        if mx and len(txt) > mx:
            cut = txt[:mx]
            end = max(cut.rfind(". "), cut.rfind("."))
            txt = cut[:end + 1] if end > mx * 0.4 else cut
        self._log("      (writer gave nothing: used the standard background answer)")
        return txt

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
