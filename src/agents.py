from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from src.config import AppConfig, CategoryConfig, PricingConfig
from src.cost_tracker import CostTracker
from src.models import (
    CategoryAgentResponse,
    CompletedCategorySection,
    CrossLinksAgentResponse,
    DailyBrief,
    MonthlyAgentResponse,
    RawItem,
    RouterBatchResponse,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class NewsAgentPipeline:
    """
    Оптимизированный агентный пайплайн:
      - Шаг 1 (Router Agent на базе быстрой и дешёвой gemini-3.1-flash-lite):
        классифицирует сырые посты по 8 категориям и отсеивает рекламу БЕЗ лишней генерации
        сотен тысяч дублирующих токенов (снижает стоимость Шага 1 с ~$1.06 до ~$0.04!).
      - Шаг 2 (8 независимых Category Agents на базе мощной gemini-3.8-flash):
        каждый получает ПОЛНЫЙ оригинальный текст постов и вложенных статей своей категории
        и пишет глубокую аналитику по своему skill-файлу (skills/01_... — 08_...).
      - Шаг 3 (Cross-Links Meta-Agent на базе gemini-3.8-flash):
        находит сквозные связи между всеми категориями.
    """

    def __init__(self, config: AppConfig, cost_tracker: CostTracker) -> None:
        self.config = config
        self.cost_tracker = cost_tracker
        self.client = genai.Client(api_key=config.gemini_api_key)
        self.sem = asyncio.Semaphore(max(1, config.llm.max_concurrent_llm_calls))

    async def _call_structured_llm(
        self,
        stage_name: str,
        system_instruction: str,
        user_prompt: str,
        schema: type[T],
        model_override: str | None = None,
        thinking_override: str | None = None,
        pricing_override: PricingConfig | None = None,
    ) -> T:
        active_model = model_override or self.config.llm.model
        level = (thinking_override or self.config.llm.thinking_level or "").strip().lower()

        thinking_cfg = None
        if level in {"minimal", "low", "medium", "high"}:
            try:
                thinking_cfg = types.ThinkingConfig(thinking_level=level)  # type: ignore[arg-type]
            except Exception:
                thinking_cfg = None

        gen_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.config.llm.temperature,
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=thinking_cfg,
        )

        async with self.sem:
            response = await self.client.aio.models.generate_content(
                model=active_model,
                contents=user_prompt,
                config=gen_config,
            )

        usage_rec = self.cost_tracker.record_response(
            stage=stage_name,
            response=response,
            model_override=active_model,
            pricing_override=pricing_override,
        )
        logger.info(
            "[LLM %s | %s] in=%d, out=%d, think=%d | cost=$%.4f",
            stage_name,
            active_model,
            usage_rec.input_tokens,
            usage_rec.output_tokens,
            usage_rec.thinking_tokens,
            usage_rec.cost_usd,
        )

        if getattr(response, "parsed", None) is not None and isinstance(response.parsed, schema):
            return response.parsed

        raw_text = (response.text or "").strip()
        return schema.model_validate_json(raw_text)

    # ==========================================================================
    # ШАГ 1: Быстрый и экономный Агент-Маршрутизатор (gemini-3.1-flash-lite)
    # ==========================================================================

    async def extract_and_route(self, raw_items: list[RawItem]) -> dict[str, list[RawItem]]:
        """Классифицирует сырые посты и письма по 8 категориям без потери исходного текста."""
        if not raw_items:
            return {cat.slug: [] for cat in self.config.categories}

        batch_size = max(1, self.config.llm.batch_size_items)
        batches = [raw_items[i : i + batch_size] for i in range(0, len(raw_items), batch_size)]

        tasks = [
            self._run_router_batch(batch_idx + 1, len(batches), batch)
            for batch_idx, batch in enumerate(batches)
        ]
        batch_outputs = await asyncio.gather(*tasks)

        valid_slugs = {cat.slug for cat in self.config.categories}
        routed: dict[str, list[RawItem]] = {cat.slug: [] for cat in self.config.categories}

        for pairs_list in batch_outputs:
            for item, slugs in pairs_list:
                for slug in slugs:
                    if slug in valid_slugs:
                        routed[slug].append(item)

        return routed

    async def _run_router_batch(
        self,
        batch_num: int,
        total_batches: int,
        items: list[RawItem],
    ) -> list[tuple[RawItem, list[str]]]:
        categories_desc = "\n".join(
            f"- `{c.slug}` ({c.title}): {c.description}" for c in self.config.categories
        )

        system_instruction = (
            "Ты — точный агент-классификатор новостей.\n"
            "Для каждого сообщения определи:\n"
            "1. `is_relevant_news`: true, если это содержательная новость, аналитика, закон, статистика или кейс; "
            "false, если это реклама, промо, анонс вебинара, поздравление, вакансия или бытовой спам.\n"
            "2. `categories`: выбери 1 или 2 подходящие категории из списка:\n"
            f"{categories_desc}\n\n"
            "Если в новости говорится об автоматизации, цифровизации производства/предприятий или есть явный кейс "
            "для промышленной автоматизации, обязательно добавляй `avtomatizatsiya`."
        )

        # Для классификации достаточно первых 1500 символов каждого поста —
        # полный 100% текст (до 15000 символов со всеми вложенными статьями) пойдёт на Шаг 2!
        items_payload = [
            {
                "raw_item_id": it.item_id,
                "source": it.source_name,
                "text_preview": it.text[:1500],
            }
            for it in items
        ]

        user_prompt = (
            f"Батч {batch_num}/{total_batches}. Классифицируй {len(items)} сообщений по категориям:\n\n"
            + json.dumps(items_payload, ensure_ascii=False)
        )

        parsed = await self._call_structured_llm(
            stage_name="1_router_classifier",
            system_instruction=system_instruction,
            user_prompt=user_prompt,
            schema=RouterBatchResponse,
            model_override=self.config.llm.router_model,
            thinking_override=self.config.llm.router_thinking_level,
            pricing_override=self.config.llm.router_pricing_per_1m_tokens,
        )

        raw_lookup = {it.item_id: it for it in items}
        routed_pairs: list[tuple[RawItem, list[str]]] = []

        for res in parsed.results:
            if not res.is_relevant_news or not res.categories:
                continue
            raw_item = raw_lookup.get(res.raw_item_id)
            if raw_item:
                routed_pairs.append((raw_item, res.categories))

        return routed_pairs

    # ==========================================================================
    # ШАГ 2: 8 Независимых Агентов Категорий (gemini-3.8-flash + skills/*.md)
    # ==========================================================================

    async def run_all_category_agents(
        self,
        routed_items: dict[str, list[RawItem]],
    ) -> list[CompletedCategorySection]:
        """Запускает независимого агента со своим skill-файлом параллельно для каждой категории."""
        tasks = [
            self._run_single_category_agent(cat, routed_items.get(cat.slug, []))
            for cat in self.config.categories
        ]
        results = await asyncio.gather(*tasks)
        return list(results)

    async def _run_single_category_agent(
        self,
        category: CategoryConfig,
        items: list[RawItem],
    ) -> CompletedCategorySection:
        if not items:
            empty_report = CategoryAgentResponse(
                category_overview="За последние 24 часа существенных новостей в данной категории не зафиксировано.",
                topics=[],
                automation_ideas=[],
            )
            return CompletedCategorySection(
                slug=category.slug,
                title=category.title,
                stories_count=0,
                report=empty_report,
            )

        skill_instructions = category.load_skill_text()
        system_instruction = (
            f"{skill_instructions}\n\n"
            "КРИТИЧЕСКИЕ ПРАВИЛА ПОЛНОТЫ И ИСТОЧНИКОВ:\n"
            "1. Ты работаешь СТРОГО независимо только над своей категорией.\n"
            "2. Входные материалы содержат полный текст постов И блоки `[Полный текст вложенной статьи (...)]`. "
            "Обязательно используй факты и цифры из вложенных статей!\n"
            "3. Если несколько постов из разных каналов пишут об одном и том же событии — "
            "объедини их в один глубокий аналитический сюжет (`SynthesizedTopic`), перечислив все их `item_id` "
            "в `covered_story_ids` и прикрепив ВСЕ их ссылки в `sources`.\n"
            "4. НЕ ТЕРЯЙ НИ ОДНОЙ СУЩЕСТВЕННОЙ НОВОСТИ и сохраняй все точные цифры, суммы, мощности, даты, "
            "названия компаний и госорганов.\n"
            "5. Для каждого сюжета обязательно заполни `sources` с точными `label` и `url` из входных данных."
        )

        items_payload = [
            {
                "item_id": it.item_id,
                "source_label": f"{it.source_name} ({it.source_title})",
                "source_url": it.source_url,
                "published_at": it.published_at.isoformat(),
                "text": it.text[:6000],
            }
            for it in items
        ]

        user_prompt = (
            f"Категория: {category.title} (`{category.slug}`)\n"
            f"Количество входных материалов: {len(items)}\n\n"
            "Составь максимально детальную и структурированную сводку по следующим материалам:\n\n"
            + json.dumps(items_payload, ensure_ascii=False)
        )

        report = await self._call_structured_llm(
            stage_name=f"2_category_{category.slug}",
            system_instruction=system_instruction,
            user_prompt=user_prompt,
            schema=CategoryAgentResponse,
        )

        return CompletedCategorySection(
            slug=category.slug,
            title=category.title,
            stories_count=len(report.topics),
            report=report,
        )

    # ==========================================================================
    # ШАГ 3: Финальный Агент Межкатегорийных Связей (Cross-Category Linker)
    # ==========================================================================

    async def run_cross_links_agent(
        self,
        sections: list[CompletedCategorySection],
    ) -> CrossLinksAgentResponse | None:
        active_sections = [s for s in sections if s.report.topics]
        if not active_sections:
            return None

        skill_instructions = self.config.load_cross_links_skill()
        digest_for_meta: list[dict[str, Any]] = []
        for sec in active_sections:
            digest_for_meta.append(
                {
                    "category": sec.title,
                    "overview": sec.report.category_overview,
                    "topics": [
                        {
                            "headline": t.headline,
                            "summary": " ".join(t.summary_paragraphs),
                            "figures": t.key_figures_and_facts,
                            "sources": [s.model_dump() for s in t.sources[:2]],
                        }
                        for t in sec.report.topics
                    ],
                }
            )

        user_prompt = (
            "Ниже приведены готовые независимые сводки по категориям за последние 24 часа.\n"
            "Проанализируй их и найди сквозные межкатегорийные связи, пересечения, идеи и главные риски дня:\n\n"
            + json.dumps(digest_for_meta, ensure_ascii=False)
        )

        return await self._call_structured_llm(
            stage_name="3_cross_links_meta",
            system_instruction=skill_instructions,
            user_prompt=user_prompt,
            schema=CrossLinksAgentResponse,
        )

    # ==========================================================================
    # ЕЖЕМЕСЯЧНЫЙ АГЕНТ: Сводка и тренды по всем дайджестам за месяц (1-го числа в 09:00)
    # ==========================================================================

    async def run_monthly_summary_agent(self, daily_briefs: list[DailyBrief]) -> MonthlyAgentResponse:
        skill_instructions = self.config.load_monthly_skill()

        monthly_payload: list[dict[str, Any]] = []
        for db in daily_briefs:
            day_str = db.generated_at.strftime("%Y-%m-%d")
            day_entry: dict[str, Any] = {
                "date": day_str,
                "telegraph_url": db.telegraph_url,
                "executive_summary": db.cross_links.executive_summary if db.cross_links else "",
                "categories": {},
            }
            for sec in db.sections:
                if not sec.report.topics:
                    continue
                day_entry["categories"][sec.title] = {
                    "overview": sec.report.category_overview,
                    "topics": [
                        {
                            "headline": t.headline,
                            "summary": " ".join(t.summary_paragraphs),
                            "figures": t.key_figures_and_facts,
                            "sources": [s.model_dump() for s in t.sources[:2]],
                        }
                        for t in sec.report.topics
                    ],
                    "automation_ideas": [idea.model_dump() for idea in sec.report.automation_ideas],
                }
            monthly_payload.append(day_entry)

        categories_list_str = "\n".join(f"- `{c.slug}`: {c.title}" for c in self.config.categories)
        user_prompt = (
            f"Перед тобой {len(daily_briefs)} ежедневных аналитических дайджестов за прошедший месяц.\n"
            f"Обязательно разбери тренды по всем 8 категориям:\n{categories_list_str}\n\n"
            "Составь подробный ежемесячный стратегический отчёт (Monthly Strategic Brief) с макротрендами, "
            "динамикой по каждой категории, межкатегорийными сдвигами, топ-идеями для автоматизации и прогнозом:\n\n"
            + json.dumps(monthly_payload, ensure_ascii=False)
        )

        return await self._call_structured_llm(
            stage_name="monthly_strategic_summary",
            system_instruction=skill_instructions,
            user_prompt=user_prompt,
            schema=MonthlyAgentResponse,
        )
