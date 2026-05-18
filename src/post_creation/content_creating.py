from typing import Iterable, Mapping, Optional, Tuple

from telegram_channel import ImageInput


def creating_message(
    content: str,
    character_profile: Mapping[str, object],
) -> Tuple[str, Optional[str]]:
    """
    Формирует текст сообщения и parse_mode по описанию контента.

    :param content: JSON-строка или описание события дня для публикации.
    :param character_profile: Словарь с описанием персонажа.
    """
    # TODO: Здесь должна быть логика формирования текста сообщения и parse_mode по описанию контента
    return content, "HTML"


def creating_pictures(
    content: str,
    character_profile: Mapping[str, object],
) -> Optional[Iterable[ImageInput]]:
    """
    Формирует изображения для публикации по описанию контента.

    :param content: JSON-строка или описание события дня для публикации.
    :param character_profile: Словарь с описанием персонажа.
    """
    # TODO: Здесь должна быть логика формирования изображений по описанию контента
    return None
