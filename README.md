# IT Helpdesk Agent

A conversational IT support agent for employees: it diagnoses through dialogue,
investigates across mocked backend sources, resolves what it can safely resolve,
and escalates the rest with a structured handoff so the employee does not repeat
themselves.

**What this submission is:** a runnable local prototype. The CLI is the only user
entry point, every enterprise backend is a local fixture or in-process mock (the
LLM API is the one real external call), and nothing here has been run against
production traffic. It is not production-ready;
§12 lists the gaps explicitly rather than hiding them.

**Evidence policy for this README.** Every capability described below exists in
the committed source, and every measured number comes from either the committed
test suite or the committed eval result (`eval/results/latest.md`). The system
writes per-case JSONL traces at runtime for debugging, but those files are
gitignored and are therefore not used as evidence for any claim here.

---

## 1. Scope: what it does and does not do

Implemented, end to end:

- Multi-turn conversational diagnosis with clarifying questions, over a CLI chat
  loop that survives process restarts (`make chat` / `make resume`).
- Investigation across 6 read tools against local fixtures, where the second
  batch of tool calls depends on what the first batch returned.
- Three resolution outcomes without human involvement: an informational answer
  (known incident), cited step-by-step guidance, or one mocked write action.
- Escalation to a mock ITSM queue with a structured, queue-filtered handoff
  packet when the agent cannot safely converge.

Deliberately not implemented (see §12 for the full list): any real integration
(ServiceNow, Okta, Slack, SSO), a web/HTTP interface, semantic retrieval or a
vector store, resolution-history search, cross-case memory, and attachment
handling.

The only user-facing write action is `send_unlock_verification`, and it runs only
after an explicit YES. Access requests are never auto-approved — the authorization
policy is deny-by-default, so `snowflake_prod` escalates to an approver queue
instead. `create_escalation_ticket` is a system-side bookkeeping write against the
ITSM mock and does not ask the user for confirmation.

**A successful interaction, for this prototype:** the agent either answers or
guides with evidence it can cite; or it obtains explicit confirmation before the
one mocked write action; or it creates an escalation ticket carrying the relevant
context, filtered to what the receiving queue is allowed to see. The design intent
across all three is that the agent stays inside its evidence and inside its
authority; §7 describes the mechanisms that enforce the authority half of that, and
§12 is honest about where the evidence half is only partially enforced.
"Most issues deflected" and "resolved in under two minutes" are the product
hypotheses this design is aimed at, not results I can show.

## 2. The problem I chose, the user, and the interaction model

Traditional IT support pushes every issue — including the repetitive,
well-documented majority (password resets, VPN problems, access requests) —
through the same ticket → assign → investigate → back-and-forth loop. The cost is
not only agent time; it is employee hours spent waiting on issues that a system
with access to the KB, service status, and the user directory could settle in
minutes.

The user is the **employee with the IT problem**, not the helpdesk operator. That
choice drives the whole design: the agent asks the questions, holds the
investigation state, and owns the handoff — the employee is never asked to
summarize their own case for a human.

Interaction model: a CLI chat, one conversation thread per case, resumable across
processes. Identity is injected by the runtime (`--as-user`, standing in for an
authenticated SSO principal) and is never a parameter the model can set.

## 3. Why an agentic workflow

The honest test is whether the next action depends on what the previous action
returned. Here it does:

- The same sentence — "Salesforce is slow" — requires a **different tool
  sequence** depending on what `check_service_status` returns. With a known
  regional incident, the correct behavior is to inform and stop; with everything
  green, the agent has to dig into recent changes and the KB. A rule engine has to
  enumerate these branches by hand, and the branch space grows with
  category × evidence state.
- FAQ search answers documented questions. It cannot ask the right clarifying
  question when the input is "my computer is broken", and it cannot refuse to
  answer when two sources disagree.
- A single-shot chatbot has no investigation state: no record of what has been
  verified, no evidence ledger, and no boundary between what it retrieved and
  what it generated.

The division of labor is the actual design decision: **the LLM produces semantics
(hypotheses, questions, wording, plans); deterministic code judges every
boundary.** Resolve-vs-escalate, policy authorization, citation checking,
contradiction detection, the human-request word list, and the write protocol are
all plain Python with unit tests. Model output shapes classification, questions,
and investigation context — and a bad classification can absolutely send a case
down the wrong path — but the policy gates and write permissions are evaluated in
code, so no model output can grant itself an authorization it does not have.

## 4. Implemented architecture

**Topology: 5 LLM nodes + 4 code handlers + 1 shared classifier.**

