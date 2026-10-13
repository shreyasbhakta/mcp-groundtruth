"""Verdict vocabulary. Derivation logic arrives with the runner (build step 4)."""

from __future__ import annotations

import enum


class Outcome(enum.Enum):
    PASS = "PASS"
    VIOLATION = "VIOLATION"
    INCONCLUSIVE = "INCONCLUSIVE"


class TestKind(enum.Enum):
    __test__ = False  # not a pytest test class

    SEQUENTIAL_IDEMPOTENCE = "sequential_idempotence"
    CONCURRENT_IDEMPOTENCE = "concurrent_idempotence"
    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"


class InconclusiveReason(enum.Enum):
    NO_DECLARATION = "NO_DECLARATION"  # hint not sent at all
    MALFORMED_DECLARATION = "MALFORMED_DECLARATION"  # hint sent, but not as a JSON boolean
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    # The only observed change is in a region the fixture did not classify as
    # business or append-only. Applies to the idempotence and destructive tests,
    # whose claims are about business state. It does NOT apply to READ_ONLY:
    # readOnlyHint:true claims no state changes at all, so a change anywhere
    # observed, classified or not, can support a VIOLATION.
    UNCLASSIFIED_STATE = "UNCLASSIFIED_STATE"
    RESET_UNVERIFIED = "RESET_UNVERIFIED"
    NONDETERMINISTIC = "NONDETERMINISTIC"
    CALL_ERROR = "CALL_ERROR"
    SETUP_FAILED = "SETUP_FAILED"
