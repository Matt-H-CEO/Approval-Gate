"""Scenario lab: run a set of AI drafts through the same gate, next to the starter baseline, and score
each proposed action against stakeholder preference profiles (multi-objective utility).

Two kinds of scenario file in wrapper/scenarios/:
  "persona": drafts for a supplied persona (customer_support); each runs through wrapper.decide().
  "options": a mock decision (aviation dispatch) where the AI proposes one option plus a rationale.
             Guardrails are hard constraints; objectives and profiles come from the file;
             the rationale's numbers are checked against the file's facts; Ezio scores the rationale.

Dry run: the lab never runs the gated action. Advisory utility never changes the gate's decision.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import textnorm as tn
import welfare
import wrapper as core
from ezio_client import EzioError, analyze
from findings import APPROVE, BLOCK, HOLD, CheckResult, Finding

DIR = Path(__file__).resolve().parent / "scenarios"
_LB = re.compile(r"(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?:lb|lbs|pounds)\b", re.I)
_MIN = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s?(?:-\s?)?(?:minutes?|mins?)\b", re.I)
_HOURS = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s?(?:-\s?)?(?:hours?|hrs?)\b", re.I)
_COMPLIANCE = re.compile(r"\b(meets?|satisf\w+|covers?|complies|compliant|legal|plenty of)\b", re.I)


def list_scenarios():
    out = []
    for f in sorted(DIR.glob("*.json")):
        d = json.loads(f.read_text())
        out.append({k: d.get(k) for k in ("id", "title", "kind", "situation", "disclaimer", "action")})
    return out


def load(sid):
    for f in DIR.glob("*.json"):
        d = json.loads(f.read_text())
        if d.get("id") == sid:
            return d
    raise KeyError(sid)


# ---------------------------------------------------------------- options scenarios (aviation)

def _facts(scn):
    return {k: v["value"] for k, v in scn["facts"].items()}


def metrics(scn, key):
    f, o = _facts(scn), scn["options"][key]
    out = {}
    for obj in scn["objectives"]:
        c = obj["compute"]
        v = float(o[c["field"]])
        if "minus_fact" in c:
            v -= f[c["minus_fact"]]
        if "per_fact" in c:
            v /= f[c["per_fact"]]
        out[obj["key"]] = round(v, 1)
    return out


def guardrails(scn, key):
    f, o, out = _facts(scn), scn["options"][key], []
    for g in scn["guardrails"]:
        if g["type"] == "min_value":
            ok = o[g["option_field"]] >= f[g["fact"]]
            detail = f"{o[g['option_field']]:,} vs required {f[g['fact']]:,}"
        elif g["type"] == "max_value":
            ok = o[g["option_field"]] <= f[g["fact"]]
            detail = f"{o[g['option_field']]:,} vs limit {f[g['fact']]:,}"
        elif g["type"] == "duty":
            left = f[g["remaining_fact"]] - o.get("delay_min", 0)
            ok = bool(o.get("fresh_crew")) or left >= f[g["needed_fact"]]
            detail = "fresh crew" if o.get("fresh_crew") else f"{left:g} min of duty left after the delay vs {f[g['needed_fact']]:g} min needed"
        else:
            continue
        out.append({"id": g["id"], "label": g["label"], "ok": ok, "detail": detail, "source": g.get("source", "")})
    return out


def _dominates(scn, a, b):
    ge = gt = True
    gt = False
    for obj in scn["objectives"]:
        k, hb = obj["key"], obj["higher_is_better"]
        x, y = a[k], b[k]
        if (x < y) if hb else (x > y):
            ge = False
        if (x > y) if hb else (x < y):
            gt = True
    return ge and gt


def option_table(scn, weights_by_profile):
    rows = []
    for key, o in scn["options"].items():
        g = guardrails(scn, key)
        rows.append({"key": key, "label": o["label"], "metrics": metrics(scn, key), "guardrails": g, "feasible": all(x["ok"] for x in g)})
    feas = [r for r in rows if r["feasible"]]
    for r in feas:
        dom = [x for x in feas if x is not r and _dominates(scn, x["metrics"], r["metrics"])]
        r["dominated_by"] = dom[0]["key"] if dom else None
        r["pareto_frontier"] = not dom
    front = [r for r in feas if r["pareto_frontier"]] or feas
    for obj in scn["objectives"]:
        k = obj["key"]
        vals = [r["metrics"][k] for r in front]
        lo, hi = (min(vals), max(vals)) if vals else (0, 0)
        for r in feas:  # scales come from the frontier; dominated options are scored on the same scale, clamped
            v = r["metrics"][k]
            sc = 1.0 if hi == lo else ((v - lo) if obj["higher_is_better"] else (hi - v)) / (hi - lo)
            r.setdefault("scores", {})[k] = round(min(1.0, max(0.0, sc)), 4)
    profiles = {}
    for pk, prof in weights_by_profile.items():
        w = _norm_weights(scn, prof["weights"])
        util = {r["key"]: round(sum(w[o["key"]] * r["scores"][o["key"]] for o in scn["objectives"]), 4) for r in feas}
        best = max(front, key=lambda r: util[r["key"]]) if front else None
        profiles[pk] = {"label": prof["label"], "weights": w, "utility": util, "best": best["key"] if best else None,
                        "best_label": best["label"] if best else None}
    return rows, profiles


def _norm_weights(scn, w):
    keys = [o["key"] for o in scn["objectives"]]
    w = {k: max(0.0, float(w.get(k, 0) or 0)) for k in keys}
    t = sum(w.values()) or 1.0
    return {k: round(v / t, 4) for k, v in w.items()}


def option_surface(scn, steps=5):
    keys = [o["key"] for o in scn["objectives"]]
    vals = [round(i / (steps - 1), 2) for i in range(steps)]
    grid = []
    for w1 in vals:
        row = {}
        for a in vals:
            w = {keys[0]: w1, keys[1]: (1 - w1) * a, keys[2]: (1 - w1) * (1 - a)}
            _, prof = option_table(scn, {"p": {"label": "", "weights": w}})
            row[str(a)] = prof["p"]["best"]
        grid.append({keys[0] + "_weight": w1, "winner_by_" + keys[1] + "_share_of_rest": row})
    return {"axes": {"rows": f"weight on {keys[0]}", "columns": f"share of the remaining weight on {keys[1]} (rest on {keys[2]})"}, "grid": grid}


def _clauses(sentence):
    out, pos = [], 0
    for m in re.finditer(r"(?<!\d),|,(?!\d)|[;:]|\band\b|\bwhich\b", sentence):
        out.append((pos, m.start()))
        pos = m.end()
    out.append((pos, len(sentence)))
    return out


def _nearest(sentence, start, end, candidates):
    """candidates: [(name, keywords)]. Prefer a keyword in the same clause, then the nearest in the sentence."""
    low = sentence.lower()
    clause = next(((a, b) for a, b in _clauses(sentence) if a <= start < b), (0, len(sentence)))
    best, best_d = None, None
    for scope_rank, (a, b) in enumerate([clause, (0, len(sentence))]):
        for name, kws in candidates:
            for kw in kws:
                for m in re.finditer(re.escape(kw.lower()), low[a:b]):
                    ks, ke = a + m.start(), a + m.end()
                    d = (start - ke) if ke <= start else (ks - end) * 1.2 if ks >= end else 0
                    if d < 0:
                        continue
                    if best_d is None or d < best_d:
                        best, best_d = name, d
        if best:
            return best
    return None


def check_rationale(scn, option_key, text):
    facts, o = scn["facts"], scn["options"][option_key]
    m = metrics(scn, option_key)
    res = CheckResult()
    lb_cands = [(k, v["keywords"]) for k, v in facts.items() if v["unit"] == "lb"] + [("option.fuel_lb", scn.get("option_fuel_keywords", []))]
    min_cands = [(k, v["keywords"]) for k, v in facts.items() if v["unit"] == "min"]
    min_cands += [(f"option.{obj['key']}", obj.get("keywords", [])) for obj in scn["objectives"] if obj["key"] in ("safety_margin_min", "delay_min")]

    def expected(name):
        if name.startswith("option."):
            field = name.split(".", 1)[1]
            return (o[field] if field in o else m[field]), f"proposed option ({o['label']})"
        return facts[name]["value"], facts[name]["source"]

    norm = tn.normalize(text)
    for s in tn.sentences(norm):
        for rx, cands, unit, mult, tol in ((_LB, lb_cands, "lb", 1, 0.01), (_MIN, min_cands, "min", 1, 0.06), (_HOURS, min_cands, "min", 60, 0.06)):
            for x in rx.finditer(s):
                val = tn.to_float(x.group("n")) * mult
                name = _nearest(s, x.start(), x.end(), cands)
                if not name:
                    res.unchecked.append(f"{x.group(0)} (no matching fact)")
                    continue
                exp, src = expected(name)
                label = name.replace("option.", "").replace("_", " ")
                if abs(val - exp) <= max(1.0, tol * abs(exp)):
                    res.verified.append(f"{label} {x.group(0)} ({src})")
                else:
                    res.findings.append(Finding(BLOCK, f"The rationale says {x.group(0)} for {label}; the data says {exp:,g} {unit}.",
                                                "facts", x.group(0), f"{exp:,g} {unit}", src, s))
        low = s.lower()
        for w in scn.get("hold_language", []):
            if re.search(r"\b" + re.escape(w) + r"\b", low):
                res.findings.append(Finding(HOLD, f"Overconfident language (\"{w}\") in a safety decision: a person must confirm.",
                                            "overconfidence", w, "calibrated wording", "scenario hold_language", s))
                break
    for g in guardrails(scn, option_key):
        if not g["ok"]:
            claims_ok = bool(_COMPLIANCE.search(norm))
            res.findings.append(Finding(BLOCK, f"Guardrail broken: {g['label']} ({g['detail']})."
                                        + (" The rationale claims it is fine." if claims_ok else ""),
                                        "guardrail", o["label"], g["label"], g["source"], text))
        else:
            res.verified.append(f"guardrail: {g['label']} ({g['detail']})")
    return res


def ezio_flags(signals):
    """What Ezio itself flagged (tier, weak dimension, contradicted or unverifiable claim), in plain words."""
    if signals is None:
        return ["Ezio unavailable"]
    out = []
    if signals.tier != "GREEN":
        out.append(f"tier {signals.tier} {signals.score:g}")
    for d in ("tone", "bias", "agreement"):
        v = getattr(signals, d)
        if v < 80:
            out.append(f"{d} {v:g} ({'critical' if v < 60 else 'warning'})")
    for c in signals.claims:
        if c.get("verdict") in ("contradicted", "unverifiable"):
            out.append(f"claim {c.get('verdict')}: {c.get('text')}")
    if signals.accuracy_status == "unavailable":
        out.append("fact-check unavailable")
    return out


def category(flags, gate_decision, findings, dominated=False):
    substantive = [f for f in findings if f.get("level") == BLOCK and f.get("rule") not in ("overconfidence", "4 hype")]
    if gate_decision == APPROVE and dominated:
        return "Approved, but not the best option: a better one exists on every objective"
    if not flags and gate_decision == APPROVE:
        return "Clean: Ezio and the gate agree"
    if not flags and gate_decision != APPROVE:
        return "Ezio clean, gate catches it: confident mistake"
    if flags and substantive:
        return "Ezio flags the wording, not enough: the gate finds the real problem"
    if flags and gate_decision == HOLD:
        return "Ezio flag, gate holds for inspection"
    return "Ezio flags it and the gate blocks"


def _ezio_card(signals, error=None):
    if signals is None:
        return {"status": "unavailable", "error": error}
    return {"status": "ok", "tier": signals.tier, "score": signals.score, "summary": signals.summary,
            "accuracy": signals.accuracy_status, "tone": signals.tone, "bias": signals.bias, "agreement": signals.agreement,
            "claims": [{"text": c.get("text"), "verdict": c.get("verdict")} for c in signals.claims]}


def _run_option_draft(scn, draft, profiles, table):
    check = check_rationale(scn, draft["option"], draft["text"])
    try:
        signals = core.parse_signals(analyze(draft["text"]))
        led, _ = core.apply_policy(signals, check, core.POLICY)
        level, reasons, card = led.level, led.decisive(), _ezio_card(signals)
    except EzioError as e:
        signals, card = None, _ezio_card(None, str(e))
        level = BLOCK if any(f.level == BLOCK for f in check.findings) else core.POLICY["on_ezio_failure"]
        reasons = [f.message for f in check.findings if f.level == level] or [f"Ezio unavailable ({e}); held for a person."]
    row = next(r for r in table if r["key"] == draft["option"])
    util = {pk: {"utility": p["utility"].get(draft["option"]), "best": p["best"],
                 "regret": (round(p["utility"][p["best"]] - p["utility"][draft["option"]], 4) if row["feasible"] and p["best"] else None)}
            for pk, p in profiles.items()}
    findings = [f.as_dict() for f in check.findings]
    flags = ezio_flags(signals)
    return {"id": draft["id"], "text": draft["text"], "option": draft["option"], "option_label": row["label"],
            "note": draft.get("note", ""), "ezio_flags": flags, "category": category(flags, level, findings, bool(row.get("dominated_by"))),
            "feasible": row["feasible"], "dominated_by": row.get("dominated_by"), "metrics": row["metrics"],
            "gate": {"decision": level, "reason": " ".join(dict.fromkeys(reasons)),
                     "findings": [f.as_dict() for f in check.findings], "checked": check.verified, "could_not_check": check.unchecked},
            "baseline": core.baseline_decision(signals), "ezio": card, "utility": util}


# ---------------------------------------------------------------- persona scenarios (customer support)

def _run_persona_draft(scn, draft, context):
    r = core.decide(draft["text"], context, scn["persona"], audit_log=False)
    signals = None
    try:
        signals = core.parse_signals(r.ezio)
    except Exception:  # noqa: BLE001 - Ezio failure response
        pass
    w = r.welfare or {}
    flags = ezio_flags(signals) if signals else ["Ezio unavailable"]
    util = {pk: {"utility": p["utility"].get(w.get("chosen")), "best": p["best"], "regret": p["regret"]}
            for pk, p in (w.get("profiles") or {}).items()}
    return {"id": draft["id"], "text": draft["text"], "option": w.get("chosen"), "option_label": w.get("chosen_label"),
            "note": draft.get("note", ""), "ezio_flags": flags, "category": category(flags, r.decision, r.findings, w.get("status") == "dominated"),
            "feasible": r.decision != BLOCK, "dominated_by": w.get("dominated_by"),
            "gate": {"decision": r.decision, "reason": r.reason, "findings": r.findings,
                     "checked": r.coverage.get("checked", []), "could_not_check": r.coverage.get("could_not_check", [])},
            "baseline": r.baseline, "ezio": _ezio_card(signals, r.ezio.get("error") if isinstance(r.ezio, dict) else None),
            "utility": util, "welfare_note": w.get("note")}


def run(sid, preferences=None):
    scn = load(sid)
    prefs = preferences if isinstance(preferences, dict) else None
    if scn["kind"] == "options":
        profiles = {k: dict(v) for k, v in scn["profiles"].items()}
        if prefs and isinstance(prefs.get("weights"), dict):
            profiles["custom"] = {"label": str(prefs.get("label") or "Your weights"), "weights": prefs["weights"]}
        table, prof = option_table(scn, profiles)
        with ThreadPoolExecutor(max_workers=8) as pool:
            drafts = list(pool.map(lambda d: _run_option_draft(scn, d, prof, table), scn["drafts"]))
        return {"scenario": {k: scn.get(k) for k in ("id", "title", "kind", "situation", "disclaimer", "action", "rules_note")},
                "guardrails": scn["guardrails"], "objectives": scn["objectives"], "options": table, "profiles": prof, "surface": option_surface(scn), "drafts": drafts}
    context = dict(scn.get("context", {}))
    if prefs:
        context["preferences"] = prefs
    with ThreadPoolExecutor(max_workers=8) as pool:
        drafts = list(pool.map(lambda d: _run_persona_draft(scn, d, context), scn["drafts"]))
    sot = core.load_source_of_truth(scn["persona"])
    from checks.customer_support import _plans  # the persona's own option data
    over = welfare.scenario_surface(context["plan"], context.get("customer_tenure_months"), _plans(sot), sot["policies"], context)
    return {"scenario": {k: scn.get(k) for k in ("id", "title", "kind", "situation", "disclaimer", "action")},
            "objectives": [{"key": k, "label": v} for k, v in welfare.PREFS["objectives"].items()],
            "options": over["options"], "profiles": over["profiles"], "surface": over["surface"], "drafts": drafts}
