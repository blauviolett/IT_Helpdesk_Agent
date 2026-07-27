"""CLI —— 唯一交付通道(guide §2):chat / resume(eval 于 D5 加入)。

开关:--as-user(运行时注入身份,模拟已认证 SSO)/ --fixture(status_a|status_b,
Demo Case B 分支)/ --fail <tool>(故障注入,可重复)。API key 走 .env,不进代码。
"""

from __future__ import annotations

import typer
from rich.console import Console

from helpdesk.config import get_settings
from helpdesk.orchestrator.runner import Ctx, handle_message
from helpdesk.state.models import CaseState
from helpdesk.state.store import SQLiteStore
from helpdesk.tools.base import ToolRuntime
from helpdesk.trace import Tracer

app = typer.Typer(add_completion=False, help="IT Helpdesk Agent")
console = Console()

_AsUser = typer.Option(None, "--as-user", help="以该用户身份对话(运行时注入,模拟 SSO)")
_Fixture = typer.Option("status_a", "--fixture", help="status fixture:status_a | status_b")
_Fail = typer.Option([], "--fail", help="注入故障的工具名(可重复)")


@app.command()
def chat(as_user: str = _AsUser, fixture: str = _Fixture, fail: list[str] = _Fail) -> None:
    """新对话(新 case)。"""
    _loop(case_id=None, as_user=as_user, fixture=fixture, fail=fail)


@app.command()
def resume(case_id: str, as_user: str = _AsUser, fixture: str = _Fixture, fail: list[str] = _Fail) -> None:
    """跨进程续接已有 case。"""
    if SQLiteStore(get_settings().db_path).get(case_id) is None:
        console.print(f"[red]case 不存在:{case_id}[/red](新对话请用 chat 命令)")
        raise typer.Exit(code=1)
    _loop(case_id=case_id, as_user=as_user, fixture=fixture, fail=fail)


def _loop(case_id: str | None, as_user: str | None, fixture: str, fail: list[str]) -> None:
    from helpdesk.llm.client import OpenAIClient  # 延迟导入:无 key 时 chat 之外的命令不受影响

    settings = get_settings()
    ctx = Ctx(
        llm=OpenAIClient(settings),
        tracer=Tracer(settings.trace_dir),
        store=SQLiteStore(settings.db_path),
        runtime=ToolRuntime(fixture=fixture, fail_tools=set(fail)),
    )
    console.print(f"[dim]fixture={fixture} fail={sorted(ctx.runtime.fail_tools) or '-'} "
                  f"as_user={as_user or '-'};输入 exit 退出[/dim]")
    while True:
        try:
            text = console.input("[bold cyan]you[/] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text or text.lower() in {"exit", "quit"}:
            break
        seen = _message_count(ctx, case_id)
        state = handle_message(text, ctx=ctx, case_id=case_id, as_user=as_user)
        case_id = state.case_id
        _print_replies(state, seen)
        _print_status(state)
    console.print("[dim]bye[/dim]")


def _message_count(ctx: Ctx, case_id: str | None) -> int:
    state = ctx.store.get(case_id) if case_id else None
    return len(state.messages) if state else 0


def _print_replies(state: CaseState, seen: int) -> None:
    replies = [m for m in state.messages[seen:] if m.role == "assistant"]
    for message in replies:
        console.print(f"[bold green]agent[/] > {message.content}")
    if not replies:
        console.print("[dim]agent >(本轮无用户可见回复;状态见下行)[/dim]")


def _print_status(state: CaseState) -> None:
    esc = state.escalation
    extra = f" reason={esc.reason_code.value}" if esc.required and esc.reason_code else ""
    console.print(
        f"[dim]case={state.case_id} phase={state.phase.value} "
        f"outcome={state.outcome.value if state.outcome else '-'}{extra} "
        f"cost=${state.budget.llm_cost_usd:.4f} tools={state.budget.tool_calls}[/dim]"
    )


if __name__ == "__main__":
    app()
