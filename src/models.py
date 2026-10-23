from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RawItem(BaseModel):
    """Сырое сообщение из Telegram-канала или письмо из Gmail за последние 24 часа."""

    item_id: str
    source_type: Literal["telegram", "email"]
    source_name: str
    source_title: str
    source_url: str
    published_at: datetime
    text: str
    external_links: list[str] = Field(default_factory=list)


# ==============================================================================
# Шаг 1: Легковесная маршрутизация (экономит ~95% выходных токенов на этапе 1!)
# Полный исходный текст RawItem передаётся напрямую профильным агентам на Шаге 2.
# ==============================================================================


class RouterItemClassification(BaseModel):
    """Компактный результат классификации одного сообщения/письма без лишнего переписывания текста."""

    raw_item_id: str = Field(description="ID исходного сообщения из входного списка")
    is_relevant_news: bool = Field(
        description="True, если это содержательная новость/аналитика; False, если это реклама, спам, промо или бытовой мусор"
    )
    categories: list[str] = Field(
        default_factory=list,
        description=(
            "1 или 2 подходящие категории (slug): "
            "energetika, voda, it, avtomatizatsiya, finansy, nalogi, yuridicheskie, svyaz_transport"
        ),
    )


class RouterBatchResponse(BaseModel):
    """Ответ модели-маршрутизатора на батч сообщений."""

    results: list[RouterItemClassification] = Field(default_factory=list)


# ==============================================================================
# Шаг 2: Схемы для Независимых Агентов Категорий (Category Agents)
# ==============================================================================


class SourceRef(BaseModel):
    """Ссылка на первоисточник для конкретного новостного сюжета."""

    label: str = Field(description="Короткое понятное название источника, например: '@kursivm' или 'Email: Forbes KZ — «Тема»'")
    url: str = Field(description="Прямой URL на пост в Telegram, письмо или статью")


class SynthesizedTopic(BaseModel):
    """Глубоко проработанный сюжет внутри одной категории."""

    headline: str = Field(description="Заголовок сюжета")
    summary_paragraphs: list[str] = Field(
        description="Подробное изложение сути, контекста и всех деталей (1–3 абзаца без потери фактов)"
    )
    key_figures_and_facts: list[str] = Field(
        default_factory=list,
        description="Ключевые цифры, показатели, даты, номера НПА/статей и конкретные условия",
    )
    implications_or_context: str = Field(
        default="",
        description="Практическое значение: что это значит для отрасли, бизнеса или инвесторов",
    )
    covered_story_ids: list[str] = Field(
        default_factory=list,
        description="Список item_id из входных данных, которые вошли в этот сюжет",
    )
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="Все первоисточники (посты Telegram / письма Email) по этому сюжету",
    )


class AutomationIdea(BaseModel):
    """Прикладная идея для автоматизации (специально для категории «Автоматизация»)."""

    title: str = Field(description="Название идеи или решения")
    target_sector_or_process: str = Field(description="Где применить (завод, ЖКХ, энергетика, склад, документооборот и т.д.)")
    proposed_solution: str = Field(description="Что именно внедрить (SCADA/IIoT, комп. зрение, ERP-модуль, AI-агент и т.д.)")
    expected_impact: str = Field(description="Какой бизнес-эффект или экономию это даст")
    inspired_by_source: str = Field(description="На основе какого кейса/новости предложена идея")


class CategoryAgentResponse(BaseModel):
    """Ответ независимого профильного агента по одной категории."""

    category_overview: str = Field(
        description="Краткое резюме главных тенденций в этой категории за последние 24 часа (2–3 предложения)"
    )
    topics: list[SynthesizedTopic] = Field(
        default_factory=list,
        description="Полный перечень детально разобранных сюжетов категории (дубликаты одной новости из разных каналов объединяются в один сюжет со всеми ссылками)",
    )
    automation_ideas: list[AutomationIdea] = Field(
        default_factory=list,
        description="Заполняется для категории «Автоматизация»: конкретные идеи внедрения на основе новостей",
    )


