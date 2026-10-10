"""Multi-objective best-option check for the customer-support persona.

1. Guardrails (hard constraints): only offers the policy allows for THIS customer enter the set
   (loyalty needs Pro/Business and 12+ months; discounts never stack). Infeasible offers are BLOCKed
   by the checker before they get here.
2. Objectives per feasible offer: customer cost, customer flexibility, company 12-month revenue.
3. Pareto check: an offer is dominated if another is at least as good for the customer on cost,
   commitment and refund window, and strictly better on one.
4. Utility per stakeholder profile (preferences.json, or custom weights in context.preferences):
   U = sum(weight_k * normalized objective_k). Report the best offer and the email's regret.
5. Trade-off surface: which offer wins as the weights move (company share x customer cost share).

Advisory by default (policy.json welfare_mode): it informs the reviewer and never changes the scored decision.
Assumption: the loyalty discount applies to the monthly price (the policy does not say which billing).
"""
import json
from pathlib import Path

PREFS = json.loads((Path(__file__).resolve().parent / "preferences.json").read_text())
OBJECTIVES = ("customer_cost", "customer_flexibility", "company_revenue")


def options(plan, tenure, plans, pol, beliefs=None):
    beliefs = beliefs or PREFS["retention_beliefs"]
    p, disc = plans[plan], pol["discounts"]
    refund_m, refund_a = pol["refunds"]["monthly_plans_days"], pol["refunds"]["annual_plans_days"]
    loyal = disc["loyalty"]
    opts = [{"name": "list", "label": f"{plan} monthly at list price", "cost_pm": p["monthly"], "commit_months": 1, "refund_days": refund_m}]
    if tenure is not None and plan in loyal["applies_to"] and tenure >= loyal["min_tenure_months"]:
        opts.append({"name": "loyalty", "label": f"{plan} monthly with {loyal['percent']:g}% loyalty discount",
                     "cost_pm": round(p["monthly"] * (1 - loyal["percent"] / 100), 2), "commit_months": 1, "refund_days": refund_m})
    opts.append({"name": "annual", "label": f"{plan} billed annually ({disc['annual_billing']['percent']:g}% off)",
                 "cost_pm": p["annual_pm"], "commit_months": 12, "refund_days": refund_a})
    for o in opts:
        o["retention_belief"] = float(beliefs.get(o["name"], 1.0))
        o["revenue_12m"] = round(o["cost_pm"] * 12 * o["retention_belief"], 2)
    _normalize(opts)
    for o in opts:
        o["pareto_frontier"] = not any(_dominates(x, o) for x in opts if x is not o)
    return opts


def _minmax(values, higher_better):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0] * len(values)
    return [round((v - lo) / (hi - lo) if higher_better else (hi - v) / (hi - lo), 4) for v in values]


def _normalize(opts):
    cost = _minmax([o["cost_pm"] for o in opts], higher_better=False)
    refund = _minmax([o["refund_days"] for o in opts], higher_better=True)
    commit = _minmax([o["commit_months"] for o in opts], higher_better=False)
    rev = _minmax([o["revenue_12m"] for o in opts], higher_better=True)
    for o, c, r, m, v in zip(opts, cost, refund, commit, rev):
        o["scores"] = {"customer_cost": c, "customer_flexibility": round((r + m) / 2, 4), "company_revenue": v}


def _dominates(a, b):
    ge = a["cost_pm"] <= b["cost_pm"] and a["commit_months"] <= b["commit_months"] and a["refund_days"] >= b["refund_days"]
    gt = a["cost_pm"] < b["cost_pm"] or a["commit_months"] < b["commit_months"] or a["refund_days"] > b["refund_days"]
    return ge and gt


def _clean_weights(w):
    w = {k: max(0.0, float(w.get(k, 0) or 0)) for k in OBJECTIVES}
    total = sum(w.values()) or 1.0
    return {k: round(v / total, 4) for k, v in w.items()}


def utility(opts, weights):
    w = _clean_weights(weights)
    table = {o["name"]: round(sum(w[k] * o["scores"][k] for k in OBJECTIVES), 4) for o in opts}
    best = max(opts, key=lambda o: (table[o["name"]], -o["cost_pm"]))
    return w, table, best


