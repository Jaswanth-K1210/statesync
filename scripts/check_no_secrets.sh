#!/usr/bin/env bash
# Fail if .env.example carries a value, or if a key-shaped string is staged.
#
# This has already happened once: real keys were pasted into .env.example,
# which is a tracked file. They were caught before the commit, but only
# because someone looked. This makes looking automatic.

set -euo pipefail
status=0

if [ -f .env.example ]; then
  filled=$(grep -vE '^\s*#' .env.example | grep -E '=[^[:space:]]+' | grep -vE '=(anthropic/|openai/|llama|meta-llama|qwen|groq/)' || true)
  if [ -n "$filled" ]; then
    echo "BLOCKED: .env.example has values after '='. It documents names only." >&2
    echo "$filled" | sed 's/=.*/=<redacted>/' | sed 's/^/  /' >&2
    echo "  Move real values to .env (gitignored)." >&2
    status=1
  fi
fi

staged=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
if [ -n "$staged" ]; then
  if git diff --cached -- $staged 2>/dev/null \
      | grep -qE '^\+.*(sk-or-v1-[A-Za-z0-9]{12,}|gsk_[A-Za-z0-9]{12,}|sk-ant-[A-Za-z0-9]{12,})'; then
    echo "BLOCKED: a provider key appears in the staged diff." >&2
    status=1
  fi
fi

if echo "$staged" | grep -qx '.env'; then
  echo "BLOCKED: .env is staged. It must never be committed." >&2
  status=1
fi

[ "$status" -eq 0 ] && echo "secret check: clean"
exit "$status"
