"""
Deterministic NOID helpers.
"""

import string

ALPHABET = string.digits + "bcdfghjkmnpqrstvwxz"  # 29 chars, no vowels
BASE = len(ALPHABET)


def encode_counter(counter: int, min_length: int = 7) -> str:
    """
    Encode integer counter into base-29 string.

    Args:
        counter: Non-negative integer value to encode
        min_length: Left-pad output with "0" to at least this length
    """
    if counter < 0:
        raise ValueError("counter must be >= 0")
    if min_length < 1:
        raise ValueError("min_length must be >= 1")

    n = counter
    encoded_reversed = []
    while True:
        n, remainder = divmod(n, BASE)
        encoded_reversed.append(ALPHABET[remainder])
        if n == 0:
            break

    encoded = "".join(reversed(encoded_reversed))
    return encoded.rjust(min_length, "0")


def _ordinal(value: str) -> int:
    """Map character to alphabet index, unknown chars map to 0 (NOID-compatible behavior)."""
    try:
        return ALPHABET.index(value)
    except ValueError:
        return 0


def compute_checkdigit(payload: str) -> str:
    """
    Compute NOID-style checkdigit for the given payload.

    Payload can include separators (e.g., "/"), unknown chars contribute 0.
    """
    normalized = payload.lower()
    checksum = sum(_ordinal(ch) * (idx + 1) for idx, ch in enumerate(normalized))
    return ALPHABET[checksum % BASE]


def validate_name_checkdigit(naan: str, name: str) -> bool:
    """
    Validate trailing checkdigit for a minted name.

    Raises:
        ValueError: If name is empty or checkdigit does not match.
    """
    if not name:
        raise ValueError("name is required")

    stem = name[:-1]
    provided = name[-1].lower()
    expected = compute_checkdigit(f"{naan}/{stem}")
    if provided != expected:
        raise ValueError(f"invalid checkdigit: got '{provided}', expected '{expected}'")
    return True


def mint_ark_id(
    naan: str,
    counter: int,
    shoulder: str = "",
    min_length: int = 7,
    checkdigit: bool = True,
) -> str:
    """
    Build full ARK using deterministic counter-based NOID suffix.
    """
    suffix = encode_counter(counter=counter, min_length=min_length)
    name = f"{shoulder}{suffix}" if shoulder else suffix
    if checkdigit:
        name = f"{name}{compute_checkdigit(f'{naan}/{name}')}"
    return f"ark:{naan}/{name}"
