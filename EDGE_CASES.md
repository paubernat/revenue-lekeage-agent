# Edge Cases: what the agent catches, and how we prove it

Every case below has an **ID**, a **plain-words explanation**, a **chat prompt to try it**, and an
**automated test**. Run all of them with `pytest -q` (42 tests, no API key needed).

## How to try them in the chat

1. `streamlit run app.py`
2. Sidebar → **Dataset**:
   - **Original**: the challenge data (`data/`).
   - **Edge cases**: `data_edge_cases/`, with one small plan per case. Each is a monthly plan of
     12,000 USD starting 2025-10-01, so October, November and December are due at 1,000 each.
     Each dataset has its own sandbox, so switching never mixes their fixes.
3. Ask the prompt from the table. Click **New chat** every few cases to keep costs down.

## Three ideas that make it robust

1. **One rule per known case.** Each case is a deterministic rule with its own test.
2. **A safety net for unknown cases.** For every plan, the agent also computes one independent
   number: *billed to date − expected to date*. The findings must add up to exactly that gap. If they
   don't, an `unexplained_difference` finding appears for human review. So even a case nobody thought
   of can't make money silently disappear from the report. A test checks that it never fires on
   either dataset, and another that it does fire when a finding is missing.
3. **Honesty about the method.** Every investigation returns a `method` block that says how the
   matching works and what it does *not* handle. The agent must explain itself from that block and
   never guess.

## How to read a finding

```
type            what kind of problem (missing_period, underbilled, collection_risk, ...)
delta           actual − expected, in plan currency.  < 0 = we lost money  · > 0 = customer overpaid
remaining_delta what is still open after existing fixes (credit memos, applied make-goods)
status          open · partially_remediated · remediated · needs_review (a human must decide)
confidence      high · medium (an assumption was needed) · low (intent unclear, ask first)
calculation     the maths, e.g. "Expected 8000 USD; billed 0 -> gap 8000"
assumptions     every guess the rule made, in words
suggested_action the prefilled propose_* call (only for open findings with money involved)
```

---

## The cases

Legend: ✅ handled · 🆕 added in this hardening pass · ⚠️ known limitation

### Core billing problems (original dataset)

| ID | Case | In plain words | What the agent does | Try it | Test |
|---|---|---|---|---|---|
| EC-01 | Missing period | A month/quarter was never billed | `missing_period`, make-good suggested | "Check C-1001" | `test_acme_missing_months` |
| EC-02 | Under/overbilled | Billed less/more than the contract | `underbilled` / `overbilled` with the delta | "Is there anything wrong with Initech?" | `test_initech_underbilled_with_amendment_alternative` |
| EC-03 | Currency mismatch | Billed 25,000 EUR on a USD plan | Converts (×1.08 = 27,000), compares: +2,000 | "Check C-1007-A1" | `test_globex_fx_overbilling_already_remediated_by_m300` |
| EC-04 | Closest earlier FX rate | No rate on that exact day | Uses the last earlier rate **and says so** | "Convert 100 EUR to USD on 2025-10-01" | `test_fx_closest_prior_rate` |
| EC-05 | Already fixed | A credit memo already covers it (M-300) | `status: remediated`, refuses a second fix | "Should we issue a credit memo for I-9123?" | `test_no_duplicate_credit_for_m300` |
| EC-06 | Amended contract | A new plan replaces the old one | Old term ends the day before (C-1007 → 2025-06-30) | "When does C-1007 end and why?" | `test_globex_original_term_late_billing_not_false_positive` |
| EC-07 | Late billing | Invoice issued after its period | Matched to the earliest unbilled period, medium confidence | "Check C-1007" | same |
| EC-08 | Stub period | The leftover month of a replaced plan | Shown as an *alternative* (−10,000), not as a gap | "Check C-1007" | same |
| EC-09 | Orphan invoice | Invoice without a plan (I-9202) | Ranked candidate plans, `needs_review` | "Which invoices have no plan?" | `test_orphan_invoice_candidates` |
| EC-10 | Orphan may explain a gap | That orphan could be Q4's payment | Flagged on the gap, **never assumed** | "Check Globex" | `test_globex_fx_overbilling_already_remediated_by_m300` |
| EC-11 | Entitlement gap | "Annual base plan" invoice, contract includes Analytics | Offers make-good **or** amendment, asks | "Why is Initech underbilled?" | `test_initech_underbilled_with_amendment_alternative` |
| EC-12 | Not due yet | Future periods | Ignored (as-of date in the sidebar) | As-of 2025-10-31, "Check C-1001" | `test_as_of_limits_due_periods` |
| EC-16 | Fix applied / undone | Make-good approved, then rolled back | Remediated → open again | Demo script in SOLUTION.md | `test_apply_then_reinvestigate_is_remediated_and_rollback_reopens` |
| EC-17 | Partial fix | Make-good for 5,000 of an 8,000 gap | `partially_remediated`, 3,000 still suggested | "Make a make-good of 5,000 for September" | `test_ec17_partial_fix_is_partially_remediated` |

### Edge cases (Edge cases dataset unless noted)

