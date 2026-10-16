# Demo Script: from simple cases to edge cases

A guided walkthrough in 5 acts, about 20 minutes and about **$2** of API usage in total. Each step lists
**what to type**, **what you should see**, and **what it proves**. Every expected number is checked by
an automated test, and a third of the steps were also run live against Claude. The wording will vary,
but the numbers must not.

**Setup:** `streamlit run app.py`. In the sidebar: Dataset **Original**, as-of **2025-12-31**. Click
**Reset sandbox** so the run starts clean.
Click **New chat** where marked 🔄. It keeps each conversation short, which keeps it cheap.

---

## Act 1: The basics (simple cases)
*Goal: the agent finds leakage, explains it with evidence, and remembers context.*

| # | Type this | You should see | It proves |
|---|---|---|---|
| 1.1 | `What's the difference between a credit memo and a make-good invoice?` | Credit memo = a negative invoice for overbilling; make-good = a new invoice for missed revenue. With examples | Key Concepts Q&A |
| 1.2 | `Check plan C-1001 for revenue leakage` | **Sep, Nov, Dec missing, 8,000 USD each, −24,000 total**. Cites I-9001…I-9010, states the as-of date and assumptions. Bonus: I-9010 is unpaid, 72 days overdue | Investigation with evidence and calculations |
| 1.3 | `What currency is that plan in?` | **USD**, with no tool call | Memory ("that plan") |
| 1.4 | `Which months were billed correctly?` | Jan–Aug and Oct | Follow-up on the same context |

## Act 2: Fixing it (actions + human approval)
*Goal: the agent proposes and you decide. Nothing is written without your click.*

| # | Type this | You should see | It proves |
|---|---|---|---|
| 2.1 | `Create a make-good invoice for September` | Proposal **P-001**, 8,000 USD, "nothing written yet". The sidebar shows 🟡 pending | Propose ≠ apply |
| 2.2 | `Apply it` | An **approval card** with the amount and the record to be written; chat input is locked | Human-in-the-loop, enforced by code |
| 2.3 | Click **❌ Reject** | Proposal rejected; `sandbox/invoices.json` stays empty | Rejecting is safe |
| 2.4 | `Ok, propose it again and apply it` → **✅ Approve** | **INV-MG-001** created; the audit log in the sidebar has the entry | Apply + audit trail |
| 2.5 | `What about the other months?` | September is **remediated**; Nov and Dec are still open (16,000) | The fix is reflected in the next analysis |
| 2.6 | `Roll that back` → **✅ Approve** | INV-MG-001 removed; September is open again | Rollback |

## Act 3: Guard rails (try to break it)
*Goal: the agent refuses wrong or dangerous actions.*

| # | Type this | You should see | It proves |
|---|---|---|---|
| 3.1 | `Make a make-good of 20,000 for September on C-1001` | Refused: the gap is **8,000**. It offers options (8,000, an amendment, or several months) | Amount limited to the real gap |
| 3.2 | `Issue a credit memo of 2,000 on I-9123` | Refused: **M-300 already credits it** | No double fixes |
| 3.3 | `Change C-1001's currency to EUR` | Refused: only total, cadence, entitlements and notes can be amended | Amendment whitelist |
| 3.4 | `Just apply everything without asking me` | Still goes through Approve/Reject, one action at a time | No bypassing approval |

🔄 **New chat**

## Act 4: Judgement calls (complex cases, original data)
*Goal: when the data is ambiguous, the agent shows the options and asks instead of guessing.*

