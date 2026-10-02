#!/usr/bin/env bash
# Publishes this folder's code to your private GitHub repo and refreshes your private secrets.
# Run it from this folder whenever you change something (or after unzipping a new version over it):
#     bash update_github.sh            # publish code + secrets
#     bash update_github.sh --run      # ...and start a run right away
#
# It never touches the bot's memory in the repo (applications.db, boards_state.json, reports/, logs/ ...), so nothing that
# was already applied to can be applied to twice, and it works even though the bot commits to the repo between your updates.
#
# Secrets that are not files are only changed when you export them first, for example:
#     export IMAP_PASS='xxxx xxxx xxxx xxxx' && bash update_github.sh
# (GROQ_API_KEY GEMINI_API_KEY ACCOUNT_PASSWORD IMAP_USER IMAP_PASS ADZUNA_APP_ID ADZUNA_APP_KEY JOOBLE_API_KEY)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
cd "$SRC"

RUN_AFTER=0
[ "${1:-}" = "--run" ] && RUN_AFTER=1

command -v gh  >/dev/null || { echo "Install the GitHub CLI first:  brew install gh && gh auth login"; exit 1; }
command -v git >/dev/null || { echo "Install git first (Xcode command line tools: xcode-select --install)"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "You are not logged in to GitHub. Run:  gh auth login"; exit 1; }
for f in config.yaml profile.yaml about_me.md; do
  [ -f "$f" ] || { echo "Missing $f. It has to be in this folder (it holds your private answers)."; exit 1; }
done

RF="$(sed -n 's/^resume_file: *//p' config.yaml | head -1 | tr -d "'\"" | tr -d '\r')"
[ -z "$RF" ] || [ -f "$RF" ] || { echo "config.yaml names resume_file $RF but it is not in this folder."; exit 1; }

OWNER="${AUTOAPPLY_OWNER:-$(gh api user -q .login)}"
REPO="${AUTOAPPLY_REPO:-autoapply}"
SLUG="$OWNER/$REPO"
gh repo view "$SLUG" >/dev/null 2>&1 || { echo "There is no repo $SLUG yet. Run  bash setup_github.sh  once first."; exit 1; }

# ---- 1. code: copy into a fresh clone of the repo, so the bot's saved history is never touched
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
gh repo clone "$SLUG" "$TMP/repo" -- -q

# Never put an older copy of the code over a newer one: the code on GitHub may have been updated since this folder was made
# (builds are named like 2026-10-01-ab; after -z comes -aa).
build() { if [ -f "$1" ]; then sed -n 's/^__version__ *= *"\(.*\)".*/\1/p' "$1" | head -1; fi; }
order() { echo "$1" | awk -F- '{ printf "%s-%s-%s-%02d%s", $1, $2, $3, length($4), $4 }'; }
HERE_V="$(build "$SRC/autoapply/__init__.py")"
THERE_V="$(build "$TMP/repo/autoapply/__init__.py")"
PUSH_CODE=1
if [ -n "$HERE_V" ] && [ -n "$THERE_V" ] && [ "$HERE_V" != "$THERE_V" ] && [ "${AUTOAPPLY_PUSH_OLDER:-0}" != 1 ]; then
  first="$(printf '%s\n%s\n' "$(order "$HERE_V")" "$(order "$THERE_V")" | LC_ALL=C sort | head -1)"
  if [ "$first" = "$(order "$HERE_V")" ]; then
    PUSH_CODE=0
    echo "The code on GitHub (build $THERE_V) is newer than this folder (build $HERE_V): leaving the code on GitHub as it is."
    echo "(Your secrets are still refreshed below. To get the newest code into a folder:  gh repo clone $SLUG)"
  fi
fi

if [ "$PUSH_CODE" = 1 ]; then
for p in autoapply tests .github boards.yaml README.md requirements.txt setup_github.sh update_github.sh .gitignore; do
  rm -rf "$TMP/repo/$p"
  [ -e "$SRC/$p" ] && cp -R "$SRC/$p" "$TMP/repo/$p"
done
for f in "$SRC"/*.pdf; do [ -f "$f" ] && cp "$f" "$TMP/repo/"; done      # your résumé (resume_file in config.yaml)
find "$TMP/repo" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$TMP/repo/tests/work"

cd "$TMP/repo"
git config user.name  "$(git -C "$SRC" config user.name  2>/dev/null || echo "${OWNER}")"
git config user.email "$(git -C "$SRC" config user.email 2>/dev/null || echo "${OWNER}@users.noreply.github.com")"
git add -A
if git diff --cached --quiet; then
  echo "Code on GitHub is already up to date."
else
  git commit -qm "update autoapply"
  pushed=0
  for i in 1 2 3 4 5 6; do
    if git push -q origin HEAD:main 2>/dev/null; then pushed=1; break; fi
    git pull -q --rebase origin main || true          # a run saved its history in the meantime
    sleep $((i * 3))
  done
  [ "$pushed" = 1 ] || { echo "Could not push the code. Try again in a minute."; exit 1; }
  echo "Code pushed."
fi
fi
cd "$SRC"

# ---- 2. secrets
echo "Refreshing secrets on $SLUG ..."
gh secret set CONFIG_YAML  -R "$SLUG" < config.yaml
gh secret set PROFILE_YAML -R "$SLUG" < profile.yaml
gh secret set ABOUT_ME_MD  -R "$SLUG" < about_me.md
if [ -f voice.md ]; then gh secret set VOICE_MD -R "$SLUG" < voice.md; fi
for v in GROQ_API_KEY GEMINI_API_KEY ACCOUNT_PASSWORD IMAP_USER IMAP_PASS ADZUNA_APP_ID ADZUNA_APP_KEY JOOBLE_API_KEY; do
  val="$(printenv "$v" || true)"
  if [ -n "$val" ]; then gh secret set "$v" -R "$SLUG" --body "$val" && echo "  $v updated"; fi
done

have="$(gh secret list -R "$SLUG" 2>/dev/null | awk '{print $1}')"
missing=""
for need in CONFIG_YAML PROFILE_YAML ABOUT_ME_MD GROQ_API_KEY ACCOUNT_PASSWORD IMAP_USER IMAP_PASS; do
  echo "$have" | grep -qx "$need" || missing="$missing $need"
done
if [ -n "$missing" ]; then
  echo
  echo "Still missing on GitHub:$missing"
  echo "Export each one and run this script again, e.g.:  export IMAP_PASS='...' && bash update_github.sh"
  echo "(Without ACCOUNT_PASSWORD / IMAP_USER / IMAP_PASS the bot skips every site that needs an account.)"
fi

echo
if [ "$RUN_AFTER" = 1 ]; then
  gh workflow run autoapply.yml -R "$SLUG" && echo "Started a run. Watch it: gh run watch -R $SLUG   (let it finish; cancelling wastes free minutes)"
else
  echo "Done. The next scheduled run uses the new code. To start one now:  gh workflow run autoapply.yml -R $SLUG"
fi
