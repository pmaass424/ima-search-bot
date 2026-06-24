from dataclasses import dataclass
from datetime import datetime, timezone

from .state import StateStore


class DailyBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAIPrices:
    text_input_per_1m: float
    text_output_per_1m: float
    tts_per_1k_chars: float


class OpenAIBudget:
    def __init__(self, state: StateStore, daily_budget_usd: float, prices: OpenAIPrices) -> None:
        self.state = state
        self.daily_budget_usd = daily_budget_usd
        self.prices = prices

    @property
    def enabled(self) -> bool:
        return self.daily_budget_usd > 0

    def usage_date(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def spent_today(self) -> float:
        if not self.enabled:
            return 0.0
        return self.state.openai_spend_for_date(self.usage_date())

    def remaining_today(self) -> float:
        if not self.enabled:
            return float("inf")
        return max(0.0, self.daily_budget_usd - self.spent_today())

    def ensure_available(self, estimated_cost_usd: float, label: str) -> None:
        if not self.enabled:
            return
        spent = self.spent_today()
        if spent + estimated_cost_usd > self.daily_budget_usd:
            raise DailyBudgetExceeded(
                f"OpenAI daily budget reached before {label}: "
                f"spent=${spent:.4f}, estimated_next=${estimated_cost_usd:.4f}, "
                f"limit=${self.daily_budget_usd:.2f}"
            )

    def estimate_text(self, input_chars: int, output_tokens: int = 1500) -> float:
        input_tokens = max(1, input_chars // 4)
        return (
            input_tokens * self.prices.text_input_per_1m
            + output_tokens * self.prices.text_output_per_1m
        ) / 1_000_000

    def estimate_tts(self, text_chars: int) -> float:
        return max(1, text_chars) / 1000 * self.prices.tts_per_1k_chars

    def record_text(self, model: str, category: str, input_tokens: int, output_tokens: int) -> None:
        if not self.enabled:
            return
        cost = (
            input_tokens * self.prices.text_input_per_1m
            + output_tokens * self.prices.text_output_per_1m
        ) / 1_000_000
        self.state.record_openai_usage(
            self.usage_date(), category, model, input_tokens, output_tokens, cost
        )

    def record_tts_estimate(self, model: str, text_chars: int) -> None:
        if not self.enabled:
            return
        self.state.record_openai_usage(
            self.usage_date(),
            "tts",
            model,
            max(1, text_chars // 4),
            0,
            self.estimate_tts(text_chars),
        )
