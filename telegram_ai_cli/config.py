"""Configuration: a YAML file overlaid with ``TGAI_``-prefixed environment vars.

Two rules shape the defaults here.

**Write is fail-closed, read is fail-closed for private chats.** An empty
allowlist means "nothing", never "everything". The opposite convention is
common and it is how a tool ends up permitting the one thing nobody meant to
permit — the list was empty because it had not been filled in yet.

**Groups and channels read freely; direct messages do not.** A tool that can
read nothing until it is configured cannot even list chats to discover their
ids, so it is useless on first run and gets configured carelessly. Private
correspondence is the part worth gating, and it is gated.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("TGAI_CONFIG", Path.home() / ".config" / "telegram-ai-cli" / "tgai.yaml")
)

Profile = Literal["readonly", "plan"]

#: Chats no configuration may open. Not a default — a constant.
#:
#: 777000 is Telegram Service Notifications, where login codes and 2FA resets
#: arrive; reading it hands over the account. Saved Messages is where people
#: keep passwords and documents precisely because it feels private.
#: Both are excluded in code so that no combination of settings, and no text
#: arriving through a tool, can reach them.
HARD_DENIED_PEERS: frozenset[int] = frozenset({777000})
HARD_DENY_SAVED_MESSAGES = True

#: Telegram's own ceiling for one upload from a non-premium account (2 GiB).
#: Configuring more than this would only move the failure from a refusal here to
#: a rejection partway through the transfer, so `upload.max_file_bytes` is
#: bounded by it rather than merely compared against it.
TELEGRAM_MAX_UPLOAD_BYTES = 2 * 1024**3


def _state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "telegram-ai-cli"


class PathsConfig(BaseModel):
    sessions: Path = Field(default_factory=lambda: _state_home() / "sessions")
    state: Path = Field(default_factory=_state_home)
    downloads: Path = Field(default_factory=lambda: _state_home() / "downloads")
    #: The only directory a plan may upload a file from — a chat photo, or the
    #: file `message send-file` sends. A separate directory from `downloads` on
    #: purpose: that one fills with media strangers sent, and "publish this file
    #: to a chat" should not be able to name one of those by accident.
    uploads: Path = Field(default_factory=lambda: _state_home() / "uploads")
    audit_log: Path = Field(default_factory=lambda: _state_home() / "audit.jsonl")
    #: The local message archive, filled only by `archive sync` on a named chat.
    #: A file of its own rather than a table in `state.db`, so that erasing every
    #: archived message cannot take the account registry, the pending plans and
    #: the rate-limit history with it. Created 0600 and not encrypted — the
    #: reasoning is in `telegram_ai_cli/archive.py` and `docs/configuration.md`.
    archive: Path = Field(default_factory=lambda: _state_home() / "archive.sqlite3")

    @field_validator("uploads")
    @classmethod
    def _uploads_must_be_absolute(cls, value: Path) -> Path:
        """Refuse a relative outbox, and an empty one especially.

        `uploads` is not merely a location: it is the allowlist deciding which
        files may leave this machine. A relative value resolves against the
        directory this process happened to be started in, and `Path("")` is
        `Path(".")` — so an outbox left blank in the config would quietly permit
        whatever sits in the working directory, which for a shell started in
        `$HOME` includes `.ssh`. The other paths are storage this tool writes
        to, and get no such rule.
        """
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError(
                "paths.uploads must be an absolute path: it decides which files may be "
                "sent, and a relative one would resolve against the working directory "
                "this process was launched from"
            )
        return value


class PeerRule(BaseModel):
    """An allow/deny pair for one capability.

    ``deny`` always wins. Keeping it per-capability rather than global is what
    lets "may read this group" and "may send to this group" be different
    answers, which they usually are.
    """

    allow: list[int | str] = Field(default_factory=list)
    deny: list[int | str] = Field(default_factory=list)


class ReadPolicy(BaseModel):
    #: Groups, channels and supergroups. Empty allow = every non-denied chat.
    chats: PeerRule = Field(default_factory=PeerRule)
    #: One-to-one conversations. Empty allow = NONE. See the module docstring.
    dms: PeerRule = Field(default_factory=PeerRule)
    members: PeerRule = Field(default_factory=PeerRule)
    media: PeerRule = Field(default_factory=PeerRule)
    #: Listing every dialog is how a reader finds what exists. Off by default:
    #: enumeration is the cheapest possible reconnaissance step.
    allow_dialog_enumeration: bool = True
    #: Enumeration still hides private chats unless they are allowlisted.
    enumerate_dms: bool = False
    #: The account's own device list: which apps are signed in, from roughly
    #: where, and when. On by default — it is the account's own security
    #: metadata, and no chat content is in it — but it carries device names and
    #: a coarse location, so it has a switch.
    sessions: bool = True


class WritePolicy(BaseModel):
    send: PeerRule = Field(default_factory=PeerRule)
    admin: PeerRule = Field(default_factory=PeerRule)
    join: PeerRule = Field(default_factory=PeerRule)
    #: Profile changes are account-scoped, so a chat list cannot express them.
    profile_enabled: bool = False


class LimitsConfig(BaseModel):
    """Rolling windows, persisted.

    In-memory counters reset on restart, which turns any limit into a
    suggestion: restart the process and the budget is fresh. These live in
    SQLite and survive.
    """

    window_seconds: int = Field(default=3600, ge=60)
    sends_per_account: int = Field(default=30, ge=0)
    sends_per_target: int = Field(default=10, ge=0)
    sends_per_fleet: int = Field(default=60, ge=0)
    joins_per_account: int = Field(default=3, ge=0)
    admin_ops_per_account: int = Field(default=10, ge=0)


class PlansConfig(BaseModel):
    ttl_seconds: int = Field(default=86400, ge=60)
    max_pending: int = Field(default=50, ge=1)
    #: Message bodies sit in this database until applied; encrypt them at rest.
    encrypt_bodies: bool = True


class LedgerConfig(BaseModel):
    """How long an outbound action is remembered, so it is not repeated.

    The window is the whole setting. The duplicate this catches is a *re-run* —
    a restarted process, a retried script, an agent in a fresh session with no
    memory of the last one — and those happen within hours. A message on a daily
    rhythm is a legitimate repeat, so six hours is chosen to sit well clear of
    one even with hours of drift: a longer window trades a rare catch for routine
    false refusals, and every false refusal teaches whoever hits it to set
    ``allow_duplicate`` by reflex, which is how the check stops working.

    Zero turns the check off. Said as a number rather than as a second boolean,
    because two switches that can disagree is one more state than this needs.
    """

    window_seconds: int = Field(default=6 * 60 * 60, ge=0)


class DownloadConfig(BaseModel):
    """Media lands in a server-chosen location, never a caller-chosen one."""

    max_file_bytes: int = Field(default=100 * 1024**2, ge=1)
    total_quota_bytes: int = Field(default=5 * 1024**3, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)


class UploadConfig(BaseModel):
    """A file leaves from a configured directory, never from anywhere on disk."""

    max_file_bytes: int = Field(default=100 * 1024**2, ge=1, le=TELEGRAM_MAX_UPLOAD_BYTES)
    #: Whether `media fetch`'s download directory may also be sent *from*. Off:
    #: a downloaded file is one a stranger chose, and re-posting one into another
    #: chat should be a decision an operator takes once, here, rather than one a
    #: tool call takes on its own.
    allow_downloads_dir: bool = False
    #: Uploading is not one round trip. The applier's per-RPC ceiling is seconds
    #: because every other write is a single request; a transfer needs minutes,
    #: and a timeout partway through lands the plan in `unknown_outcome`, which
    #: costs a person a look at the chat.
    timeout_seconds: int = Field(default=300, ge=1)


class McpConfig(BaseModel):
    """What the MCP surface publishes — a ceiling, never a source of tools.

    ``tools`` is the only key, and its default is the important part:

    **Unset means every tool, exactly as before.** This gate is opt-in. A
    fail-closed default here would be a different kind of mistake from the one
    the empty-``allow`` convention guards against — it would make a fresh
    install publish nothing, and the first thing anybody did about it would be
    to write a list they did not think about.

    **Set, it can only narrow.** The names are matched against the tools the
    registry already publishes; a name that is not one of them is a
    configuration error rather than a new tool, so nothing here can reach past
    the profile, the capability matrix or the hard denylist.

    **Set to an empty list, it publishes nothing** — the same reading every
    other allow list in this file gets, and the only one that does not make
    "empty because nobody filled it in" mean "everything".

    The point is narrow and worth stating: a prompt injection cannot invoke a
    tool it never saw in the tool list. That is a coarse second layer in front
    of the per-peer rules in :mod:`telegram_ai_cli.safety`, not a replacement
    for them.
    """

    tools: list[str] | None = None


class TranscribeConfig(BaseModel):
    """Speech-to-text, in a container, on this machine only.

    There is no API key here and no endpoint, because there is no remote
    service: a voice message is somebody's actual voice, and the decision was
    that it never leaves the host. The whole feature is an optional Docker
    image, so an installation that never builds it carries no extra dependency
    and sees no extra behaviour.

    The model is not a setting. It is baked into the image at ``small``, which
    is markedly better than ``base`` on Russian and still around half a
    gigabyte; making it configurable here would mean a cache of several models,
    a rule for choosing between them, and a manager for keeping them — a
    subsystem, in place of a feature that shells out to one container.
    """

    image: str = "telegram-ai-cli-transcribe:latest"
    #: Where the container reads the downloaded model from, so it is fetched
    #: once. Not under `paths.downloads`: that directory holds media strangers
    #: sent, and it is not a `paths.*` entry at all because nothing in the tool
    #: writes here — `make transcribe-model` does, and the operation mounts it
    #: read-only.
    model_cache: Path = Field(default_factory=lambda: _state_home() / "whisper-models")
    #: Longest audio accepted, in seconds. Ten minutes covers a voice message a
    #: person actually recorded by hand; past that it is a recording, and
    #: transcribing recordings is a batch job rather than a chat read.
    max_audio_seconds: int = Field(default=600, ge=1, le=7200)
    #: Wall clock for the whole container. Fifteen minutes, because the worst
    #: case it has to survive is ten minutes of audio on a loaded CPU at real
    #: time plus a cold start and a model load — a ceiling tight enough to be
    #: hit by a hung container is a ceiling that fails legitimate work.
    timeout_seconds: int = Field(default=900, ge=1)
    docker_binary: str = "docker"
    #: ``uid:gid`` the container runs as. ``None`` means this process's own,
    #: which is the only answer that keeps files written to the mounted cache
    #: owned by the user who ran the command. Root is refused outright.
    run_as: str | None = None


class AuditConfig(BaseModel):
    enabled: bool = True
    #: Off by default: the log records that a message was sent, not its text.
    #: An audit trail that mirrors every conversation is a second archive of
    #: the thing we are trying to protect.
    include_bodies: bool = False
    rotate_bytes: int = Field(default=64 * 1024**2, ge=1024)


class SecretsConfig(BaseModel):
    """Encryption at rest for api_hash and proxy credentials.

    The key is never a config value — only the name of the variable or the path
    of the file that holds it.
    """

    enabled: bool = True
    key_env: str = "TGAI_SECRET_KEY"
    key_file: Path | None = None
    auto_create_key: bool = True


class HTTPConfig(BaseModel):
    """The MCP server's HTTP transport. Loopback only, bearer token required.

    Neither of those is a default that can be turned off, and both are checked
    at start-up rather than warned about. A server that reads a personal
    Telegram account is not a thing to expose on a routable address, and "no
    auth on localhost" means every other process and every other user on the
    machine can read the account.

    The token is never a config value — only the name of the variable holding
    it — for the same reason as :class:`SecretsConfig`: a secret in a YAML file
    is a secret in a backup, in a bug report and in a screen share.
    """

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    #: The environment variable holding the bearer token. The *name* of a
    #: variable, which is the whole point of the field — never the token.
    token_env: str = "TGAI_HTTP_TOKEN"  # noqa: S105 - a variable name, not a secret
    #: Shortest token accepted. Not a strength estimate — a bound below which
    #: the value is plainly not a secret, refused rather than served.
    min_token_length: int = Field(default=16, ge=16)
    #: Where the Streamable HTTP endpoint is mounted.
    path: str = "/mcp"
    #: Drop a session that has gone this long without a request. The SDK's own
    #: default is "never", which keeps a transport and a task per session for
    #: the life of the process — and a session is created by any accepted
    #: request, so an abandoned client leaks one until a restart.
    session_idle_timeout_seconds: float = Field(default=1800.0, gt=0)


class DaemonConfig(BaseModel):
    """A local daemon that owns one account's client so callers can share it.

    Off by default, and opt-in for a reason: without it every caller opens the
    account itself and holds the auth key for the duration, which is simple and
    correct. The daemon is what makes two callers queue instead of the second
    one being refused with `SESSION_LOCKED` — worth having when a five-minute
    `watch` is running, unnecessary otherwise.

    Nothing here starts a daemon. A person runs `tg-ai daemon serve --account`;
    a client that finds no socket falls back to opening the account itself.
    """

    enabled: bool = False
    #: Shut down and remove the socket after this long with nothing to do. A
    #: daemon holds the auth key, so an abandoned one locks the account out of
    #: every other process until someone notices.
    idle_timeout_seconds: int = Field(default=300, ge=10)
    #: A socket on this machine answers at once or is not there.
    connect_timeout_seconds: float = Field(default=2.0, gt=0)
    #: How long to wait for an answer once the request has left. Generous: the
    #: work would otherwise be running in this process with no timeout at all.
    request_timeout_seconds: float = Field(default=900.0, gt=0)


class SafetyConfig(BaseModel):
    read: ReadPolicy = Field(default_factory=ReadPolicy)
    write: WritePolicy = Field(default_factory=WritePolicy)
    #: Which accounts are reachable at all. Empty = all registered accounts.
    accounts: PeerRule = Field(default_factory=PeerRule)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TGAI_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    #: Least privilege by default. `plan` additionally allows creating plans;
    #: nothing is ever sent without `tg-ai plan apply`.
    profile: Profile = "readonly"

    #: Fallback app credentials from https://my.telegram.org. A fingerprint
    #: frozen next to an account's session always wins over these.
    api_id: int | None = None
    api_hash: str | None = None

    paths: PathsConfig = Field(default_factory=PathsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    plans: PlansConfig = Field(default_factory=PlansConfig)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    http: HTTPConfig = Field(default_factory=HTTPConfig)

    #: Telethon retries and sleeps through flood waits on its own, which can
    #: resend a message we decided not to resend. We drive retries ourselves.
    telethon_flood_sleep_threshold: int = 0
    telethon_request_retries: int = 1

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Environment first, then the YAML file, then the defaults.

        ``load_settings`` hands the YAML in as init keyword arguments, and
        pydantic-settings ranks those *above* the environment by default — so
        without this reordering ``TGAI_PROFILE`` could not override a `profile:`
        line in the file, which is the opposite of what this project documents.

        The direction that matters is not the widening one. It is that an
        operator (or a container) must be able to *narrow* what the file grants:
        ``TGAI_PROFILE=readonly`` has to win over ``profile: plan`` on disk.
        """
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)


def load_settings(path: Path | None = None) -> Settings:
    """YAML provides the base, environment variables win.

    A section whose keys are all commented out parses as ``None``, which is the
    normal shape of an example config; those are dropped so they fall back to
    defaults instead of failing validation.
    """
    path = path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings(**{key: value for key, value in data.items() if value is not None})
