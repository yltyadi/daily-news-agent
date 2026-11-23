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
    AutomationIdea,
    CategoryAgentResponse,
    CompletedCategorySection,
    CrossLinksAgentResponse,
    DailyBrief,
    MonthlyAgentResponse,
    RawItem,
    RouterBatchResponse,
    SynthesizedTopic,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class NewsAgentPipeline:
    """
    Оптимизированный агентный пайплайн (Option A — баланс максимального качества и сниженной цены):
      - Шаг 1 (Router Agent на базе gemini-3.1-flash-lite):
        классифицирует дедуплицированные посты (RU + KZ) по всем 10 категориям, отсеивает рекламу/мусор
        и гарантированно пропускает все сообщения из приоритетных официальных каналов (Акорда, Правительство и др.).
      - Шаг 2 (10 независимых Category Agents на базе gemini-3.8-flash):
        каждый получает ПОЛНЫЙ текст постов своей категории, разбивает тяжёлые категории на чанки
        против «потери середины», строит подразделы (например, Казахстан / ЦА / Мир для Политики)
        и проверяет, что 100% приоритетных источников попали в сводку.
      - Шаг 3 (Cross-Links Meta-Agent на базе gemini-3.8-flash):
        находит сквозные связи между всеми 10 категориями.
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
    # ШАГ 1: Быстрый и точный Агент-Маршрутизатор (gemini-3.1-flash-lite)
    # ==========================================================================

    async def extract_and_route(self, raw_items: list[RawItem]) -> dict[str, list[RawItem]]:
        """Классифицирует сырые посты и письма по всем категориям с гарантией сохранения приоритетных источников."""
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
                seen_for_item: set[str] = set()
                for slug in slugs:
                    if slug in valid_slugs and slug not in seen_for_item:
                        routed[slug].append(item)
                        seen_for_item.add(slug)

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
            "Ты — высокоточный агент-классификатор новостей (работаешь с текстами на русском и казахском языках).\n"
            "Для каждого сообщения определи:\n"
            "1. `is_relevant_news`: true — если это содержательная новость, аналитика, официальное заявление, "
            "закон, статистика или отраслевой кейс; false — если это реклама, промо, шоу-бизнес/развлечения, поздравление или спам.\n"
            "2. `categories`: выбери ПРЕДПОЧТИТЕЛЬНО 1 главную категорию из списка ниже "
            "(2 категории указывай ТОЛЬКО если новость явно и существенно относится к двум разным сферам).\n"
            f"Список категорий:\n{categories_desc}\n\n"
            "Правила:\n"
            "- Сообщения с флагом `priority=true` не отбрасывай, если они содержат содержательную информацию.\n"
            "- Посты на казахском языке классифицируй с той же точностью, что и на русском."
        )

        items_payload = [
            {
                "id": it.item_id,
                "src": it.source_name,
                **({"priority": True} if it.is_priority else {}),
                "text": it.text[:1200],
            }
            for it in items
        ]

        user_prompt = (
            f"Батч {batch_num}/{total_batches}. Классифицируй {len(items)} сообщений:\n"
            + json.dumps(items_payload, ensure_ascii=False, separators=(",", ":"))
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

        valid_slugs = {c.slug for c in self.config.categories}
        classified_map = {res.raw_item_id: res for res in parsed.results}
        routed_pairs: list[tuple[RawItem, list[str]]] = []

        for raw_item in items:
            res = classified_map.get(raw_item.item_id)
            slugs: list[str] = []
            if res and res.is_relevant_news and res.categories:
                slugs = [s for s in res.categories if s in valid_slugs][:2]

            # Гарантия для приоритетных каналов из config.yaml (priority_channels):
            # всегда сохраняем приоритетный пост как минимум в его категорию по умолчанию
            if raw_item.is_priority and raw_item.priority_default_category in valid_slugs:
                default_cat = raw_item.priority_default_category
                if not slugs:
                    slugs = [default_cat]
                elif default_cat not in slugs:
                    slugs = [default_cat, *slugs[:1]]

            if slugs:
                routed_pairs.append((raw_item, slugs))

        return routed_pairs

    # ==========================================================================
    # ШАГ 2: Независимые Агенты Категорий (gemini-3.8-flash + skills/*.md)
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

    def _build_item_payload_for_category(self, it: RawItem) -> dict[str, Any]:
        sources = [{"label": f"{it.source_name} ({it.source_title})", "url": it.source_url}]
        for extra in it.extra_sources:
            sources.append({"label": extra.label, "url": extra.url})
        char_limit = 6000 if it.is_priority else 4200
        payload: dict[str, Any] = {
            "id": it.item_id,
            "sources": sources,
            "text": it.text[:char_limit],
        }
        if it.is_priority:
            payload["priority"] = "ПРИОРИТЕТНЫЙ ИСТОЧНИК — НЕ ПРОПУСКАТЬ"
        return payload

    def _build_category_system_instruction(self, category: CategoryConfig) -> str:
        skill_instructions = category.load_skill_text()
        subsection_rule = ""
        if category.subsections:
            subs_list = ", ".join(f'"{s}"' for s in category.subsections)
            subsection_rule = (
                f"\n6. ПОДРАЗДЕЛЫ (ОБЯЗАТЕЛЬНО): для КАЖДОГО сюжета в `topics` заполни поле `subsection` "
                f"ровно одним из значений: {subs_list}. Расположи сюжеты в порядке этих подразделов."
            )

        return (
            f"{skill_instructions}\n\n"
            "КРИТИЧЕСКИЕ ПРАВИЛА ПОЛНОТЫ И ИСТОЧНИКОВ:\n"
            "1. Ты работаешь СТРОГО независимо только над своей категорией.\n"
            "2. Входные материалы содержат посты (на русском и/или казахском языке) И блоки "
            "`[Полный текст вложенной статьи (...)]`. Обязательно извлекай факты и цифры из вложенных статей "
            "и грамотно передавай казахскоязычные материалы на русском языке!\n"
            "3. Объединяй дубли об одном событии из разных каналов в один глубокий сюжет (`SynthesizedTopic`), "
            "перечислив ВСЕ их `id` в `covered_story_ids` и прикрепив ВСЕ их ссылки из `sources`.\n"
            "4. ПРИОРИТЕТНЫЕ ИСТОЧНИКИ (`priority`): ни один пост с пометкой `ПРИОРИТЕТНЫЙ ИСТОЧНИК` "
            "(в первую очередь выступления, встречи и поручения Президента РК, Правительства и профильных министерств) "
            "НЕ МОЖЕТ БЫТЬ ПРОПУЩЕН — обязательно раскрой их подробно с цитатами, цифрами и именами.\n"
            "5. Для каждого сюжета обязательно заполни `sources` точными `label` и `url` из входных данных."
            f"{subsection_rule}"
        )

    def _sort_topics_by_subsections(
        self,
        topics: list[SynthesizedTopic],
        subsections: list[str],
    ) -> list[SynthesizedTopic]:
        if not subsections:
            return topics
        order_map = {sub: i for i, sub in enumerate(subsections)}
        return sorted(topics, key=lambda t: order_map.get(t.subsection, len(subsections)))

    async def _run_category_chunk(
        self,
        category: CategoryConfig,
        system_instruction: str,
        chunk_items: list[RawItem],
        chunk_label: str = "",
    ) -> CategoryAgentResponse:
        items_payload = [self._build_item_payload_for_category(it) for it in chunk_items]
        user_prompt = (
            f"Категория: {category.title} (`{category.slug}`){chunk_label}\n"
            f"Количество входных материалов: {len(chunk_items)}\n"
            "Составь максимально детальную и структурированную сводку:\n"
            + json.dumps(items_payload, ensure_ascii=False, separators=(",", ":"))
        )
        return await self._call_structured_llm(
            stage_name=f"2_category_{category.slug}",
            system_instruction=system_instruction,
            user_prompt=user_prompt,
            schema=CategoryAgentResponse,
        )

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

        # Ставим приоритетные источники в начало списка, чтобы они всегда были в фокусе модели
        ordered_items = sorted(items, key=lambda it: (not it.is_priority, it.published_at))
        system_instruction = self._build_category_system_instruction(category)

        # Защита от «потери середины» в тяжёлых категориях:
        # если материалов > 50 или суммарный объём > 130 000 символов, делим на 2 сбалансированных чанка
        total_chars = sum(min(len(it.text), 6000 if it.is_priority else 4200) for it in ordered_items)
        if len(ordered_items) > 50 or total_chars > 130_000:
            mid = len(ordered_items) // 2
            chunk1, chunk2 = ordered_items[:mid], ordered_items[mid:]
            rep1, rep2 = await asyncio.gather(
                self._run_category_chunk(category, system_instruction, chunk1, " (Часть 1/2)"),
                self._run_category_chunk(category, system_instruction, chunk2, " (Часть 2/2)"),
            )
            merged_overview = " ".join(
                p.strip() for p in (rep1.category_overview, rep2.category_overview) if p and p.strip()
            )
            merged_topics = [*rep1.topics, *rep2.topics]
            merged_ideas: list[AutomationIdea] = [*rep1.automation_ideas, *rep2.automation_ideas]
            report = CategoryAgentResponse(
                category_overview=merged_overview,
                topics=merged_topics,
                automation_ideas=merged_ideas,
            )
        else:
            report = await self._run_category_chunk(category, system_instruction, ordered_items)

        # Zero-Loss Проверка для ПРИОРИТЕТНЫХ источников (Акорда, Правительство, Минпромстрой, Нацбанк):
        # если хоть один приоритетный item_id не попал в covered_story_ids — добираем его отдельным мини-вызовом!
        priority_items = [it for it in ordered_items if it.is_priority]
        if priority_items:
            covered_ids: set[str] = set()
            for topic in report.topics:
                covered_ids.update(topic.covered_story_ids)

            missed_priority = [it for it in priority_items if it.item_id not in covered_ids]
            if missed_priority:
                logger.info(
                    "Категория %s: добираем %d пропущенных приоритетных материалов...",
                    category.slug,
                    len(missed_priority),
                )
                recovery_report = await self._run_category_chunk(
                    category,
                    system_instruction,
                    missed_priority,
                    " (ДОБОР ПРИОРИТЕТНЫХ МАТЕРИАЛОВ — ОБЯЗАТЕЛЬНО ВКЛЮЧИТЬ ВСЕ)",
                )
                report.topics = [*recovery_report.topics, *report.topics]
                report.automation_ideas = [*report.automation_ideas, *recovery_report.automation_ideas]

        if category.subsections:
            report.topics = self._sort_topics_by_subsections(report.topics, category.subsections)

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
                            **({"subsection": t.subsection} if t.subsection else {}),
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
            f"Ниже приведены готовые независимые сводки по {len(active_sections)} категориям за последние 24 часа.\n"
            "Проанализируй их и найди сквозные межкатегорийные связи, пересечения, идеи и главные риски дня:\n"
            + json.dumps(digest_for_meta, ensure_ascii=False, separators=(",", ":"))
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
                            **({"subsection": t.subsection} if t.subsection else {}),
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
            f"Обязательно разбери тренды по всем {len(self.config.categories)} категориям:\n{categories_list_str}\n\n"
            "Составь подробный ежемесячный стратегический отчёт (Monthly Strategic Brief) с макротрендами, "
            "динамикой по каждой категории, межкатегорийными сдвигами, топ-идеями для автоматизации/локализации и прогнозом:\n"
            + json.dumps(monthly_payload, ensure_ascii=False, separators=(",", ":"))
        )

        return await self._call_structured_llm(
            stage_name="monthly_strategic_summary",
            system_instruction=skill_instructions,
            user_prompt=user_prompt,
            schema=MonthlyAgentResponse,
        )
