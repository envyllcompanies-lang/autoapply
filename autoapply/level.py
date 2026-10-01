"""How senior a posting really is, from everything it says, not just keywords in the title.

classify(title, description, pay_low, pay_high) -> Level(level, reasons, eligible)
  level 0 = entry (new grad, 0-1 yrs), 1 = early (1-2 yrs), 2 = mid (3-5 yrs), 3 = senior (6+ yrs, manager, lead)
  eligible = False for things Brian can't apply to at any level (part-time, internships, veterans-only programs,
             MBA/PhD programs, security clearance required)

Signals, strongest first:
  1. years of experience the posting REQUIRES (preferred / nice-to-have years are ignored; 'X or Y years' takes the smaller)
  2. managing people (direct reports, 'manage a team of')
  3. title rank words (senior, lead, principal, manager, director, II/III, junior, associate, coordinator, entry level)
  4. pay (a range that starts at $100K+ is almost never entry level)
  5. explicit entry-level wording ('entry level', 'new grad', 'recent graduate', '0-2 years')
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
       "twelve": 12, "fifteen": 15}
_N = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)"


def _n(s: str) -> int:
    s = s.lower()
    return NUM[s] if s in NUM else int(s)


# -------------------------------------------------------------------------------------------------------- title
T_SENIOR = re.compile(r"\b(senior|sr\.?|snr|principal|staff|lead|head|director|vp|vice president|chief|executive director|"
                      r"partner|architect|supervisor|superintendent)\b", re.I)
T_MANAGER = re.compile(r"\bmanager\b", re.I)
T_MANAGER_OK = re.compile(r"\b(associate|assistant|junior|jr\.?|entry[- ]level)\s+([\w&/-]+\s+){0,3}manager\b|"
                          r"\bmanager\s+(trainee|in training)\b|\baccount manager\b", re.I)
T_NUMERAL = re.compile(r"(?:\b|\s)(ii|iii|iv|v|2|3|4|5)\s*(?:$|[-,(/|:])|\blevel\s*(2|3|4|5|ii|iii|iv)\b", re.I)
T_ONE = re.compile(r"(?:\b|\s)(i|1)\s*(?:$|[-,(/|:])|\blevel\s*(1|i)\b", re.I)
T_ENTRY = re.compile(r"\b(junior|jr\.?|entry[- ]level|entry|new grad(uate)?|graduate|early career|trainee|apprentice|"
                     r"assistant|associate|coordinator|rotational|rotation program|analyst program)\b", re.I)
T_MID = re.compile(r"\b(intermediate|experienced|mid[- ]level|specialist iii|consultant|advisor|expert)\b", re.I)

# -------------------------------------------------------------------------------------------------------- eligibility
NOT_ELIGIBLE = [
    (re.compile(r"\bpart[- ]time\b(?!\s*(or|/)\s*full)", re.I), "part-time"),
    (re.compile(r"\b(intern|internship|co-?op|summer (associate|analyst|program))\b", re.I), "internship / summer program"),
    (re.compile(r"\bveterans?\b.{0,40}\b(program|transition|only|fellowship)|\b(transition(ing)?|separating) (service ?members?|military)|"
                r"\bskillbridge\b|\bmilitary spouse", re.I), "veterans / military program"),
    (re.compile(r"\b(mba|phd|ph\.d|doctoral|jd|law student)\b.{0,30}\b(program|track|candidate|student|required|intern)|"
                r"\b(pursuing|enrolled in) an? (mba|master|phd)", re.I), "MBA / PhD / grad-school program"),
    (re.compile(r"\b(active|current|existing|must (have|hold|possess))\b[^.]{0,40}\b(secret|ts/sci|top secret|security clearance|clearance)\b|"
                r"\b(ts/sci|top secret)\b[^.]{0,30}\b(required|clearance)", re.I), "needs a security clearance"),
]
TITLE_NOT_ELIGIBLE = [
    (re.compile(r"\bpart[- ]time\b", re.I), "part-time"),
    (re.compile(r"\b(seasonal|temporary|temp)\b|\bper diem\b", re.I), "seasonal / temporary"),
    (re.compile(r"\b(intern|internship|co-?op|summer associate|summer analyst)\b", re.I), "internship / summer program"),
    (re.compile(r"\bveterans?\b|\bmilitary\b|skillbridge", re.I), "veterans / military program"),
    (re.compile(r"\b(mba|phd|ph\.d|doctoral)\b", re.I), "MBA / PhD program"),
]

# -------------------------------------------------------------------------------------------------------- description
PREFERRED = re.compile(r"prefer|nice to have|nice-to-have|\bbonus\b|\bplus\b|ideal(ly)?|desired|a plus|advantage|beneficial|"
                       r"would be great|not required|optional", re.I)
REQUIRED_HDR = re.compile(r"(requirements|required|qualifications|what you.ll need|what we.re looking for|you have|must have|"
                          r"minimum|basic qualifications|who you are|about you)", re.I)
PREF_HDR = re.compile(r"(preferred|nice to have|bonus|pluses|extra credit|desired qualifications)", re.I)
YEARS = re.compile(
    rf"(?:(?:at least|minimum(?: of)?|min\.?|over|more than|a minimum of)\s+)?"
    rf"{_N}\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*{_N}\s*\+?\s*)?(?:\(\s*\d+\s*\)\s*)?\+?\s*(?:years?|yrs?)\b", re.I)
EXP_CONTEXT = re.compile(r"experience|work(ing)?|professional|industry|background|role|position|track record|"
                         r"relevant|related|progressive|hands-on|in (a|an) ", re.I)
NOT_EXP_CONTEXT = re.compile(r"years? old|age|anniversary|founded|over the (past|last|next)|in the (past|last|next)|"
                             r"within \w+ years|years? (ago|from now|of age)|per year|a year\b|each year|every year|"
                             r"year[- ]round|multi-year|\d+ years? (in business|of history)|for (over|more than) \d+ years|"
                             r"we('ve| have) been|company|history|grown|growth|retention|warranty|degree (program|in \w+ years)", re.I)
MANAGES = re.compile(r"direct reports|people manage|manage (a|the)? ?team(s)? of|managing (a|the)? ?team|lead (a|the)? ?team of|"
                     r"leading a team of|supervis(e|ing|ion of) (a )?(team|staff|\d+)|hire,? (train|develop|and)|"
                     r"responsible for (hiring|managing (a|the)? ?team)|build and (lead|manage) (a|the)? ?team|"
                     r"performance (reviews|management) (of|for) (your )?(team|direct)", re.I)
ENTRY_WORDS = re.compile(r"entry[- ]level|new grad|recent (college )?grad|recent graduate|early[- ]career|no (prior )?experience (is )?(required|necessary)|"
                         r"0\s*(-|–|to)\s*[12]\s*(\+\s*)?years|less than (1|one|2|two) years?|up to (1|one|2|two) years|"
                         r"(1|one)\s*(-|–|to)\s*(2|two)\s*years|class of 20(25|26)|graduat(ed|ing) (in|by) 20(25|26)|"
                         r"launch your career|start your career|first (job|role) out of", re.I)
ADV_DEGREE_REQ = re.compile(r"(mba|master'?s|ph\.?d|juris doctor|\bjd\b)[^.]{0,40}\brequired\b|requires? (an? )?(mba|master'?s|ph\.?d)", re.I)


@dataclass
class Level:
    level: int
    eligible: bool = True
    reasons: list = field(default_factory=list)
    years: int | None = None

    @property
    def name(self) -> str:
        return ["entry", "early", "mid", "senior"][max(0, min(3, self.level))]

    def why(self) -> str:
        return f"{self.name}" + (f" ({'; '.join(self.reasons)})" if self.reasons else "")


def _sentences(desc: str) -> list[tuple[str, bool]]:
    """(sentence, in a 'preferred' section) pairs. Bullets and line breaks count as sentence ends."""
    out, pref = [], False
    for line in re.split(r"[\n\r]+|(?<=[.;!?])\s+(?=[A-Z•\-*])|•|·|•", desc or ""):
        s = line.strip(" \t-*•:")
        if not s:
            continue
        header = len(s) < 60 and not re.search(r"\d|\byears?\b", s, re.I)
        if header and PREF_HDR.search(s):
            pref = True
            continue
        if header and REQUIRED_HDR.search(s) and not PREF_HDR.search(s):
            pref = False
            continue
        out.append((s, pref))
    return out


def required_years(desc: str) -> int | None:
    """The years of experience the posting insists on: the largest 'required' requirement, where each requirement that offers
    alternatives ('2 years, or 4 years without a degree') counts at its smallest. None when it names no number."""
    reqs = []
    for s, in_pref in _sentences(desc):
        if in_pref or PREFERRED.search(s):
            continue
        low = s.lower()
        found = []
        for m in YEARS.finditer(low):
            a = _n(m.group(1))
            if a > 20:
                continue
            around = low[max(0, m.start() - 60): m.end() + 80]
            if NOT_EXP_CONTEXT.search(low[max(0, m.start() - 25): m.end() + 25]) or not EXP_CONTEXT.search(around):
                continue
            found.append(a)
        if found:
            reqs.append(min(found))
    return max(reqs) if reqs else None


def classify(title: str, desc: str = "", pay_low: float | None = None, pay_high: float | None = None) -> Level:
    t = re.sub(r"\s*(?:/|\bor\b)\s*(?:senior|sr\.?)\s+[\w&-]+", "", title or "", flags=re.I)      # 'Associate / Senior Associate'
    lv = Level(level=1)
    reasons = lv.reasons

    for rx, why in TITLE_NOT_ELIGIBLE:
        if rx.search(t):
            lv.eligible = False
            reasons.append(why)
    body = (desc or "")[:12000]
    head = body[:2500]
    for rx, why in NOT_ELIGIBLE:
        if (rx.search(body) if "clearance" in why else rx.search(head)) and why not in reasons:
            lv.eligible = False
            reasons.append(why)

    # title rank
    title_lvl = None
    if T_SENIOR.search(t):
        title_lvl = 3
        reasons.append(f"title says '{T_SENIOR.search(t).group(0)}'")
    elif T_MANAGER.search(t) and not T_MANAGER_OK.search(t):
        title_lvl = 3
        reasons.append("manager title")
    elif (m := T_NUMERAL.search(t)):
        title_lvl = 2
        reasons.append(f"level {m.group(1) or m.group(2)} title")
    elif T_MID.search(t):
        title_lvl = 2
        reasons.append(f"title says '{T_MID.search(t).group(0)}'")
    elif T_ENTRY.search(t) or T_ONE.search(t):
        title_lvl = 0

    yrs = required_years(body) if body else None
    lv.years = yrs
    manages = bool(MANAGES.search(body)) if body else False
    entry_words = bool(ENTRY_WORDS.search(body)) if body else False
    adv_degree = bool(ADV_DEGREE_REQ.search(body)) if body else False

    # combine: years decide when the posting states them; otherwise the title
    if yrs is not None:
        y_lvl = 0 if yrs <= 1 else 1 if yrs <= 2 else 2 if yrs <= 5 else 3
        reasons.append(f"requires {yrs}+ yrs")
        level = y_lvl
        if title_lvl == 3 and y_lvl <= 1:
            level = 2                     # 'Senior Analyst, 2+ years' is still not entry level
        elif title_lvl is not None and title_lvl > level and title_lvl >= 2 and y_lvl >= 1:
            level = title_lvl
    else:
        level = title_lvl if title_lvl is not None else 1
    if manages:
        level = max(level, 3)
        reasons.append("manages people")
    if adv_degree:
        level = max(level, 2)
        reasons.append("requires a graduate degree")
    lo = pay_low or 0
    if lo >= 130000:
        level = max(level, 3)
        reasons.append(f"pay starts at ${lo:,.0f}")
    elif lo >= 100000:
        level = max(level, 2)
        reasons.append(f"pay starts at ${lo:,.0f}")
    if entry_words and not manages and (yrs is None or yrs <= 2) and title_lvl != 3:
        level = min(level, 0)
        reasons.append("says entry level / new grad")
    lv.level = level
    return lv


PAY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?([kK])?\s*(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?([kK])?", re.I)


def pay_range(desc: str) -> tuple[float | None, float | None]:
    """Yearly pay range named in the posting: '$70,000 - $85,000', '$70k-$85k', '$1,500-$1,800 per month' (x12) or
    '$22 - $25 / hour' (x2080). (None, None) when there is none."""
    for m in PAY.finditer(desc or ""):
        lo = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        hi = float(m.group(3).replace(",", "")) * (1000 if m.group(4) else 1)
        if not (0 < lo <= hi):
            continue
        after = (desc or "")[m.end(): m.end() + 30].lower()
        if re.search(r"bonus|stipend|relocation|signing|sign-on|equity|reimburs|allowance|credit", after):
            continue
        if re.search(r"^\W*(usd|us dollars|dollars)?\W*(/|per|an|a|each)\s*(hour|hr\b)|^\W*(usd)?\W*hourly", after):
            lo, hi = lo * 2080, hi * 2080
        elif re.search(r"^\W*(usd|us dollars|dollars)?\W*(/|per|a|each)\s*(month|mo\b)|^\W*(usd)?\W*monthly", after):
            lo, hi = lo * 12, hi * 12
        if 15000 <= lo <= hi <= 900000:
            return lo, hi
    return None, None
