#!/usr/bin/env bash
# Pre-publish safety check. Inspects a COMMITTED git ref (default HEAD), i.e. exactly what a push
# would publish, not the working tree.   Usage: scripts/prepublish_check.sh [ref]
# Extra private words to refuse: QG_PRIVATE_WORDS="name|hostname|org_id" scripts/prepublish_check.sh main
set -u
cd "$(dirname "$0")/.."
ref=${1:-HEAD}
git rev-parse --verify --quiet "$ref^{commit}" >/dev/null || { echo "FAIL: no such commit: $ref"; exit 1; }

fail=0
bad() { printf 'FAIL: %s\n' "$*"; fail=1; }
files=$(git ls-tree -r --name-only "$ref")

# 1. Local override files are never part of the package.
printf '%s\n' "$files" | grep -E '^dashboard/local_[^/]*\.py$' && bad "local override file committed"

# 2. Data, caches, credential-shaped files.
printf '%s\n' "$files" | grep -E '\.(db|db-wal|db-shm|sqlite|log|pyc)$|auth\.json$|(^|/)\.env($|\.)|ledger|(^|/)fixture\.json$' \
  && bad "data/cache/credential-shaped files committed"

# 3. Secrets and personal identifiers in committed text.
patterns=(
  'sk-ant-[a-z]{2,4}[0-9]{2}-[A-Za-z0-9_-]{8,}'   # Anthropic keys / OAuth tokens
  'xai-[A-Za-z0-9]{20,}'                          # xAI API keys
  'Bearer [A-Za-z0-9._-]{20,}'                    # literal bearer tokens
  'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'     # JWTs
  '/home/[a-z0-9_-]+/'                            # absolute home paths
  '/Users/[A-Za-z0-9_-]+/'
  '/mnt/[a-z0-9_-]+/'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}'   # e-mail addresses
)
extra="${QG_PRIVATE_WORDS:-}"
while IFS= read -r f; do
  case "$f" in *.png|*.jpg|*.jpeg|*.gif|*.webp|scripts/prepublish_check.sh) continue ;; esac
  body=$(git show "$ref:$f")
  for p in "${patterns[@]}"; do
    hits=$(printf '%s\n' "$body" | grep -nEI "$p" | grep -vE 'example\.com|users\.noreply\.github\.com')
    [ -n "$hits" ] && { bad "$f matches /$p/"; printf '%s\n' "$hits" | head -3; }
  done
  if [ -n "$extra" ]; then
    hits=$(printf '%s\n' "$body" | grep -nEIi "$extra")
    [ -n "$hits" ] && { bad "$f contains a private word"; printf '%s\n' "$hits" | head -3; }
  fi
done <<< "$files"

# 4. Images only under docs/images (rendered from scripts/preview/fixture.demo.json).
printf '%s\n' "$files" | grep -E '\.(png|jpe?g|webp)$' | grep -v '^docs/images/' && bad "image outside docs/images"

# 5. Commit identity on every commit in the ref.
git log --format='%an <%ae>%n%cn <%ce>' "$ref" | sort -u | grep -vE 'users\.noreply\.github\.com>$' \
  && bad "a commit carries a non-noreply e-mail (above)"

# 6. Hermes's own admission check on the committed tree.
if command -v hermes >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  git archive "$ref" | tar -xf - -C "$tmp"
  hermes plugins validate "$tmp" >"$tmp.log" 2>&1 || { bad "hermes plugins validate failed"; tail -20 "$tmp.log"; }
  grep -E '⚠' "$tmp.log" && echo "WARN: validator warnings above; reviewers will read them"
  rm -rf "$tmp" "$tmp.log"
else
  echo "SKIP: hermes not on PATH; run 'hermes plugins validate' yourself"
fi

[ "$fail" -eq 0 ] && echo "OK: $ref is clean to publish" || echo "Fix the failures above before publishing."
exit "$fail"
