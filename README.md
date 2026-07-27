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

**Topology (frozen): 5 LLM nodes + 4 code handlers + 1 shared classifier.**
LLM nodes generate semantics only — `intake` (structured issue profile),
`investigate` (hypotheses + next tool batch, max 3 batches), `clarify` (one
question per gap), `resolve` (cited plan), `escalate` (two narrative sections).
Code handlers own the lifecycle — `ingress` (every message: credential
redaction, human-request ratchet, security fast-path), `decide`, `act`, `close`
— plus one shared deterministic-wordlist-first `classifier` (confirm YES/NO/OTHER,
verify RESOLVED/FAILED/UNKNOWN, escalated RESOLVED/OTHER) with a SMALL-model
fallback. No node does boundary judgment; no handler calls an LLM node.

**State machine.** 7 phases (`INTAKE → INVESTIGATING → AWAITING_CLARIFY /
AWAITING_CONFIRM / AWAITING_VERIFY → ESCALATED / CLOSED`); each waiting phase
has exactly one recovery route, driven by the classifier. `outcome` is settled
once by `close` and decoupled from `phase` (a case closed after escalation keeps
`outcome=ESCALATED` — metrics attribute at ticket-creation time).

**`decide()` is a pure function** (zero IO, zero LLM, unit-tested gate by gate):
L0 lifecycle → L1 hard red lines (security, policy-requires-human, user asked
for a human, guard failures) → L2 budget → L3 capability (missing critical info,
unresolved contradictions, out-of-scope) → L4 normal branches (R1–R3 all-PASS →
RESOLVE; question gap → ASK; tool gap → INVESTIGATE) → L5 fallback = escalate.
`confidence` (HIGH/LOW, two booleans) only affects wording, never safety.
System errors (E4) deliberately live in the **runner**, not in `decide`:
snapshot before every node, roll back on exception, escalate with
`SYSTEM_ERROR`, never leak a stack trace.

