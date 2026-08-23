"""AES-256-GCM for the values that must not sit in the database in the clear.

What this protects: a copy of the state database or a stray backup. The key is
held outside it, so those copies are useless on their own.

What it does not protect: a reader who can already run as this user. They can
read the key the same way this process does. Saying so plainly matters, because
"encrypted at rest" is routinely heard as a stronger claim than it is.

Ciphertext is stored as ``enc:v1:<base64(nonce||ciphertext||tag)>``. The prefix
is what makes a migration possible: a value without it is plaintext from an
older run and can be re-encrypted in place instead of failing to decrypt.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import SecretsConfig
from .errors import InsecurePermissions, TelegramAIError

PREFIX = "enc:v1:"
_NONCE_BYTES = 12
_KEY_BYTES = 32


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)  # type: ignore[union-attr]


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise TelegramAIError(f"secret key must be {_KEY_BYTES} bytes, got {len(key)}")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        blob = nonce + self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return PREFIX + base64.b64encode(blob).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not is_encrypted(value):
            # Not an error: a value written before encryption was switched on.
            return value
        blob = base64.b64decode(value[len(PREFIX) :])
        nonce, payload = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, payload, None).decode("utf-8")
        except InvalidTag as exc:
            raise TelegramAIError(
                "could not decrypt a stored secret: wrong key, or the value was altered",
                suggestion="Check that TGAI_SECRET_KEY matches the key used to write this data.",
            ) from exc


def load_key(config: SecretsConfig, *, state_dir: Path) -> bytes | None:
    """Find the key, or return ``None`` when encryption is off.

    Environment first, then the key file. A key file is created only when
    ``auto_create_key`` is set — a deployment that manages keys elsewhere must
    not get a silently generated one it will never back up.
    """
    if not config.enabled:
        return None

    if raw := os.environ.get(config.key_env):
        return _decode_key(raw)

    path = config.key_file or (state_dir / "secret.key")
    if path.exists():
        _require_private(path)
        return _decode_key(path.read_text(encoding="utf-8").strip())

    if not config.auto_create_key:
        raise TelegramAIError(
            f"secrets are enabled but no key was found in ${config.key_env} or {path}",
            suggestion="Provide the key, or set secrets.auto_create_key to generate one.",
        )

    key = secrets.token_bytes(_KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # O_EXCL so a concurrent start cannot end with two processes each believing
    # they created the key — the loser would encrypt with a key the winner's
    # data cannot be read with.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, base64.b64encode(key))
        os.fsync(fd)
    finally:
        os.close(fd)
    return key


def _decode_key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - any decode failure means the same thing
        key = raw.encode("utf-8")
    if len(key) != _KEY_BYTES:
        raise TelegramAIError(
            f"secret key must decode to {_KEY_BYTES} bytes, got {len(key)}",
            suggestion="Generate one with: python -c "
            "'import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())'",
        )
    return key


def _require_private(path: Path) -> None:
    """Refuse to use a key file others can read.

    Fail closed. Warning and continuing would leave the key exposed and the run
    successful, which is the outcome this check exists to prevent.
    """
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise InsecurePermissions(
            f"{path} is mode {mode:o}; it must not be readable by group or others",
            suggestion=f"chmod 600 {path}",
        )
