#!/usr/bin/env bash
# One-time setup for a NEW install: creates a PRIVATE GitHub repo, pushes the code, stores your private files and keys
# as secrets. (Already set up? Use  bash update_github.sh  instead.)
#
# Needs the GitHub CLI (brew install gh; gh auth login). Export your keys first, then run from this folder:
#     export GROQ_API_KEY=...        # free, console.groq.com
#     export GEMINI_API_KEY=...      # free, aistudio.google.com
#     export ACCOUNT_PASSWORD=...    # the one password the bot uses when it creates accounts on employer sites
#     export IMAP_USER=you@gmail.com IMAP_PASS='app password'   # the inbox that receives your applications' emails
#     bash setup_github.sh [repo-name]
set -euo pipefail
cd "$(dirname "$0")"
REPO="${1:-autoapply}"
command -v gh >/dev/null || { echo "Install GitHub CLI first: brew install gh && gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run: gh auth login"; exit 1; }
for f in config.yaml profile.yaml about_me.md; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
: "${GROQ_API_KEY:?export GROQ_API_KEY=... first (free key from console.groq.com)}"
: "${GEMINI_API_KEY:?export GEMINI_API_KEY=... first (free key from aistudio.google.com)}"

OWNER="$(gh api user -q .login)"
SLUG="$OWNER/$REPO"
[ -d .git ] || git init -q -b main
git add -A && git commit -qm "autoapply" || true
gh repo create "$REPO" --private --source=. --remote=origin --push 2>/dev/null || git push -u origin main

gh secret set CONFIG_YAML  -R "$SLUG" < config.yaml
gh secret set PROFILE_YAML -R "$SLUG" < profile.yaml
gh secret set ABOUT_ME_MD  -R "$SLUG" < about_me.md
if [ -f voice.md ]; then gh secret set VOICE_MD -R "$SLUG" < voice.md; fi
for v in GROQ_API_KEY GEMINI_API_KEY ACCOUNT_PASSWORD IMAP_USER IMAP_PASS ADZUNA_APP_ID ADZUNA_APP_KEY JOOBLE_API_KEY; do
  val="$(printenv "$v" || true)"
  if [ -n "$val" ]; then gh secret set "$v" -R "$SLUG" --body "$val" && echo "  $v set"; fi
done
echo
echo "Secrets are set. It now runs by itself four times a day (Actions tab). To start the first run now:"
echo "    gh workflow run autoapply.yml -R $SLUG"
echo "Later changes:  bash update_github.sh"
