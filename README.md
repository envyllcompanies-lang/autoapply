# autoapply

A free, hands-off job applier. It runs on GitHub Actions in your private repo, so your Mac can be closed. Four times a day it
finds new entry-level operations / coordination / analyst / supply-chain / project jobs, checks each one against your résumé,
opens the good matches' own application forms, fills them in from your résumé and answers, and submits. Cost: $0.

## Turning it on and off
The schedule lives in `.github/workflows/autoapply.yml`. On GitHub: **Actions → autoapply → "…" menu → Disable workflow / Enable
workflow**. While it is disabled nothing runs and no free minutes are used. "Run workflow" on the same page starts one run by
hand (tick "dry run" to fill forms without submitting anything).

## Day to day: what you do
| When | What |
|---|---|
| Nothing to do most days | While enabled it runs by itself, four times a day. Don't cancel runs: a cancelled run still uses free minutes. |
| Changing your private answers | Edit `config.yaml` / `profile.yaml` in your own folder, then `bash update_github.sh`: it refreshes your secrets and never touches the bot's memory of what it already applied to. It refuses to put an older copy of the code over a newer one on GitHub. |
| Jobs the bot can't finish | They arrive in the email under **EXCEPTIONS**, best fit first, with a link, the reason, and how far the bot got. **NEEDS YOU** lists employers that emailed asking for something more (an assessment, an "incomplete application"). |

## How you know it's working
- **Emails to delgado@alumni.usc.edu** from the run: one whenever it applied, has something to finish by hand or could not
  confirm a submit, and one check-in a day even if nothing happened (so silence never means "broken"). Each email lists what
  was applied to, what is left for you, why other postings didn't go through, and how many free minutes are used.
- **The employers' own "we received your application" emails** land in the same inbox. The bot reads that inbox to confirm submits.
- **Actions tab** on GitHub: open a run and read the log. It starts with the build name (`build 2026-10-02-a`); each success
  is a `✓ applied` line with how long it took. The run's **Artifacts** hold the résumé, cover letter and form screenshots for
  every application (kept 30 days).
- `reports/<date>.md` and `logs/<date>.log` in the repo keep the history.
- `logs/snapshots.log`: for every application that did not go through, a short text picture of the page it stopped on
  (headings, questions, which boxes were empty or marked wrong, the site's error messages). Nothing you typed is in it, and
  your name, email, phone and address are blanked out. It is what makes a failure fixable without guessing.
- A run that cannot start (a mistake in the bot's own code) stops before touching any job and emails you what is wrong.

## Where the jobs come from
1. **Company career sites** in `boards.yaml` (public list, no secrets): Greenhouse, Workday (the big employers, searched by role and city),
   Workable, Lever, Breezy, BambooHR. To add a company, open its careers page, click a job, and copy the pattern shown at the top of
   `boards.yaml`. A wrong entry is harmless; the bot stops asking after two misses.
2. **Job boards** (`aggregators` in `config.yaml`): The Muse, RemoteOK, Remotive, Jicy, Himalayas, We Work Remotely, Workable's job board and
   LinkedIn's public search pages (read without logging in, so your account is never touched). Each listing is followed to the employer's
   own form, and any company board found that way is remembered for later runs. Optional, free and worth adding: **Adzuna**
   (developer.adzuna.com, secrets `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`) and **Jooble** (jooble.org/api/about, secret `JOOBLE_API_KEY`).

Every posting is filtered (titles, seniority, licences, years asked, location, pay, security clearance and citizenship-only wording) and
given a keyword score of 0–100; postings at or above `min_score` are queued.

**The match check.** Right before a queued job would be applied to, the bot reads the full posting from the employer's own
feed (which also says in a fraction of a second whether the job is still open) and has the free writer compare it with your
résumé, the way LinkedIn's match score does. Only jobs at **60% or more** (`search.min_fit` in `settings.yaml`) are applied
to; the rest are set aside with the reason. Each job is checked once and the result is remembered. When the free writer has
no allowance left, only jobs with a keyword score of 80+ go ahead and the rest wait for a later run. The email lists the best
matches it checked.

Location rules: remote and US-wide roles anywhere in the US;
on-site and hybrid roles only in the places in `relocation_ok_locations` (the same list that answers "are you willing to relocate");
anything tied to another country (for example "Belize (Remote)") is dropped. Add cities, or `"*"`, to that list to widen it.

## How it applies
- **Workday** (where its applications have actually gone through) has its own driver. It always knows which step it is on
  (Create Account / Sign In → My Information → My Experience → Application Questions → Voluntary Disclosures → Self Identify
  → Review), fills the step, presses Next and then looks at what Workday did. If Workday sends the page back, it redoes only
  the fields Workday flagged (adding a required Work Experience / Education block if that is what was missing) and tries
  again; after three tries it stops and reports Workday's own words. A job Workday says you already applied to is recorded
  as applied and nothing is sent again. It never submits a Workday application whose résumé box did not show the file, and
  when your school is not in an employer's list it picks "Other", never a school with a similar name.
- **Accounts, on any site:** when a form needs one, the bot creates it with **delgado@alumni.usc.edu** and the
  `ACCOUNT_PASSWORD` secret; if the site says an account already exists it signs in; if the sign-in is refused it uses
  "Forgot password?", opens the reset email from your inbox, sets the password to `ACCOUNT_PASSWORD` and signs in. When a
  sign-up says "check your email", it finds that site's verification email and either types the code in (one box, or one
  box per digit; numbers like `482913`, `731 204` or letter-number codes like `X7K2QF`) or opens the verification link, then
  carries on with the application. It only ever takes the email of the site it is on (never another employer's, and never
  a reset email for a verify email).
- Other sites: multi-page forms (Next / Save and Continue) and one-page forms are read field by field and filled the same way.
- Uploads **your own résumé file** (`briandelgado_resume.pdf`, set by `resume_file` in `config.yaml`) unchanged on every application. No résumé is generated.
- **Cover letters:** when a form has a cover-letter upload or box, the bot writes a full one-page letter (about 320 to 380 words, four paragraphs, letterhead, date, signed with your name) from your real background only. If the writer can't: an optional letter is left out and it applies with the résumé alone; a **required** one gets `cover_letter_template` from `config.yaml` (a full, true one-page letter with the company and role filled in).
- **Résumé upload is checked:** styled upload boxes (Greenhouse, Lever, Workday) must show the file name before it moves on; if they don't, it
  uses the site's own Attach button. If a site still answers the submit with "Resume/CV is required", nothing was sent, so it attaches the
  résumé again and submits once more; if that fails too, the job is retried in a later run instead of being counted as submitted.
- Answers every standard question from your `facts` and `answers:` (work authorization as a permanent resident, sponsorship, EEO, dates,
  education, "N years of X", travel, relocation for the cities you approved). A required multiple-choice or short question that no rule covers
  is answered by the writer from your facts; required essays the writer can't do get `fallback_answer`. Only these still stop an application:
  human checks, legal waivers (arbitration etc.), "do you meet the minimum qualifications" certifications, AI-policy confirmations and oddly
  negated work-authorization questions. Each employer gets at most `max_per_company` (3) applications.
- Submits, then checks the confirmation page or the inbox. Everything is logged in `applications.db`, so nothing is applied to twice.
  The moment before Submit is clicked is written down first: whatever happens after that click (a crash, a timeout, a
  cancelled run), that application is not submitted a second time. Two exceptions, both cases where nothing was sent: the site
  itself bounced the submit with "X is required", or the form never contacted the employer's server after the click (that
  one gets one more try, and only after the inbox has been checked for a confirmation).

