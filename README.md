# autoapply

A free, hands-off job applier. It runs on GitHub Actions in your private repo, so your Mac can be closed. Four times a day it
finds new entry-level operations / coordination / analyst / supply-chain / project jobs, opens each one's own application
form, fills it in from your résumé and answers, and submits. Cost: $0.

## Day to day: what you do
| When | What |
|---|---|
| Nothing to do most days | It runs by itself (about 7am, 11am, 3pm, 7pm Denver time in summer). Don't cancel runs: a cancelled run still uses free minutes. |
| After a new zip / any change | Unzip over this folder, then `bash update_github.sh`. It pushes the code, refreshes your secrets and never touches the bot's memory of what it already applied to. Add `--run` to start a run right away. |
| Jobs the bot can't finish | They arrive in the email as **FINISH BY HAND**, best fit first, with a link. |

## How you know it's working
- **Emails to delgado@alumni.usc.edu** from the run: one whenever it applied, has something to finish by hand or could not
  confirm a submit, and one check-in a day even if nothing happened (so silence never means "broken"). Each email lists what
  was applied to, what is left for you, why other postings didn't go through, and how many free minutes are used.
- **The employers' own "we received your application" emails** land in the same inbox. The bot reads that inbox to confirm submits.
- **Actions tab** on GitHub: open a run and read the log. It starts with `build 2026-09-29-i`; each success is a `✓ applied` line.
  The run's **Artifacts** hold the résumé, cover letter and form screenshots for every application (kept 30 days).
- `reports/<date>.md` and `logs/<date>.log` in the repo keep the history.

## Where the jobs come from
1. **Company career sites** in `boards.yaml` (public list, no secrets): Greenhouse, Workday (the big employers, searched by role and city),
   Workable, Lever, Breezy, BambooHR. To add a company, open its careers page, click a job, and copy the pattern shown at the top of
   `boards.yaml`. A wrong entry is harmless; the bot stops asking after two misses.
2. **Job boards** (`aggregators` in `config.yaml`): The Muse, RemoteOK, Remotive, Jicy, Himalayas, We Work Remotely, Workable's job board and
   LinkedIn's public search pages (read without logging in, so your account is never touched). Each listing is followed to the employer's
   own form, and any company board found that way is remembered for later runs. Optional, free and worth adding: **Adzuna**
   (developer.adzuna.com, secrets `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`) and **Jooble** (jooble.org/api/about, secret `JOOBLE_API_KEY`).

Every posting is filtered (titles, seniority, years asked, location, pay, security clearance and citizenship-only wording) and scored
0–100. Postings at or above `min_score` are applied to, best first.

## How it applies
- Multi-page forms (Next / Save and Continue) and one-page forms, including account gates: it creates an account with
  **delgado@alumni.usc.edu** and the `ACCOUNT_PASSWORD` secret, confirms it from the inbox, and signs in on later visits.
- Uploads **your own résumé file** (`briandelgado_resume.pdf`, set by `resume_file` in `config.yaml`) unchanged on every application. No résumé is generated.
- **Cover letters:** when a form has a cover-letter upload or box, the bot writes a full one-page letter (about 320 to 380 words, four paragraphs, letterhead, date, signed with your name) from your real background only. If the cover letter is optional and a full one can't be written, it applies with the résumé alone; if it is required, the job goes on your hand list. Thin template letters are never sent.
- Answers every standard question from your `facts` (work authorization as a permanent resident, sponsorship, EEO, dates, education,
  "N years of X", travel, relocation for the cities you approved). Anything it can't answer truthfully means the job is skipped,
  never guessed. Each employer gets at most `max_per_company` (3) applications.
- Submits, then checks the confirmation page or the inbox. Everything is logged in `applications.db`, so nothing is applied to twice.

## What it will not do (on purpose), and what lands on your hand list
- **CAPTCHAs and "are you human" checks are never solved or worked around.** Those jobs go on the FINISH BY HAND list.
- **Sites that email a security code before accepting an application** (some Greenhouse forms) are also left for you. The bot does not read
  or enter those codes, because they are that site's human check.
- If a site keeps stopping the bot this way (three in a row from at least two employers), the bot **pauses that site for a day**
  instead of spending your free minutes on it, lists that site's good fits for you, and lets one application through the next day
  to see whether the check went away. The same goes for one employer whose form has already stopped it this week.
- **Ashby** forms flag automated submissions as spam, so they are not submitted (`skip_sources`). Add Ashby companies to `boards.yaml`
  and set `manual_sources: [ashby]` to have good fits listed for you to do by hand.
- Legal waivers, "I did not use AI" certifications and qualification statements it can't back up. It also skips postings that say they
  don't want AI-assisted applications, and answers truthfully if a form asks whether AI was used.
- It never logs into your personal LinkedIn.

