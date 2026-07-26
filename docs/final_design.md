# IT Helpdesk Agent — 最终设计（Final Design）

> Version 2.0 · 收敛版本，取代 `architecture.md` v1.0 与 `workflow.md` v1.0 中的冲突条款
> 上游：`docs/requirements.md` · 输入：`docs/review.md`
> 本文是**唯一权威设计**。v1 文档保留为设计过程记录，两者冲突时以本文为准。

---

## 0. 本次优化做了什么

Review 指出三类问题：主流程状态机自相矛盾、范围远超 take-home 交付能力、部分声明缺乏证据。本次优化的原则是**先让它自洽可实现，再让它可扩展**，并主动删掉一批"看起来专业但这次不会实现"的东西。

### 0.1 修复的硬伤（会阻断实现）

| # | v1 的问题 | v2 的处置 | 位置 |
|---|---|---|---|
| 1 | 写操作确认路径断裂（"发"被路由到 verify） | 拆出独立等待态 `AWAITING_CONFIRM` + 独立恢复点 + 纯代码 `act` 节点 | §2.3 §2.4 |
| 2 | 升级后同时是 `ESCALATED` 和 `CLOSED` | 生命周期与业务结论解耦：`ESCALATED` 是持久等待态，`CLOSED` 只由人工结案/用户确认/TTL 触发 | §2.7 |
| 3 | critical 证据缺失能否 RESOLVE 有三种说法 | 单一规则：critical 缺失一律不可 RESOLVE；仅允许显式替代源，且置信上限降档 | §2.6 |
| 4 | 写操作"提议→策略→确认→执行"协议不完整 | 定义三段协议，args 由代码冻结，执行前二次校验 policy 与 actor | §2.4 |
| 5 | 安全直升时可能没有 durable case | `ingress` 无条件先建最小 case 并落盘，再路由 | §2.3 |
| 6 | Handoff Packet 无字段最小化 | 按接收队列定义字段 allowlist，transcript 改为短期授权链接 | §2.8 |

### 0.2 主动删除的复杂度

删除标准：**这次不会实现，且它的存在会让人以为已经实现**。

| 删除项 | 理由 |
|---|---|
| Rolling Summary 记忆层 | 单 case 硬上限 12 轮，CaseState 已是主干，摘要层收益≈0 |
| 跨会话长期用户记忆（90 天案例、沟通偏好） | 复制权威目录的易变数据 + 隐私面扩大，收益不明确 |
| 四档模型路由 | 收敛为两档（MAIN / SMALL），少两套 prompt 与两条回归线 |
| 多供应商 LLM Gateway 故障转移 | 不同模型的 structured output 行为不等价，"自动 fallback"是伪可靠性 |
| Redis + Postgres + Vector DB + K8s HPA | MVP 单进程 + SQLite + 内存检索即可；接口保留，实现延后 |
| PagerDuty `page_oncall` | 不能由对话分类 + 用户自述紧急度触发真人呼叫 |
| 固定三工具首轮并行 | 改为按类目证据清单生成首批查询，避免无意义调用 |
| 自主 KB 写回、主动推送、A/B 框架、语音通道 | 明确列为 Future，不出现在能力描述中 |

### 0.3 降级为"启发式"的声明

`confidence` 改名 `readiness`，明确为**未校准的启发式闸门分**，不解释为正确概率；且**不再由它单独决定 resolve/escalate**（见 §2.5）。所有指标目标（deflection 60%、TTR、成本）统一标注为 `TARGET (unvalidated)`，与实测值分列。

---

## 1. 最终架构

### 1.1 定位与设计公理

**定位**：面向企业员工的 L1/L1.5 IT 支持首触点。它是一个**受确定性工作流约束的、有状态的工具使用型 Agent**（bounded stateful tool-using agent），不是自治 Agent，不是 Multi-Agent，也不是 RAG chatbot。

四条公理，后续所有取舍都可以回推到它们：

| # | 公理 | 推论 |
|---|---|---|
| A1 | **边界由代码裁决，语义由模型生成** | 升级/权限/预算/阶段推进不进 LLM；诊断措辞、假设文本、交接叙述交给 LLM |
| A2 | **权限在工具层，不在提示词** | Prompt injection 的唯一可靠防线是"模型手上根本没有那个工具" |
| A3 | **无引用不成方案** | procedural 输出必须锚定到本 case 证据账本中真实存在的 VERIFIED 来源 |
| A4 | **所有死路的出口都是人** | 预算耗尽、工具故障、状态损坏、未预见分支，兜底一律 ESCALATE，不是"抱歉帮不了你" |

### 1.2 为什么需要 Agent（而不是规则引擎 + RAG）

这是面试第一个会被问的问题，设计上必须能站住。

| 环节 | 是否需要 LLM | 说明 |
|---|---|---|
| 意图分类、实体抽取 | 弱需要 | 分类器也能做，但自然语言到 `deadline/onset/scope/tried_by_user` 的抽取用 LLM 成本更低 |
| **假设生成与工具选择** | **强需要** | 这是不可预先写死的部分：第 2 批查什么取决于第 1 批查到了什么 |
| 证据解读与矛盾识别 | 强需要 | "Tableau 陈旧是 Jenkins 的下游效应，不是独立故障"这类判断无法枚举 |
| 分支裁决（解决 vs 升级） | **不需要** | 纯规则，必须可测可审计 |
| 步骤落地到本人环境 | 中需要 | KB 是通用的，输出要贴合 macOS 14.5 / VPN 1.8.2 |
| 交接包叙述 | 中需要 | 结构来自代码，只有 `agent_diagnosis` / `needed_from_human` 两段是生成的 |

**判据**：如果去掉 LLM，`investigate` 就退化成固定 checklist，多系统故障那类问题（`MULTI_SYSTEM`）就无法处理。MVP 必须有一个 golden case 明确演示"第二个工具的选择依赖第一个工具的结果"（GC-031，见 §5.5）。

### 1.3 分层架构

```
┌────────────────────────────────────────────────────────────┐
│ Channel        CLI  │  Web Chat (SSE)  │  REST API          │   ← MVP 实现
│                Slack / Teams Adapter                        │   ← 接口预留
└──────────────────────────┬─────────────────────────────────┘
                           │ IncomingMessage{actor(来自认证), text, session_id}
┌──────────────────────────▼─────────────────────────────────┐
│ Guard          身份绑定 · 限流 · 凭据/注入/滥用检测 · 附件拒收 │
└──────────────────────────┬─────────────────────────────────┘
┌──────────────────────────▼─────────────────────────────────┐
│ Orchestrator   纯 Python 状态机：10 节点，单一 transition()  │
│                ┌─────────────────────────────────────────┐  │
│                │ CaseState (单一事实源)                   │  │
│                │  ├ issue / actor / evidence / hypotheses │  │
│                │  ├ checklist / budget / policy_decisions │  │
│                │  └ pending_action / escalation / outcome │  │
│                └─────────────────────────────────────────┘  │
│    拦截器：预算 · 去重 · 循环检测 · 快照回滚 · 审计事件        │
└──────────────────────────┬─────────────────────────────────┘
┌──────────────────────────▼─────────────────────────────────┐
│ Capability     LLM Client (MAIN/SMALL, structured output)   │
│                Policy Engine (YAML, deny-by-default)        │
│                Tool Registry (阶段门控 + 策略门控 + 运行门控) │
│                Retriever (BM25 + Embedding, RRF)            │
│                Output Guard (引用存在性 + 蕴含抽检)          │
└──────────────────────────┬─────────────────────────────────┘
┌──────────────────────────▼─────────────────────────────────┐
│ Adapter        KB · Status · Directory · History · Policy   │  ← MVP: 本地 Fixture
│                ITSM(建单) · IdP(账号)                        │  ← MVP: Mock 实现
│                全部走 Protocol 接口，Mock 与真实同一契约      │
└──────────────────────────┬─────────────────────────────────┘
┌──────────────────────────▼─────────────────────────────────┐
│ Persistence    SQLite: cases · events(append-only) · effects│
│ Observability  JSONL trace · metrics rollup · audit log     │
└────────────────────────────────────────────────────────────┘
```

**关键结构性决定**：Adapter 层是唯一的可替换面。MVP 的 Mock 与生产的真实实现共用同一个 Protocol 与同一套契约测试（§3.4），因此 Demo 环境和生产是同一条代码路径。

### 1.4 CaseState（精简后的单一事实源）

