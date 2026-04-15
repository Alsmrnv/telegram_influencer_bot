from datetime import datetime
from time import sleep
from typing import Mapping

from content_creating import creating_message, creating_pictures
from telegram_channel import publish_to_channel


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