# ==============================================================================
# Шаг 3: Схема для Финального Агента Межкатегорийных Связей (Cross-Category Linker)
# ==============================================================================


class CrossCategoryConnection(BaseModel):
    """Связь или инсайт на стыке двух и более категорий."""

    title: str = Field(description="Заголовок инсайта или связки")
    involved_categories: list[str] = Field(
        description="Названия категорий, которые пересекаются в этом выводе (минимум 2)"
    )
    analysis: str = Field(
        description="Подробное объяснение взаимосвязи, цепной реакции или системного тренда с опорой на факты дня"
    )
    actionable_takeaway: str = Field(
        description="Практический вывод, идея для бизнеса/инвестиций/автоматизации или предупреждение о риске"
    )
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="Ссылки на ключевые источники, подтверждающие эту связку",
    )


class CrossLinksAgentResponse(BaseModel):
    """Ответ финального мета-аналитика по связям между категориями."""

    executive_summary: str = Field(
        description="Общая картина дня в 3–4 предложениях: главный вектор событий за сутки"
    )
    connections: list[CrossCategoryConnection] = Field(
        default_factory=list,
        description="Межкатегорийные связи, пересечения и стратегические выводы",
    )
    key_risks_and_signals: list[str] = Field(
        default_factory=list,
        description="2–4 главных риска или сигнала дня, за которыми стоит следить",
    )


# ==============================================================================
# Итоговый объект ежедневной сводки
# ==============================================================================


class CompletedCategorySection(BaseModel):
    slug: str
    title: str
    stories_count: int
    report: CategoryAgentResponse


class DailyBrief(BaseModel):
    generated_at: datetime
    window_hours: int
    total_raw_telegram_posts: int
    total_raw_emails: int
    total_extracted_stories: int
    sections: list[CompletedCategorySection]
    cross_links: CrossLinksAgentResponse | None = None
    telegraph_url: str | None = None


# ==============================================================================
# Схемы для Ежемесячного Итогового Агента (Monthly Digest — 1-го числа в 09:00)
# ==============================================================================


class MonthlyCategoryTrend(BaseModel):
    category_slug: str = Field(description="Slug категории")
    category_title: str = Field(description="Название категории")
    main_trend_summary: str = Field(
        description="Глубокое резюме главного вектора и динамики в категории за прошедший месяц (2–3 абзаца)"
    )
    key_milestones_and_events: list[str] = Field(
        default_factory=list,
        description="Ключевые события, принятые законы/решения, крупные проекты и сделки месяца с точными цифрами и датами",
    )
    notable_numbers: list[str] = Field(
        default_factory=list,
        description="Главные цифры, суммы, тарифы, ставки и показатели месяца в этой категории",
    )
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="Ссылки на ключевые первоисточники месяца",
    )


class MonthlyAgentResponse(BaseModel):
    month_label: str = Field(description="Название периода, например: 'Итоги за последние 30 дней'")
    executive_macro_overview: str = Field(
        description="Масштабная картина месяца (3–4 абзаца): главные макроэкономические, регуляторные, инфраструктурные и технологические сдвиги"
    )
    category_trends: list[MonthlyCategoryTrend] = Field(
        default_factory=list,
        description="Подробный разбор трендов и главных событий месяца по каждой из 8 категорий",
    )
    cross_category_structural_shifts: list[CrossCategoryConnection] = Field(
        default_factory=list,
        description="Сквозные структурные связи месяца между секторами",
    )
    top_automation_and_business_ideas: list[AutomationIdea] = Field(
        default_factory=list,
        description="Топ прикладных идей для автоматизации и бизнеса по итогам месяца",
    )
    watch_next_month: list[str] = Field(
        default_factory=list,
        description="За чем следить в следующем месяце: вступающие в силу законы, дедлайны, риски и тренды",
    )


class MonthlyBrief(BaseModel):
    generated_at: datetime
    days_covered: int
    daily_digests_count: int
    total_stories_in_month: int
    report: MonthlyAgentResponse
    telegraph_url: str | None = None
