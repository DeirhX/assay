#!/bin/sh
# Single source of truth for the personal/financial-data leak patterns.
#
# The whole privacy model of this public repo is "holdings never land here" —
# real data lives in the private `data` submodule. This script encodes the
# blocked filenames and high-signal markers ONCE so the same rules run in three
# places:
#
#   - pre-commit hook   : tools/hooks/pre-commit  -> `leakcheck.sh staged`
#   - CI backstop       : .github/workflows/guard.yml -> `leakcheck.sh range …`
#   - Pages / tree scan : `leakcheck.sh tree site`
#
# Modes:
#   leakcheck.sh staged             Scan the staged index (git diff --cached).
#   leakcheck.sh range <BASE> <REF> Scan a commit range (BASE...REF) and also
#                                   fail if it moves the `data` submodule pointer.
#   leakcheck.sh tree <path>...     Scan tracked file CONTENTS under paths.
#
# Exit 0 = clean, non-zero = a potential leak was found.
#
# NOTE: deliberately NOT using `set -e`. The `[ -n "$x" ] && cmd` idiom below
# returns non-zero on the common no-match case, which would make `set -e` abort
# the script (silent false positive). All fallible commands are guarded with
# `|| true` instead.

# --- Patterns (this is the only copy; keep it that way) ---------------------

# Root-level *.html: generated mini-site pages that embed real holdings.
RE_BLOCKED_HTML='^[^/]+\.html$'

# Obviously sensitive filenames (matched case-insensitively).
RE_BLOCKED_FILES='(^|/)(current-holdings|portfolio)\.json$|(^|/)secrets\.env$|\.xml$|pplx-auth\.json$'

# High-signal personal-data markers, chosen to avoid firing on ordinary
# code/tests (which use bare numbers):
#   - IBKR account ids (Uxxxxxxx)
#   - a cash field carrying a real value
#   - CZK amounts in the millions (>=2 thousands-groups, or "N.Nm CZK")
#   - per-position USD P/L like minus-dollar-NN-point-N-k
RE_MARKERS='\bU[0-9]{7}\b|"ending_cash"[[:space:]]*:[[:space:]]*[0-9]|[0-9]{1,3}(,[0-9]{3}){2,}[[:space:]]*CZK|[0-9]+\.[0-9]+m[[:space:]]*CZK|-?\$[0-9]+(\.[0-9]+)?k\b'

fail=0

report() {
  # $1 = message, $2 = matched lines
  printf 'leakcheck: %s\n' "$1"
  printf '%s\n' "$2" | head -n 20 | sed 's/^/  /'
  fail=1
}

scan_names() {
  # $1 = newline-separated file paths
  names="$1"
  if [ -z "$names" ]; then return 0; fi

  html=$(printf '%s\n' "$names" | grep -E "$RE_BLOCKED_HTML" || true)
  if [ -n "$html" ]; then
    report "refusing root HTML pages (generated, may embed real holdings):" "$html"
  fi

  files=$(printf '%s\n' "$names" | grep -Ei "$RE_BLOCKED_FILES" || true)
  if [ -n "$files" ]; then
    report "refusing sensitive files:" "$files"
  fi
}

scan_added_lines() {
  # $1 = diff text; only added (+) lines are inspected
  diff_text="$1"
  if [ -z "$diff_text" ]; then return 0; fi
  markers=$(printf '%s\n' "$diff_text" | grep -E '^\+' | grep -E "$RE_MARKERS" || true)
  if [ -n "$markers" ]; then
    report "changed lines contain possible personal financial data:" "$markers"
  fi
}

mode="${1:-staged}"

case "$mode" in
  staged)
    names=$(git diff --cached --name-only --diff-filter=AM || true)
    diff_text=$(git diff --cached -U0 --diff-filter=AM || true)
    scan_names "$names"
    scan_added_lines "$diff_text"
    ;;

  range)
    base="${2:?range mode needs <BASE> <REF>}"
    ref="${3:?range mode needs <BASE> <REF>}"
    names=$(git diff --name-only --diff-filter=AM "$base...$ref" || true)
    diff_text=$(git diff -U0 --diff-filter=AM "$base...$ref" || true)
    scan_names "$names"
    scan_added_lines "$diff_text"

    # Never move the private `data` submodule pointer in a public PR.
    submod=$(git diff --name-only "$base...$ref" | grep -E '^data$' || true)
    if [ -n "$submod" ]; then
      report "PR modifies the private 'data' submodule pointer — not allowed in the public repo:" "$submod"
    fi
    ;;

  tree)
    shift
    if [ "$#" -lt 1 ]; then
      echo "leakcheck: tree mode needs at least one path" >&2
      exit 2
    fi
    tracked=$(git ls-files -- "$@" || true)
    if [ -n "$tracked" ]; then
      names_hit=$(printf '%s\n' "$tracked" | grep -Ei "$RE_BLOCKED_FILES" || true)
      if [ -n "$names_hit" ]; then
        report "tracked sensitive files under scanned paths:" "$names_hit"
      fi
      content_hits=$(printf '%s\n' "$tracked" | xargs -r grep -EnI "$RE_MARKERS" 2>/dev/null || true)
      if [ -n "$content_hits" ]; then
        report "file contents contain possible personal financial data:" "$content_hits"
      fi
    fi
    ;;

  *)
    echo "leakcheck: unknown mode '$mode' (use: staged | range <BASE> <REF> | tree <path>...)" >&2
    exit 2
    ;;
esac

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "leakcheck: blocked. Personal data belongs in the private 'data' submodule."
  echo "If this is a genuine false positive in a local commit, bypass with: git commit --no-verify"
fi
exit "$fail"
