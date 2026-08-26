"""The device fingerprint an account must present forever.

Telegram sees ``api_id`` plus a set of device strings on every reconnect. A
fingerprint that changes between restarts looks exactly like a session someone
stole and is replaying from their own machine — which is a good way to have the
session killed. So it is generated once, deterministically from the label, and
then frozen in ``<label>.api.json`` next to the session.

The stored ``api_hash`` is encrypted. The file it lives in is ``0600``, but that
is the same protection the session file has, and the whole point of encrypting
secrets at rest is that a stray copy — a backup, a synced folder, a container
layer — is useless without the key. A ``0600`` file full of plaintext
credentials next to an encrypted database is a promise the deployment does not
actually keep.

``opentele`` is imported inside the functions that need it: reading and writing
a profile must work in a test suite that has no MTProto stack installed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ..secretbox import SecretBox
from .fs import read_private_text, write_private_text
from .proxy import mask_secret

if TYPE_CHECKING:  # pragma: no cover - only type checkers pay for this
    from opentele.api import APIData

log = logging.getLogger(__name__)

API_FIELDS: Final = (
    "api_id",
    "api_hash",
    "device_model",
    "system_version",
    "app_version",
    "lang_code",
    "system_lang_code",
    "lang_pack",
)


def save_api_profile(path: Path, api: APIData, box: SecretBox | None = None) -> Path:
    """Persist the fingerprint, ``0600``, with ``api_hash`` encrypted."""
    payload: dict[str, Any] = {field: getattr(api, field, None) for field in API_FIELDS}
    if box is not None and payload.get("api_hash"):
        payload["api_hash"] = box.encrypt(str(payload["api_hash"]))
    return write_private_text(path, json.dumps(payload, indent=2, sort_keys=True))


def load_api_profile(path: Path, box: SecretBox | None = None) -> APIData | None:
    """Rebuild a stored fingerprint, or ``None`` if there is none.

    A profile that cannot be parsed is ignored rather than fatal — it is
    regenerated from the label and lands back on disk. A profile whose
    ``api_hash`` cannot be *decrypted* is a different matter and propagates:
    silently generating a new fingerprint there would change what Telegram sees
    for an account that has been connecting with the old one for months.
    """
    if not path.exists():
        return None
    from opentele.api import APIData as _APIData

    try:
        payload = json.loads(read_private_text(path))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable api profile %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        log.warning("ignoring api profile %s: not an object", path)
        return None

    api_hash = payload.get("api_hash")
    if api_hash and box is not None:
        api_hash = box.decrypt(str(api_hash))
    if not payload.get("api_id") or not api_hash:
        return None

    fields = {field: payload.get(field) for field in API_FIELDS}
    fields["api_hash"] = api_hash
    return _APIData(**fields)


def resolve_api(
    *,
    label: str,
    api_file: Path,
    api_id: int | None = None,
    api_hash: str | None = None,
    box: SecretBox | None = None,
) -> APIData:
    """Return this account's frozen fingerprint, generating it on first use.

    Operator-supplied credentials win over the generated ``api_id``/``api_hash``
    pair, but the generated device strings are kept: they are what make the
    client look like a plausible Telegram Desktop install rather than a script.
    """
    stored = load_api_profile(api_file, box)
    if stored is not None:
        return stored

    from opentele.api import API

    api = API.TelegramDesktop.Generate(unique_id=label)
    if api_id and api_hash:
        api.api_id = int(api_id)
        api.api_hash = str(api_hash)
    save_api_profile(api_file, api, box)
    log.info(
        "generated api fingerprint for %s: api_id=%s device=%r hash=%s",
        label,
        api.api_id,
        api.device_model,
        mask_secret(api.api_hash),
    )
    return api
