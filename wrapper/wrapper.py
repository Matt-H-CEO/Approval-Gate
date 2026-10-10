"""Approval Gate decision core.

Flow: run the persona's source-of-truth checker -> call Ezio over HTTP -> read its verdict, claims and
dimensions -> apply policy.json (worst signal wins) -> decision + plain reason + evidence + coverage
-> audit log -> the mock action runs only on APPROVE (serve.py / gate()).

The checker always runs, even when Ezio fails, so a known-bad draft is blocked with evidence during an outage.

Run one input:  python3 wrapper.py customer_support "Your 30% loyalty discount applies to the Pro plan." '{"plan":"Pro","customer_tenure_months":18}'
Score yourself: python3 evaluate.py customer_support
"""
import csv
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import audit
import checks
import mock_actions
from ezio_client import EzioError, analyze
from findings import APPROVE, BLOCK, HOLD, RANK, CheckResult, Finding

HERE = Path(__file__).resolve().parent
PERSONAS = HERE.parent / "personas"
POLICY = json.loads((HERE / "policy.json").read_text())
ACTION = {"customer_support": ("email", "the customer"), "paralegal": ("memo", "the partner"),
          "financial_analyst": ("note", "the client")}


@dataclass
class Signals:
    tier: str
    score: float
    summary: str
    accuracy_status: str
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    tone: float
    bias: float
    agreement: float
    claims: list = field(default_factory=list)


@dataclass
class Decision:
    decision: str
    reason: str
    evidence: list
    ezio: dict
    findings: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    consequence: str = ""
    ezio_status: str = "ok"
    welfare: dict = None
    baseline: dict = None
    decision_id: str = ""
    four_tests: dict = None
    issues: list = field(default_factory=list)
    ezio_flags: list = field(default_factory=list)
    recommendation: dict = None
    preferences: dict = None


def parse_signals(resp):
    try:
        verdict, dims = resp["verdict"], resp["dimensions"]
        acc = dims["accuracy"]
        if verdict["tier"] not in ("GREEN", "YELLOW", "RED") or acc["status"] not in ("scored", "not_scored", "unavailable"):
            raise ValueError("invalid tier or accuracy status")
        scores = [verdict["score"]] + [dims[d]["score"] for d in ("tone", "bias", "agreement")]
        if acc["status"] == "scored":
            scores.append(acc["score"])
        if any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or not 0 <= s <= 100 for s in scores):
            raise ValueError("scores must be finite numbers between 0 and 100")
        if not isinstance(verdict["summary"], str) or not isinstance(resp.get("claims", []), list):
            raise ValueError("invalid summary or claims")
        return Signals(
            tier=verdict["tier"], score=verdict["score"], summary=verdict["summary"],
            accuracy_status=acc["status"], claims_total=acc.get("claims_total", 0),
            claims_verified=acc.get("claims_verified", 0), claims_contradicted=acc.get("claims_contradicted", 0),
            tone=dims["tone"]["score"], bias=dims["bias"]["score"], agreement=dims["agreement"]["score"],
            claims=[c for c in resp.get("claims", []) if isinstance(c, dict)],
        )
    except (KeyError, TypeError, ValueError) as e:
        raise EzioError("malformed mock response", resp) from e


@lru_cache(maxsize=None)
def load_source_of_truth(persona):
    """Every .json and .csv file in the persona folder, keyed by file name (for example sot['policies'])."""
    data = {}
    for f in sorted((PERSONAS / persona).iterdir()):
        if f.stem in ("practice_set", "business_rules"):
            continue
        if f.suffix == ".json":
            data[f.stem] = json.loads(f.read_text())
        elif f.suffix == ".csv":
            with f.open() as handle:
                data[f.stem] = list(csv.DictReader(handle))
    return data


def check_source_of_truth(text, context, persona):
    """The independent signal. A checker crash becomes a HOLD, never a 500 or a silent approve."""
    try:
        return checks.run(persona, text, context, load_source_of_truth(persona))
    except Exception as e:  # noqa: BLE001 - any checker bug must fail closed
        return CheckResult(findings=[Finding(HOLD, f"The company-data check failed ({type(e).__name__}); a person must review.",
                                             "checker error")], unchecked=["company data (checker error)"])


