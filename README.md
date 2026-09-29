# autoapply (free edition, with a free LLM writer)

## 30-minute setup with no computer left running (GitHub Actions)
1. Get two free keys: Groq (console.groq.com) and Gemini (aistudio.google.com, "Get API key").
2. On your Mac, once: `brew install gh && gh auth login`.
3. In this folder: `export GROQ_API_KEY=... GEMINI_API_KEY=... && bash setup_github.sh`
   It creates a **private** repo, uploads the code, stores your config/profile/keys as secrets, and starts a dry run.
4. `gh run watch`, then download the run's artifact and check the filled-form screenshots. If they look right: `gh workflow run autoapply.yml`. After that it runs itself every 2 hours, day and night, with your Mac closed.
Free budget: private repos get 2,000 Actions minutes a month, which is roughly 15 to 25 applications a day. That is the real ceiling of the $0 setup (daily_cap 30 is an upper bound, not a promise).


Hands-off job applier that costs **$0**. Every weekday morning it:

1. **Discovers** every open role at your target companies through the public Greenhouse, Lever and Ashby job APIs.
2. **Scores** each new posting 0–100 with your keyword rules (title, skills, location, red flags, years demanded).
3. For each posting at or above `min_score` (best first, up to `daily_cap`) it opens the application, checks for CAPTCHAs, logins and "no AI" notices, then:
   - assembles a one-page résumé from `profile.yaml` (best-matching bullets, matching skills first) and renders it to PDF,
   - writes a cover letter and any **essay/prompt questions** with a **free LLM**, using only your résumé, `about_me.md` and true stories,
   - answers every standard question from your `facts` (work authorization, sponsorship, EEO, dates, education, and so on),
   - fills the form, submits, and verifies the confirmation.
4. Logs everything to SQLite so it **never applies twice**, and writes `reports/YYYY-MM-DD.md`.

## The free writer
It talks to any OpenAI-compatible endpoint and tries providers best-first, falling through when one is out of quota (`rpd` = requests per day you allow it) or dead (bad key, repeated empty replies):

| Order | Provider | Cost | Notes (free tiers and model names change; check your AI Studio / Groq model list) |
|---|---|---|---|
| 1 | **Gemini Pro** (`gemini-2.5-pro`) | free tier | Strongest writer available free, but tightly capped (~45 requests/day used here). **Google may use free-tier prompts for training and human review**, so it sees your résumé text. Remove it if that bothers you. Key: aistudio.google.com |
| 2 | **Gemini Flash** | free tier | Same privacy caveat, far higher daily limit. |
| 3 | **Groq** `gpt-oss-120b`, then `gpt-oss-20b` | free, no card | Fast, small token budget per minute. Key: console.groq.com |
| 4 | **Ollama (local)** | free, private | `ollama pull qwen2.5:7b`; only when running on your own machine. |

Nothing free writes as well as a top paid model (Claude via API would). A paid provider can be added as one more entry in `writer.providers` if you ever want that; the default stays $0. Free keys needed: `GEMINI_API_KEY` and `GROQ_API_KEY`.

A ChatGPT subscription doesn't include API access, and driving the ChatGPT website with a bot violates its terms, so it can't be the engine. The way to use everything ChatGPT knows about you is to have it write your `about_me.md` and voice notes (below).

### Feed it what ChatGPT knows about you
Paste this into ChatGPT, then save part 1 as `about_me.md` and part 2 as `voice.md`. **Read both and delete anything wrong, because the writer treats them as true.**

> Based on everything you know about me and our past conversations, write two things. (1) ABOUT ME: a plain-text background file, about 900 words, of true things that help write job applications: education, jobs and projects with concrete numbers, tools I use, how I like to work, my values, and real stories where I solved a problem, failed, resolved a conflict or led people, plus what I'm looking for next. Only include things I've actually told you; mark anything uncertain with (unsure). Leave out health, immigration, family and dating details. (2) MY VOICE: analyze how I write in my messages and describe my style in about 150 words: sentence length, tone, words I lean on or avoid, quirks. Don't flatter me.