## What it will not do (on purpose), and what lands on your list
- **Human checks are never solved, entered or worked around:** CAPTCHAs, Cloudflare / "are you human" checks, and the
  security code some sites (Greenhouse) email *after* you press Submit to prove a person is applying. The application stops
  there at once, nothing is sent, and the job is listed under EXCEPTIONS for you to finish by hand. (Codes and links for
  *creating or signing in to an account* are a different thing, and the bot does handle those.)
- **No site is paused or skipped.** Instead they are tried in the order of what has actually been going through lately:
  sites where applications finish (Workday) first, sites whose submits keep ending at a human check last. The email shows
  the tally per site.
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
| `GROQ_API_KEY` | free writer key (console.groq.com/keys) | yes |
| `GEMINI_API_KEY` | optional second free writer (aistudio.google.com) | optional |
| `ACCOUNT_PASSWORD` | the one password used for every account the bot creates. **Workday requires 8+ characters with an uppercase letter, a lowercase letter, a number and a special character**; a weaker one makes every Workday application fail at the account step | for sites that need an account |
| `IMAP_USER`, `IMAP_PASS` | `delgado@alumni.usc.edu` and a Google **app password** for it (myaccount.google.com/apppasswords); used to confirm new accounts, confirm submits and send the summary emails. The log's first lines say `mail: logged in … OK` or `MAIL OFF` with the reason | for accounts and emails |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `JOOBLE_API_KEY` | free job-board keys | optional |

## Tuning (`config.yaml`)
- More applications: lower `min_score`, add boards to `boards.yaml`, add title keywords; `daily_cap` / `per_run_cap` are ceilings.
- Better matches: raise `min_score`, add `negative_keywords`, lower `max_years_hard`.
- Many `skipped`? The report lists each question it couldn't answer truthfully. Add a `facts` value, an `answers:` regex or a story in `profile.yaml`.
- Pay: target $75K; postings whose top pay is under `salary_floor` ($65K) lose 40 points. Locations: Denver / Colorado, Los Angeles area, NYC and
  remote score higher and are approved for relocation answers; add cities to `relocation_ok_locations`.
- `search.workday_places` and `search.workday_queries` control how the big Workday employers are searched ("<role> <city>").

