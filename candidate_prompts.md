# Candidate AI Tool Usage Log

**Instructions for Candidates:**
Please document all prompts you use with AI tools during this challenge. This helps us understand your problem-solving approach and AI tool utilization skills.

---

## AI Tool Usage Log

All prompts were made in one Claude Code session (VS Code extension, Claude Opus), first in **plan mode**
(read-only analysis, no code) and then in implementation.

### Prompt 1: Planning kickoff
**Tool Used:** Claude Code (plan mode)
**Context:** Understand the case and plan a 1.5 h build that I fully understand and own.
**Prompt:**
```
hi claude: Thanks Info about the task …[objective/scenario/requirements]… Vamos a planificar este
trabajo, lo tengo que sacar en una hora y media y owning the code, asi que vamos a ver, tienes el
caso de uso en technical-case
```
**Result:** Claude read the README and all data files, mapped each anomaly in the dataset, and asked me to pick the LLM provider and the stack.
**Follow-up:** Chose Anthropic Claude and LangGraph + Streamlit.

---

### Prompt 2: Domain understanding
**Tool Used:** Claude Code (plan mode)
**Context:** Before approving any plan I wanted to understand the finance domain.
**Prompt:**
```
okay before that, explain me the anomalies and the key concepts of the readme for dummies, what is
that we're doing and why does the key concept matter
```
**Result:** A plain-language explanation of revenue leakage, make-good invoices, credit memos and plan amendments, mapped to the concrete anomalies in the data.
**Follow-up:** `how do we know that a billing plan is over?` This led to the term-inference rule (12 months, cut short by `amends`) and to choosing an as-of date of 2025-12-31.

---

### Prompt 3: Tools and determinism
**Tool Used:** Claude Code (plan mode)
**Context:** Deciding how much the LLM should decide versus the code.
**Prompt:**
```
available tools means that the problem limits itself the amount of tools?
what does read plan details do? the tools for proposing good invoice im guessing it means that based
on that there is an anomaly somewhere and after determining the type of anomaly, the solving of it
becomes deterministic, no?
when u say the code detects the anomaly im guessing the agent launches some kind of code to analyze
or what are we thinking
and you think that a deterministic algorithm fits al cases? (for this specific example)
all the possible cases of error and how the result of this investigation would be enough information
for each agent to proposea a new invoice, credit memom or plan ammendment
```
**Result:** The design principle "code computes, LLM converses, human approves". Detection is deterministic. Ambiguity (C-1007 late billing or prorated stub, Initech entitlement intent, the orphan invoice) is surfaced as assumptions, confidence and alternatives for the human. This also produced the Finding schema with `suggested_action`.
**Follow-up:** Identified the traps: C-1007 false positives (fixed with late-billing matching) and M-300 already remediating I-9123.

---

### Prompt 4: Architecture plan
**Tool Used:** Claude Code (plan mode)
**Prompt:**
```
okay so create a plan, the idea is to use langraph to bind all this tools to the llm, using
streamlit for the interface, make it modular, show me the file structure and a brief summary of
what you would do
```
**Result:** A modular plan: `core/` (pure logic) → `tools/` → `agent/graph.py` (ToolNode, MemorySaver, `interrupt()` on apply/rollback) → `ui/`, with a timed build order and a verification script.
**Follow-up:** `okay explian what is in core, step by step.` Walked through models → repository → fx → schedule → detectors → actions and the closed loop with the sandbox.

---

### Prompt 5: Implementation
**Tool Used:** Claude Code (auto mode)
**Prompt:**
```
run the plan
```
**Result:** Claude implemented the plan and verified it at each layer:
- `investigate` output checked against the expected findings
- 18 pytest tests: detectors, actions, and the human-in-the-loop graph with a scripted fake LLM
- a headless Streamlit `AppTest` smoke test: approval card shown, ledger empty until Approve, then INV-MG-001 written

**Follow-up:** I removed `temperature=0` from `agent/llm.py` by hand.

---

## Reflection
- Spending most of the time in plan mode, asking "for dummies" questions, paid off. I understood the
  domain traps (late billing, already-remediated FX) before any code existed, and the code mirrors
  that reasoning.
- Keeping the maths in deterministic, tested Python and using the LLM only for conversation and
  ambiguity makes the agent explainable: every number in a reply comes from a tool result with a
  `calculation` string.
- Enforcing approval with `interrupt()` instead of prompt instructions is the key safety decision.
  A test proves it.