### Writing rules it enforces
- **Truth only.** Every number in an answer must exist in your facts, or the answer is revised and then discarded. If a question needs a story you haven't provided (a failure, a conflict, leading people), the model replies CANNOT_ANSWER and the job is **skipped**, not invented. Add stories to `profile.yaml` to unlock those.
- **Your master profile is built in.** `about_me.md` and `profile.yaml` carry your ChatGPT master profile: which experience to use for which kind of question (AbilityFirst for systems, Roaring Fork for operations, Hernandez for construction and finance, Otto's for sourcing), your story bank, the "what I'm NOT" and experience-gap rules, and your preferred answer patterns. The writer also refuses to volunteer GPA, Spanish or personal background unless a question asks, calls Otto's a project rather than a business, and may not name software that isn't in your facts (Rippling, Zapier, Airtable and so on are rejected).
- **Voice.** `voice.md` follows your style rules (start with the answer, 75 to 200 words, no buzzwords, no em dashes). If it's missing, `voice_default.md` is used (varied sentence rhythm, contractions, concrete details, no stock phrases or em dashes). Put your own in `voice.md` to override it.
- **Length limits** from the form (maxlength, "250 words") are respected.
- **No demographic or immigration details** ever appear in essays.

### About "undetectable by AI detectors"
I didn't build detector-evasion (adding fake typos, tangents or slang), for two reasons. Detectors are unreliable and no tool can guarantee a pass, and deliberate mistakes make an application look worse to a human reader. What helps is writing that's specific and sounds like you, which is what the voice guide and your real stories do. **It also never hides AI use:** if a form asks whether you used AI it answers truthfully (`ai_use_disclosure` in config), it never ticks an "I did not use AI" box, and it **skips any posting that says it doesn't want AI-assisted applications** (`respect_ai_policies`).

## Pay, travel and locations
- **Salary:** target $75K (posted range $70–80K goes in forms as "$70,000 to $80,000"; number fields get 75000). Postings whose top pay is under `salary_floor` ($65K) lose 40 points, so slightly-below roles still apply but clearly low ones don't. Pay near or above target gets a small bonus. Postings without pay are unaffected.
- **Travel:** dropdowns pick the most open option up to `travel_max_percent` (50). Free text uses `willing_to_travel`.
- **Locations:** Denver/Colorado, Los Angeles area and NYC are scored and approved for relocation answers. For "others", add cities to `relocation_ok_locations` or put `"*"` there to say yes anywhere.

## Location-aware answers
Relocation and in-office questions are answered only for remote jobs or cities in `relocation_ok_locations` (New York, Colorado and so on). For any other city the question is left unanswered, so a job that requires it is skipped instead of you promising to move anywhere.

## Your details
`config.yaml` is prefilled from your résumé and what you told me: permanent-resident answers (authorized: yes, sponsorship: no, "U.S. person": yes, "U.S. citizen": no, clearance: no), your EEO answers, education, and the rest. Search the file for **FILL IN** (blank on purpose; jobs that require them are skipped until you fill them) and **ASSUMED** (my guesses; confirm them). Hispanic/Latino is answered Yes and race "Hispanic or Latino" (from your master profile), used only where a form offers those options; edit or blank them to decline. GPA (3.07) is only ever given when a form asks for it.

## Setup (about 15 min)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export GROQ_API_KEY=...            # free key from console.groq.com (optional: GEMINI_API_KEY)
python -m autoapply --dry-run --limit 3   # fills 3 real forms, submits nothing
```
Check `applications/<today>/*/form.png`, the PDFs and the essay text in the report. When they look right, run `python -m autoapply`.

## Run it 24/7 for free (best: `--loop`)
```bash
python -m autoapply --loop      # checks every search.loop_minutes (20) for new postings, forever
```
Each cycle re-reads every company board, scores only postings it hasn't seen (so new listings are picked up within ~20 minutes), and applies to at most `per_run_cap` (8) per cycle, up to `daily_cap` (60) a day. A crash or network error never stops the loop. The free LLM quotas (`rpd`) are tracked per day in `logs/usage.json`, so they hold across cycles and restarts.
Where to run it for $0:
- **Your Mac:** `caffeinate -i python -m autoapply --loop` (keep it plugged in and awake), or a launchd job that starts it at login.
- **A free always-on VM** (for example Oracle Cloud Always Free): same command under `systemd` or `tmux`. Best if you want it running while your laptop is closed.
- **GitHub Actions** is the fallback: the included workflow now runs hourly, but a private repo only gets 2,000 free minutes a month, so that is roughly hourly checks, not continuous.
More postings depends mostly on **more companies** in `config.yaml` (aim for 100+). Raising `daily_cap` further just makes you look like a bot to ATS spam filters; quality of match matters more.

## Run it on a schedule for free (GitHub Actions)
1. Push this folder to a **private** repo (private repos get 2,000 free Actions minutes a month). Personal files are gitignored.
2. Repo → Settings → Secrets and variables → Actions → add: `CONFIG_YAML`, `PROFILE_YAML`, `ABOUT_ME_MD` (paste each file's contents), `GROQ_API_KEY` (and optionally `GEMINI_API_KEY`, `VOICE_MD`).
3. Actions tab → **autoapply** → *Run workflow* with **dry_run** checked once. After that it runs itself on weekdays around 8am Mountain.

Essay-heavy applications take longer because the writer paces itself to stay under the free-tier rate limits. That's still well inside the free Actions minutes at ~20 applications a day.

## Guardrails
- Refuses to run while any `<placeholder>` remains in your profile, answers or `about_me.md`.
- **Skip, don't guess:** any required question it can't answer truthfully → `skipped`, listed in the report.
- CAPTCHA, login or Cloudflare challenge → `blocked`. It never tries to solve them.
- `daily_cap` submissions/day, random pause between them. Audit trail per application: résumé, cover letter, `form.png` (filled form just before submit), `confirmation.png`.

## Tuning
- Too few applications? Lower `min_score`, add companies (50–100 is a good range), add title keywords.
- Bad matches? Raise `min_score`, add `negative_keywords`, lower `max_years_experience`.
- Many `skipped`? The report lists each unanswerable question. Add a `facts` value, an `answers:` regex, or a story.
- Company tokens come from careers URLs: `job-boards.greenhouse.io/`**`figma`**, `jobs.lever.co/`**`palantir`**, `jobs.ashbyhq.com/`**`ramp`**.

## Statuses
`applied` confirmed · `blocked` CAPTCHA/login, do by hand · `skipped` required question with no truthful answer, or the posting bans AI · `failed` retried next run (max 2) · `low_score` / `filtered` not a fit · `dry_run` filled but not sent

## Not supported (on purpose)
LinkedIn Easy Apply, Workday, iCIMS and Taleo: they need accounts, block bots, and prohibit automation, so using them risks your account.

## Test
`python tests/run_mock.py` runs the real pipeline against local mock forms and a mock LLM server: EEO and permanent-resident answers, essay writing, fabricated-number rejection, provider fallback, the AI-policy skip, CAPTCHA, unanswerable questions and the placeholder guard. No network or keys needed.