LLM nodes (`src/helpdesk/orchestrator/nodes/`) produce semantics only:

| Node | Produces |
|---|---|
| `intake` | structured issue profile (category, affected systems, requested resources) |
| `investigate` | hypotheses + the next batch of tool calls (max 3 batches) |
| `clarify` | one question per information gap |
| `resolve` | a cited plan, or an action *intent* |
| `escalate` | exactly two narrative sections of the handoff packet |

Code handlers (`src/helpdesk/orchestrator/handlers.py`, `gates.py`) own the
lifecycle and never call an LLM node: `ingress` (runs on every message —
credential redaction, human-request ratchet, security fast path; sole writer of
`issue.verbatim`), `decide` (the boundary function), `act` (the EXECUTE segment of
the write protocol), `close` (settles `outcome`).

One shared `classifier` (`classifier.py`) serves three jobs — confirm
(YES/NO/OTHER), verify (RESOLVED/FAILED/UNKNOWN), escalated-followup
(RESOLVED/OTHER). It is a deterministic anchored word list with negation and
question-marker rules first, and falls back to the SMALL model only when the word
list is inconclusive.

**State machine.** 7 phases (`INTAKE → INVESTIGATING → AWAITING_CLARIFY /
AWAITING_CONFIRM / AWAITING_VERIFY → ESCALATED / CLOSED`) and a **six-row recovery
table** (`transition.py`), so each phase has exactly one entry point for the next
user message. System errors are handled **outside** this table and outside
`decide()`: the runner snapshots state before each node, rolls back on an uncaught
exception, and escalates directly with `SYSTEM_ERROR` — no stack trace reaches the
user.

**Outcome attribution.** `outcome` is written once. Agent resolutions
(`RESOLVED_BY_AGENT`, `INFORMED_KNOWN_INCIDENT`, `REDIRECTED`) are settled by
`close`. Escalations are attributed as `ESCALATED` at ticket-creation time, and if
the user later confirms the issue is fixed, `phase` becomes `CLOSED` while
`outcome` stays `ESCALATED` — so metrics count the escalation that actually
happened.

**Single-ownership state fields.** Every `CaseState` field has exactly one writer
(`issue.verbatim` → ingress, `pending_clarify_item_id` → clarify,
`declined_actions` → the confirm handler, `guard_failures` → resolve's exit
guard). The three highest-risk fields (`actor`, `evidence`, `pending_action`) are
enforced with runtime asserts. `user_requested_human` is a ratchet: once true, no
code path resets it.

**Storage.** One SQLite table, `cases` (`Store` is a Protocol, `SQLiteStore` the
only implementation). There is no events table — events go to
`traces/<case_id>.jsonl`, append-only, one file per case, carrying node
transitions, tool calls, gate decisions, latency, and profiling spans, with
prompt and completion token counts on LLM spans.

## 5. Resolution vs. escalation boundary

Every boundary decision lives in one function, `decide()`
(`orchestrator/gates.py`): it calls no LLM, opens no network connection, reads no
database, and mutates no state, and it is unit-tested gate by gate. Its inputs
are `CaseState`, deterministic YAML configuration (`categories` and `limits`),
and a clock — the latter two are injectable keyword arguments, which is how the
tests drive it. When the runner calls `decide(state)` without them, the function
falls back to the `lru_cache`d config loaders (so each YAML file is read from
disk at most once per process) and to `datetime.now(timezone.utc)`, whose only
use is a defensive pending-action expiry check that `act` re-does
authoritatively. The property that matters for the gates is that the same state
and configuration always produce the same decision, with no LLM and no
side effects — not that the call is literally IO-free.

It evaluates a frozen short-circuit order — L0 lifecycle → L1 hard red lines
→ L2 budget → L3 capability → L4 normal branches → L5 fallback — and returns a
branch plus a reason code. The table below is ordered by gate number, which is
**not** the evaluation order; the `Layer` column gives the actual short-circuit
position. E10 is the row worth noting: it sits in L1 next to E1–E3, so a plan
that has already failed the Output Guard twice escalates as `GUARD_FAILED` even
when the budget is exhausted or a critical source is dead.

