# IT Helpdesk Agent

> Production-**aware**, fully runnable. Not production-ready — the gap list is in
> [Known Gaps](#8-known-gaps--assumptions), not hidden.

An AI-powered IT support agent that replaces the ticket queue as the employee's
first point of contact: it diagnoses through conversation, investigates across
mocked backend sources, resolves what it safely can, and escalates the rest with
a complete handoff packet so the employee never repeats themselves.

Target architecture: `docs/final_design.md`. Implementation freeze: `docs/mvp_plan_v3.1.md`
(entry point: `docs/implementation_guide.md`).

---

## 1. The problem I chose and why

Traditional IT support pushes every issue — even the repetitive, well-documented
majority (password resets, VPN problems, access requests) — through the same
ticket → assign → investigate → back-and-forth loop. The cost is not just agent
time; it is employee hours lost waiting on issues that a system with access to
the KB, service status, and the user directory could have settled in minutes.

The user is the **employee with the IT problem**, not the helpdesk operator.
Interaction model: a CLI chat (`make chat`), single conversation thread per case,
resumable across processes (`--resume <case_id>`).

## 2. Why agentic (vs. chatbot / FAQ search / rule engine)

The honest test is: *does the next action depend on what the previous action
returned?* Here it does.

- The same sentence — "Salesforce is slow" — requires **different tool sequences**
  depending on what `check_service_status` returns (known regional incident →
  inform; all green → dig into changes and KB). A rule engine enumerates these
  branches by hand; the branch space explodes with category × evidence state.
- FAQ search answers *documented* questions; it cannot **ask the right clarifying
  question** when the input is "my computer is broken", nor refuse to answer when
  sources contradict each other.
- A single-shot chatbot has no investigation state: no checklist of what has been
  verified, no evidence ledger, no boundary between "what I know" and "what I
  generated".

What stays **deterministic code, never LLM**: the resolve-vs-escalate decision
(`decide()` — a pure function with unit tests), policy authorization, output
citation checks, contradiction detection, and the write-action protocol.
The LLM generates semantics (hypotheses, wording, plans); code judges boundaries.

## 3. Architecture & design decisions

*(filled on D3 — node/handler topology, state machine, decide() gate ordering,
single-ownership state fields, three-segment write protocol)*

## 4. Resolution vs. escalation boundary

*(filled on D4 — E1–E10 gates, R1–R3 resolve gates, reason codes, handoff packet)*

## 5. Data sources simulated and why

*(filled on D4 — KB with authority tiers, status fixtures a/b, directory with a
planted contradiction, policy rules)*

## 6. How to run

```bash
make setup          # pip install -e .
cp .env.example .env  # add your OPENAI_API_KEY
make chat           # start a conversation
make test           # L1 deterministic suite (no LLM, no network)
make eval           # L2 golden scenarios (real model)
```

## 7. Evaluation

*(filled on D5 — two-tier evaluation; TARGET vs MEASURED columns, never mixed)*

| Metric | TARGET (unvalidated assumption) | MEASURED (5 golden cases) |
|---|---|---|
| *(D5)* | | |

## 8. Known gaps & assumptions

*(finalized on D5; standing list from the plan, recorded up front)*

- Human-request detection is a deterministic word list; oblique phrasings can slip
  through. The cost of a miss is the user rephrasing — not a safety issue.
- No crash-recovery reconciliation, no optimistic locking, no tool circuit
  breaker, no TTL auto-close, no human-side webhook.
- User mid-conversation abandonment (ABANDONED) is not modeled.
- Output Guard checks citation existence + authority, not content entailment.
- `send_unlock_verification` realism depends on the IdP (Okta self-service unlock
  policy; Entra is not equivalent).

## 9. What I'd improve with more time

*(filled on D5 — future extensions with entry points and reversal conditions)*