**Single-ownership state fields.** Every `CaseState` field has exactly one
writer (e.g. `issue.verbatim` → ingress, `pending_clarify_item_id` → clarify,
`declined_actions` → confirm handler, `guard_failures` → resolve's exit guard);
the three highest-risk fields (`actor`, `evidence`, `pending_action`) are
enforced with runtime asserts. `user_requested_human` is a ratchet — once true,
no code path resets it.

**Three-segment write protocol** (D4 completes wiring): PROPOSE — the LLM emits
an *intent* only; FREEZE — code builds the args, checks policy (deny-by-default,
4 rules), refuses intents the user already declined, stamps an idempotency key
and a 5-minute expiry; EXECUTE — four pre-checks, then the write tool runs.
Write tools never appear in the model's tool list; no tool signature accepts
`target_user` — identity is injected by the runtime, so "I'm the CEO's
assistant, reset his password" fails at the architecture layer.

**Guards are deterministic code.** Input Guard: credential redaction,
attachment refusal, security-signal fast-path (case is persisted first, then
lifted straight to escalation — no advice given). Output Guard at resolve's
exit: every citation must exist in this case's evidence ledger; KB citations
must be VERIFIED (DRAFT is background only, DEPRECATED is excluded at the index
layer); uncited generic steps alone can never constitute a resolution. One
in-node retry with the violation fed back, then `guard_failures=2` → E10
escalation. Tools return a four-state envelope (OK/EMPTY/DEGRADED/ERROR —
"searched and found nothing" is evidence, not an error) and pass three gate
layers: stage (read tools only during investigation phases), policy, runtime
(budget interceptor, actor injection, dedup, retry).

## 4. Resolution vs. escalation boundary

Every boundary decision lives in one pure function, `decide()`, evaluated in a
frozen short-circuit order. Escalation gates (E4 deliberately absent — it lives
in the runner):

| Gate | Trigger | Reason code | Behavior |
|---|---|---|---|
| E1 | `category == SECURITY` (or ingress security wordlist) | `SECURITY` | Straight to `security-ir`; **no diagnosis, no advice** |
| E2 | any policy decision is `DENY_REQUIRE_HUMAN` | `POLICY_REQUIRED` | Queue comes from the matching rule (e.g. `data-platform-approvers`) |
| E3 | `user_requested_human` ratchet (deterministic wordlist, every message) | `USER_REQUESTED` | Immediate, outranks budget/capability gates |
| E4 | *moved to runner*: uncaught node exception | `SYSTEM_ERROR` | Snapshot rollback → escalate directly, never through `decide`, no stack trace to the user |
| E5 | budget cap (≥10 tool calls / ≥8 turns / ≥$0.10 / ≥3 min) | `BUDGET_EXHAUSTED` | Ticket creation itself is exempt from the budget interceptor |
| E6 | `resolution_attempts ≥ 2` (counted by the runner, not in-node retries) | `BUDGET_EXHAUSTED` | Repeated-failure semantics |
| E7 | a critical checklist item is `UNAVAILABLE` (tool ERROR) | `TOOL_UNAVAILABLE` | Honest disclosure instead of guessing around a dead source |
| E8 | `contradictions` non-empty (deterministic cross-source check) | `UNRESOLVED_CONTRADICTION` | e.g. directory groups vs. entitlements disagree |
| E9 | `category == OUT_OF_SCOPE_NON_IT` | — | REDIRECT: point to the right channel, **no ticket** |
| E10 | `guard_failures ≥ 2` (Output Guard failed twice) | `GUARD_FAILED` | Never ship an uncited plan |
| L5 | nothing else matched | `LOW_CONFIDENCE` | Fallback is escalation, not improvisation |

**Resolve gates (R1–R3, all must PASS, no exemptions):** R1 category is
auto-resolvable; R2 every critical checklist item SATISFIED; R3 exactly one
SUPPORTED hypothesis. `UNKNOWN` input ("my computer is broken") is not a dead
end: two clarify rounds first, then LOW_CONFIDENCE escalation *carrying both
answers* in the packet.

**Handoff packet.** The LLM writes exactly two narrative sections
(`agent_diagnosis`, `needed_from_human`); everything else — verbatim, evidence
digests, clarify answers, requester identity — is code-rendered from state and
filtered by a **per-queue field allowlist** (`data-platform-approvers` never
sees device info; the conversation transcript never enters a ticket).
Priority is an impact×urgency table lookup; individual-scope cases cap at P2.
A case closed after escalation keeps `outcome=ESCALATED`; follow-up messages
append ticket comments instead of re-diagnosing.

## 5. Data sources simulated and why

All six read sources + two write actions run against local fixtures — each one
exists to exercise a specific failure mode, not to pad the tool list:

- **`data/kb/` (5 Markdown docs, YAML frontmatter).** Authority tiers are the
  point: KB-1001 VERIFIED (citable), KB-1005 DRAFT (background only, Output
  Guard rejects it as a citation), KB-1004 DEPRECATED (excluded at the index
  layer, never retrievable), KB-1003 contains a **prompt-injection paragraph**
  ("ignore all previous instructions…") — only digests/snippets enter context,
  and no instruction in retrieved content can add tools or change gates.
  Retrieval is BM25 + `applies_to` hard filter (no embeddings, by design).
- **`data/status_a.json` / `status_b.json`.** Two fixtures for the same demo
  question ("Salesforce is slow"): all-green forces real investigation;
  status_b has an EU regional incident + change log, so the correct behavior is
  *inform and stop* (`INFORMED_KNOWN_INCIDENT`), proving tool results steer the
  path. Switched with `--fixture`.
- **`data/directory.json`.** Two **independent views** — `users[].groups`
  (directory) and `entitlements` (authorization) — with one planted
  contradiction (u-eve: `grafana-editors` group but only `grafana:viewer`
  entitlement) that the deterministic consistency check turns into E8.
- **IdP mock (in-process).** Account states incl. a locked account (u-alice,
  5 failed attempts) driving the full unlock write-protocol demo.
- **ITSM mock (in-process).** `create_escalation_ticket` (bookkeeping write, no
  user confirmation needed) + ticket comments for post-escalation follow-ups.
- **`config/policy.yaml`.** The single source of truth for resource enums
  (injected into intake's schema), 4 authorization rules (deny-by-default),
  3 queues with packet-field allowlists, and the priority matrix.

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