def _src(source):
    if isinstance(source, dict):
        source = source.get("title") or source.get("url") or ""
    return f" (source: {source})" if source else ""


class _Ledger:
    """Worst signal wins. Keeps every reason, in order, with its level."""

    def __init__(self):
        self.level, self.items = APPROVE, []

    def add(self, level, reason):
        self.items.append((level, reason))
        if RANK[level] > RANK[self.level]:
            self.level = level

    def decisive(self):
        return list(dict.fromkeys(r for lvl, r in self.items if lvl == self.level))


def apply_policy(signals, check, policy):
    led, ezio_supported = _Ledger(), []
    for f in check.findings:
        led.add(f.level, f.message)
    contradicted = 0
    for c in signals.claims:
        verdict, txt = c.get("verdict"), c.get("text", "a claim")
        src = _src(c.get("source"))
        if verdict == "contradicted":
            contradicted += 1
            led.add(policy["contradicted_claim"], f'Ezio\'s fact-check contradicts "{txt}"{src}.')
        elif verdict == "unverifiable":
            led.add(policy["unverifiable_claim"], f'Ezio could not verify "{txt}".')
        elif verdict == "supported":
            ezio_supported.append(txt)
    if signals.claims_contradicted and not contradicted:
        led.add(policy["contradicted_claim"], f"Ezio reports {signals.claims_contradicted} contradicted claim(s).")
    for dim, rule in policy["dimension_minimums"].items():
        score = getattr(signals, dim)
        if score < rule["hold_below"]:
            led.add(HOLD, f"Ezio's {dim} score is {score:g} (below {rule['hold_below']}).")
    if signals.accuracy_status == "unavailable":
        led.add(policy["when_accuracy_unavailable"], "Ezio's fact-check was unavailable, so public-knowledge claims went unchecked.")
    if signals.accuracy_status == "not_scored" and policy["when_accuracy_not_scored"] != "ignore":
        led.add(policy["when_accuracy_not_scored"], "Ezio found no claims to check.")
    tier_level = policy["tier_decisions"][signals.tier]
    if tier_level != APPROVE:
        led.add(tier_level, f"Ezio rates it {signals.tier} {signals.score:g}: {signals.summary}")
    w = check.welfare
    if w and w.get("status") == "dominated" and policy.get("welfare_mode") == "enforce":
        led.add(HOLD, w["note"])
    return led, ezio_supported


def _coverage(check, ezio_supported, unavailable):
    checked = list(dict.fromkeys(check.verified + [f"Ezio: {t}" for t in ezio_supported]))
    unchecked = list(dict.fromkeys(check.unchecked + (["public-knowledge claims (Ezio unavailable)"] if unavailable else [])))
    return {"checked": checked, "could_not_check": unchecked}


def _coverage_line(cov):
    checked = "; ".join(cov["checked"]) if cov["checked"] else "no pricing or policy figures to check"
    line = f"Checked: {checked}."
    if cov["could_not_check"]:
        line += f" Could not check: {'; '.join(cov['could_not_check'])}."
    return line


def _consequence(level, check, signals_claims, persona):
    noun, who = ACTION.get(persona, ("message", "the recipient"))
    blocks = [f for f in check.findings if f.level == BLOCK]
    if level == BLOCK and blocks:
        f = blocks[0]
        return f"If sent, {who} would read \"{f.stated or f.span}\", but {f.source or 'company data'} says {f.expected or 'otherwise'}."
    if level == BLOCK:
        bad = next((c.get("text") for c in signals_claims if c.get("verdict") == "contradicted"), None)
        return f"If sent, {who} would read a claim Ezio's fact-check contradicts" + (f": \"{bad}\"." if bad else ".")
    if level == HOLD:
        return f"Not sent. A person reviews the {noun} first; nothing reaches {who} until they release it."
    return f"Sent: the {noun} goes to {who}."


