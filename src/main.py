from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.agents import NewsAgentPipeline
from src.bookmarks import listen_telegram_bookmarks_loop, sync_telegram_bookmarks_once
from src.collectors import collect_gmail_emails, collect_telegram_posts
from src.config import load_config
from src.cost_tracker import CostTracker, format_usage_text_ru
from src.models import DailyBrief, MonthlyBrief
from src.publisher import (
    load_recent_daily_digests,
    publish_monthly_to_telegraph,
    publish_to_telegraph,
    save_daily_digest_json,
    save_markdown_file,
    save_monthly_markdown_file,
    send_email_digest,
    send_monthly_email_digest,
    send_monthly_telegram_bot_digest,
    send_telegram_bot_digest,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("daily-news-agent")


async def run_daily_pipeline(hours_override: int | None = None, dry_run: bool = False) -> int:
    config = load_config()
    # Сначала синхронизируем любые нажатия «📌 Сохранить» / «💡 Сохранить идею» из очереди бота
    await sync_telegram_bookmarks_once(config)

    if not config.gemini_api_key:
        logger.error("ОШИБКА: GEMINI_API_KEY не задан в .env или переменных окружения!")
        return 1

    lookback_hours = hours_override or config.general.lookback_hours
    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(hours=lookback_hours)

    try:
        local_tz = ZoneInfo(config.schedule.timezone)
        now_local = now_utc.astimezone(local_tz)
    except Exception:
        now_local = now_utc

    num_cats = len(config.categories)
    logger.info(
        "Запуск ЕЖЕДНЕВНОГО сбора новостей за последние %d ч (с %s UTC, каналов: %d, категорий: %d)",
        lookback_hours,
        cutoff_utc.strftime("%Y-%m-%d %H:%M"),
        len(config.telegram.channels),
        num_cats,
    )

    # 1. Сбор сообщений из Telegram (с умной гидрацией и дедупликацией) и писем из Gmail за 24 часа
    tg_items = await collect_telegram_posts(config, cutoff_utc=cutoff_utc)
    email_items = collect_gmail_emails(config, cutoff_utc=cutoff_utc)
    all_raw_items = tg_items + email_items

    logger.info(
        "Итого уникальных материалов после дедупликации: %d (Telegram: %d, Gmail: %d)",
        len(all_raw_items),
        len(tg_items),
        len(email_items),
    )

    cost_tracker = CostTracker(
        model=config.llm.model,
        pricing=config.llm.pricing_per_1m_tokens,
        history_file=config.usage_history_file,
    )
    pipeline = NewsAgentPipeline(config=config, cost_tracker=cost_tracker)

    # 2. Этап 1: Быстрый агент-маршрутизатор (RU + KZ + защита приоритетных источников)
    logger.info(
        "Этап 1/3: Классификация и маршрутизация по %d категориям (%s)...",
        num_cats,
        config.llm.router_model,
    )
    routed_items = await pipeline.extract_and_route(all_raw_items)
    total_routed = sum(len(v) for v in routed_items.values())
    logger.info("Распределено назначений по категориям: %d", total_routed)

    # 3. Этап 2: Независимые отраслевые агенты (параллельно, каждый со своим skill-файлом)
    logger.info(
        "Этап 2/3: Запуск %d независимых отраслевых агентов (%s)...",
        num_cats,
        config.llm.model,
    )
    completed_sections = await pipeline.run_all_category_agents(routed_items)
    total_topics = sum(s.stories_count for s in completed_sections)

    # 4. Этап 3: Финальный мета-агент для поиска межкатегорийных связей
    logger.info("Этап 3/3: Поиск сквозных межкатегорийных связей и стратегических выводов...")
    cross_links = await pipeline.run_cross_links_agent(completed_sections)

    # 5. Подсчёт и сохранение статистики токенов и стоимости
    usage_stats = cost_tracker.finalize_and_save()
    logger.info("\n%s", format_usage_text_ru(usage_stats))

    brief = DailyBrief(
        generated_at=now_local,
        window_hours=lookback_hours,
        total_raw_telegram_posts=len(tg_items),
        total_raw_emails=len(email_items),
        total_extracted_stories=total_topics,
        sections=completed_sections,
        cross_links=cross_links,
    )

    # 6. Публикация в Telegraph + отправка в Telegram-бота и на Email
    if not dry_run:
        telegraph_url = await publish_to_telegraph(config, brief, usage_stats)
        if telegraph_url:
            brief.telegraph_url = telegraph_url
            logger.info("Опубликована статья Telegraph Instant View: %s", telegraph_url)

        await send_telegram_bot_digest(config, brief, usage_stats)
        send_email_digest(config, brief, usage_stats)

    # 7. Сохраняем структурированный JSON-дайджест дня (+ автоочистка архивов старше 45 дней)
    save_daily_digest_json(config, brief)

    if config.general.save_local_markdown or dry_run:
        save_markdown_file(brief, usage_stats, config=config)

    return 0


async def run_monthly_pipeline(days_override: int | None = None, dry_run: bool = False) -> int:
    """
    Ежемесячный запуск (1-го числа каждого месяца в 09:00 утра по Алматы):
    читает все сохранённые ежедневные дайджесты за прошедший месяц и строит итоговый отчёт
    о трендах, сквозных инсайтах и прогнозах.
    """
    config = load_config()
    if not config.gemini_api_key:
        logger.error("ОШИБКА: GEMINI_API_KEY не задан в .env или переменных окружения!")
        return 1

    lookback_days = days_override or config.general.monthly_lookback_days
    try:
        local_tz = ZoneInfo(config.schedule.timezone)
        now_local = datetime.now(timezone.utc).astimezone(local_tz)
    except Exception:
        now_local = datetime.now(timezone.utc)

    daily_briefs = load_recent_daily_digests(config, lookback_days=lookback_days)
    if not daily_briefs:
        logger.warning(
            "В папке %s пока нет сохранённых ежедневных дайджестов за последние %d дней.",
            config.general.daily_digests_dir,
            lookback_days,
        )
        return 0

    total_stories = sum(b.total_extracted_stories for b in daily_briefs)
    logger.info(
        "Запуск ЕЖЕМЕСЯЧНОГО аналитического агента: загружено %d ежедневных дайджестов (%d сюжетов)",
        len(daily_briefs),
        total_stories,
    )

    cost_tracker = CostTracker(
        model=config.llm.model,
        pricing=config.llm.pricing_per_1m_tokens,
        history_file=config.usage_history_file,
    )
    pipeline = NewsAgentPipeline(config=config, cost_tracker=cost_tracker)

    monthly_report = await pipeline.run_monthly_summary_agent(daily_briefs)
    usage_stats = cost_tracker.finalize_and_save()
    logger.info("\n%s", format_usage_text_ru(usage_stats))

    monthly_brief = MonthlyBrief(
        generated_at=now_local,
        days_covered=lookback_days,
        daily_digests_count=len(daily_briefs),
        total_stories_in_month=total_stories,
        report=monthly_report,
    )

    if not dry_run:
        telegraph_url = await publish_monthly_to_telegraph(config, monthly_brief, usage_stats)
        if telegraph_url:
            monthly_brief.telegraph_url = telegraph_url
            logger.info("Опубликован ежемесячный доклад Telegraph Instant View: %s", telegraph_url)

        await send_monthly_telegram_bot_digest(config, monthly_brief, usage_stats)
        send_monthly_email_digest(config, monthly_brief, usage_stats)

    if config.general.save_local_markdown or dry_run:
        save_monthly_markdown_file(monthly_brief, usage_stats)

    return 0


async def republish_latest_digest() -> int:
    """Публикует последний сохранённый дайджест из data/daily_digests/ в Telegraph/Telegram/Email без затрат на LLM."""
    config = load_config()
    await sync_telegram_bookmarks_once(config)
    digests = load_recent_daily_digests(config, lookback_days=30)
    if not digests:
        logger.error("Нет сохранённых дайджестов в %s", config.general.daily_digests_dir)
        return 1

    latest_brief = digests[-1]
    tracker = CostTracker(
        model=config.llm.model,
        pricing=config.llm.pricing_per_1m_tokens,
        history_file=config.usage_history_file,
    )
    history = tracker._load_history()
    if history:
        usage_stats = tracker._compute_summary(history[-1], history, datetime.now(timezone.utc))
    else:
        usage_stats = tracker.finalize_and_save()

    telegraph_url = await publish_to_telegraph(config, latest_brief, usage_stats)
    if telegraph_url:
        latest_brief.telegraph_url = telegraph_url
        logger.info("Опубликована полная статья Telegraph Instant View: %s", telegraph_url)

    await send_telegram_bot_digest(config, latest_brief, usage_stats)
    send_email_digest(config, latest_brief, usage_stats)
    save_daily_digest_json(config, latest_brief)
    save_markdown_file(latest_brief, usage_stats, config=config)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Новостной агент: ежедневная сводка (19:00 Алматы) и ежемесячный обзор трендов (1-го числа в 09:00)"
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "monthly", "bookmarks"],
        default="daily",
        help="Режим работы: 'daily' (за 24ч), 'monthly' (итоги месяца) или 'bookmarks' (синхронизация заметок и идей)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Для режима daily: за сколько последних часов собирать новости (по умолчанию из config.yaml: 24)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Для режима monthly: за сколько последних дней брать ежедневные дайджесты (по умолчанию из config.yaml: 32)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Собрать и проанализировать, сохранить локальный Markdown, но не отправлять в Telegram/Email",
    )
    parser.add_argument(
        "--republish-latest",
        action="store_true",
        help="Повторно опубликовать последний сохранённый дайджест в Telegraph/Telegram/Email без вызова LLM",
    )
    parser.add_argument(
        "--sync-bookmarks",
        action="store_true",
        help="Обработать нажатия кнопок сохранения новостей и идей из Telegram и обновить страницы ($0.00)",
    )
    parser.add_argument(
        "--listen-bot",
        action="store_true",
        help="Запустить локальный обработчик кнопок сохранения в реальном времени ($0.00)",
    )
    parser.add_argument(
        "--max-minutes",
        type=int,
        default=0,
        help="Для --listen-bot: сколько минут работать до завершения (0 = бесконечно)",
    )
    args = parser.parse_args()

    if args.sync_bookmarks or args.mode == "bookmarks":
        config = load_config()
        asyncio.run(sync_telegram_bookmarks_once(config))
        sys.exit(0)

    if args.listen_bot:
        config = load_config()
        asyncio.run(listen_telegram_bookmarks_loop(config, max_minutes=args.max_minutes))
        sys.exit(0)

    if args.republish_latest:
        exit_code = asyncio.run(republish_latest_digest())
        sys.exit(exit_code)

    if args.mode == "monthly":
        exit_code = asyncio.run(run_monthly_pipeline(days_override=args.days, dry_run=args.dry_run))
    else:
        exit_code = asyncio.run(run_daily_pipeline(hours_override=args.hours, dry_run=args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
