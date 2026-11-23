from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup, Tag

from src.config import AppConfig
from src.models import RawItem, SourceRef

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".mp4", ".mp3", ".pdf", ".zip", ".rar", ".svg", ".ico",
)

SKIP_HOSTS = (
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com",
    "facebook.com", "vk.com", "whatsapp.com", "tgstat.ru",
    "tgstat.com", "telega.in", "x.com", "twitter.com",
)

KAZAKH_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")


# ==============================================================================
# 1. Подгрузка статей по ссылкам (глубина 1, параллельно, с кэшем URL)
# ==============================================================================


def _unwrap_url(raw_url: str) -> str:
    """Распаковывает ссылки вида https://t.me/iv?url=... в прямую ссылку на статью."""
    url = raw_url.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.netloc.lower() in {"t.me", "telegram.me"} and parsed.path == "/iv":
        qs = parse_qs(parsed.query)
        if "url" in qs and qs["url"]:
            return qs["url"][0].strip()
    return url


def _canonicalize_url(raw_url: str) -> str:
    """Убирает UTM-метки и якоря для выявления одинаковых статей из разных каналов."""
    url = _unwrap_url(raw_url)
    if not url:
        return ""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    clean_qs = {
        k: v
        for k, v in qs.items()
        if not k.lower().startswith(("utm_", "fbclid", "gclid", "yclid", "ref", "from"))
    }
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", urlencode(clean_qs, doseq=True), ""))


