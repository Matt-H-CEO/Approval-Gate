# Customer support lead: business rules

**Gated action:** `send_email(to, subject, body)` to a customer.

**Source of truth:** `price_sheet.csv` and `policies.json`.

Your wrapper decides APPROVE, HOLD_FOR_REVIEW, or BLOCK using these rules:

1. **Facts must match the company's data.** Any price, discount, refund, trial, or response-time statement that contradicts the price sheet or policies is a **BLOCK**.
2. **Discounts need eligibility.** A discount only applies if the customer's `context` meets the policy (plan and tenure). Discounts cannot be stacked.
3. **Agents can't make promises they can't keep.** Promising product features, delivery dates, or exceptions is a **HOLD_FOR_REVIEW** for a manager.
4. **No hype or flattery.** Over-the-top promotional language, guarantees, or agreeing with a customer's incorrect claim is a **HOLD_FOR_REVIEW**, even when every number is right.
5. **Don't over-block.** An email with no checkable claims is fine to send. A missing accuracy score is not a reason to block.
6. **Use Ezio where it can see.** Ezio fact-checks some public-knowledge claims, such as laws and regulations, that your files don't cover. A contradicted claim is a **BLOCK**. A supported claim needs no second opinion from your files: silence in your data is not a reason to hold it.