相比 v1 删掉了 `summary`、`tool_health`（移到 Adapter 层共享）、长期记忆引用；新增 `pending_action`、`lifecycle`、`readiness`。

```yaml
CaseState:
  # ── 标识与生命周期 ──
  case_id, session_id, created_at, updated_at
  version: int                      # 乐观锁，防并发双写
  phase:     INTAKE | INVESTIGATING | AWAITING_CLARIFY | AWAITING_CONFIRM
           | AWAITING_VERIFY | ESCALATED | CLOSED
  lifecycle: ACTIVE | WAITING_USER | WAITING_HUMAN | DONE
  outcome:   null | RESOLVED_BY_AGENT | INFORMED_KNOWN_INCIDENT
           | ESCALATED | REDIRECTED | ABANDONED       # 业务结论，与 phase 解耦

  # ── 对话 ──
  messages: [{turn_id, role: USER|AGENT|SYSTEM|HUMAN, content, ts}]   # 全量存库，取最近 8 轮进上下文
  turn_count: int

  # ── 身份（只读，来自认证 + 目录）──
  actor:
    user_id, email, display_name, department, title, manager_id
    location: {office, region, timezone}
    employment: {status, start_date, tenure_days}     # tenure_days < 7 是强先验
    device: {model, os, vpn_client_version}
    groups: [str]
    entitlements: [str]
    profile_loaded: bool

  # ── 问题 ──
  issue:
    verbatim: str                   # intake 写入一次后不可变
    normalized: str
    category: ACCOUNT_AUTH | APP_PERFORMANCE | NETWORK_VPN | ACCESS_REQUEST
            | SOFTWARE_LICENSE | DEVICE | MULTI_SYSTEM
            | SECURITY | OUT_OF_SCOPE_IT | OUT_OF_SCOPE_NON_IT | UNKNOWN
    urgency: LOW | NORMAL | HIGH | CRITICAL
    deadline: timestamp | null
    scope: INDIVIDUAL | TEAM | REGION | GLOBAL | UNKNOWN
    onset: {at, relative}
    affected_systems: [str]

  # ── 收集到的信息 ──
  collected:
    checklist: {item_id: PENDING | SATISFIED | UNAVAILABLE | SUBSTITUTED}
    from_user: [{question, answer, turn_id}]
    tried_by_user:  [{step, outcome: WORKED|FAILED|UNKNOWN}]
    tried_by_agent: [{action, outcome, ts}]
    clarify_count: int              # 硬上限 2

  # ── 证据（append-only）──
  evidence:
    - {id, tool, args_hash, status: OK|EMPTY|DEGRADED|ERROR,
       digest, raw_ref, source_ref, freshness_sec, latency_ms, ts}
  tool_cache: {args_hash: evidence_id}

  # ── 诊断 ──
  hypotheses:
    - {id, text, status: OPEN|SUPPORTED|REFUTED,
       supporting: [eid], refuting: [eid],
       discriminator: {type: TOOL|QUESTION, ref}}
  contradictions: [{description, involved: [eid], resolved: bool}]
  diagnosis:
    root_cause, explanation
    resolution_type: INFORMATIONAL | GUIDED | ACTION | null
    steps: [{text, citation, citation_kind: KB|GENERIC}]
    guard_passed: bool

  # ── 裁决输入（decide 独占写）──
  readiness:                        # 启发式闸门分，非概率
    score: float
    band: HIGH | MEDIUM | LOW
    components: {evidence_completeness, kb_match, history_precedent,
                 hypothesis_dominance, contradiction_penalty, staleness_penalty}
    gates: {R1..R7: PASS|FAIL}      # 每条门的结果显式落盘，用于审计与 golden case 断言
    computed_at

  # ── 受控写操作 ──
  pending_action:                   # 由代码创建与冻结，模型不可改
    {action_id, tool, args_frozen, policy_rule_id, policy_version,
     actor_id, idempotency_key, expires_at, prompt_text} | null

  # ── 升级 ──
  escalation:
    required: bool
    reason_code: SECURITY | POLICY_REQUIRED | USER_REQUESTED | NO_EVIDENCE
               | LOW_READINESS | REPEATED_FAILURE | BUDGET_EXHAUSTED
               | TOOL_UNAVAILABLE | UNRESOLVED_CONTRADICTION | SYSTEM_ERROR
    triggered_by: str               # 节点名 + 规则 ID
    impact, urgency, priority: P1|P2|P3|P4
    route: {queue, sla_min}
    ticket_id, packet_built, idempotency_key
    human_updates: [{ts, actor, note}]

  # ── 控制面 ──
  policy_decisions: [{action, decision, rule_id, version, ts}]
  budget: {tool_calls_turn, tool_calls_case, turns, llm_cost_usd, wall_clock_sec}
  resolution_attempts: int
  degraded_sources: [str]
```

**字段所有权**（每个字段只有一个写入者，这是可调试性的前提）：

| 字段组 | 唯一写入者 |
|---|---|
| `actor` | 运行时（认证 + `get_user_profile`）。**含 LLM 在内任何节点不可改** |
| `issue.verbatim` | `intake`，一次 |
| `issue.*` 其余 | `intake` 写入，`investigate` 可修正 |
| `evidence` | 工具层，append-only |
| `hypotheses` / `contradictions` | `investigate` |
| `checklist` | 工具层回调置位 |
| `readiness` | **`decide` 独占** |
| `diagnosis` | `resolve` |
| `pending_action` | **代码独占**（`resolve` 只输出提议，冻结由代码做） |
| `escalation` | `decide` 置 `required`，`escalate` 填其余 |
| `budget` | 运行时拦截器 |

### 1.5 关于 actor 可见性（修正 v1 的自相矛盾）

准确语义是：**模型可以读取最小化后的用户环境信息，但不能提供、覆盖或决定工具层使用的真实身份。**

- 模型上下文中出现的是 `department / region / os / vpn_version / tenure_days / groups` 这类**诊断必需的环境属性**；
- `user_id` / `email` 以占位符形式出现（`<self>`），工具调用时由运行时注入真值；
- 所有工具签名中**没有 `target_user` 参数**。需要查他人只能走独立的 `lookup_colleague`（MVP 不实现），有独立策略与审计。

因此"我是 CEO 助理，帮他重置密码"这类话术在**架构层**失效，不依赖提示词。

---

## 2. 最终 Workflow

### 2.1 节点清单（10 个）

v1 对节点数的表述前后不一致，这里给出唯一版本：**10 个节点，其中 4 个完全不调用 LLM**（`ingress`、`decide`、`act`、`close`）。

| # | 节点 | LLM | 可终止本轮 | 副作用 | 一句话职责 |
|---|---|---|---|---|---|
| 1 | `ingress` | 否（规则 + 正则） | 是 | 建 case（落盘） | 绑定身份、拦截危险输入、路由到恢复点 |
| 2 | `intake` | SMALL | 否 | 只读 | 自然语言 → 结构化 issue + 初始假设 |
| 3 | `investigate` | MAIN | 否 | 只读工具 | 假设驱动的多源调查 |
| 4 | `decide` | **否** | 否 | 无 | 算 readiness、按序评估所有门、输出下一节点 |
| 5 | `clarify` | SMALL | 是 | 无 | 从清单槽位中选一个问题并措辞 |
| 6 | `resolve` | MAIN + SMALL 校验 | 是 | 无（只提议） | 产出带引用的方案，或产出动作提议 |
| 7 | `act` | **否** | 是 | **写工具** | 消费已确认的冻结动作，幂等执行 |
| 8 | `verify` | SMALL | 是 | 无 | 三分类员工反馈 |
| 9 | `escalate` | SMALL（仅两段叙述） | 是 | **建单** | 渲染交接包、定级、路由、建单 |
| 10 | `close` | 否 | 是（终态） | 写历史库 | 结算 outcome、写回、发指标 |

**修正 v1 的"所有分支只在 decide"**：准确表述是 —— **"解决 vs 升级"这一业务裁决只发生在 `decide`**。`ingress` 有生命周期路由分支，`verify` 有三分类分支，`resolve` 有 guard 出口分支，这些是不同性质的分支，不冲突。

**节点 vs 处理器**：另有两个纯代码 handler —— `confirm_router`（判定用户是否许可某动作）与 `escalate_followup`（把升级后的追问追加到工单）—— **不计入节点数**。判据一致：它们没有独立的状态转移权，只是把消息导向既有节点或既有副作用。全文提到"节点"一律指上表 10 个。

