from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from src.config import AppConfig, ROOT_DIR
from src.models import DailyBrief, SourceRef

logger = logging.getLogger(__name__)

BOOKMARKS_FILE = ROOT_DIR / "data" / "saved_bookmarks.json"
BOOKMARKS_MD_FILE = ROOT_DIR / "output" / "saved_bookmarks.md"


class SavedBookmark(BaseModel):
    """Отдельная сохранённая новость/секция в Базе Заметок."""

    bookmark_id: str  # Например: "20261023_it_9"
    saved_at: str
    digest_date: str  # "2026-10-23"
    category_slug: str
    category_title: str
    topic_index: int
    headline: str
    summary_paragraphs: list[str] = Field(default_factory=list)
    key_figures_and_facts: list[str] = Field(default_factory=list)
    implications_or_context: str = ""
    sources: list[SourceRef] = Field(default_factory=list)
    user_note: str = ""


class SavedIdeaBookmark(BaseModel):
    """Отдельная сохранённая идея для автоматизации в отдельной Базе Идей."""

    idea_id: str  # Например: "20261029_avtomatizatsiya_1"
    saved_at: str
    digest_date: str
    category_slug: str
    category_title: str
    idea_index: int
    title: str
    target_sector_or_process: str
    proposed_solution: str
    expected_impact: str
    inspired_by_source: str
    user_note: str = ""


class BookmarksStore(BaseModel):
    """Хранилище базы заметок (новости) и отдельной базы идей для автоматизации."""

    last_update_id: int = 0
    bot_username: str = ""
    telegraph_access_token: str = ""
    telegraph_url: str = ""
    telegraph_path: str = ""
    items: list[SavedBookmark] = Field(default_factory=list)

    # Отдельная страница Telegraph и список для идей автоматизации
    ideas_telegraph_url: str = ""
    ideas_telegraph_path: str = ""
    idea_items: list[SavedIdeaBookmark] = Field(default_factory=list)
    last_saved_type: str = "news"  # "news" | "idea"


def load_bookmarks_store() -> BookmarksStore:
    if not BOOKMARKS_FILE.exists():
        return BookmarksStore()
    try:
        raw = json.loads(BOOKMARKS_FILE.read_text(encoding="utf-8"))
        return BookmarksStore.model_validate(raw)
    except Exception as exc:
        logger.warning("Не удалось загрузить базу заметок %s: %s", BOOKMARKS_FILE, exc)
        return BookmarksStore()


def save_bookmarks_store(store: BookmarksStore) -> None:
    BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = store.model_dump(mode="json")
    # Никогда не сохраняем токен Telegraph в открытый JSON в git — он читается из TELEGRAPH_ACCESS_TOKEN
    payload["telegraph_access_token"] = ""
    BOOKMARKS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _save_bookmarks_markdown(store)


def _save_bookmarks_markdown(store: BookmarksStore) -> None:
    BOOKMARKS_MD_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# 📌 Моя база сохранённых новостей и заметок",
        "",
        f"- **Всего сохранено сюжетов:** {len(store.items)}",
        f"- **Всего сохранено идей автоматизации:** {len(store.idea_items)}",
    ]
    if store.telegraph_url:
        lines.append(f"- **База Новостей (Telegraph Instant View):** {store.telegraph_url}")
    if store.ideas_telegraph_url:
        lines.append(f"- **База Идей Автоматизации (Telegraph Instant View):** {store.ideas_telegraph_url}")
    lines.append("")

    if store.idea_items:
        lines.append("# 💡 Сохранённые идеи для автоматизации")
        lines.append("")
        for idx, idea in enumerate(reversed(store.idea_items), start=1):
            lines.append(f"## {idx}. {idea.title} ({idea.target_sector_or_process})")
            lines.append(f"*Дата дайджеста: {idea.digest_date} | Идея #{idea.idea_index}*")
            if idea.user_note:
                lines.append(f"> 💬 **Моя заметка:** {idea.user_note}")
            lines.append(f"- **Решение:** {idea.proposed_solution}")
            lines.append(f"- **Ожидаемый эффект:** {idea.expected_impact}")
            lines.append(f"- **Основано на:** {idea.inspired_by_source}")
            lines.append("")
        lines.append("---")
        lines.append("")

    for idx, item in enumerate(reversed(store.items), start=1):
        lines.append(f"## {idx}. [{item.category_title}] {item.headline}")
        lines.append(f"*Дата дайджеста: {item.digest_date} | Сюжет #{item.topic_index} | ID: `{item.bookmark_id}`*")
        lines.append("")
        if item.user_note:
            lines.append(f"> 💬 **Моя заметка:** {item.user_note}")
            lines.append("")
        for p in item.summary_paragraphs:
            lines.append(p)
            lines.append("")
        if item.key_figures_and_facts:
            lines.append("**Ключевые цифры и факты:**")
            for f in item.key_figures_and_facts:
                lines.append(f"- {f}")
            lines.append("")
        if item.implications_or_context:
            lines.append(f"**Значение / Вывод:** {item.implications_or_context}")
            lines.append("")
        if item.sources:
            src_links = [
                f"[{s.label}]({s.url})" if s.url.startswith("http") else s.label
                for s in item.sources
            ]
            lines.append(f"**Первоисточники:** {' | '.join(src_links)}")
            lines.append("")
        lines.append("---")
        lines.append("")

    BOOKMARKS_MD_FILE.write_text("\n".join(lines), encoding="utf-8")


