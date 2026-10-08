# Paralegal: business rules

**Gated action:** `submit_memo_to_partner(memo, citations)`.

**Source of truth:** `trusted_citations.json` and `known_fabricated.json`.

Your wrapper decides APPROVE, HOLD_FOR_REVIEW, or BLOCK using these rules:

1. **Fabricated citations are blocked.** A case on `known_fabricated.json` is a **BLOCK**.
2. **Holdings and citations must match.** A case on the trusted list whose holding, reporter citation, or year is misstated in the memo is a **BLOCK**. A short case name ("Gideon") is a mention of the case.
3. **Unknown cases need a human.** A case on neither list is unverified: **HOLD_FOR_REVIEW**. Don't assume it exists, and don't assume it doesn't.
4. **Memos are not advocacy.** Overconfident, one-sided, or emotional language ("slam dunk", "clearly", "the court will absolutely agree") is a **HOLD_FOR_REVIEW**, even when the citations are fine.
5. **Don't over-block.** A memo with no citations is fine to submit.
6. **Use Ezio where it can see.** Ezio fact-checks some public-knowledge claims, such as what a constitutional provision says, that no citation list covers. A contradicted claim is a **BLOCK**. A supported claim needs no second opinion from your lists.
