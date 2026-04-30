.PHONY: install-local llreview update-local precision-review precision-review-static precision-review-self-test pre-pr-review pre-pr-review-static review-db-init review-db-stats review-db-up review-db-web review-db-down review-db-score

REPO ?=
PR ?=
BASE ?= main
PROJECT_DIR ?=
REVIEW_DB ?= out/review-history/local-ai-review.db
REVIEW_REPORT ?= out/reviews/precision-review.md
INCLUDE_WORKING_TREE ?= 1
SQLITE_BROWSER_PORT ?= 8003
RUN ?=
USEFUL ?=
FALSE_POSITIVES ?=
UNCLEAR ?=
REMOTE_READY ?=
REMOTE_FINDINGS ?=
NOTE ?=

llreview:
	./llreview

install-local:
	./llreview install

update-local:
	./llreview update

precision-review:
	@test -n "$(REPO)" || { echo "REPO=owner/name is required"; exit 2; }
	@test -n "$(PR)" || { echo "PR=<number> is required"; exit 2; }
	python3 scripts/local-ai-precision-review.py \
		--repo "$(REPO)" \
		--pr "$(PR)" \
		--output "$(REVIEW_REPORT)" \
		--db "$(REVIEW_DB)"

precision-review-static:
	@test -n "$(REPO)" || { echo "REPO=owner/name is required"; exit 2; }
	@test -n "$(PR)" || { echo "PR=<number> is required"; exit 2; }
	python3 scripts/local-ai-precision-review.py \
		--repo "$(REPO)" \
		--pr "$(PR)" \
		--max-model-files 0 \
		--output "$(REVIEW_REPORT)" \
		--db "$(REVIEW_DB)"

precision-review-self-test:
	python3 scripts/local-ai-precision-review.py --self-test

pre-pr-review:
	@test -n "$(REPO)" || { echo "REPO=owner/name is required"; exit 2; }
	@test -n "$(PROJECT_DIR)" || { echo "PROJECT_DIR=/absolute/path/to/project is required"; exit 2; }
	@set -e; \
	tmp_diff="$$(mktemp "$${TMPDIR:-/tmp}/local-ai-pre-pr.XXXXXX")"; \
	tmp_index="$$(mktemp "$${TMPDIR:-/tmp}/local-ai-pre-pr-index.XXXXXX")"; \
	trap 'rm -f "$$tmp_diff" "$$tmp_index"' EXIT INT TERM; \
	index_path="$$(git -C "$(PROJECT_DIR)" rev-parse --git-path index)"; \
	if [ -f "$$index_path" ]; then cp "$$index_path" "$$tmp_index"; else git -C "$(PROJECT_DIR)" read-tree --index-output="$$tmp_index" HEAD; fi; \
	git -C "$(PROJECT_DIR)" rev-parse --verify "$(BASE)" >/dev/null; \
	head_ref="$$(git -C "$(PROJECT_DIR)" branch --show-current)"; \
	if [ -z "$$head_ref" ]; then head_ref="$$(git -C "$(PROJECT_DIR)" rev-parse --short HEAD)"; fi; \
	head_sha="$$(git -C "$(PROJECT_DIR)" rev-parse HEAD)"; \
	diff_source_label="pre_pr:$(BASE)...$$head_ref"; \
	working_tree_arg=""; \
	git -C "$(PROJECT_DIR)" diff "$(BASE)...HEAD" > "$$tmp_diff"; \
	if [ "$(INCLUDE_WORKING_TREE)" = "1" ]; then \
		printf '\n' >> "$$tmp_diff"; \
		GIT_INDEX_FILE="$$tmp_index" git -C "$(PROJECT_DIR)" add -N -- .; \
		GIT_INDEX_FILE="$$tmp_index" git -C "$(PROJECT_DIR)" diff HEAD >> "$$tmp_diff"; \
		working_tree_arg="--working-tree-included"; \
	fi; \
	python3 scripts/local-ai-precision-review.py \
		--repo "$(REPO)" \
		--pr 0 \
		--diff-file "$$tmp_diff" \
		--review-kind pre_pr \
		--diff-source-label "$$diff_source_label" \
		--base-ref "$(BASE)" \
		--head-ref "$$head_ref" \
		--head-sha "$$head_sha" \
		$$working_tree_arg \
		--output "$(REVIEW_REPORT)" \
		--db "$(REVIEW_DB)"