## `settings.yaml` (public tuning, in the repository)
Merged over your private `config.yaml` on every run: sections merge, lists replace. Holds the match bar (`search.min_fit`, 60),
the entry-level setting (`search.max_level`: 0 entry only, 1 entry + early career), the senior-pay cutoff and the writer's model
list, so they can be tuned without re-sending secrets.

## Entry-level check
Every posting is rated entry / early / mid / senior from the years it *requires* (preferred years are ignored), whether it manages
people, title rank (senior, lead, manager, II/III ...), a graduate-degree requirement and pay (starting at $100K+ = not entry level).
Part-time, internships, veterans-only and MBA/PhD programs, clearance-required roles and pay under `salary_floor` are dropped.
Queued jobs are re-checked by title at the start of each run and against the full posting right before applying.

## Test runs on real forms (`probe` branch)
Pushing a change to `probe/request.yaml` on the `probe` branch runs `.github/workflows/probe.yml`: it opens real application forms,
fills them exactly like a live run and **never submits**, then saves what it found on each form (fields, answers, what stayed empty,
screenshots) to `probe/out/<id>/` on that branch. Live runs on `main` are not affected.

## The free writer (essays and cover letters)
Only providers with a key are used (the log's first lines list them). With just `GROQ_API_KEY`: Groq `gpt-oss-120b` → `llama-3.3-70b` →
`gpt-oss-20b` → `qwen3.8-27b`, each with its own free daily allowance; a model whose daily limit is used up is set aside until it resets,
and per-minute limits are waited out. A Gemini key (aistudio.google.com) adds Gemini in front. Ollama is only for running on your own Mac.
Only résumé-style facts are sent (never your address, phone or EEO answers).
- **Truth first.** The writer is told to use only your facts. An answer that mentions a number or tool not in your facts is sent back once
  for a fix; it is never thrown away and a job is never skipped because of it.
- **Your voice.** `voice.md` sets the style (answer first, 75 to 200 words, no buzzwords, no em dashes); `about_me.md` and `profile.yaml`
  hold the true stories and which experience to use for which kind of question.
- It doesn't try to fool AI detectors (no fake typos or tangents) and never hides AI use.

To improve `about_me.md` and `voice.md`, paste this into ChatGPT and read the result carefully (the writer treats it as true):
> Based on everything you know about me and our past conversations, write two things. (1) ABOUT ME: a plain-text background file, about 900 words, of true things that help write job applications: education, jobs and projects with concrete numbers, tools I use, how I like to work, my values, and real stories where I solved a problem, failed, resolved a conflict or led people, plus what I'm looking for next. Only include things I've actually told you; mark anything uncertain with (unsure). Leave out health, immigration, family and dating details. (2) MY VOICE: analyze how I write in my messages and describe my style in about 150 words: sentence length, tone, words I lean on or avoid, quirks. Don't flatter me.

## Statuses
`applied` confirmed · `unconfirmed` submitted but no confirmation seen (check the inbox, not retried) · `blocked` stopped by the site
(a human check, an account it could not get into): finish by hand ·
`skipped` a question it won't answer for you (see above), or the posting bans AI, or the per-employer cap · `failed` not sent (e.g. the form
rejected it), retried in a later run ·
`queued` waiting for a later run · `low_score` under the match bar or the keyword bar · `filtered` not a fit · `dry_run` filled but not sent

## Run it on your own machine instead (optional)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python -m playwright install chromium
export GROQ_API_KEY=... ACCOUNT_PASSWORD=... IMAP_USER=... IMAP_PASS=...
python -m autoapply --dry-run --limit 3    # fills 3 real forms and submits nothing; look at applications/<today>/*/form.png
python -m autoapply --loop                 # forever, checking every search.loop_minutes
```
The free-minutes limits don't apply there, so raise `max_run_minutes` if you like.

## Tests (no network, no keys)
`python tests/unit_checks.py` runs everything except the full mock run: question answering (83 real-world questions), fit scoring,
every job-board feed with mocked responses, the minutes budget, emails, inbox matching, the history-saving workflow, the update
script, the Workday driver, and a check of the bot's own code (no name used without being defined).
- `python tests/workday_checks.py`: the Workday driver against real Workday pages saved from employer sites (`tests/fixtures/workday`,
  personal details replaced) and against `tests/wd_mock.py`, a stand-in Workday site that behaves like the real one where it is
  hard: pop-up lists, boxes that are redrawn while you type, checkboxes that only react to their label, verify-your-email,
  forgot-password, required blocks behind "Add", pages sent back with complaints.
- `python tests/run_mock.py`: the real pipeline, live, through local mock forms and a mock writer: the match check, a multi-page
  wizard with account creation, emailed-confirmation handling, CAPTCHA and security-code stops, the per-employer cap and more.
- `python tests/captcha_boundary.py`: fails if code for a CAPTCHA-solving service ever appears in the bot.

These prove the logic. They cannot prove a real employer's site behaves like the stand-ins; that only shows in a real run, and
`logs/snapshots.log` is there so that whatever a real site does differently can be seen and fixed.
