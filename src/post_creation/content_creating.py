from typing import Iterable, Optional, Tuple
from tg.telegram_channel import ImageInput


def creating_message(content: str) -> Tuple[str, Optional[str]]:
    """
    Заглушка для формирования текста сообщения и parse_mode.
    """
    # TODO: Здесь должна быть логика формирования текста сообщения и parse_mode по описанию контента
    return content, "HTML"


def creating_pictures(content: str) -> Optional[Iterable[ImageInput]]:
    """
    Заглушка для формирования изображений.
    """
    # TODO: Здесь должна быть логика формирования изображений по описанию контента
    return None
