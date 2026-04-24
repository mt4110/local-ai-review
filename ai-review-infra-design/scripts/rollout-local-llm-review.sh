#!/usr/bin/env bash
set -euo pipefail

OWNER="${1:?Usage: ./rollout-local-llm-review.sh <OWNER> [LIMIT]}"
LIMIT="${2:-3}"
BRANCH="add-local-llm-review"
WORKROOT="$(mktemp -d)"
SOURCE_WORKFLOW="$(cd "$(dirname "$0")/.." && pwd)/workflows/local-llm-review-diff-only.yml"

trap 'rm -rf "$WORKROOT"' EXIT

gh auth status >/dev/null

if [ ! -f "$SOURCE_WORKFLOW" ]; then
  echo "Workflow not found: $SOURCE_WORKFLOW" >&2
  exit 1
fi

gh repo list "$OWNER" \
  --source \
  --no-archived \
  --json nameWithOwner \
  --limit "$LIMIT" \
  --jq '.[].nameWithOwner' |
while read -r repo
do
  echo "==> $repo"
  repo_dir="$WORKROOT/${repo//\//__}"

  gh repo clone "$repo" "$repo_dir" -- --quiet
  cd "$repo_dir"

  git checkout -B "$BRANCH"
  mkdir -p .github/workflows
  cp "$SOURCE_WORKFLOW" .github/workflows/local-llm-review.yml

  git add .github/workflows/local-llm-review.yml

  if git diff --cached --quiet; then
    echo "No workflow changes."
  else
    git commit -m "Add local LLM PR review workflow"
    git push --force-with-lease -u origin "$BRANCH"

    existing_pr="$(gh pr list --repo "$repo" --head "$BRANCH" --state open --json number --jq '.[0].number // empty')"
    if [ -z "$existing_pr" ]; then
      gh pr create \
        --repo "$repo" \
        --title "Add local LLM PR review workflow" \
        --body "Adds a label-triggered, diff-only local LLM PR review workflow.\n\nUsage: add the \`local-ai-review\` label to a PR.\n\nSecurity design: no checkout, no PR code execution, no OpenAI API key."
    fi
  fi

  gh label create local-ai-review \
    --repo "$repo" \
    --color "0e8a16" \
    --description "Run local LLM PR diff review" \
    2>/dev/null || true

done
