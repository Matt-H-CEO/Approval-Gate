"""Scenario lab: run a set of AI drafts through the same gate, next to the starter baseline, and score
each proposed action against stakeholder preference profiles (multi-objective utility).

Two kinds of scenario file in wrapper/scenarios/:
  "persona": drafts for a supplied persona (customer_support); each runs through wrapper.decide().
  "options": a mock decision (aviation dispatch) where the AI proposes one option plus a rationale.
             Guardrails are hard constraints; objectives and profiles come from the file;
             the rationale's numbers are checked against the file's facts; Ezio scores the rationale.

Dry run: the lab never runs the gated action. Advisory utility never changes the gate's decision.
"""
import hashlib
import json
import re
import time
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
_DERIVED_AFTER = re.compile(r"^[\s)\]]{0,2}(?:of fuel )?(below|above|over|under|short|more than|less than|of\b|margin|buffer|left|remaining|extra)", re.I)
_DERIVED_BEFORE = re.compile(r"(totals?|plus|leaving|leaves|by|uses all|use of|=)\s*$", re.I)
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
            detail = f"{o[g['option_field']]:,} lb on board vs {f[g['fact']]:,} lb needed"
        elif g["type"] == "max_value":
            ok = o[g["option_field"]] <= f[g["fact"]]
            detail = f"{o[g['option_field']]:,} lb vs a {f[g['fact']]:,} lb limit"
        elif g["type"] == "duty":
            left = f[g["remaining_fact"]] - o.get("delay_min", 0)
            ok = bool(o.get("fresh_crew")) or left >= f[g["needed_fact"]]
            detail = "fresh crew" if o.get("fresh_crew") else f"{left:g} minutes of work time left after the delay vs {f[g['needed_fact']]:g} needed"
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
                label = (facts[name].get("plain") if name in facts else None) or {"option.fuel_lb": "fuel load", "option.delay_min": "delay",
                         "option.safety_margin_min": "safety cushion"}.get(name, name.replace("option.", "").replace("_", " "))
                derived_cue = bool(_DERIVED_AFTER.search(s[x.end():x.end() + 18]) or _DERIVED_BEFORE.search(s[max(0, x.start() - 14):x.start()]))
                other = (_other_option_value(scn, s, unit, val) or _named_fact_value(scn, s, unit, val)
                         or _option_by_value(scn, s[x.end():x.end() + 12], unit, val))
                if abs(val - exp) <= max(1.0, tol * abs(exp)):
                    res.verified.append(f"{label} {x.group(0)} ({src})")
                elif other:
                    res.verified.append(f"{x.group(0)} matches {other}")
                elif derived_cue and _is_derived(scn, unit, val):
                    res.verified.append(f"{x.group(0)} is a difference or total of known figures")
                else:
                    res.findings.append(Finding(BLOCK, f"The AI says {x.group(0)} for the {label}; the flight data says {exp:,g} {unit}.",
                                                "facts", x.group(0), f"{exp:,g} {unit}", src, s))
        low = s.lower()
        for w in scn.get("hold_language", []):
            if re.search(r"\b" + re.escape(w) + r"\b", low):
                res.findings.append(Finding(HOLD, f"Sounds overconfident (\"{w}\"). In a safety decision, a person should confirm.",
                                            "overconfidence", w, "calibrated wording", "scenario hold_language", s))
                break
    for g in guardrails(scn, option_key):
        if not g["ok"]:
            claims_ok = bool(_COMPLIANCE.search(norm))
            res.findings.append(Finding(BLOCK, f"Breaks a rule: {g['label']} ({g['detail']})."
                                        + (" The AI says it's fine; it isn't." if claims_ok else ""),
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
        return "Allowed, but not the best choice: another option is better on every count"
    if not flags and gate_decision == APPROVE:
        return "Fine: Ezio and HoldPoint both approve"
    if not flags and gate_decision != APPROVE:
        return "Ezio saw no problem; HoldPoint caught a confident mistake"
    if flags and substantive:
        return "Ezio flagged the wording but missed the real problem; HoldPoint caught it"
    if flags and gate_decision == HOLD:
        return "Ezio raised a flag; HoldPoint holds it for a person to check"
    return "Ezio and HoldPoint both caught it"


def _known(scn, unit):
    """Every base figure of a unit: facts plus every option's fields and computed margins."""
    vals = [v["value"] for v in scn["facts"].values() if v["unit"] == unit]
    for key, o in scn["options"].items():
        if unit == "lb":
            vals.append(o["fuel_lb"])
        else:
            vals += [o["delay_min"], metrics(scn, key)["safety_margin_min"]]
            vals.append(_facts(scn)["flight_time_min"] + o["delay_min"])          # duty used
            vals.append(_facts(scn)["crew_duty_remaining_min"] - o["delay_min"])  # duty left after delay
    return [v for v in vals if v is not None]


def _is_derived(scn, unit, val):
    base = _known(scn, unit)
    cands = set(base) | {abs(a - b) for a in base for b in base} | {a + b for a in base for b in base}
    if unit == "lb":
        cands |= {o["fuel_lb"] - _facts(scn)["required_fuel_lb"] for o in scn["options"].values()}
    return any(abs(val - c) <= max(1.0, 0.01 * abs(c)) for c in cands)


def _option_by_value(scn, after, unit, val):
    """'15,400 lb options ...': a figure naming options by their value."""
    if not re.match(r"^\s*options?\b", after, re.I):
        return None
    for key, o in scn["options"].items():
        f = o["fuel_lb"] if unit == "lb" else o["delay_min"]
        if abs(val - f) <= 0.5:
            return f"options at {val:,g} {unit}"
    return None


def _named_fact_value(scn, sentence, unit, val):
    """A figure exactly equal to another fact whose keyword the sentence uses (attribution was ambiguous)."""
    low = sentence.lower()
    for k, v in scn["facts"].items():
        if v["unit"] == unit and abs(val - v["value"]) <= 0.5 and any(kw.lower() in low for kw in v["keywords"]):
            return k.replace("_", " ")
    return None


def _other_option_value(scn, sentence, unit, val):
    """A figure that belongs to another option the sentence names (e.g. 'max_fuel (+25 min)')."""
    low = sentence.lower()
    for key, o in scn["options"].items():
        if key.lower() in low or key.replace("_", " ").lower() in low:
            fields = [o["fuel_lb"]] if unit == "lb" else [o["delay_min"], metrics(scn, key)["safety_margin_min"]]
            if any(abs(val - f) <= max(1.0, 0.06 * abs(f)) for f in fields):
                return f"option {key}"
    return None


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


# ---------------------------------------------------------------- live dispatch decisions (audited)

DISPATCH_LOG = "dispatch_actions.log"  # separate from actions.log, which must equal the email gate's APPROVE decisions


def _profiles_with(scn, preferences):
    profiles = {k: dict(v) for k, v in scn["profiles"].items()}
    key = scn.get("default_profile")
    if isinstance(preferences, dict) and isinstance(preferences.get("weights"), dict):
        profiles["custom"] = {"label": str(preferences.get("label") or "Custom weights"), "weights": preferences["weights"],
                              "why": "Entered up front on the page."}
        key = "custom"
    elif isinstance(preferences, dict) and preferences.get("profile") in profiles:
        key = preferences["profile"]
    elif isinstance(preferences, str) and preferences in profiles:
        key = preferences
    return profiles, key


def _release_text(scn, row):
    f, o, m = _facts(scn), scn["options"][row["key"]], row["metrics"]
    used = f["flight_time_min"] + o["delay_min"]
    label = o["label"][0].lower() + o["label"][1:]
    return (f"Recommend: {label}. That puts {o['fuel_lb']:,} lb on board, {m['safety_margin_min']:.0f} minutes of extra flying beyond the "
            f"{f['required_fuel_lb']:,} lb legal minimum, which already includes the 45-minute reserve. "
            f"The crew uses {used:g} of its {f['crew_duty_remaining_min']:g} remaining duty minutes.")


def dispatch_recommendation(scn, preferences):
    profiles, key = _profiles_with(scn, preferences)
    table, prof = option_table(scn, profiles)
    p = prof[key]
    best = next(r for r in table if r["key"] == p["best"])
    front = [r for r in table if r["feasible"] and r.get("pareto_frontier")]
    runner = max((r for r in front if r is not best), key=lambda r: p["utility"][r["key"]], default=None)
    w = p["weights"]
    excluded = [{"label": r["label"], "reason": "Not allowed: breaks the rule \u201c" + "; ".join(g["label"] for g in r["guardrails"] if not g["ok"]) + "\u201d"}
                for r in table if not r["feasible"]]
    excluded += [{"label": r["label"], "reason": "Never the best choice: \u201c" + next(x['label'] for x in table if x['key'] == r['dominated_by'])
                  + "\u201d is at least as safe, sooner and cheaper"} for r in table if r["feasible"] and r.get("dominated_by")]
    trade = ""
    if runner:
        bm, rm = best["metrics"], runner["metrics"]
        trade = (f"Closest alternative: {runner['label'][0].lower() + runner['label'][1:]}. That gives {rm['safety_margin_min']:.0f} minutes of extra fuel "
                 f"instead of {bm['safety_margin_min']:.0f}, leaves {rm['delay_min']:.0f} minutes late instead of {bm['delay_min']:.0f}, "
                 f"and costs ${rm['cost_usd']:,.0f} instead of ${bm['cost_usd']:,.0f}.")
    return {
        "preferences": {"profile": key, "label": p["label"], "weights": w, "why": profiles[key].get("why", ""),
                        "stated": bool(preferences)},
        "choice": best["key"], "choice_label": best["label"],
        "why": (f"With these priorities (safety {w['safety_margin_min']:.0%}, schedule {w['delay_min']:.0%}, cost {w['cost_usd']:.0%}), "
                f"the best choice is: {best['label'][0].lower() + best['label'][1:]}. It breaks no rule, and no other legal option is "
                f"better on safety, schedule and cost at once. It scores {p['utility'][best['key']] * 100:.0f} out of 100 for this weighting."),
        "tradeoff": trade,
        "frontier": [{"key": r["key"], "label": r["label"], "metrics": r["metrics"], "score": p["utility"].get(r["key"])} for r in front],
        "excluded": excluded,
        "suggested_release": {"option": best["key"], "rationale": _release_text(scn, best)},
    }


def decide_dispatch(option, rationale, preferences=None, context=None, audit_log=True):
    """Gate an AI dispatch recommendation (option + rationale) the same way the email gate works, and audit it."""
    import hashlib
    import time
    from datetime import datetime, timezone
    import audit
    scn = load("aviation_dispatch")
    if option not in scn["options"]:
        raise ValueError("unknown option")
    check = check_rationale(scn, option, rationale)
    try:
        response = analyze(rationale)
        signals = core.parse_signals(response)
        led, _ = core.apply_policy(signals, check, core.POLICY)
        level, status = led.level, "ok"
        reasons = led.decisive()
    except EzioError as e:
        response, signals, status = {"error": str(e), "response": e.response}, None, "unavailable"
        level = BLOCK if any(f.level == BLOCK for f in check.findings) else core.POLICY["on_ezio_failure"]
        reasons = [f.message for f in check.findings if f.level == level] or [f"Ezio unavailable ({e}); held for the dispatcher."]
    cov = {"checked": check.verified, "could_not_check": check.unchecked + (["Ezio unavailable"] if signals is None else [])}
    r = core.Decision(level, " ".join(dict.fromkeys(reasons)) or "Every figure matched the flight data and no rule is broken.",
                      [f"[{f.level}] {f.message}" for f in check.findings] + [core._coverage_line(cov)], response,
                      findings=[f.as_dict() for f in check.findings], coverage=cov, ezio_status=status,
                      baseline=core.baseline_decision(signals))
    r.consequence = ("Release signed (mock): the flight departs as proposed." if level == APPROVE else
                     "Not signed. Escalated to the dispatcher: nothing departs until a person decides.")
    r.recommendation = dispatch_recommendation(scn, preferences)
    r.preferences = r.recommendation["preferences"]
    r.ezio_flags, r.issues = core.issue_ledger(r, check)
    for i in r.issues:  # HoldPoint's overconfidence rule overlaps Ezio's tone/bias/agreement flags
        if i["caught_by"] == "HoldPoint only" and "overconfident" in i["issue"].lower() and any(k in " ".join(r.ezio_flags) for k in ("Tone", "Bias", "Agreement")):
            i["caught_by"] = "Ezio + HoldPoint"
    if any(i["caught_by"] == "Ezio + HoldPoint" for i in r.issues):
        r.issues = [i for i in r.issues if not (i["caught_by"] == "Ezio" and i["source"] == "Ezio dimensions")]
    r.decision_id = hashlib.sha1(f"{time.time_ns()}|{rationale}".encode()).hexdigest()[:12]
    r.four_tests = core.four_tests(r)
    executed = level == APPROVE
    if audit_log:
        ctx = dict(context or {}, option=option, scenario="aviation_dispatch (MOCK)")
        audit.record("aviation_dispatch", rationale, ctx, r)
        if executed:
            with (audit.LOG.parent / DISPATCH_LOG).open("a") as fh:
                fh.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": "sign_dispatch_release",
                                     "option": option, "decision_id": r.decision_id}) + "\n")
    return r, executed