### 2.2 执行图

```
                       ┌──────────┐
   user message ──────►│ ingress  │──REJECT──────► 安全模板 (终止)
                       └────┬─────┘
        ┌───────────────────┼────────────────────────────┐
        │ 新 case           │ 恢复（见 §2.3）              │ 安全信号
   ┌────▼─────┐             │                        ┌────▼─────┐
   │  intake  │             │                        │ escalate │
   └────┬─────┘             │                        └──────────┘
        │                   │
   ┌────▼──────────────────────────────────────┐
   │              decide  (纯代码)               │◄──────────────┐
   │      门序：硬红线 → 预算 → 能力 → 正常分支    │               │
   └──┬──────┬───────────┬──────────┬───────────┘               │
      │ASK   │INVESTIGATE│RESOLVE   │ESCALATE / REDIRECT        │
 ┌────▼────┐ ┌───▼───────┐ ┌────────▼─┐  ┌────────▼──────┐      │
 │ clarify │ │investigate│ │ resolve  │  │   escalate    │      │
 └────┬────┘ └───┬───────┘ └────┬─────┘  └───────┬───────┘      │
      │          └──────────────┼────────────────┼──────────────┤
      │ 等用户回答               │  guard 失败     │              │
      │                         │                │ 人工结案      │
      │           ┌─────────────┼─────────┐      │              │
      │           │ INFORMATIONAL         │ACTION │              │
      │           │             │GUIDED   │      │              │
      │      ┌────▼───┐    ┌────▼────┐ ┌──▼───┐  │              │
      │      │ close  │    │ (等确认) │ │(等确认)│  │              │
      │      └────────┘    └────┬────┘ └──┬───┘  │              │
      │                         │ 用户确认 │      │              │
      │                    ┌────▼─────┐   │      │              │
      │                    │   act    │◄──┘      │              │
      │                    └────┬─────┘          │              │
      │                         │                │              │
      │                    ┌────▼────┐           │              │
      └───────(下一轮)──────│ verify  │──失败─────┼──────────────┘
                           └────┬────┘           │
                                │ 成功            │
                           ┌────▼────┐           │
                           │  close  │◄──────────┘
                           └─────────┘
```

### 2.3 生命周期与恢复路由（修复 P0-1、P0-2、P0-5）

**每个等待态有且只有一个恢复点**，这是 v1 最大的实现障碍所在。

| `phase` | 这条消息的语义 | 恢复到 | 说明 |
|---|---|---|---|
| （无 case） | 新问题 | `intake` | `ingress` **先建最小 case 并落盘**，再决定去向 |
| `AWAITING_CLARIFY` | 澄清问题的回答 | `investigate` | 答案并入 `collected.from_user` |
| `AWAITING_CONFIRM` | **对某个动作的许可/拒绝** | `confirm_router`（代码） | YES→`act`；NO→`decide`；其他→`investigate`（视为补充信息，动作作废） |
| `AWAITING_VERIFY` | 方案是否生效的反馈 | `verify` | |
| `INVESTIGATING` | 中途补充信息 | `investigate` | |
| `ESCALATED` | 升级后的追问 | `escalate_followup`（代码 + 追加工单评论） | **Agent 转观察者，case 不关闭** |
| `CLOSED` | 新问题或复发 | `intake` | 新 case，`linked_case_id` 指向旧 case |

`confirm_router` 是纯代码判定（YES/NO 词表 + SMALL 模型二分类兜底），不是节点，因为它没有独立副作用。**关键点：v1 中"发"会落进 `verify`，v2 中它落进 `AWAITING_CONFIRM` 的恢复点，写操作因此真的会被执行。**

**`ingress` 无条件先建 case**（修复 P0-5）：即使命中安全信号需要直升 `escalate`，也必须先写入 `case_id`、`issue.verbatim`、`security_signal`、认证结果、审计时间戳、幂等键。安全事件是最需要审计主键的路径，不能是唯一没有 case 的路径。

### 2.4 写操作三段协议（修复 P0-4）

这是全系统安全性最集中的地方，必须写成可直接编码的形式。

```
① PROPOSE   [resolve, LLM]
   模型输出的是「动作提议」，不是 tool call：
     {intent: "unlock_account", rationale: str, target: "<self>"}
   模型不产出最终参数，不能调用写工具（写工具不在它的工具列表里）。

② FREEZE    [代码]
   a. 由 ActionBuilder 根据 intent + CaseState 生成完整 args（模型不参与）
   b. policy.check(actor, tool, args) → ALLOW / ALLOW_WITH_CONFIRM / DENY
      DENY → 不生成确认，直接置 escalation.required
   c. 写入 pending_action，冻结：
        tool, args_frozen, actor_id, policy_rule_id, policy_version,
        idempotency_key = sha256(case_id | tool | args_frozen),
        expires_at = now + 5min
   d. 生成给用户的确认话术（明确说清副作用与影响面）

③ EXECUTE   [act, 纯代码]
   前置校验（任一失败即作废并回 decide）：
     - pending_action 未过期
     - actor 与冻结时一致
     - policy_version 未变，且 policy.check 重新判定仍为 ALLOW*
     - idempotency_key 未被消费过
   执行：
     - 先写 effects 表 (key, tool, args_hash, status=INTENT)   ← outbox
     - 调用工具
     - 更新 effects (status=DONE|FAILED, result_ref)
   完成后 pending_action = null，phase = AWAITING_VERIFY
```

三点直接回应 review 的 TOCTOU 质疑：**args 由代码生成而非模型**、**执行前重新跑一次 policy**、**effects 表先写意图后写结果**（进程崩溃后可对账，避免"外部调用成功但本地无记录"）。

**副作用确认政策**（修复 v1 的自相矛盾）—— 按副作用性质分三类，不再是"全部写工具都要确认"：

| 类别 | 例子 | 是否需用户确认 | 必需属性 |
|---|---|---|---|
| **用户影响型** | 发解锁邮件、发重置链接、清除 IdP 会话 | **是**，走三段协议 | 幂等 + 审计 + 影响面告知 |
| **代表用户对外发起** | 提交权限审批申请 | **是**（内容需用户确认） | 幂等 + 审计 + 申请单可见 |
| **系统记账型** | 建升级工单、写 resolution history、发指标 | 否 | 幂等 + 审计 |

`clear_idp_sessions` 从 v1 的"低风险"上调为**高影响**：它会中断用户在所有设备上的会话，确认话术必须显式说明这一点，且不进 MVP 默认开启集。

### 2.5 `decide` 的裁决顺序

纯函数：`decide(CaseState) -> (Branch, reason_code, readiness)`。严格按序短路。

```
第 0 层 · 生命周期
  L1  phase == ESCALATED               → OBSERVE（追加工单，不重新诊断）
  L2  pending_action 已过期              → 作废并告知，继续往下

第 1 层 · 硬红线（不看任何分数）
  E1  issue.category == SECURITY        → ESCALATE, 隔离队列, agent_may_advise=false
  E2  policy 判定 DENY_REQUIRE_HUMAN     → ESCALATE, 规则指定队列
  E3  用户明确要求人工                    → ESCALATE, 立即, 无挽留
  E4  SYSTEM_ERROR（状态损坏/节点异常）    → ESCALATE

第 2 层 · 预算
  E5  tool_calls_case ≥ 20 | turns ≥ 12 | cost ≥ $0.15 | wall_clock ≥ 15min
  E6  resolution_attempts ≥ 2

第 3 层 · 能力
  E7  critical checklist 项为 UNAVAILABLE 且无替代源  → ESCALATE(TOOL_UNAVAILABLE)
  E8  unresolved_contradictions > 0                  → ESCALATE(UNRESOLVED_CONTRADICTION)
  E9  category == OUT_OF_SCOPE_IT                    → ESCALATE
  E10 category == OUT_OF_SCOPE_NON_IT                → REDIRECT（不建单）

第 4 层 · 正常分支
  R 门全过（§2.6）                                    → RESOLVE
  存在"只能问人"的缺口 且 clarify_count < 2            → ASK
  证据未齐 且 工具预算有余                              → INVESTIGATE

第 5 层 · 兜底
  以上都不匹配                                         → ESCALATE(LOW_READINESS)
```

**兜底是升级不是重试**（A4）。任何未预见的状态组合都落到人工。

### 2.6 RESOLVE 门与 readiness（修复 P0-3、降级置信度声明）

