"""LLMClient Protocol + OpenAI 实现(MAIN/SMALL 两档 + cost 记账,guide §2)。

只需两个方法:complete_structured / complete_text。cost 记入传入的 Budget
(E5 预算门消费);真实模型端到端按计划 D3 首次成立。
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

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

    def _bill(self, budget: Budget | None, model: str, usage) -> None:
        if budget is None or usage is None:
            return
        price_in, price_out = _PRICES.get(model, _DEFAULT_PRICE)
        budget.llm_cost_usd += usage.prompt_tokens * price_in + usage.completion_tokens * price_out

    def complete_structured(
        self, node: str, prompt: str, schema: type[M], *, tier: str = "MAIN", budget: Budget | None = None
    ) -> M:
        model = self._model(tier)
        rsp = self._client.beta.chat.completions.parse(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format=schema,
        )
        self._bill(budget, model, rsp.usage)
        parsed = rsp.choices[0].message.parsed
        assert parsed is not None, f"structured parse failed for node {node}"
        return parsed

    def complete_text(
        self, node: str, prompt: str, *, tier: str = "MAIN", budget: Budget | None = None
    ) -> str:
        model = self._model(tier)
        rsp = self._client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )
        self._bill(budget, model, rsp.usage)
        return rsp.choices[0].message.content or ""