# ---------------------------------------------------------------- one live dispatch release (aviation)

def _dispatch_issues(findings, flags):
    """Every issue in the rationale and who caught it: Ezio, HoldPoint's rule check, or both."""
    issues = []
    for f in findings:
        issues.append({"issue": f.get("message", ""), "level": f.get("level", ""),
                       "expected": f.get("expected", "") or "", "source": f.get("source", "") or "",
                       "caught_by": "HoldPoint only"})
    for fl in flags:
        txt = fl["issue"] if isinstance(fl, dict) else str(fl)
        kind = fl.get("kind", "") if isinstance(fl, dict) else ""
        issues.append({"issue": f"Ezio flagged {txt}: its own read of the wording, not of the facts", "level": "HOLD_FOR_REVIEW" if ("critical" in txt or kind == "tier") else "NOTE",
                       "expected": "", "source": "Ezio's own read of the wording", "caught_by": "Ezio"})
    return issues


def _suggested_release(scn, choice, prof, table):
    """A rationale for the recommended option that the rule check already accepts."""
    for d in scn["drafts"]:
        if d.get("option") != choice:
            continue
        try:
            r = _run_option_draft(scn, d, prof, table)
        except Exception:  # noqa: BLE001
            continue
        if r["gate"]["decision"] == "APPROVE" and not r["ezio_flags"]:
            return {"option": choice, "rationale": d["text"]}
    row = next((r for r in table if r["key"] == choice), None)
    if not row:
        return {"option": choice, "rationale": ""}
    bits = "; ".join(f"{o['label'].lower()} {row['metrics'][o['key']]}" for o in scn["objectives"] if o["key"] in row["metrics"])
    return {"option": choice, "rationale": f"Recommend {row['label'].lower()}: {bits}. Every hard limit is met."}