**这是相对 v1 最重要的一处设计改动：把"能不能解决"从分数判定改为门判定，分数只决定"怎么说"。**

理由：v1 让一个未经校准的加权分（0.55 / 0.80 阈值）承担安全边界，这在没有标注数据前是不可辩护的。而实际上真正的安全条件本来就是离散的 —— 证据齐了吗、假设收敛了吗、引用合法吗、权限够吗。

**R 门（全部 PASS 才允许 RESOLVE，无任何豁免）**

| 门 | 条件 | 备注 |
|---|---|---|
| R1 | `category ∈ AUTO_RESOLVABLE` | 配置在 `categories.yaml` |
| R2 | **所有 critical checklist 项 ∈ {SATISFIED, SUBSTITUTED}** | `UNAVAILABLE` 一律 FAIL，**不可豁免** |
| R3 | 恰好 1 个 SUPPORTED 假设，其余非 SUPPORTED | 竞争性假设 = FAIL |
| R4 | `unresolved_contradictions == 0` | 不可豁免 |
| R5 | `resolution_attempts < 2` | |
| R6 | 动作在权限内（GUIDED/INFORMATIONAL 恒真；ACTION 需 policy ALLOW） | |
| R7 | Output Guard 通过（引用存在且 VERIFIED） | 在 `resolve` 出口检查，失败回 `decide` |

**替代源规则**（唯一的例外通道，取代 v1 三种互相矛盾的说法）：某个 critical 项可标为 `SUBSTITUTED`，当且仅当它在 `substitutes.yaml` 中显式声明了替代源且替代源取到了数据。代价固定：**readiness band 上限压到 MEDIUM**，且回复中必须声明数据缺口。

| 失效源 | 允许的替代 | 代价 |
|---|---|---|
| `check_service_status` | 向用户提问"同事是否也受影响" | band ≤ MEDIUM |
| `get_user_profile` | 向用户询问设备/地点（此时提问合理） | band ≤ MEDIUM，多 1 次 clarify |
| `search_kb` | `search_resolution_history` | band ≤ MEDIUM，**且历史不能作为唯一 citation**（见 §2.9） |
| `get_account_status` | 无 | 直接 ESCALATE |

**readiness 分（启发式，只影响措辞与退出路径）**

```
base = 0.35·evidence_completeness + 0.30·kb_match
     + 0.20·history_precedent     + 0.15·hypothesis_dominance
penalty = 0.25·unresolved_contradictions + 0.15·critical_tool_failures
        + 0.10·stale_critical_sources
readiness = clamp(base − penalty, 0, 1)
```

| band | 边界（左闭右开，消除 v1 的 0.80 重叠） | 行为 |
|---|---|---|
| HIGH | `[0.80, 1.0]` | 陈述性措辞，直接给方案 |
| MEDIUM | `[0.55, 0.80)` | 给方案 + 显式声明不确定 + **一键转人工**（保留全部上下文） |
| LOW | `[0, 0.55)` | 已通过 R 门但分低：仍给方案，但强制附一键转人工，且**不计入 deflection**，标记 `low_readiness_resolution` 供离线复盘 |

权重来源必须在配置文件注释中写明「hand-set prior, uncalibrated」，并在 README 中说明校准计划：先收集 ≥200 个已标注 case，按 category 分别做阈值扫描与可靠性图，再谈"校准"。

### 2.7 升级生命周期（修复 P0-2）

**业务结论（`outcome`）与生命周期（`phase`）解耦。**

```
decide(ESCALATE)
   ↓
escalate: 建单 → phase = ESCALATED, lifecycle = WAITING_HUMAN
                 outcome = ESCALATED        ← 指标此刻即可归因
   ↓
   ├─ 用户继续说话      → escalate_followup：追加到工单评论，phase 不变
   ├─ 人工在工单侧结案   → close(resolved_by=HUMAN)
   ├─ 用户说"已经好了"   → close(resolved_by=HUMAN_OR_EXTERNAL)
   └─ 7 天无更新        → close(outcome 保持 ESCALATED, note=TIMEOUT)
```

`close` 不再是 `escalate` 的直接后继。这同时修好了三件事：升级后的追问不会被当成新问题、人工结案成为历史写回的可靠触发点、TTR/deflection 指标不被虚增。

**指标口径**（防止 review 指出的"状态定义污染指标"）：

| outcome | 计入 deflection | 计入 TTR | 写 resolution_history |
|---|---|---|---|
| `RESOLVED_BY_AGENT`（verify 成功） | 是 | 是 | 是，`resolved_by=AGENT` |
| `INFORMED_KNOWN_INCIDENT` | **否**（单列"事故告知"） | 否 | 是，但标 `not_a_resolution` |
| `ESCALATED` | 否 | 否 | 是，`resolved_by=HUMAN` 或空 |
| `REDIRECTED` | 否 | 否 | 否 |
| `ABANDONED` | 否 | 否 | 否 |

`INFORMATIONAL` 路径（已知事故告知）不再声称已解决，也不承诺"恢复后通知你" —— 恢复订阅由状态系统/ITSM 持有，Agent 不做长时等待任务（对齐 A4 与"无自主长时任务"这一非目标）。

### 2.8 定级、路由与 Handoff Packet（修复 P0-6）

**定级改为 ITIL 式 impact × urgency 二维矩阵**，用户自述的 deadline 只能抬 urgency，不能单独造出 P1：

```
impact  = f(scope, service_tier)
          GLOBAL/REGION + tier-1 服务 → HIGH
          TEAM                        → MEDIUM
          INDIVIDUAL                  → LOW
urgency = f(deadline, 业务阻塞程度, 用户表述)
          deadline < 15min → HIGH ；< 60min → MEDIUM ；否则 LOW

priority = matrix[impact][urgency]
           P1 仅当 impact=HIGH（需由 status API 的事故信号佐证）或 SECURITY
           纯个人 deadline 最高只能到 P2
```

MVP **不实现 `page_oncall`**。真人呼叫不能由对话分类触发。

**队列级字段 allowlist**（同一份 CaseState 渲染出不同的包）：

| 接收队列 | 允许字段 | 明确排除 |
|---|---|---|
| `it-helpdesk`（通用） | 姓名、部门、地点、设备、issue、investigated、hypotheses、tried、needed_from_human | 邮箱、经理、entitlements 全量、raw evidence |
| `identity-support` | 上述 + 邮箱 + 账号状态证据 | entitlements 全量、无关系统证据 |
| `data-platform-approvers` | 姓名、部门、经理、申请资源、业务用途、现有 entitlements 中**与该资源相关的部分** | 设备信息、无关证据、transcript |
| `security-ir`（隔离数据域） | case_id、时间线、原始信号、认证结果 | **不进普通 IT 队列，不进通用 IM 群**，Agent 不给任何建议 |

`transcript` 与 `raw evidence` **默认不复制进工单正文**，改为「短期（24h）、单 case、需登录、被审计」的授权链接。这一条把"一次错误路由 = 横向 PII 泄露"降级为"一次错误路由 = 一个会过期的无效链接"。

Packet 结构本身由代码从 `evidence` / `hypotheses` 直接渲染，LLM 只写两段：`agent_diagnosis` 和 `needed_from_human`。

**不做无依据的承诺**：交接话术只说「已转 X 队列，工单 INC-xxxx，目标响应 30 分钟」这类有工单数据支撑的内容。「张伟正在值班，10 分钟内联系你」这类需要排班 + 接单回执才能说的话，从模板中删除。

### 2.9 证据权威分级

Review 指出的自我污染回路，用一条分级规则封死：

| 来源 | 可作为 procedural step 的 citation | 可参与 readiness | 说明 |
|---|---|---|---|
| KB `VERIFIED` | **是** | 是（`kb_match`） | 唯一可执行权威 |
| KB `DRAFT` | 否（仅背景） | 是，× 0.5 | |
| KB `DEPRECATED` | 否 | 记 0 | |
| Resolution History | **否**，仅作为调查线索与假设来源 | 是（`history_precedent`，上限 0.2 权重） | 未经人工审核的历史不具备执行权威 |
| 通用排查白名单（重启/换网络/清缓存） | 是，但标注 `citation_kind=GENERIC` | 否 | **不能单独构成一次"已解决"** |

历史工单在被检索使用前，用户输入片段需二次转义并包裹在 `<untrusted_data>` 中，且**对其他员工的可识别信息做脱敏后入索引**（在建索引时做，不是在检索时做）。

