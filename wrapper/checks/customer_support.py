"""Customer support lead (Lumen Cloud, fictional): check an outbound email against
price_sheet.csv and policies.json, following personas/customer_support/business_rules.md.

Rule 1 facts -> BLOCK. Rule 2 discount eligibility / stacking -> BLOCK.
Rule 3 promises beyond agent authority -> HOLD. Rule 4 hype or guarantees -> HOLD.
Every expected value is read from the data files; nothing here matches practice text.
Calibration: a figure tied to a plan the draft names explicitly can BLOCK; a figure we could only
tie to the customer's plan from context, or an ambiguous one, is HOLD (a human checks it).
"""
import re

import textnorm as tn
from findings import BLOCK, HOLD, CheckResult, Finding
import welfare

PLANS = ("Starter", "Pro", "Business", "Enterprise")
_PLAN = re.compile(r"\b(Starter|Pro|Business|Enterprise)\b|\b(starter|pro|business|enterprise)(?=\s+(?:plan|tier|customers?|subscription|account)s?\b)")
_YOUR_PLAN = re.compile(r"\byour\s+(?:current\s+|existing\s+)?(?:plan|subscription|tier|account)\b", re.I)
_ANNUAL = re.compile(r"\b(annual|annually|yearly|per year|a year|/\s?yr|12-month|twelve-month|year-long)\b", re.I)
_MONTHLY_BILL = re.compile(r"\b(monthly|month-to-month)\b", re.I)
_LOYALTY = re.compile(r"\bloyal(?:ty)?\b", re.I)
_DISCOUNT_CTX = re.compile(r"\b(discount\w*|off|save\w*|saving\w*|promo\w*|coupon\w*|deal|cheaper|reduc\w*|loyalty)\b", re.I)
_STACK = re.compile(r"\b(combine\w*|stack\w*|together|on top of|plus|both|in addition to|as well as)\b", re.I)
_SAVINGS_BEFORE = re.compile(r"\b(save|saves|saving|savings|off|discount|credit|cheaper|less|reduce\w*|knock\w*)\b[^$\d]{0,20}$", re.I)
_SAVINGS_AFTER = re.compile(r"^[^.]{0,15}\b(off|savings?|discount|less|cheaper|credit)\b", re.I)
_UNLIMITED = re.compile(r"\b(any ?time|anytime|lifetime|forever|no time limit|whenever you (?:like|want)|at any point|no questions asked, ever)\b", re.I)
_FIRST_PERIOD = re.compile(r"\bfirst (year|month|week|quarter)\b", re.I)
_NEGATION = re.compile(r"\b(no|not|n't|without|never|isn't|doesn't|does not|don't|do not)\b", re.I)
_RESPONSE = re.compile(r"\b(respon\w*|repl(?:y|ies|ied)|get back to you|hear back|first reply|answer\w*|turnaround|sla)\b", re.I)
_COMMIT = re.compile(r"\b(i'll|i will|we'll|we will|i promise|we promise|i guarantee|we guarantee|i can promise|make sure|going to|i've arranged|we've arranged|i can get)\b", re.I)
_SCOPE = re.compile(r"\b(feature\w*|engineer\w*|ship\w*|release\w*|roadmap|build\w*|built|export\w*|integration\w*|fix\w*|bug\w*|deliver\w*|exception\w*|waive\w*|credit\w*|extend\w*|extension|custom|priorit\w*|free month|upgrade you|override)\b", re.I)
_EXCEPTION = re.compile(r"\b(make an exception|one-time exception|waive (?:the|your|this)|override the policy|bend the rules|off the books)\b", re.I)
_STRONG_HYPE = re.compile(r"\b(guarantee[ds]?|risk-free|unbeatable|you(?:'ll| will) love|best deal|lowest price ever|never pay again|100% satisf\w*)\b", re.I)
_MILD_HYPE = re.compile(r"\b(amazing|incredible|awesome|fantastic|unbelievable|spectacular|life-changing|revolutionary|insane|mind-blowing|best ever)\b", re.I)


