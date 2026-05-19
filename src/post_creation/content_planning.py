from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from time import sleep
from typing import Any, Mapping

try:
    from dotenv import load_dotenv
except ImportError as e:
    raise ImportError("Установите python-dotenv: pip install python-dotenv") from e

try:
    import requests
except ImportError as e:
    raise ImportError("Установите requests: pip install requests") from e

try:
    from character.character_memory import register_published_post
    from post_creation.content_creating import build_character_memory, creating_message, creating_pictures
    from tg.telegram_channel import publish_to_channel
except ImportError:
    from src.character.character_memory import register_published_post
    from src.post_creation.content_creating import build_character_memory, creating_message, creating_pictures
    from src.tg.telegram_channel import publish_to_channel

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-chat-v3.1"
PUBLICATIONS_PER_WEEK = 3


def current_week_start(now: datetime | None = None) -> datetime:
    """Понедельник 00:00 недели, в которую попадает *now* (локальное время)."""
    moment = now or datetime.now()
    monday = moment.date() - timedelta(days=moment.weekday())
    return datetime.combine(monday, time.min)


def sleep_until_next_week_start(now: datetime | None = None) -> None:
    """Ждёт до понедельника 00:00 следующей календарной недели."""
    moment = now or datetime.now()
    target = current_week_start(moment)
    if moment >= target:
        target += timedelta(weeks=1)
    wait_seconds = (target - moment).total_seconds()
    if wait_seconds > 0:
        sleep(wait_seconds)


def run_weekly_content_cycle(character_profile: Mapping[str, object]) -> None:
    """Строит план на неделю и выполняет публикации по расписанию."""
    plan = build_weekly_publication_plan(character_profile)
    run_content_schedule(plan, character_profile)
    print("Content schedule completed")