async def get_or_fetch_bot_username(config: AppConfig, store: BookmarksStore) -> str:
    """Возвращает username вашего Telegram-бота (для генерации deep-ссылок 📌 Сохранить)."""
    if store.bot_username:
        return store.bot_username
    if not config.tg_bot_token:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{config.tg_bot_token}/getMe",
                timeout=10.0,
            )
            data = resp.json()
            if data.get("ok") and data.get("result", {}).get("username"):
                store.bot_username = str(data["result"]["username"])
                save_bookmarks_store(store)
                return store.bot_username
    except Exception as exc:
        logger.warning("Не удалось получить username бота: %s", exc)
    return ""


def _find_story_in_saved_digests(
    config: AppConfig,
    date_compact: str,
    category_slug: str,
    topic_idx: int,
) -> SavedBookmark | None:
    """Ищет сюжет (например, 20261023, 'it', 9) в архиве data/daily_digests/."""
    if len(date_compact) == 8 and date_compact.isdigit():
        iso_date = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"
    else:
        iso_date = date_compact

    digests_dir = ROOT_DIR / config.general.daily_digests_dir
    target_file = digests_dir / f"digest_{iso_date}.json"
    if not target_file.exists() and os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        subprocess.run(["git", "pull", "--rebase"], cwd=ROOT_DIR, check=False)
    candidate_files = [target_file] if target_file.exists() else sorted(digests_dir.glob("digest_*.json"), reverse=True)

    for json_path in candidate_files:
        try:
            brief = DailyBrief.model_validate_json(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for sec in brief.sections:
            if sec.slug == category_slug:
                if 1 <= topic_idx <= len(sec.report.topics):
                    t = sec.report.topics[topic_idx - 1]
                    b_date = brief.generated_at.strftime("%Y-%m-%d")
                    b_compact = brief.generated_at.strftime("%Y%m%d")
                    return SavedBookmark(
                        bookmark_id=f"{b_compact}_{category_slug}_{topic_idx}",
                        saved_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        digest_date=b_date,
                        category_slug=sec.slug,
                        category_title=sec.title,
                        topic_index=topic_idx,
                        headline=t.headline,
                        summary_paragraphs=t.summary_paragraphs,
                        key_figures_and_facts=t.key_figures_and_facts,
                        implications_or_context=t.implications_or_context,
                        sources=t.sources,
                        user_note="",
                    )
    return None


def _find_idea_in_saved_digests(
    config: AppConfig,
    date_compact: str,
    category_slug: str,
    idea_idx: int,
) -> SavedIdeaBookmark | None:
    """Ищет прикладную идею для автоматизации в архиве data/daily_digests/."""
    if len(date_compact) == 8 and date_compact.isdigit():
        iso_date = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"
    else:
        iso_date = date_compact

    digests_dir = ROOT_DIR / config.general.daily_digests_dir
    target_file = digests_dir / f"digest_{iso_date}.json"
    if not target_file.exists() and os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        subprocess.run(["git", "pull", "--rebase"], cwd=ROOT_DIR, check=False)
    candidate_files = [target_file] if target_file.exists() else sorted(digests_dir.glob("digest_*.json"), reverse=True)

    for json_path in candidate_files:
        try:
            brief = DailyBrief.model_validate_json(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for sec in brief.sections:
            if sec.slug == category_slug and sec.report.automation_ideas:
                if 1 <= idea_idx <= len(sec.report.automation_ideas):
                    idea = sec.report.automation_ideas[idea_idx - 1]
                    b_date = brief.generated_at.strftime("%Y-%m-%d")
                    b_compact = brief.generated_at.strftime("%Y%m%d")
                    return SavedIdeaBookmark(
                        idea_id=f"{b_compact}_{category_slug}_{idea_idx}",
                        saved_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        digest_date=b_date,
                        category_slug=sec.slug,
                        category_title=sec.title,
                        idea_index=idea_idx,
                        title=idea.title,
                        target_sector_or_process=idea.target_sector_or_process,
                        proposed_solution=idea.proposed_solution,
                        expected_impact=idea.expected_impact,
                        inspired_by_source=idea.inspired_by_source,
                        user_note="",
                    )
    return None


def _build_bookmarks_telegraph_nodes(store: BookmarksStore) -> list[dict[str, Any]]:
    """Формирует узлы страницы Telegraph для «📌 Моя база сохранённых заметок»."""
    nodes: list[dict[str, Any]] = []
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    nodes.append(
        {
            "tag": "p",
            "children": [
                {"tag": "strong", "children": ["Всего сохранено сюжетов: "]},
                f"{len(store.items)} | ",
                {"tag": "em", "children": [f"Обновлено: {now_str}"]},
            ],
        }
    )
    nodes.append(
        {
            "tag": "blockquote",
            "children": [
                "💡 Чтобы добавить свою текстовую заметку к последнему сохранённому сюжету, "
                "просто отправьте любой текст в чат с ботом после нажатия «📌 Сохранить»!"
            ],
        }
    )
    nodes.append({"tag": "hr"})

    if not store.items:
        nodes.append({"tag": "p", "children": ["База заметок пока пуста. Нажмите «📌 Сохранить сюжет» под любой новостью в ежедневном дайджесте!"]})
        return nodes

    by_cat: dict[str, list[SavedBookmark]] = {}
    for item in reversed(store.items):
        by_cat.setdefault(item.category_title, []).append(item)

    cat_summary = " | ".join(f"{cat}: {len(items)}" for cat, items in by_cat.items())
    nodes.append({"tag": "p", "children": [{"tag": "strong", "children": ["По категориям: "]}, cat_summary]})
    nodes.append({"tag": "hr"})

    for cat_title, cat_items in by_cat.items():
        nodes.append({"tag": "h3", "children": [f"{cat_title} ({len(cat_items)})"]})
        for idx, item in enumerate(cat_items, start=1):
            nodes.append(
                {
                    "tag": "h4",
                    "children": [f"{idx}. {item.headline} ({item.digest_date}, #{item.topic_index})"],
                }
            )
            if item.user_note:
                nodes.append(
                    {
                        "tag": "blockquote",
                        "children": [
                            {"tag": "strong", "children": ["💬 Моя заметка: "]},
                            item.user_note,
                        ],
                    }
                )
            for para in item.summary_paragraphs:
                nodes.append({"tag": "p", "children": [para]})

            if item.key_figures_and_facts:
                nodes.append({"tag": "p", "children": [{"tag": "strong", "children": ["Ключевые цифры и факты:"]}]})
                nodes.append(
                    {
                        "tag": "ul",
                        "children": [{"tag": "li", "children": [f]} for f in item.key_figures_and_facts],
                    }
                )

            if item.implications_or_context:
                nodes.append(
                    {
                        "tag": "p",
                        "children": [
                            {"tag": "strong", "children": ["Значение / Вывод: "]},
                            item.implications_or_context,
                        ],
                    }
                )

            if item.sources:
                src_children: list[Any] = [{"tag": "strong", "children": ["Первоисточники: "]}]
                for s_idx, src in enumerate(item.sources):
                    if s_idx > 0:
                        src_children.append(" | ")
                    if src.url and src.url.startswith("http"):
                        src_children.append({"tag": "a", "attrs": {"href": src.url}, "children": [src.label or "Ссылка"]})
                    else:
                        src_children.append(src.label or "Источник")
                nodes.append({"tag": "p", "children": src_children})

        nodes.append({"tag": "hr"})

    return nodes


def _build_ideas_telegraph_nodes(store: BookmarksStore) -> list[dict[str, Any]]:
    """Формирует узлы отдельной страницы Telegraph для «💡 Моя база идей для автоматизации»."""
    nodes: list[dict[str, Any]] = []
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    nodes.append(
        {
            "tag": "p",
            "children": [
                {"tag": "strong", "children": ["Всего сохранено идей автоматизации: "]},
                f"{len(store.idea_items)} | ",
                {"tag": "em", "children": [f"Обновлено: {now_str}"]},
            ],
        }
    )
    nodes.append(
        {
            "tag": "blockquote",
            "children": [
                "💡 Чтобы добавить свой комментарий к последней сохранённой идее, "
                "просто напишите любой текст в чат с ботом сразу после нажатия «💡 Сохранить идею»!"
            ],
        }
    )
    nodes.append({"tag": "hr"})

    if not store.idea_items:
        nodes.append(
            {
                "tag": "p",
                "children": [
                    "База идей пока пуста. Нажмите «💡 Сохранить идею» под любой идеей в разделе «Автоматизация» ежедневного дайджеста!"
                ],
            }
        )
        return nodes

    for idx, idea in enumerate(reversed(store.idea_items), start=1):
        nodes.append(
            {
                "tag": "h4",
                "children": [f"{idx}. {idea.title} ({idea.digest_date}, Идея #{idea.idea_index})"],
            }
        )
        if idea.user_note:
            nodes.append(
                {
                    "tag": "blockquote",
                    "children": [
                        {"tag": "strong", "children": ["💬 Моя заметка: "]},
                        idea.user_note,
                    ],
                }
            )
        nodes.append(
            {
                "tag": "p",
                "children": [
                    {"tag": "strong", "children": ["Сектор / Процесс: "]},
                    idea.target_sector_or_process,
                ],
            }
        )
        nodes.append(
            {
                "tag": "p",
                "children": [
                    {"tag": "strong", "children": ["Предлагаемое решение: "]},
                    idea.proposed_solution,
                ],
            }
        )
        nodes.append(
            {
                "tag": "p",
                "children": [
                    {"tag": "strong", "children": ["Ожидаемый бизнес-эффект: "]},
                    idea.expected_impact,
                ],
            }
        )
        nodes.append(
            {
                "tag": "p",
                "children": [
                    {"tag": "em", "children": [f"Основано на: {idea.inspired_by_source}"]},
                ],
            }
        )
        nodes.append({"tag": "hr"})

    return nodes


async def _ensure_telegraph_token(client: httpx.AsyncClient, config: AppConfig, store: BookmarksStore) -> str:
    if not store.telegraph_access_token:
        store.telegraph_access_token = config.telegraph_access_token
    if not store.telegraph_access_token:
        acc_resp = await client.post(
            "https://api.telegra.ph/createAccount",
            data={"short_name": "DailyNewsNotes", "author_name": "Daily News Agent"},
            timeout=15.0,
        )
        acc_data = acc_resp.json()
        if acc_data.get("ok"):
            store.telegraph_access_token = str(acc_data["result"]["access_token"])
    return store.telegraph_access_token


async def publish_or_update_bookmarks_telegraph(config: AppConfig, store: BookmarksStore) -> str:
    """Создаёт или обновляет постоянную страницу «📌 Моя база сохранённых заметок» в Telegraph."""
    async with httpx.AsyncClient() as client:
        token = await _ensure_telegraph_token(client, config, store)
        if not token:
            return store.telegraph_url

        title = f"📌 Моя база сохранённых новостей и заметок ({len(store.items)})"
        nodes = _build_bookmarks_telegraph_nodes(store)

        while len(json.dumps(nodes, ensure_ascii=False).encode("utf-8")) > 55_000 and len(nodes) > 10:
            nodes.pop(-2)

        if store.telegraph_path:
            resp = await client.post(
                "https://api.telegra.ph/editPage",
                data={
                    "access_token": token,
                    "path": store.telegraph_path,
                    "title": title,
                    "author_name": "Daily News Agent",
                    "content": json.dumps(nodes, ensure_ascii=False),
                    "return_content": "false",
                },
                timeout=20.0,
            )
            data = resp.json()
            if data.get("ok") and data.get("result", {}).get("url"):
                store.telegraph_url = str(data["result"]["url"])
                save_bookmarks_store(store)
                return store.telegraph_url

        resp = await client.post(
            "https://api.telegra.ph/createPage",
            data={
                "access_token": token,
                "title": title,
                "author_name": "Daily News Agent",
                "content": json.dumps(nodes, ensure_ascii=False),
                "return_content": "false",
            },
            timeout=20.0,
        )
        data = resp.json()
        if data.get("ok") and data.get("result", {}).get("url"):
            store.telegraph_url = str(data["result"]["url"])
            store.telegraph_path = str(data["result"].get("path") or "")
            save_bookmarks_store(store)
            return store.telegraph_url

    return store.telegraph_url


async def publish_or_update_ideas_telegraph(config: AppConfig, store: BookmarksStore) -> str:
    """Создаёт или обновляет постоянную страницу «💡 Моя база идей для автоматизации» в Telegraph."""
    async with httpx.AsyncClient() as client:
        token = await _ensure_telegraph_token(client, config, store)
        if not token:
            return store.ideas_telegraph_url

        title = f"💡 Моя база идей для автоматизации ({len(store.idea_items)})"
        nodes = _build_ideas_telegraph_nodes(store)

        while len(json.dumps(nodes, ensure_ascii=False).encode("utf-8")) > 55_000 and len(nodes) > 10:
            nodes.pop(-2)

        if store.ideas_telegraph_path:
            resp = await client.post(
                "https://api.telegra.ph/editPage",
                data={
                    "access_token": token,
                    "path": store.ideas_telegraph_path,
                    "title": title,
                    "author_name": "Daily News Agent",
                    "content": json.dumps(nodes, ensure_ascii=False),
                    "return_content": "false",
                },
                timeout=20.0,
            )
            data = resp.json()
            if data.get("ok") and data.get("result", {}).get("url"):
                store.ideas_telegraph_url = str(data["result"]["url"])
                save_bookmarks_store(store)
                return store.ideas_telegraph_url

        resp = await client.post(
            "https://api.telegra.ph/createPage",
            data={
                "access_token": token,
                "title": title,
                "author_name": "Daily News Agent",
                "content": json.dumps(nodes, ensure_ascii=False),
                "return_content": "false",
            },
            timeout=20.0,
        )
        data = resp.json()
        if data.get("ok") and data.get("result", {}).get("url"):
            store.ideas_telegraph_url = str(data["result"]["url"])
            store.ideas_telegraph_path = str(data["result"].get("path") or "")
            save_bookmarks_store(store)
            return store.ideas_telegraph_url

    return store.ideas_telegraph_url


async def _send_bot_reply(config: AppConfig, chat_id: str | int, html_text: str) -> None:
    if not config.tg_bot_token:
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{config.tg_bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15.0,
        )


async def sync_telegram_bookmarks_once(config: AppConfig) -> int:
    """
    Проверяет очередь сообщений бота (getUpdates) на нажатия кнопок:
      - «📌 Сохранить сюжет» (/start save_YYYYMMDD_slug_idx) -> База Заметок
      - «💡 Сохранить идею» (/start saveidea_YYYYMMDD_slug_idx) -> База Идей Автоматизации
      - и текстовые заметки пользователя.
    """
    if not config.tg_bot_token:
        return 0

    store = load_bookmarks_store()
    await get_or_fetch_bot_username(config, store)

    params: dict[str, Any] = {"timeout": 1}
    if store.last_update_id > 0:
        params["offset"] = store.last_update_id + 1

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{config.tg_bot_token}/getUpdates",
                params=params,
                timeout=15.0,
            )
            data = resp.json()
    except Exception as exc:
        logger.warning("Ошибка чтения getUpdates для заметок: %s", exc)
        return 0

    if not data.get("ok") or not data.get("result"):
        if store.items and not store.telegraph_url:
            await publish_or_update_bookmarks_telegraph(config, store)
        if store.idea_items and not store.ideas_telegraph_url:
            await publish_or_update_ideas_telegraph(config, store)
        return 0

    processed_count = 0
    changed = False

    for upd in data["result"]:
        upd_id = int(upd.get("update_id", 0))
        if upd_id > store.last_update_id:
            store.last_update_id = upd_id

        msg = upd.get("message") or {}
        text = str(msg.get("text") or "").strip()
        chat_id = msg.get("chat", {}).get("id") or config.tg_chat_id
        if not text or not chat_id:
            continue

        # 1a. Нажатие кнопки «💡 Сохранить идею в Базу Идей» из Telegraph: /start saveidea_20261029_avtomatizatsiya_1
        m_save_idea = re.match(r"^/start\s+saveidea_(\d{8})_([a-z0-9_]+)_(\d+)$", text, flags=re.IGNORECASE)
        if m_save_idea:
            date_compact, cat_slug, idx_str = m_save_idea.group(1), m_save_idea.group(2).lower(), int(m_save_idea.group(3))
            idea_id = f"{date_compact}_{cat_slug}_{idx_str}"
            existing_idea = next((x for x in store.idea_items if x.idea_id == idea_id), None)

            if existing_idea:
                store.last_saved_type = "idea"
                ideas_url = await publish_or_update_ideas_telegraph(config, store)
                await _send_bot_reply(
                    config,
                    chat_id,
                    (
                        f"💡 <b>Эта идея уже есть в вашей Базе Идей:</b>\n"
                        f"«{html.escape(existing_idea.title)}» ({html.escape(existing_idea.target_sector_or_process)})\n\n"
                        f"💬 <i>Отправьте ответным сообщением любой текст, чтобы добавить или обновить свой комментарий к этой идее!</i>\n\n"
                        f"⚡ <b><a href=\"{html.escape(ideas_url, quote=True)}\">ОТКРЫТЬ БАЗУ ИДЕЙ (Instant View)</a></b>"
                    ),
                )
                processed_count += 1
                continue

            found_idea = _find_idea_in_saved_digests(config, date_compact, cat_slug, idx_str)
            if found_idea:
                store.idea_items.append(found_idea)
                store.last_saved_type = "idea"
                changed = True
                processed_count += 1
                ideas_url = await publish_or_update_ideas_telegraph(config, store)
                await _send_bot_reply(
                    config,
                    chat_id,
                    (
                        f"✅ <b>Идея сохранена в Базу Идей Автоматизации (#{len(store.idea_items)})!</b>\n"
                        f"💡 <b>{html.escape(found_idea.title)}</b> ({html.escape(found_idea.target_sector_or_process)})\n\n"
                        f"💬 <i>Хотите добавить свою заметку к этой идее? Просто напишите её следующим сообщением сюда в чат!</i>\n\n"
                        f"⚡ <b><a href=\"{html.escape(ideas_url, quote=True)}\">ОТКРЫТЬ БАЗУ ИДЕЙ ({len(store.idea_items)} шт. — Instant View)</a></b>"
                    ),
                )
            else:
                await _send_bot_reply(
                    config,
                    chat_id,
                    f"⚠️ Не удалось найти идею `{html.escape(idea_id)}` в локальном архиве дайджестов.",
                )
            continue

        # 1b. Нажатие кнопки «📌 Сохранить сюжет в Базу Заметок» из Telegraph: /start save_20261023_it_9
        m_save = re.match(r"^/start\s+save_(\d{8})_([a-z0-9_]+)_(\d+)$", text, flags=re.IGNORECASE)
        if m_save:
            date_compact, cat_slug, idx_str = m_save.group(1), m_save.group(2).lower(), int(m_save.group(3))
            bookmark_id = f"{date_compact}_{cat_slug}_{idx_str}"
            existing = next((x for x in store.items if x.bookmark_id == bookmark_id), None)

            if existing:
                store.last_saved_type = "news"
                kb_url = await publish_or_update_bookmarks_telegraph(config, store)
                await _send_bot_reply(
                    config,
                    chat_id,
                    (
                        f"📌 <b>Этот сюжет уже есть в вашей Базе Заметок:</b>\n"
                        f"«{html.escape(existing.headline)}» ({html.escape(existing.category_title)} #{existing.topic_index})\n\n"
                        f"💬 <i>Отправьте ответным сообщением любой текст, чтобы добавить или обновить свою заметку к этому сюжету!</i>\n\n"
                        f"⚡ <b><a href=\"{html.escape(kb_url, quote=True)}\">ОТКРЫТЬ БАЗУ ЗАМЕТОК (Instant View)</a></b>"
                    ),
                )
                processed_count += 1
                continue

            found = _find_story_in_saved_digests(config, date_compact, cat_slug, idx_str)
            if found:
                store.items.append(found)
                store.last_saved_type = "news"
                changed = True
                processed_count += 1
                kb_url = await publish_or_update_bookmarks_telegraph(config, store)
                await _send_bot_reply(
                    config,
                    chat_id,
                    (
                        f"✅ <b>Сохранено в вашу Базу Заметок (#{len(store.items)})!</b>\n"
                        f"📂 <b>{html.escape(found.category_title)} #{found.topic_index}:</b> {html.escape(found.headline)}\n\n"
                        f"💬 <i>Хотите добавить личную заметку к этой новости? Просто напишите её следующим сообщением сюда в чат!</i>\n\n"
                        f"⚡ <b><a href=\"{html.escape(kb_url, quote=True)}\">ОТКРЫТЬ БАЗУ ЗАМЕТОК ({len(store.items)} шт. — Instant View)</a></b>"
                    ),
                )
            else:
                await _send_bot_reply(
                    config,
                    chat_id,
                    f"⚠️ Не удалось найти сюжет `{html.escape(bookmark_id)}` в локальном архиве дайджестов.",
                )
            continue

        # 2. Команды просмотра базы заметок (/bookmarks) и базы идей (/ideas)
        if text.lower() in {"/bookmarks", "/saved", "/notes", "заметки", "база"}:
            kb_url = await publish_or_update_bookmarks_telegraph(config, store)
            ideas_url = await publish_or_update_ideas_telegraph(config, store)
            await _send_bot_reply(
                config,
                chat_id,
                (
                    f"📌 <b>Ваша База Заметок</b> (сюжетов: <b>{len(store.items)}</b>) и "
                    f"<b>База Идей</b> (идей: <b>{len(store.idea_items)}</b>)\n\n"
                    f"📌 <b><a href=\"{html.escape(kb_url, quote=True)}\">ОТКРЫТЬ БАЗУ НОВОСТЕЙ (Instant View)</a></b>\n"
                    f"💡 <b><a href=\"{html.escape(ideas_url, quote=True)}\">ОТКРЫТЬ БАЗУ ИДЕЙ АВТОМАТИЗАЦИИ (Instant View)</a></b>"
                ),
            )
            processed_count += 1
            continue

        if text.lower() in {"/ideas", "идеи"}:
            ideas_url = await publish_or_update_ideas_telegraph(config, store)
            await _send_bot_reply(
                config,
                chat_id,
                (
                    f"💡 <b>Ваша База Идей для Автоматизации</b> (сохранено: <b>{len(store.idea_items)}</b>)\n\n"
                    f"⚡ <b><a href=\"{html.escape(ideas_url, quote=True)}\">ОТКРЫТЬ БАЗУ ИДЕЙ (Instant View)</a></b>"
                ),
            )
            processed_count += 1
            continue

        # 2b. Команды очистки (/clear и /clear_ideas)
        if text.lower() in {"/clear", "/clear_bookmarks", "очистить базу"}:
            store.items = []
            changed = True
            processed_count += 1
            kb_url = await publish_or_update_bookmarks_telegraph(config, store)
            await _send_bot_reply(
                config,
                chat_id,
                (
                    "🗑 <b>База заметок очищена (0 сюжетов).</b>\n\n"
                    f"⚡ <b><a href=\"{html.escape(kb_url, quote=True)}\">ОТКРЫТЬ БАЗУ ЗАМЕТОК (Instant View)</a></b>"
                ),
            )
            continue

        if text.lower() in {"/clear_ideas", "очистить идеи"}:
            store.idea_items = []
            changed = True
            processed_count += 1
            ideas_url = await publish_or_update_ideas_telegraph(config, store)
            await _send_bot_reply(
                config,
                chat_id,
                (
                    "🗑 <b>База идей очищена (0 идей).</b>\n\n"
                    f"⚡ <b><a href=\"{html.escape(ideas_url, quote=True)}\">ОТКРЫТЬ БАЗУ ИДЕЙ (Instant View)</a></b>"
                ),
            )
            continue

        # 3. Ручное сохранение по команде вида: "save it 9 Моя заметка"
        m_manual = re.match(
            r"^(?:/save|save|сохранить)\s+([a-z_]+)\s+(\d+)(?:\s+(.+))?$",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m_manual:
            cat_slug = m_manual.group(1).lower()
            idx_num = int(m_manual.group(2))
            note_txt = (m_manual.group(3) or "").strip()
            today_compact = datetime.now(timezone.utc).strftime("%Y%m%d")
            found = _find_story_in_saved_digests(config, today_compact, cat_slug, idx_num)
            if found:
                if note_txt:
                    found.user_note = note_txt
                store.items = [x for x in store.items if x.bookmark_id != found.bookmark_id]
                store.items.append(found)
                store.last_saved_type = "news"
                changed = True
                processed_count += 1
                kb_url = await publish_or_update_bookmarks_telegraph(config, store)
                await _send_bot_reply(
                    config,
                    chat_id,
                    (
                        f"✅ <b>Сохранено в Базу Заметок!</b>\n"
                        f"📂 <b>{html.escape(found.category_title)} #{found.topic_index}:</b> {html.escape(found.headline)}\n"
                        + (f"💬 <b>Заметка:</b> {html.escape(note_txt)}\n\n" if note_txt else "\n")
                        + f"⚡ <b><a href=\"{html.escape(kb_url, quote=True)}\">ОТКРЫТЬ БАЗУ ЗАМЕТОК (Instant View)</a></b>"
                    ),
                )
            continue

        # 4. Любой обычный текст (не /start) — прикрепляется как заметка к последнему сохранённому элементу (новости или идее!)
        if not text.startswith("/"):
            if store.last_saved_type == "idea" and store.idea_items:
                last_idea = store.idea_items[-1]
                last_idea.user_note = text
                changed = True
                processed_count += 1
                ideas_url = await publish_or_update_ideas_telegraph(config, store)
                await _send_bot_reply(
                    config,
                    chat_id,
                    (
                        f"💬 <b>Заметка добавлена к идее «{html.escape(last_idea.title)}»:</b>\n"
                        f"<i>«{html.escape(text)}»</i>\n\n"
                        f"⚡ <b><a href=\"{html.escape(ideas_url, quote=True)}\">ОТКРЫТЬ ОБНОВЛЁННУЮ БАЗУ ИДЕЙ (Instant View)</a></b>"
                    ),
                )
            elif store.items:
                last_item = store.items[-1]
                last_item.user_note = text
                changed = True
                processed_count += 1
                kb_url = await publish_or_update_bookmarks_telegraph(config, store)
                await _send_bot_reply(
                    config,
                    chat_id,
                    (
                        f"💬 <b>Заметка добавлена к сюжету «{html.escape(last_item.headline)}»:</b>\n"
                        f"<i>«{html.escape(text)}»</i>\n\n"
                        f"⚡ <b><a href=\"{html.escape(kb_url, quote=True)}\">ОТКРЫТЬ ОБНОВЛЁННУЮ БАЗУ ЗАМЕТОК (Instant View)</a></b>"
                    ),
                )

    save_bookmarks_store(store)
    if changed:
        logger.info(
            "База обновлена (обработано действий: %d | сюжетов: %d | идей: %d)",
            processed_count,
            len(store.items),
            len(store.idea_items),
        )
    return processed_count


def _git_push_bookmarks_if_in_ci() -> None:
    """Если запущено в GitHub Actions — сразу сохраняет обновлённый data/saved_bookmarks.json в git."""
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT_DIR, check=False)
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            cwd=ROOT_DIR,
            check=False,
        )
        subprocess.run(["git", "add", "data/saved_bookmarks.json"], cwd=ROOT_DIR, check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT_DIR)
        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "chore: save bookmark in real-time [skip ci]"],
                cwd=ROOT_DIR,
                check=False,
            )
            branch = os.getenv("GITHUB_REF_NAME", "main")
            subprocess.run(["git", "pull", "--rebase", "origin", branch], cwd=ROOT_DIR, check=False)
            subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], cwd=ROOT_DIR, check=False)
            logger.info("data/saved_bookmarks.json автоматически сохранён в GitHub репозиторий.")
    except Exception as exc:
        logger.warning("Не удалось отправить закладки в git: %s", exc)


async def listen_telegram_bookmarks_loop(config: AppConfig, max_minutes: int = 0) -> None:
    """
    Фоновый режим реального времени (0 токенов LLM, $0.00):
    мгновенно обрабатывает нажатия кнопок «📌 Сохранить сюжет» и «💡 Сохранить идею» из Telegraph.
    Если задан max_minutes > 0 (для GitHub Actions), работает указанное время и штатно завершается.
    """
    logger.info(
        "Запущен 24/7 обработчик заметок и идей (--listen-bot, max_minutes=%d).",
        max_minutes,
    )
    start_ts = time.monotonic()
    while True:
        try:
            count = await sync_telegram_bookmarks_once(config)
            if count > 0:
                _git_push_bookmarks_if_in_ci()
        except Exception as exc:
            logger.warning("Ошибка в цикле заметок: %s", exc)

        if max_minutes > 0 and (time.monotonic() - start_ts) >= (max_minutes * 60):
            logger.info("Достигнут лимит времени сессии (%d мин), штатное завершение.", max_minutes)
            break
        await asyncio.sleep(2.0)
