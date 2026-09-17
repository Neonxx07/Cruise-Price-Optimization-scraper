#!/usr/bin/env bash
# Install the repository's git hooks. Run once per clone:
#
#     ./.githooks/install.sh
#
# Git does not install hooks automatically when you clone -- that is a
# deliberate security property of git, not an oversight -- so every clone
# (including every fresh clone after a history rewrite) needs this once.
set -euo pipefail

cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit 2>/dev/null || true

echo "Installed: core.hooksPath -> .githooks"
echo

DENY="$(git rev-parse --git-dir)/sensitive-terms.txt"
if [ ! -f "$DENY" ]; then
  cat > "$DENY" <<'EOF'
# Exact private strings to block from every commit, one per line.
# This file lives inside .git/ and is NEVER committed -- put real values here.
# Blank lines and #-comments are ignored. Matching is case-insensitive.
#
# Suggested contents:
#   - the operator's real first and last name
#   - the agent login / agency id used on the cruise-line portals
#   - any customer name that has appeared in captured data
#   - specific real booking references you want hard-blocked
#
# Example (replace with real values, then delete these lines):
# Jane Smith
# agentlogin123
EOF
  echo "Created $DENY"
  echo "  -> add your real name, agent login and agency id to it now."
else
  echo "Local denylist already present: $DENY"
fi

echo
echo "Verify with:  git config core.hooksPath"
