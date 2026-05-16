"""
Единая точка публикации в Telegram.

publish_to_channel использует Telethon publisher-бота из tools/tg_publisher:
- review: отправляет пост ревьюерам с кнопками;
- direct: сразу публикует в канал;
- auto: review, если заданы TG_REVIEW_CHAT_ID/TG_REVIEW_CHAT_IDS, иначе direct.
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import BinaryIO, Iterable, List, Optional, Union

ImageInput = Union[str, Path, bytes, bytearray, BinaryIO]
MAX_MEDIA_GROUP = 10


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
    parse_mode: Optional[str] = "HTML",
) -> dict:
    """
    Публикует пост через publisher-бота из tools/tg_publisher.

    Управление режимом:
    - TG_PUBLISH_MODE=review: отправить preview ревьюерам;
    - TG_PUBLISH_MODE=direct: сразу отправить в TG_CHANNEL_ID;
    - TG_PUBLISH_MODE=auto: review при наличии TG_REVIEW_CHAT_ID(S), иначе direct.

    Бот-слушатель запускается вместе с сервисом в main.py. Здесь используется
    отдельная sender-session, чтобы отправка preview/direct не конфликтовала с
    процессом, который слушает кнопки ревью.
    """
    PendingPost, DEFAULT_SEND_SESSION_NAME, build_bot_from_env = _import_publisher()

    image_paths = _prepare_image_paths(images)
    post = PendingPost.create(text=text or "", image_paths=image_paths)
    mode = _selected_publish_mode()

    if mode == "review":
        bot = build_bot_from_env(
            require_review=True,
            require_channel=True,
            session_name=os.getenv("TG_PUBLISHER_SEND_SESSION", DEFAULT_SEND_SESSION_NAME),
            parse_mode=parse_mode,
        )
        _run_sync(bot.deliver_for_review(post))
        return {"ok": True, "mode": "review", "pending_id": post.id}

    bot = build_bot_from_env(
        require_channel=True,
        session_name=os.getenv("TG_PUBLISHER_SEND_SESSION", DEFAULT_SEND_SESSION_NAME),
        parse_mode=parse_mode,
    )
    _run_sync(bot.deliver_direct(post))
    return {"ok": True, "mode": "direct", "pending_id": post.id}
