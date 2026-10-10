"""Shared result types for the decision core and the persona checkers."""
from dataclasses import dataclass, field

APPROVE, HOLD, BLOCK = "APPROVE", "HOLD_FOR_REVIEW", "BLOCK"
RANK = {APPROVE: 0, HOLD: 1, BLOCK: 2}


@dataclass
class Finding:
    """One problem the gate found. level is BLOCK or HOLD_FOR_REVIEW."""
    level: str
    message: str
    rule: str = ""          # which business rule fired, e.g. "1 facts", "3 promise"
    stated: str = ""        # what the draft says
    expected: str = ""      # what the source of truth says
    source: str = ""        # file and field the expected value came from
    span: str = ""          # the text fragment that triggered it

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class CheckResult:
    """What a persona checker returns: problems, plus what it could and could not check."""
    findings: list = field(default_factory=list)
    verified: list = field(default_factory=list)    # claims that matched the source of truth
    unchecked: list = field(default_factory=list)   # claims seen but not checkable here
    welfare: dict = None                            # optional multi-objective note (advisory)