| Gate | Layer | Trigger | Reason code | Behavior |
|---|---|---|---|---|
| E1 | L1 | `category == SECURITY` (the ingress security word list does not reach `decide()` — see below) | `SECURITY` | Straight to the `security-ir` queue; no diagnosis and no remediation steps — only a minimal safety caution |
| E2 | L1 | any policy decision is `DENY_REQUIRE_HUMAN` | `POLICY_REQUIRED` | Queue comes from the matching rule (e.g. `data-platform-approvers`) |
| E3 | L1 | `user_requested_human` ratchet | `USER_REQUESTED` | Immediate; outranks the budget and capability gates |
| E10 | L1 | `guard_failures >= 2` (Output Guard failed twice) | `GUARD_FAILED` | A plan that fails the citation check twice escalates instead of shipping |
| E5 | L2 | budget cap: 10 tool calls / 8 turns / $0.10 estimated / 180s (`config/limits.yaml`) | `BUDGET_EXHAUSTED` | Ticket creation itself is exempt from the budget interceptor |
| E6 | L2 | `resolution_attempts >= 2`, counted by the runner | `BUDGET_EXHAUSTED` | Repeated-failure semantics |
| E7 | L3 | a critical checklist item is `UNAVAILABLE` (tool returned ERROR) | `TOOL_UNAVAILABLE` | Disclose the dead source instead of guessing around it |
| E8 | L3 | `contradictions` non-empty (deterministic cross-source check) | `UNRESOLVED_CONTRADICTION` | Present both views, pick neither |
| E9 | L3 | `category == OUT_OF_SCOPE_NON_IT` | — | REDIRECT to the right channel; no ticket created |
| L5 | L5 | nothing above matched and no branch is available | `LOW_CONFIDENCE` | The fallback is escalation, not improvisation |
| E4 | *outside `decide()`* | uncaught node exception, handled in the runner | `SYSTEM_ERROR` | Snapshot rollback, then escalate directly |

Two escalation paths deliberately bypass `decide()` entirely. E4 is one, as the
table says. The other is the ingress security word list (§7): when it fires,
`ingress` sets the escalation reason to `SECURITY` itself and routes straight to
the escalate node, so no gate evaluation happens on that turn. E1 inside
`decide()` is the second net, catching cases the word list missed but `intake`
classified as `SECURITY`.

**Resolve gates (R1–R3; all three must PASS, no exemptions):** R1 the category is
in the auto-resolvable set; R2 every *critical* checklist item is SATISFIED; R3
exactly one hypothesis is SUPPORTED. `confidence` (HIGH/LOW, derived from two
booleans) affects wording and disclosure only — it is deliberately excluded from
every safety decision, so there are no weighted scores and no threshold bands.

An `UNKNOWN` input ("my computer is broken") is not a dead end: up to two
clarifying rounds first, then a `LOW_CONFIDENCE` escalation that carries both
answers into the packet.

**Handoff packet** (`handoff.py`). The LLM writes exactly two narrative sections
(`agent_diagnosis`, `needed_from_human`). Everything else — verbatim problem
statement, evidence digests, clarify answers, requester identity — is rendered
from state by code, then filtered through a **per-queue field allowlist** defined
in `config/policy.yaml`. `data-platform-approvers` receives no device information,
and the conversation transcript never enters a ticket on any queue. Priority is a
lookup in an impact × urgency matrix, with individual-scope cases clamped at P2.
After an escalation, follow-up messages append a ticket comment rather than
re-diagnosing the case.

## 6. Tools, mocked backends, and retrieval

**6 read tools** (visible to the model): `get_user_profile`,
`get_account_status`, `get_entitlements`, `search_kb`, `check_service_status`,
`get_recent_changes`.

**2 write tools** (never in the model's tool list, callable only with
`invoked_by="system"`): `send_unlock_verification` (requires an explicit YES
through the write protocol) and `create_escalation_ticket` (system bookkeeping, no
user confirmation).

Every tool returns a four-state envelope — `OK` / `EMPTY` / `DEGRADED` / `ERROR`
— because "searched and found nothing" is evidence, not a failure. Each call
passes three gate layers in `tools/registry.py`: a stage gate (read tools only
during investigation phases; `send_unlock_verification` only in
`AWAITING_CONFIRM`), a policy gate (deny-by-default for writes), and a runtime
gate (budget interception, actor injection, deduplication, one retry).

Backends, all local — each fixture exists to exercise a specific failure mode
rather than to pad the list:

| Backend | Serves | Why it is shaped this way |
|---|---|---|
| `data/kb/` — 5 Markdown docs with YAML frontmatter | `search_kb` | Authority tiers: KB-1001/1002/1003 VERIFIED (citable), KB-1005 DRAFT (background only, rejected as a citation), KB-1004 DEPRECATED (excluded at the index layer, never retrievable). KB-1003 also contains a prompt-injection paragraph. |
| `data/status_a.json` / `status_b.json` | `check_service_status`, `get_recent_changes` | Two worlds for one demo question: all-green forces real investigation; status_b has an EU regional incident plus a change log, so the correct behavior is to inform and stop. Switch with `--fixture`. |
| `data/directory.json` | `get_user_profile`, `get_entitlements` | Two *independent* views — `users[].groups` and `entitlements` — with one planted contradiction (u-eve is in `grafana-editors` but holds only `grafana:viewer`) that the deterministic consistency check turns into E8. |
| In-process IdP mock | `get_account_status`, `send_unlock_verification` | u-alice is locked after 5 failed attempts, which drives the full write-protocol path. |
| In-process ITSM mock | `create_escalation_ticket` | Ticket creation plus comments for post-escalation follow-ups. |
| `config/policy.yaml` | not a tool — deterministic configuration | Single source of truth for the resource enum (injected into intake's schema), 4 authorization rules, 3 queues with packet-field allowlists, and the priority matrix. |

**Retrieval is BM25 lexical search plus an `applies_to` hard filter**
(`rank-bm25`, `tools/retrieval.py`). DEPRECATED documents are dropped when the
index is built. DRAFT documents can be retrieved as background but are rejected by
the Output Guard as citations. There are no embeddings, no vector database, no
semantic search, no resolution-history archive, and no cross-case retrieval — with
5 documents, lexical search with a hard metadata filter is the honest choice, and
the authority tiering is what actually matters for safety.

Only KB digests and snippets enter the model context, and no instruction found in
retrieved content can add a tool or change a gate — the tool list and the gate
order are code, not prompt content. That is a structural argument about what
injected text can reach, not a claim that the model is immune to being misled by
document text.

## 7. Safety and the write-action protocol

**Input Guard** (`guards.py`, deterministic): credential patterns are redacted
before anything is persisted or traced; attachments are refused with an
explanation instead of being silently dropped; a security-signal word list
triggers a fast path where the case is persisted first and then lifted straight
to escalation with no LLM call at all — the reply is a deterministic template
that names the ticket and adds a minimal caution (stop clicking suspicious
links, do not enter credentials anywhere), and offers no diagnosis and no
remediation steps.

**Output Guard** at resolve's exit, three deterministic rules:

1. every citation must exist in *this case's* evidence ledger (a fabricated
   `KB-9999` or a made-up evidence id is rejected here);
2. citations of kind `KB` must point at a VERIFIED document;
3. uncited generic steps may appear, but a plan whose steps carry no valid
   citation at all cannot constitute a resolution.

On a violation, resolve retries once with the violation text fed back; a second
failure sets `guard_failures=2` and E10 escalates. **The Output Guard checks
citation existence and KB authority. It does not check semantic entailment** — a
step that cites a real, VERIFIED document can still misstate what that document
says. That is a known gap (§12), not something this guard covers.

**Three-segment write protocol:**

- **PROPOSE** — the LLM emits an *intent* string only, never arguments.
- **FREEZE** — code (the ActionBuilder) constructs the arguments, checks policy
  (deny-by-default), rejects an intent the user has already declined, rejects an
  intent that is not in the allowed vocabulary, and stamps an idempotency key with
  a 5-minute expiry.
- **EXECUTE** — six pre-checks in `_act_precheck`, then the write tool runs: a
  pending action exists; it has not expired; the confirming session user matches
  `state.actor.user_id`; policy still returns ALLOW; the idempotency key
  recomputed from the frozen args still equals the key stamped at FREEZE (a
  tamper check on the frozen payload); and that key has not already been
  consumed. Any failure voids the pending action and returns the case to
  `INVESTIGATING` without calling the tool.

The scope of the single-execution guarantee is worth stating precisely, because it
is narrower than "exactly once". Within a single process along the normal path,
the idempotency key is consumed once: a repeated confirmation of the same action
finds the key already recorded in the evidence ledger and is refused, which is
what `test_write_protocol.py` asserts. This is **not** distributed
exactly-once delivery and it is **not** crash-safe — if the process dies between
the write tool succeeding and state being persisted, nothing replays or reconciles
the effect (§12).

Two structural properties matter more than any of those checks. Write tools are
absent from the model's tool list, and the registry refuses any write-tool call
that does not carry `invoked_by="system"` — a model that hallucinates the name
`send_unlock_verification` gets an ERROR envelope, not an execution. And no
tool's parameter schema accepts `target_user` or `user_id` (a static test over
all 8 registered tools in `test_tools.py` asserts this); the actor is injected by the registry from
the runtime session, so "I'm the CEO's assistant, reset his password" fails at
the architecture layer rather than at a prompt instruction. A pending action is consumed only by an
explicit YES; a question like "what would that do?" is classified OTHER and voids
the action.