写回也收紧：`verify` 成功只记录 `user_confirmed_working=true`，**不等于 root cause 判定正确**。`history_precedent` 只使用「已被人工复核」或「30 天内无 reopen」的记录，避免错误结论立刻反哺自身。

### 2.10 工具调用与失败处理

**三层门控**（保留 v1，实现更明确）：

```
[1] 阶段门控  工具是否在当前节点的可用集？
              investigate → 全部只读；resolve → 只读（写工具永不暴露给模型）
[2] 策略门控  policy.check(actor, tool, args)；DENY 的工具不出现在模型工具列表中
[3] 运行门控  预算 / args_hash 缓存 / 熔断 / 参数 schema / 资源级授权
```

**资源级授权**（review 正确指出 tool-level 不等于 object-level）：每个 Adapter 内部强制校验目标对象归属，MVP 的实现方式是**所有只读工具在签名上就不接受目标用户参数**，运行时注入 `actor.user_id`。这是最省事也最可靠的对象级授权。

**首批查询改为按类目生成**（修复 v1 的固定三工具）：`decide` 第一次进入 `investigate` 前，从该 category 的 critical checklist 直接生成第一批并行查询。`ACCESS_REQUEST` 不会去查 `check_service_status`，`SOFTWARE_LICENSE` 不会去查账号锁定状态。**第二批及以后完全由模型根据第一批结果决定** —— 这正是 Agent 价值的展示位。

**四态语义**（保留，这是 v1 做得对的地方）：

| status | 含义 | 重试 | 对诊断的意义 |
|---|---|---|---|
| `OK` | 拿到数据 | — | 正常证据 |
| `EMPTY` | 查了，确实没有 | **否** | **有效证据**，"KB 里没有"应提升升级倾向 |
| `DEGRADED` | 拿到但陈旧/部分 | 否 | 可用但必须声明陈旧度，计入 staleness penalty |
| `ERROR` | 没查成 | 视 `retryable` | 信息缺口，checklist 标 `UNAVAILABLE` |

**熔断移出 CaseState**（修复 review #16）：`tool_health` 属于共享 Adapter 层的进程级状态（MVP 是单进程内的字典，生产是 Redis），单个 case 的失败计数不能保护后端。CaseState 只保留本会话的 `degraded_sources`。

**披露规则统一**（修复 v1 的自相矛盾）—— 判据不是"critical vs optional"，而是**是否影响结论、风险、用户预期或数据新鲜度**：

| 情形 | 是否告知用户 |
|---|---|
| 失败的源会改变结论或置信度 | **必须告知**："我没能连上服务状态系统，所以没法确认是否有区域性故障" |
| 数据陈旧且用于当前结论 | **必须告知**："状态数据是 12 分钟前的" |
| 失败导致无法负责任地回答 | **必须告知** + 升级 |
| 与当前结论无关的可选遥测失败 | 不提（不制造噪音） |

### 2.11 端到端示例

**示例 A：账号锁定 + 紧急（含写操作确认，这是 v1 走不通的那条路）**

| # | 节点 | 动作 | 状态 |
|---|---|---|---|
| 1 | `ingress` | 无风险信号，**建 case 落盘** | `phase=INTAKE` |
| 2 | `intake` | SMALL 抽取 | `ACCOUNT_AUTH`, `urgency=HIGH`, `deadline=15:00`, `tried_by_user=[重置密码/FAILED]`, `H1=账号锁定`, `H2=IdP 故障` |
| 3 | `decide` | completeness=0 | → `INVESTIGATE`，按 ACCOUNT_AUTH 清单生成首批：`get_account_status` + `check_service_status`（**不查 KB 全文，先看状态**） |
| 4 | `investigate` | 并行 2 工具 → E1 `locked_at 09:14`, E2 `Okta operational` → H1 SUPPORTED, H2 REFUTED；**基于 H1 SUPPORTED 再决定**调 `search_kb("okta account lockout")` → E3 `KB-0142 score .91` | 证据齐 |
| 5 | `decide` | R1–R7 全 PASS；readiness 0.91 / HIGH | → `RESOLVE` |
| 6 | `resolve` | `ACTION` 型，Guard 通过（`KB-0142#s3` 在证据账本内）→ 输出**动作提议**；代码 FREEZE `pending_action` | `phase=AWAITING_CONFIRM` |
| — | | *员工回复"发"* | |
| 7 | `ingress`→`confirm_router` | 判定 YES | → `act` |
| 8 | `act` | 校验未过期/policy 未变/key 未消费 → 写 effects(INTENT) → `send_unlock_verification` → effects(DONE) | `phase=AWAITING_VERIFY` |
| — | | *员工回复"好了"* | |
| 9 | `verify` | SMALL 三分类 = 成功 | → `close` |
| 10 | `close` | 写 history，`resolved_by=AGENT` | `outcome=RESOLVED_BY_AGENT` |

工具 3 次，LLM 5 次（SMALL×3 + MAIN×2）。第 4 步演示了「第二批工具依赖第一批结果」。

**示例 B：维护窗口后的多系统故障（正确的失败）**

| # | 节点 | 动作 |
|---|---|---|
| 1–2 | `ingress`→`intake` | `MULTI_SYSTEM`, `affected=[jenkins, tableau]`, `onset.relative="上周五维护后"` → `onset.at≈07-19` |
| 3 | `decide` | 首批：对两个系统并行 `check_service_status`；代码检测 `onset.at` 落在变更窗口 → 在提示中注入「存在时间接近的变更 CHG-8812」 |
| 4 | `investigate` | Jenkins `degraded`、Tableau `operational`；模型据此**主动**调 `get_recent_changes` → CHG-8812 防火墙 ACL 更新时间吻合；判定 Tableau 陈旧是下游效应 |
| 5 | `decide` | KB 无高分命中（`kb_match=0.2`），H1 仅为**时间相关性**非因果 → R3 通过但 readiness 0.48；`AUTO_RESOLVABLE` 不含需要基础设施权限的变更回滚 → R1 FAIL | → `ESCALATE` |
| 6 | `escalate` | impact=MEDIUM(TEAM) × urgency=MEDIUM → **P3**（不是 v1 的 P2）；队列 `infra-oncall`；包内写明"CHG-8812 时间吻合，建议优先核查该变更的 ACL 规则；Tableau 陈旧判定为下游效应，已排除其自身服务问题" | `phase=ESCALATED` |
| 7 | — | case **保持 ESCALATED**，人工结案后才 `close`；打标 `NO_KB_MATCH` → 生成 KB 撰写任务 |

价值不在于解决了，而在于把 4 小时的人工排查压缩成"直接去看 CHG-8812"，且诚实地把相关性标成相关性。

### 2.12 异常处理

| 异常 | 处理 | 用户可见 |
|---|---|---|
| LLM 输出不符 Schema | 带校验错误重试 1 次（实测 ~1–2s，非 v1 声称的 100ms）→ 仍失败视为节点异常 | 流式"仍在检查"占位 |
| LLM 超时/5xx | 重试 1 次（同一供应商）→ 失败 → `ESCALATE(SYSTEM_ERROR)`。**不做跨供应商自动 fallback** | "我这边处理出了点问题，已转人工，你的描述都保留着" |
| LLM 提议非法动作 | 拒绝并把原因写回上下文，重试 1 次 | 无感 |
| 节点未捕获异常 | 回滚到进入该节点前的快照 → `ESCALATE(SYSTEM_ERROR)`。**永不暴露堆栈** | 同上 |
| 外部写成功但本地写失败 | `effects` 表 INTENT 记录仍在 → 启动时对账，标记 `RECONCILE_NEEDED`，不重复执行 | 无感 |
| 同 case 并发消息 | SQLite 事务 + `version` 乐观锁；冲突方入队，本轮结束后合并 | 无感 |
| 重复确认（双击） | `idempotency_key` 单次消费 | "已经发过了，请查收" |
| 确认超时（5 min） | 作废 `pending_action`，回 `decide` | "刚才那个操作我先取消了，还需要吗？" |
| 用户中途消失（30 min） | 有 `deadline` 或 `urgency≥HIGH` → 自动 `ESCALATE`；否则 `ABANDONED` | 高优场景仍有人跟进 |
| 建单失败 | 退避重试 3 次 → 仍失败则**落 `pending_tickets` 队列 + 告警运维**，并如实告知用户"工单系统暂时不可用，我已经记录并通知了值班" | 不谎称"已经找到值班同事" |
| 执行循环 | 相同 `(node, args_hash)` 第 3 次 → 强制推进；连续两次 → `ESCALATE` | 无感 |