def _is_hydratable_article_url(url: str) -> bool:
    """Проверяет, ведёт ли ссылка на читаемую статью или вложенный пост."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return False
    if any(skip in host for skip in SKIP_HOSTS):
        return False

    if host in {"t.me", "telegram.me"}:
        parts = [p for p in parsed.path.split("/") if p]
        return (len(parts) == 2 and parts[0] != "s" and parts[1].isdigit()) or (
            len(parts) == 3 and parts[0] == "s" and parts[2].isdigit()
        )

    return path not in {"", "/", "/ru", "/kz", "/en"}


def _extract_article_text_from_html(html_text: str, base_url: str) -> str:
    """Извлекает чистый текст статьи без меню и скриптов."""
    soup = BeautifulSoup(html_text, "lxml")
    host = urlparse(base_url).netloc.lower()

    # 1. Вложенный пост Telegram (t.me/channel/12345?embed=1)
    if host in {"t.me", "telegram.me"}:
        msg_text_el = soup.select_one(".tgme_widget_message_text")
        if msg_text_el:
            text, _ = _extract_message_text_and_links(msg_text_el)
            return text[:3000]
        return ""

    # 2. Статья в Telegraph (telegra.ph)
    if "telegra.ph" in host:
        article_node = soup.select_one("article.tl_article_content") or soup.find("article") or soup.body
        if not article_node:
            return ""
        title_el = soup.find("h1")
        title_str = title_el.get_text(" ", strip=True) if title_el else ""
        blocks = [
            el.get_text(" ", strip=True)
            for el in article_node.find_all(["h3", "h4", "p", "li", "blockquote"])
            if el.get_text(" ", strip=True)
        ]
        body = "\n".join(blocks)
        return (f"{title_str}\n{body}".strip() if title_str else body.strip())[:4500]

    # 3. Обычная веб-страница / статья
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "form", "svg", "iframe"]):
        tag.decompose()

    title_el = soup.find("h1") or soup.find("title")
    title_str = title_el.get_text(" ", strip=True) if title_el else ""

    main_container = (
        soup.find("article")
        or soup.find("main")
        or soup.select_one(".tm-article-body, .article-body, .post-content, .entry-content, .article__text, .news-text")
        or soup.body
    )
    if not main_container:
        return ""

    paragraphs: list[str] = []
    seen_p: set[str] = set()
    for el in main_container.find_all(["h2", "h3", "p", "li", "blockquote"]):
        txt = el.get_text(" ", strip=True)
        if len(txt) >= 45 and txt not in seen_p:
            seen_p.add(txt)
            paragraphs.append(txt)

    if not paragraphs:
        return ""

    body = "\n".join(paragraphs)[:3200].strip()
    return f"Заголовок статьи: {title_str}\n{body}" if title_str else body


async def _fetch_article_cached(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    inflight_cache: dict[str, asyncio.Task[str]],
    url: str,
) -> tuple[str, str]:
    """Загружает URL максимум один раз за весь запуск."""
    clean_url = _unwrap_url(url)
    canon = _canonicalize_url(clean_url)
    if not clean_url or not canon or not _is_hydratable_article_url(clean_url):
        return clean_url, ""

    async def _do_fetch() -> str:
        fetch_url = clean_url
        parsed = urlparse(clean_url)
        if parsed.netloc.lower() in {"t.me", "telegram.me"} and "embed=1" not in parsed.query:
            fetch_url = f"{clean_url}?embed=1"
        async with sem:
            try:
                resp = await client.get(fetch_url, follow_redirects=True, timeout=8.0)
                ctype = resp.headers.get("content-type", "").lower()
                if resp.status_code == 200 and ("text/html" in ctype or "application/xhtml" in ctype):
                    return _extract_article_text_from_html(resp.text, str(resp.url))
            except Exception:
                return ""
        return ""

    task = inflight_cache.get(canon)
    if task is None:
        task = asyncio.create_task(_do_fetch())
        inflight_cache[canon] = task

    try:
        return clean_url, await task
    except Exception:
        return clean_url, ""


# ==============================================================================
# 2. Простая дедупликация до LLM (без словарей стоп-слов)
# ==============================================================================


def _is_kazakh_text(text: str) -> bool:
    """Определяет казахский текст по доле специфических казахских букв (әғқңөұүһі) без хардкода слов."""
    sample = text.split("[Полный текст вложенной статьи", 1)[0][:1000]
    letters = [ch for ch in sample if ch.isalpha()]
    if len(letters) < 30:
        return False
    kz_count = sum(1 for ch in letters if ch in KAZAKH_LETTERS)
    return kz_count >= 6 and (kz_count / len(letters)) >= 0.035


def _extract_token_set(text: str) -> set[str]:
    body = text.split("[Полный текст вложенной статьи", 1)[0]
    body = re.sub(r"https?://\S+", " ", body.lower())
    return {w for w in re.findall(r"[a-zа-яёәғқңөұүһі0-9]{4,}", body) if not w.isdigit()}


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def _overlap_coefficient(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _normalized_prefix(text: str, length: int = 90) -> str:
    body = text.split("[Полный текст вложенной статьи", 1)[0]
    body = re.sub(r"https?://\S+", " ", body.lower())
    return re.sub(r"[^a-zа-яёәғқңөұүһі0-9]+", " ", body).strip()[:length]


def _merge_source_into(keeper: RawItem, dup: RawItem) -> None:
    """Сохраняет ссылку дубля в keeper.extra_sources без потери первоисточников."""
    existing_urls = {keeper.source_url} | {s.url for s in keeper.extra_sources}
    if dup.source_url not in existing_urls:
        keeper.extra_sources.append(
            SourceRef(label=f"{dup.source_name} ({dup.source_title})", url=dup.source_url)
        )
        existing_urls.add(dup.source_url)
    for src in dup.extra_sources:
        if src.url not in existing_urls:
            keeper.extra_sources.append(src)
            existing_urls.add(src.url)
    if dup.is_priority and not keeper.is_priority:
        keeper.is_priority = True
        keeper.priority_default_category = dup.priority_default_category


def _deduplicate_same_channel(items: list[RawItem]) -> list[RawItem]:
    """Убирает двойные посты и KZ/RU пары-близнецы внутри одного канала (сохраняя KZ-only посты!)."""
    if len(items) <= 1:
        return items

    sorted_items = sorted(items, key=lambda x: x.published_at)
    kept: list[RawItem] = []

    for item in sorted_items:
        item_tokens = _extract_token_set(item.text)
        item_is_kz = _is_kazakh_text(item.text)
        item_body_len = len(item.text.split("[Полный текст вложенной статьи", 1)[0])
        item_prefix = _normalized_prefix(item.text)
        merged = False

        for idx in range(len(kept) - 1, max(-1, len(kept) - 6), -1):
            prev = kept[idx]
            dt_sec = abs((item.published_at - prev.published_at).total_seconds())
            if dt_sec > 900:
                continue

            prev_tokens = _extract_token_set(prev.text)
            same_headline = len(item_prefix) >= 45 and item_prefix == _normalized_prefix(prev.text)

            if (
                _jaccard_similarity(item_tokens, prev_tokens) >= 0.60
                or _overlap_coefficient(item_tokens, prev_tokens) >= 0.80
                or same_headline
            ):
                if len(item.text) > len(prev.text):
                    _merge_source_into(item, prev)
                    kept[idx] = item
                else:
                    _merge_source_into(prev, item)
                merged = True
                break

            # Двуязычная пара KZ + RU в пределах 3 минут сопоставимой длины -> оставляем русскую версию
            prev_is_kz = _is_kazakh_text(prev.text)
            if dt_sec <= 180 and (item_is_kz != prev_is_kz):
                prev_body_len = len(prev.text.split("[Полный текст вложенной статьи", 1)[0])
                if min(item_body_len, prev_body_len) / max(item_body_len, prev_body_len, 1) >= 0.55:
                    if prev_is_kz and not item_is_kz:
                        _merge_source_into(item, prev)
                        kept[idx] = item
                    else:
                        _merge_source_into(prev, item)
                    merged = True
                    break

        if not merged:
            kept.append(item)

    return kept


def deduplicate_raw_items(items: list[RawItem]) -> list[RawItem]:
    """Объединяет репосты одной статьи/пресс-релиза между каналами ДО отправки в LLM."""
    if len(items) <= 1:
        return items

    kept: list[RawItem] = []
    kept_tokens: list[set[str]] = []
    url_to_idx: dict[str, int] = {}

    ordered = sorted(
        items,
        key=lambda x: (not x.is_priority, _is_kazakh_text(x.text), -len(x.text)),
    )

    for item in ordered:
        matched_idx: int | None = None
        canon_urls = [_canonicalize_url(u) for u in item.external_links if _is_hydratable_article_url(u)]
        for cu in canon_urls:
            if cu and cu in url_to_idx:
                matched_idx = url_to_idx[cu]
                break

        item_tokens = _extract_token_set(item.text)
        if matched_idx is None and len(item_tokens) >= 18:
            for idx, prev_tokens in enumerate(kept_tokens):
                if len(prev_tokens) >= 18 and _jaccard_similarity(item_tokens, prev_tokens) >= 0.68:
                    matched_idx = idx
                    break

        if matched_idx is not None:
            _merge_source_into(kept[matched_idx], item)
            for cu in canon_urls:
                if cu:
                    url_to_idx.setdefault(cu, matched_idx)
        else:
            new_idx = len(kept)
            kept.append(item)
            kept_tokens.append(item_tokens)
            for cu in canon_urls:
                if cu:
                    url_to_idx.setdefault(cu, new_idx)

    kept.sort(key=lambda x: x.published_at)
    return kept


# ==============================================================================
# 3. Сбор постов за последние 24 часа из публичных Telegram-каналов
# ==============================================================================


async def _resolve_channel_username(raw: str) -> str:
    cleaned = raw.strip().rstrip("/")
    if re.search(r"t\.me/\+|t\.me/joinchat/", cleaned, flags=re.IGNORECASE):
        logger.info("Пропускаем закрытый invite-канал: %s", raw)
        return ""
    cleaned = re.sub(r"^https?://(www\.)?(t\.me|telegram\.me)/(s/)?", "", cleaned, flags=re.IGNORECASE)
    return cleaned.lstrip("@").split("?")[0].split("/")[0].strip()


def _extract_message_text_and_links(text_div: Tag) -> tuple[str, list[str]]:
    extracted_links: list[str] = []
    soup_copy = BeautifulSoup(str(text_div), "lxml")

    for br in soup_copy.find_all("br"):
        br.replace_with("\n")

    for a_tag in soup_copy.find_all("a", href=True):
        href = _unwrap_url(str(a_tag["href"]).strip())
        if not href or href.startswith("tg://") or "?q=%23" in href:
            continue
        if _is_hydratable_article_url(href) and href not in extracted_links:
            extracted_links.append(href)

        anchor_text = a_tag.get_text(strip=True)
        if anchor_text and anchor_text != href and not anchor_text.startswith("#"):
            a_tag.replace_with(f"{anchor_text} ({href})")
        else:
            a_tag.replace_with(href)

    lines = [line.strip() for line in soup_copy.get_text().splitlines()]
    return "\n".join(line for line in lines if line).strip(), extracted_links


def _extract_preview_and_reply_context(msg_node: Tag) -> tuple[str, list[str]]:
    extra_parts: list[str] = []
    extra_links: list[str] = []

    reply_el = msg_node.select_one(".tgme_widget_message_reply")
    if reply_el:
        reply_txt = reply_el.get_text(" ", strip=True)
        reply_href = _unwrap_url(str(reply_el.get("href") or "").strip())
        if reply_txt:
            extra_parts.append(f"[Цитата ({reply_href})]: {reply_txt}")
        if reply_href and _is_hydratable_article_url(reply_href):
            extra_links.append(reply_href)

    preview_el = msg_node.select_one(".tgme_widget_message_link_preview")
    if preview_el:
        preview_href = _unwrap_url(str(preview_el.get("href") or "").strip())
        preview_bits = [
            el.get_text(" ", strip=True)
            for el in (
                preview_el.select_one(".link_preview_site_name"),
                preview_el.select_one(".link_preview_title"),
                preview_el.select_one(".link_preview_description"),
            )
            if el and el.get_text(" ", strip=True)
        ]
        if preview_bits:
            extra_parts.append(f"[Превью ({preview_href})]: " + " — ".join(preview_bits))
        if preview_href and _is_hydratable_article_url(preview_href) and preview_href not in extra_links:
            extra_links.append(preview_href)

    return "\n".join(extra_parts), extra_links


async def _fetch_single_channel(
    client: httpx.AsyncClient,
    channel_sem: asyncio.Semaphore,
    article_sem: asyncio.Semaphore,
    inflight_cache: dict[str, asyncio.Task[str]],
    channel_raw: str,
    cutoff_utc: datetime,
    config: AppConfig,
) -> list[RawItem]:
    async with channel_sem:
        username = await _resolve_channel_username(channel_raw)
        if not username:
            return []

        priority_default = config.telegram.get_priority_default_category(username)
        is_priority = priority_default is not None

        raw_posts: list[RawItem] = []
        seen_post_ids: set[str] = set()
        before_id: int | None = None

        while True:
            url = f"https://t.me/s/{username}"
            if before_id is not None:
                url = f"{url}?before={before_id}"

            try:
                resp = await client.get(url, follow_redirects=True, timeout=15.0)
                if resp.status_code != 200:
                    logger.warning("Не удалось загрузить канал @%s (HTTP %s)", username, resp.status_code)
                    break
            except Exception as exc:
                logger.warning("Ошибка при чтении канала @%s: %s", username, exc)
                break

            soup = BeautifulSoup(resp.text, "lxml")
            channel_title_el = soup.select_one(".tgme_channel_info_header_title")
            channel_title = channel_title_el.get_text(strip=True) if channel_title_el else f"@{username}"

            message_nodes = soup.select(".tgme_widget_message")
            if not message_nodes:
                break

            page_post_nums: list[int] = []
            oldest_on_page: datetime | None = None

            for msg in message_nodes:
                data_post = str(msg.get("data-post") or "").strip()
                if not data_post or "/" not in data_post or data_post in seen_post_ids:
                    continue
                seen_post_ids.add(data_post)

                post_num_str = data_post.split("/")[-1]
                if post_num_str.isdigit():
                    page_post_nums.append(int(post_num_str))

                time_el = msg.select_one("time[datetime]")
                if not time_el or not time_el.get("datetime"):
                    continue

                try:
                    dt = datetime.fromisoformat(str(time_el["datetime"]))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                if oldest_on_page is None or dt < oldest_on_page:
                    oldest_on_page = dt
                if dt < cutoff_utc:
                    continue

                text_div = msg.select_one(".tgme_widget_message_text")
                text, external_links = _extract_message_text_and_links(text_div) if text_div else ("", [])
                preview_text, preview_links = _extract_preview_and_reply_context(msg)

                for pl in preview_links:
                    if pl not in external_links:
                        external_links.append(pl)

                full_post_text = "\n\n".join(part for part in (text, preview_text) if part).strip()
                if not full_post_text or (len(full_post_text) < 55 and not external_links):
                    continue

                raw_posts.append(
                    RawItem(
                        item_id=f"tg_{data_post.replace('/', '_')}",
                        source_type="telegram",
                        source_name=f"@{username} ({channel_title})",
                        source_title=f"Пост #{post_num_str} в @{username}",
                        source_url=f"https://t.me/{data_post}",
                        published_at=dt,
                        text=full_post_text,
                        external_links=external_links,
                        is_priority=is_priority,
                        priority_default_category=priority_default or "",
                    )
                )

            if not page_post_nums or (oldest_on_page and oldest_on_page < cutoff_utc):
                break
            new_before_id = min(page_post_nums)
            if before_id is not None and new_before_id >= before_id:
                break
            before_id = new_before_id

        if config.telegram.deduplicate_before_llm and len(raw_posts) > 1:
            raw_posts = _deduplicate_same_channel(raw_posts)

        if config.telegram.read_nested_articles:
            for item in raw_posts:
                if not item.external_links:
                    continue
                # Подгружаем статьи для приоритетных каналов, коротких постов (<1600 символов) и ссылок telegra.ph
                if item.is_priority or len(item.text) < 1600:
                    links_to_fetch = item.external_links[:2]
                else:
                    links_to_fetch = [u for u in item.external_links if "telegra.ph" in urlparse(u).netloc.lower()][:2]

                if not links_to_fetch:
                    continue

                fetched = await asyncio.gather(
                    *(_fetch_article_cached(client, article_sem, inflight_cache, u) for u in links_to_fetch)
                )
                hydrated_blocks = [
                    f"[Полный текст вложенной статьи ({art_url})]:\n{art_text}"
                    for art_url, art_text in fetched
                    if art_text
                ]
                if hydrated_blocks:
                    item.text = (item.text + "\n\n" + "\n\n".join(hydrated_blocks))[:9000]

        raw_posts.sort(key=lambda x: x.published_at)
        logger.info("Канал @%s: собрано %d уникальных постов за последние 24ч", username, len(raw_posts))
        return raw_posts


async def collect_telegram_posts(config: AppConfig, cutoff_utc: datetime) -> list[RawItem]:
    """Собирает все посты за последние 24 часа из каналов config.yaml с дедупликацией."""
    if not config.telegram.enabled or not config.telegram.channels:
        return []

    unique_channels: list[str] = []
    seen_ch: set[str] = set()
    for ch in config.telegram.channels:
        key = ch.strip().lower().rstrip("/")
        if key and key not in seen_ch:
            seen_ch.add(key)
            unique_channels.append(ch.strip())

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    channel_sem = asyncio.Semaphore(12)
    article_sem = asyncio.Semaphore(16)
    inflight_cache: dict[str, asyncio.Task[str]] = {}

    async with httpx.AsyncClient(headers=headers) as client:
        tasks = [
            _fetch_single_channel(
                client=client,
                channel_sem=channel_sem,
                article_sem=article_sem,
                inflight_cache=inflight_cache,
                channel_raw=ch,
                cutoff_utc=cutoff_utc,
                config=config,
            )
            for ch in unique_channels
        ]
        channel_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_items: list[RawItem] = []
    for res in channel_lists:
        if isinstance(res, list):
            all_items.extend(res)
        elif isinstance(res, Exception):
            logger.warning("Ошибка при сборе канала: %s", res)

    if config.telegram.deduplicate_before_llm and len(all_items) > 1:
        before_cross = len(all_items)
        all_items = deduplicate_raw_items(all_items)
        logger.info(
            "Кросс-канальная дедупликация до LLM: %d -> %d уникальных материалов (экономия %.0f%%)",
            before_cross,
            len(all_items),
            (1 - len(all_items) / max(1, before_cross)) * 100,
        )

    return all_items


# ==============================================================================
# 4. Сбор писем из Gmail через стандартный IMAP за последние 24 часа
# ==============================================================================


def _decode_mime_header(raw_header: str | None) -> str:
    if not raw_header:
        return ""
    decoded_chunks: list[str] = []
    for content, charset in decode_header(raw_header):
        if isinstance(content, bytes):
            enc = charset or "utf-8"
            try:
                decoded_chunks.append(content.decode(enc, errors="replace"))
            except LookupError:
                decoded_chunks.append(content.decode("utf-8", errors="replace"))
        else:
            decoded_chunks.append(str(content))
    return "".join(decoded_chunks).strip()


def _html_to_clean_text(html_content: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html_content, "lxml")
    for tag in soup(["script", "style", "head", "meta", "noscript", "svg"]):
        tag.decompose()

    links: list[str] = []
    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag["href"]).strip()
        if href.startswith("http") and "unsubscribe" not in href.lower():
            if href not in links:
                links.append(href)
            label = a_tag.get_text(" ", strip=True)
            if label and label != href:
                a_tag.replace_with(f"{label} ({href})")

    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)[:10000], links[:20]


def _extract_email_body(msg: email.message.Message) -> tuple[str, list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if msg.is_multipart() and "attachment" in str(part.get("Content-Disposition") or "").lower():
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")

        ctype = part.get_content_type()
        if ctype == "text/html":
            html_parts.append(decoded)
        elif ctype == "text/plain":
            plain_parts.append(decoded)

    if html_parts:
        text, links = _html_to_clean_text("\n".join(html_parts))
        if text:
            return text, links

    plain_text = "\n".join(plain_parts).strip()
    return plain_text[:10000], re.findall(r"https?://[^\s<>()\"']+", plain_text)[:20]


def _matches_allowed_senders(sender: str, allowed_senders: Iterable[str]) -> bool:
    allowed = [s.strip().lower() for s in allowed_senders if s.strip()]
    return True if not allowed else any(pattern in sender.lower() for pattern in allowed)


def collect_gmail_emails(config: AppConfig, cutoff_utc: datetime) -> list[RawItem]:
    """Подключается к Gmail по IMAP и забирает все письма за последние 24 часа."""
    if not config.gmail.enabled or not config.gmail_user or not config.gmail_app_password:
        return []

    collected: list[RawItem] = []
    imap_since_date = (cutoff_utc - timedelta(days=1)).strftime("%d-%b-%Y")

    try:
        with imaplib.IMAP4_SSL(config.gmail.imap_host, config.gmail.imap_port) as mail:
            mail.login(config.gmail_user, config.gmail_app_password)
            status, _ = mail.select(f'"{config.gmail.folder}"', readonly=True)
            if status != "OK":
                mail.select("INBOX", readonly=True)

            status, data = mail.search(None, f'(SINCE "{imap_since_date}")')
            if status != "OK" or not data or not data[0]:
                return []

            for num in reversed(data[0].split()):
                status, msg_data = mail.fetch(num, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue

                raw_bytes = next((part[1] for part in msg_data if isinstance(part, tuple) and len(part) >= 2), b"")
                if not raw_bytes:
                    continue

                msg = email.message_from_bytes(raw_bytes)
                date_str = msg.get("Date")
                if not date_str:
                    continue
                try:
                    dt = parsedate_to_datetime(date_str)
                    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
                except Exception:
                    continue

                if dt < cutoff_utc:
                    continue

                sender = _decode_mime_header(msg.get("From"))
                sender_low = sender.lower()
                if any(
                    sys_pat in sender_low
                    for sys_pat in (
                        "mailer-daemon@",
                        "postmaster@",
                        "forwarding-noreply@google.com",
                        "no-reply@accounts.google.com",
                        config.gmail_user.lower(),
                    )
                ):
                    continue
                if not _matches_allowed_senders(sender, config.gmail.allowed_senders):
                    continue

                subject = _decode_mime_header(msg.get("Subject")) or "(Без темы)"
                message_id_hdr = (msg.get("Message-ID") or "").strip().strip("<>")
                body_text, links = _extract_email_body(msg)
                if not body_text or len(body_text.strip()) < 30:
                    continue

                gmail_url = (
                    f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{quote(message_id_hdr)}"
                    if message_id_hdr
                    else f"https://mail.google.com/mail/u/0/#search/{quote(subject[:50])}"
                )
                uid_str = num.decode("utf-8", errors="ignore")
                collected.append(
                    RawItem(
                        item_id=f"email_{uid_str}",
                        source_type="email",
                        source_name=f"Email: {sender}",
                        source_title=f"Письмо «{subject}» от {sender}",
                        source_url=gmail_url,
                        published_at=dt,
                        text=f"Тема письма: {subject}\nОтправитель: {sender}\n\n{body_text}",
                        external_links=links,
                    )
                )

    except Exception as exc:
        logger.warning("Ошибка при сборе писем из Gmail IMAP: %s", exc)

    collected.sort(key=lambda x: x.published_at)
    logger.info("Gmail IMAP: собрано %d писем за последние 24ч", len(collected))
    return collected