| ID | Case | In plain words | Before | Now | Try it | Test |
|---|---|---|---|---|---|---|
| EC-13 | Duplicate invoice | November billed twice in full | ✅ untested | `duplicate_invoice` +1,000 → credit memo | "Check P-DUP" | `test_ec13_duplicate_invoice` |
| EC-14 | Out of term | Invoice before the contract started | ✅ untested | `out_of_term`, needs review | "Check P-OOT" | `test_ec14_out_of_term_invoice` |
| EC-15 | Notes contradict plan | Notes say "~1500 USD per month", fields say 1,000 | ✅ untested | `plan_inconsistency` | "Check P-NOTES" | `test_ec15_notes_contradict_plan` |
| EC-18 | Missing FX rate | December billed in GBP, no GBP rate | 🔴 invoice **silently skipped** | 🆕 `fx_rate_missing`; period marked *unverified*, not *missing* | "Check P-FX" | `test_ec18_missing_fx_rate_is_flagged_not_skipped` |
| EC-19 | Split billing | November paid as 600 + 400 | 🟠 false *duplicate* | 🆕 amounts summed → OK | "Check P-SPLIT" | `test_ec19_split_billing_is_not_a_duplicate` |
| EC-20 | Early billing | December billed on Nov 26 | 🟠 "Nov duplicate + Dec missing" | 🆕 ≤ 10 days before a period counts for it | "Check P-EARLY" | `test_ec20_early_billing_counts_for_next_period` |
| EC-21 | Void invoice | December invoice was cancelled | 🟠 counted as billed (leak hidden) | 🆕 not counted → December gap, explained | "Check P-VOID" | `test_ec21_void_invoice_is_not_counted` |
| EC-22 | Unpaid past due | Billed but never collected (also I-9010 in the original data!) | ❌ ignored | 🆕 `collection_risk` with days overdue | "Check P-UNPAID" / "Check C-1001" | `test_ec22_unpaid_past_due_is_collection_risk` |
| EC-23 | Refund | A −1,000 "service outage" invoice | 🟡 plain underbilling | 🆕 netted, **low confidence**, asks if it was agreed | "Check P-REFUND" | `test_ec23_refund_is_netted_with_low_confidence` |
| EC-24 | Name variants | Orphan from "INITRODE", plan for "Initrode Inc" | ❌ no candidate | 🆕 names normalised → P-NAME, linked to its December gap | "Which invoices have no plan?" | `test_ec24_orphan_matched_by_normalised_customer_name` |
| EC-25 | Plan id typo | Orphan with plan_id "p typo" | ❌ no candidate | 🆕 ids normalised → P-TYPO as the top candidate | same | `test_ec25_orphan_matched_by_plan_id_lookalike` |
| EC-26 | Multi-year contract | 200,000 over 2 years, billed yearly | 🟠 assumed 12 months → false −100,000 | 🆕 optional `end_date` respected | "Check P-2YEAR" | `test_ec26_multi_year_contract_uses_end_date` |
| EC-27 | Rounding | 10,000 / 12 = 833.33… | 🟡 periods summed to 9,999.96 | 🆕 last period absorbs the remainder (833.37) | "Load plan P-ROUND" | `test_ec27_rounding_remainder_on_last_period` |
| EC-28 | Unexplained credit | 300 "goodwill" credit on a correct invoice | ❌ ignored (revenue given away) | 🆕 `unmatched_credit`, needs review | "Check P-CREDIT" | `test_ec28_unexplained_credit_memo` |
| EC-29 | Invalid record | Plan with cadence "Weekly", invoice without an amount | 🔴 **app crashed** | 🆕 record ignored + `data_quality` finding | "Are there any data problems?" | `test_ec29_invalid_records_are_reported_not_fatal` |
| EC-30 | Duplicate id | Invoice E-3001 appears twice | ❌ counted twice | 🆕 second copy ignored + `data_quality` | same | `test_ec30_duplicate_invoice_id` |
| EC-31 | Safety net | Findings don't add up to the plan's total gap | ❌ no check | 🆕 `unexplained_difference` | (unit test; must never fire on real data) | `test_ec31_*` |
| EC-32 | Honest method | "What are the limits of your check?" | 🟠 agent guessed | 🆕 answers from the `method` block | "What are the limits of your check?" | `test_ec32_method_note_is_returned` |

### Guard rails on actions

| Rule | Test |
|---|---|
| Nothing is written before a human clicks Approve; Reject writes nothing | `test_apply_waits_for_human_and_approve_writes`, `test_reject_writes_nothing` |
| Amount can't exceed the open gap | `test_amount_cannot_exceed_gap` |
| No fix for an already-remediated finding (M-300) | `test_no_duplicate_credit_for_m300` |
| Make-good only for underbilling, credit memo only for overbilling | `test_wrong_direction_rejected` |
| The same proposal twice returns the existing one | `test_same_draft_is_idempotent_and_reject_works` |
| Amendments may only change total, cadence, entitlements, notes | `test_amendment_rejects_unknown_fields` |
| 🆕 Apply re-checks: if the gap was closed meanwhile, apply is refused | `test_apply_revalidates_when_gap_closed_meanwhile` |

### Known limitations (stated in the `method` block)

| ID | Case | Why | Impact |
|---|---|---|---|
| L-1 | September billed on Oct 2 and October never billed | Indistinguishable from "September missing, October billed" without a service-period field on invoices | Amount right, month label may be off |
| L-2 | Pauses, usage-based billing, taxes, price changes without an amending plan | Not in the data model | Out of scope |
| L-3 | Orphan invoices | Attributing a payment is a business decision | Always `needs_review`, never automatic |

**The real fix for L-1 and L-2 is upstream:** invoices that carry `service_period_start/end`
and contracts with an explicit `end_date`, which the model already supports (EC-26).

## Tuning knobs (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `EARLY_BILLING_DAYS` | 10 | How many days before a period an invoice may be issued and still count for it |
| `REASSIGN_MIN_SHARE` | 0.5 | Only invoices worth at least 50% of a period are moved to another period, so a split payment or a refund stays in its own period |
| as-of date | 2025-12-31 | Sidebar; periods that start after it are not due |
