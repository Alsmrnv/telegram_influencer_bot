"""
Единая точка публикации в Telegram.

publish_to_channel использует Telethon publisher-бота из tools/tg_publisher:
- review: отправляет пост ревьюерам с кнопками;
- direct: сразу публикует в канал;
- auto: review, если заданы TG_REVIEW_CHAT_ID/TG_REVIEW_CHAT_IDS, иначе direct.

Файл также нормализует пользовательскую разметку перед отправкой:
- parse_mode=None: отправляем текст как есть;
- parse_mode="HTML": санитайзим Telegram HTML;
- parse_mode="Markdown"/"md": переводим простой Markdown в Telegram HTML.

Внутрь Telegram в итоге уходит либо plain text, либо безопасный HTML. Так мы не зависим
от различий между Markdown-диалектами Telegram/Telethon и не ломаем дефисы, подчёркивания
в словах, ссылки и случайные угловые скобки.
"""

from __future__ import annotations

import asyncio
import html
import io
import os
import re
import tempfile
import threading
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO, Iterable, List, Optional, Union
from urllib.parse import urlparse

ImageInput = Union[str, Path, bytes, bytearray, BinaryIO]
MAX_MEDIA_GROUP = 10

_TELEGRAM_HTML_TAGS = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "u": "u",
    "s": "s",
    "strike": "s",
    "del": "s",
    "code": "code",
    "a": "a",
}

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\(([^)\s]{1,1000})\)")
_ALLOWED_LINK_SCHEMES = {"http", "https", "tg"}

