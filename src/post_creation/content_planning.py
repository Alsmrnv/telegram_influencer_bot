from datetime import datetime, time, timedelta
from time import sleep
from typing import Mapping

from content_creating import creating_message, creating_pictures
from telegram_channel import publish_to_channel


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
    run_content_schedule(plan)
    print("Content schedule completed")


def build_weekly_publication_plan(
    character_profile: Mapping[str, object],
) -> dict[datetime, str]:
    """
    Формирует план публикаций на текущую неделю и возвращает его.

    :param character_profile: Словарь с описанием персонажа.
    :returns: Словарь с датой/временем публикации и описанием контента.
    """
    # TODO: Реализовать эту функцию
    return {}


def run_content_schedule(schedule: Mapping[datetime, str]) -> None:
    """
    Выполняет обработку описаний контента по расписанию.

    :param schedule: Словарь, где ключ — datetime, а значение — описание контента для обработки.
    """
    prepared: list[tuple[datetime, str]] = []

    for when, content in schedule.items():
        prepared.append((when, content))

    for when, content in sorted(prepared, key=lambda item: item[0]):
        now = datetime.now()
        wait_seconds = (when - now).total_seconds()

        if wait_seconds > 0:
            sleep(wait_seconds)
            text, parse_mode = creating_message(content)
            images = creating_pictures(content)
            publish_to_channel(text=text, images=images, parse_mode=parse_mode)
            continue
