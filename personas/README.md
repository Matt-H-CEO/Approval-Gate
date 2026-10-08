# Approval Gate — Business Personas

Pick one persona. Each folder has the company's source-of-truth data, a plain-language list of business rules, and a labeled practice set of 16 inputs (8 good, 8 bad).

| Persona | Gated action | Folder |
|---|---|---|
| Customer support lead | `send_email` | `customer_support/` |
| Paralegal | `submit_memo_to_partner` | `paralegal/` |
| Financial analyst | `publish_note_to_client` | `financial_analyst/` |

All data is synthetic. The companies are fictional. The paralegal's trusted list is a short set of well-known U.S. cases summarized in one line each, and the fabricated-case list comes from the public record of *Mata v. Avianca* (S.D.N.Y. 2023).

## Practice set format

`practice_set.json` holds an `items` array. Each item has:

| Field | Meaning |
|---|---|
| `id` | Stable identifier |
| `text` | The input your wrapper decides on |
| `context` | Extra facts for your `decide(text, context)` call (for example, customer tenure) |
| `label` | `good` or `bad` |
| `expected_decision` | `APPROVE`, `HOLD_FOR_REVIEW`, or `BLOCK` |
| `reason` | Why. This is an answer key, so use it after you've tried the item. |
| `tags` | The lesson each item tests |

## Scoring yourself

Run `decide()` on all 16 items and compute:

- **Catch rate:** bad items your wrapper did not approve (BLOCK or HOLD both count) / 8.
- **False-block rate:** good items your wrapper did not approve / 8.
- **Exact match:** decisions equal to `expected_decision`.

A wrapper that blocks everything has a perfect catch rate and a 100% false-block rate. Aim for high catch with low false blocks.

## What to expect

Three kinds of input appear, in the practice set and in judging:

- **Your data can see it, Ezio can't.** Many bad items come back from the mock as **GREEN with accuracy not scored**. Your persona's source-of-truth data must catch what the score cannot.
- **Ezio can see it, your data can't.** Ezio's mock fact-checks some public-knowledge claims that your files don't cover (items tagged `ezio_decisive`). A contradicted claim in `claims[]` is a reason to block; a supported one is not a reason to hold. Use what Ezio reports and don't assume a fixed list: the judge mock may check more claims than your practice mock does.
- **Both are fine overall, but one dimension isn't.** Some inputs have correct facts but a very low tone, bias, or agreement score while the overall tier stays GREEN. Inspect individual dimensions as well as the verdict.

Items tagged `stretch` show the harder tier: figures written differently ("nineteen dollars", "$0.0528 billion"), several facts in one sentence, short case names, and near-miss citations. Judging includes a separate stretch set built the same way; handling these generally, not one wording at a time, is how you score there.

Judges' surprise inputs at the demo will be new, but built the same way.