_HTML_PLACEHOLDER_OPEN = "\uE100"
_HTML_PLACEHOLDER_CLOSE = "\uE101"
_PLACEHOLDER_TOKEN_RE = re.compile(
    rf"{_HTML_PLACEHOLDER_OPEN}(\d+){_HTML_PLACEHOLDER_CLOSE}|\uE000(\d+)\uE000"
)
_ALLOWED_SIMPLE_HTML_TAG_RE = re.compile(
    r"</?\s*(b|strong|i|em|u|s|strike|del|code)\s*>",
    flags=re.IGNORECASE,
)
_ALLOWED_HTML_A_TAG_RE = re.compile(
    r"<\s*a\s+[^>]*href\s*=\s*(['\"])(.*?)\1[^>]*>|</\s*a\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)


def _make_placeholder(index: int) -> str:
    return f"{_HTML_PLACEHOLDER_OPEN}{index}{_HTML_PLACEHOLDER_CLOSE}"


def _restore_placeholders(text: str, placeholders: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        raw_index = match.group(1) or match.group(2)
        if not raw_index or not raw_index.isdigit():
            return match.group(0)
        idx = int(raw_index)
        if 0 <= idx < len(placeholders):
            return placeholders[idx]
        return match.group(0)

    return _PLACEHOLDER_TOKEN_RE.sub(repl, text)


def _replace_allowed_html_with_placeholders(text: str, placeholders: list[str]) -> str:
    """
    Keeps already-written Telegram HTML usable inside parse_mode="Markdown" posts.

    The publisher historically receives parse_mode="Markdown" from callers, while the
    generated posts often already contain Telegram HTML such as <b>...</b> and <i>...</i>.
    Without placeholders those tags are escaped by the Markdown converter and become visible
    text. Only a tiny allow-list is preserved; everything else remains plain escaped text.
    """
    simple_map = {
        "strong": "b",
        "em": "i",
        "strike": "s",
        "del": "s",
    }

    def replace_a(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw.lower().startswith("</"):
            placeholders.append("</a>")
            return _make_placeholder(len(placeholders) - 1)

        href = (match.group(2) or "").strip()
        if not _is_safe_url(href):
            return html.escape(raw, quote=False)
        placeholders.append(f'<a href="{html.escape(href, quote=True)}">')
        return _make_placeholder(len(placeholders) - 1)

    text = _ALLOWED_HTML_A_TAG_RE.sub(replace_a, text)

    def replace_simple(match: re.Match[str]) -> str:
        raw = match.group(0)
        tag = match.group(1).lower()
        tag = simple_map.get(tag, tag)
        is_close = raw.lstrip().startswith("</")
        normalized = f"</{tag}>" if is_close else f"<{tag}>"
        placeholders.append(normalized)
        return _make_placeholder(len(placeholders) - 1)

    return _ALLOWED_SIMPLE_HTML_TAG_RE.sub(replace_simple, text)


class _TelegramHtmlSanitizer(HTMLParser):
    """Small allow-list sanitizer for Telegram-compatible HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        normalized = tag.lower()
        out_tag = _TELEGRAM_HTML_TAGS.get(normalized)
        if out_tag is None:
            return

        if out_tag == "a":
            href = ""
            for name, value in attrs:
                if name.lower() == "href" and value:
                    href = value.strip()
                    break
            if not _is_safe_url(href):
                return
            self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
            self.stack.append("a")
            return

        self.parts.append(f"<{out_tag}>")
        self.stack.append(out_tag)

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        normalized = tag.lower()
        out_tag = _TELEGRAM_HTML_TAGS.get(normalized)
        if out_tag is None or out_tag not in self.stack:
            return

        # Close tags until the matching one. It keeps broken/nested HTML valid enough
        # for Telegram instead of leaking an unclosed formatting tag into the post.
        while self.stack:
            opened = self.stack.pop()
            self.parts.append(f"</{opened}>")
            if opened == out_tag:
                break

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:  # type: ignore[override]
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:  # type: ignore[override]
        self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts)


def _is_safe_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme.lower() in _ALLOWED_LINK_SCHEMES and bool(parsed.netloc or parsed.scheme == "tg")


def _normalize_parse_mode(parse_mode: Optional[str]) -> Optional[str]:
    if parse_mode is None:
        return None
    normalized = parse_mode.strip().lower()
    if normalized in {"", "none", "plain", "text"}:
        return None
    if normalized in {"html", "htm"}:
        return "html"
    if normalized in {"markdown", "md", "markdownv2"}:
        return "markdown"
    return normalized


def _sanitize_telegram_html(text: str) -> str:
    parser = _TelegramHtmlSanitizer()
    parser.feed(text or "")
    parser.close()
    return parser.get_html()


def _strip_markdown_fences(text: str) -> str:
    # Пользователь специально просил не завязываться на ```.
    # Если они всё же попали в текст, не делаем code block, а просто убираем маркеры.
    return re.sub(r"^\s*```[a-zA-Z0-9_-]*\s*$", "", text, flags=re.MULTILINE)


def _replace_markdown_links_with_placeholders(text: str, placeholders: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        if not _is_safe_url(url):
            return html.escape(label, quote=False)
        placeholders.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>')
        return _make_placeholder(len(placeholders) - 1)

    return _MARKDOWN_LINK_RE.sub(repl, text)


def _markdown_inline_to_html(text: str) -> str:
    """
    Converts a small, predictable Markdown subset into Telegram HTML.

    Supported:
    - **bold**
    - *italic* and _italic_
    - ~~strike~~
    - `inline code`
    - [label](https://example.com)

    Unsupported Markdown stays visible as plain text. Ordinary hyphens and underscores inside
    words are preserved: прямо-таки, ML-стек, some_variable.
    """
    text = _strip_markdown_fences(text or "")
    placeholders: list[str] = []
    text = _replace_allowed_html_with_placeholders(text, placeholders)
    text = _replace_markdown_links_with_placeholders(text, placeholders)

    result: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # HTML/link placeholders generated before escaping.
        if text.startswith(_HTML_PLACEHOLDER_OPEN, i):
            end = text.find(_HTML_PLACEHOLDER_CLOSE, i + len(_HTML_PLACEHOLDER_OPEN))
            if end != -1:
                raw_index = text[i + len(_HTML_PLACEHOLDER_OPEN) : end]
                if raw_index.isdigit():
                    idx = int(raw_index)
                    if 0 <= idx < len(placeholders):
                        result.append(placeholders[idx])
                        i = end + len(_HTML_PLACEHOLDER_CLOSE)
                        continue

        if text.startswith("**", i):
            end = text.find("**", i + 2)
            if end != -1 and end > i + 2:
                inner = text[i + 2 : end]
                result.append("<b>" + html.escape(inner, quote=False) + "</b>")
                i = end + 2
                continue

        if text.startswith("~~", i):
            end = text.find("~~", i + 2)
            if end != -1 and end > i + 2:
                inner = text[i + 2 : end]
                result.append("<s>" + html.escape(inner, quote=False) + "</s>")
                i = end + 2
                continue

        if ch == "`":
            end = text.find("`", i + 1)
            if end != -1 and end > i + 1:
                inner = text[i + 1 : end]
                result.append("<code>" + html.escape(inner, quote=False) + "</code>")
                i = end + 1
                continue

        if ch in {"*", "_"} and _can_open_emphasis(text, i):
            end = _find_closing_emphasis(text, ch, i + 1)
            if end != -1:
                inner = text[i + 1 : end]
                result.append("<i>" + html.escape(inner, quote=False) + "</i>")
                i = end + 1
                continue

        result.append(html.escape(ch, quote=False))
        i += 1

    return "".join(result)


def _can_open_emphasis(text: str, index: int) -> bool:
    marker = text[index]
    prev_char = text[index - 1] if index > 0 else "\n"
    next_char = text[index + 1] if index + 1 < len(text) else ""

    if not next_char or next_char.isspace():
        return False
    if marker == "*" and (index == 0 or prev_char == "\n") and next_char.isspace():
        return False
    if marker == "_" and (prev_char.isalnum() or next_char.isalnum() and prev_char.isalnum()):
        return False
    return True


def _find_closing_emphasis(text: str, marker: str, start: int) -> int:
    search_from = start
    while True:
        end = text.find(marker, search_from)
        if end == -1:
            return -1
        prev_char = text[end - 1] if end > 0 else ""
        next_char = text[end + 1] if end + 1 < len(text) else "\n"
        if prev_char and not prev_char.isspace() and not next_char.isalnum():
            return end
        search_from = end + 1


def prepare_telegram_text(text: str, parse_mode: Optional[str]) -> tuple[str, Optional[str]]:
    """
    Returns text and Telethon parse mode ready for Telegram.

    Plain text stays plain. HTML is sanitized. Markdown is converted to sanitized HTML,
    because Telethon and Telegram Markdown dialects differ and can mangle normal posts.
    """
    mode = _normalize_parse_mode(parse_mode)
    value = text or ""

    if mode is None:
        return value, None
    if mode == "html":
        return _sanitize_telegram_html(value), "html"
    if mode == "markdown":
        return _markdown_inline_to_html(value), "html"

    # Unknown parse mode: do not risk broken Telegram rendering.
    return value, None


def _load_tools_env() -> None:
    try:
        from tools.env import load_tools_env
    except ImportError:
        from src.tools.env import load_tools_env

    load_tools_env()


def _import_publisher():
    try:
        from tools.tg_publisher.models import PendingPost
        from tools.tg_publisher.publisher_telethon import (
            DEFAULT_SEND_SESSION_NAME,
            build_bot_from_env,
        )
    except ImportError:
        from src.tools.tg_publisher.models import PendingPost
        from src.tools.tg_publisher.publisher_telethon import (
            DEFAULT_SEND_SESSION_NAME,
            build_bot_from_env,
        )

    return PendingPost, DEFAULT_SEND_SESSION_NAME, build_bot_from_env


def _media_root() -> Path:
    _load_tools_env()
    root = os.getenv("TG_MEDIA_DIR", "data/tg_publisher/media")
    path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _copy_image_to_file(img: ImageInput, dst_dir: Path, index: int) -> str:
    if isinstance(img, (str, Path)):
        path = Path(img).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")
        return str(path)

    suffix = ".jpg"
    out = dst_dir / f"image_{index}{suffix}"

    if isinstance(img, (bytes, bytearray)):
        out.write_bytes(bytes(img))
        return str(out)

    data = img.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    out.write_bytes(data)
    return str(out)


def _prepare_image_paths(images: Optional[Iterable[ImageInput]]) -> list[str]:
    imgs: List[ImageInput] = list(images) if images is not None else []
    if len(imgs) > MAX_MEDIA_GROUP:
        raise ValueError(f"Не больше {MAX_MEDIA_GROUP} изображений за один альбом")

    if not imgs:
        return []

    # Пути передаем как есть; bytes/streams складываем в отдельную папку pending-медиа.
    temp_dir: Path | None = None
    paths: list[str] = []
    for idx, img in enumerate(imgs):
        if isinstance(img, (str, Path)):
            paths.append(_copy_image_to_file(img, Path("."), idx))
            continue
        if temp_dir is None:
            temp_dir = _media_root() / uuid.uuid4().hex
            temp_dir.mkdir(parents=True, exist_ok=True)
        paths.append(_copy_image_to_file(img, temp_dir, idx))
    return paths


def _selected_publish_mode() -> str:
    _load_tools_env()
    raw = os.getenv("TG_PUBLISH_MODE", "auto").strip().lower()
    if raw not in {"auto", "review", "direct"}:
        raise ValueError("TG_PUBLISH_MODE must be one of: auto, review, direct")
    if raw != "auto":
        return raw

    if os.getenv("TG_REVIEW_CHAT_ID") or os.getenv("TG_REVIEW_CHAT_IDS"):
        return "review"
    return "direct"


def _run_sync(coro) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    error: list[BaseException] = []

    def runner() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - forwarded to caller
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=False)
    thread.start()
    thread.join()
    if error:
        raise error[0]


def publish_to_channel(
    text: str,
    images: Optional[Iterable[ImageInput]] = None,
    *,
    parse_mode: Optional[str] = None,
) -> dict:
    """
    Публикует пост через publisher-бота из tools/tg_publisher.

    parse_mode:
    - None/plain/text: без разметки;
    - HTML: разрешенный Telegram HTML санитайзится;
    - Markdown/md: простой Markdown переводится в Telegram HTML.

    Управление режимом:
    - TG_PUBLISH_MODE=review: отправить preview ревьюерам;
    - TG_PUBLISH_MODE=direct: сразу отправить в TG_CHANNEL_ID;
    - TG_PUBLISH_MODE=auto: review при наличии TG_REVIEW_CHAT_ID(S), иначе direct.

    Бот-слушатель запускается вместе с сервисом в main.py. Здесь используется
    отдельная sender-session, чтобы отправка preview/direct не конфликтовала с
    процессом, который слушает кнопки ревью.
    """
    PendingPost, DEFAULT_SEND_SESSION_NAME, build_bot_from_env = _import_publisher()

    prepared_text, prepared_parse_mode = prepare_telegram_text(text, parse_mode)
    image_paths = _prepare_image_paths(images)
    post = PendingPost.create(
        text=prepared_text,
        image_paths=image_paths,
        metadata={"parse_mode": prepared_parse_mode},
    )
    mode = _selected_publish_mode()

    if mode == "review":
        bot = build_bot_from_env(
            require_review=True,
            require_channel=True,
            session_name=os.getenv("TG_PUBLISHER_SEND_SESSION", DEFAULT_SEND_SESSION_NAME),
            parse_mode=prepared_parse_mode,
        )
        _run_sync(bot.deliver_for_review(post))
        return {"ok": True, "mode": "review", "pending_id": post.id, "parse_mode": prepared_parse_mode}

    bot = build_bot_from_env(
        require_channel=True,
        session_name=os.getenv("TG_PUBLISHER_SEND_SESSION", DEFAULT_SEND_SESSION_NAME),
        parse_mode=prepared_parse_mode,
    )
    _run_sync(bot.deliver_direct(post))
    return {"ok": True, "mode": "direct", "pending_id": post.id, "parse_mode": prepared_parse_mode}