def _plans(sot):
    out = {}
    for row in sot["price_sheet"]:
        out[row["plan"]] = {
            "monthly": float(row["monthly_price_usd"]),
            "annual_pm": float(row["annual_price_per_month_usd"]),
            "response_h": float(row["first_response_hours"]),
            "trial_days": float(row["free_trial_days"]),
        }
    return out


def _canon(m):
    return (m.group(1) or m.group(2)).capitalize()


def check(text, context, sot):
    plans, pol = _plans(sot), sot["policies"]
    disc = pol["discounts"]
    loyal_pct, annual_pct = float(disc["loyalty"]["percent"]), float(disc["annual_billing"]["percent"])
    loyal_plans, loyal_min = disc["loyalty"]["applies_to"], float(disc["loyalty"]["min_tenure_months"])
    refund_m, refund_a = float(pol["refunds"]["monthly_plans_days"]), float(pol["refunds"]["annual_plans_days"])
    trial_days, trial_plans = float(pol["free_trial"]["days"]), pol["free_trial"]["plans"]

    ctx_plan = context.get("plan") if context.get("plan") in plans else None
    tenure = context.get("customer_tenure_months")
    tenure = float(tenure) if isinstance(tenure, (int, float)) and not isinstance(tenure, bool) else None

    res = CheckResult()
    offer = {"chosen": None, "plan": None}
    norm = tn.normalize(text)

    for s in tn.sentences(norm):
        low = s.lower()
        mentions = [(m.start(), _canon(m), False) for m in _PLAN.finditer(s)]
        if ctx_plan:
            mentions += [(m.start(), ctx_plan, True) for m in _YOUR_PLAN.finditer(s)]
        mentions.sort()

        def plan_at(pos):
            before = [m for m in mentions if m[0] <= pos]
            if before:
                return before[-1][1], before[-1][2]
            after = [m for m in mentions if m[0] > pos]
            if after:
                return after[0][1], after[0][2]
            if ctx_plan and re.search(r"\b(you|your)\b", low):
                return ctx_plan, True
            return None, False

        annual_ctx = bool(_ANNUAL.search(s))
        loyalty_ctx = bool(_LOYALTY.search(s))

        # ---- rule 1: prices --------------------------------------------------------------
        for m in tn.money(s):
            before, after = s[max(0, m["start"] - 30):m["start"]], s[m["end"]:m["end"] + 20]
            plan, inferred = plan_at(m["start"])
            if _SAVINGS_BEFORE.search(before) or _SAVINGS_AFTER.search(after):
                _check_savings(res, m, plan, plans, loyal_pct, annual_pct)
                continue
            if plan == "Enterprise":
                res.findings.append(Finding(BLOCK, f"Enterprise pricing is custom (contact sales); the email quotes {m['text']}.",
                                            "1 facts", m["text"], "custom pricing via sales", "policies.json enterprise", s))
                continue
            if plan is None:
                every = [v for p in plans.values() for v in (p["monthly"], p["annual_pm"])]
                if any(tn.close(m["value"], v, rel=0) for v in every):
                    res.verified.append(f"{m['text']} is a listed price (plan not named)")
                else:
                    res.findings.append(Finding(HOLD, f"{m['text']} is not tied to a plan and matches no listed price.",
                                                "1 facts", m["text"], "a price from price_sheet.csv", "price_sheet.csv", s))
                continue
            p = plans[plan]
            if m["period"] == "year":
                allowed = {"annual billing, per year": p["annual_pm"] * 12, "monthly billing over 12 months": p["monthly"] * 12}
            else:
                allowed = {"monthly price": p["monthly"]}
                if annual_ctx:
                    allowed["annual billing, per month"] = p["annual_pm"]
                if loyalty_ctx:
                    allowed["with loyalty discount"] = round(p["monthly"] * (1 - loyal_pct / 100), 2)
            hit = next((k for k, v in allowed.items() if tn.close(m["value"], v, rel=0)), None)
            if hit:
                res.verified.append(f"{plan} {hit} {tn.fmt_money(allowed[hit])} (price_sheet.csv)")
                if plan == ctx_plan:
                    offer.update(plan=plan, chosen="annual" if "annual" in hit else "loyalty" if "loyalty" in hit else "list")
                continue
            other = {"annual billing, per month": p["annual_pm"], "with loyalty discount": round(p["monthly"] * (1 - loyal_pct / 100), 2)}
            near = next((k for k, v in other.items() if tn.close(m["value"], v, rel=0)), None)
            exp = " or ".join(f"{k} {tn.fmt_money(v)}" for k, v in allowed.items())
            if near:
                res.findings.append(Finding(HOLD, f"{plan} at {m['text']} only holds for {near}, and the email doesn't say so.",
                                            "1 facts", m["text"], exp, "price_sheet.csv", s))
            else:
                lvl = HOLD if inferred else BLOCK
                res.findings.append(Finding(lvl, f"{plan} is {exp}; the email says {m['text']}.",
                                            "1 facts", m["text"], exp, "price_sheet.csv", s))

        # ---- rules 1-2: discounts, eligibility, stacking ------------------------------------
        pcts = tn.percents(s) if _DISCOUNT_CTX.search(s) else []
        stacked = (loyalty_ctx and annual_ctx and _STACK.search(s)) or any(
            tn.close(x["value"], v, rel=0) for x in pcts
            for v in (loyal_pct + annual_pct, round(100 - (100 - loyal_pct) * (100 - annual_pct) / 100, 1)))
        if stacked and (pcts or "discount" in low):
            res.findings.append(Finding(BLOCK, "Discounts cannot be stacked (loyalty and annual billing are either/or).",
                                        "2 stacking", s.strip(), "stacking_allowed: false", "policies.json discounts", s))
            pcts = []
        for x in pcts:
            near_txt = s[max(0, x["start"] - 40):x["end"] + 40].lower()
            is_loyal = bool(_LOYALTY.search(near_txt)) or (loyalty_ctx and not annual_ctx)
            is_annual = bool(_ANNUAL.search(near_txt)) or (annual_ctx and not loyalty_ctx)
            if is_loyal and not tn.close(x["value"], loyal_pct, rel=0):
                res.findings.append(Finding(BLOCK, f"The loyalty discount is {loyal_pct:g}%; the email says {x['text']}.",
                                            "1 facts", x["text"], f"{loyal_pct:g}%", "policies.json discounts.loyalty", s))
                continue
            if is_annual and not is_loyal and not tn.close(x["value"], annual_pct, rel=0):
                res.findings.append(Finding(BLOCK, f"The annual-billing discount is {annual_pct:g}%; the email says {x['text']}.",
                                            "1 facts", x["text"], f"{annual_pct:g}%", "policies.json discounts.annual_billing", s))
                continue
            if not is_loyal and not is_annual:
                if any(tn.close(x["value"], v, rel=0) for v in (loyal_pct, annual_pct)):
                    res.verified.append(f"{x['text']} matches a published discount")
                else:
                    res.findings.append(Finding(BLOCK, f"No {x['text']} discount exists (loyalty {loyal_pct:g}%, annual billing {annual_pct:g}%).",
                                                "1 facts", x["text"], f"{loyal_pct:g}% or {annual_pct:g}%", "policies.json discounts", s))
                continue
            if is_annual and not is_loyal:
                res.verified.append(f"annual-billing discount {annual_pct:g}% (policies.json)")
                if ctx_plan and re.search(r"\b(you|your)\b", low):
                    offer.update(plan=ctx_plan, chosen="annual")
                continue
            # loyalty at the right percent: plan and tenure eligibility
            plan, inferred = plan_at(x["start"])
            if plan and plan not in loyal_plans:
                res.findings.append(Finding(BLOCK, f"The loyalty discount applies only to {' and '.join(loyal_plans)}, not {plan}.",
                                            "2 eligibility", x["text"], ", ".join(loyal_plans), "policies.json discounts.loyalty.applies_to", s))
                continue
            stated_min = [d for d in tn.durations(s) if d["unit"] in ("month", "year")]
            bad_min = [d for d in stated_min if not tn.close(d["days"] / 30 if d["unit"] == "month" else d["days"] / 365 * 12, loyal_min, rel=0)]
            if bad_min:
                res.findings.append(Finding(BLOCK, f"Loyalty needs {loyal_min:g}+ months of tenure; the email says {bad_min[0]['text']}.",
                                            "2 eligibility", bad_min[0]["text"], f"{loyal_min:g} months", "policies.json discounts.loyalty.min_tenure_months", s))
                continue
            if re.search(r"\b(you|your)\b", low):
                if ctx_plan is None or tenure is None:
                    res.findings.append(Finding(HOLD, "The email offers this customer the loyalty discount, but their plan and tenure aren't known.",
                                                "2 eligibility", x["text"], f"{' or '.join(loyal_plans)} and {loyal_min:g}+ months", "context", s))
                elif ctx_plan not in loyal_plans or tenure < loyal_min:
                    res.findings.append(Finding(BLOCK, f"Customer is on {ctx_plan} with {tenure:g} months; loyalty needs {' or '.join(loyal_plans)} and {loyal_min:g}+ months.",
                                                "2 eligibility", x["text"], f"{' or '.join(loyal_plans)}, {loyal_min:g}+ months", "policies.json + context", s))
                else:
                    res.verified.append(f"loyalty {loyal_pct:g}%: customer on {ctx_plan}, {tenure:g} months (eligible)")
                    offer.update(plan=ctx_plan, chosen=offer["chosen"] or "loyalty")
            else:
                res.verified.append(f"loyalty discount {loyal_pct:g}% (policies.json)")

        # ---- rule 1: refunds ----------------------------------------------------------------
        if re.search(r"\b(refund\w*|money back|money-back)\b", low):
            billing = "annual" if re.search(r"\b(annual|annually|yearly)\b", low) else "monthly" if _MONTHLY_BILL.search(s) else None
            allowed = {"annual": [refund_a], "monthly": [refund_m], None: [refund_m, refund_a]}[billing]
            exp = " or ".join(f"{v:g} days" for v in allowed) + (f" ({billing} plans)" if billing else "")
            spans = [(d["days"], d["text"]) for d in tn.durations(s) if d["unit"] != "hour"]
            spans += [({"year": 365, "month": 30, "week": 7, "quarter": 91}[f.group(1).lower()], f.group(0)) for f in _FIRST_PERIOD.finditer(s)]
            if _UNLIMITED.search(s):
                res.findings.append(Finding(BLOCK, f"Refunds are limited to {exp}; the email promises no time limit.",
                                            "1 facts", _UNLIMITED.search(s).group(0), exp, "policies.json refunds", s))
            elif spans:
                bad = [t for d, t in spans if not any(tn.close(d, a, rel=0, abs_tol=0.5) for a in allowed)]
                if bad:
                    res.findings.append(Finding(BLOCK, f"Refund window is {exp}; the email says {bad[0]}.",
                                                "1 facts", bad[0], exp, "policies.json refunds", s))
                else:
                    res.verified.append(f"refund window {exp} (policies.json)")

        # ---- rule 1: free trial -------------------------------------------------------------
        if "trial" in low:
            pos = low.index("trial")
            negated = bool(_NEGATION.search(low[max(0, pos - 35):pos + 25]))
            plan, inferred = plan_at(pos)
            if plan and plan not in trial_plans and not negated:
                res.findings.append(Finding(HOLD if inferred else BLOCK, f"Only {' and '.join(trial_plans)} have a free trial; {plan} does not.",
                                            "1 facts", s.strip(), ", ".join(trial_plans), "policies.json free_trial", s))
            days = [d for d in tn.durations(s) if d["unit"] in ("day", "week", "month")]
            bad = [d for d in days if not tn.close(d["days"], trial_days, rel=0, abs_tol=0.5)]
            if bad and not negated:
                res.findings.append(Finding(BLOCK, f"The free trial is {trial_days:g} days; the email says {bad[0]['text']}.",
                                            "1 facts", bad[0]["text"], f"{trial_days:g} days", "policies.json free_trial.days", s))
            elif days and not negated:
                res.verified.append(f"free trial {trial_days:g} days (policies.json)")
            card = re.search(r"\b(credit\s+)?card\b", low)
            if card and re.search(r"\b(required|needed|need|must|necessary)\b", low) and not _NEGATION.search(low) and not pol["free_trial"]["card_required"]:
                res.findings.append(Finding(BLOCK, "No card is required for the free trial.", "1 facts", card.group(0),
                                            "card_required: false", "policies.json free_trial", s))

        # ---- rule 1: first-response time ----------------------------------------------------
        if _RESPONSE.search(s):
            for d in tn.durations(s):
                if d["unit"] not in ("hour", "day"):
                    continue
                plan, inferred = plan_at(d["start"])
                if plan is None or plan == "Enterprise":
                    continue
                exp = plans[plan]["response_h"]
                if tn.close(d["hours"], exp, rel=0, abs_tol=0.5):
                    res.verified.append(f"{plan} first response {exp:g} hours (price_sheet.csv)")
                else:
                    res.findings.append(Finding(HOLD if inferred else BLOCK,
                                                f"{plan} first response is {exp:g} hours; the email says {d['text']}.",
                                                "1 facts", d["text"], f"{exp:g} hours", "price_sheet.csv first_response_hours", s))

        # ---- rule 3: promises beyond the agent's authority ------------------------------------
        if _EXCEPTION.search(s) or (_COMMIT.search(s) and _SCOPE.search(s)):
            hit = (_EXCEPTION.search(s) or _SCOPE.search(s)).group(0)
            res.findings.append(Finding(HOLD, f"Promise a support agent can't make on their own (\"{hit}\"): a manager should review.",
                                        "3 promise", hit, "agent_authority: no feature promises or exceptions", "policies.json agent_authority", s))

        # ---- rule 4: hype and guarantees ----------------------------------------------------
        strong, mild = _STRONG_HYPE.search(s), _MILD_HYPE.findall(s)
        if strong or len(mild) >= 2 or (mild and s.count("!") >= 2) or s.count("!") >= 3:
            word = strong.group(0) if strong else (mild[0] if mild else "!!!")
            res.findings.append(Finding(HOLD, f"Over-the-top or guaranteed language (\"{word}\") needs review before it goes to a customer.",
                                        "4 hype", word, "neutral, factual tone", "business_rules.md rule 4", s))

    res.welfare = welfare.assess(offer, ctx_plan, tenure, plans, pol, context)
    return res


def _check_savings(res, m, plan, plans, loyal_pct, annual_pct):
    """A dollar figure next to 'save' / 'off' is a saving, not a price."""
    cands = []
    for name, p in plans.items():
        if plan and name != plan:
            continue
        cands += [p["monthly"] - p["annual_pm"], round(p["monthly"] * loyal_pct / 100, 2), round(p["monthly"] * annual_pct / 100, 2)]
        cands += [c * 12 for c in list(cands)]
    if any(tn.close(m["value"], c, rel=0, abs_tol=0.02) for c in cands):
        res.verified.append(f"saving {m['text']} matches the published discounts")
    else:
        res.findings.append(Finding(HOLD, f"The saving {m['text']} doesn't match any published discount{' on ' + plan if plan else ''}.",
                                    "1 facts", m["text"], "a saving from policies.json discounts", "policies.json discounts", m["text"]))