## The free minutes budget
Private repos get 2,000 Actions minutes a month. The bot plans on 1,850 (`actions_minutes_budget`) and spreads them over the month:
each run gets a time allowance (early in the month roughly 12 minutes, more when earlier runs were quiet), always at least
`min_apply_minutes` to apply after finding jobs, and never more than `max_run_minutes`. One stuck site can't take more than
`max_minutes_per_job`. Runs that are cancelled or killed are still counted by the next run (`logs/usage_minutes.json`).
Realistic output on the free tier: roughly 10 to 25 applications a day; more only comes with more minutes.
If you change how often it runs, edit the `cron:` line in `.github/workflows/autoapply.yml` **and** `runs_per_day` in `config.yaml`.

## Secrets (GitHub repo → Settings → Secrets and variables → Actions)
`update_github.sh` sets the file-based ones from this folder; the rest come from environment variables you export before running it.
| Secret | What | Needed |
|---|---|---|
| `CONFIG_YAML`, `PROFILE_YAML`, `ABOUT_ME_MD`, `VOICE_MD` | your `config.yaml`, `profile.yaml`, `about_me.md`, `voice.md` | yes (voice optional). **After editing config.yaml, run the update script** or the bot keeps using the old copy. |
| `GROQ_API_KEY`, `GEMINI_API_KEY` | free writer keys (console.groq.com, aistudio.google.com) | yes |
| `ACCOUNT_PASSWORD` | the one password used for every account the bot creates | for sites that need an account |
| `IMAP_USER`, `IMAP_PASS` | the Gmail login (app password) that receives alumni-address mail; used to confirm new accounts, confirm submits, and send the summary emails | for accounts and emails |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `JOOBLE_API_KEY` | free job-board keys | optional |

## Tuning (`config.yaml`)
- More applications: lower `min_score`, add boards to `boards.yaml`, add title keywords; `daily_cap` / `per_run_cap` are ceilings.
- Better matches: raise `min_score`, add `negative_keywords`, lower `max_years_hard`.
- Many `skipped`? The report lists each question it couldn't answer truthfully. Add a `facts` value, an `answers:` regex or a story in `profile.yaml`.
- Pay: target $75K; postings whose top pay is under `salary_floor` ($65K) lose 40 points. Locations: Denver / Colorado, Los Angeles area, NYC and
  remote score higher and are approved for relocation answers; add cities to `relocation_ok_locations`.
- `search.workday_places` and `search.workday_queries` control how the big Workday employers are searched ("<role> <city>").

## The free writer (essays and cover letters)
Providers are tried best-first and fall through when one is out of quota: Gemini Pro → Gemini Flash → Gemini Flash-Lite → Groq
`gpt-oss-120b` → Groq `gpt-oss-20b` → Ollama on your own machine. Free tiers change; if a model name stops working, edit `model` in `config.yaml`.
Google may use free-tier prompts for training, so only résumé-style facts are sent (never your address or phone).
- **Truth only.** Every number in an answer must exist in your facts, or the answer is revised and then dropped. A question that needs a story
  you haven't given (a failure, a conflict, leading people) skips the job rather than inventing one. It may not name software that isn't in your facts.
- **Your voice.** `voice.md` sets the style (answer first, 75 to 200 words, no buzzwords, no em dashes); `about_me.md` and `profile.yaml`
  hold the true stories and which experience to use for which kind of question.
- It doesn't try to fool AI detectors (no fake typos or tangents) and never hides AI use.

To improve `about_me.md` and `voice.md`, paste this into ChatGPT and read the result carefully (the writer treats it as true):
> Based on everything you know about me and our past conversations, write two things. (1) ABOUT ME: a plain-text background file, about 900 words, of true things that help write job applications: education, jobs and projects with concrete numbers, tools I use, how I like to work, my values, and real stories where I solved a problem, failed, resolved a conflict or led people, plus what I'm looking for next. Only include things I've actually told you; mark anything uncertain with (unsure). Leave out health, immigration, family and dating details. (2) MY VOICE: analyze how I write in my messages and describe my style in about 150 words: sentence length, tone, words I lean on or avoid, quirks. Don't flatter me.

## Statuses
`applied` confirmed · `unconfirmed` submitted but no confirmation seen (check the inbox, not retried) · `manual` / `blocked` finish by hand ·
`skipped` no truthful answer, or the posting bans AI, or the per-employer cap · `failed` retried once in a later run ·
`queued` waiting for a later run · `low_score` / `filtered` not a fit · `dry_run` filled but not sent

## Run it on your own machine instead (optional)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python -m playwright install chromium
export GROQ_API_KEY=... GEMINI_API_KEY=... ACCOUNT_PASSWORD=... IMAP_USER=... IMAP_PASS=...
python -m autoapply --dry-run --limit 3    # fills 3 real forms and submits nothing; look at applications/<today>/*/form.png
python -m autoapply --loop                 # forever, checking every search.loop_minutes
```
The free-minutes limits don't apply there, so raise `max_run_minutes` if you like.

## Tests (no network, no keys)
`python tests/unit_checks.py` runs everything: question answering (83 real-world questions), fit scoring, every job-board feed with mocked
responses, the minutes budget, emails, inbox matching, the history-saving workflow and the update script. `python tests/run_mock.py`
drives the real pipeline (live) through local mock forms and a mock writer: multi-page wizard with account creation, emailed-confirmation
handling, CAPTCHA and security-code stops, per-employer cap and more. Add `--dry` for a fill-only run.