def profiles_from_context(context):
    """Built-in profiles plus an optional custom one: context.preferences = {"weights": {...}, "label": "..."}."""
    profs = {k: dict(v) for k, v in PREFS["profiles"].items()}
    custom = context.get("preferences") if isinstance(context, dict) else None
    if isinstance(custom, dict) and isinstance(custom.get("weights"), dict):
        profs["custom"] = {"label": str(custom.get("label") or "Custom stakeholder weights"), "stakeholder": "custom",
                           "weights": custom["weights"], "why": "Entered on the persona page."}
    beliefs = dict(PREFS["retention_beliefs"])
    if isinstance(custom, dict) and isinstance(custom.get("retention_beliefs"), dict):
        for k in ("list", "loyalty", "annual"):
            v = custom["retention_beliefs"].get(k)
            if isinstance(v, (int, float)) and 0 <= v <= 1:
                beliefs[k] = float(v)
    return profs, beliefs


def surface(opts, steps=5):
    """Winner for each (company weight, customer cost share) cell: the trade-off surface."""
    grid = []
    vals = [round(i / (steps - 1), 2) for i in range(steps)]
    for wk in vals:
        row = []
        for a in vals:
            _, _, best = utility(opts, {"customer_cost": (1 - wk) * a, "customer_flexibility": (1 - wk) * (1 - a), "company_revenue": wk})
            row.append(best["name"])
        grid.append({"company_weight": wk, "winners_by_customer_cost_share": dict(zip([str(v) for v in vals], row))})
    return grid


def assess(offer, ctx_plan, tenure, plans, pol, context=None):
    chosen = offer.get("chosen")
    if not chosen or not ctx_plan or ctx_plan not in plans or ctx_plan == "Enterprise" or offer.get("plan") != ctx_plan:
        return None
    profs, beliefs = profiles_from_context(context or {})
    opts = options(ctx_plan, tenure, plans, pol, beliefs)
    pick = next((o for o in opts if o["name"] == chosen), None)
    if pick is None:
        return None
    by_profile = {}
    for key, prof in profs.items():
        w, table, best = utility(opts, prof["weights"])
        by_profile[key] = {"label": prof["label"], "weights": w, "utility": table, "best": best["name"],
                           "best_label": best["label"], "regret": round(table[best["name"]] - table[chosen], 4)}
    result = {"plan": ctx_plan, "tenure_months": tenure, "chosen": chosen, "chosen_label": pick["label"],
              "options": opts, "profiles": by_profile, "surface": surface(opts), "retention_beliefs": beliefs}
    better = [o for o in opts if _dominates(o, pick)]
    if better:
        best = min(better, key=lambda o: o["cost_pm"])
        gap = round(pick["cost_pm"] - best["cost_pm"], 2)
        result.update(status="dominated", dominated_by=best["label"], gap_per_month=gap, gap_per_year=round(gap * 12, 2),
                      note=(f"Best-option check: dominated. This customer qualifies for {best['label']} at ${best['cost_pm']:.2f}/mo "
                            f"on the same or better terms; the email leaves them paying ${gap:.2f}/mo (${gap * 12:.2f}/yr) more. "
                            f"Not the best offer under any stakeholder profile."))
    else:
        wins = [p["label"] for p in by_profile.values() if p["best"] == chosen]
        loses = [f"{p['label']} prefers {p['best_label']}" for p in by_profile.values() if p["best"] != chosen]
        result.update(status="frontier", note=("Best-option check: on the frontier (no offer beats it on every customer objective). "
                                               + (f"Best for: {', '.join(wins)}. " if wins else "")
                                               + (f"Trade-off: {'; '.join(loses)}." if loses else "")))
    return result


def scenario_surface(plan, tenure, plans, pol, context=None):
    """Offers, profile optima and the surface for a customer, without a draft (for the page's trade-off view)."""
    profs, beliefs = profiles_from_context(context or {})
    opts = options(plan, tenure, plans, pol, beliefs)
    out = {}
    for key, prof in profs.items():
        w, table, best = utility(opts, prof["weights"])
        out[key] = {"label": prof["label"], "weights": w, "utility": table, "best": best["name"], "best_label": best["label"]}
    return {"plan": plan, "tenure_months": tenure, "options": opts, "profiles": out, "surface": surface(opts), "retention_beliefs": beliefs}


