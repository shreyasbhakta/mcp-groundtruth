"""Verdict vocabulary. Derivation logic arrives with the runner (build step 4)."""

from __future__ import annotations

import enum


class Outcome(enum.Enum):
    PASS = "pass"
    VIOLATION = "violation"
    INCONCLUSIVE = "inconclusive"


class InconclusiveReason(enum.Enum):
    NO_DECLARATION = "NO_DECLARATION"  # hint not sent at all
    MALFORMED_DECLARATION = "MALFORMED_DECLARATION"  # hint sent, but not as a JSON boolean
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    RESET_UNVERIFIED = "RESET_UNVERIFIED"
    NONDETERMINISTIC = "NONDETERMINISTIC"
    CALL_ERROR = "CALL_ERROR"
    SETUP_FAILED = "SETUP_FAILED"
