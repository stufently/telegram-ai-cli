"""What an account looks like once it is safe to print.

:class:`AccountView` is the read model the CLI and the MCP server render. It is
built by dropping or masking everything that would be dangerous in output: no
``api_hash``, only a flag saying whether one is set; a proxy with its password
replaced; and a ``session_path`` that is checked in case someone stored the auth
key itself in that column.

The alternative — handing out records and trusting each call site to redact — is
the arrangement where one new print statement leaks a credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ..errors import TelegramAIError
from ..secretbox import is_encrypted
from .models import AccountStatus
from .proxy import mask_secret, redact_proxy_url

if TYPE_CHECKING:  # pragma: no cover
    from .store import AccountRecord, AccountStore

#: Shown where a proxy would be, when the reader holds no decryption key.
ENCRYPTED_PLACEHOLDER: Final = "<encrypted>"

#: Shorter than this, a value in ``session_path`` cannot be a session string.
MIN_STRING_SESSION_LEN: Final = 40


@dataclass(frozen=True, slots=True)
class AccountView:
    """Read model for the CLI and the MCP server. Deliberately free of secrets."""

    label: str
    source: str
    status: str
    phone: str | None = None
    user_id: int | None = None
    api_id: int | None = None
    has_api_hash: bool = False
    proxy: str = "<none>"
    has_proxy: bool = False
    session_path: str | None = None
    last_error: str | None = None
    created_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.status != str(AccountStatus.DISABLED)

    @classmethod
    def from_record(cls, record: AccountRecord, store: AccountStore | None = None) -> AccountView:
        """``store`` only affects how the proxy is *displayed*.

        Without a usable key the stored value is ciphertext, which would render
        as a wall of base64 where a host and port belong. This is a listing, so
        it says ``<encrypted>`` and moves on; refusing to *use* an unreadable
        secret is the loader's job, and it does refuse.
        """
        proxy_url = record.proxy_url
        if is_encrypted(proxy_url):
            try:
                proxy_url = store.reveal(proxy_url) if store else None
            except TelegramAIError:
                proxy_url = None
            proxy_url = proxy_url or ENCRYPTED_PLACEHOLDER
        return cls(
            label=record.label,
            source=str(record.source),
            status=str(record.status),
            phone=record.phone,
            user_id=record.user_id,
            api_id=record.api_id,
            has_api_hash=bool(record.api_hash),
            proxy=redact_proxy_url(proxy_url),
            has_proxy=bool(proxy_url),
            session_path=safe_session_path(record.session_path),
            last_error=record.last_error,
            created_at=record.created_at,
        )


def safe_session_path(value: str | None) -> str | None:
    """Never let a raw session string reach a view.

    ``session_path`` normally holds a path, but a hand-edited database may carry
    the ``StringSession`` itself — which *is* the auth key, and this view gets
    printed.
    """
    if not value:
        return None
    text = str(value)
    looks_like_a_path = "/" in text or text.endswith((".session", ".string"))
    if looks_like_a_path or len(text) < MIN_STRING_SESSION_LEN:
        return text
    return mask_secret(text)
