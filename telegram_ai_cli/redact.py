"""Remove the obviously sensitive parts of Telegram text before it leaves.

Scope, stated plainly because overestimating this is dangerous: redaction is a
second line of defence, not the main one. It catches values that are recognisable
by shape — a card number, a seed phrase, a login code. It cannot make a
conversation non-personal, because names, usernames and the sentences themselves
carry identity and no regular expression is going to find that. The control that
actually limits exposure is the read allowlist in :mod:`telegram_ai_cli.safety`;
this module reduces the damage of what does get read.

The Telegram-specific patterns matter more than the generic ones. An account is
a wallet and a second factor: seed phrases, TON and EVM addresses, and the login
codes Telegram itself sends are the values whose leak is unrecoverable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MASK = "[redacted:{kind}]"


@dataclass(frozen=True, slots=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    #: Called with the match; return False to keep the text as-is.
    verify: object = None


def _luhn(digits: str) -> bool:
    """Cheap structural check so ordinary long numbers survive.

    Order numbers, message ids and timestamps are long digit runs too. Without
    this, redaction eats them and the output becomes useless for the work it
    was fetched for.
    """
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# The domain takes any number of labels. Matching only two meant
# `alice@dept.internal.example.com` was masked as `[redacted:email].example.com`
# — the tail of the domain survived in the clear.
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")

# E.164 and the common written forms. Eight digits is the floor: shorter runs
# are far more often quantities than phone numbers.
_PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{7,20}\d(?![\w])")

_CARD = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")

# TON: EQ/UQ (bounceable/non-bounceable) plus 46 base64url characters.
_TON = re.compile(r"\b[EU]Q[A-Za-z0-9_-]{46}\b")

# EVM: 0x plus exactly 40 hex digits. Longer runs are hashes, not addresses.
_EVM = re.compile(r"\b0x[0-9a-fA-F]{40}\b(?![0-9a-fA-F])")

# BIP39 mnemonics are 12 or 24 short lowercase words. Matching the wordlist
# would be exact, but shipping 2048 words to catch a value that must never be
# read anyway is not worth it — the shape alone is distinctive.
_SEED = re.compile(r"\b(?:[a-z]{3,8}\s+){11}[a-z]{3,8}\b(?:(?:\s+[a-z]{3,8}){12})?")

# What Telegram's own service messages look like.
_LOGIN_CODE = re.compile(
    r"(?i)\b(?:login\s*code|код\s*(?:для\s*)?вход\w*|verification\s*code|"
    r"confirmation\s*code)\b\D{0,20}(\d{4,8})\b"
)

_API_TOKEN = re.compile(
    r"(?i)\b(?:bot|api|access|bearer|secret)[\s_-]*(?:token|key)\b\W{0,4}([A-Za-z0-9_:.-]{16,})"
)


def _mask(kind: str) -> str:
    return MASK.format(kind=kind)


def redact(text: str) -> str:
    """Return *text* with recognisable secrets replaced by typed placeholders.

    Placeholders name the kind rather than blanking the value, so a reader can
    tell that a phone number was present without learning which one.
    """
    if not text:
        return text

    # Highest-value first: a seed phrase overlaps with nothing else, and a card
    # number can otherwise be partly eaten by the phone rule.
    text = _SEED.sub(_mask("seed-phrase"), text)
    text = _TON.sub(_mask("ton-address"), text)
    text = _EVM.sub(_mask("evm-address"), text)
    text = _LOGIN_CODE.sub(lambda m: m.group(0).replace(m.group(1), _mask("otp")), text)
    text = _API_TOKEN.sub(lambda m: m.group(0).replace(m.group(1), _mask("token")), text)
    # Before the digit rules: an address whose local part is a long number would
    # otherwise come back as a masked phone inside an intact domain, which leaks
    # the domain and misnames what was found.
    text = _EMAIL.sub(_mask("email"), text)

    def _card_sub(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return _mask("card") if _luhn(digits) else match.group(0)

    text = _CARD.sub(_card_sub, text)

    def _phone_sub(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        # Below nine digits it is more likely an id or an amount than a number
        # someone can be reached on.
        return _mask("phone") if len(digits) >= 9 else match.group(0)

    return _PHONE.sub(_phone_sub, text)


def redact_mapping(value: object) -> object:
    """Walk a JSON-shaped structure, redacting every string in it.

    Used on whole tool results: redacting at the point of assembly is how a
    newly added field ends up covered by default instead of being remembered.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    return value
