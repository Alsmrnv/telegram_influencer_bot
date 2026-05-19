from __future__ import annotations

import asyncio
import os
import threading

try:
    from character.character_creation import get_or_create_character
    from post_creation.content_planning import run_weekly_content_cycle, sleep_until_next_week_start
except ImportError:
    from src.character.character_creation import get_or_create_character
    from src.post_creation.content_planning import run_weekly_content_cycle, sleep_until_next_week_start


def _load_tools_env() -> None:
    try:
        from tools.env import load_tools_env
    except ImportError:
        from src.tools.env import load_tools_env

    load_tools_env()


def _env_enabled(name: str, default: str = "1") -> bool:
    value = os.getenv(name, default).strip().lower()
    return value not in {"0", "false", "no", "off"}


def start_tg_publisher_bot_with_service() -> threading.Thread | None:
    """Starts the review bot in the same process as the main service."""
    _load_tools_env()

    if not _env_enabled("TG_PUBLISHER_RUN_WITH_SERVICE", "1"):
        return None

    required = ("TG_API_ID", "TG_API_HASH", "TG_API_KEY")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print("TG publisher bot is not started; missing env: " + ", ".join(missing))
        return None

    try:
        from tools.tg_publisher.publisher_telethon import build_bot_from_env
    except ImportError:
        from src.tools.tg_publisher.publisher_telethon import build_bot_from_env

    def runner() -> None:
        bot = build_bot_from_env()
        asyncio.run(bot.run())

    thread = threading.Thread(target=runner, name="tg-publisher-bot", daemon=True)
    thread.start()
    return thread


def main():
    start_tg_publisher_bot_with_service()

    profile = get_or_create_character(
        concept="Опытный рыбак и наставник из Сибири: спокойные выезды к озёрам, "
        "заметки о снастях и погоде, тёплый разговорный тон без пафоса."
    )
    first_cycle = True
    while True:
        if not first_cycle:
            sleep_until_next_week_start()
        first_cycle = False
        run_weekly_content_cycle(profile)


if __name__ == "__main__":
    main()
