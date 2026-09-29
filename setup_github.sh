#!/usr/bin/env bash
# One-shot setup: creates a PRIVATE GitHub repo, pushes the code, stores your private files as secrets.
# Needs: GitHub CLI (brew install gh; gh auth login). Run from this folder:  bash setup_github.sh
set -euo pipefail
REPO="${1:-autoapply}"
command -v gh >/dev/null || { echo "Install GitHub CLI first: brew install gh && gh auth login"; exit 1; }
gh auth status >/dev/null || { echo "Run: gh auth login"; exit 1; }
for f in config.yaml profile.yaml about_me.md; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
: "${GROQ_API_KEY:?export GROQ_API_KEY=... first (free key from console.groq.com)}"
: "${GEMINI_API_KEY:?export GEMINI_API_KEY=... first (free key from aistudio.google.com)}"

[ -d .git ] || git init -q -b main
git add -A && git commit -qm "autoapply" || true
gh repo create "$REPO" --private --source=. --remote=origin --push 2>/dev/null || git push -u origin main
gh secret set CONFIG_YAML  < config.yaml
gh secret set PROFILE_YAML < profile.yaml
gh secret set ABOUT_ME_MD  < about_me.md
[ -f voice.md ] && gh secret set VOICE_MD < voice.md
gh secret set GROQ_API_KEY   --body "$GROQ_API_KEY"
gh secret set GEMINI_API_KEY --body "$GEMINI_API_KEY"
echo "Secrets set. Starting a DRY RUN (fills forms, submits nothing)..."
gh workflow run autoapply.yml -f dry_run=true
echo "Watch it: gh run watch    |  when the dry run looks right:  gh workflow run autoapply.yml"