def dispatch(sid, option_key, rationale, preferences=None, context=None):
    """Check one proposed release (an option plus the rationale a person would sign)."""
    scn = load(sid)
    if scn.get("kind") != "options":
        raise ValueError("dispatch is only available for option scenarios")
    if option_key not in scn["options"]:
        raise KeyError(option_key)
    profiles = {k: dict(v) for k, v in scn["profiles"].items()}
    prefs = preferences if isinstance(preferences, dict) else None
    pkey = "balanced"
    if prefs and isinstance(prefs.get("weights"), dict):
        profiles["custom"] = {"label": str(prefs.get("label") or "Your weights"), "weights": prefs["weights"]}
        pkey = "custom"
    elif prefs and prefs.get("profile") in profiles:
        pkey = prefs["profile"]
    table, prof = option_table(scn, profiles)
    run = _run_option_draft(scn, {"id": "live", "option": option_key, "text": rationale, "note": ""}, prof, table)
    chosen, gate = prof[pkey], run["gate"]
    issues = _dispatch_issues(gate["findings"], run["ezio_flags"])

    frontier = sorted(
        [{"key": r["key"], "label": r["label"], "metrics": r["metrics"],
          "score": round((chosen["utility"].get(r["key"]) or 0) * 100)}
         for r in table if r["feasible"] and not r.get("dominated_by")],
        key=lambda x: -x["score"])
    excluded = []
    for r in table:
        if not r["feasible"]:
            broken = "; ".join(g["label"] for g in r["guardrails"] if not g["ok"])
            excluded.append({"label": r["label"], "reason": f"ruled out by a hard limit that is never traded away: {broken}"})
        elif r.get("dominated_by"):
            other = next((x for x in table if x["key"] == r["dominated_by"]), None)
            excluded.append({"label": r["label"], "reason": f"another allowed option ({other['label'] if other else '?'}) is better on every count"})

    choice = chosen["best"]
    choice_row = next((r for r in table if r["key"] == choice), None)
    second = [f for f in frontier if f["key"] != choice]
    tradeoff = ""
    if choice_row and second:
        alt = second[0]
        diffs = [f"{o['label'].lower()} {alt['metrics'][o['key']]} instead of {choice_row['metrics'][o['key']]}"
                 for o in scn["objectives"] if o["key"] in alt["metrics"] and alt["metrics"][o["key"]] != choice_row["metrics"][o["key"]]]
        if diffs:
            tradeoff = f"{alt['label']} would give you {', '.join(diffs)}."
    pct = {k: f"{round(v * 100)}%" for k, v in chosen["weights"].items()}
    why = (f"Under '{chosen['label']}' ({', '.join(f'{k} {v}' for k, v in pct.items())}), "
           f"{choice_row['label'] if choice_row else '?'} scores "
           f"{round((chosen['utility'].get(choice) or 0) * 100)} out of 100 - the best of the options that break no hard limit "
           f"and that no other allowed option beats on every count.")

    did = hashlib.sha1(f"{time.time_ns()}|{option_key}|{rationale}".encode()).hexdigest()[:12]
    blocks = [f for f in gate["findings"] if f.get("level") == "BLOCK"]
    approved = gate["decision"] == "APPROVE"
    return {
        "decision": gate["decision"], "reason": gate["reason"], "decision_id": did,
        "consequence": ("Release signed (mock): the dispatcher may sign this release as written."
                        if approved else "Not signed. The release is held until a dispatcher decides."),
        "action_executed": approved,
        "option": option_key, "option_label": run["option_label"], "rationale": rationale,
        "metrics": run["metrics"], "feasible": run["feasible"],
        "ezio": run["ezio"], "ezio_flags": run["ezio_flags"], "issues": issues,
        "baseline": run["baseline"], "context": dict(context or {}),
        "four_tests": {
            "verified": {"ok": not blocks, "text": f"{len(gate.get('checked') or [])} figure(s) matched the flight's own numbers; {len(blocks)} contradicted."},
            "calibrated": {"ok": True, "text": ("Could not check: " + "; ".join(gate.get("could_not_check") or [])) if gate.get("could_not_check") else "Nothing left unchecked that this scenario's rules cover."},
            "overseeable": {"ok": True, "text": "A dispatcher must decide before anything is signed; the problem text is named and the consequence stated." if not approved else "Signed automatically: every checked figure matched."},
            "auditable": {"ok": True, "text": f"Decision {did} logged with the option, rationale, Ezio's full response, evidence, reason and time."},
        },
        "recommendation": _plain_recommendation(scn, preferences),
        "scenario": {k: scn.get(k) for k in ("id", "title", "situation", "disclaimer", "action", "rules_note")},
        "guardrails": scn["guardrails"], "objectives": scn["objectives"],
    }


def _plain_recommendation(scn, preferences):
    """The plain-language recommendation (dispatch_recommendation) in the shape the dispatch page reads."""
    rec = dispatch_recommendation(scn, preferences)
    rec["frontier"] = sorted([dict(f, score=round((f["score"] or 0) * 100)) for f in rec["frontier"]], key=lambda x: -x["score"])
    names = {"safety_margin_min": "safety", "delay_min": "schedule", "cost_usd": "cost"}
    w = rec["preferences"]["weights"]
    rec["preferences"]["weights_pct"] = {k: f"{round(v * 100)}%" for k, v in w.items()}
    rec["preferences"]["stated_text"] = ("Priorities set before the AI recommended anything: "
                                         + ", ".join(f"{names.get(k, k)} {round(v * 100)}%" for k, v in w.items()) + ".")
    return rec
