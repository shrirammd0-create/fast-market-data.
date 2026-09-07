"""Malformed-packet guard: schema validation for incoming JSON payloads.

APIs occasionally emit partial or garbage records (null prices, string
volumes, missing fields). Rather than crashing the whole snapshot, records
are validated against a lightweight schema and invalid ones are dropped with
a log line and a counter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    """One expected field: accepted types, optionality, extra check."""

    types: tuple[type, ...]
    required: bool = True
    check: Callable[[Any], bool] | None = None  # extra predicate, e.g. positivity


def number_like(value: Any) -> bool:
    """True for ints/floats and numeric strings (OANDA sends prices as strings)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


def validate_record(record: Any, schema: Mapping[str, Field]) -> list[str]:
    """Return a list of problems; empty list means the record is valid."""
    errors: list[str] = []
    if not isinstance(record, Mapping):
        return [f"record is {type(record).__name__}, expected object"]
    for name, field in schema.items():
        if name not in record or record[name] is None:
            if field.required:
                errors.append(f"missing required field '{name}'")
            continue
        value = record[name]
        if field.types and not isinstance(value, field.types):
            errors.append(
                f"field '{name}' is {type(value).__name__}, "
                f"expected {'/'.join(t.__name__ for t in field.types)}"
            )
            continue
        if field.check is not None and not field.check(value):
            errors.append(f"field '{name}' failed validation: {value!r}")
    return errors


def filter_valid(
    records: Iterable[Any],
    schema: Mapping[str, Field],
    context: str = "payload",
) -> tuple[list[Any], int]:
    """Split records into (valid, dropped_count), logging what was dropped."""
    valid: list[Any] = []
    dropped = 0
    for record in records:
        errors = validate_record(record, schema)
        if errors:
            dropped += 1
            logger.warning("dropping malformed %s record: %s", context, "; ".join(errors))
        else:
            valid.append(record)
    if dropped:
        logger.warning("%s: dropped %d malformed record(s), kept %d", context, dropped, len(valid))
    return valid, dropped


def require_keys(payload: Any, keys: Sequence[str], context: str = "payload") -> bool:
    """Top-level envelope check; logs and returns False instead of raising."""
    if not isinstance(payload, Mapping):
        logger.error("%s: expected JSON object, got %s", context, type(payload).__name__)
        return False
    missing = [k for k in keys if k not in payload]
    if missing:
        logger.error("%s: missing top-level key(s): %s", context, ", ".join(missing))
        return False
    return True
