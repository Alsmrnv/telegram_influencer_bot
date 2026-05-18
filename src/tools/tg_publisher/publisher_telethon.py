from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _PROJECT_ROOT / "src"
for _path in (_PROJECT_ROOT, _SRC_ROOT):
    if _path.exists():
        _path_str = str(_path)
        if _path_str not in sys.path:
            sys.path.insert(0, _path_str)

import argparse
import asyncio
import json
import os
from typing import Any, Iterable

try:
    from src.tools.env import env_int, env_str, load_tools_env
except ImportError:
    try:
        from tools.env import env_int, env_str, load_tools_env
    except ImportError:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tools.env import env_int, env_str, load_tools_env

from telethon import Button, TelegramClient, events, connection
from telethon.errors import RPCError

try:
    from src.tools.tg_publisher.models import PendingPost, PendingPostStore
except ImportError:
    try:
        from tools.tg_publisher.models import PendingPost, PendingPostStore
    except ImportError:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tools.tg_publisher.models import PendingPost, PendingPostStore


ACTION_POST = "post"
ACTION_POST_TEXT = "post_text"
ACTION_POST_IMAGE = "post_image"
ACTION_REJECT = "reject"

DEFAULT_PENDING_DIR = "data/tg_publisher/pending"
DEFAULT_SESSION_DIR = "data/tg_publisher/sessions"
DEFAULT_SESSION_NAME = "data/tg_publisher/sessions/review_bot"
DEFAULT_SEND_SESSION_NAME = "data/tg_publisher/sessions/sender"
DEFAULT_CHECK_SESSION_NAME = "data/tg_publisher/sessions/check"

DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 1443
MAX_MEDIA_GROUP = 10


def _env(name: str, default: str | None = None) -> str:
    value = env_str(name, default, required=True)
    assert value is not None
    return value


def _optional_env(name: str, default: str | None = None) -> str | None:
    return env_str(name, default)


def _int_env(name: str, default: int | None = None) -> int:
    value = env_int(name, default, required=default is None)
    assert value is not None
    return value


def _callback_data(action: str, post_id: str) -> bytes:
    return f"{action}:{post_id}".encode("utf-8")


def _review_buttons(post_id: str):
    return [
        [
            Button.inline("POST", data=_callback_data(ACTION_POST, post_id)),
            Button.inline("POST TEXT", data=_callback_data(ACTION_POST_TEXT, post_id)),
        ],
        [
            Button.inline("POST IMAGE", data=_callback_data(ACTION_POST_IMAGE, post_id)),
            Button.inline("REJECT", data=_callback_data(ACTION_REJECT, post_id)),
        ],
    ]


def _format_review_text(post: PendingPost) -> str:
    if post.image_paths:
        image_lines = "\n".join(f"- `{path}`" for path in post.image_paths)
    else:
        image_lines = "—"

    return (
        "**Generated post review**\n\n"
        f"**ID:** `{post.id}`\n"
        f"**Images:**\n{image_lines}\n\n"
        f"{post.text}"
    )


def _read_input_payload(args: argparse.Namespace) -> tuple[str, list[str], dict[str, Any]]:
    metadata: dict[str, Any] = {}

    if args.json_file:
        payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        text = str(payload.get("post_text") or payload.get("text") or "").strip()
        raw_images = payload.get("image_paths") or payload.get("images") or payload.get("image_path") or payload.get("image")
        if isinstance(raw_images, (str, Path)):
            image_paths = [str(raw_images)]
        elif isinstance(raw_images, list):
            image_paths = [str(path) for path in raw_images if path]
        else:
            image_paths = []
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if not text:
            raise ValueError("JSON input must contain 'post_text' or 'text'.")
        return text, image_paths, metadata

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text.strip()
    else:
        raise ValueError("Provide --text, --text-file, or --json-file.")

    if not text:
        raise ValueError("Post text is empty.")

    return text, list(args.image or []), metadata


def _coerce_parse_mode(parse_mode: str | None) -> str | None:
    if parse_mode is None:
        return None
    normalized = parse_mode.strip().lower()
    if normalized in {"html", "markdown", "md"}:
        return "md" if normalized == "markdown" else normalized
    # Telegram Bot API supports MarkdownV2, but Telethon's markdown parser is different.
    # Falling back to plain text is safer than mangling generated content.
    if normalized == "markdownv2":
        return None
    return parse_mode


