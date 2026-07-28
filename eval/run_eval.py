"""L2 golden 评测 runner(guide §2 / §7.1 / §7.3)。

- 只支持 4 个断言 key:expect_outcome / expect_escalated / must_call_tools /
  must_not_call_tools;出现其他 key 直接报错。零特例分支:所有 case 走同一条路径,
  禁止为任何 golden case 加 runner 特例(冻结契约)。
- 真实模型驱动:逐 case 逐 turn 调 runner.handle_message,断言最终 CaseState;
  "调用过的工具"口径 = evidence 账本(执行过且落账的工具,含写工具)。
- 每个 case 用独立临时 SQLite,不污染 helpdesk.db;trace 照常写 traces/ 供人工复盘。
- 输出 eval/results/latest.md:逐例结果 + 失败详情 + p50/p95 / 成本 / 工具调用实测。
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from helpdesk.config import get_settings
from helpdesk.llm.client import OpenAIClient
from helpdesk.orchestrator.runner import Ctx, handle_message
from helpdesk.state.store import SQLiteStore
from helpdesk.tools.base import ToolRuntime
from helpdesk.trace import Tracer

ASSERT_KEYS = frozenset(
    {"expect_outcome", "expect_escalated", "must_call_tools", "must_not_call_tools"}
)
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
RESULTS_PATH = Path(__file__).resolve().parent / "results" / "latest.md"


def check(spec: dict, state) -> list[str]:
    """4 个断言 key 的唯一实现;返回失败描述列表(空 = PASS)。"""
    called = {e.tool for e in state.evidence}
    outcome = state.outcome.value if state.outcome else None
    checks = {
        "expect_outcome": lambda v: [] if outcome == v else [f"outcome={outcome},期望 {v}"],
        "expect_escalated": lambda v: []
        if state.escalation.required == v
        else [f"escalated={state.escalation.required},期望 {v}"],
        "must_call_tools": lambda v: [f"未调用 {t}" for t in v if t not in called],
        "must_not_call_tools": lambda v: [f"调用了禁止工具 {t}" for t in v if t in called],
    }
    return [msg for key, expected in spec.items() for msg in checks[key](expected)]


def run_case(path: Path) -> dict:
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    unknown = set(case["assert"]) - ASSERT_KEYS
    if unknown:
        raise SystemExit(f"{case['id']}: 不支持的断言 key {sorted(unknown)}(冻结:仅 4 个)")
    settings = get_settings()
    ctx = Ctx(
        llm=OpenAIClient(settings),
        tracer=Tracer(settings.trace_dir),
        store=SQLiteStore(Path(tempfile.mkdtemp(prefix="eval-")) / "eval.db"),
        runtime=ToolRuntime(
            fixture=case.get("fixture", "status_a"),
            fail_tools=set(case.get("fail_tools", [])),
        ),
    )
    case_id, state, latencies = None, None, []
    for text in case["turns"]:
        started = time.perf_counter()
        state = handle_message(text, ctx=ctx, case_id=case_id, as_user=case.get("as_user"))
        latencies.append(time.perf_counter() - started)
        case_id = state.case_id
    failures = check(case["assert"], state)
    reason = state.escalation.reason_code.value if state.escalation.reason_code else ""
    print(f"[{'PASS' if not failures else 'FAIL'}] {case['id']} {case['title']}"
          + (f" — {'; '.join(failures)}" if failures else ""))
    return {
        "id": case["id"], "title": case["title"], "case_id": state.case_id,
        "failures": failures, "latencies": latencies,
        "outcome": state.outcome.value if state.outcome else "-",
        "escalated": f"{state.escalation.required}" + (f"({reason})" if reason else ""),
        "tools": state.budget.tool_calls, "cost": state.budget.llm_cost_usd,
    }


def render(results: list[dict]) -> str:
    turns = [x for r in results for x in r["latencies"]]
    passed = sum(1 for r in results if not r["failures"])
    settings = get_settings()
    lines = [
        "# L2 Golden Eval — 实测结果", "",
        f"- 运行时间:{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 模型:MAIN={settings.model_main} / SMALL={settings.model_small}",
        f"- 结果:**{passed}/{len(results)} PASS**", "",
        "| ID | 场景 | 结果 | outcome | escalated(reason) | 工具调用 | 成本 USD | 各轮耗时 s |",
        "|---|---|---|---|---|---:|---:|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['title']} | {'PASS' if not r['failures'] else 'FAIL'} "
            f"| {r['outcome']} | {r['escalated']} | {r['tools']} | {r['cost']:.4f} "
            f"| {' / '.join(f'{x:.1f}' for x in r['latencies'])} |"
        )
    failed = [r for r in results if r["failures"]]
    if failed:
        lines += ["", "## 失败详情", ""]
        lines += [f"- **{r['id']}**(trace: `traces/{r['case_id']}.jsonl`):"
                  f"{';'.join(r['failures'])}" for r in failed]
    lines += [
        "", "## 汇总(本次运行实测)", "",
        f"- 每轮延迟 p50 / p95:{statistics.median(turns):.1f}s / "
        f"{statistics.quantiles(turns, n=20)[18]:.1f}s(共 {len(turns)} 轮)",
        f"- 总成本:${sum(r['cost'] for r in results):.4f};"
        f"工具调用总数:{sum(r['tools'] for r in results)}", "",
    ]
    return "\n".join(lines)


def main() -> None:
    results = [run_case(p) for p in sorted(GOLDEN_DIR.glob("*.yaml"))]
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(render(results), encoding="utf-8")
    print(f"\n结果已写入 {RESULTS_PATH}")
    sys.exit(0 if all(not r["failures"] for r in results) else 1)


if __name__ == "__main__":
    main()
