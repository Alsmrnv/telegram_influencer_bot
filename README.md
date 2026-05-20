# telegram_influencer_bot

Автономный Telegram-инфлюенсер: LLM создаёт персонажа, планирует travel-контент на неделю, генерирует посты и картинки, публикует в канал — с опциональным ревью через бота.

## Что делает сервис

1. **Персонаж** — при первом запуске OpenRouter генерирует профиль блогера и сохраняет его в `src/character/character_profile.json`. При следующих запусках профиль переиспользуется.
2. **Недельный план** — LLM выбирает трендовое направление и 3 слота публикаций в текущей календарной неделе (понедельник–воскресенье).
3. **Контент** — для каждого слота: текст поста (OpenRouter), промпт и изображение (OpenRouter + локальный `ImageGenerator` с LoRA на CUDA).
4. **Публикация** — Telethon-бот отправляет пост в канал или на ревью ревьюерам (кнопки «Опубликовать» / «Отклонить»).
5. **Цикл** — после недели сервис ждёт до понедельника 00:00 и повторяет планирование.

Концепт персонажа задаётся в `src/main.py` (сейчас — сибирский рыбак-наставник; можно изменить перед запуском).

## Архитектура

```text
main.py
  ├─ character/          → профиль персонажа (OpenRouter)
  ├─ post_creation/      → план, текст, промпт картинки (OpenRouter)
  ├─ generation/image/   → локальная генерация (Z-Image-Turbo + LoRA, CUDA)
  ├─ tg/                 → publish_to_channel (обёртка над publisher)
  └─ tools/tg_publisher/ → Telethon-бот ревью и отправки в канал
```

```text
Telethon ──► MTProto proxy (tg-ws-proxy, 127.0.0.1:1443) ──► Telegram
```

## Требования

- Python 3.10+
- [OpenRouter](https://openrouter.ai/) API-ключ
- Telegram: `api_id` / `api_hash` с [my.telegram.org](https://my.telegram.org/apps), токен бота от [@BotFather](https://t.me/BotFather)
- Канал, куда бот может публиковать (добавьте бота админом)
- Для генерации картинок: NVIDIA GPU с CUDA, `torch`, `diffusers` (см. раздел «Генерация изображений»)
- Для стабильного MTProto из РФ/ограниченных сетей: локальный [tg-ws-proxy](https://github.com/Flowseal/tg-ws-proxy)

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Для генерации изображений дополнительно установите PyTorch и diffusers под вашу CUDA-сборку (в `requirements.txt` их нет).

## Конфигурация

Секреты не коммитятся (см. `.gitignore`). Создайте файлы по образцу ниже.

### Корень проекта — `.env`

Используется модулями `post_creation` и `character`:

```env
OPENROUTER_API_KEY=sk-or-v1-...

# опционально
OPENROUTER_TEXT_MODEL=deepseek/deepseek-chat-v3.1
POST_MAX_CHARS=1400
```

Дублировать ключ можно в `src/character/.env`, если запускаете только модуль персонажа.

### Telegram и publisher — `src/tools/.env.tools`

Основной конфиг для Telethon (ищется также как `tools/.env.tools` относительно cwd). Переменные подхватывает `src/tools/env.py`.

```env
# Telegram API (my.telegram.org)
TG_API_ID=12345678
TG_API_HASH=your_api_hash
TG_API_KEY=1234567890:AA...   # токен бота

# Канал (@username или числовой id)
TG_CHANNEL_ID=@your_channel

# Ревьюеры (chat_id через запятую; узнать: /id у publisher-бота)
TG_REVIEW_CHAT_IDS=123456789,987654321

# Режим публикации: review | direct | auto
TG_PUBLISH_MODE=review

# Запускать publisher-бота в том же процессе, что и main.py
TG_PUBLISHER_RUN_WITH_SERVICE=1

# MTProto proxy (tg-ws-proxy)
TG_PROXY_HOST=127.0.0.1
TG_PROXY_PORT=1443
TG_PROXY_SECRET=32_hex_chars_without_spaces
```

| Переменная | Описание |
|------------|----------|
| `TG_PUBLISH_MODE=review` | Превью поста ревьюерам с кнопками |
| `TG_PUBLISH_MODE=direct` | Сразу в `TG_CHANNEL_ID` |
| `TG_PUBLISH_MODE=auto` | Ревью, если задан `TG_REVIEW_CHAT_ID(S)`, иначе direct |
| `TG_PUBLISHER_RUN_WITH_SERVICE=0` | Не поднимать бота в `main.py` (запускайте отдельно) |
| `TG_MEDIA_DIR` | Каталог временных медиа (по умолчанию `data/tg_publisher/media`) |
| `IMAGE_LORA_PATH` | Путь к LoRA для персонажа на картинках |
| `POST_IMAGE_*` | Сид, aspect, steps и т.д. — см. `content_creating.py` |

Альтернатива: `export TOOLS_ENV=/path/to/.env.tools`.

## Запуск

### Основной сервис (планирование + контент + publisher в одном процессе)

```bash
cd src
PYTHONPATH=character:post_creation:tg:tools python main.py
```

Из корня репозитория:

```bash
PYTHONPATH=src python -m src.main
```

При старте поднимается фоновый поток publisher-бота (если заданы `TG_API_ID`, `TG_API_HASH`, `TG_API_KEY` и `TG_PUBLISHER_RUN_WITH_SERVICE=1`).

### Publisher-бот отдельно

Проверка подключения через proxy:

```bash
python -m src.tools.tg_publisher.publisher_telethon check
```

Запуск бота ревью:

```bash
python -m src.tools.tg_publisher.publisher_telethon run
```

Из каталога `src`:

```bash
python -m tools.tg_publisher.publisher_telethon run
```

Сессии и очередь постов: `data/tg_publisher/` (в `.gitignore`).

## Ревью постов

1. Запустите publisher-бот (`run` или основной сервис).
2. Напишите боту `/id` — получите свой `chat_id`.
3. Добавьте id в `TG_REVIEW_CHAT_IDS` в `.env.tools` и перезапустите сервис.
4. При `TG_PUBLISH_MODE=review` в личку придёт превью с кнопками публикации в канал или отклонения.

## Генерация изображений

Пайплайн в `creating_pictures`:

1. OpenRouter формирует prompt по сюжету дня.
2. `ImageGenerator` (модель `Tongyi-MAI/Z-Image-Turbo` + LoRA) рисует файл (по умолчанию `image.png` в cwd).

Нужны CUDA и файл LoRA (путь — `IMAGE_LORA_PATH`, trigger в коде — `LORA_CHARACTER_TRIGGER`). Обучение LoRA: `src/generation/image/train_lora.py` и конфиги в `src/generation/image/lora-config/`.

Без GPU генерация картинок упадёт; текстовые посты и планирование работают только с OpenRouter.
