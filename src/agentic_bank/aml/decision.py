"""OpenRouter decision-model second opinion. Names and long digit runs are tokenized."""

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agentic_bank.shared.pii import TokenizeError, Tokenizer

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "morph/reflex"
DREX_MODEL = "nace-ai/drex-max-finance"
TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 2
AUTO_ESCALATE_AT = 0.9
LIKELY_CLOSE_AT = 0.1
BAND_AUTO = "auto-escalate band"
BAND_CLOSE = "likely-close band (still human-reviewed, never auto-close)"
BAND_UNSURE = "unsure → human"
Recommendation = Literal["close", "escalate"]
Transport = Callable[[bytes, dict[str, str], float], bytes]


def model_slug(model: str) -> str:
    if model == DREX_MODEL:
        return "drex"
    if model == MODEL:
        return "reflex"
    return model.split("/")[-1]


class DecisionHTTPError(Exception):
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        super().__init__(f"decisions HTTP {status}")


@dataclass(frozen=True)
class DecisionOpinion:
    p_escalate: float
    p_suspicion: float
    choice: Recommendation
    band: str
    model: str
    cost_usd: float | None


def route_band(p_escalate: float) -> str:
    if p_escalate >= AUTO_ESCALATE_AT:
        return BAND_AUTO
    if p_escalate <= LIKELY_CLOSE_AT:
        return BAND_CLOSE
    return BAND_UNSURE


class _Names:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def detect_pii_entities(self, Text: str, **_: object) -> dict[str, Any]:
        entities: list[dict[str, Any]] = []
        for name in self.names:
            start = 0
            while name and (at := Text.find(name, start)) >= 0:
                entities.append(
                    {"Type": "NAME", "Score": 1.0, "BeginOffset": at, "EndOffset": at + len(name)}
                )
                start = at + len(name)
        return {"Entities": entities}

    def encrypt(self, **_: object) -> dict[str, bytes]:
        return {"CiphertextBlob": b"local"}

    def put_item(self, **_: object) -> None:
        return None


def tokenized_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    rows: list[Any] = [snapshot.get("customer") or {}]
    rows += list(snapshot.get("linked_customers") or [])
    rows += list(snapshot.get("counterparties") or [])
    found = [row.get("name") for row in rows if isinstance(row, dict)]
    names = sorted(
        {name for name in found if isinstance(name, str) and name}, key=len, reverse=True
    )
    local = _Names(names)
    tokenizer = Tokenizer(local, local, local, "local")
    case_id = str(snapshot.get("case_id") or "case")

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return tokenizer.tokenize(value, case_id)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(snapshot)


def request_body(state: dict[str, Any], model: str = MODEL) -> dict[str, Any]:
    return {
        "model": model,
        "provider": {"zdr": True},
        "state": state,
        "questions": {
            "disposition": {
                "type": "choice",
                "instructions": "Do notes, memos, or expected countries explain it, or escalate?",
                "criteria": {
                    "close": "A documented legitimate reason covers the flagged activity.",
                    "escalate": "Nothing documented explains it, or the case is unclear.",
                },
            },
            "suspicion": {
                "type": "noul",
                "instructions": "Money laundering, or a documented false positive?",
                "criteria": {
                    "true": "Suspicious and not covered by the profile.",
                    "false": "Fits a documented legitimate reason.",
                },
            },
        },
    }


def second_opinion(
    snapshot: dict[str, Any], *, transport: Transport | None = None, model: str = MODEL
) -> DecisionOpinion | None:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return None
    try:
        body = json.dumps(request_body(tokenized_state(snapshot), model)).encode()
    except TokenizeError as exc:
        _log_failure(exc)
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    post = transport or urlopen_transport
    for attempt in range(MAX_ATTEMPTS):
        try:
            payload = json.loads(post(body, headers, TIMEOUT_SECONDS))
        except Exception as exc:
            if attempt + 1 < MAX_ATTEMPTS and _retryable(exc):
                continue
            _log_failure(exc)
            return None
        return _parse(payload, model) if isinstance(payload, dict) else None
    return None


def urlopen_transport(body: bytes, headers: Mapping[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(DECISIONS_URL, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise DecisionHTTPError(exc.code, raw.decode("utf-8", "replace")) from None


def _log_failure(exc: BaseException) -> None:
    detail = f"HTTP {exc.status}" if isinstance(exc, DecisionHTTPError) else type(exc).__name__
    print(f"aml decision: no opinion ({detail})", file=sys.stderr)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse(body: dict[str, Any], model: str = MODEL) -> DecisionOpinion | None:
    answers = _as_dict(body.get("answers"))
    disposition = _as_dict(answers.get("disposition"))
    suspicion = _as_dict(answers.get("suspicion"))
    probs = _as_dict(disposition.get("probabilities"))
    p_escalate, p_suspicion = _unit(probs.get("escalate")), _unit(suspicion.get("noul"))
    choice, returned = disposition.get("choice"), body.get("model")
    ok = (
        disposition.get("type") == "choice"
        and suspicion.get("type") == "noul"
        and isinstance(returned, str)
        and returned.startswith(model)
        and p_escalate is not None
        and p_suspicion is not None
        and choice in ("close", "escalate")
    )
    if not ok:
        return None
    cost = _as_dict(body.get("usage")).get("cost")
    cost_usd = None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        cost_usd = float(cost)
    return DecisionOpinion(
        p_escalate, p_suspicion, choice, route_band(p_escalate), returned, cost_usd
    )


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, DecisionHTTPError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError))


def _unit(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0 <= number <= 1 else None