def resolve_preferences(context):
    """The stakeholder preferences stated up front: a named profile, or custom weights. Default from preferences.json."""
    prefs = context.get("preferences") if isinstance(context, dict) else None
    profs, beliefs = profiles_from_context(context or {})
    if isinstance(prefs, dict) and isinstance(prefs.get("weights"), dict):
        key = "custom"
    elif isinstance(prefs, dict) and prefs.get("profile") in profs:
        key = prefs["profile"]
    elif isinstance(prefs, str) and prefs in profs:
        key = prefs
    else:
        key = PREFS["default_profile"]
    prof = profs[key]
    return {"profile": key, "label": prof["label"], "weights": _clean_weights(prof["weights"]),
            "why": prof.get("why", ""), "retention_beliefs": beliefs, "stated": bool(prefs)}


def _reply_for(o, plan, tenure, pol):
    refund_m, refund_a = pol["refunds"]["monthly_plans_days"], pol["refunds"]["annual_plans_days"]
    if o["name"] == "loyalty":
        return (f"Thanks for being with us for {tenure:g} months. You qualify for our {pol['discounts']['loyalty']['percent']:g}% loyalty discount, "
                f"which brings {plan} to ${o['cost_pm']:.2f} per month, still billed monthly with a {refund_m:g}-day refund window on your first payment.")
    if o["name"] == "annual":
        return (f"If you switch to annual billing, {plan} is ${o['cost_pm']:.2f} per month, billed annually; "
                f"annual plans can be refunded within {refund_a:g} days of the first payment.")
    return f"Your {plan} plan is ${o['cost_pm']:.2f} per month."


def recommend(plan, tenure, plans, pol, context=None):
    """Pareto-optimal, policy-compliant recommendation under the stakeholder preferences stated up front."""
    if not plan or plan not in plans or plan == "Enterprise":
        return None
    pref = resolve_preferences(context or {})
    profs, beliefs = profiles_from_context(context or {})
    opts = options(plan, tenure, plans, pol, beliefs)
    frontier = [o for o in opts if o["pareto_frontier"]]
    w, table, _ = utility(frontier, pref["weights"])
    best = max(frontier, key=lambda o: (table[o["name"]], -o["cost_pm"]))
    others = [o for o in frontier if o is not best]
    alt = max(others, key=lambda o: table[o["name"]]) if others else None
    trade = []
    if alt:
        if alt["cost_pm"] < best["cost_pm"]:
            trade.append(f"{alt['label']} is ${best['cost_pm'] - alt['cost_pm']:.2f}/mo cheaper")
        if alt["refund_days"] > best["refund_days"] or alt["commit_months"] < best["commit_months"]:
            trade.append(f"{alt['label']} keeps a {alt['refund_days']}-day refund window and {alt['commit_months']}-month commitment")
    excluded = [f"{o['label']}: dominated by a cheaper offer on the same terms" for o in opts if not o["pareto_frontier"]]
    if tenure is None or plan not in pol["discounts"]["loyalty"]["applies_to"] or (tenure or 0) < pol["discounts"]["loyalty"]["min_tenure_months"]:
        excluded.append("Loyalty discount: not eligible (needs Pro or Business and 12+ months)")
    excluded.append("Stacked discounts: not allowed by policy")
    return {
        "preferences": pref,
        "choice": best["name"], "choice_label": best["label"], "cost_pm": best["cost_pm"],
        "suggested_reply": _reply_for(best, plan, tenure, pol) + (f" {_reply_for(alt, plan, tenure, pol)}" if alt and alt["name"] != "list" else ""),
        "why": (f"Under '{pref['label']}' (weights: cost {w['customer_cost']:.0%}, flexibility {w['customer_flexibility']:.0%}, "
                f"company revenue {w['company_revenue']:.0%}), {best['label']} scores {table[best['name']] * 100:.0f}/100, "
                f"the best of the Pareto-optimal, policy-compliant offers for this customer."),
        "tradeoff": "; ".join(trade),
        "frontier": [{"name": o["name"], "label": o["label"], "cost_pm": o["cost_pm"], "commit_months": o["commit_months"],
                      "refund_days": o["refund_days"], "score": table[o["name"]]} for o in frontier],
        "excluded": excluded,
    }