class TgTelethonPublisher:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        bot_token: str,
        review_chat_id: str | int | None = None,
        review_chat_ids: Iterable[str | int] | None = None,
        channel_id: str | int | None = None,
        pending_dir: str | Path = DEFAULT_PENDING_DIR,
        session_name: str = DEFAULT_SESSION_NAME,
        proxy_host: str = DEFAULT_PROXY_HOST,
        proxy_port: int = DEFAULT_PROXY_PORT,
        proxy_secret: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        ids: list[str | int] = []
        if review_chat_ids is not None:
            ids.extend(chat_id for chat_id in review_chat_ids if chat_id is not None)
        if review_chat_id is not None and review_chat_id not in ids:
            ids.append(review_chat_id)
        self.review_chat_ids = ids
        self.review_chat_id = ids[0] if ids else None  # backward-compatible attribute
        self.channel_id = channel_id
        self.store = PendingPostStore(pending_dir)
        self.session_name = session_name
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.proxy_secret = proxy_secret
        self.parse_mode = _coerce_parse_mode(parse_mode)
        self.client: TelegramClient | None = None

    def _proxy(self):
        if not self.proxy_secret:
            return None
        return (self.proxy_host, self.proxy_port, self.proxy_secret)

    def build_client(self) -> TelegramClient:
        session_path = Path(self.session_name).expanduser()
        if session_path.parent != Path("."):
            session_path.parent.mkdir(parents=True, exist_ok=True)

        kwargs: dict[str, Any] = {}
        if self.proxy_secret:
            kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
            kwargs["proxy"] = self._proxy()

        return TelegramClient(
            str(session_path),
            self.api_id,
            self.api_hash,
            **kwargs,
        )

    def require_review_chat_ids(self) -> list[str | int]:
        if not self.review_chat_ids:
            raise RuntimeError(
                "TG_REVIEW_CHAT_ID or TG_REVIEW_CHAT_IDS is required for sending posts to review. "
                "Run the bot and send /id in each private chat or project group first."
            )
        return self.review_chat_ids

    def require_review_chat_id(self) -> str | int:
        # Backward-compatible helper for older code paths.
        return self.require_review_chat_ids()[0]

    def require_channel_id(self) -> str | int:
        if self.channel_id is None:
            raise RuntimeError("TG_CHANNEL_ID is required for publishing posts.")
        return self.channel_id

    async def start(self) -> None:
        self.client = self.build_client()
        await self.client.start(bot_token=self.bot_token)
        self._register_handlers()

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    def _register_handlers(self) -> None:
        assert self.client is not None

        @self.client.on(events.NewMessage(pattern=r"^/start$"))
        async def on_start(event):
            await event.reply("TG Publisher is alive. Use /id to get this chat id.")

        @self.client.on(events.NewMessage(pattern=r"^/id$"))
        async def on_id(event):
            await event.reply(f"chat_id: {event.chat_id}")

        @self.client.on(events.CallbackQuery)
        async def on_callback(event):
            await self.handle_callback(event)

    async def send_for_review(self, post: PendingPost) -> None:
        assert self.client is not None
        self.store.save(post)

        existing_images = post.existing_image_paths()
        review_text = _format_review_text(post)

        for chat_id in self.require_review_chat_ids():
            buttons = _review_buttons(post.id)

            if len(existing_images) == 1:
                await self.client.send_file(
                    chat_id,
                    file=existing_images[0],
                    caption=review_text,
                    buttons=buttons,
                    parse_mode="md",
                )
                continue

            if len(existing_images) > 1:
                # Telegram albums cannot reliably carry inline buttons, so the media preview
                # and the approval controls are sent as two messages.
                await self.client.send_file(
                    chat_id,
                    file=existing_images,
                    caption="Preview media for pending post " + post.id,
                )

            await self.client.send_message(
                chat_id,
                review_text,
                buttons=buttons,
                parse_mode="md",
            )

    async def deliver_for_review(self, post: PendingPost) -> None:
        await self.start()
        try:
            await self.send_for_review(post)
        finally:
            await self.stop()

    async def publish_full(self, post: PendingPost) -> None:
        assert self.client is not None
        existing_images = post.existing_image_paths()
        if existing_images:
            await self.client.send_file(
                self.require_channel_id(),
                file=existing_images if len(existing_images) > 1 else existing_images[0],
                caption=post.text or None,
                parse_mode=self.parse_mode,
            )
        else:
            await self.publish_text(post)

    async def publish_text(self, post: PendingPost) -> None:
        assert self.client is not None
        await self.client.send_message(self.require_channel_id(), post.text or "", parse_mode=self.parse_mode)

    async def publish_image(self, post: PendingPost) -> None:
        assert self.client is not None
        existing_images = post.existing_image_paths()
        if not existing_images:
            raise FileNotFoundError(f"Image files not found for post {post.id}: {post.image_paths}")
        await self.client.send_file(
            self.require_channel_id(),
            file=existing_images if len(existing_images) > 1 else existing_images[0],
        )

    async def deliver_direct(self, post: PendingPost) -> None:
        await self.start()
        try:
            await self.publish_full(post)
        finally:
            await self.stop()

    async def handle_callback(self, event) -> None:
        data = (event.data or b"").decode("utf-8", errors="replace")
        if ":" not in data:
            await event.answer("Bad callback data", alert=True)
            return

        action, post_id = data.split(":", 1)

        try:
            post = self.store.load(post_id)
        except FileNotFoundError:
            await event.answer("Post is already gone or not found", alert=True)
            return

        try:
            if action == ACTION_POST:
                await self.publish_full(post)
                self.store.delete(post.id)
                await event.answer("Published full post")
                await event.edit(buttons=None)
            elif action == ACTION_POST_TEXT:
                await self.publish_text(post)
                self.store.delete(post.id)
                await event.answer("Published text only")
                await event.edit(buttons=None)
            elif action == ACTION_POST_IMAGE:
                await self.publish_image(post)
                self.store.delete(post.id)
                await event.answer("Published image only")
                await event.edit(buttons=None)
            elif action == ACTION_REJECT:
                self.store.delete(post.id)
                await event.answer("Rejected")
                await event.edit(buttons=None)
            else:
                await event.answer(f"Unknown action: {action}", alert=True)

        except (RPCError, OSError, RuntimeError) as exc:
            await event.answer(f"Action failed: {type(exc).__name__}", alert=True)
            await event.respond(f"Action failed for `{post.id}`: `{type(exc).__name__}: {exc}`", parse_mode="md")

    async def check_connection(self) -> None:
        await self.start()
        try:
            assert self.client is not None
            me = await self.client.get_me()
            username = getattr(me, "username", None)
            print(f"Connected as @{username} / id={me.id}")
        finally:
            await self.stop()

    async def run(self) -> None:
        await self.start()
        assert self.client is not None
        me = await self.client.get_me()
        username = getattr(me, "username", None)
        print(f"TG Publisher bot started as @{username} / id={me.id}")
        await self.client.run_until_disconnected()

    async def send_one(
        self,
        *,
        text: str,
        image_path: str | None = None,
        image_paths: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PendingPost:
        paths = list(image_paths or ([] if image_path is None else [image_path]))
        if len(paths) > MAX_MEDIA_GROUP:
            raise ValueError(f"Не больше {MAX_MEDIA_GROUP} изображений за один альбом")
        post = PendingPost.create(text=text, image_paths=paths, metadata=metadata)
        await self.deliver_for_review(post)
        return post


def _coerce_chat_id(value: str | None) -> str | int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("@"):
        return value
    try:
        return int(value)
    except ValueError:
        return value


def _split_chat_ids(raw: str | None) -> list[str | int]:
    if not raw:
        return []
    normalized = raw.replace(";", ",").replace("\n", ",").replace(" ", ",")
    result: list[str | int] = []
    for item in normalized.split(","):
        chat_id = _coerce_chat_id(item)
        if chat_id is not None and chat_id not in result:
            result.append(chat_id)
    return result


def build_bot_from_env(
    *,
    require_review: bool = False,
    require_channel: bool = False,
    session_name: str | None = None,
    parse_mode: str | None = None,
) -> TgTelethonPublisher:
    load_tools_env()
    review = _optional_env("TG_REVIEW_CHAT_ID")
    review_many = _optional_env("TG_REVIEW_CHAT_IDS")
    review_ids = _split_chat_ids(review_many)
    legacy_review = _coerce_chat_id(review)
    if legacy_review is not None and legacy_review not in review_ids:
        review_ids.append(legacy_review)

    channel = _optional_env("TG_CHANNEL_ID")

    if require_review and not review_ids:
        raise RuntimeError("Missing required environment variable: TG_REVIEW_CHAT_ID or TG_REVIEW_CHAT_IDS")
    if require_channel and not channel:
        raise RuntimeError("Missing required environment variable: TG_CHANNEL_ID")

    return TgTelethonPublisher(
        api_id=_int_env("TG_API_ID"),
        api_hash=_env("TG_API_HASH"),
        bot_token=_env("TG_API_KEY"),
        review_chat_ids=review_ids,
        channel_id=_coerce_chat_id(channel),
        pending_dir=_optional_env("TG_PENDING_DIR", DEFAULT_PENDING_DIR) or DEFAULT_PENDING_DIR,
        session_name=session_name or _optional_env("TG_PUBLISHER_SESSION", DEFAULT_SESSION_NAME) or DEFAULT_SESSION_NAME,
        proxy_host=_optional_env("TG_PROXY_HOST", DEFAULT_PROXY_HOST) or DEFAULT_PROXY_HOST,
        proxy_port=_int_env("TG_PROXY_PORT", DEFAULT_PROXY_PORT),
        proxy_secret=_optional_env("TG_PROXY_SECRET"),
        parse_mode=parse_mode,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TG Publisher over Telethon + MTProto proxy.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run bot and wait for review button callbacks.")
    sub.add_parser("check", help="Connect through MTProto and print bot identity.")

    return parser


def main() -> int:
    load_tools_env()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        bot = build_bot_from_env()
        asyncio.run(bot.run())
        return 0

    if args.command == "check":
        check_session = _optional_env("TG_PUBLISHER_CHECK_SESSION", DEFAULT_CHECK_SESSION_NAME) or DEFAULT_CHECK_SESSION_NAME
        bot = build_bot_from_env(session_name=check_session)
        asyncio.run(bot.check_connection())
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
