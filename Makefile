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

# The optional transcription image. Deliberately NOT built by `make build`:
# nothing else needs it, and an installation that does not want local speech-to-
# text should not be downloading half a gigabyte of model weights to find out.
# Must match `transcribe.image` and `transcribe.model_cache` in the config.
TRANSCRIBE_IMAGE ?= telegram-ai-cli-transcribe:latest
STATE_HOME ?= $(if $(XDG_STATE_HOME),$(XDG_STATE_HOME),$(HOME)/.local/state)
TRANSCRIBE_CACHE ?= $(STATE_HOME)/telegram-ai-cli/whisper-models

# Empty by default so `make test` alone runs the whole suite via Dockerfile.test's
# CMD; set it to target specific tests, e.g. `make test PYTEST_ARGS="-k plans -x"`.
PYTEST_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help build test lint fmt shell transcribe-image transcribe-model

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

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

transcribe-image: ## Build the OPTIONAL local transcription image (Whisper small)
	docker build -f Dockerfile.transcribe -t $(TRANSCRIBE_IMAGE) \
		--build-arg UID=$(UID) --build-arg GID=$(GID) .

# The one command in this project that downloads a model, and the only
# invocation of the transcriber that has a network at all. `media transcribe`
# runs the same image with --network none, so the weights have to already be
# here — which is exactly why fetching them is a separate, visible step rather
# than something a tool call does on its own the first time it is used.
transcribe-model: ## Download the Whisper model once into the local cache
	mkdir -p "$(TRANSCRIBE_CACHE)"
	docker run --rm \
		--user $(UID):$(GID) \
		-v "$(TRANSCRIBE_CACHE)":/models \
		-e HF_HOME=/models \
		$(TRANSCRIBE_IMAGE) download-model

shell: ## Drop into a shell in the test image with the repo bind-mounted
	docker build -f Dockerfile.test -t $(TEST_IMAGE) .
	docker run --rm -it \
		--user $(UID):$(GID) \
		-v "$(CURDIR)":/app \
		-w /app \
		--entrypoint bash \
		$(TEST_IMAGE)