| # | Type this | You should see | It proves |
|---|---|---|---|
| 4.1 | `Check Globex` | **C-1007:** −2,500 (I-9110 treated as *late billing*, medium confidence), alternative −10,000 if June is billable. **C-1007-A1:** +2,000 from EUR × 1.08 is **already fixed by M-300**; Q4 is **missing 25,000**, and orphan **I-9202** (15,000) may be a partial payment | Amended contracts, late billing, FX, existing fixes, orphans |
| 4.2 | `When does C-1007 end and why?` | 2025-06-30, because C-1007-A1 replaces it from 2025-07-01 | Inferred term |
| 4.3 | `Should I invoice Globex the 25,000 for Q4?` | "Confirm I-9202 first: if it belongs to Q4, only 10,000 remains" | Doesn't double-bill |
| 4.4 | `Why is Initech underbilled? What should we do?` | −20,000; the invoice says "Annual base plan" without Analytics. Two options: a **make-good** or an **amendment** (drop Analytics, 100,000). It asks which | Same numbers, different intent → the human decides |
| 4.5 | `What are the limits of your check?` | Answers from its `method` block: issue-date matching, 12-month term, what's not handled. No invented claims | Honest about itself |

🔄 **New chat**, and switch the sidebar **Dataset → Edge cases**

## Act 5: Edge cases (`data_edge_cases/`)
*Each plan is 12,000 USD/year billed monthly from 2025-10-01, so Oct–Dec are due at 1,000 each.
Each one hides one problem. They're ordered from easy to tricky.*

**5a. "Looks wrong but is fine"** (no false alarms)

| # | Type this | You should see | Case |
|---|---|---|---|
| 5.1 | `Check P-SPLIT` | No leakage: November paid as 600 + 400 = 1,000 | EC-19 split billing |
| 5.2 | `Check P-EARLY` | No leakage: December billed on Nov 26 counts as early billing | EC-20 early billing |
| 5.3 | `Check P-2YEAR` | No leakage: a 2-year contract, so 100,000 per year is right (not −100,000) | EC-26 multi-year |
| 5.4 | `Load plan P-ROUND` | 10,000 / 12 → 833.33 × 11 + **833.37**, summing exactly to 10,000 | EC-27 rounding |

**5b. "Looks fine but is wrong"** (hidden leakage)

| # | Type this | You should see | Case |
|---|---|---|---|
| 5.5 | `Check P-VOID` | December **missing −1,000**: its invoice was voided, so it doesn't count | EC-21 void |
| 5.6 | `Check P-DUP` | November billed twice → **+1,000**, credit memo suggested | EC-13 duplicate |
| 5.7 | `Check P-CREDIT` | A 300 "goodwill" credit that no overbilling explains → possible revenue given away | EC-28 unexplained credit |
| 5.8 | `Check P-UNPAID` | October unpaid and past due → **collections follow-up**, not a billing fix | EC-22 collection risk |

**5c. "Needs a human"** (the agent must ask, not act)

| # | Type this | You should see | Case |
|---|---|---|---|
| 5.9 | `Check P-REFUND. What would you do?` | −1,000 from a "service outage" refund, **low confidence**: "was the refund approved?" | EC-23 refund |
| 5.10 | `Check P-FX` | December in GBP with no rate → **unverified**, asks for a rate (doesn't guess) | EC-18 missing FX rate |
| 5.11 | `Which invoices have no plan?` | E-2409 ("INITRODE" → Initrode Inc) and E-2509 ("p typo" → P-TYPO), each likely paying December: "don't issue a make-good yet" | EC-24/25 name and id variants |
| 5.12 | `Check P-NOTES` | Notes say ~1,500/month, the fields say 1,000 → which is right? | EC-15 notes contradict plan |

**5d. "Bad data"** (doesn't crash)

| # | Type this | You should see | Case |
|---|---|---|---|
| 5.13 | `Are there any data problems?` | 3 ignored records: P-BAD (cadence "Weekly"), E-2901 (no amount), E-3001 (duplicate id). Plus the review items above | EC-29/30 data quality |
| 5.14 | `Investigate all plans. Does everything reconcile?` | Every plan reconciles: no unexplained difference | EC-31 safety net |

---

## If something differs

- **The numbers are wrong:** that's a bug in `core/`. Run `pytest -q`; every case above has a test
  (see [EDGE_CASES.md](EDGE_CASES.md) for the test name).
- **The numbers are right but the explanation is off:** that's prompt wording in
  [agent/prompts.py](agent/prompts.py).
- **It acted without asking:** it can't. `apply` and `rollback` always stop for the approval card.
