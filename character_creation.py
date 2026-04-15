"""
Создание и хранение персонажа для Telegram-канала о путешествиях.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

try:
    from dotenv import load_dotenv
except ImportError as e:
    raise ImportError("Установите python-dotenv: pip install python-dotenv") from e

try:
    import requests
except ImportError as e:
    raise ImportError("Установите requests: pip install requests") from e

load_dotenv(Path(__file__).resolve().parent / ".env")
_CHARACTER_FILE = Path(__file__).resolve().parent / "character_profile.json"


def _normalize_character_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Нормализует ответ LLM в стабильный формат профиля.
    """
    return {
        "name": str(raw.get("name", "Без имени")),
        "age": int(raw.get("age", 25)),
        "home_city": str(raw.get("home_city", "Неизвестно")),
        "travel_style": str(raw.get("travel_style", "Смешанный")),
        "tone_of_voice": str(raw.get("tone_of_voice", "Дружелюбный")),
        "interests": [str(x) for x in raw.get("interests", [])],
        "goals": [str(x) for x in raw.get("goals", [])],
        "quirks": [str(x) for x in raw.get("quirks", [])],
        "backstory": str(raw.get("backstory", "")),
    }


def generate_character(
    concept: str,
    model: str = "deepseek/deepseek-chat-v3.1",
) -> Dict[str, Any]:
    """
    Генерирует характеристики персонажа через OpenRouter Chat API.
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError(
            "Задайте OPENROUTER_API_KEY в .env"
        )

    url = "https://openrouter.ai/api/v1/chat/completions"

    system_prompt = (
        "Ты создаёшь выдуманную личность для Telegram-канала о путешествиях. "
        "Верни только JSON без Markdown. "
        "Формат JSON: "
        '{"name":"...","age":28,"home_city":"...","travel_style":"...",'
        '"tone_of_voice":"...","interests":["..."],"goals":["..."],'
        '"quirks":["..."],"backstory":"..."}'
    )
    user_prompt = (
        "Сгенерируй персонажа для travel-канала.\n"
        f"Концепт: {concept}\n"
        "Характеристики должны быть правдоподобными, цельными и пригодными "
        "для постоянного ведения канала."
    )

    payload: Dict[str, Any] = {
        "model": model,
        "temperature": 0.9,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        url,
        headers=headers,
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
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM вернул невалидный JSON: {content}") from e

    return _normalize_character_payload(raw)


def save_character(character: Dict[str, Any]) -> Path:
    """
    Сохраняет характеристики персонажа в JSON-файл.
    """
    _CHARACTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CHARACTER_FILE.write_text(
        json.dumps(_normalize_character_payload(character), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return _CHARACTER_FILE


def load_character() -> Dict[str, Any]:
    """
    Возвращает характеристики персонажа из JSON-файла.
    """
    if not _CHARACTER_FILE.is_file():
        raise FileNotFoundError(f"Файл персонажа не найден: {_CHARACTER_FILE}")
    raw = json.loads(_CHARACTER_FILE.read_text(encoding="utf-8"))
    return _normalize_character_payload(raw)


def get_or_create_character(
    concept: str,
    model: str = "deepseek/deepseek-chat-v3.1",
) -> Dict[str, Any]:
    """
    Загружает персонажа из файла или генерирует и сохраняет, если файла нет.
    """
    if _CHARACTER_FILE.is_file():
        return load_character()

    character = generate_character(
        concept=concept,
        model=model,
    )
    save_character(character)
    return character
