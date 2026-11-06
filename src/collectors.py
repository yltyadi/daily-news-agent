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
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from src.config import AppConfig
from src.models import RawItem

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SKIP_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".mp4",
    ".mp3",
    ".pdf",
    ".zip",
    ".rar",
    ".svg",
    ".ico",
)


# ==============================================================================
# 1. Глубокое чтение вложенных статей (Telegraph, внешние СМИ, вложенные посты TG)
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


def _is_hydratable_article_url(url: str) -> bool:
    """Проверяет, стоит ли переходить по ссылке для чтения полной вложенной статьи."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return False

    if any(
        skip in host
        for skip in (
            "youtube.com",
            "youtu.be",
            "instagram.com",
            "tiktok.com",
            "facebook.com",
            "vk.com",
            "whatsapp.com",
            "tgstat.ru",
            "tgstat.com",
            "telega.in",
        )
    ):
        return False

    if host in {"t.me", "telegram.me"}:
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 2 and parts[0] != "s" and parts[1].isdigit():
            return True
        if len(parts) == 3 and parts[0] == "s" and parts[2].isdigit():
            return True
        return False

    return True


def _extract_article_and_nested_links_from_html(
    html_text: str,
    base_url: str,
) -> tuple[str, list[str]]:
    """
    Извлекает полный текст статьи (с поддержкой telegra.ph, Habr, новостных порталов)
    И возвращает список вложенных ссылок внутри этой статьи.
    """
    soup = BeautifulSoup(html_text, "lxml")
    parsed_base = urlparse(base_url)
    host = parsed_base.netloc.lower()

    nested_links: list[str] = []

    # 1. Вложенный пост Telegram (t.me/channel/12345?embed=1)
    if host in {"t.me", "telegram.me"}:
        msg_text_el = soup.select_one(".tgme_widget_message_text")
        if msg_text_el:
            text, links = _extract_message_text_and_links(msg_text_el)
            return text[:4000], links
        return "", []

    # 2. Статья в Telegraph (telegra.ph)
    if "telegra.ph" in host:
        article_node = soup.select_one("article.tl_article_content") or soup.find("article") or soup.body
        if not article_node:
            return "", []
        title_el = soup.find("h1")
        title_str = title_el.get_text(" ", strip=True) if title_el else ""
        for a_tag in article_node.find_all("a", href=True):
            full_href = _unwrap_url(urljoin(base_url, str(a_tag["href"]).strip()))
            if _is_hydratable_article_url(full_href) and full_href != base_url and full_href not in nested_links:
                nested_links.append(full_href)
        blocks = [
            el.get_text(" ", strip=True)
            for el in article_node.find_all(["h3", "h4", "p", "li", "blockquote"])
            if el.get_text(" ", strip=True)
        ]
        body = "\n".join(blocks)
        full_text = f"{title_str}\n{body}".strip() if title_str else body.strip()
        return full_text[:5000], nested_links

    # 3. Обычный новостной сайт / статья
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "form", "svg", "iframe"]):
        tag.decompose()

    title_el = soup.find("h1") or soup.find("title")
    title_str = title_el.get_text(" ", strip=True) if title_el else ""

    main_container = (
        soup.find("article")
        or soup.find("main")
        or soup.select_one(".tm-article-body, .article-body, .post-content, .entry-content, .article__text")
        or soup.body
    )
    if not main_container:
        return "", []

    for a_tag in main_container.find_all("a", href=True):
        full_href = _unwrap_url(urljoin(base_url, str(a_tag["href"]).strip()))
        if (
            _is_hydratable_article_url(full_href)
            and full_href != base_url
            and urlparse(full_href).netloc.lower() != host
            and full_href not in nested_links
        ):
            nested_links.append(full_href)

    paragraphs: list[str] = []
    seen_p: set[str] = set()
    for el in main_container.find_all(["h2", "h3", "p", "li", "blockquote"]):
        txt = el.get_text(" ", strip=True)
        if len(txt) >= 45 and txt not in seen_p:
            seen_p.add(txt)
            paragraphs.append(txt)

    if not paragraphs:
        return "", []

    body = "\n".join(paragraphs)[:4500].strip()
    full_text = f"Заголовок статьи: {title_str}\n{body}" if title_str else body
    return full_text, nested_links


async def _hydrate_url_recursive(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url_cache: dict[str, str],
    url: str,
    depth: int,
    visited: set[str],
) -> list[tuple[str, str]]:
    """
    Рекурсивно скачивает текст вложенной статьи и вложенных в неё материалов.
    """
    clean_url = _unwrap_url(url)
    if not clean_url or clean_url in visited or not _is_hydratable_article_url(clean_url):
        return []
    visited.add(clean_url)

    fetch_url = clean_url
    parsed = urlparse(clean_url)
    if parsed.netloc.lower() in {"t.me", "telegram.me"} and "embed=1" not in parsed.query:
        fetch_url = f"{clean_url}?embed=1"

    nested_urls: list[str] = []
    if clean_url in url_cache:
        extracted_text = url_cache[clean_url]
    else:
        extracted_text = ""
        async with sem:
            try:
                resp = await client.get(fetch_url, follow_redirects=True, timeout=12.0)
                ctype = resp.headers.get("content-type", "").lower()
                if resp.status_code == 200 and ("text/html" in ctype or "application/xhtml" in ctype):
                    extracted_text, nested_urls = _extract_article_and_nested_links_from_html(
                        resp.text, str(resp.url)
                    )
            except Exception:
                extracted_text = ""
        url_cache[clean_url] = extracted_text

    results: list[tuple[str, str]] = []
    if extracted_text:
        results.append((clean_url, extracted_text))

    # Читаем также вложенные статьи внутри прочитанной статьи/поста
    if depth < 2 and nested_urls:
        for child_url in nested_urls[:3]:
            child_res = await _hydrate_url_recursive(
                client=client,
                sem=sem,
                url_cache=url_cache,
                url=child_url,
                depth=depth + 1,
                visited=visited,
            )
            results.extend(child_res)

    return results


# ==============================================================================
# 2. Сбор ВСЕХ постов за последние 24 часа из публичных Telegram-каналов
# ==============================================================================


async def _resolve_channel_username(client: httpx.AsyncClient, raw: str) -> str:
    """
    Превращает 'https://t.me/finmentorkz' или '@finmentorkz' в 'finmentorkz'.
    Пропускает закрытые invite-ссылки вида 'https://t.me/+...' или 't.me/joinchat/...'.
    """
    cleaned = raw.strip().rstrip("/")
    if re.search(r"t\.me/\+|t\.me/joinchat/", cleaned, flags=re.IGNORECASE):
        logger.info("Пропускаем закрытый invite-канал: %s", raw)
        return ""

    cleaned = re.sub(r"^https?://(www\.)?(t\.me|telegram\.me)/(s/)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.lstrip("@").split("?")[0].split("/")[0]
    return cleaned.strip()


def _extract_message_text_and_links(text_div: Tag) -> tuple[str, list[str]]:
    """Извлекает чистый текст поста, сохраняя встроенные ссылки, и список внешних/вложенных URL."""
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

    raw_text = soup_copy.get_text()
    lines = [line.strip() for line in raw_text.splitlines()]
    cleaned_text = "\n".join(line for line in lines if line)
    return cleaned_text.strip(), extracted_links


def _extract_preview_and_reply_context(msg_node: Tag) -> tuple[str, list[str]]:
    """Извлекает контекст из превью статьи и цитируемого сообщения, если они есть в посте."""
    extra_parts: list[str] = []
    extra_links: list[str] = []

    reply_el = msg_node.select_one(".tgme_widget_message_reply")
    if reply_el:
        reply_txt = reply_el.get_text(" ", strip=True)
        reply_href = _unwrap_url(str(reply_el.get("href") or "").strip())
        if reply_txt:
            extra_parts.append(f"[В ответ на / Цитата ({reply_href})]: {reply_txt}")
        if reply_href and _is_hydratable_article_url(reply_href):
            extra_links.append(reply_href)

    preview_el = msg_node.select_one(".tgme_widget_message_link_preview")
    if preview_el:
        preview_href = _unwrap_url(str(preview_el.get("href") or "").strip())
        site_el = preview_el.select_one(".link_preview_site_name")
        title_el = preview_el.select_one(".link_preview_title")
        desc_el = preview_el.select_one(".link_preview_description")
        preview_bits = [
            el.get_text(" ", strip=True)
            for el in (site_el, title_el, desc_el)
            if el and el.get_text(" ", strip=True)
        ]
        if preview_bits:
            extra_parts.append(f"[Превью вложенного материала ({preview_href})]: " + " — ".join(preview_bits))
        if preview_href and _is_hydratable_article_url(preview_href) and preview_href not in extra_links:
            extra_links.append(preview_href)

    return "\n".join(extra_parts), extra_links


async def _fetch_single_channel(
    client: httpx.AsyncClient,
    channel_sem: asyncio.Semaphore,
    article_sem: asyncio.Semaphore,
    url_cache: dict[str, str],
    channel_raw: str,
    cutoff_utc: datetime,
    read_nested_articles: bool = True,
) -> list[RawItem]:
    """
    Листает историю канала назад до тех пор, пока не дойдёт до постов старше cutoff_utc (24 часа),
    и загружает все посты + все вложенные в них статьи.
    """
    async with channel_sem:
        username = await _resolve_channel_username(client, channel_raw)
        if not username:
            return []

        collected: list[RawItem] = []
        seen_post_ids: set[str] = set()
        before_id: int | None = None

        # Листаем назад до тех пор, пока не упрёмся в cutoff_utc (24 часа назад)
        while True:
            url = f"https://t.me/s/{username}"
            if before_id is not None:
                url = f"{url}?before={before_id}"

            try:
                resp = await client.get(url, follow_redirects=True, timeout=20.0)
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
                if not data_post or "/" not in data_post:
                    continue
                if data_post in seen_post_ids:
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
                if not full_post_text:
                    continue

                post_url = f"https://t.me/{data_post}"

                # Читаем все вложенные статьи и вложенные ссылки внутри этого поста
                if read_nested_articles and external_links:
                    visited_for_post: set[str] = {post_url}
                    hydrated_blocks: list[str] = []
                    for link_url in external_links:
                        nested_articles = await _hydrate_url_recursive(
                            client=client,
                            sem=article_sem,
                            url_cache=url_cache,
                            url=link_url,
                            depth=1,
                            visited=visited_for_post,
                        )
                        for art_url, art_text in nested_articles:
                            hydrated_blocks.append(f"[Полный текст вложенной статьи ({art_url})]:\n{art_text}")

                    if hydrated_blocks:
                        full_post_text = full_post_text + "\n\n" + "\n\n".join(hydrated_blocks)

                collected.append(
                    RawItem(
                        item_id=f"tg_{data_post.replace('/', '_')}",
                        source_type="telegram",
                        source_name=f"@{username} ({channel_title})",
                        source_title=f"Пост #{post_num_str} в @{username}",
                        source_url=post_url,
                        published_at=dt,
                        text=full_post_text[:15000],
                        external_links=external_links,
                    )
                )

            if not page_post_nums:
                break
            # Останавливаем пагинацию только тогда, когда дошли до постов старше 24 часов!
            if oldest_on_page and oldest_on_page < cutoff_utc:
                break
            new_before_id = min(page_post_nums)
            if before_id is not None and new_before_id >= before_id:
                break
            before_id = new_before_id

        collected.sort(key=lambda x: x.published_at)
        logger.info("Канал @%s: собрано %d постов за последние 24ч", username, len(collected))
        return collected


async def collect_telegram_posts(config: AppConfig, cutoff_utc: datetime) -> list[RawItem]:
    """Собирает ВСЕ посты за последние 24 часа из всех каналов в config.yaml."""
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
    channel_sem = asyncio.Semaphore(10)
    article_sem = asyncio.Semaphore(12)
    url_cache: dict[str, str] = {}

    async with httpx.AsyncClient(headers=headers) as client:
        tasks = [
            _fetch_single_channel(
                client=client,
                channel_sem=channel_sem,
                article_sem=article_sem,
                url_cache=url_cache,
                channel_raw=ch,
                cutoff_utc=cutoff_utc,
                read_nested_articles=config.telegram.read_nested_articles,
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

    return all_items


# ==============================================================================
# 3. Сбор ВСЕХ писем из Gmail через стандартный IMAP за последние 24 часа
# ==============================================================================


def _decode_mime_header(raw_header: str | None) -> str:
    if not raw_header:
        return ""
    parts = decode_header(raw_header)
    decoded_chunks: list[str] = []
    for content, charset in parts:
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

    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned[:12000], links[:20]


def _extract_email_body(msg: email.message.Message) -> tuple[str, list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain":
                plain_parts.append(decoded)
            elif content_type == "text/html":
                html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(decoded)
            else:
                plain_parts.append(decoded)

    if html_parts:
        text, links = _html_to_clean_text("\n".join(html_parts))
        if text:
            return text, links

    plain_text = "\n".join(plain_parts).strip()
    urls = re.findall(r"https?://[^\s<>()\"']+", plain_text)
    return plain_text[:12000], urls[:20]


def _matches_allowed_senders(sender: str, allowed_senders: Iterable[str]) -> bool:
    allowed = [s.strip().lower() for s in allowed_senders if s.strip()]
    if not allowed:
        return True
    sender_lower = sender.lower()
    return any(pattern in sender_lower for pattern in allowed)


def collect_gmail_emails(config: AppConfig, cutoff_utc: datetime) -> list[RawItem]:
    """Подключается к Gmail по IMAP и забирает ВСЕ письма за последние 24 часа."""
    if not config.gmail.enabled:
        return []
    if not config.gmail_user or not config.gmail_app_password:
        logger.info("GMAIL_USER / GMAIL_APP_PASSWORD не заданы — пропускаем сбор почты.")
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

            msg_nums = data[0].split()

            for num in reversed(msg_nums):
                status, msg_data = mail.fetch(num, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue

                raw_bytes = b""
                for part in msg_data:
                    if isinstance(part, tuple) and len(part) >= 2:
                        raw_bytes = part[1]
                        break
                if not raw_bytes:
                    continue

                msg = email.message_from_bytes(raw_bytes)
                date_str = msg.get("Date")
                if not date_str:
                    continue
                try:
                    dt = parsedate_to_datetime(date_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                except Exception:
                    continue

                if dt < cutoff_utc:
                    continue

                sender = _decode_mime_header(msg.get("From"))
                sender_low = sender.lower()
                # Пропускаем системные письма Google, отчёты о доставке (mailer-daemon) и собственные отправленные дайджесты
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

                if message_id_hdr:
                    gmail_url = f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{quote(message_id_hdr)}"
                else:
                    gmail_url = f"https://mail.google.com/mail/u/0/#search/{quote(subject[:50])}"

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