def _evidence(check, signals, led, cov):
    ev = []
    for f in check.findings:
        ev.append(f"[{f.level}] {f.message}" + (f" (source: {f.source})" if f.source else ""))
    if signals is not None:
        for c in signals.claims:
            ev.append(f"Ezio claim {c.get('verdict', '?')}: \"{c.get('text', '')}\"{_src(c.get('source'))}")
        ev.append(f"Ezio verdict: {signals.tier} {signals.score:g}. {signals.summary}")
        ev.append(f"Ezio accuracy: {signals.accuracy_status}; {signals.claims_verified}/{signals.claims_total} supported, "
                  f"{signals.claims_contradicted} contradicted.")
        ev.append(f"Ezio dimensions: tone {signals.tone:g}, bias {signals.bias:g}, agreement {signals.agreement:g} "
                  f"(hold below {POLICY['dimension_minimums']['tone']['hold_below']}).")
    ev.append(_coverage_line(cov))
    if check.welfare:
        ev.append(check.welfare["note"])
    return ev


def _reason(led, check, signals, cov):
    if led.level != APPROVE:
        lead = led.decisive()
        extra = len(dict.fromkeys(r for _, r in led.items)) - len(lead)
        return " ".join(lead) + (f" (+{extra} other signal{'s' if extra != 1 else ''}; see evidence)" if extra > 0 else "")
    n = len(cov["checked"])
    what = f"{n} claim{'s' if n != 1 else ''} matched company data or Ezio's fact-check" if n else "No pricing or policy claims to check"
    return f"{what}; Ezio rates it {signals.tier} {signals.score:g} with no low dimension."


STARTER_TIERS = {"GREEN": APPROVE, "YELLOW": HOLD, "RED": BLOCK}


def baseline_decision(signals):
    """What the unmodified starter would do: follow Ezio's overall tier, nothing else. Shown for comparison only."""
    if signals is None:
        return {"decision": HOLD, "reason": "Starter gate: Ezio unavailable, so it holds."}
    return {"decision": STARTER_TIERS[signals.tier],
            "reason": f"Starter gate: follows Ezio's tier only ({signals.tier} {signals.score:g}); no company-data check."}


def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    context = context if isinstance(context, dict) else {}
    check = check_source_of_truth(text, context, persona)
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        blocks = [f for f in check.findings if f.level == BLOCK]
        level = BLOCK if blocks else POLICY["on_ezio_failure"]
        cov = _coverage(check, [], True)
        cov["could_not_check"] = list(dict.fromkeys(cov["could_not_check"] + ["tone, bias and agreement (Ezio unavailable)"]))
        lead = blocks[0].message if blocks else f"Held for a person: without Ezio, tone and public-knowledge claims are unchecked."
        result = Decision(
            level, f"Ezio unavailable ({e}). {lead}",
            [f"Ezio call failed after retries: {e}."] + _evidence(check, None, None, cov),
            {"error": str(e), "response": e.response},
            findings=[f.as_dict() for f in check.findings], coverage=cov,
            consequence=_consequence(level, check, [], persona), ezio_status="unavailable", welfare=check.welfare,
            baseline=baseline_decision(None),
        )
    else:
        led, ezio_supported = apply_policy(signals, check, POLICY)
        cov = _coverage(check, ezio_supported, signals.accuracy_status == "unavailable")
        result = Decision(
            led.level, _reason(led, check, signals, cov), _evidence(check, signals, led, cov), response,
            findings=[f.as_dict() for f in check.findings], coverage=cov,
            consequence=_consequence(led.level, check, signals.claims, persona),
            ezio_status="degraded" if signals.accuracy_status == "unavailable" else "ok", welfare=check.welfare,
            baseline=baseline_decision(signals),
        )
    result.recommendation = check.recommendation
    result.preferences = (check.recommendation or {}).get("preferences")
    result.ezio_flags, result.issues = issue_ledger(result, check)
    result.decision_id = hashlib.sha1(f"{time.time_ns()}|{text}".encode()).hexdigest()[:12]
    result.four_tests = four_tests(result)
    if audit_log:
        audit.record(persona, text, context, result)
    return result


