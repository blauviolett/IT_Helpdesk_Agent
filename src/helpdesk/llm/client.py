"""LLMClient Protocol + OpenAI 实现(MAIN/SMALL 两档 + cost 记账,guide §2)。

只需两个方法:complete_structured / complete_text。cost 记入传入的 Budget
(E5 预算门消费);真实模型端到端按计划 D3 首次成立。
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from helpdesk import perf
from helpdesk.config import Settings, get_settings
from helpdesk.state.models import Budget

M = TypeVar("M", bound=BaseModel)

# 每 token 单价(input, output;USD)。粗粒度记账,服务于 E5 预算门,非精确计费。
# qwen 档为百炼列表价换算的近似值(flash 档偏保守高估,方向安全;D5 以实测校准)。
_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.5e-06, 1.0e-05),
    "gpt-4o-mini": (1.5e-07, 6.0e-07),
    "qwen3.7-max": (2.5e-06, 7.5e-06),
    "qwen3.7-flash-2026-07-15": (1.5e-07, 6.0e-07),
}
_DEFAULT_PRICE = (2.5e-06, 1.0e-05)


class LLMClient(Protocol):
    def complete_structured(
        self, node: str, prompt: str, schema: type[M], *, tier: str = "MAIN", budget: Budget | None = None
    ) -> M: ...

    def complete_text(
        self, node: str, prompt: str, *, tier: str = "MAIN", budget: Budget | None = None
    ) -> str: ...


class OpenAIClient:
    def __init__(self, settings: Settings | None = None) -> None:
        from openai import OpenAI

        self._settings = settings or get_settings()
        # api_key / base_url 经 Settings 从 .env 读入(SDK 只认进程环境变量,
        # 直接 OpenAI() 会漏掉 .env);None 时回落 SDK 默认行为。
        # 显式超时:SDK 默认 600s×3 次重试,一次网络抖动可静默挂近半小时;
        # 收紧到 120s×2 次,仍挂则异常上抛,由 runner E4 快照回滚兜底(D3 实测)。
        self._client = OpenAI(
            api_key=self._settings.openai_api_key,
            base_url=self._settings.openai_base_url,
            timeout=120.0,
            max_retries=1,
        )

    def _model(self, tier: str) -> str:
        return self._settings.model_main if tier == "MAIN" else self._settings.model_small

    def _extra_body(self) -> dict[str, Any] | None:
        """enable_thinking 实验开关(config):None 时不下发,维持服务端默认。"""
        if self._settings.enable_thinking is None:
            return None
        return {"enable_thinking": self._settings.enable_thinking}

    def _bill(self, budget: Budget | None, model: str, usage) -> None:
        if budget is None or usage is None:
            return
        price_in, price_out = _PRICES.get(model, _DEFAULT_PRICE)
        budget.llm_cost_usd += usage.prompt_tokens * price_in + usage.completion_tokens * price_out

    def complete_structured(
        self, node: str, prompt: str, schema: type[M], *, tier: str = "MAIN", budget: Budget | None = None
    ) -> M:
        model = self._model(tier)
        usage_meta: dict[str, Any] = {}  # span 结束时才序列化,块内回填 usage 可见
        with perf.span("llm_api", node=node, tier=tier, model=model, mode="structured", usage=usage_meta):
            # system 消息作用有二(DashScope 对部分档位如 flash 把结构化输出降级为
            # json_object 模式):① 必须含 "JSON" 字样,否则 400;② 降级模式下服务端
            # 不再强制 schema、模型也看不到 schema,故把 schema 本体注入,由模型自约束
            # (原生 json_schema 档位不受影响,服务端照旧强约束)。
            rsp = self._client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": _schema_system_message(schema)},
                    {"role": "user", "content": prompt},
                ],
                response_format=schema,
                extra_body=self._extra_body(),
            )
            _fill_usage(usage_meta, rsp.usage)
        self._bill(budget, model, rsp.usage)
        parsed = rsp.choices[0].message.parsed
        assert parsed is not None, f"structured parse failed for node {node}"
        return parsed

    def complete_text(
        self, node: str, prompt: str, *, tier: str = "MAIN", budget: Budget | None = None
    ) -> str:
        model = self._model(tier)
        usage_meta: dict[str, Any] = {}
        with perf.span("llm_api", node=node, tier=tier, model=model, mode="text", usage=usage_meta):
            rsp = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                extra_body=self._extra_body(),
            )
            _fill_usage(usage_meta, rsp.usage)
        self._bill(budget, model, rsp.usage)
        return rsp.choices[0].message.content or ""


def _schema_system_message(schema: type[BaseModel]) -> str:
    import json

    return (
        "只输出一个 JSON 对象,不输出任何其他内容;必须严格符合以下 JSON Schema"
        "(字段名、枚举值、类型一律精确匹配):\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )


def _fill_usage(meta: dict[str, Any], usage: Any) -> None:
    """把 usage 回填进 perf span meta(纯观测);reasoning_tokens 用于验证 thinking 假设。"""
    if usage is None:
        return
    meta["prompt_tokens"] = usage.prompt_tokens
    meta["completion_tokens"] = usage.completion_tokens
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details else None
    if reasoning is not None:
        meta["reasoning_tokens"] = reasoning
