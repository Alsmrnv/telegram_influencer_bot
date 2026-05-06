"""
Публикация текста и изображений в Telegram-канал через Bot API.
"""

import io
import json
import os
from pathlib import Path
from typing import BinaryIO, Iterable, List, Optional, Union

try:
    from dotenv import load_dotenv
except ImportError as e:
    raise ImportError("Установите python-dotenv: pip install python-dotenv") from e

try:
    import requests
except ImportError as e:
    raise ImportError("Установите requests: pip install requests") from e

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)

ImageInput = Union[str, Path, bytes, bytearray, BinaryIO]

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MEDIA_GROUP = 10
MAX_CAPTION_LENGTH = 1024


def _normalize_image(img: ImageInput) -> tuple[str, BinaryIO, bool]:
    """
    Приводит вход к бинарному потоку для multipart-загрузки в Telegram.

    :returns: (имя файла для поля формы, поток, закрывать ли поток после использования).
    :raises FileNotFoundError: если указан несуществующий файл.
    """
    if isinstance(img, (str, Path)):
        path = Path(img)
        if not path.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")
        f = open(path, "rb")
        return path.name, f, True
    if isinstance(img, (bytes, bytearray)):
        return "photo.jpg", io.BytesIO(bytes(img)), True
    return "photo.jpg", img, False


def _truncate_caption(text: str) -> str:
    """
    Укорачивает подпись к фото до лимита Telegram.
    Если текст длиннее, обрезает и добавляет многоточие в конце.
    """
    if len(text) <= MAX_CAPTION_LENGTH:
        return text
    return text[:MAX_CAPTION_LENGTH - 3] + "..."


def publish_to_channel(
    text: str,
    images: Optional[Iterable[ImageInput]] = None,
    *,
    parse_mode: Optional[str] = "HTML",
) -> dict:
    """
    Публикует пост в канал: текст, одно фото с подписью или альбом (до 10 снимков).

    :param text: Текст сообщения или подпись к фото/альбому.
    :param images: Пути, bytes или бинарные потоки; None — только текст.
    :param parse_mode: "HTML", "MarkdownV2" или None (без разметки).
    :returns: Полный JSON-ответ метода Bot API.
    :raises ValueError: больше 10 изображений или нет токена/id канала в окружении.
    :raises requests.HTTPError: ошибка HTTP при обращении к API.
    :raises RuntimeError: в теле ответа 'ok: false'.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    cid = os.environ.get("TELEGRAM_CHANNEL_ID")
    if not token:
        raise ValueError(
            "В .env (или окружении) задайте TELEGRAM_BOT_TOKEN"
        )
    if not cid:
        raise ValueError(
            "В .env (или окружении) задайте TELEGRAM_CHANNEL_ID"
        )
    cid = str(cid)
    imgs: List[ImageInput] = list(images) if images is not None else []

    if len(imgs) > MAX_MEDIA_GROUP:
        raise ValueError(
            f"Не больше {MAX_MEDIA_GROUP} изображений за один альбом"
        )

    caption = _truncate_caption(text) if text else None

    if not imgs:
        payload: dict = {"chat_id": cid, "text": text or ""}
        if parse_mode and text:
            payload["parse_mode"] = parse_mode
        r = requests.post(
            TELEGRAM_API.format(token=token, method="sendMessage"),
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", str(data)))
        return data

    opened: list[tuple[BinaryIO, bool]] = []

    try:
        if len(imgs) == 1:
            name, stream, close_me = _normalize_image(imgs[0])
            opened.append((stream, close_me))
            files = {"photo": (name, stream)}
            data_fields: dict = {"chat_id": cid}
            if caption is not None:
                data_fields["caption"] = caption
            if parse_mode and caption:
                data_fields["parse_mode"] = parse_mode
            r = requests.post(
                TELEGRAM_API.format(token=token, method="sendPhoto"),
                data=data_fields,
                files=files,
                timeout=120,
            )
        else:
            media: list[dict] = []
            multipart: list[tuple[str, tuple[str, BinaryIO]]] = []
            for i, item in enumerate(imgs):
                name, stream, close_me = _normalize_image(item)
                opened.append((stream, close_me))
                attach = f"file{i}"
                multipart.append(
                    (attach, (name or f"photo_{i}.jpg", stream))
                )
                entry: dict = {"type": "photo", "media": f"attach://{attach}"}
                if i == 0 and caption is not None:
                    entry["caption"] = caption
                    if parse_mode:
                        entry["parse_mode"] = parse_mode
                media.append(entry)

            data_fields = {
                "chat_id": cid,
                "media": json.dumps(media),
            }
            r = requests.post(
                TELEGRAM_API.format(token=token, method="sendMediaGroup"),
                data=data_fields,
                files=multipart,
                timeout=120,
            )

        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", str(body)))
        return body
    finally:
        for stream, close_me in opened:
            if close_me:
                try:
                    stream.close()
                except Exception:
                    pass