def ezio_flags_of(response):
    """What Ezio itself flagged: weak tier, weak dimension, contradicted or unverifiable claim."""
    try:
        sig = parse_signals(response)
    except Exception:  # noqa: BLE001
        return ["Ezio unavailable"]
    out = []
    if sig.tier != "GREEN":
        out.append({"issue": f"Overall tier {sig.tier} {sig.score:g}", "kind": "tier"})
    for d in ("tone", "bias", "agreement"):
        v = getattr(sig, d)
        if v < 80:
            out.append({"issue": f"{d.capitalize()} {v:g} ({'critical' if v < 60 else 'warning'})", "kind": d})
    for c in sig.claims:
        if c.get("verdict") in ("contradicted", "unverifiable"):
            out.append({"issue": f"Claim {c.get('verdict')}: {c.get('text')}", "kind": "claim"})
    return out


def issue_ledger(r, check):
    """Every issue in the draft and who caught it: Ezio, HoldPoint's data check, or both."""
    flags = ezio_flags_of(r.ezio)
    ezio_kinds = {f["kind"] for f in flags if isinstance(f, dict)}
    issues = []
    for f in check.findings:
        both = (f.rule.startswith("4") and ezio_kinds & {"tone", "bias", "agreement"})
        issues.append({"issue": f.message, "level": f.level, "expected": f.expected, "source": f.source,
                       "caught_by": "Ezio + HoldPoint" if both else "HoldPoint only"})
    for fl in flags:
        if not isinstance(fl, dict):
            continue
        if fl["kind"] == "claim":
            issues.append({"issue": fl["issue"], "level": BLOCK if "contradicted" in fl["issue"] else HOLD,
                           "expected": "", "source": "Ezio fact-check", "caught_by": "Ezio"})
        elif not any(i["caught_by"] == "Ezio + HoldPoint" for i in issues):
            issues.append({"issue": f"Ezio {fl['issue']}", "level": HOLD if "critical" in fl["issue"] or fl["kind"] == "tier" else "NOTE",
                           "expected": "", "source": "Ezio dimensions", "caught_by": "Ezio"})
    return [f["issue"] if isinstance(f, dict) else f for f in flags], issues


def four_tests(r):
    """The talk's decision-grade tests, answered for this one decision."""
    cov = r.coverage or {}
    blocks = [f for f in r.findings if f.get("level") == BLOCK]
    holds = [f for f in r.findings if f.get("level") == HOLD]
    unsure = list(cov.get("could_not_check", []))
    if r.ezio_status != "ok":
        unsure.append(f"Ezio {r.ezio_status}")
    return {
        "verified": {"ok": not blocks, "text": (f"{len(cov.get('checked', []))} claim(s) matched company data or Ezio's fact-check; "
                                                 f"{len(blocks)} contradicted.")},
        "calibrated": {"ok": True, "text": ("Held back: " + "; ".join(dict.fromkeys(f["message"] for f in holds))[:300] if holds and r.decision == HOLD
                                             else ("Could not check: " + "; ".join(unsure) if unsure else "Nothing left unchecked that this persona's rules cover."))},
        "overseeable": {"ok": True, "text": ("A person must review before anything is sent; the problem text is marked and the consequence stated."
                                              if r.decision != APPROVE else "Sent automatically: every checked figure matched. Spot-check from the audit trail.")},
        "auditable": {"ok": True, "text": f"Decision {r.decision_id} logged with the input, Ezio's full response, evidence, reason and time."},
    }


def gate(text, context=None, persona="customer_support"):
    result = decide(text, context, persona)
    print(f"{result.decision}: {result.reason}")
    for line in result.evidence:
        print(f"  - {line}")
    if result.decision == APPROVE:
        mock_actions.run_for_persona(persona, text)
    else:
        print("[GATE] action not executed")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python3 wrapper.py <persona> "<text>" [context-json]')
    gate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}, sys.argv[1])
