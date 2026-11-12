"""OpenTelemetry spans for the dispute review. A no-op unless a tracer is configured.

The Lambda zip ships no third-party packages, so `opentelemetry` is imported only if it is
there (an ADOT layer, AgentCore Runtime, or the dev environment). Without the API package, or with
the API but no SDK and exporter, every call here does nothing. Attribute names follow the
OpenTelemetry GenAI semantic conventions (`gen_ai.*`), which CloudWatch GenAI observability and
AgentCore Observability read. Spans carry numbers and enums only: never the case file, the
statement or a rationale, because trace stores are not the PII vault.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace
except ImportError:  # the deployed Lambdas: no layer, no spans
    trace = None

TRACER = "agentic_bank.disputes"
PROVIDER = "aws.bedrock"


class _NoSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        pass


def put(span: Any, attributes: dict[str, Any]) -> None:
    """Set attributes, skipping None (OpenTelemetry rejects it)."""
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Generator[Any]:
    """A child of the current span. An exception inside is recorded and re-raised."""
    if trace is None:
        yield _NoSpan()
        return
    with trace.get_tracer(TRACER).start_as_current_span(name) as current:
        put(current, attributes or {})
        yield current


def annotate(attributes: dict[str, Any]) -> None:
    """Add attributes to whatever span is current (for example the Lambda invocation span)."""
    if trace is not None:
        put(trace.get_current_span(), attributes)
