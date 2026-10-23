from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import httpx

from src.bookmarks import (
    get_or_fetch_bot_username,
    load_bookmarks_store,
    publish_or_update_bookmarks_telegraph,
)
from src.config import AppConfig, ROOT_DIR
from src.cost_tracker import UsageSummaryStats, format_usage_text_ru
from src.models import CompletedCategorySection, DailyBrief, MonthlyBrief

logger = logging.getLogger(__name__)


# ==============================================================================
# 0. Сохранение и загрузка ежедневных дайджестов (для ежемесячного агента)
# ==============================================================================


def save_daily_digest_json(config: AppConfig, brief: DailyBrief) -> Path:
    """Сохраняет структурированный ежедневный дайджест в data/daily_digests/YYYY-MM-DD.json."""
    digests_dir = ROOT_DIR / config.general.daily_digests_dir
    digests_dir.mkdir(parents=True, exist_ok=True)
    filename = brief.generated_at.strftime("digest_%Y-%m-%d.json")
    path = digests_dir / filename
    path.write_text(
        json.dumps(brief.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Ежедневный дайджест сохранён в архив для месячного отчёта: %s", path)
    return path


def load_recent_daily_digests(config: AppConfig, lookback_days: int | None = None) -> list[DailyBrief]:
    """Загружает все ежедневные дайджесты за последний месяц из data/daily_digests/."""
    days = lookback_days or config.general.monthly_lookback_days
    digests_dir = ROOT_DIR / config.general.daily_digests_dir
    if not digests_dir.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    loaded: list[DailyBrief] = []

    for json_path in sorted(digests_dir.glob("digest_*.json")):
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            brief = DailyBrief.model_validate(raw)
            gen_dt = brief.generated_at
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            if gen_dt >= cutoff:
                loaded.append(brief)
        except Exception as exc:
            logger.warning("Не удалось прочитать архив дайджеста %s: %s", json_path, exc)

    loaded.sort(key=lambda b: b.generated_at)
    return loaded


# ==============================================================================
# 1. Публикация статьи в Telegraph (telegra.ph) для Instant View в Telegram
# ==============================================================================


def _build_telegraph_nodes_for_section(
    sec: CompletedCategorySection,
    date_compact: str = "",
    bot_username: str = "",
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    nodes.append({"tag": "h3", "children": [f"{sec.title} ({sec.stories_count})"]})

    if not sec.report.topics:
        nodes.append({"tag": "p", "children": [{"tag": "em", "children": [sec.report.category_overview]}]})
        nodes.append({"tag": "hr"})
        return nodes

    if sec.report.category_overview:
        nodes.append({"tag": "blockquote", "children": [sec.report.category_overview]})

    for idx, topic in enumerate(sec.report.topics, start=1):
        nodes.append({"tag": "h4", "children": [f"{idx}. {topic.headline}"]})

        for para in topic.summary_paragraphs:
            nodes.append({"tag": "p", "children": [para]})

        if topic.key_figures_and_facts:
            li_nodes = [{"tag": "li", "children": [fact]} for fact in topic.key_figures_and_facts]
            nodes.append({"tag": "p", "children": [{"tag": "strong", "children": ["Ключевые цифры и факты:"]}]})
            nodes.append({"tag": "ul", "children": li_nodes})

        if topic.implications_or_context:
            nodes.append(
                {
                    "tag": "p",
                    "children": [
                        {"tag": "strong", "children": ["Значение / Вывод: "]},
                        topic.implications_or_context,
                    ],
                }
            )

        footer_children: list[Any] = []
        if topic.sources:
            footer_children.append({"tag": "strong", "children": ["Источники: "]})
            for s_idx, src in enumerate(topic.sources):
                if s_idx > 0:
                    footer_children.append(" | ")
                if src.url and src.url.startswith("http"):
                    footer_children.append({"tag": "a", "attrs": {"href": src.url}, "children": [src.label or "Ссылка"]})
                else:
                    footer_children.append(src.label or "Источник")

        # Кнопка в 1 клик «📌 Сохранить в заметки» прямо внутри каждого сюжета Telegraph!
        if bot_username and date_compact:
            save_url = f"https://t.me/{bot_username}?start=save_{date_compact}_{sec.slug}_{idx}"
            if footer_children:
                footer_children.append("   •   ")
            footer_children.append(
                {
                    "tag": "strong",
                    "children": [
                        {
                            "tag": "a",
                            "attrs": {"href": save_url},
                            "children": [f"📌 Сохранить #{idx} в Базу Заметок"],
                        }
                    ],
                }
            )

        if footer_children:
            nodes.append({"tag": "p", "children": footer_children})

    if sec.report.automation_ideas:
        nodes.append({"tag": "h4", "children": ["💡 Прикладные идеи для автоматизации"]})
        for idea in sec.report.automation_ideas:
            nodes.append(
                {
                    "tag": "p",
                    "children": [
                        {"tag": "strong", "children": [f"• {idea.title} ({idea.target_sector_or_process}): "]},
                        f"{idea.proposed_solution} → Эффект: {idea.expected_impact} ",
                        {"tag": "em", "children": [f"[Основано на: {idea.inspired_by_source}]"]},
                    ],
                }
            )

    nodes.append({"tag": "hr"})
    return nodes


def _build_telegraph_nodes(
    brief: DailyBrief,
    stats: UsageSummaryStats,
    bot_username: str = "",
    bookmarks_url: str = "",
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    date_str = brief.generated_at.strftime("%d.%m.%Y %H:%M")
    date_compact = brief.generated_at.strftime("%Y%m%d")

    header_children: list[Any] = [
        {"tag": "strong", "children": ["Период: "]},
        f"последние {brief.window_hours}ч (сформировано {date_str}) | ",
        {"tag": "strong", "children": ["Постов TG: "]},
        f"{brief.total_raw_telegram_posts} | ",
        {"tag": "strong", "children": ["Писем Gmail: "]},
        f"{brief.total_raw_emails} | ",
        {"tag": "strong", "children": ["Новостных сюжетов: "]},
        f"{brief.total_extracted_stories}",
    ]
    if bookmarks_url:
        header_children.extend(
            [
                " | ",
                {
                    "tag": "strong",
                    "children": [
                        {
                            "tag": "a",
                            "attrs": {"href": bookmarks_url},
                            "children": ["📌 Открыть Базу Заметок"],
                        }
                    ],
                },
            ]
        )

    nodes.append({"tag": "p", "children": header_children})

    if brief.cross_links and brief.cross_links.executive_summary:
        nodes.append({"tag": "h3", "children": ["🧭 Главная картина дня"]})
        nodes.append({"tag": "blockquote", "children": [brief.cross_links.executive_summary]})
        nodes.append({"tag": "hr"})

    for sec in brief.sections:
        nodes.extend(_build_telegraph_nodes_for_section(sec, date_compact=date_compact, bot_username=bot_username))

    if brief.cross_links and brief.cross_links.connections:
        nodes.append({"tag": "h3", "children": ["🔗 Межкатегорийные связи и стратегические выводы"]})
        for conn in brief.cross_links.connections:
            cats_badge = " × ".join(conn.involved_categories)
            nodes.append({"tag": "h4", "children": [f"{conn.title} [{cats_badge}]"]})
            nodes.append({"tag": "p", "children": [conn.analysis]})
            if conn.actionable_takeaway:
                nodes.append(
                    {
                        "tag": "p",
                        "children": [
                            {"tag": "strong", "children": ["Практический вывод: "]},
                            conn.actionable_takeaway,
                        ],
                    }
                )
            if conn.sources:
                src_children: list[Any] = [{"tag": "strong", "children": ["Источники: "]}]
                for idx, src in enumerate(conn.sources):
                    if idx > 0:
                        src_children.append(" | ")
                    if src.url and src.url.startswith("http"):
                        src_children.append({"tag": "a", "attrs": {"href": src.url}, "children": [src.label]})
                    else:
                        src_children.append(src.label)
                nodes.append({"tag": "p", "children": src_children})

        if brief.cross_links.key_risks_and_signals:
            nodes.append({"tag": "h4", "children": ["⚠️ Ключевые сигналы и риски дня"]})
            nodes.append(
                {
                    "tag": "ul",
                    "children": [{"tag": "li", "children": [r]} for r in brief.cross_links.key_risks_and_signals],
                }
            )
        nodes.append({"tag": "hr"})

    nodes.append({"tag": "h3", "children": ["💰 Расход токенов и стоимость пайплайна"]})
    for line in format_usage_text_ru(stats).splitlines()[1:]:
        nodes.append({"tag": "p", "children": [line]})

    return nodes


def _build_monthly_telegraph_nodes(monthly: MonthlyBrief, stats: UsageSummaryStats) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    date_str = monthly.generated_at.strftime("%d.%m.%Y %H:%M")
    rep = monthly.report

    nodes.append(
        {
            "tag": "p",
            "children": [
                {"tag": "strong", "children": ["Период: "]},
                f"{rep.month_label} ({monthly.days_covered} дн., дайджестов: {monthly.daily_digests_count}, сюжетов: {monthly.total_stories_in_month}) | Сформировано: {date_str}",
            ],
        }
    )

    nodes.append({"tag": "h3", "children": ["🧭 Главные макротренды и сдвиги месяца"]})
    nodes.append({"tag": "blockquote", "children": [rep.executive_macro_overview]})
    nodes.append({"tag": "hr"})

    for trend in rep.category_trends:
        nodes.append({"tag": "h3", "children": [trend.category_title]})
        nodes.append({"tag": "p", "children": [trend.main_trend_summary]})
        if trend.key_milestones_and_events:
            nodes.append({"tag": "p", "children": [{"tag": "strong", "children": ["Ключевые события и вехи месяца:"]}]})
            nodes.append({"tag": "ul", "children": [{"tag": "li", "children": [ev]} for ev in trend.key_milestones_and_events]})
        if trend.notable_numbers:
            nodes.append({"tag": "p", "children": [{"tag": "strong", "children": ["Главные цифры месяца:"]}]})
            nodes.append({"tag": "ul", "children": [{"tag": "li", "children": [num]} for num in trend.notable_numbers]})
        if trend.sources:
            src_children: list[Any] = [{"tag": "strong", "children": ["Источники: "]}]
            for idx, src in enumerate(trend.sources):
                if idx > 0:
                    src_children.append(" | ")
                if src.url and src.url.startswith("http"):
                    src_children.append({"tag": "a", "attrs": {"href": src.url}, "children": [src.label]})
                else:
                    src_children.append(src.label)
            nodes.append({"tag": "p", "children": src_children})
        nodes.append({"tag": "hr"})

    if rep.cross_category_structural_shifts:
        nodes.append({"tag": "h3", "children": ["🔗 Сквозные межкатегорийные сдвиги месяца"]})
        for conn in rep.cross_category_structural_shifts:
            cats = " × ".join(conn.involved_categories)
            nodes.append({"tag": "h4", "children": [f"{conn.title} [{cats}]"]})
            nodes.append({"tag": "p", "children": [conn.analysis]})
            if conn.actionable_takeaway:
                nodes.append({"tag": "p", "children": [{"tag": "strong", "children": ["Вывод: "]}, conn.actionable_takeaway]})
        nodes.append({"tag": "hr"})

    if rep.top_automation_and_business_ideas:
        nodes.append({"tag": "h3", "children": ["💡 Топ идей для автоматизации и бизнеса за месяц"]})
        for idea in rep.top_automation_and_business_ideas:
            nodes.append(
                {
                    "tag": "p",
                    "children": [
                        {"tag": "strong", "children": [f"• {idea.title} ({idea.target_sector_or_process}): "]},
                        f"{idea.proposed_solution} → Эффект: {idea.expected_impact}",
                    ],
                }
            )
        nodes.append({"tag": "hr"})

    if rep.watch_next_month:
        nodes.append({"tag": "h3", "children": ["🔭 Прогноз: за чем следить в следующем месяце"]})
        nodes.append({"tag": "ul", "children": [{"tag": "li", "children": [w]} for w in rep.watch_next_month]})
        nodes.append({"tag": "hr"})

    nodes.append({"tag": "h3", "children": ["💰 Расход токенов и стоимость пайплайна"]})
    for line in format_usage_text_ru(stats).splitlines()[1:]:
        nodes.append({"tag": "p", "children": [line]})

    return nodes


async def _create_telegraph_page(
    client: httpx.AsyncClient,
    access_token: str,
    title: str,
    nodes: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Создаёт страницу в Telegraph с автоматическим ожиданием при FLOOD_WAIT. Возвращает (url, path)."""
    for _attempt in range(4):
        try:
            resp = await client.post(
                "https://api.telegra.ph/createPage",
                data={
                    "access_token": access_token,
                    "title": title[:250],
                    "author_name": "Daily News Agent",
                    "content": json.dumps(nodes, ensure_ascii=False),
                    "return_content": "false",
                },
                timeout=25.0,
            )
            data = resp.json()
            if data.get("ok") and data.get("result", {}).get("url"):
                await asyncio.sleep(0.5)
                res = data["result"]
                return str(res["url"]), str(res.get("path") or "")

            err = str(data.get("error") or "")
            if err.startswith("FLOOD_WAIT_"):
                wait_sec = int(re.sub(r"\D+", "", err) or "9") + 1
                logger.info("Telegraph попросил подождать %d сек (FLOOD_WAIT), ждём и продолжаем...", wait_sec)
                await asyncio.sleep(wait_sec)
                continue

            logger.warning("Ошибка ответа Telegraph createPage: %s", data)
            return None, None
        except Exception as exc:
            logger.warning("Сетевая ошибка при публикации в Telegraph: %s", exc)
            await asyncio.sleep(2.0)
    return None, None


async def _edit_telegraph_page(
    client: httpx.AsyncClient,
    access_token: str,
    path: str,
    title: str,
    nodes: list[dict[str, Any]],
) -> bool:
    """Обновляет существующую страницу Telegraph (чтобы добавить ссылки на все части сверху и Назад/Вперёд снизу)."""
    for _attempt in range(4):
        try:
            resp = await client.post(
                "https://api.telegra.ph/editPage",
                data={
                    "access_token": access_token,
                    "path": path,
                    "title": title[:250],
                    "author_name": "Daily News Agent",
                    "content": json.dumps(nodes, ensure_ascii=False),
                    "return_content": "false",
                },
                timeout=25.0,
            )
            data = resp.json()
            if data.get("ok"):
                await asyncio.sleep(0.5)
                return True

            err = str(data.get("error") or "")
            if err.startswith("FLOOD_WAIT_"):
                wait_sec = int(re.sub(r"\D+", "", err) or "9") + 1
                logger.info("Telegraph попросил подождать %d сек (FLOOD_WAIT при editPage), ждём...", wait_sec)
                await asyncio.sleep(wait_sec)
                continue
            return False
        except Exception:
            await asyncio.sleep(2.0)
    return False


def _build_top_all_parts_bar(part_urls: list[str], current_idx: int) -> dict[str, Any]:
    """Верхняя панель: полный список всех частей (текущая часть выделена жирным, остальные — ссылки)."""
    children: list[Any] = [{"tag": "strong", "children": ["📑 Оглавление частей: "]}]
    for i, url in enumerate(part_urls):
        if i > 0:
            children.append(" | ")
        label = f"Часть {i + 1}/{len(part_urls)}"
        if i == current_idx:
            children.append({"tag": "strong", "children": [f"[{label}]"]})
        else:
            children.append({"tag": "a", "attrs": {"href": url}, "children": [label]})
    return {"tag": "blockquote", "children": children}


def _build_bottom_prev_next_bar(part_urls: list[str], current_idx: int) -> dict[str, Any]:
    """Нижняя панель: только 2 кнопки/ссылки — Предыдущая часть и Следующая часть."""
    children: list[Any] = []
    if current_idx > 0:
        prev_url = part_urls[current_idx - 1]
        children.append(
            {
                "tag": "a",
                "attrs": {"href": prev_url},
                "children": [f"⬅️ Предыдущая (Часть {current_idx}/{len(part_urls)})"],
            }
        )
    if current_idx < len(part_urls) - 1:
        if children:
            children.append("   |   ")
        next_url = part_urls[current_idx + 1]
        children.append(
            {
                "tag": "a",
                "attrs": {"href": next_url},
                "children": [f"➡️ Следующая (Часть {current_idx + 2}/{len(part_urls)})"],
            }
        )
    return {"tag": "blockquote", "children": [{"tag": "strong", "children": children}]}


async def _publish_nodes_to_telegraph(
    config: AppConfig,
    base_title: str,
    all_nodes: list[dict[str, Any]],
) -> str | None:
    if not config.delivery.publish_telegraph:
        return None

    async with httpx.AsyncClient() as client:
        access_token = config.telegraph_access_token
        if not access_token:
            acc_resp = await client.post(
                "https://api.telegra.ph/createAccount",
                data={"short_name": "DailyNewsAgent", "author_name": "Daily News Agent"},
                timeout=15.0,
            )
            acc_data = acc_resp.json()
            if not acc_data.get("ok"):
                logger.warning("Не удалось создать аккаунт Telegraph: %s", acc_data)
                return None
            access_token = acc_data["result"]["access_token"]

        serialized = json.dumps(all_nodes, ensure_ascii=False)
        if len(serialized.encode("utf-8")) <= 55_000:
            url, _ = await _create_telegraph_page(client, access_token, base_title, all_nodes)
            return url

        chunks: list[list[dict[str, Any]]] = []
        current_chunk: list[dict[str, Any]] = []
        current_size = 0
        for node in all_nodes:
            node_size = len(json.dumps(node, ensure_ascii=False).encode("utf-8"))
            if current_chunk and (current_size + node_size > 48_000):
                chunks.append(current_chunk)
                current_chunk = [node]
                current_size = node_size
            else:
                current_chunk.append(node)
                current_size += node_size
        if current_chunk:
            chunks.append(current_chunk)

        total_parts = len(chunks)
        part_urls: list[str] = [""] * total_parts
        part_paths: list[str] = [""] * total_parts
        part_titles: list[str] = [f"{base_title} (Часть {i + 1}/{total_parts})" for i in range(total_parts)]

        # 1. Создаём страницы с конца к началу (чтобы Часть 1 сразу получила ссылки на все остальные части!)
        for idx in range(total_parts - 1, -1, -1):
            part_nodes = list(chunks[idx])
            if idx == 0 and all(part_urls[1:]):
                # Для Части 1 мы уже знаем URL всех остальных частей — сразу ставим финальную навигацию!
                temp_urls = ["https://telegra.ph/", *part_urls[1:]]
                top_bar = _build_top_all_parts_bar(temp_urls, 0)
                bottom_bar = _build_bottom_prev_next_bar(temp_urls, 0)
                part_nodes.insert(0, top_bar)
                part_nodes.append({"tag": "hr"})
                part_nodes.append(bottom_bar)
            elif idx < total_parts - 1 and part_urls[idx + 1]:
                # Для промежуточных частей сразу ставим кнопку "Следующая часть" снизу
                next_only_bar = {
                    "tag": "blockquote",
                    "children": [
                        {
                            "tag": "strong",
                            "children": [
                                {
                                    "tag": "a",
                                    "attrs": {"href": part_urls[idx + 1]},
                                    "children": [f"➡️ Следующая (Часть {idx + 2}/{total_parts})"],
                                }
                            ],
                        }
                    ],
                }
                part_nodes.append({"tag": "hr"})
                part_nodes.append(next_only_bar)

            url, path = await _create_telegraph_page(client, access_token, part_titles[idx], part_nodes)
            if url:
                part_urls[idx] = url
                part_paths[idx] = path or ""

        # 2. Обновляем Части 2..N, чтобы добавить в них полное оглавление всех частей сверху и кнопку "⬅️ Предыдущая" снизу
        if all(part_urls):
            for idx in range(1, total_parts):
                if not part_paths[idx]:
                    continue
                updated_nodes = list(chunks[idx])
                updated_nodes.insert(0, _build_top_all_parts_bar(part_urls, idx))
                updated_nodes.append({"tag": "hr"})
                updated_nodes.append(_build_bottom_prev_next_bar(part_urls, idx))
                await _edit_telegraph_page(client, access_token, part_paths[idx], part_titles[idx], updated_nodes)

        return part_urls[0] if part_urls[0] else None


async def publish_to_telegraph(
    config: AppConfig,
    brief: DailyBrief,
    stats: UsageSummaryStats,
) -> str | None:
    store = load_bookmarks_store()
    bot_username = await get_or_fetch_bot_username(config, store)
    if not store.telegraph_url:
        await publish_or_update_bookmarks_telegraph(config, store)
    date_str = brief.generated_at.strftime("%d.%m.%Y")
    base_title = f"Ежедневная аналитическая сводка — {date_str}"
    nodes = _build_telegraph_nodes(
        brief,
        stats,
        bot_username=bot_username,
        bookmarks_url=store.telegraph_url,
    )
    return await _publish_nodes_to_telegraph(config, base_title, nodes)


async def publish_monthly_to_telegraph(
    config: AppConfig,
    monthly: MonthlyBrief,
    stats: UsageSummaryStats,
) -> str | None:
    date_str = monthly.generated_at.strftime("%m.%Y")
    base_title = f"Итоги и тренды месяца — {date_str}"
    nodes = _build_monthly_telegraph_nodes(monthly, stats)
    return await _publish_nodes_to_telegraph(config, base_title, nodes)


# ==============================================================================
# 2. Отправка сводки и ссылки на Instant View через Telegram-бота (без обрыва HTML!)
# ==============================================================================


def _assemble_safe_telegram_html(body_lines: list[str], footer_html: str, max_chars: int = 4000) -> str:
    """
    Собирает сообщение построчно так, чтобы НИКОГДА не разрезать HTML-тег (<b>, <i>, <a href=...>)
    посередине и не превысить лимит в 4096 символов Telegram.
    """
    notice_line = "\n<i>...полный разбор всех сюжетов доступен по ссылке Instant View выше...</i>"
    reserved = len(footer_html) + len(notice_line) + 20
    budget = max(500, max_chars - reserved)

    kept_lines: list[str] = []
    current_len = 0
    truncated = False

    for line in body_lines:
        line_len = len(line) + 1
        if current_len + line_len > budget:
            truncated = True
            break
        kept_lines.append(line)
        current_len += line_len

    result = "\n".join(kept_lines)
    if truncated:
        result += notice_line
    result += "\n\n" + footer_html
    return result


async def send_telegram_bot_digest(
    config: AppConfig,
    brief: DailyBrief,
    stats: UsageSummaryStats,
) -> bool:
    """Отправляет в Telegram карточку дня со ссылкой на Telegraph Instant View и статистикой расходов."""
    if not config.delivery.send_telegram_bot:
        return False
    if not config.tg_bot_token or not config.tg_chat_id:
        logger.info("TG_BOT_TOKEN или TG_CHAT_ID не заданы — пропускаем отправку в Telegram-бота.")
        return False

    date_str = brief.generated_at.strftime("%d.%m.%Y %H:%M")
    body_lines: list[str] = [
        f"📰 <b>Ежедневная аналитическая сводка ({date_str})</b>",
        (
            f"За 24ч проверено: <b>{brief.total_raw_telegram_posts}</b> постов TG | "
            f"<b>{brief.total_raw_emails}</b> писем Gmail → "
            f"<b>{brief.total_extracted_stories}</b> сюжетов"
        ),
        "",
    ]

    if brief.telegraph_url:
        body_lines.append(
            f"⚡ <b><a href=\"{html.escape(brief.telegraph_url, quote=True)}\">ОТКРЫТЬ ПОЛНЫЙ ОТЧЁТ (Instant View)</a></b>"
        )
        store = load_bookmarks_store()
        if store.telegraph_url:
            body_lines.append(
                f"📌 <b><a href=\"{html.escape(store.telegraph_url, quote=True)}\">МОЯ БАЗА СОХРАНЁННЫХ ЗАМЕТОК ({len(store.items)} шт.)</a></b>"
            )
        body_lines.append("")

    if brief.cross_links and brief.cross_links.executive_summary:
        body_lines.append("🧭 <b>Картина дня:</b>")
        body_lines.append(html.escape(brief.cross_links.executive_summary))
        body_lines.append("")

    body_lines.append("📂 <b>Сводка по категориям:</b>")
    for sec in brief.sections:
        body_lines.append(f"• <b>{html.escape(sec.title)}</b>: {sec.stories_count} сюж.")
        for topic in sec.report.topics[:2]:
            first_src = topic.sources[0] if topic.sources else None
            if first_src and first_src.url.startswith("http"):
                body_lines.append(
                    f"   — {html.escape(topic.headline)} "
                    f"(<a href=\"{html.escape(first_src.url, quote=True)}\">{html.escape(first_src.label)}</a>)"
                )
            else:
                body_lines.append(f"   — {html.escape(topic.headline)}")
        if len(sec.report.topics) > 2:
            body_lines.append(f"   <i>...и ещё {len(sec.report.topics) - 2} в полной версии</i>")

    if brief.cross_links and brief.cross_links.connections:
        body_lines.append("")
        body_lines.append("🔗 <b>Главные межкатегорийные связи:</b>")
        for conn in brief.cross_links.connections[:3]:
            cats = " × ".join(conn.involved_categories)
            body_lines.append(f"• <b>{html.escape(conn.title)}</b> <i>({html.escape(cats)})</i>")

    footer_html = html.escape(format_usage_text_ru(stats))
    full_message = _assemble_safe_telegram_html(body_lines, footer_html, max_chars=4000)

    api_url = f"https://api.telegram.org/bot{config.tg_bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            api_url,
            json={
                "chat_id": config.tg_chat_id,
                "text": full_message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20.0,
        )
        data = resp.json()
        if not data.get("ok"):
            desc = str(data.get("description") or "")
            if "chat not found" in desc.lower():
                logger.warning(
                    "Telegram вернул 'chat not found' для chat_id=%s! "
                    "ОБЯЗАТЕЛЬНО откройте вашего бота в Telegram (https://t.me/dy_daily_news_digest_bot) "
                    "и нажмите кнопку START (/start), после чего выполните: python -m src.main --republish-latest",
                    config.tg_chat_id,
                )
            else:
                logger.warning("Ошибка отправки сообщения в Telegram-бота: %s", data)
            return False

    logger.info("Сводка успешно отправлена в Telegram-бота (chat_id=%s)", config.tg_chat_id)
    return True


async def send_monthly_telegram_bot_digest(
    config: AppConfig,
    monthly: MonthlyBrief,
    stats: UsageSummaryStats,
) -> bool:
    """Отправляет ежемесячный итоговый отчёт в Telegram-бота."""
    if not config.delivery.send_telegram_bot or not config.tg_bot_token or not config.tg_chat_id:
        return False

    rep = monthly.report
    date_str = monthly.generated_at.strftime("%d.%m.%Y")
    body_lines: list[str] = [
        f"📊 <b>ЕЖЕМЕСЯЧНЫЙ АНАЛИТИЧЕСКИЙ ОТЧЁТ ({date_str})</b>",
        f"Проанализировано дайджестов за месяц: <b>{monthly.daily_digests_count}</b> | Сюжетов: <b>{monthly.total_stories_in_month}</b>",
        "",
    ]

    if monthly.telegraph_url:
        body_lines.append(
            f"⚡ <b><a href=\"{html.escape(monthly.telegraph_url, quote=True)}\">ОТКРЫТЬ ПОЛНЫЙ МЕСЯЧНЫЙ ДОКЛАД (Instant View)</a></b>"
        )
        body_lines.append("")

    body_lines.append("🧭 <b>Главные макротренды месяца:</b>")
    body_lines.append(html.escape(rep.executive_macro_overview[:1000]))
    body_lines.append("")

    if rep.watch_next_month:
        body_lines.append("🔭 <b>За чем следить в новом месяце:</b>")
        for item in rep.watch_next_month[:5]:
            body_lines.append(f"• {html.escape(item)}")

    footer_html = html.escape(format_usage_text_ru(stats))
    full_message = _assemble_safe_telegram_html(body_lines, footer_html, max_chars=4000)

    api_url = f"https://api.telegram.org/bot{config.tg_bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            api_url,
            json={
                "chat_id": config.tg_chat_id,
                "text": full_message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20.0,
        )
        return bool(resp.json().get("ok"))


# ==============================================================================
# 3. Формирование и отправка полного HTML-дайджеста на почту (Gmail SMTP)
# ==============================================================================


def render_html_email(brief: DailyBrief, stats: UsageSummaryStats) -> str:
    """Генерирует адаптивное HTML-письмо со всеми категориями, источниками и таблицей стоимости."""
    date_str = brief.generated_at.strftime("%d.%m.%Y %H:%M")
    parts: list[str] = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<style>",
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; line-height: 1.6; max-width: 860px; margin: 0 auto; padding: 20px; background: #f9fafb; }",
        ".card { background: #ffffff; border-radius: 12px; padding: 24px; margin-bottom: 20px; border: 1px solid #e5e7eb; }",
        "h1 { color: #111827; font-size: 24px; margin-top: 0; }",
        "h2 { color: #1e3a8a; font-size: 20px; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; margin-top: 0; }",
        "h3 { color: #111827; font-size: 16px; margin-bottom: 6px; }",
        ".meta { color: #4b5563; font-size: 14px; }",
        ".overview { background: #eff6ff; border-left: 4px solid #2563eb; padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }",
        ".idea { background: #ecfdf5; border-left: 4px solid #10b981; padding: 12px 16px; border-radius: 6px; margin-top: 12px; }",
        ".sources { font-size: 13px; color: #4b5563; margin-top: 8px; }",
        ".sources a { color: #2563eb; text-decoration: none; font-weight: 500; }",
        "table.cost { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 10px; }",
        "table.cost th, table.cost td { border: 1px solid #e5e7eb; padding: 8px 12px; text-align: left; }",
        "table.cost th { background: #f3f4f6; }",
        "</style></head><body>",
        "<div class='card'>",
        f"<h1>📰 Ежедневная аналитическая сводка ({html.escape(date_str)})</h1>",
        f"<p class='meta'>Окно: последние {brief.window_hours}ч | Постов Telegram: <b>{brief.total_raw_telegram_posts}</b> | Писем Gmail: <b>{brief.total_raw_emails}</b> | Выделено сюжетов: <b>{brief.total_extracted_stories}</b></p>",
    ]

    if brief.telegraph_url:
        parts.append(
            f"<p><a href='{html.escape(brief.telegraph_url)}' style='display:inline-block;background:#2563eb;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600;'>⚡ Открыть в Telegraph (Instant View)</a></p>"
        )

    if brief.cross_links and brief.cross_links.executive_summary:
        parts.append(f"<div class='overview'><b>🧭 Главная картина дня:</b><br>{html.escape(brief.cross_links.executive_summary)}</div>")

    parts.append("<b>Оглавление по категориям:</b><ul>")
    for sec in brief.sections:
        parts.append(f"<li><a href='#sec_{sec.slug}'>{html.escape(sec.title)}</a> ({sec.stories_count})</li>")
    parts.append("<li><a href='#sec_cross'>🔗 Межкатегорийные связи и выводы</a></li>")
    parts.append("<li><a href='#sec_cost'>💰 Статистика токенов и стоимости</a></li>")
    parts.append("</ul></div>")

    for sec in brief.sections:
        parts.append(f"<div class='card' id='sec_{sec.slug}'>")
        parts.append(f"<h2>{html.escape(sec.title)} ({sec.stories_count})</h2>")
        if sec.report.category_overview:
            parts.append(f"<div class='overview'>{html.escape(sec.report.category_overview)}</div>")

        for idx, topic in enumerate(sec.report.topics, start=1):
            parts.append(f"<h3>{idx}. {html.escape(topic.headline)}</h3>")
            for para in topic.summary_paragraphs:
                parts.append(f"<p>{html.escape(para)}</p>")

            if topic.key_figures_and_facts:
                parts.append("<ul>")
                for fact in topic.key_figures_and_facts:
                    parts.append(f"<li>{html.escape(fact)}</li>")
                parts.append("</ul>")

            if topic.implications_or_context:
                parts.append(f"<p><b>Значение и последствия:</b> {html.escape(topic.implications_or_context)}</p>")

            if topic.sources:
                links_html: list[str] = []
                for src in topic.sources:
                    if src.url and src.url.startswith("http"):
                        links_html.append(f"<a href='{html.escape(src.url)}'>{html.escape(src.label)}</a>")
                    else:
                        links_html.append(html.escape(src.label))
                parts.append(f"<div class='sources'>🔗 Источники: {' | '.join(links_html)}</div>")
            parts.append("<hr style='border:none;border-top:1px solid #f3f4f6;margin:16px 0;'>")

        if sec.report.automation_ideas:
            parts.append("<h3>💡 Прикладные идеи для автоматизации</h3>")
            for idea in sec.report.automation_ideas:
                parts.append(
                    "<div class='idea'>"
                    f"<b>{html.escape(idea.title)}</b> ({html.escape(idea.target_sector_or_process)})<br>"
                    f"<b>Решение:</b> {html.escape(idea.proposed_solution)}<br>"
                    f"<b>Ожидаемый эффект:</b> {html.escape(idea.expected_impact)}<br>"
                    f"<span class='meta'>Основано на: {html.escape(idea.inspired_by_source)}</span>"
                    "</div>"
                )

        parts.append("</div>")

    if brief.cross_links and brief.cross_links.connections:
        parts.append("<div class='card' id='sec_cross'>")
        parts.append("<h2>🔗 Межкатегорийные связи и стратегические выводы</h2>")
        for conn in brief.cross_links.connections:
            cats = " × ".join(conn.involved_categories)
            parts.append(f"<h3>{html.escape(conn.title)} <span class='meta'>[{html.escape(cats)}]</span></h3>")
            parts.append(f"<p>{html.escape(conn.analysis)}</p>")
            if conn.actionable_takeaway:
                parts.append(f"<p><b>💡 Практический вывод:</b> {html.escape(conn.actionable_takeaway)}</p>")
        if brief.cross_links.key_risks_and_signals:
            parts.append("<h3>⚠️ Ключевые сигналы и риски дня</h3><ul>")
            for risk in brief.cross_links.key_risks_and_signals:
                parts.append(f"<li>{html.escape(risk)}</li>")
            parts.append("</ul>")
        parts.append("</div>")

    cur = stats.current_run
    parts.extend(
        [
            "<div class='card' id='sec_cost'>",
            "<h2>💰 Статистика токенов и стоимости (LLM Usage)</h2>",
            f"<p class='meta'>Модели: <b>{html.escape(cur.model)}</b> | Вызовов LLM за запуск: <b>{cur.calls_count}</b></p>",
            "<table class='cost'>",
            "<tr><th>Период / Метрика</th><th>Запусков</th><th>Токены</th><th>Стоимость (USD)</th></tr>",
            f"<tr><td><b>Текущий запуск</b></td><td>1</td><td>{cur.total_tokens:,} (in: {cur.input_tokens:,}, out: {cur.output_tokens:,}, think: {cur.thinking_tokens:,})</td><td><b>${cur.cost_usd:.4f}</b></td></tr>",
            f"<tr><td><b>Среднее за запуск</b></td><td>—</td><td>{stats.avg_tokens_per_run:,}</td><td><b>${stats.avg_cost_per_run_usd:.4f}</b></td></tr>",
            f"<tr><td>За сегодня (24ч)</td><td>{stats.today_runs}</td><td>{stats.today_tokens:,}</td><td>${stats.today_cost_usd:.4f}</td></tr>",
            f"<tr><td>За 7 дней (неделя)</td><td>{stats.week_runs}</td><td>{stats.week_tokens:,}</td><td>${stats.week_cost_usd:.4f}</td></tr>",
            f"<tr><td>За 30 дней (месяц)</td><td>{stats.month_runs}</td><td>{stats.month_tokens:,}</td><td>${stats.month_cost_usd:.4f}</td></tr>",
            f"<tr><td><b>За всё время (Total)</b></td><td>{stats.total_runs_count}</td><td>{stats.all_time_tokens:,}</td><td><b>${stats.all_time_cost_usd:.4f}</b></td></tr>",
            "</table></div></body></html>",
        ]
    )
    return "\n".join(parts)


def _send_smtp_message(config: AppConfig, msg: MIMEMultipart) -> bool:
    """Отправляет MIME-письмо через Gmail SMTP (сначала порт 465 SSL, при обрыве — порт 587 STARTTLS)."""
    try:
        with smtplib.SMTP_SSL(config.delivery.smtp_host, config.delivery.smtp_port, timeout=30) as server:
            server.login(config.gmail_user, config.gmail_app_password)
            server.sendmail(config.gmail_user, [config.recipient_email], msg.as_string())
        return True
    except Exception:
        try:
            with smtplib.SMTP(config.delivery.smtp_host, 587, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(config.gmail_user, config.gmail_app_password)
                server.sendmail(config.gmail_user, [config.recipient_email], msg.as_string())
            return True
        except Exception as exc:
            logger.warning("Ошибка при отправке Email-дайджеста: %s", exc)
            return False


def send_email_digest(config: AppConfig, brief: DailyBrief, stats: UsageSummaryStats) -> bool:
    """Отправляет сформированный HTML-дайджест через Gmail SMTP."""
    if not config.delivery.send_email_digest:
        return False
    if not config.gmail_user or not config.gmail_app_password or not config.recipient_email:
        logger.info("Параметры Gmail (GMAIL_USER / GMAIL_APP_PASSWORD) не заполнены — пропускаем отправку Email.")
        return False

    date_str = brief.generated_at.strftime("%d.%m.%Y")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📰 Ежедневная сводка новостей за {date_str} ({brief.total_extracted_stories} сюжетов)"
    msg["From"] = config.gmail_user
    msg["To"] = config.recipient_email

    html_body = render_html_email(brief, stats)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if _send_smtp_message(config, msg):
        logger.info("HTML Email-дайджест успешно отправлен на %s", config.recipient_email)
        return True
    return False


def send_monthly_email_digest(config: AppConfig, monthly: MonthlyBrief, stats: UsageSummaryStats) -> bool:
    """Отправляет ежемесячный аналитический доклад на почту через Gmail SMTP."""
    if not config.delivery.send_email_digest:
        return False
    if not config.gmail_user or not config.gmail_app_password or not config.recipient_email:
        return False

    date_str = monthly.generated_at.strftime("%m.%Y")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 Итоги и тренды месяца за {date_str} ({monthly.daily_digests_count} дайджестов)"
    msg["From"] = config.gmail_user
    msg["To"] = config.recipient_email

    md_text = render_monthly_markdown(monthly, stats)
    msg.attach(MIMEText(md_text, "plain", "utf-8"))

    if _send_smtp_message(config, msg):
        logger.info("Ежемесячный Email-отчёт отправлен на %s", config.recipient_email)
        return True
    return False


# ==============================================================================
# 4. Сохранение локальной Markdown-копии (для архива / отладки)
# ==============================================================================


def render_markdown_brief(brief: DailyBrief, stats: UsageSummaryStats) -> str:
    date_str = brief.generated_at.strftime("%d.%m.%Y %H:%M")
    lines: list[str] = [
        f"# 📰 Ежедневная аналитическая сводка ({date_str})",
        "",
        f"- **Период:** последние {brief.window_hours} часов",
        f"- **Проверено постов Telegram:** {brief.total_raw_telegram_posts}",
        f"- **Проверено писем Gmail:** {brief.total_raw_emails}",
        f"- **Извлечено содержательных сюжетов:** {brief.total_extracted_stories}",
    ]
    if brief.telegraph_url:
        lines.append(f"- **Telegraph Instant View:** {brief.telegraph_url}")
    lines.append("")

    if brief.cross_links and brief.cross_links.executive_summary:
        lines.extend(
            [
                "## 🧭 Главная картина дня",
                brief.cross_links.executive_summary,
                "",
            ]
        )

    for sec in brief.sections:
        lines.append(f"## {sec.title} ({sec.stories_count})")
        lines.append(f"*{sec.report.category_overview}*")
        lines.append("")

        for idx, topic in enumerate(sec.report.topics, start=1):
            lines.append(f"### {idx}. {topic.headline}")
            for para in topic.summary_paragraphs:
                lines.append(para)
                lines.append("")
            if topic.key_figures_and_facts:
                lines.append("**Ключевые цифры и факты:**")
                for fact in topic.key_figures_and_facts:
                    lines.append(f"- {fact}")
                lines.append("")
            if topic.implications_or_context:
                lines.append(f"**Значение / Вывод:** {topic.implications_or_context}")
                lines.append("")
            if topic.sources:
                src_links = [
                    f"[{s.label}]({s.url})" if s.url.startswith("http") else s.label
                    for s in topic.sources
                ]
                lines.append(f"**Источники:** {' | '.join(src_links)}")
                lines.append("")

        if sec.report.automation_ideas:
            lines.append("### 💡 Прикладные идеи для автоматизации")
            for idea in sec.report.automation_ideas:
                lines.append(
                    f"- **{idea.title}** (*{idea.target_sector_or_process}*): "
                    f"{idea.proposed_solution} → **Эффект:** {idea.expected_impact} "
                    f"*(Основано на: {idea.inspired_by_source})*"
                )
            lines.append("")

    if brief.cross_links and brief.cross_links.connections:
        lines.append("## 🔗 Межкатегорийные связи и стратегические выводы")
        for conn in brief.cross_links.connections:
            cats = " × ".join(conn.involved_categories)
            lines.append(f"### {conn.title} [{cats}]")
            lines.append(conn.analysis)
            if conn.actionable_takeaway:
                lines.append(f"- **Практический вывод:** {conn.actionable_takeaway}")
            lines.append("")

        if brief.cross_links.key_risks_and_signals:
            lines.append("### ⚠️ Ключевые сигналы и риски дня")
            for risk in brief.cross_links.key_risks_and_signals:
                lines.append(f"- {risk}")
            lines.append("")

    lines.append("## 💰 Статистика токенов и стоимости")
    lines.append(format_usage_text_ru(stats))
    lines.append("")
    return "\n".join(lines)


def render_monthly_markdown(monthly: MonthlyBrief, stats: UsageSummaryStats) -> str:
    date_str = monthly.generated_at.strftime("%d.%m.%Y %H:%M")
    rep = monthly.report
    lines: list[str] = [
        f"# 📊 Ежемесячный аналитический доклад ({date_str})",
        "",
        f"- **Период:** {rep.month_label} ({monthly.days_covered} дн.)",
        f"- **Проанализировано ежедневных дайджестов:** {monthly.daily_digests_count}",
        f"- **Всего сюжетов за месяц:** {monthly.total_stories_in_month}",
    ]
    if monthly.telegraph_url:
        lines.append(f"- **Telegraph Instant View:** {monthly.telegraph_url}")
    lines.append("")
    lines.append("## 🧭 Главные макротренды и сдвиги месяца")
    lines.append(rep.executive_macro_overview)
    lines.append("")

    for trend in rep.category_trends:
        lines.append(f"## {trend.category_title}")
        lines.append(trend.main_trend_summary)
        lines.append("")
        if trend.key_milestones_and_events:
            lines.append("**Ключевые события и вехи месяца:**")
            for ev in trend.key_milestones_and_events:
                lines.append(f"- {ev}")
            lines.append("")
        if trend.notable_numbers:
            lines.append("**Главные цифры месяца:**")
            for num in trend.notable_numbers:
                lines.append(f"- {num}")
            lines.append("")
        if trend.sources:
            src_links = [
                f"[{s.label}]({s.url})" if s.url.startswith("http") else s.label
                for s in trend.sources
            ]
            lines.append(f"**Источники:** {' | '.join(src_links)}")
            lines.append("")

    if rep.cross_category_structural_shifts:
        lines.append("## 🔗 Сквозные межкатегорийные сдвиги месяца")
        for conn in rep.cross_category_structural_shifts:
            cats = " × ".join(conn.involved_categories)
            lines.append(f"### {conn.title} [{cats}]")
            lines.append(conn.analysis)
            if conn.actionable_takeaway:
                lines.append(f"- **Практический вывод:** {conn.actionable_takeaway}")
            lines.append("")

    if rep.top_automation_and_business_ideas:
        lines.append("## 💡 Топ идей для автоматизации и бизнеса за месяц")
        for idea in rep.top_automation_and_business_ideas:
            lines.append(
                f"- **{idea.title}** (*{idea.target_sector_or_process}*): "
                f"{idea.proposed_solution} → **Эффект:** {idea.expected_impact}"
            )
        lines.append("")

    if rep.watch_next_month:
        lines.append("## 🔭 Прогноз: за чем следить в следующем месяце")
        for w in rep.watch_next_month:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("## 💰 Статистика токенов и стоимости")
    lines.append(format_usage_text_ru(stats))
    lines.append("")
    return "\n".join(lines)


def save_markdown_file(brief: DailyBrief, stats: UsageSummaryStats) -> Path:
    out_dir = ROOT_DIR / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = brief.generated_at.strftime("brief_%Y-%m-%d.md")
    path = out_dir / filename
    path.write_text(render_markdown_brief(brief, stats), encoding="utf-8")
    logger.info("Локальная копия отчёта сохранена: %s", path)
    return path


def save_monthly_markdown_file(monthly: MonthlyBrief, stats: UsageSummaryStats) -> Path:
    out_dir = ROOT_DIR / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = monthly.generated_at.strftime("monthly_%Y-%m.md")
    path = out_dir / filename
    path.write_text(render_monthly_markdown(monthly, stats), encoding="utf-8")
    logger.info("Локальная копия месячного доклада сохранена: %s", path)
    return path
