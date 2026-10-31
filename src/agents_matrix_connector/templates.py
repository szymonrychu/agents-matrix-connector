"""Validation and sample context of the preamble templates (no I/O)."""

from datetime import UTC, datetime

from agents_common.templating import TemplateError, undeclared_variables, validate

from agents_matrix_connector.prompt import PREAMBLE_VARIABLES, preamble_context

SAMPLE_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def sample_context() -> dict:
    return preamble_context("Family chat", "!room:example.org", "Alice", "family", SAMPLE_NOW)


def check_preamble(template: str | None) -> str | None:
    """None when the template is acceptable, else a message for the caller.

    A template must parse and may read only the preamble variables; anything else would fail
    at render time with an undefined variable.
    """
    if template is None:
        return None
    try:
        validate(template)
        unknown = [v for v in undeclared_variables(template) if v not in PREAMBLE_VARIABLES]
    except TemplateError as exc:
        return exc.message
    if unknown:
        return f"unknown variable {', '.join(unknown)}; allowed: {', '.join(PREAMBLE_VARIABLES)}"
    return None
