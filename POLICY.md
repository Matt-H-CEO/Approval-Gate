# Decision policy: HoldPoint (customer support persona)

**Gated action:** `send_email` to a Lumen Cloud customer (fictional). **Decisions:** APPROVE (send), HOLD_FOR_REVIEW (a person decides), BLOCK (never send as written). **The worst signal wins.** Every decision carries a plain reason, evidence, a coverage line (what was and wasn't checked) and an audit record.

## Signals

| Signal | Source | Effect |
|---|---|---|
| Company-data check (independent signal) | `price_sheet.csv`, `policies.json`, the customer's `context` (plan, tenure) | Wrong price, discount, refund window, trial or response time → **BLOCK**; discount without eligibility or stacked → **BLOCK**; promise of features, dates or exceptions → **HOLD**; hype or guarantees → **HOLD** |
| Ezio claim verdicts (`claims[]`) | mock Ezio over HTTP | contradicted → **BLOCK**; unverifiable → **HOLD**; supported → no second opinion needed (rule 6) |
| Ezio tier | mock Ezio | GREEN → defer to the checks; YELLOW or RED → **HOLD** (BLOCK is reserved for proven contradictions) |
| Ezio tone, bias, agreement | mock Ezio | below **60** → **HOLD** |
| Ezio accuracy status | mock Ezio | `not_scored` → ignore (rule 5); `unavailable` → **HOLD** unless the data check blocks |

## Thresholds and why

- **GREEN is not proof of accuracy.** 20 of 24 bad practice items across the three personas score GREEN; per persona, 6 of 8 bad items are GREEN with no weak dimension. Only our data check catches them.
- **Dimensions hold below 60.** That is Ezio's critical band. In the mock's formula, acronyms (SSO, USD, API) cost at most 30 tone points, so acronym-heavy correct emails stay at 70 or above. Practice hype items scored 26 and 38, and good items 90 or above. Mild wording that the mock scores 100 ("slam dunk", "guaranteed") is covered by our own word list.
- **Calibration.** A figure tied to a plan the email names explicitly can BLOCK. A figure we can only tie to the customer's plan from context, or one that matches a different legitimate price (say, the annual rate quoted without "annual"), is HOLD: a person checks it instead of the gate guessing.

## Failure behavior

The data check always runs, even when Ezio fails.

| Ezio state | Decision | Why |
|---|---|---|
| down, malformed, timed out | BLOCK if the data check finds a contradiction, else **HOLD** | Fail closed. An email can wait; a wrong price can't be unsent. Evidence names the failure and the data-check result. |
| 429 or 503, then OK | normal decision | The client retries with bounded backoff (4 attempts, 45 s budget, under the 60 s harness limit) |
| slow (8 s) | normal decision | Within the per-attempt timeout |
| accuracy `unavailable` | BLOCK if the data check finds a contradiction, else **HOLD** | Only Ezio can see public-law claims (GDPR) |
| checker crash | **HOLD** | Never approve without the independent signal |

Practice run: 8/8 bad caught, 0/8 good blocked, 16/16 exact, failure drills 15/15. Team-written variants (`tests/variants.json`, 20 good and 20 bad, not from practice): **20/20 bad caught, 20/20 good approved, 0 false blocks**. Reproduce it: start `python3 gate.py serve --persona customer_support`, then run `python3 tests/run_variants.py http://127.0.0.1:<port>`. We wrote both the checker and the variants, so that is a smoke test, not an independent score.

## Human review: overseeable, not a rubber stamp

A HOLD can be released or rejected only in the review queue, and the **server** enforces three rules:

1. Each flagged problem is ticked as personally checked.
2. The reason is specific: "ok" or "looks good" is refused.
3. The decision exists in the audit log as a HOLD. A BLOCK can't be released; it needs an edited draft.

The log records the seconds from hold to release and marks releases under 15 s as **fast releases** (possible rubber stamps). Releases go to `review_log.jsonl`, never to `actions.log`, so `actions.log` always equals the gate's APPROVE decisions.

## Best-option check: multi-objective and advisory

A correct email can still leave the customer on a worse deal. For the customer's plan and tenure, the gate lists the feasible offers. Policy rules are hard constraints, never traded. It scores the offers on the customer's cost, the customer's flexibility (refund window, commitment) and the company's 12-month revenue, using stakeholder weights from `preferences.json`. It then flags dominated offers, for example: "$49 list price when 15% loyalty is available on the same terms: $88.20/yr worse". **Advisory by default** (`welfare_mode`): it informs the reviewer and never changes the scored decision. The retention beliefs in `preferences.json` are placeholder stakeholder inputs, not data.

## The aviation dispatch scenario (MOCK, the page's first tab)

Fictional carrier, fictional flight, invented numbers; not operational guidance. It is here because it separates the two things a single trust score cannot separate: **limits** and **preferences**.

- **Rules run first and are never traded.** The 45-minute IFR fuel reserve (modelled on 14 CFR 91.167(a)), the fuel limit and crew duty **remove** options before any scoring. No weighting, and no score however high, can buy an option past them.
- **Preferences are stated before the AI recommends**, not after, so the goal cannot be chosen to suit the answer. Profiles: balanced, safety first, schedule first, cost first, or custom weights.
- **Only then** are the surviving options compared on safety margin, delay and cost. The page shows the Pareto frontier, names every excluded option with its reason (rule-broken, or beaten on every count), and gives a release the gate itself accepts.
- **Same gate, same four tests, same audit trail** as the email flow. `POST /api/dispatch` is logged to `audit_log.jsonl` under persona `aviation_dispatch`, and a signed release is recorded in `dispatch_actions.log`.
- **Provenance.** Drafts H1-H6 were generated live in HarnessRouter's Arena by two different agents, with model calls billed through a TokenRouter key, then saved verbatim and replayed offline. The wrapper and the page make no network call.

The judged run is still the customer support persona and the gated `send_email` action; this scenario is a second demonstration of the same decision layer.

## Decision-grade tests (from the opening talk)

Every decision shows four checks:

- **Verified:** figures checked against data and Ezio.
- **Calibrated:** what it couldn't check, and where it held back.
- **Overseeable:** problem text marked, consequence stated, real review required.
- **Auditable:** decision id, input, Ezio response, evidence and timing, all logged.

## Known limits (where it could be wrong)

- Promises and hype are found by word patterns, not meaning. A promise phrased unusually can pass. Ezio's dimensions and the HOLD-on-ambiguity rule are the backstop.
- Figures without a unit or plan ("it's 49 a month for you") rely on the customer's context; with no context they are HOLD, not checked.
- It knows only what the two data files say. Anything else is HOLD (unverifiable) or passes if it contains no checkable figure.
- The best-option weights and retention beliefs are inputs, not facts, so the trade-off view is only as good as the stakeholders' answers.
- In the aviation lab, crew-duty buffer is a rule (enough duty for the flight), not an objective. One real agent (Claude Code) avoided max fuel because it leaves zero duty buffer, a trade-off our three objectives don't score yet.
- The aviation scenario in the lab is a **mock**: fictional carrier and numbers. Only the 45-minute reserve mirrors 14 CFR 91.167(a). It is not operational guidance.