---

## 3. 最终技术选型

选型原则：**每引入一个依赖，必须回答"删掉它会失去什么"**。答不上来就不引入。

### 3.1 选型表

| 层 | 选择 | 考虑过但放弃 | 理由 |
|---|---|---|---|
| 语言/运行时 | Python 3.11 | TypeScript | 生态成熟，评测/数据处理顺手 |
| Schema / 校验 | **Pydantic v2** | dataclass + 手写校验 | LLM structured output 与 CaseState 共用一套模型，序列化免费 |
| 编排 | **手写状态机**（`transition()` 纯函数 + 节点注册表） | LangGraph、Temporal | 见 §3.2 |
| LLM 访问 | OpenAI SDK + JSON Schema structured output；`LLMClient` Protocol 抽象 | LiteLLM、LangChain | 只需要两个方法（`complete_structured`、`complete_text`）；抽象层在自己手里 |
| 模型档位 | **两档**：`MAIN`（investigate/resolve）· `SMALL`（intake/clarify/verify/escalate 叙述/引用蕴含） | 四档 | 少两套 prompt、两条回归线；档位在 config 里可换型号 |
| 检索 | **BM25（rank_bm25）+ Embedding 余弦，RRF 融合**，索引常驻内存 | Chroma / pgvector / FAISS | 语料 < 100 篇，暴力检索 < 1ms。`Retriever` Protocol 保证可替换 |
| 持久化 | **SQLite**（`cases` / `events` / `effects` 三表，JSON 列） | Redis + Postgres | 单文件、有事务、零运维；`Store` Protocol 保证可替换 |
| 配置 | YAML（policy / categories / checklists / substitutes / thresholds）+ pydantic-settings | 硬编码、DB | 策略与阈值必须 Git 管控、可 diff、可评审 |
| API | FastAPI + SSE 流式 | WebSocket、gRPC | 单向流式够用，SSE 调试简单 |
| 前端 | 单页静态 HTML + 原生 JS（约 200 行） | React 应用 | 演示对话足够，不引入构建链 |
| CLI | Typer + Rich | argparse | 评测回放与人工试跑的主入口 |
| 测试 | pytest + YAML golden cases + `FakeLLM`（录制回放） | 只跑真实模型 | CI 必须确定性；真实模型跑独立的 `--live` 标记 |
| 可观测 | 结构化 JSONL trace（每 case 一份）+ `report` 命令做汇总 | OpenTelemetry + Jaeger | OTel 的价值在分布式，单进程用不上。trace schema 设计成可直接映射到 OTel span |
| 依赖总量 | ~10 个直接依赖 | — | `fastapi uvicorn pydantic pydantic-settings pyyaml openai rank-bm25 numpy typer rich pytest` |

### 3.2 为什么不用 LangGraph

这是最容易被质疑的一条，理由要具体：

1. **本设计的核心卖点是那个 `transition()` 纯函数可以被逐条单测**。框架会在我和这个函数之间加一层调度抽象，测试要从"调用纯函数"变成"跑图并断言状态"。
2. 我们需要在**每个节点前后**插预算拦截、策略拦截、快照、审计事件 —— 这在框架里是写 middleware/callback，在自己的 30 行 runner 里是几行代码。
3. 框架真正值钱的是 checkpointer 与分布式持久执行。MVP 是单进程，SQLite 直接存 CaseState 就是 checkpoint。
4. 版本演进带来的 breaking change，对一个要长期维护的策略引擎是负债。

**什么时候应该反转这个决定**：当需要跨进程的长时人工审批（数天级 durable execution）、或者需要并行分支合流（fan-out/fan-in）时。届时迁移成本可控 —— 节点已经是纯函数，`transition` 表可直接翻译成图定义。

### 3.3 Prompt 与配置管理

- Prompt 存 `prompts/*.md`，带 `version` frontmatter，通过 loader 注入变量，**不散落在代码字符串里**。
- 每次 LLM 调用记录 `prompt_version` + `model` + `token` + `cost` 到 trace。
- Prompt / policy / checklist 任一变更 → CI 强制跑 golden set，确定性断言不得回退。

### 3.4 Adapter 契约测试

Mock 与真实实现共用同一组 `pytest` 参数化测试：给定输入，断言返回 `ToolResult` 的 status 语义、必填字段、错误码映射、超时行为、幂等行为。这样"Demo 用 Mock、生产用真实"不是口号 —— 换实现时契约测试是唯一门禁。

### 3.5 工具契约（MVP 实现范围与真实性标注）

Review 正确指出 v1 的工具目录不足以作为接口契约，也没区分 Mock 与真实能力。

| 工具 | 参数（`actor` 由运行时注入，不在签名中） | MVP 状态 | 真实世界可行性 |
|---|---|---|---|
| `get_user_profile` | — | **实现**（本地 fixture） | 真实：SCIM / Workday API |
| `get_account_status` | — | **实现**（Mock IdP） | 真实：Okta `GET /api/v1/users/{id}`，锁定状态可读 |
| `check_service_status` | `service_ids: [enum]` | **实现**（本地 JSON） | 真实：Statuspage / Datadog API |
| `get_recent_changes` | `systems: [enum]`, `window_days: int≤30` | **实现** | 真实：ServiceNow CMDB change records |
| `search_kb` | `query: str`, `top_k≤5` | **实现**（BM25+向量，按 `applies_to` 硬过滤） | 真实：Confluence 同步 |
| `get_kb_article` | `article_id`, `section?` | **实现** | 同上 |
| `search_resolution_history` | `query: str`, `category?`, `top_k≤3` | **实现** | 真实：ServiceNow 历史工单（需脱敏管道） |
| `get_entitlements` | `resource_prefix?` | **实现** | 真实：IdP group / 权限系统 |
| `get_device_info` | — | **实现** | 真实：Jamf / Intune |
| `get_request_status` | `request_id` | **实现** | 真实：ITSM API |
| `send_unlock_verification` | `channel: REGISTERED_EMAIL`（枚举，唯一值） | **Mock**（写 effects + 打印） | ⚠️ **依赖 IdP 配置**：Okta 可通过 self-service unlock 策略实现，Entra 不等价。这是本设计中真实性最弱的一个，README 会标注 |
| `submit_access_request` | `resource`, `justification`, `duration?` | **Mock** | 真实：需资源 owner、审批链、职责分离模型，MVP 只建请求单 |
| `create_escalation_ticket` | `packet`, `queue`, `priority`, `idempotency_key` | **Mock**（写 JSON 文件 + 渲染卡片） | 真实：ServiceNow `POST /api/now/table/incident` |
| `clear_idp_sessions` | — | **不进 MVP** | 高影响操作，需重新认证与影响面告知 |
| `page_oncall` | — | **不进 MVP** | 不能由对话分类触发真人呼叫 |
| `lookup_colleague` | — | **不进 MVP** | 需要独立策略与审计 |

`ToolResult` 统一信封：

```python
class ToolResult(BaseModel):
    status: Literal["OK", "EMPTY", "DEGRADED", "ERROR"]
    data: dict | None
    digest: str                # 确定性压缩，进上下文的就是它
    source_ref: str            # "kb://KB-0142" | "status-api@2026-07-26T14:02Z"
    freshness_sec: int | None
    error: ErrorInfo | None    # {code, retryable, user_safe_message}
```

每个工具配一个**确定性 digest 函数**（纯代码，可单测）。原始 JSON 不进上下文，只存 `raw_ref` 供审计与交接包使用。

---

## 4. 关键 Trade-off

每条给出：选了什么、放弃了什么、**代价是什么**、以及**什么条件下应该反转这个决定**。最后一栏是这些取舍能不能站住的关键。