def _openrouter_headers() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("Задайте OPENROUTER_API_KEY в .env")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _chat_json(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.8,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        OPENROUTER_URL,
        headers=_openrouter_headers(),
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM API вернул пустой ответ")
    content = choices[0].get("message", {}).get("content", "").strip()
    if not content:
        raise RuntimeError("LLM API не вернул content")
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM вернул невалидный JSON: {content}") from e


def _week_bounds(week_start: datetime) -> tuple[datetime, datetime]:
    end = week_start + timedelta(days=7) - timedelta(seconds=1)
    return week_start, end


def _normalize_day_event(
    raw: Mapping[str, Any],
    *,
    destination: Mapping[str, Any],
) -> dict[str, Any]:
    def _strings(key: str) -> list[str]:
        value = raw.get(key, [])
        if not isinstance(value, list):
            return [str(value)] if value else []
        return [str(item) for item in value if str(item).strip()]

    return {
        "destination": {
            "name": str(destination.get("name", "")),
            "country": str(destination.get("country", "")),
            "why_trending": str(destination.get("why_trending", "")),
        },
        "location": str(raw.get("location", destination.get("name", ""))),
        "events": _strings("events"),
        "actions": _strings("actions"),
        "observations": _strings("observations"),
        "facts": _strings("facts"),
        "result": str(raw.get("result", "")),
    }


def _parse_scheduled_at(value: str, week_start: datetime, week_end: datetime) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    if moment < week_start:
        moment = week_start.replace(
            hour=moment.hour,
            minute=moment.minute,
            second=0,
            microsecond=0,
        )
    if moment > week_end:
        moment = week_end.replace(hour=12, minute=0, second=0, microsecond=0)
    return moment


def _clamp_future(moment: datetime, now: datetime) -> datetime:
    if moment >= now:
        return moment
    candidate = now.replace(
        hour=moment.hour,
        minute=moment.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate


def _request_weekly_plan(
    character_profile: Mapping[str, object],
    *,
    week_start: datetime,
    now: datetime,
    model: str,
) -> dict[str, Any]:
    week_end = week_start + timedelta(days=6, hours=23, minutes=59)
    profile_json = json.dumps(dict(character_profile), ensure_ascii=False, indent=2)
    system_prompt = (
        "Ты редактор travel-канала в Telegram. Составь правдоподобный недельный план "
        "для вымышленного блогера, который как будто реально путешествует.\n"
        "Сначала выбери одно направление, которое сейчас популярно у путешественников "
        "(тренды сезона, события, хайп в соцсетях) и подходит персонажу.\n"
        "Затем придумай ровно 3 дня публикаций в пределах указанной календарной недели: "
        "у каждого дня — сюжет одного travel-дня с событиями, действиями, наблюдениями, "
        "фактами и итогом.\n"
        "Верни только JSON без Markdown. Формат:\n"
        '{"destination":{"name":"...","country":"...","why_trending":"..."},'
        '"publications":[{"scheduled_at":"YYYY-MM-DDTHH:MM:SS","location":"...",'
        '"events":["..."],"actions":["..."],"observations":["..."],'
        '"facts":["..."],"result":"..."}]}'
    )
    user_prompt = (
        f"Сегодня: {now.strftime('%Y-%m-%d %H:%M')} (локальное время).\n"
        f"Неделя плана: с {week_start.strftime('%Y-%m-%d')} "
        f"по {week_end.strftime('%Y-%m-%d')}.\n"
        f"Нужно ровно {PUBLICATIONS_PER_WEEK} публикации в разные дни недели, "
        "время — реалистичные часы для поста (утро/день/вечер).\n"
        f"scheduled_at строго внутри этой недели и не раньше текущего момента.\n"
        f"Профиль персонажа:\n{profile_json}"
    )
    return _chat_json(system_prompt, user_prompt, model=model)


def build_weekly_publication_plan(
    character_profile: Mapping[str, object],
    *,
    model: str = DEFAULT_MODEL,
    now: datetime | None = None,
) -> dict[datetime, str]:
    """
    Формирует план публикаций на текущую неделю и возвращает его.

    :param character_profile: Словарь с описанием персонажа.
    :returns: Словарь: ключ — datetime публикации, значение — JSON-строка
        с событием дня (destination, location, events, actions,
        observations, facts, result).
    """
    moment = now or datetime.now()
    week_start = current_week_start(moment)
    _, week_end = _week_bounds(week_start)

    raw_plan = _request_weekly_plan(
        character_profile,
        week_start=week_start,
        now=moment,
        model=model,
    )

    destination = raw_plan.get("destination") or {}
    if not isinstance(destination, dict):
        destination = {}

    publications = raw_plan.get("publications") or []
    if not isinstance(publications, list) or not publications:
        raise RuntimeError("LLM не вернул publications для недельного плана")

    schedule: dict[datetime, str] = {}
    for item in publications[:PUBLICATIONS_PER_WEEK]:
        if not isinstance(item, dict):
            continue
        scheduled_raw = item.get("scheduled_at")
        if not scheduled_raw:
            continue
        when = _parse_scheduled_at(str(scheduled_raw), week_start, week_end)
        when = _clamp_future(when, moment)
        day_event = _normalize_day_event(item, destination=destination)
        while when in schedule:
            when += timedelta(minutes=5)
        schedule[when] = json.dumps(day_event, ensure_ascii=False)

    if len(schedule) < PUBLICATIONS_PER_WEEK:
        raise RuntimeError(
            f"Не удалось собрать {PUBLICATIONS_PER_WEEK} слотов публикации из ответа LLM"
        )
    return schedule


def run_content_schedule(
    schedule: Mapping[datetime, str],
    character_profile: Mapping[str, object],
) -> None:
    """
    Выполняет обработку описаний контента по расписанию.

    :param schedule: Словарь, где ключ — datetime, а значение — описание контента для обработки.
    :param character_profile: Словарь с описанием персонажа.
    """
    prepared: list[tuple[datetime, str]] = []

    for when, content in schedule.items():
        prepared.append((when, content))

    for when, content in sorted(prepared, key=lambda item: item[0]):
        now = datetime.now()
        wait_seconds = (when - now).total_seconds()

        if wait_seconds > 0:
            sleep(wait_seconds)
        memory_context = build_character_memory(content, character_profile)
        text, parse_mode = creating_message(
            content,
            character_profile,
            memory_context=memory_context,
        )
        images = creating_pictures(content, character_profile)
        publish_to_channel(text=text, images=images, parse_mode=parse_mode)
        register_published_post(
            character_profile,
            post_text=text,
            source_event=content,
            published_at=datetime.now(),
        )
