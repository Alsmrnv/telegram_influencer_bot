from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple

try:
    from dotenv import load_dotenv
except ImportError as e:  # pragma: no cover
    raise ImportError("Установите python-dotenv: pip install python-dotenv") from e

try:
    import requests
except ImportError as e:  # pragma: no cover
    raise ImportError("Установите requests: pip install requests") from e

try:
    from tg.telegram_channel import ImageInput
except ImportError:  # pragma: no cover
    from src.tg.telegram_channel import ImageInput

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(Path.cwd() / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENROUTER_TEXT_MODEL", "deepseek/deepseek-chat-v3.1")
MAX_TELEGRAM_POST_CHARS = int(os.getenv("POST_MAX_CHARS", "1400"))

# Only typographic punctuation is normalized here. The ordinary hyphen "-" is NOT touched,
# so words like "прямо-таки", "по-хорошему" and "ML-стек" stay intact.
PUNCT_TRANSLATION = str.maketrans(
    {
        "—": " - ",
        "–": " - ",
        "−": " - ",
        "‒": " - ",
        "―": " - ",
        "…": "...",
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "’": "'",
        "‘": "'",
        " ": " ",
        " ": " ",
    }
)

# These are not requested from the model anymore, but OpenRouter models sometimes still leak
# Markdown-ish wrappers. Since parse_mode is None, we clean simple balanced wrappers instead of
# forwarding visible markup to Telegram.
MD_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\s*(.*?)\s*```", re.DOTALL)
MD_INLINE_CODE_RE = re.compile(r"`([^`\n]{1,240})`")
MD_BOLD_RE = re.compile(r"(?<!\\)\*\*([^*\n]{1,240})\*\*")
MD_ITALIC_STAR_RE = re.compile(r"(?<![\\*])\*([^*\n]{1,180})\*(?!\*)")
MD_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![\\\w])_([^_\n]{1,180})_(?!\w)")
MD_LINK_RE = re.compile(r"\[([^\]\n]{1,180})\]\(([^)\n]{1,500})\)")
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


class MessageCreationError(RuntimeError):
    """Raised when a Telegram post cannot be generated or validated."""


def _openrouter_headers() -> dict[str, str]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("Задайте OPENROUTER_API_KEY в .env в корне проекта")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _json_or_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _pretty(value: Any) -> str:
    parsed = _json_or_text(value)
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    return str(parsed)


def _chat_json(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.9,
) -> dict[str, Any]:
    payload = {
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

    choices = response.json().get("choices") or []
    if not choices:
        raise MessageCreationError("LLM API вернул пустой choices")

    message = choices[0].get("message", {})
    raw_content = str(message.get("content") or "").strip()
    if not raw_content:
        raise MessageCreationError("LLM API не вернул content")

    try:
        result = json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise MessageCreationError(f"LLM вернул невалидный JSON: {raw_content}") from e

    if not isinstance(result, dict):
        raise MessageCreationError(f"LLM вернул JSON не-объект: {result!r}")
    return result


def _system_prompt() -> str:
    return """
Ты пишешь один пост для Telegram-канала персонажа.

Это не эссе, не отчет, не тревел-гайд и не пересказ входного JSON. Нужен обычный пост человека:
короткая заметка, наблюдение, ворчание, радость, мелкая победа, неловкость, самоирония или история из дня.

Правила:
- пиши от лица персонажа, если профиль этому не противоречит;
- выбери одну понятную реакцию персонажа на событие;
- используй только факты из профиля и события;
- не добавляй новые числа, места, имена, должности, причины и последствия;
- возьми 1-3 детали события, а не весь вход целиком;
- длину выбирай по насыщенности входа: бедный вход = короткий пост, богатый вход = можно 2-3 небольших абзаца;
- показывай характер через словарь, ритм, внимание к деталям и выбор темы;
- не объясняй мораль, тезис, стратегию, структуру расходов, пользу или вывод дня;
- не пиши списки, заголовки, таблицы, хэштеги, рекламные фразы, служебные комментарии;
- не используй HTML или Markdown-разметку.

Верни строго JSON-объект:
{"post": "готовый текст поста"}
""".strip()


def _user_prompt(content: Any, character_profile: Mapping[str, object]) -> str:
    return f"""
Профиль персонажа:
{json.dumps(dict(character_profile), ensure_ascii=False, indent=2)}

Событие дня:
{_pretty(content)}

Напиши готовый пост для канала персонажа.
""".strip()


def _strip_markup_artifacts(value: str) -> str:
    # Preserve the text content if the model accidentally wraps output in code fences.
    value = MD_CODE_FENCE_RE.sub(lambda m: m.group(1).strip(), value)
    value = MD_LINK_RE.sub(lambda m: m.group(1), value)
    value = MD_INLINE_CODE_RE.sub(lambda m: m.group(1), value)
    value = MD_BOLD_RE.sub(lambda m: m.group(1), value)
    value = MD_ITALIC_STAR_RE.sub(lambda m: m.group(1), value)
    value = MD_ITALIC_UNDERSCORE_RE.sub(lambda m: m.group(1), value)
    value = HTML_TAG_RE.sub("", value)
    # Remove escaping before Markdown punctuation if it leaked after cleanup.
    value = value.replace(r"\*", "*").replace(r"\_", "_").replace(r"\`", "`")
    return value


def _normalize_text(text: Any) -> str:
    value = str(text or "").strip().strip('"').strip()
    value = _strip_markup_artifacts(value)
    value = value.translate(PUNCT_TRANSLATION)
    # Normalize spaces around dash only when dash is already separated as punctuation.
    # This intentionally does not match hyphenated words.
    value = re.sub(r"\s+-\s+", " - ", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _validate_post(post: str) -> None:
    if not post:
        raise MessageCreationError("LLM вернул пустой пост")
    if len(post) > MAX_TELEGRAM_POST_CHARS:
        raise MessageCreationError(
            f"Пост слишком длинный: {len(post)} символов, максимум {MAX_TELEGRAM_POST_CHARS}"
        )
    if "```" in post:
        raise MessageCreationError("Пост содержит code block")
    if re.search(r"(?m)^\s*(?:[-*]|•|\d+[.)])\s+", post):
        raise MessageCreationError("Пост похож на список")
    if HTML_TAG_RE.search(post):
        raise MessageCreationError("Пост содержит HTML-разметку")


def creating_message(
    content: str,
    character_profile: Mapping[str, object],
    *,
    model: str = DEFAULT_MODEL,
    parse_mode: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """
    Создаёт текст поста по событию и профилю персонажа.

    Возвращает (post, None): сейчас разметка не используется и дальше по пайплайну
    передаётся обычный plain text.
    """
    last_error: Exception | None = None

    for attempt, temperature in enumerate((0.95, 0.8), start=1):
        try:
            result = _chat_json(
                _system_prompt(),
                _user_prompt(content, character_profile),
                model=model,
                temperature=temperature,
            )
            post = _normalize_text(result.get("post"))
            _validate_post(post)
            return post, None
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                break

    raise MessageCreationError(f"Не удалось сгенерировать пост: {last_error}")


def creating_pictures(
    content: str,
    character_profile: Mapping[str, object],
) -> Optional[Iterable[ImageInput]]:
    """Заглушка для будущей генерации изображений."""
    return None


TEST_CASES: list[dict[str, Any]] = [
    {
        "name": "travel_analyst_budapest",
        "character_profile": {
            "name": "Марк Орлов",
            "age": 32,
            "home_city": "Москва",
            "travel_style": "системный аналитический подход с акцентом на эффективность, бюджет и комфорт",
            "tone_of_voice": "сдержанный, информативный, структурированный, без лишних эмоций, с четкими выводами",
            "interests": [
                "статистика путешествий",
                "оптимизация маршрутов",
                "экономика туризма",
                "урбанистика",
                "исторический контекст мест",
            ],
            "goals": [
                "создать базу данных объективных оценок направлений",
                "помочь путешественникам принимать взвешенные решения",
                "разработать алгоритмы для идеального планирования поездок",
            ],
            "quirks": [
                "всегда считает стоимость поездки до копейки",
                "сравнивает отели по соотношению цена/качество в Excel",
                "предпочитает общественный транспорт такси для анализа инфраструктуры",
            ],
            "backstory": "Бывший финансовый аналитик, который устал от офиса и превратил любовь к цифрам и путешествиям в личный канал.",
        },
        "content": {
            "destination": {
                "name": "Будапешт",
                "country": "Венгрия",
                "why_trending": "недорогой европейский город для коротких поездок",
            },
            "location": "Будапешт",
            "events": ["много ходил пешком", "вечером зашел в термальные купальни"],
            "actions": ["сравнил дневной проездной и разовые билеты", "несколько раз отказался от такси"],
            "observations": ["город удобно смотреть пешком", "общественный транспорт закрывает основные точки"],
            "result": "день вышел дешевле и спокойнее, чем ожидалось",
        },
    },
    {
        "name": "old_fisherman_baikal_fine",
        "character_profile": {
            "name": "Семён Петрович",
            "age": 67,
            "home_city": "Иркутск",
            "tone_of_voice": "ворчливый, тёплый, с короткими фразами, без пафоса",
            "interests": ["рыбалка", "старые лодочные моторы", "погода", "байкальские истории"],
            "quirks": [
                "разговаривает с озером как с живым",
                "ругает правила, но в итоге их соблюдает",
                "любит бытовые детали про снасти",
            ],
            "backstory": "Старый рыбак, который всю жизнь ездит на Байкал и считает, что раньше всё было проще.",
        },
        "content": {
            "destination": {"name": "озеро Байкал", "country": "Россия"},
            "location": "берег Байкала",
            "events": [
                "приехал рано утром на рыбалку",
                "пытался ловить мальков у берега",
                "получил штраф от инспектора",
            ],
            "actions": ["достал старую удочку", "спорил с инспектором про правила", "убрал снасти после штрафа"],
            "observations": [
                "вода была прозрачная",
                "мальки стояли у самой кромки",
                "инспектор говорил спокойно, но уверенно",
            ],
            "facts": ["ловля мальков в этом месте запрещена", "штраф выписали на месте"],
            "result": "уехал без улова, но с квитанцией",
        },
    },
    {
        "name": "hse_phystech_deputy_director_mobile",
        "character_profile": {
            "name": "Игорь Валерьевич",
            "age": 44,
            "home_city": "Москва",
            "role": "заместитель директора в Физтех-школе ВШПИ",
            "tone_of_voice": "ироничный, управленческий, слегка злорадный, но не карикатурный",
            "interests": [
                "образовательные траектории",
                "нагрузка преподавателей",
                "карьерные треки студентов",
                "таблицы распределения",
            ],
            "goals": [
                "закрыть проблемные направления студентами",
                "сделать вид, что всё было стратегическим решением",
            ],
            "quirks": [
                "потирает руки, когда план сходится",
                "называет хаос гибким управлением",
                "любит слово перераспределение",
            ],
            "backstory": "Администратор, который слишком хорошо понял, что студентов можно двигать между треками почти как ресурсы в расписании.",
        },
        "content": {
            "location": "Физтех-школа ВШПИ",
            "events": [
                "трёх студентов из ML-стека перевели на мобильную разработку",
                "в мобильном треке не хватало людей на проект",
                "в расписании освободились спорные слоты",
            ],
            "actions": [
                "подписал перераспределение",
                "отправил студентам письмо с нейтральной формулировкой",
                "отметил изменения в таблице",
            ],
            "observations": [
                "студенты сначала думали, что это временно",
                "куратор мобильного трека заметно оживился",
            ],
            "facts": [
                "перевод касается трёх студентов",
                "исходный трек студентов - ML",
                "новый трек - мобильная разработка",
            ],
            "result": "мобильный проект получил команду, ML-стек стал чуть тише",
        },
    },
    {
        "name": "minimal_food_blogger_failed_soup",
        "character_profile": {
            "name": "Лена Морковь",
            "tone_of_voice": "быстрая, эмоциональная, самоироничная",
            "quirks": ["всё сравнивает с супом", "делает вид, что провалы - это концепция"],
        },
        "content": {
            "location": "домашняя кухня",
            "events": ["готовила тыквенный суп", "пересолила"],
            "observations": ["цвет получился красивый", "вкус напоминал море"],
            "result": "суп ушёл в статус соуса",
        },
    },
    {
        "name": "museum_guard_new_exhibit",
        "character_profile": {
            "name": "Аркадий",
            "age": 58,
            "home_city": "Санкт-Петербург",
            "role": "смотритель в небольшом музее",
            "tone_of_voice": "сдержанный, сухой, наблюдательный, с неожиданной нежностью",
            "interests": ["тишина в залах", "посетители, которые читают таблички", "старые рамы"],
            "goals": ["чтобы люди не трогали экспонаты", "чтобы новый зал не превратили в фотозону"],
            "quirks": [
                "запоминает посетителей по обуви",
                "раздражается на громкий шёпот",
                "уважает тех, кто смотрит дольше минуты",
            ],
            "backstory": "Работает в музее много лет и делает вид, что устал от людей, хотя на самом деле внимательно за ними следит.",
        },
        "content": {
            "destination": {"name": "зал северного модерна", "country": "Россия"},
            "location": "малый музейный корпус",
            "events": [
                "открыли новый зал",
                "первые посетители пришли сразу после обеда",
                "один школьник долго рассматривал маленький эскиз в углу",
            ],
            "actions": [
                "поправил табличку у входа",
                "три раза попросил не прислоняться к витрине",
                "посоветовал паре начать осмотр с правой стены",
            ],
            "observations": [
                "в новом зале стало тише, чем в основном",
                "люди сначала фотографировали большую работу, а потом замечали эскизы",
                "на полу снова появились мокрые следы от обуви",
            ],
            "facts": ["экспозиция временная", "в зале есть эскизы и большая центральная работа"],
            "result": "зал пережил первый день без отпечатков пальцев на стекле",
        },
    },
    {
        "name": "sparse_courier_rain",
        "character_profile": {
            "name": "Даня",
            "age": 23,
            "tone_of_voice": "коротко, устало, смешно, без литературности",
            "quirks": ["считает лужи личными врагами", "пишет так, будто рассказывает другу в голосовом"],
        },
        "content": {
            "location": "город после дождя",
            "events": ["развозил заказы под дождём", "пакет с раменом остался цел"],
            "result": "промок сам, еда доехала нормально",
        },
    },
]


if __name__ == "__main__":
    for case in TEST_CASES:
        print("\n" + "=" * 80)
        print("[CASE]", case["name"])
        text, mode = creating_message(
            json.dumps(case["content"], ensure_ascii=False),
            case["character_profile"],
        )
        print("[PARSE_MODE]", mode)
        print(text)
