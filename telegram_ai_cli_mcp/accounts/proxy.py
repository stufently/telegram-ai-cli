"""Proxy URLs, and the masking that keeps their passwords out of everything else.

The parser is strict on purpose. A proxy that silently fails to apply is worse
than no proxy at all: the account connects anyway, from the host's own address,
and the operator finds out when the whole fleet is banned together. So a URL is
either understood completely or rejected — never partially honoured.

Nothing in this module puts a password into a message it raises or returns.
Exception text ends up in ``accounts.last_error``, which is rendered verbatim by
whatever displays the account list.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from ..errors import InvalidInput

log = logging.getLogger(__name__)

#: Below this length a masked secret shows no tail at all — a "hint" of a
#: six-character value is most of the value.
MIN_MASK_TAIL_LEN: Final = 12

#: URL scheme -> the protocol name Telethon's ``_parse_proxy`` understands.
#: ``socks5h``/``socks4a`` are the "resolve DNS at the proxy" spellings; we ask
#: for that unconditionally via ``rdns``, so they collapse onto the base type.
_SCHEMES: Final[Mapping[str, str]] = {
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4",
    "http": "http",
    "https": "http",
}

NO_PROXY_WARNING: Final = (
    "account %s has no proxy: every account then shares this host's egress IP, "
    "and a fleet behind one address is the fastest way to lose all of it at once"
)


def mask_secret(value: object | None, *, keep: int = 0) -> str:
    """Render a secret safe for logs; only its length survives by default.

    ``keep`` may expose a short tail so two log lines can be correlated, but it
    is capped at a quarter of the value and suppressed entirely for short ones.
    """
    if value is None:
        return "<unset>"
    text = str(value)
    if not text:
        return "<empty>"
    keep = 0 if len(text) < MIN_MASK_TAIL_LEN else max(0, min(keep, len(text) // 4))
    tail = text[-keep:] if keep else ""
    return f"***{tail}(len={len(text)})"


def redact_proxy_url(url: str | None) -> str:
    """``socks5://user:hunter2@host:1080`` -> ``socks5://user:***@host:1080``.

    The username stays readable because it is routinely what tells two proxy
    pools apart; the password never is.
    """
    if not url:
        return "<none>"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparsable-proxy-url>"
    if not parts.password:
        return url
    user = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{parts.scheme}://{user}:***@{host}{port}"


def redact_secrets(text: str, *secrets: str | None) -> str:
    """Blank out literal secrets in free-form text.

    Exception messages are the leak nobody plans for. Anything that could carry
    an ``api_hash``, a proxy password or a session string passes through here
    before it is stored or displayed.
    """
    out = str(text)
    for secret in secrets:
        if secret and len(str(secret)) >= 6:
            out = out.replace(str(secret), "***")
    return out


def proxy_password(url: str | None) -> str | None:
    """The password inside a proxy URL, for redaction lists."""
    if not url:
        return None
    try:
        return urlsplit(url).password
    except ValueError:
        return None


def parse_proxy_url(url: str | None) -> dict[str, Any] | None:
    """Parse ``socks5://user:pass@host:1080`` into Telethon's ``proxy`` kwarg.

    Telethon feeds the dict straight into ``_parse_proxy(**proxy)``, so these
    keys are the contract. ``rdns`` is always on: resolving Telegram's hostnames
    through the local resolver instead of the proxy would publish the account's
    activity to whoever runs that resolver.

    Returns ``None`` when no proxy is configured.
    """
    if url is None or not url.strip():
        return None
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise InvalidInput(f"malformed proxy URL ({exc})") from None

    supported = ", ".join(sorted(_SCHEMES))
    if not parts.scheme:
        raise InvalidInput(
            f"proxy URL needs a scheme, e.g. socks5://host:1080 (supported: {supported})"
        )
    scheme = parts.scheme.lower()
    if scheme not in _SCHEMES:
        raise InvalidInput(f"unsupported proxy scheme {scheme!r} (supported: {supported})")
    if not host:
        raise InvalidInput(f"proxy URL {scheme}://... has no host")
    if port is None:
        raise InvalidInput(f"proxy URL {scheme}://{host} has no port")
    if not 1 <= port <= 65535:
        raise InvalidInput(f"proxy port {port} out of range")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise InvalidInput("proxy URL must be scheme://[user:pass@]host:port with no path or query")

    if scheme == "https":
        # python-socks speaks plain CONNECT to an HTTP proxy; there is no TLS
        # between us and it. Accepting the spelling but naming the gap beats
        # letting a config read as though the first hop were encrypted.
        log.warning(
            "proxy %s is used as a plain HTTP CONNECT proxy: the hop to the proxy "
            "itself is NOT TLS-encrypted",
            redact_proxy_url(raw),
        )

    proxy: dict[str, Any] = {
        "proxy_type": _SCHEMES[scheme],
        "addr": host,
        "port": port,
        "rdns": True,
    }
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


def validate_proxy_url(url: str | None) -> str | None:
    """Normalise and validate for storage; ``None`` means "no proxy"."""
    if url is None or not url.strip():
        return None
    parse_proxy_url(url)  # raises InvalidInput on anything unusable
    return url.strip()
