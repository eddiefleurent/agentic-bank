import pytest

from agentic_bank.shared.pii import TokenizeError, Tokenizer

TEXT = "Ship to 1600 Example St, call me at 512-555-0142. Card 4111 1111 1111 1111."


class Comprehend:
    def __init__(self, entities=(), fail=False):
        self.entities, self.fail = list(entities), fail

    def detect_pii_entities(self, **_):
        if self.fail:
            raise RuntimeError("throttled")
        return {"Entities": self.entities}


class Kms:
    """Records the encryption context; decrypt refuses a different one, like the real service."""

    def encrypt(self, KeyId, Plaintext, EncryptionContext):  # noqa: N803
        return {
            "CiphertextBlob": b"ENC" + Plaintext + repr(sorted(EncryptionContext.items())).encode()
        }

    def decrypt(self, CiphertextBlob, EncryptionContext):  # noqa: N803
        body, _, ctx = CiphertextBlob[3:].partition(b"[")
        assert b"[" + ctx == repr(sorted(EncryptionContext.items())).encode()
        return {"Plaintext": body}


class Vault:
    def __init__(self):
        self.rows = {}

    def put_item(self, Item, **_):  # noqa: N803
        self.rows[Item["token"]] = Item

    def get_item(self, Key):  # noqa: N803
        row = self.rows.get(Key["token"])
        return {"Item": row} if row else {}


def make(entities=(), fail=False):
    vault = Vault()
    return Tokenizer(Comprehend(entities, fail), Kms(), vault, "key"), vault


def ent(text, value, kind):
    start = text.index(value)
    return {"Type": kind, "Score": 0.99, "BeginOffset": start, "EndOffset": start + len(value)}


def test_round_trip_restores_every_value():
    entities = [ent(TEXT, "1600 Example St", "ADDRESS")]
    tk, vault = make(entities)
    out = tk.tokenize(TEXT, "dsp_1")
    assert "Example" not in out and "512" not in out and "4111" not in out
    restored = out
    for token in vault.rows:
        restored = restored.replace(token, tk.reveal(token, "dsp_1"))
    assert restored == TEXT


def test_digit_rule_catches_what_comprehend_misses():
    tk, _ = make([])  # Comprehend found nothing at all
    out = tk.tokenize(TEXT, "dsp_1")
    assert "512-555-0142" not in out and "4111" not in out
    assert "<NUMBER_" in out


def test_digit_rule_keeps_dates_but_not_card_digits():
    tk, _ = make([])
    out = tk.tokenize("Due 2026-11-07, ordered 11-07-2026, card 4111-1111-1111-1111.", "dsp_1")
    assert "2026-11-07" in out and "11-07-2026" in out
    assert "4111" not in out


def test_repeated_value_gets_one_token_and_overlaps_merge():
    text = "call 512-555-0142 or 512-555-0142"
    tk, vault = make([ent(text, "512-555-0142", "PHONE")])
    out = tk.tokenize(text, "dsp_1")
    assert (
        len(vault.rows) == 1 and out.count(next(iter(vault.rows))) == 2
    )  # PHONE span won the merge


def test_reveal_refuses_another_cases_token():
    tk, vault = make()
    tk.tokenize("call 512-555-0142", "dsp_1")
    with pytest.raises(KeyError):
        tk.reveal(next(iter(vault.rows)), "dsp_2")


def test_any_dependency_error_fails_closed():
    tk, vault = make(fail=True)
    with pytest.raises(TokenizeError):
        tk.tokenize(TEXT, "dsp_1")
    assert not vault.rows
