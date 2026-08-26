"""What the redactor must catch, and what it must leave alone.

Redaction is the second line of defence — the read allowlist is the first — but
the Telegram-specific values are the ones whose leak is unrecoverable: a seed
phrase, a wallet address, the login code Telegram itself sends. Those get the
most attention here.

The other half of the job is restraint. A redactor that eats order numbers and
message ids makes the output useless for the work it was fetched for, and a
useless redactor is a redactor somebody switches off.

Every value below is invented. Phone numbers are assembled from pieces on
purpose, so that the repository scan in ``test_no_private_data.py`` stays a real
check rather than one with an exception carved out for this file.
"""

from __future__ import annotations

import pytest

from telegram_ai_cli_mcp.redact import redact, redact_mapping

# Fictional numbers, written in parts so no E.164-shaped literal exists in the
# repository. The +1-555-01xx range is reserved for fiction.
FAKE_E164 = "+" + "15550100999"
FAKE_LOCAL = "8 " + "(999) 555-01-99"

# A Luhn-valid test card number published for exactly this purpose.
TEST_CARD = "4111 1111 1111 1111"

SEED_12 = "abandon ability able about above absent absorb abstract absurd abuse access accident"
SEED_24 = SEED_12 + " " + SEED_12

TON_ADDRESS = "EQ" + "A" * 46
EVM_ADDRESS = "0x" + "a1b2c3d4e5" * 4


def masked(kind: str) -> str:
    return f"[redacted:{kind}]"


# --- phone numbers ---------------------------------------------------------


@pytest.mark.parametrize("number", [FAKE_E164, FAKE_LOCAL])
def test_phone_numbers_are_masked(number: str) -> None:
    out = redact(f"call me on {number} tomorrow")
    assert number not in out
    assert masked("phone") in out


def test_short_number_runs_are_left_alone() -> None:
    """Quantities and short ids are not phone numbers."""
    assert redact("order 12345678 has 350 items") == "order 12345678 has 350 items"


def test_a_negative_telegram_peer_id_is_not_mistaken_for_a_phone() -> None:
    peer_id = -1001234567890
    assert redact(f"peer {peer_id} was refused") == f"peer {peer_id} was refused"


# --- email -----------------------------------------------------------------


@pytest.mark.parametrize("address", ["alice@example.com", "first.last+tag@mail.example.org"])
def test_email_addresses_are_masked(address: str) -> None:
    out = redact(f"write to {address} please")
    assert address not in out
    assert "[redacted:" in out


def test_a_username_is_not_mistaken_for_an_email() -> None:
    assert redact("ask @someone about it") == "ask @someone about it"


def test_an_address_with_a_numeric_local_part_is_masked_whole() -> None:
    """Pins the rule order: email must be masked before the digit rules.

    The phone rule matches a long digit run, so if it ran first this address
    came back as ``[redacted:phone]@domain.com`` — leaking the domain and
    naming the wrong kind of secret. Asserting only that *something* was
    redacted would not catch that, so assert the kind and the domain.
    """
    out = redact("write to 79991234567@domain.com please")
    assert out == "write to [redacted:email] please"


def test_a_deep_subdomain_is_masked_to_the_end() -> None:
    """A domain of any depth must go whole.

    With the domain limited to two labels this returned
    ``[redacted:email].example.com``, leaking the employer. Assert equality:
    checking only that the full address vanished passes either way.
    """
    out = redact("write to alice@dept.internal.example.com please")
    assert out == "write to [redacted:email] please"


# --- cards -----------------------------------------------------------------


def test_a_card_number_is_masked() -> None:
    out = redact(f"pay to {TEST_CARD} today")
    assert masked("card") in out
    assert "4111" not in out


def test_a_long_number_that_fails_the_luhn_check_is_not_called_a_card() -> None:
    """Order numbers and message ids are long digit runs too.

    The structural check is what keeps them out of the card rule; without it
    the redactor eats the values the output was fetched for.
    """
    out = redact("order 1234567890123456 shipped")
    assert masked("card") not in out


def test_card_digits_survive_when_they_are_plainly_an_identifier() -> None:
    assert redact("invoice 4590 for 12 units") == "invoice 4590 for 12 units"


# --- crypto ----------------------------------------------------------------


def test_seed_phrase_of_twelve_words_is_masked() -> None:
    out = redact(f"my seed is {SEED_12} keep it safe")
    assert masked("seed-phrase") in out
    for word in SEED_12.split():
        assert f" {word} " not in out


def test_seed_phrase_of_twenty_four_words_is_masked_whole() -> None:
    """Half a mnemonic left in the output is still most of the secret."""
    out = redact(SEED_24)
    assert out == masked("seed-phrase")


def test_an_ordinary_sentence_is_not_taken_for_a_mnemonic() -> None:
    sentence = "the quick brown fox jumps over the lazy dog again and"
    assert redact(sentence) == sentence


@pytest.mark.parametrize("prefix", ["EQ", "UQ"])
def test_ton_addresses_are_masked(prefix: str) -> None:
    address = prefix + "A" * 46
    out = redact(f"send it to {address} now")
    assert address not in out
    assert masked("ton-address") in out


def test_evm_addresses_are_masked() -> None:
    out = redact(f"deposit to {EVM_ADDRESS} now")
    assert EVM_ADDRESS not in out
    assert masked("evm-address") in out


def test_a_transaction_hash_is_not_an_evm_address() -> None:
    """Sixty-four hex digits is a hash; masking it hides nothing worth hiding."""
    tx = "0x" + "b" * 64
    assert redact(f"tx {tx} confirmed") == f"tx {tx} confirmed"


# --- one-time codes and tokens --------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Login code: 51439. Do not give this code to anyone.", "51439"),
        ("Your verification code is 902144", "902144"),
        ("Код для входа: 51439", "51439"),
    ],
)
def test_login_codes_are_masked(text: str, code: str) -> None:
    """Telegram's own service messages are the highest-value text there is."""
    out = redact(text)
    assert masked("otp") in out
    assert code not in out


@pytest.mark.parametrize(
    "text",
    [
        "api_token: abcdefghij1234567890",
        "bot token 1234567890:AAHkQwErTyUiOpAsDfGhJkLzXcVbNm",
        "secret key = s3cr3t_value_that_is_long",
    ],
)
def test_tokens_are_masked(text: str) -> None:
    assert masked("token") in redact(text)


# --- structures ------------------------------------------------------------


def test_redact_mapping_walks_dicts_and_lists() -> None:
    """Redacting at assembly is how a newly added field is covered by default."""
    payload = {
        "messages": [
            {"id": 1, "text": f"call {FAKE_E164}"},
            {"id": 2, "text": f"card {TEST_CARD}"},
        ],
        "meta": {"note": f"wallet {EVM_ADDRESS}", "count": 2, "flag": True},
    }
    out = redact_mapping(payload)

    assert out["messages"][0]["text"] == f"call {masked('phone')}"
    assert masked("card") in out["messages"][1]["text"]
    assert masked("evm-address") in out["meta"]["note"]
    # Non-string leaves keep their type: this is not a stringifier.
    assert out["meta"]["count"] == 2
    assert out["meta"]["flag"] is True
    assert out["messages"][0]["id"] == 1


def test_redact_mapping_leaves_the_input_untouched() -> None:
    payload = {"text": f"call {FAKE_E164}"}
    redact_mapping(payload)
    assert payload["text"] == f"call {FAKE_E164}"


def test_empty_values_are_returned_as_they_are() -> None:
    assert redact("") == ""
    assert redact_mapping({}) == {}
    assert redact_mapping([]) == []
    assert redact_mapping(None) is None
