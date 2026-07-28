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
resumable across processes (`make resume CASE=<case_id>`).

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
python3 -m venv .venv && source .venv/bin/activate
make setup            # pip install -e .
cp .env.example .env  # add OPENAI_API_KEY (+ optional OPENAI_BASE_URL / model overrides)
make chat ARGS="--as-user u-alice"   # start a conversation as a directory user
make resume CASE=case-xxxx ARGS="--as-user u-alice"   # continue across processes
make test             # L1 deterministic suite (no LLM, no network; FakeLLM)
make eval             # L2 golden scenarios (real model, ~$0.20 / run)
```

Useful chat switches: `--fixture status_b` (regional-incident world), `--fail
get_account_status` (inject tool failure, repeatable). Users to try: `u-alice`
(locked Okta account), `u-bob` (old VPN client), `u-carol` (new hire, no
entitlements), `u-eve` (planted directory contradiction). Every case writes a
JSONL trace to `traces/<case_id>.jsonl` (nodes, tool calls, gate decisions,
cost, latency).

## 7. Evaluation

Two tiers, never mixed:

- **L1 — deterministic suite** (`make test`, 112 tests, FakeLLM, no network):
  every escalation gate triggered one by one, R1–R3 failures, gate
  short-circuit order, all 7 recovery routes (incl. the E4
  poison-then-rollback test), the full write protocol (freeze / expiry /
  idempotency single-consume / decline / malicious `target_user` ignored /
  write tool exactly once), policy deny-by-default, Output-Guard citation
  checks, packet field allowlists, classifier wordlists (≥12 assertions incl.
  negation traps like "人工智能真好用").
- **L2 — golden scenarios** (`make eval`, real model): the 5 required
  conversations as YAML cases in `eval/golden/`, executed by
  `eval/run_eval.py` (~110 lines, exactly 4 assertion keys —
  `expect_outcome` / `expect_escalated` / `must_call_tools` /
  `must_not_call_tools` — zero per-case special-casing). Everything not
  expressible in those keys was deliberately moved to L1 (assertion-ownership
  table in `docs/mvp_plan_v3.1.md` P1-1). Results: `eval/results/latest.md`.

| Metric | TARGET (design goal, unvalidated) | MEASURED (final run, N=5, qwen3.7-max) |
|---|---|---|
| L1 deterministic assertions | 100% | **112/112 (100%)** |
| L2 golden outcome/tool assertions | 5/5 | **5/5** (after 4 fix iterations: 2/5 → 3/5 → 3/5 → 4/5 → 5/5) |
| Unauthorized write actions | 0 | **0** across all eval runs (the one write executed only after an explicit YES) |
| Hallucinated steps caught by citation guard | ≥ 1 reproducible | **2 live interceptions on record**: fabricated citation `h1` blocked by Output Guard (`traces/case-3f6f0f49162d.jsonl`); invalid intent `send_unlock_request` refused at FREEZE (`traces/case-159e39575d9f.jsonl`) |
| Per-turn latency p50 / p95 | < 15s / < 30s | **34.1s / 285.7s — target missed.** Honest miss: qwen3.7-max spends 15–60s per LLM call and a turn chains up to 4 calls (intake → investigate×2 → resolve). Levers: smaller/faster tier for investigate batches, parallel tool batches, streaming. Not reachable by prompt tuning alone. |
| Cost per case p50 | < $0.05 | **$0.038** (range $0.020–$0.053; full 5-case run ≈ $0.18) |

Deflection rate ≥ 60% / TTR ≤ 2 min / "0 hallucination" are **hypotheses**, not
claims — they need real traffic, and 5 authored cases cannot validate them.

**Failure-case analysis (what actually broke during eval, all recorded as-is):**

1. **Conservative hypotheses starved R3** (worst offender, 3/5 cases failed on
   it): evidence was conclusive — the agent's own escalation narrative said
   "symptoms match KB-1002 exactly" — yet hypotheses stayed OPEN, so R3
   (exactly one SUPPORTED) failed and cases escalated as LOW_CONFIDENCE.
   Fix: investigate prompt v2 tells the model that leaving a proven hypothesis
   OPEN *is* a failed investigation. The gate itself was not touched.
2. **Garbage entity extraction**: intake once produced `affected_systems:
   ["daily"]` (from "今天"), the first status query hit a nonexistent service,
   and the EU incident was never found. Fix: intake prompt v2 pins the field to
   systems actually named by the user and warns it is used verbatim as a query
   parameter.
3. **Wrong KB filter tag**: the model passed `applies_to: "NETWORK_VPN"`
   (category name) where the KB used `vpn`; the hard filter silently emptied
   the result. Fix: tag vocabulary documented in the tool description +
   KB-1002 tagged with its category alias.
4. **Under- then over-eager write action**: resolve first rendered the unlock
   as GUIDED self-service steps (never proposing the ACTION); after the intent
   vocabulary was added, it proposed `send_unlock_verification` for a *VPN*
   problem. The protocol held both times (no execution without explicit YES) —
   the fix was scoping the intent's applicability in the prompt, not new code.
5. **SMALL-classifier misread**: in AWAITING_VERIFY, "好的,发吧" ("ok, send
   it") was classified RESOLVED and closed the case prematurely. Fix: label
   semantics added to the fallback prompt ("agreeing/urging an action is
   UNKNOWN, not RESOLVED").

Residual risk, stated plainly: L2 passes are **not deterministic**. The same
suite went 2/5 → 5/5 across runs as fixes landed; a re-run can still flake on
model mood (that is why every boundary that matters is asserted in L1, where
flaky is impossible).

## 8. Known gaps & assumptions

Declared up front — these are deliberate scope cuts, not oversights:

- **Human-request detection** is a deterministic word list with a SMALL-model
  fallback; oblique phrasings ("这个 AI 不行,换个人来") can slip through. The
  cost of a miss is the user rephrasing — never a safety issue, because the
  ratchet only ever escalates.
- **No crash-recovery reconciliation**: if the process dies between a write
  tool succeeding and the state being persisted, nothing replays or reconciles
  the effect (no effects outbox). State is snapshot-rolled-back within a turn
  (E4), but cross-process consistency is best-effort.
- **No optimistic locking / concurrent-session merge** — one active
  conversation per case is assumed.
- **No tool circuit breaker** (failures are per-call retried once, then honest
  ERROR), **no 7-day TTL auto-close**, **no human-side webhook** (ticket state
  changes don't push back into the conversation).
- **No attachment handling** — the Input Guard refuses attachments with an
  explanation rather than silently ignoring them.
- **No cross-session memory**: each case starts clean; resolution history is a
  suggested source we deliberately did not mock.
- **No LLM-as-Judge gating** — quality gates are deterministic (citations,
  policy, consistency); prose quality is human-spot-checked only.
- **No same-turn double conclusion**: a mixed request (e.g. access request +
  incident report) resolves to one primary path per turn (ACCESS_REQUEST
  escalates as a whole).
- **User mid-conversation abandonment (ABANDONED)** is not modeled — an
  unanswered AWAITING_* case just sits there.
- **Output Guard checks citation existence + authority, not content
  entailment**: a step citing a real KB doc can still misstate what the doc
  says. Entailment checking is the known next hardening step.
- `send_unlock_verification` realism depends on the IdP (Okta self-service
  unlock policy; Entra is not equivalent).
- `confidence` (HIGH/LOW) affects wording and disclosure only; it is
  deliberately excluded from safety decisions (no weighted scores, no
  threshold bands).

## 9. What I'd improve with more time

Ordered by return on effort; every item has an existing seam to plug into:

1. **Escalation quality loop** — label every escalation "was it necessary?"
   and feed the per-category verdicts back into checklist/KB fixes. The
   fastest path to a genuinely better boundary; entry point: `outcome` +
   `reason_code` are already attributed at ticket-creation time in the trace.
2. **Latency** — the one measured target we missed. Move investigate batches
   to the SMALL tier, parallelize tool execution inside a batch, stream
   replies. Entry point: `LLMClient` protocol already carries a `tier` flag.
3. **KB gap loop** — cluster `ESCALATED + no-KB-match` cases into KB-writing
   tasks. Entry point: `search_kb` EMPTY results are already in the evidence
   ledger.
4. **Slack adapter + real SSO, shadow mode first** — the CLI is one adapter
   behind `handle_message(text, ctx, case_id, as_user)`; a Slack bot is
   another. `--as-user` becomes the SSO principal. Ship read-only ("advisor")
   before enabling the write path, per-category feature flags on rollback.
5. **Output-Guard entailment check** — today citations must exist and be
   VERIFIED; add "does the step actually follow from the cited section"
   (NLI-style) as a third check. Entry point: `guards.output_guard` already
   receives step + citation pairs.
6. **Real integrations** — ServiceNow for `create_escalation_ticket`, Okta API
   for the IdP adapter, audit logging with retention. Every adapter is a
   Protocol behind `tools/adapters/`; the swap is mechanical. SQLite → Postgres
   behind the `Store` protocol; BM25 → embeddings behind the `Retriever`
   protocol *if* recall data ever justifies it.