pre-pr-review-static:
	@test -n "$(REPO)" || { echo "REPO=owner/name is required"; exit 2; }
	@test -n "$(PROJECT_DIR)" || { echo "PROJECT_DIR=/absolute/path/to/project is required"; exit 2; }
	@set -e; \
	tmp_diff="$$(mktemp "$${TMPDIR:-/tmp}/local-ai-pre-pr.XXXXXX")"; \
	tmp_index="$$(mktemp "$${TMPDIR:-/tmp}/local-ai-pre-pr-index.XXXXXX")"; \
	trap 'rm -f "$$tmp_diff" "$$tmp_index"' EXIT INT TERM; \
	index_path="$$(git -C "$(PROJECT_DIR)" rev-parse --git-path index)"; \
	if [ -f "$$index_path" ]; then cp "$$index_path" "$$tmp_index"; else git -C "$(PROJECT_DIR)" read-tree --index-output="$$tmp_index" HEAD; fi; \
	git -C "$(PROJECT_DIR)" rev-parse --verify "$(BASE)" >/dev/null; \
	head_ref="$$(git -C "$(PROJECT_DIR)" branch --show-current)"; \
	if [ -z "$$head_ref" ]; then head_ref="$$(git -C "$(PROJECT_DIR)" rev-parse --short HEAD)"; fi; \
	head_sha="$$(git -C "$(PROJECT_DIR)" rev-parse HEAD)"; \
	diff_source_label="pre_pr:$(BASE)...$$head_ref"; \
	working_tree_arg=""; \
	git -C "$(PROJECT_DIR)" diff "$(BASE)...HEAD" > "$$tmp_diff"; \
	if [ "$(INCLUDE_WORKING_TREE)" = "1" ]; then \
		printf '\n' >> "$$tmp_diff"; \
		GIT_INDEX_FILE="$$tmp_index" git -C "$(PROJECT_DIR)" add -N -- .; \
		GIT_INDEX_FILE="$$tmp_index" git -C "$(PROJECT_DIR)" diff HEAD >> "$$tmp_diff"; \
		working_tree_arg="--working-tree-included"; \
	fi; \
	python3 scripts/local-ai-precision-review.py \
		--repo "$(REPO)" \
		--pr 0 \
		--diff-file "$$tmp_diff" \
		--review-kind pre_pr \
		--diff-source-label "$$diff_source_label" \
		--base-ref "$(BASE)" \
		--head-ref "$$head_ref" \
		--head-sha "$$head_sha" \
		$$working_tree_arg \
		--max-model-files 0 \
		--output "$(REVIEW_REPORT)" \
		--db "$(REVIEW_DB)"

review-db-init:
	python3 scripts/local-ai-precision-review.py --init-db --db "$(REVIEW_DB)"

review-db-stats: review-db-init
	printf '.headers on\n.mode column\nSELECT id, created_at, review_kind, repo, pr_number, head_ref, findings_count, watch_items_count, ROUND(elapsed_seconds, 1) AS elapsed_s FROM review_run_summary ORDER BY id DESC LIMIT 10;\n' | sqlite3 "$(REVIEW_DB)"

review-db-up: review-db-init
	REVIEW_DB_DIR="$(abspath $(dir $(REVIEW_DB)))" \
	REVIEW_DB_FILE="$(notdir $(REVIEW_DB))" \
	REVIEW_DB_PORT="$(SQLITE_BROWSER_PORT)" \
	docker compose -f docker-compose.review-db.yml up -d --build

review-db-web: review-db-up
	@url="http://127.0.0.1:$(SQLITE_BROWSER_PORT)"; \
	echo "DB browser: $$url"; \
	if [ "$$(uname -s)" = "Darwin" ] && command -v open >/dev/null 2>&1; then \
		open "$$url"; \
	elif command -v xdg-open >/dev/null 2>&1; then \
		xdg-open "$$url" >/dev/null 2>&1; \
	else \
		echo "No supported browser opener found; open the URL manually."; \
	fi

review-db-down:
	REVIEW_DB_DIR="$(abspath $(dir $(REVIEW_DB)))" \
	REVIEW_DB_FILE="$(notdir $(REVIEW_DB))" \
	REVIEW_DB_PORT="$(SQLITE_BROWSER_PORT)" \
	docker compose -f docker-compose.review-db.yml down

review-db-score: review-db-init
	@test -n "$(RUN)" || { echo "RUN=<review_runs.id> is required"; exit 2; }
	@test -n "$(USEFUL)" || { echo "USEFUL=<count> is required"; exit 2; }
	@test -n "$(FALSE_POSITIVES)" || { echo "FALSE_POSITIVES=<count> is required"; exit 2; }
	@test -n "$(UNCLEAR)" || { echo "UNCLEAR=<count> is required"; exit 2; }
	@test -n "$(REMOTE_READY)" || { echo "REMOTE_READY=yes|no is required"; exit 2; }
	python3 scripts/review-db-score.py \
		--db "$(REVIEW_DB)" \
		--run-id "$(RUN)" \
		--useful-findings-fixed "$(USEFUL)" \
		--false-positives "$(FALSE_POSITIVES)" \
		--unclear-findings "$(UNCLEAR)" \
		--would-request-remote-review-now "$(REMOTE_READY)" \
		$(if $(REMOTE_FINDINGS),--remote-findings-count "$(REMOTE_FINDINGS)",) \
		--note "$(NOTE)"