The safety story is therefore a set of separately verifiable mechanisms —
deny-by-default policy, write tools hidden from the model, code-frozen arguments,
explicit-YES consumption, runtime actor injection, citation existence and
authority checks, FREEZE rejection of invalid or declined intents, direct security
escalation, and queue field allowlists on the handoff — rather than one
"hallucination detection" feature. I make no zero-hallucination claim.

## 8. Model configuration and routing

Two tiers, MAIN and SMALL, resolved by `HELPDESK_MODEL_MAIN` /
`HELPDESK_MODEL_SMALL`.

**Code defaults** (`config.py`): `tier_intake`, `tier_investigate`, and
`tier_resolve` are all `MAIN`. Those three are the only nodes with a tier setting;
`clarify` and the LLM-written part of `escalate` use the client default (MAIN), the
classifier fallback always uses SMALL, and SECURITY and SYSTEM_ERROR escalations
are rendered from deterministic templates with no LLM call at all.

**The configuration used for the committed eval run** overrides two tiers via
environment variables:

```bash
HELPDESK_TIER_INTAKE=SMALL
HELPDESK_TIER_INVESTIGATE=SMALL   # resolve stays MAIN
HELPDESK_ENABLE_THINKING=false    # only sent when set; otherwise the server default applies
```

The recorded run used MAIN=`qwen3.7-plus` and SMALL=`qwen3.7-flash-2026-07-15`
against an OpenAI-compatible endpoint. These values live in a local `.env` that is
not committed; with the code defaults alone, all three nodes run on MAIN and the
latency and cost figures will differ.

One gap in that artifact worth stating plainly: `eval/results/latest.md` records
the two model names but **not** the three environment overrides above. So the run
configuration is documented here, in prose, rather than captured by the result
file — you cannot reconstruct it from the artifact alone. Combined with L2
non-determinism (§10), re-running `make eval` should be expected to produce
similar-shaped results, not the same numbers. Writing the resolved tier and
thinking settings into the result header is a small, obvious fix I did not make
before submitting.

One compatibility note that cost real debugging time: the DashScope-compatible
endpoint degrades structured output to `json_object` mode for the flash tier, where
the server no longer enforces the schema and the model never sees it. The client
therefore injects the JSON schema into the system message, which keeps
structured-output behavior identical across tiers.

## 9. How to run

