# Approval Gate — Wrapper Starter

Python 3.9+, standard library only. The runnable baseline mostly follows the overall tier; it is deliberately weak. **Your task is the independent signal and decision policy, not the HTTP plumbing.**

## Run from the repository root

The harness starts the mock and your wrapper for you:

```bash
python3 gate.py try --persona customer_support "Your 30% loyalty discount applies to the Pro plan."
python3 gate.py practice --persona customer_support
```

Use `paralegal` or `financial_analyst` for another persona. To work on the pieces by hand (for example while building your demo), start the mock with `python3 mock_ezio.py`, then in a second terminal run `python3 wrapper/evaluate.py customer_support`, `python3 wrapper/wrapper.py customer_support "..."`, or `python3 wrapper/serve.py` (port 9000).

## Wrapper HTTP contract

```bash
curl -s http://127.0.0.1:9000/decide \
  -H "Content-Type: application/json" \
  -d '{"text":"Please review this draft.","context":{},"persona":"customer_support"}'
```

| Request field | Required | Type |
|---|---|---|
| `text` | Yes | Non-empty string, at most 20,000 characters |
| `persona` | Yes | `customer_support`, `paralegal`, or `financial_analyst` |
| `context` | No | JSON object; defaults to `{}` |

Return HTTP 200 and a JSON object for a valid decision:

```json
{
  "decision": "HOLD_FOR_REVIEW",
  "reason": "The draft needs an independent evidence check.",
  "evidence": ["A source-of-truth check is needed before approval."],
  "ezio": {},
  "action_executed": false
}
```

`decision` must be `APPROVE`, `HOLD_FOR_REVIEW`, or `BLOCK`; `reason` must be a non-empty string. Include supporting `evidence`. The starter also returns the mock response in `ezio` and whether its local mock action ran. Bad requests return 400; implementation failures return 500 and count as harness errors, not successful holds.

`GET /health` returns 200 when the wrapper is ready. `GET /` serves your persona page from `ui/`, and `GET /api/samples?persona=<name>` returns the practice inputs (text and context only) for a page that simulates the action from a test input. Other languages must implement this same decision contract; the health endpoint is useful but not scored.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EZIO_URL` | `http://localhost:8787` | Mock API base URL; read when the wrapper starts. `gate.py` sets it to its private mock. |
| `EZIO_KEY` | `mock-team-key` | Local mock's example key |
| `HOST` | `127.0.0.1` | Wrapper bind address. Keep it on loopback: nothing here needs the network. |
| `PORT` | `9000` | Wrapper port. `gate.py` picks a free one and sets it. |
| `GATE_LOG_DIR` | wrapper folder | Where `audit_log.jsonl` and `actions.log` go. `gate.py judge` sets it to `runs/…` so the evaluation does not mix with your demo logs. Optional for other languages. |

A wrapper in another language must read `HOST`, `PORT`, and `EZIO_URL` from the environment, never hard-code them, and expose the contract above. If you also write `actions.log` (one line per executed mock action) into `GATE_LOG_DIR`, `gate.py judge` can cross-check your action gate automatically.

### Evaluation runs

`gate.py judge` sends the same requests as any client, without special headers: your wrapper audits and acts exactly as in production, into `GATE_LOG_DIR`. The wrapper has no authentication and is for local fictional-data exercises only; never expose it to a network.

## What is provided

| File | Responsibility |
|---|---|
| `serve.py` | Request validation, HTTP interface, the action gate, `GET /api/samples`, and the static files in `ui/` |
| `ui/persona.html` | Skeleton page for your persona with `TODO` instructions; yours to replace (optional, 25 points) |
| `ezio_client.py` | Mock authentication, timeouts, bounded retries, and backoff |
| `wrapper.py` | Response validation, persona data loading, policy application, decision and evidence |
| `mock_actions.py` | Local simulated actions recorded in `actions.log`; no real messages are sent |
| `audit.py` | Thread-safe JSONL audit entries in `audit_log.jsonl` |
| `evaluate.py` | Practice catch rate, false-block rate, exact match, and missing policy justifications |
| `policy.json` | Thresholds, accuracy-state policy, failure behavior, and your explanations |

The client allows up to four attempts, with a 15-second per-attempt timeout and a 45-second request budget. Retry delays and server-supplied wait hints are bounded. The harness timeout is 60 seconds per decision. Keep your full policy, including any model checks, within that limit.

## What you build

1. **Independent signal:** implement `check_source_of_truth()` in `wrapper.py`. Read your persona's `business_rules.md` and data first. Do not look up answers by matching the practice text.
2. **Policy:** choose thresholds and explicit handling of all three accuracy states. Replace every TODO justification in `policy.json`.
3. **Failure behavior:** decide whether a missing signal means hold, block, or approve based on independent evidence. Defend that choice.
4. **Evidence and audit:** make your evidence useful to a human reviewer. Ordinary entries include the full mock response; check failures and successes, not just blocked cases.
5. **Optional extensions:** per-claim decisions, calibration, review workflows, caching, or a second model.

Blocking everything produces a perfect catch rate and a terrible false-block rate. A GREEN tier with `not_scored` means accuracy was not checked. Neither shortcut is a successful decision policy.

See the [challenge requirements and scoring](../README.md#minimum-requirements) before your demo.