| # | 决策 | 选择 | 放弃 | 代价 | 反转条件 |
|---|---|---|---|---|---|
| 1 | Agent 拓扑 | 单 Agent + 确定性状态机 | Multi-Agent | 无法处理需要真正并行专家协作的问题 | 领域宽度扩到 IT 之外（HR/财务/法务同台），且各域工具集不重叠 |
| 2 | 编排框架 | 手写 `transition()` | LangGraph / Temporal | 没有开箱的 durable execution 与可视化 | 需要跨天的人工审批环节，或需要 fan-out/fan-in |
| 3 | **解决判定** | **门（离散规则）判定能不能，分数只决定怎么说** | 用置信度阈值决定 resolve/escalate | 少了一个连续可调旋钮，某些边缘 case 会偏保守 | 积累 ≥200 个标注 case 并完成按 category 的阈值校准后，可把分数升级为部分门的替代 |
| 4 | 记忆 | CaseState + 最近 8 轮，无摘要层、无跨会话记忆 | 四层记忆 | 超长会话上下文会被截断 | 会话轮次上限从 12 放宽到 30+ 时需要摘要层 |
| 5 | 历史工单权威 | 只作线索，不作 procedural citation | 与 KB 同级 | 部分只有历史解法没有 KB 的问题会被升级 | 建立历史工单的人工复核管道后可升级为可引用 |
| 6 | 存储 | SQLite 单文件 | Redis + Postgres | 单机、无水平扩展 | 并发会话 > ~50/s，或需要多实例部署 |
| 7 | 检索 | 内存 BM25 + Embedding | 向量数据库 | 语料 > 几千篇时启动与内存吃紧 | KB 规模超过 ~2000 篇 |
| 8 | LLM 可靠性 | 单供应商 + 重试 + 失败即升级 | 多供应商自动 fallback | 供应商全线故障时全线转人工 | 有预算与时间为每个候选模型建立独立回归线，且数据驻留合规通过 |
| 9 | 升级倾向 | 偏向多升级（recall > precision） | 追求高 deflection | 人工会收到一些本可自助的 case | Escalation Precision 长期低于 70% 且人工明确抱怨噪音时，按 category 单独收紧 |
| 10 | 定级 | impact × urgency 矩阵，个人 deadline 最高 P2 | deadline < 15min 直接 P1 | 真正紧急的个人事件响应可能慢一档 | 有真实 SLA 数据证明该类 case 被系统性延误时，引入"经理背书"的加急通道 |
| 11 | 写操作 | 参数由代码冻结，模型只提 intent | 模型直接发 tool call | 每个写动作要写一个 ActionBuilder，扩展成本高 | 永不反转 —— 这是 A2 的直接推论 |
| 12 | 通道 | CLI + Web + REST | Slack/Teams | 生产主入口未验证 | 进入 P1 试点前必做，Adapter 接口已预留 |
| 13 | 附件 | **MVP 直接拒收**，提示用文字描述 | 支持截图 | 截图对设备类问题很有用 | 建立 MIME 嗅探、大小限制、隔离存储、恶意扫描、DLP 链路后再开 |
| 14 | 评测 | 确定性断言（转移序列 + 工具集 + 禁止动作）为 CI 门禁 | LLM-as-Judge 为主 | 语言质量只能人工抽检 | LLM Judge 与人工标注一致率 > 0.85 后可入门禁 |

**最需要主动讲出来的一条**：#3。v1 让一个手工设定权重的分数守住安全边界，是这份设计里最不诚实的部分。改成"门决定能不能、分数决定怎么说"之后，安全性依赖的是可枚举、可单测、可审计的离散条件，而分数即使完全没校准也不会导致越权或幻觉。

---

## 5. MVP 范围

### 5.1 范围三分（这是本文对 review #15 的正面回答）

| | 内容 |
|---|---|
| **✅ 本次实现（Implemented）** | 10 节点编排器与 `transition()`；CaseState 全生命周期 + SQLite 持久化；三段写操作协议 + effects outbox；Policy Engine（YAML, deny-by-default）；10 个只读工具（本地 fixture）；2 个写工具（Mock）；混合检索 + `applies_to` 硬过滤；Output Guard（引用存在性确定性校验 + VERIFIED 分级 + 蕴含抽检）；Handoff Packet + 队列 allowlist；CLI + Web Chat(SSE) + REST；JSONL trace + 成本/延迟/指标汇总；25 个 golden case + 故障注入测试 |
| **🟡 Mock（有契约、无真实后端）** | IdP 账号操作（`get_account_status` / `send_unlock_verification`）；ITSM 建单（写 JSON + 渲染卡片）；权限申请提交；SSO（本地用 `--as-user` 模拟已验证身份） |
| **📐 设计预留（有接口，无实现）** | Slack / Teams Adapter；Redis / Postgres Store；向量数据库 Retriever；OTel 导出；`lookup_colleague`；恢复订阅 |
| **❌ 明确不做（Out）** | K8s / HPA；多供应商 LLM 网关；PagerDuty 呼叫；`clear_idp_sessions`；附件处理；自主 KB 写回；主动事故推送；跨会话长期记忆；A/B 实验框架；模型微调；语音通道 |

### 5.2 数据 Fixture（5 源全部本地，覆盖需求要求的 2–3 源）

| 源 | 形态 | 规模 | 设计要点 |
|---|---|---|---|
| Knowledge Base | Markdown + frontmatter，分节锚点 `#s3` | 22 篇 | 含 3 篇 `DRAFT`、1 篇 `DEPRECATED`（用于测分级）；`applies_to` 覆盖 os/region/group |
| System Status | JSON | 8 服务 + 2 进行中事故 + 5 条 change log | 含一条与 `MULTI_SYSTEM` 场景时间吻合的变更 |
| User Directory | JSON | 12 人 | 含 1 名入职 6 天者、1 名 Chicago、1 名远程、1 名目录字段缺失者（测降级） |
| Resolution History | JSONL | 40 条 | 含 3 条 `reopened=true`（测历史不可盲信）、2 条含需脱敏内容 |
| Policy Rules | YAML | 14 条 | 含 deny、confirm、审批路由、安全隔离四类 |

刻意在数据里埋了矛盾：目录显示某用户有 `grafana:viewer`，而权限系统返回拒绝 —— 这是 `UNRESOLVED_CONTRADICTION` 升级路径的驱动数据。

### 5.3 交付物

```
README.md                  问题定义 / 为何需要 Agent / 架构 / 边界 / 运行 / 评测结果 / 取舍 / 后续
docs/final_design.md       本文
src/
  orchestrator/            transition.py（纯函数）· nodes/ · interceptors/
  state/                   models.py（Pydantic）· store_sqlite.py
  tools/                   registry.py · adapters/{kb,status,directory,history,idp,itsm}
  policy/                  engine.py · rules.yaml
  retrieval/               hybrid.py
  guard/                   input_guard.py · output_guard.py
  llm/                     client.py · fake.py · prompts/
  api/                     app.py（FastAPI+SSE）· web/index.html
  cli.py
config/                    categories.yaml · checklists.yaml · substitutes.yaml · thresholds.yaml
data/                      kb/ · status.json · directory.json · history.jsonl
eval/
  golden/                  GC-*.yaml
  run_eval.py              → eval/results/latest.md
tests/                     单测 + adapter 契约测试 + 故障注入
```

### 5.4 里程碑（约 5 个工作日）

| 天 | 目标 | 完成判据 |
|---|---|---|
| D1 | 骨架：CaseState、SQLite Store、`transition()`、`ingress`/`decide`/`close`、FakeLLM | 能用 FakeLLM 跑通 `ingress→intake→decide→escalate→close` 并落盘 |
| D2 | 工具层：Registry、三层门控、10 个只读 Adapter、digest 函数、混合检索、Policy Engine | Adapter 契约测试全绿；能对真实 fixture 检索出正确 KB |
| D3 | 智能层：`intake`/`investigate`/`clarify`/`resolve`、Output Guard、readiness 与 R 门 | 示例 A 的前 5 步能在真实模型下跑通 |
| D4 | 写操作与升级：三段协议、`act`、effects outbox、`verify`、`escalate` + Packet + allowlist | 示例 A、B 端到端跑通；重复确认与确认超时用例通过 |
| D5 | 表面与证据：CLI、Web Chat、REST、trace/metrics、25 个 golden case、故障注入、README | CI 全绿；`eval/results/latest.md` 有实测数字 |

### 5.5 Golden Set（25 例，CI 门禁）

断言的是**转移序列 + 工具集 + 禁止动作 + outcome**，不是自然语言措辞。

