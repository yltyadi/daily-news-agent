from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.config import PricingConfig, ROOT_DIR


class LLMCallUsage(BaseModel):
    """Токены и стоимость одного вызова модели."""

    stage: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0


class RunUsageRecord(BaseModel):
    """Запись об одном полном запуске пайплайна (сохраняется в data/usage_history.json)."""

    run_id: str
    timestamp_utc: str
    model: str
    calls_count: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cached_tokens: int
    total_tokens: int
    cost_usd: float
    stage_breakdown: dict[str, dict[str, float | int]] = Field(default_factory=dict)


class UsageSummaryStats(BaseModel):
    """Агрегированная статистика расходов: текущий запуск, среднее, день, неделя, месяц и всё время."""

    current_run: RunUsageRecord
    total_runs_count: int
    avg_tokens_per_run: int
    avg_cost_per_run_usd: float
    today_tokens: int
    today_cost_usd: float
    today_runs: int
    week_tokens: int
    week_cost_usd: float
    week_runs: int
    month_tokens: int
    month_cost_usd: float
    month_runs: int
    all_time_tokens: int
    all_time_cost_usd: float


class CostTracker:
    """Отслеживает токены и стоимость каждого вызова Gemini и ведёт историю расходов."""

    def __init__(
        self,
        model: str,
        pricing: PricingConfig,
        history_file: str | Path = "data/usage_history.json",
    ) -> None:
        self.model = model
        self.pricing = pricing
        self.history_path = Path(history_file)
        if not self.history_path.is_absolute():
            self.history_path = ROOT_DIR / self.history_path
        self.calls: list[LLMCallUsage] = []

    def record_response(
        self,
        stage: str,
        response: Any,
        model_override: str | None = None,
        pricing_override: PricingConfig | None = None,
    ) -> LLMCallUsage:
        """Извлекает usage_metadata из ответа google-genai SDK и считает стоимость в USD."""
        active_model = model_override or self.model
        active_pricing = pricing_override or self.pricing

        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        candidates_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
        thoughts_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0) if usage else 0
        cached_tokens = int(getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0

        non_cached_input = max(0, prompt_tokens - cached_tokens)
        billable_output = candidates_tokens + thoughts_tokens

        cost_usd = (
            (non_cached_input / 1_000_000.0) * active_pricing.input_usd
            + (cached_tokens / 1_000_000.0) * active_pricing.cached_input_usd
            + (billable_output / 1_000_000.0) * active_pricing.output_usd
        )

        call_record = LLMCallUsage(
            stage=stage,
            model=active_model,
            input_tokens=prompt_tokens,
            output_tokens=candidates_tokens,
            thinking_tokens=thoughts_tokens,
            cached_tokens=cached_tokens,
            cost_usd=round(cost_usd, 6),
        )
        self.calls.append(call_record)
        return call_record

    def finalize_and_save(self) -> UsageSummaryStats:
        """Сохраняет итоги текущего запуска в JSON-историю и возвращает сводную статистику."""
        now_utc = datetime.now(timezone.utc)
        total_in = sum(c.input_tokens for c in self.calls)
        total_out = sum(c.output_tokens for c in self.calls)
        total_think = sum(c.thinking_tokens for c in self.calls)
        total_cached = sum(c.cached_tokens for c in self.calls)
        total_tokens = total_in + total_out + total_think
        total_cost = round(sum(c.cost_usd for c in self.calls), 6)

        used_models = sorted({c.model for c in self.calls})
        model_label = " + ".join(used_models) if used_models else self.model

        stage_breakdown: dict[str, dict[str, float | int]] = {}
        for c in self.calls:
            bucket = stage_breakdown.setdefault(
                c.stage,
                {"calls": 0, "tokens": 0, "cost_usd": 0.0},
            )
            bucket["calls"] = int(bucket["calls"]) + 1
            bucket["tokens"] = int(bucket["tokens"]) + c.input_tokens + c.output_tokens + c.thinking_tokens
            bucket["cost_usd"] = round(float(bucket["cost_usd"]) + c.cost_usd, 6)

        current_run = RunUsageRecord(
            run_id=now_utc.strftime("run_%Y%m%d_%H%M%S"),
            timestamp_utc=now_utc.isoformat(),
            model=model_label,
            calls_count=len(self.calls),
            input_tokens=total_in,
            output_tokens=total_out,
            thinking_tokens=total_think,
            cached_tokens=total_cached,
            total_tokens=total_tokens,
            cost_usd=total_cost,
            stage_breakdown=stage_breakdown,
        )

        history_runs = self._load_history()
        history_runs.append(current_run)
        self._save_history(history_runs)

        return self._compute_summary(current_run, history_runs, now_utc)

    def _load_history(self) -> list[RunUsageRecord]:
        if not self.history_path.exists():
            return []
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
            runs_raw = raw.get("runs", []) if isinstance(raw, dict) else []
            return [RunUsageRecord(**item) for item in runs_raw]
        except Exception:
            return []

    def _save_history(self, runs: list[RunUsageRecord]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"runs": [r.model_dump() for r in runs]}
        self.history_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _compute_summary(
        self,
        current_run: RunUsageRecord,
        all_runs: list[RunUsageRecord],
        now_utc: datetime,
    ) -> UsageSummaryStats:
        day_cutoff = now_utc - timedelta(days=1)
        week_cutoff = now_utc - timedelta(days=7)
        month_cutoff = now_utc - timedelta(days=30)

        today_tokens, today_cost, today_runs = 0, 0.0, 0
        week_tokens, week_cost, week_runs = 0, 0.0, 0
        month_tokens, month_cost, month_runs = 0, 0.0, 0
        all_tokens, all_cost = 0, 0.0

        for r in all_runs:
            try:
                ts = datetime.fromisoformat(r.timestamp_utc)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = now_utc

            all_tokens += r.total_tokens
            all_cost += r.cost_usd

            if ts >= day_cutoff:
                today_tokens += r.total_tokens
                today_cost += r.cost_usd
                today_runs += 1
            if ts >= week_cutoff:
                week_tokens += r.total_tokens
                week_cost += r.cost_usd
                week_runs += 1
            if ts >= month_cutoff:
                month_tokens += r.total_tokens
                month_cost += r.cost_usd
                month_runs += 1

        total_count = max(1, len(all_runs))
        return UsageSummaryStats(
            current_run=current_run,
            total_runs_count=len(all_runs),
            avg_tokens_per_run=round(all_tokens / total_count),
            avg_cost_per_run_usd=round(all_cost / total_count, 6),
            today_tokens=today_tokens,
            today_cost_usd=round(today_cost, 6),
            today_runs=today_runs,
            week_tokens=week_tokens,
            week_cost_usd=round(week_cost, 6),
            week_runs=week_runs,
            month_tokens=month_tokens,
            month_cost_usd=round(month_cost, 6),
            month_runs=month_runs,
            all_time_tokens=all_tokens,
            all_time_cost_usd=round(all_cost, 6),
        )


def format_usage_text_ru(stats: UsageSummaryStats) -> str:
    """Форматирует блок статистики токенов и стоимости для Telegram / Markdown."""
    cur = stats.current_run
    lines = [
        "💰 Статистика токенов и стоимости (LLM Usage):",
        f"• Модели: {cur.model} (вызовов: {cur.calls_count})",
        (
            f"• Текущий запуск: ${cur.cost_usd:.4f} | "
            f"{cur.total_tokens:,} токенов "
            f"(in: {cur.input_tokens:,}, out: {cur.output_tokens:,}, think: {cur.thinking_tokens:,})"
        ),
        f"• Среднее за запуск: ${stats.avg_cost_per_run_usd:.4f} | {stats.avg_tokens_per_run:,} токенов",
        f"• За сегодня (24ч): ${stats.today_cost_usd:.4f} ({stats.today_tokens:,} токенов, запусков: {stats.today_runs})",
        f"• За 7 дней (неделя): ${stats.week_cost_usd:.4f} ({stats.week_tokens:,} токенов, запусков: {stats.week_runs})",
        f"• За 30 дней (месяц): ${stats.month_cost_usd:.4f} ({stats.month_tokens:,} токенов, запусков: {stats.month_runs})",
        f"• За всё время (Total): ${stats.all_time_cost_usd:.4f} ({stats.all_time_tokens:,} токенов, всего запусков: {stats.total_runs_count})",
    ]
    return "\n".join(lines)
