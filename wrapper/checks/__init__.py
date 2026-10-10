"""Persona checkers. Each takes (text, context, sot) and returns a CheckResult.

The decision core never changes when the persona changes; only this registry does.
"""
from findings import CheckResult

from . import customer_support

REGISTRY = {
    "customer_support": customer_support.check,
}


def run(persona, text, context, sot):
    fn = REGISTRY.get(persona)
    if fn is None:
        return CheckResult(unchecked=[f"no source-of-truth checker for persona '{persona}'"])
    return fn(text, context, sot)
