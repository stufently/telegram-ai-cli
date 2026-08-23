# Everything here runs through Docker. Nothing is installed on the host —
# that is a hard project rule (see CLAUDE.md environment rules), not a style
# preference, so resist the urge to add a "just run pytest directly" shortcut.
#
# UID/GID default to the *invoking* user, not a hardcoded value: a bind-mounted
# repo must stay owned by whoever ran `make`, on whichever host/user this runs
# under (see CLAUDE.md: containers must run as the host's own non-root user).
UID ?= $(shell id -u)
GID ?= $(shell id -g)

IMAGE ?= telegram-ai-cli
TEST_IMAGE ?= telegram-ai-cli-test
RUFF_IMAGE := ghcr.io/astral-sh/ruff:0.16.4

# Empty by default so `make test` alone runs the whole suite via Dockerfile.test's
# CMD; set it to target specific tests, e.g. `make test PYTEST_ARGS="-k plans -x"`.
PYTEST_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help build test lint fmt shell

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build: ## Build the runtime image (tg-ai entrypoint, non-root)
	docker build -f Dockerfile -t $(IMAGE):latest \
		--build-arg UID=$(UID) --build-arg GID=$(GID) .

test: ## Build the test image and run pytest inside it (PYTEST_ARGS=...)
	docker build -f Dockerfile.test -t $(TEST_IMAGE) .
	docker run --rm \
		--user $(UID):$(GID) \
		-v "$(CURDIR)":/app \
		-w /app \
		$(TEST_IMAGE) $(PYTEST_ARGS)

# The ruff image is distroless: its entrypoint is ruff itself and there is no
# shell to run `sh -c "... && ..."` in. Hence two invocations rather than one
# chained command; make already stops the target on the first failure.
# --no-cache because the source is mounted read-only and ruff would otherwise
# try to write .ruff_cache into it.
lint: ## Check formatting and lint rules without modifying files (ruff, read-only)
	docker run --rm \
		--user $(UID):$(GID) \
		-v "$(CURDIR)":/io:ro \
		-w /io \
		$(RUFF_IMAGE) check --no-cache .
	docker run --rm \
		--user $(UID):$(GID) \
		-v "$(CURDIR)":/io:ro \
		-w /io \
		$(RUFF_IMAGE) format --no-cache --check .

fmt: ## Apply ruff formatting and autofixes in place
	docker run --rm \
		--user $(UID):$(GID) \
		-v "$(CURDIR)":/io \
		-w /io \
		$(RUFF_IMAGE) check --fix .
	docker run --rm \
		--user $(UID):$(GID) \
		-v "$(CURDIR)":/io \
		-w /io \
		$(RUFF_IMAGE) format .

shell: ## Drop into a shell in the test image with the repo bind-mounted
	docker build -f Dockerfile.test -t $(TEST_IMAGE) .
	docker run --rm -it \
		--user $(UID):$(GID) \
		-v "$(CURDIR)":/app \
		-w /app \
		--entrypoint bash \
		$(TEST_IMAGE)
