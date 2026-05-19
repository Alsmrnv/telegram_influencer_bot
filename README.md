# telegram_influencer_bot

Telegram-бот, который создаёт персонажа (через LLM), планирует публикации на неделю и постит контент в канал.

## Требования

- Python 3.10+
- Токен бота и ID канала в Telegram
- API-ключ [OpenRouter](https://openrouter.ai/)

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Создайте `.env` в каталогах модулей:

`src/tg/.env`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHANNEL_ID=
```

`src/character/.env`:

```env
OPENROUTER_API_KEY=
```

## Запуск

```bash
cd src
PYTHONPATH=character:post_creation:tg python main.py
```

Профиль персонажа сохраняется в `src/character/character_profile.json`.
