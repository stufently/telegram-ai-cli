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
from pydantic import BaseModel, Field
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


def _state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "telegram-ai-cli"


class PathsConfig(BaseModel):
    sessions: Path = Field(default_factory=lambda: _state_home() / "sessions")
    state: Path = Field(default_factory=_state_home)
    downloads: Path = Field(default_factory=lambda: _state_home() / "downloads")
    audit_log: Path = Field(default_factory=lambda: _state_home() / "audit.jsonl")


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


class DownloadConfig(BaseModel):
    """Media lands in a server-chosen location, never a caller-chosen one."""

    max_file_bytes: int = Field(default=100 * 1024**2, ge=1)
    total_quota_bytes: int = Field(default=5 * 1024**3, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)


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
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

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
