"""The account fleet: storage, session material, locking, login and loading.

Nothing here imports Telethon or opentele at module scope. Labels, proxies, the
registry and the store all have to work in a test suite and on a machine that
has never installed an MTProto stack; the imports that need one live inside the
functions that connect.

The layout, in dependency order:

``fs`` / ``paths`` / ``lock``
    Private files, where an account's artefacts live, and the ``flock`` that
    keeps one auth key attached to one client.
``proxy`` / ``models`` / ``runtime``
    Proxy URL parsing plus redaction, the source/status vocabulary, and the few
    facts about Telethon and opentele that are looked up at call time.
``store`` / ``views``
    The ``accounts`` table with the encryption boundary around its secrets, and
    the read model that is safe to print.
``api_profile`` / ``spec`` / ``sources`` / ``loader``
    The frozen device fingerprint, one decrypted account plus its connected
    client, one builder per kind of material, and the connect-and-verify path
    that ties them together.
``discovery`` / ``login`` / ``registry`` / ``bulk``
    Recognising material on disk, interactive sign-in, everything an operator
    does to one account, and the same over a whole backup tree.
"""

from __future__ import annotations

from .api_profile import load_api_profile, resolve_api, save_api_profile
from .bulk import ImportResult, import_session_files, import_tdata_batch
from .discovery import discover_tdata_dirs, is_sqlite, looks_like_tdata
from .fs import (
    ensure_private_dir,
    harden_path,
    read_private_text,
    require_no_symlink,
    require_private,
    write_private_text,
)
from .loader import build_client, load_account
from .lock import SessionLock, SessionLocks
from .login import (
    LoginResult,
    interactive_login,
    login_and_register,
    qr_login,
    qr_login_and_register,
)
from .models import USABLE_STATUSES, AccountSource, AccountStatus
from .paths import (
    SessionPaths,
    auth_key_id,
    auth_key_ids,
    sanitize_label,
    session_lock_path,
    session_lock_paths,
)
from .proxy import mask_secret, parse_proxy_url, redact_proxy_url, redact_secrets
from .registry import AccountRegistry
from .runtime import recoverable_errors, revoked_errors, telethon_options
from .sources import USE_CURRENT_SESSION_WARNING
from .spec import AccountSpec, LoadedClient, SessionFlag
from .store import AccountRecord, AccountStore
from .views import AccountView

__all__ = [
    "USABLE_STATUSES",
    "USE_CURRENT_SESSION_WARNING",
    "AccountRecord",
    "AccountRegistry",
    "AccountSource",
    "AccountSpec",
    "AccountStatus",
    "AccountStore",
    "AccountView",
    "ImportResult",
    "LoadedClient",
    "LoginResult",
    "SessionFlag",
    "SessionLock",
    "SessionLocks",
    "SessionPaths",
    "auth_key_id",
    "auth_key_ids",
    "build_client",
    "discover_tdata_dirs",
    "ensure_private_dir",
    "harden_path",
    "import_session_files",
    "import_tdata_batch",
    "interactive_login",
    "is_sqlite",
    "load_account",
    "load_api_profile",
    "login_and_register",
    "looks_like_tdata",
    "mask_secret",
    "parse_proxy_url",
    "qr_login",
    "qr_login_and_register",
    "read_private_text",
    "recoverable_errors",
    "redact_proxy_url",
    "redact_secrets",
    "require_no_symlink",
    "require_private",
    "resolve_api",
    "revoked_errors",
    "sanitize_label",
    "save_api_profile",
    "session_lock_path",
    "session_lock_paths",
    "telethon_options",
    "write_private_text",
]
