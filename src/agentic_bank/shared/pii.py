"""Tokenize a customer's free text before a model, a log or Step Functions history sees it.

Comprehend finds names, addresses, emails and so on. A deterministic backstop turns every run of
7+ digits into a token whatever Comprehend says, so card and account digits never depend on a model.
A run that is exactly a date (2026-11-07, 11-07-2026) is left alone: the review needs dates.
Each original is KMS-encrypted (context: case_id + token) into the vault. Fail closed: any
dependency error raises TokenizeError and nothing cleartext is stored.
"""

import re
import secrets
import time
from typing import Any

KINDS = {
    "NAME",
    "ADDRESS",
    "PHONE",
    "EMAIL",
    "CREDIT_DEBIT_NUMBER",
    "CREDIT_DEBIT_CVV",
    "CREDIT_DEBIT_EXPIRY",
    "BANK_ACCOUNT_NUMBER",
    "BANK_ROUTING",
    "SSN",
    "PIN",
    "USERNAME",
    "PASSWORD",
}  # DATE_TIME is kept on purpose: dates matter to the review
MIN_SCORE = 0.5
DIGITS = re.compile(r"\d(?:[ -]?\d){6,}")
DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-\d{4}")


class TokenizeError(Exception):
    pass


def find_spans(text: str, entities: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    """Comprehend spans plus the digit rule, with overlapping spans merged into one."""
    spans = [
        (e["BeginOffset"], e["EndOffset"], e["Type"])
        for e in entities
        if e["Type"] in KINDS and e["Score"] >= MIN_SCORE
    ]
    spans += [
        (m.start(), m.end(), "NUMBER") for m in DIGITS.finditer(text) if not DATE.fullmatch(m[0])
    ]
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(spans):
        if merged and start < merged[-1][1]:
            prev_start, prev_end, prev_kind = merged[-1]
            keep = prev_kind if prev_kind != "NUMBER" else kind
            merged[-1] = (prev_start, max(prev_end, end), keep)
        else:
            merged.append((start, end, kind))
    return merged


class Tokenizer:
    def __init__(self, comprehend: Any, kms: Any, vault: Any, key_id: str) -> None:
        self.comprehend, self.kms, self.vault, self.key_id = comprehend, kms, vault, key_id

    def tokenize(self, text: str, case_id: str) -> str:
        try:
            found = self.comprehend.detect_pii_entities(Text=text, LanguageCode="en")["Entities"]
            seen: dict[str, str] = {}  # one token per repeated value
            out = text
            for start, end, kind in reversed(find_spans(text, found)):
                value = text[start:end]
                if value not in seen:
                    seen[value] = f"<{kind}_{secrets.token_hex(4)}>"
                    self._store(seen[value], value, kind, case_id)
                out = out[:start] + seen[value] + out[end:]
            return out
        except Exception as exc:  # fail closed: refuse the dispute, never store cleartext
            raise TokenizeError("could not tokenize the statement") from exc

    def _store(self, token: str, value: str, kind: str, case_id: str) -> None:
        blob = self.kms.encrypt(
            KeyId=self.key_id,
            Plaintext=value.encode(),
            EncryptionContext={"case_id": case_id, "token": token},
        )["CiphertextBlob"]
        self.vault.put_item(
            Item={
                "token": token,
                "case_id": case_id,
                "kind": kind,
                "ciphertext": blob,
                "created_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(#t)",
            ExpressionAttributeNames={"#t": "token"},
        )

    def reveal(self, token: str, case_id: str) -> str:
        """Decrypt one token of this case (the analyst Lambda audits first). KeyError if foreign."""
        row = self.vault.get_item(Key={"token": token}).get("Item")
        if not row or row["case_id"] != case_id:
            raise KeyError("no such token for this case")
        plain = self.kms.decrypt(
            CiphertextBlob=bytes(row["ciphertext"]),
            EncryptionContext={"case_id": case_id, "token": token},
        )["Plaintext"]
        return plain.decode()
