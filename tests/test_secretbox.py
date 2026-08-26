"""Encryption at rest for the values that must not sit in the clear.

What it protects is a copy of the database or a stray backup. What it does not
protect is anyone who can already run as this user — they read the key the same
way this process does. The tests keep to the first claim.

Two behaviours matter as much as the round-trip: a wrong key must produce an
explanation rather than a traceback out of the crypto library, and a key file
that other people can read must be refused rather than warned about.
"""

from __future__ import annotations

import base64
import os
import secrets
import stat
from pathlib import Path

import pytest

from telegram_ai_cli_mcp.config import SecretsConfig
from telegram_ai_cli_mcp.errors import ErrorCode, InsecurePermissions, TelegramAIError
from telegram_ai_cli_mcp.secretbox import PREFIX, SecretBox, is_encrypted, load_key

SAMPLE_VALUE = "an api_hash nobody else should read"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def key() -> bytes:
    return secrets.token_bytes(32)


# --- the box ---------------------------------------------------------------


def test_round_trip(key: bytes) -> None:
    box = SecretBox(key)
    assert box.decrypt(box.encrypt(SAMPLE_VALUE)) == SAMPLE_VALUE


def test_ciphertext_is_tagged_and_does_not_contain_the_plaintext(key: bytes) -> None:
    ciphertext = SecretBox(key).encrypt(SAMPLE_VALUE)
    assert ciphertext.startswith(PREFIX)
    assert SAMPLE_VALUE not in ciphertext
    assert is_encrypted(ciphertext)


def test_the_same_value_encrypts_differently_every_time(key: bytes) -> None:
    """A fresh nonce per call: equal ciphertexts would leak equal secrets."""
    box = SecretBox(key)
    assert box.encrypt(SAMPLE_VALUE) != box.encrypt(SAMPLE_VALUE)


def test_unicode_and_empty_values_survive(key: bytes) -> None:
    box = SecretBox(key)
    for value in ["", "ключ 🎉", "a" * 5000]:
        assert box.decrypt(box.encrypt(value)) == value


def test_another_key_is_refused_with_an_explanation(key: bytes) -> None:
    """Not an InvalidTag traceback: this is a configuration mistake, not a crash."""
    ciphertext = SecretBox(key).encrypt(SAMPLE_VALUE)
    with pytest.raises(TelegramAIError) as failure:
        SecretBox(secrets.token_bytes(32)).decrypt(ciphertext)
    assert "wrong key" in failure.value.message
    assert failure.value.suggestion


def test_a_tampered_value_is_refused(key: bytes) -> None:
    box = SecretBox(key)
    blob = base64.b64decode(box.encrypt(SAMPLE_VALUE)[len(PREFIX) :])
    tampered = PREFIX + base64.b64encode(blob[:-1] + bytes([blob[-1] ^ 0x01])).decode("ascii")
    with pytest.raises(TelegramAIError):
        box.decrypt(tampered)


def test_a_value_without_the_prefix_is_returned_as_it_is(key: bytes) -> None:
    """Plaintext from a run before encryption was switched on, not an error.

    Failing here would make the migration impossible: the value could never be
    read in order to be re-encrypted.
    """
    assert SecretBox(key).decrypt("plain-api-hash") == "plain-api-hash"
    assert is_encrypted("plain-api-hash") is False
    assert is_encrypted("") is False
    assert is_encrypted(None) is False


def test_a_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(TelegramAIError, match="32 bytes"):
        SecretBox(b"too-short")


# --- finding the key -------------------------------------------------------


def test_the_environment_variable_wins(tmp_path: Path, key: bytes, monkeypatch) -> None:
    monkeypatch.setenv("TGAI_SECRET_KEY", base64.b64encode(key).decode("ascii"))
    file_key = tmp_path / "secret.key"
    file_key.write_text(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
    file_key.chmod(0o600)

    assert load_key(SecretsConfig(key_file=file_key), state_dir=tmp_path) == key


def test_a_key_file_is_read_when_the_environment_is_empty(tmp_path: Path, key: bytes) -> None:
    path = tmp_path / "secret.key"
    path.write_text(base64.b64encode(key).decode("ascii"))
    path.chmod(0o600)

    assert load_key(SecretsConfig(key_file=path), state_dir=tmp_path) == key


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_a_key_file_others_can_read_is_refused(tmp_path: Path, key: bytes, mode: int) -> None:
    """Fail closed: a warning would leave the key exposed and the run successful."""
    path = tmp_path / "secret.key"
    path.write_text(base64.b64encode(key).decode("ascii"))
    path.chmod(mode)

    with pytest.raises(InsecurePermissions) as refusal:
        load_key(SecretsConfig(key_file=path), state_dir=tmp_path)
    assert refusal.value.code is ErrorCode.INSECURE_PERMISSIONS
    assert "chmod 600" in (refusal.value.suggestion or "")


def test_a_generated_key_file_is_private(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    generated = load_key(SecretsConfig(), state_dir=state)

    path = state / "secret.key"
    assert generated is not None
    assert len(generated) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # And it is stable: a second call must not mint a different key.
    assert load_key(SecretsConfig(), state_dir=state) == generated


def test_a_deployment_that_manages_keys_elsewhere_is_not_given_one(tmp_path: Path) -> None:
    """A silently generated key is one nobody backs up."""
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(TelegramAIError, match="no key was found"):
        load_key(SecretsConfig(auto_create_key=False), state_dir=state)
    assert not (state / "secret.key").exists()


def test_disabling_secrets_returns_no_key(tmp_path: Path) -> None:
    assert load_key(SecretsConfig(enabled=False), state_dir=tmp_path) is None


def test_a_key_of_the_wrong_size_in_the_environment_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TGAI_SECRET_KEY", base64.b64encode(b"short").decode("ascii"))
    with pytest.raises(TelegramAIError, match="32 bytes"):
        load_key(SecretsConfig(), state_dir=tmp_path)


def test_a_raw_thirty_two_byte_key_is_accepted(monkeypatch, tmp_path) -> None:
    """Not everything in an environment variable is base64.

    The value below cannot be base64 (``!`` is outside the alphabet), so it is
    taken as raw bytes. A 32-character value that *is* valid base64 decodes to
    24 bytes and is rejected — which is the documented way to supply a key.
    """
    raw = "k" * 31 + "!"
    monkeypatch.setenv("TGAI_SECRET_KEY", raw)
    assert load_key(SecretsConfig(), state_dir=tmp_path) == raw.encode("utf-8")


def test_the_key_variable_can_be_renamed(monkeypatch, tmp_path, key: bytes) -> None:
    monkeypatch.setenv("TGAI_OTHER_KEY", base64.b64encode(key).decode("ascii"))
    assert load_key(SecretsConfig(key_env="TGAI_OTHER_KEY"), state_dir=tmp_path) == key