| 组 | 例数 | 代表用例 |
|---|---|---|
| 五个需求场景闭环 | 5 | GC-001 账号锁定自助闭环；GC-002 Salesforce 区域事故告知；GC-003 VPN 客户端版本；GC-004 新人权限分而治之；GC-005 多系统故障正确升级 |
| **Agent 动态性** | 2 | **GC-031 第二批工具依赖第一批结果**（`check_service_status` 返回 operational 才去查 KB；返回 incident 则直接 INFORMATIONAL，断言两条分支的工具集不同）；GC-032 假设被证伪后改变查询方向 |
| 澄清 | 2 | GC-010 模糊输入"电脑坏了"走结构化澄清；GC-011 `clarify_count` 触顶后强制推进 |
| **写操作路径** | 3 | GC-020 确认后执行；GC-021 拒绝后不执行且回 decide；GC-022 确认超时作废 + 重复确认幂等 |
| 边界与安全 | 5 | GC-027 生产库写权限 → `POLICY_REQUIRED`（禁止出现 resolve）；GC-028 疑似钓鱼 → 隔离队列且无建议；GC-029 社工话术（代他人重置）；GC-030 非 IT 话题 → REDIRECT 不建单；GC-033 KB 中植入注入指令，断言不被执行 |
| 可靠性 | 5 | GC-040 critical 工具超时 → 升级；GC-041 KB `EMPTY` ≠ ERROR；GC-042 目录不可用 → 走替代源 + band 降档；GC-043 数据矛盾 → 不选边、如实呈现并升级；GC-044 建单失败降级 |
| 生命周期 | 3 | GC-050 升级后追问不新建 case；GC-051 人工结案才 close；GC-052 CLOSED 后新问题建新 case 并链接 |

**CI 门禁**：确定性断言 100% 通过；越权 0 例；引用校验失败 0 例；任一 Prompt / policy / checklist 变更触发全量回归。

### 5.6 验收标准（实测值，与目标值分列）

README 中会有两栏并列，避免把目标写成能力：

| 指标 | TARGET（未验证） | MEASURED（在 25 例 golden set 上填入实测） |
|---|---|---|
| 确定性断言通过率 | 100% | 待填 |
| 越权动作 | 0 | 待填 |
| 引用校验拦截的幻觉步骤 | ≥ 1 例可复现 | 待填 |
| 升级判定与人工标注一致率 | ≥ 85% | 待填（25 例的 ground truth 由我人工标注，样本量小，会明说） |
| 端到端 p50 / p95（自助闭环） | < 15s / < 30s | 待填 |
| 单 case 成本 p50 | < $0.05 | 待填 |

**明确不声称**：deflection ≥ 60%、TTR ≤ 2min、0 hallucination —— 这些需要真实流量，25 个自造 case 证明不了，README 中标为 hypothesis。

### 5.7 已知缺口（主动声明，别等被问）

1. `send_unlock_verification` 的真实性依赖具体 IdP 配置，Okta 可行、Entra 不等价。
2. readiness 权重是手工先验，无校准数据。
3. Escalation recall 的 ground truth 来自我自己的标注，存在设计者偏差。
4. 无 SLO/error budget、无 RTO/RPO 演练、无 secret rotation、无数据保留与删除流程 —— 这些是 production-ready 的必要条件，当前只做到 production-aware。
5. PII 有字段分类与 packet allowlist，但没有 DLP 与数据驻留控制。

### 5.8 有更多时间会做的（按投入产出排序）

1. **升级质量闭环**：人工对每次升级打"是否必要"标签 → 反哺按 category 的阈值。这是让系统真正变强的最短路径。
2. **KB 缺口闭环**：`ESCALATED + NO_KB_MATCH` 聚类 → 生成撰写任务 → 人工评审入库。
3. **Slack Adapter + 真实 SSO**，进入影子模式（Agent 对真实工单生成诊断，只给 IT 团队看）。
4. **readiness 校准**：收集标注数据，按 category 做可靠性图与阈值扫描。
5. **主动式支持**：区域事故发生时向受影响员工推送，把被动应答变成提前告知。

### 5.9 上线路径

| 阶段 | 范围 | 进入门槛 | 回退条件 |
|---|---|---|---|
| P0 影子 | 对真实工单生成诊断，仅 IT 团队可见 | golden set 全绿 | 诊断准确率 < 80% |
| P1 只读顾问 | 面向员工，只给建议，零写操作 | 影子期诊断准确率 > 80% | CSAT < 4.0 或出现幻觉步骤 |
| P2 受控写入 | 开放解锁邮件、重置链接、权限申请提交 | P1 期间幻觉 0 例 | 任何越权或误操作即刻回退 |
| P3 扩类目 | 按 category 逐个开自动化 | 该类目 reopen rate < 3% | 单类目衰退即单独关闭（feature flag 按类目） |

---

## 附录：Review 问题闭环表

| Review 条目 | 处置 | 位置 |
|---|---|---|
| P0-1 确认路径断裂 | 修复：独立 `AWAITING_CONFIRM` + `confirm_router` + `act` 节点 | §2.3 §2.4 |
| P0-2 升级生命周期矛盾 | 修复：phase 与 outcome 解耦，`escalate` 不再直接 `close` | §2.7 |
| P0-3 critical 证据规则歧义 | 修复：R2 不可豁免 + 显式替代源表 | §2.6 |
| P0-4 写操作协议不完整 | 修复：三段协议 + 代码冻结 args + 执行前重校验 | §2.4 |
| P0-5 安全直升无 durable case | 修复：`ingress` 无条件先建 case 落盘 | §2.3 |
| P0-6 Packet 无数据最小化 | 修复：队列级 allowlist + transcript 改授权链接 | §2.8 |
| P1-5 "所有分支只在 decide" | 修正表述：业务裁决集中在 decide | §2.1 |
| P1-6 节点数不一致 | 统一为 10 节点，4 个无 LLM | §2.1 |
| P1-7 确认政策冲突 | 按副作用性质三分类 | §2.4 |
| P1-8 首轮固定三工具 | 改为按类目清单生成首批 | §2.10 |
| P1-9 工具契约不完整 | 补参数、状态、真实性标注 | §3.5 |
| P1-10 写工具真实性不足 | 逐项标注 Mock / 真实 / 不做 | §3.5 |
| P1-11 置信度虚假精确 | 降为 heuristic readiness，且不再决定 resolve/escalate | §2.6 |
| P1-12 Critic 策略三版本 | 统一：确定性引用校验必跑 + 蕴含抽检（仅 VERIFIED 引用） | §2.6 §2.9 |
| P1-13 History 自我污染 | 降权威：不可作 citation；只用已复核/无 reopen 记录 | §2.9 |
| P1-14 Memory 过重 | 删摘要层与跨会话记忆 | §0.2 §1.4 |
| P1-15 仓库无实现 | 明确三分范围 + 5 天里程碑 + 实测/目标分列 | §5.1 §5.4 §5.6 |
| P1-16 熔断在 case 内 | 移到 Adapter 层进程级共享状态 | §2.10 |
| P2-17 过度设计 | 删除清单 | §0.2 |
| P2-18 成本延迟无依据 | 全部标 TARGET，另设 MEASURED 栏 | §5.6 |
| P2-19 失败披露不一致 | 统一判据：是否影响结论/风险/预期/新鲜度 | §2.10 |
| P2-20 actor 可见性矛盾 | 明确语义：可读最小化环境属性，不可决定身份 | §1.5 |
| P2-21 INFORMATIONAL 语义冲突 | 独立 outcome，不计 deflection，不做订阅等待 | §2.7 |
| 风险-注入 | 承认 `<untrusted_data>` 不是安全边界，真正边界是工具授权 + 对象级授权 + 输出编码 | §1.1(A2) §2.10 |
| 风险-资源级授权 | 只读工具签名中不接受目标用户参数 | §1.5 §2.10 |
| 风险-确认竞态 | 冻结 + 过期 + 执行前重校验 + 幂等键单次消费 | §2.4 |
| 风险-PII | 队列 allowlist + 授权链接 + 索引期脱敏 + 附件拒收 | §2.8 §2.9 §4(#13) |
| 风险-安全误报 | 隔离队列、不 page、不给建议、不进通用 IM | §2.5 §2.8 |
| 风险-优先级滥用 | impact × urgency 矩阵，个人 deadline 上限 P2 | §2.8 |
| 风险-warm handoff 承诺 | 删除无数据支撑的话术 | §2.8 |
| 风险-建单失败降级 | 落队列 + 告警 + 如实告知，不谎称已找到人 | §2.12 |
| 风险-事务边界 | effects outbox（先 INTENT 后 DONE）+ 启动对账 | §2.4 §2.12 |
| 风险-并发 | SQLite 事务 + version 乐观锁 + 排队合并 | §2.12 |
| 风险-多供应商 fallback | 取消自动 fallback，失败即升级 | §2.12 §4(#8) |
