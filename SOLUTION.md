# Revenue Leakage Agent: Solution

A stateful chat agent (LangGraph + Claude + Streamlit) that investigates billing plans vs. invoices,
proposes make-good invoices, credit memos or plan amendments, and writes them to `sandbox/` **only
after a human clicks Approve**.

**Design principle:** *code computes, the LLM converses, the human approves.*

## Run

```bash
python -m venv .venv && .venv/Scripts/activate      # (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
cp .env.example .env                                 # add ANTHROPIC_API_KEY
pytest -q                                            # 18 tests, no API key needed
streamlit run app.py
```

## Architecture

```
app.py            Streamlit entry point: wires ui/ to the graph
config.py         paths, model, as-of date (default 2025-12-31)
core/             pure business logic, no LLM and no UI, fully unit-tested
  models.py       pydantic records: Plan, Invoice, CreditMemo, Finding, Proposal, Action
  repository.py   the only disk access: data/ (read-only) merged with sandbox/ (writable)
  fx.py           conversion with the rate for the date or the closest earlier one
  schedule.py     plan term + expected billing periods
  detectors.py    expected vs. actual -> Findings (deterministic ids, calculation, evidence)
  actions.py      drafts with validation, apply, reject, rollback, audit log
tools/            @tool wrappers over core/, the contract the LLM sees
agent/            system prompt, LLM factory, LangGraph graph
ui/               chat history + tool trace, approval card, sidebar (as-of, sandbox, audit log)
tests/            detectors, actions, and the human-in-the-loop graph (scripted fake LLM)
```

### Graph

```
START -> agent --tool call?--> tools -> agent -> ... -> END
```

- **State / memory:** `MessagesState` persisted by a `MemorySaver` checkpointer, one `thread_id`
  per Streamlit chat. Follow-ups ("what currency is *that* plan?") work because the whole history,
  including tool results, is replayed to the model each turn.
- **Human in the loop:** the `apply` and `rollback` tools call `langgraph.types.interrupt()`. The graph
  stops, the UI renders an approval card (amount, record to be written, warnings), and resumes with
  `Command(resume="approve" | "reject")`. Approval is enforced by code, not by the prompt. The test
  `tests/test_graph.py` proves nothing is written before approval.
- `parallel_tool_calls=False`: on resume LangGraph re-runs the paused tools node, so a tool call made in
  parallel with `apply` would run twice. Proposals are also idempotent as a second safeguard.

### Tools

The README's 8 tools with their exact names: `load_plan`, `query_invoices`, `fx_convert`,
`propose_make_good_invoice`, `propose_credit_memo`, `propose_plan_amendment`, `apply`, `rollback`.
Two helpers: `investigate(plan_id|"all")` runs the full analysis, and `list_proposals` shows
proposals and actions.

`apply(action_draft)` takes the **proposal id**, so the LLM cannot alter amounts between proposal and
apply. Proposals are validated in `core/actions.py`:
- the amount is at most the open gap of the linked finding
- the record is in the plan currency
- the finding is not already remediated (so M-300 is never duplicated)
- the direction is right (make-good for underbilling, credit for overbilling)
- credits never exceed the invoice
- amendments may only change `total_value`, `cadence`, `entitlements` or `notes`

Every apply is re-validated, and every step is written to `sandbox/audit_log.json`.

## Detection logic and assumptions

1. **Term:** the plans have no end date, so a term is 12 months from `start_date`, ended the day before
   an amendment (`amends`) starts. C-1007 therefore runs 2025-03-01..2025-06-30.
2. **Expected periods:** cadence gives the number of periods (12/4/1), each worth `total_value / n`. A
   period is *due* if it started on or before the **as-of date** (sidebar, default 2025-12-31). A
   period cut short by an amendment is a *partial stub*, prorated by months, and treated as an
   alternative reading rather than the primary one.
3. **Matching:** each invoice belongs to the plan in effect on its issue date, following the amendment
   chain. It goes into the period containing its issue date. If that slot is taken or is a stub while
   an earlier due period is unbilled, it is treated as **late billing** of that earlier period (stated
   as an assumption, medium confidence). This avoids reporting C-1007 as "missing Mar–May +
   overbilled June".
4. **Comparison:** invoices are converted to the plan currency on their issue date. Each finding has a
   signed `delta` = actual − expected (negative means underbilled).
5. **Existing remediation:** credit memos (data and sandbox) and applied make-goods are netted
   against findings, giving `remaining_delta` and `status = remediated | partially_remediated`.
6. **Needs review:** orphan invoices (ranked candidate plans) and entitlement gaps, flagged only when the
   plan notes ask for it. These are never acted on automatically.

Finding types:
- `missing_period`, `underbilled`, `overbilled`, `currency_mismatch` (each with a suggested action)
- `duplicate_invoice`, `out_of_term`, `orphan_invoice`, `entitlement_gap`, `plan_inconsistency`

### Findings on the provided data (as of 2025-12-31)

| Plan | Finding | Delta | Notes |
|---|---|---|---|
| C-1001 ACME | Sep, Nov, Dec missing | −8,000 USD each | high confidence → make-good |
| C-1007 Globex | Mar–May billed 20,000 vs 22,500 (I-9110 late) | −2,500 USD | alt.: prorated June stub → −10,000 |
| C-1007-A1 Globex | I-9123 25,000 EUR × 1.08 = 27,000 vs 25,000 | +2,000 USD | **already remediated by M-300** |
| C-1007-A1 Globex | Q4 (Oct–Dec) missing | −25,000 USD | orphan I-9202 (15,000) may be a partial payment |
| C-1010 Initech | I-9201 "Annual base plan" 100,000 vs 120,000 | −20,000 USD | make-good **or** amendment (drop Analytics), so the agent asks |
| — | I-9202 orphan (no plan_id) | — | candidates: C-1007-A1 (best), C-1007 |

## Demo script

1. "Check plan C-1001": Sep/Nov/Dec missing, with the calculation.
2. "What currency is that plan in?": USD, answered from memory.
3. "Create a make-good for September": proposal P-001 is shown; `sandbox/invoices.json` is still empty.
4. "Apply it": approval card → **Approve** → INV-MG-001, plus an audit entry.
5. "Re-check C-1001. What about the other months?": September is now remediated; Nov and Dec are still open.
6. "And Globex?": the FX overbilling is already covered by M-300 (no duplicate memo), Q4 is missing, and the orphan is flagged.
7. "Roll that back": approval → reverted.
8. "What is a credit memo?": answered from the Key Concepts.

## Limitations / next steps

- `MemorySaver` is in-memory: conversations are lost on restart. Swap in `SqliteSaver` for persistence.
- The as-of date is a process-wide setting, which is fine for a single-user demo.
- Free-text signals (entitlements, notes) use simple heuristics and are always marked low confidence.