Requires Python **3.11+** (`python3 --version`; on stock macOS use `python3.12`
from Homebrew or python.org).

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # or any python >= 3.11
make setup            # upgrades pip, then pip install -e .
cp .env.example .env  # set OPENAI_API_KEY (+ optional OPENAI_BASE_URL / model overrides)
make chat ARGS="--as-user u-alice"                      # start a conversation
make resume CASE=case-xxxx ARGS="--as-user u-alice"     # continue it in a new process
make test             # L1 deterministic suite — no LLM, no network
make eval             # L2 golden scenarios — real model, small API spend
```

`make test` needs no API key. `make chat` and `make eval` do.

Useful switches: `--fixture status_b` (the regional-incident world), `--fail
get_account_status` (repeatable tool-failure injection, drives E7). Users worth
trying: `u-alice` (locked Okta account), `u-bob` (outdated VPN client), `u-carol`
(new hire with no entitlements), `u-eve` (planted directory contradiction),
`u-dan` (the Salesforce scenario). Each turn prints a status line with case id,
phase, outcome, estimated cost, and tool-call count; each case also writes
`traces/<case_id>.jsonl`. `docs/demo_script.md` walks through two full scenarios
plus the boundary cases.

## 10. Evaluation: method and latest results

Two tiers, never mixed, because they answer different questions.

**L1 — deterministic suite** (`make test`): **112 tests**, driven by a FakeLLM
with no network access, so results are reproducible and cannot flake. Coverage by
file:

| File | Tests | Covers |
|---|---:|---|
| `test_gates.py` | 31 | E1–E3 and E5–E10 triggered one by one; E3 outranking lower layers; R1–R3 each failing and all passing; short-circuit order; the priority matrix |
| `test_routing.py` | 28 | all six recovery routes; the runner's poison-then-rollback test for E4; ≥12 classifier assertions including negation traps |
| `test_safety.py` | 28 | policy deny-by-default; a fabricated citation rejected twice → `GUARD_FAILED` with `guard_failures=2` persisted; the consistency check reproduced 10/10; the queue packet allowlists |
| `test_tools.py` | 12 | `EMPTY` is not `ERROR`; digest determinism; DEPRECATED docs unretrievable; failure injection; the static check that no tool signature accepts `target_user` or `user_id` |
| `test_write_protocol.py` | 13 | freeze; 5-minute expiry with an injected clock; single-consumption of the idempotency key; a declined intent producing no pending action; a malicious `target_user` ignored; a second confirmation of the same action running the write tool no further times |

**L2 — golden scenarios** (`make eval`): the 5 required conversation types as YAML
cases in `eval/golden/`, run against a real model by `eval/run_eval.py`, a single
small script. The runner supports exactly four assertion keys — `expect_outcome`,
`expect_escalated`, `must_call_tools`, `must_not_call_tools` — and has zero
per-case special cases; any other key is a hard error.

That assertion vocabulary is a deliberate limit, and it defines who verifies what.
L2 verifies end-to-end outcomes, the escalation decision, and which tools were
actually used. Everything narrower — exact write-tool call counts, single
consumption of the idempotency key, packet field allowlists, the priority matrix,
citation checking — is asserted in L1, where a real model cannot introduce
flakiness. Prose quality is spot-checked by hand; there is no LLM-as-judge gate.

**Latest committed run** (`eval/results/latest.md`, 2026-07-28, MAIN=`qwen3.7-plus`
/ SMALL=`qwen3.7-flash-2026-07-15`): **5/5 PASS**. That file records the model
names but not the tier and thinking overrides the run used, so treat it as a
record of one observed run rather than a reproducible configuration (§8).

| Case | Scenario | Outcome | Escalated (reason) | Tool calls | Est. cost |
|---|---|---|---|---:|---:|
| GC-01 | Locked Okta account, full loop through the write action | `RESOLVED_BY_AGENT` | no | 5 | $0.0064 |
| GC-02 | Salesforce slow → known regional incident | `INFORMED_KNOWN_INCIDENT` | no | 3 | $0.0057 |
| GC-03 | Vague VPN report, resolved after clarification | `RESOLVED_BY_AGENT` | no | 3 | $0.0061 |
| GC-04 | New-hire access request | `ESCALATED` | yes (`POLICY_REQUIRED`) | 2 | $0.0031 |
| GC-05 | Jenkins + Tableau multi-system failure | `ESCALATED` | yes (`LOW_CONFIDENCE`) | 4 | $0.0042 |

Run totals **as reported by `run_eval.py` in that file**: **10 turns, median
per-turn latency 4.4s, slowest turn 9.1s, 17 tool calls, estimated total LLM cost
$0.0256.** Those two summary figures are computed from full-precision runtime
values, while the table above is rounded for display — re-deriving from the
printed columns gives a median of 4.35s (4.3s at one decimal) and a cost sum of
$0.0255. The difference is display rounding, not a second measurement; the
full-precision values are not part of the committed artifact, so treat 4.4s and
$0.0256 as the evaluation artifact's reported values rather than something you
can reproduce by hand from the table.

Reading those numbers honestly:

- The latency unit is **wall time per user turn** across all 10 turns of the run.
  Four of those turns were handled entirely by deterministic paths (word-list
  classifier, act, close) with no LLM call and completed in under 0.05s, which
  pulls the median down; the six turns that involved at least one LLM call ranged
  from 2.9s to 9.1s. I quote the median and the maximum — both supported by the
  committed per-turn column to within display rounding — rather than the p95 that
  `run_eval.py` also prints: with only 10 samples,
  `statistics.quantiles(..., n=20)` uses the exclusive method and extrapolates past
  the largest observation, so the printed 9.4s exceeds the slowest turn that
  actually occurred. A percentile tail needs a sample size this run does not have.
  First-turn measurements from local optimization work (§11) use a different unit
  and sample again, and are not comparable to either.
- Cost is **internally estimated**, not billed: the client multiplies reported
  token counts by a local price table. `qwen3.7-plus` has no entry in that table,
  so the conservative default rate applies. The estimate is precise enough to
  drive the E5 budget gate and to compare runs; a real invoice will differ with
  the provider and the model. Per case, the estimate ranges $0.0031–$0.0064
  (median $0.0057) across these five cases.
- 5 authored cases with a real model cannot establish production accuracy. A
  re-run can flake on model behavior; the L2 suite went 2/5 → 3/5 → 3/5 → 4/5 →
  5/5 as the fixes in §11 landed. Deflection rate, time-to-resolution, and
  hallucination rate are all **unvalidated hypotheses** — they need real traffic.
  That non-determinism is precisely why every boundary that matters is asserted in
  L1.

## 11. Observed failure modes and what I changed

What actually broke while building and evaluating this, recorded as it happened.
In every case the fix was a prompt or configuration change; no gate, tool, or
state transition was modified to make a case pass.

1. **Conservative hypotheses starved R3** — the worst offender, responsible for 3
   of the 5 initial failures. The evidence was conclusive (the agent's own
   escalation narrative said the symptoms matched KB-1002 exactly) yet hypotheses
   stayed OPEN, so R3 ("exactly one SUPPORTED") failed and the case escalated as
   `LOW_CONFIDENCE`. Fix: the investigate prompt now states that leaving a proven
   hypothesis OPEN *is* a failed investigation. The gate was not touched — it was
   reporting the truth about the state it was given.
2. **Garbage entity extraction** — intake once produced `affected_systems:
   ["daily"]` from the word "今天", the first status query hit a nonexistent
   service, and the EU incident was never found. Fix: the intake prompt pins that
   field to systems the user actually named and warns that it is used verbatim as
   a query parameter.
3. **Wrong KB filter tag** — the model passed `applies_to: "NETWORK_VPN"` (the
   category name) where the KB used `vpn`, and the hard filter silently returned
   nothing. Fix: the tag vocabulary is documented in the tool description, and
   KB-1002 carries its category alias.
4. **Under- then over-eager write action** — resolve first rendered the account
   unlock as self-service guidance without ever proposing the action; after the
   intent vocabulary was added, it proposed `send_unlock_verification` for a *VPN*
   problem. The protocol held both times (nothing executed without an explicit
   YES), so the fix was scoping the intent's applicability in the prompt, not new
   code. A separate run had the model propose an intent that does not exist
   (`send_unlock_request`), which FREEZE rejected — that is the intent-vocabulary
   check, not the citation guard.
5. **SMALL-classifier misread** — in `AWAITING_VERIFY`, "好的，发吧" ("ok, send
   it") was classified RESOLVED and closed the case early. Fix: label semantics in
   the fallback prompt ("agreeing to or urging an action is UNKNOWN, not
   RESOLVED"). The deterministic word list already handled the traps it was
   written for; this was the model fallback filling a gap it should not have.
6. **Live-demo rehearsal findings** — typing "keyboard got wet" mid-demo exposed
   two real bugs: the CLI printed no reply when a closed case rolled into a new
   one (fixed in `cli.py`), and hardware faults were classified
   `OUT_OF_SCOPE_NON_IT` and redirected away (fixed in the intake prompt: hardware
   is IT; with no fitting category, use UNKNOWN and clarify).

**Latency work, labeled as what it is.** The first turn of a fresh case chains
several sequential LLM calls, and profiling showed essentially all of the wall
time was spent inside the model API rather than in local code — so the levers were
per-call, not structural. Turning off reasoning tokens gave the largest single
improvement by far; output-side prompt budgets and routing intake and investigate
to the SMALL tier accounted for most of the rest. A prompt phrasing lesson worth
recording: "at most N steps" anchors the model to produce exactly N, whereas "as
few as needed, hard cap N" does what was intended. These were single-sample local
measurements on first-turn wall time, gated on L1 and L2 staying green; they are
**not** part of the submitted evidence, and they are not comparable to the
per-turn figures in §10. The SMALL-tier rollout also introduced two regressions of
its own — investigate returning an empty tool-call batch while a critical gap was
still open, and resolve occasionally shipping a single-step plan with no citation
(caught by the Output Guard and retried) — both addressed in the prompts, with the
guard retry path deliberately kept as the backstop.

## 12. Known limitations and assumptions

Declared up front. These are scope decisions, not oversights.

**Not implemented at all:** real integrations (ServiceNow, Okta, Slack/Teams,
SSO); any web, HTTP, or streaming interface; embeddings or a vector database;
resolution-history search; cross-case memory; attachment handling; optimistic
locking or concurrent-session merge; crash-recovery reconciliation; a tool circuit
breaker; TTL-based auto-close; human-side webhooks; audit-log retention; and
semantic entailment checking in the Output Guard.

Specific limitations worth a reviewer's attention:

- **The Output Guard does not check entailment.** A step that cites a real,
  VERIFIED document can still misstate what the document says. Citation existence
  and authority are checked; meaning is not. This is the first hardening step I
  would take.
- **Human-request detection is a word list** plus a SMALL-model fallback. Oblique
  phrasing ("这个 AI 不行，换个人来") can slip past it. The ratchet only ever
  escalates, so a miss cannot cause an unauthorized action — but it is a real
  routing and user-experience failure, and I am not going to call it harmless.
- **Resource-enum mapping happens on the LLM side.** The enum and its aliases are
  injected into intake's prompt and the model emits an enum key or `other`. Terse
  phrasing can produce an empty `requested_resources`, in which case E2 is
  unreachable and the case degrades to a `LOW_CONFIDENCE` escalation on the
  generic queue instead of the approver queue. Found during a fresh-clone smoke
  test; the golden phrasing passes consistently. Deny-by-default means a missed
  mapping worsens routing rather than granting anything, but the routing miss is
  real. A deterministic alias fallback in code would close it.
- **No crash-recovery reconciliation.** If the process dies between a write tool
  succeeding and state being persisted, nothing replays or reconciles the effect.
  State is snapshot-rolled-back within a turn (E4), but cross-process consistency
  is best-effort.
- **One active conversation per case is assumed** — there is no locking or merge.
- **No same-turn double conclusion.** A mixed request (access request plus
  incident report) takes one primary path per turn; an ACCESS_REQUEST escalates as
  a whole rather than being decided item by item.
- **Abandonment is not modeled.** A case left in an `AWAITING_*` phase simply
  sits there.
- **L2 results are not deterministic**, per §10.
- **`send_unlock_verification` realism depends on the IdP** — it is modeled on
  Okta's self-service unlock policy; Entra is not equivalent.
- **`confidence` affects wording only** and is excluded from safety decisions by
  design.

## 13. Production considerations

What is already wired, and what productionizing would actually require:

- **Observability.** Every case emits an append-only JSONL trace with node
  transitions, tool calls and their four-state results, gate decisions with reason
  codes, token usage on LLM spans, and per-stage latency spans (`perf.py`
  collects spans across the runner, LLM API, tools, SQLite, policy, classifier, and
  the Output Guard). Estimated cost is accumulated on
  `CaseState.budget.llm_cost_usd` and surfaced in the CLI status line and the eval
  report rather than written into the trace — folding it into the LLM span meta is
  a small gap. `outcome` and `reason_code` are attributed at
  ticket-creation time, so escalation reasons are queryable per category without
  post-hoc inference. In production these traces would go to a log pipeline rather
  than local files, and the escalation-reason distribution is the first dashboard
  I would build.
- **Cost.** Token-based estimation feeds a live budget that gates the run (E5).
  Ticket creation is exempt from the budget interceptor, because running out of
  budget must never prevent a case from being handed off. The local price table
  needs per-model entries to be accurate for billing; today an unknown model falls
  back to a conservative default rate.
- **Latency.** Measured per turn (§10) and profiled per stage. The remaining
  cheap levers are parallelizing tool execution inside a batch and streaming the
  resolve reply for perceived latency; `complete_text` is the streaming seam and
  batches already arrive as lists.
- **Maintainability.** Policy, category checklists, and budget limits are YAML,
  not code, so a new resource or queue is a configuration change. `LLMClient` and
  `Store` are Protocols. Each backend lives in its own adapter module behind a
  single tool registry, so swapping the ITSM mock for ServiceNow or the IdP mock
  for the Okta API is localized to one adapter module — plus the credential
  handling, error mapping, and rate-limit behavior a real API brings with it. The
  entire
  deterministic core — gates, guards, routing, write protocol — is testable without
  a network, which is what keeps the 112-test suite fast enough to run on every
  change.
- **Rollout.** The CLI is one adapter over `handle_message(text, ctx, case_id,
  as_user)`; a Slack bot would be another, with `--as-user` becoming the SSO
  principal. I would ship read-only advisor mode first, with the write path behind
  a per-category feature flag, and shadow-run escalation decisions against human
  triage before letting the agent create tickets directly.

## 14. What I would do next

Ordered by return on effort:

1. **Escalation-quality loop.** Label each escalation "was it necessary?" and
   feed per-category verdicts back into checklist and KB fixes. This is the
   fastest path to a genuinely better boundary, and the trace already carries the
   attribution needed to sample it.
2. **Output-Guard entailment check.** Add a third check — does the step actually
   follow from the cited section — alongside existence and authority. The guard
   already receives step and citation pairs.
3. **Deterministic resource-alias fallback**, closing the routing gap in §12
   without moving any authorization decision into the model.
4. **KB gap loop.** Cluster `ESCALATED` cases that found no KB match into
   KB-writing tasks; `search_kb` EMPTY results are already recorded as evidence.
5. **Slack adapter and real SSO**, read-only first, per the rollout plan in §13.
6. **Real integrations and durability.** ServiceNow for ticket creation, the Okta
   API for the IdP adapter, audit logging with retention, SQLite → Postgres behind
   the `Store` Protocol, and an effects outbox so a write can be reconciled after a
   crash. BM25 → embeddings only if recall data ever justifies it.
