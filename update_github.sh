#!/usr/bin/env bash
# Pushes code changes and refreshes your private secrets. Run from the autoapply folder:  bash update_github.sh
set -euo pipefail
git add -A && git commit -qm "update" || true
git push -q -u origin HEAD
gh secret set CONFIG_YAML  < config.yaml
gh secret set PROFILE_YAML < profile.yaml
gh secret set ABOUT_ME_MD  < about_me.md
[ -f voice.md ] && gh secret set VOICE_MD < voice.md
echo "Updated. Start a dry run:  gh workflow run autoapply.yml -f dry_run=true   (or a real run without the -f part)"
