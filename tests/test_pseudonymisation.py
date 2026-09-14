"""HASH and MASK must not hand back what they claim to remove.

Both modes used to leak. `hash` was an unsalted sha256 of the value: PII is
drawn from small spaces — an SSN is 10^9 values — so enumerating the whole
space and matching digests recovers the original in seconds, whatever the
digest is truncated to. `mask` emitted `mask_char * len(original)`, which
reproduces the length of what it removed.
"""

import hashlib
import re

from redact.backends.builtin import MASK_WIDTH, apply_redactions, detect_entities
from redact.types import RedactionMode, RedactionOptions

SSN = "123-45-6789"
TEXT = f"SSN {SSN}"


def _hash(text, **kw):
    opts = RedactionOptions(mode=RedactionMode.HASH, **kw)
    return apply_redactions(text, detect_entities(text), opts), opts


# -- the attack that used to work ---------------------------------------------

def test_the_token_is_not_a_bare_digest_of_the_value():
    """Enumerate the SSN space, match digests, recover the original."""
    out, _ = _hash(TEXT)
    token = re.search(r"<US_SSN:([0-9a-f]+)>", out).group(1)
    naive = hashlib.sha256(SSN.encode()).hexdigest()[: len(token)]
    assert token != naive, "an attacker who guesses the value confirms it by digest"


def test_guessing_the_value_does_not_confirm_it_without_the_key():
    """Simulated brute force over a candidate set containing the true value."""
    out, _ = _hash(TEXT)
    token = re.search(r"<US_SSN:([0-9a-f]+)>", out).group(1)
    candidates = [SSN, "000-00-0000", "987-65-4321"]
    recovered = [
        c for c in candidates
        if hashlib.sha256(c.encode()).hexdigest()[: len(token)] == token
    ]
    assert recovered == [], "the true value must not be identifiable by digest alone"


# -- linkage is a deliberate choice, not an accident --------------------------

def test_runs_are_unlinkable_without_a_key():
    a, _ = _hash(TEXT)
    b, _ = _hash(TEXT)
    assert a != b, "two unkeyed runs must not produce correlatable pseudonyms"


def test_an_explicit_key_makes_runs_correlatable():
    a, _ = _hash(TEXT, hash_key="shared-secret")
    b, _ = _hash(TEXT, hash_key="shared-secret")
    assert a == b


def test_different_keys_disagree():
    a, _ = _hash(TEXT, hash_key="key-one")
    b, _ = _hash(TEXT, hash_key="key-two")
    assert a != b


def test_equal_values_agree_inside_one_run():
    """Pseudonymisation is the point: the same person must map to one token."""
    text = f"{SSN} appears twice: {SSN}"
    opts = RedactionOptions(mode=RedactionMode.HASH)
    out = apply_redactions(text, detect_entities(text), opts)
    tokens = re.findall(r"<US_SSN:([0-9a-f]+)>", out)
    assert len(tokens) == 2 and tokens[0] == tokens[1]


# -- mask must not leak size --------------------------------------------------

def test_mask_width_is_fixed_regardless_of_input_length():
    short = "a@b.co"
    long_ = "a.very.long.address@example-company.com"
    outs = []
    for value in (short, long_):
        text = f"mail {value}"
        outs.append(apply_redactions(
            text, detect_entities(text), RedactionOptions(mode=RedactionMode.MASK)
        ))
    assert outs[0] == outs[1], "mask length must not distinguish the originals"
    assert outs[0] == f"mail {'*' * MASK_WIDTH}"


def test_mask_does_not_reproduce_the_original_length():
    text = f"SSN {SSN}"
    out = apply_redactions(
        text, detect_entities(text), RedactionOptions(mode=RedactionMode.MASK)
    )
    assert "*" * len(SSN) not in out
