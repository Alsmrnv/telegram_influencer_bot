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
try:
    from character.character_memory import build_memory_context
except ImportError:  # pragma: no cover
    from src.character.character_memory import build_memory_context

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(Path.cwd() / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENROUTER_TEXT_MODEL", "deepseek/deepseek-chat-v3.1")
MAX_TELEGRAM_POST_CHARS = int(os.getenv("POST_MAX_CHARS", "1400"))

LORA_CHARACTER_TRIGGER = "ohwx_borat_jeffrey_v1"
DEFAULT_IMAGE_LORA_PATH = os.getenv(
    "IMAGE_LORA_PATH",
    "src/generation/image/loras/zimage_turbo_lora_a100/zimage_turbo_lora_a100.safetensors",
)
DEFAULT_IMAGE_OUTPUT_PATH = os.getenv("POST_IMAGE_OUTPUT_PATH", "image.png")
DEFAULT_IMAGE_ASPECT = os.getenv("POST_IMAGE_ASPECT", "1:1")
DEFAULT_IMAGE_SEED = int(os.getenv("POST_IMAGE_SEED", "42"))
DEFAULT_IMAGE_STEPS = int(os.getenv("POST_IMAGE_STEPS", "50"))
DEFAULT_IMAGE_GUIDANCE_SCALE = float(os.getenv("POST_IMAGE_GUIDANCE_SCALE", "0.0"))
DEFAULT_IMAGE_NEGATIVE_PROMPT = os.getenv(
    "POST_IMAGE_NEGATIVE_PROMPT",
    "low resolution, low quality, bad anatomy, deformed hands, extra fingers, "
    "oversaturated, waxy skin, plastic face, no facial details, overly smooth, "
    "AI-looking image, messy composition, blurry text, distorted text, watermark, logo",
)

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


def _user_prompt(
    content: Any,
    character_profile: Mapping[str, object],
    memory_context: str = "",
) -> str:
    memory_block = f"\n\n{memory_context}\n" if memory_context.strip() else ""
    return f"""
Профиль персонажа:
{json.dumps(dict(character_profile), ensure_ascii=False, indent=2)}

Событие дня:
{_pretty(content)}
{memory_block}

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
    memory_context: str = "",
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
                _user_prompt(content, character_profile, memory_context=memory_context),
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


def _image_system_prompt() -> str:
    return f"""
Ты пишешь промпт для генератора изображений по посту Telegram-персонажа.

Нужно выбрать ОДИН визуальный сюжет из события и превратить его в обычный image-generation prompt:
- prompt должен быть на английском;
- prompt должен начинаться с точного LoRA-триггера: {LORA_CHARACTER_TRIGGER};
- в prompt должен быть один главный персонаж, одна сцена и одно действие/состояние;
- используй только факты из профиля и события, не выдумывай новые места, предметы, людей, бренды и текст на картинке;
- не пытайся пересказать всё событие, выбери самый визуальный момент;
- добавь перечисление видимых деталей: character, setting, action, key objects, mood, composition, lighting, camera/framing, image quality;
- не добавляй надписи, субтитры, мемный текст, speech bubbles, watermark или логотипы;
- не делай NSFW, насилие, кровь, политическую агитацию или оскорбительные карикатуры.

Верни строго JSON-объект:
{{
  "plot": "кратко по-русски, какой сюжет выбран",
  "prompt": "готовый английский prompt для генератора",
  "negative_prompt": "английский negative prompt, можно дополнить стандартный"
}}
""".strip()


def _image_user_prompt(content: Any, character_profile: Mapping[str, object]) -> str:
    return f"""
Профиль персонажа:
{json.dumps(dict(character_profile), ensure_ascii=False, indent=2)}

Событие дня:
{_pretty(content)}

Выбери один визуальный сюжет и напиши prompt для генерации картинки.
""".strip()


def _normalize_image_prompt(prompt: Any) -> str:
    value = _normalize_text(prompt)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        raise MessageCreationError("LLM вернул пустой image prompt")
    if LORA_CHARACTER_TRIGGER not in value:
        value = f"{LORA_CHARACTER_TRIGGER}, {value}"
    elif not value.startswith(LORA_CHARACTER_TRIGGER):
        value = re.sub(rf"\b{re.escape(LORA_CHARACTER_TRIGGER)}\b\s*,?\s*", "", value).strip()
        value = f"{LORA_CHARACTER_TRIGGER}, {value}"
    return value


def _normalize_negative_prompt(negative_prompt: Any) -> str:
    custom = _normalize_text(negative_prompt)
    parts = [DEFAULT_IMAGE_NEGATIVE_PROMPT]
    if custom:
        parts.append(custom)
    return ", ".join(part.strip().strip(",") for part in parts if part and part.strip())


def _resolve_project_path(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return (_PROJECT_ROOT / p).resolve()


def _load_image_generator(*, lora_path: str | Path = DEFAULT_IMAGE_LORA_PATH):
    try:
        from generation.image.image_generator import ImageGenerator
    except ImportError:  # pragma: no cover
        from src.generation.image.image_generator import ImageGenerator

    return ImageGenerator(lora_path=str(_resolve_project_path(lora_path)))


def creating_image_prompt(
    content: str,
    character_profile: Mapping[str, object],
    *,
    model: str = DEFAULT_MODEL,
) -> tuple[str, str]:
    """
    Создаёт prompt/negative_prompt для image generator через OpenRouter.

    Агент выбирает один визуальный сюжет из content и обязательно вставляет
    LoRA-триггер персонажа LORA_CHARACTER_TRIGGER в начало prompt.
    """
    result = _chat_json(
        _image_system_prompt(),
        _image_user_prompt(content, character_profile),
        model=model,
        temperature=0.55,
    )
    prompt = _normalize_image_prompt(result.get("prompt"))
    negative_prompt = _normalize_negative_prompt(result.get("negative_prompt"))

    print("\n[IMAGE PROMPT GENERATOR]")
    print("[PROMPT]", prompt)
    print("[NEGATIVE_PROMPT]", negative_prompt)
    print("[/IMAGE PROMPT GENERATOR]\n")

    return prompt, negative_prompt


def creating_pictures(
    content: str,
    character_profile: Mapping[str, object],
    *,
    model: str = DEFAULT_MODEL,
    output_path: str | Path = DEFAULT_IMAGE_OUTPUT_PATH,
    lora_path: str | Path = DEFAULT_IMAGE_LORA_PATH,
    aspect: str = DEFAULT_IMAGE_ASPECT,
    seed: int = DEFAULT_IMAGE_SEED,
) -> Optional[Iterable[ImageInput]]:
    """
    Генерирует одну картинку к посту и возвращает список путей для publish_to_channel.

    Пайплайн:
    1. OpenRouter выбирает один визуальный сюжет и пишет prompt.
    2. Локальный ImageGenerator с LoRA рисует картинку в output_path.
    3. Возвращается [output_path], потому что Telegram publisher уже умеет принимать paths.
    """
    prompt, negative_prompt = creating_image_prompt(
        content,
        character_profile,
        model=model,
    )

    out = Path(output_path).expanduser()
    if not out.is_absolute():
        out = Path.cwd() / out
    out.parent.mkdir(parents=True, exist_ok=True)

    generator = _load_image_generator(lora_path=lora_path)
    generator.generate(
        prompt=prompt,
        negative_prompt=negative_prompt,
        aspect=aspect,  # type: ignore[arg-type]
        num_inference_steps=DEFAULT_IMAGE_STEPS,
        guidance_scale=DEFAULT_IMAGE_GUIDANCE_SCALE,
        seed=seed,
        output_path=out,
    )
    return [str(out)]


def build_character_memory(
    content: str,
    character_profile: Mapping[str, object],
) -> str:
    """Builds optional memory context for post generation."""
    return build_memory_context(character_profile, current_event=content)
