from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_THIS_FILE = Path(__file__).resolve()
# Normal location: <project>/src/tools/tg_publisher/publisher_telethon.py
_PROJECT_ROOT = _THIS_FILE.parents[3] if len(_THIS_FILE.parents) >= 4 else Path.cwd().resolve()
_SRC_ROOT = _PROJECT_ROOT / "src"
for _path in (_PROJECT_ROOT, _SRC_ROOT):
    if _path.exists():
        _path_str = str(_path)
        if _path_str not in sys.path:
            sys.path.insert(0, _path_str)

try:
    from src.tools.env import env_int, env_str, load_tools_env
except ImportError:
    from tools.env import env_int, env_str, load_tools_env

from telethon import Button, TelegramClient, connection, events
from telethon.errors import RPCError

try:
    from src.tools.tg_publisher.models import PendingPost, PendingPostStore
except ImportError:
    from tools.tg_publisher.models import PendingPost, PendingPostStore


ACTION_POST = "post"
ACTION_POST_TEXT = "post_text"
ACTION_POST_IMAGE = "post_image"
ACTION_REJECT = "reject"
KNOWN_ACTIONS = {ACTION_POST, ACTION_POST_TEXT, ACTION_POST_IMAGE, ACTION_REJECT}

DEFAULT_PENDING_DIR = "data/tg_publisher/pending"
DEFAULT_SESSION_NAME = "data/tg_publisher/sessions/review_bot"
DEFAULT_SEND_SESSION_NAME = "data/tg_publisher/sessions/sender"
DEFAULT_CHECK_SESSION_NAME = "data/tg_publisher/sessions/check"

DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 1443
MAX_MEDIA_GROUP = 10
DEFAULT_PENDING_TTL_SECONDS = 7 * 24 * 60 * 60


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


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _log(message: str) -> None:
    if not _bool_env("TG_PUBLISHER_QUIET", False):
        print(f"[tg-publisher] {message}", flush=True)


def _resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (_PROJECT_ROOT / path).resolve()


def _legacy_candidate_dirs(value: str | Path) -> list[Path]:
    """Return possible dirs used by older cwd-relative versions of this file."""
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return [raw]

    candidates = [
        (_PROJECT_ROOT / raw).resolve(),
        (Path.cwd() / raw).resolve(),
        (_SRC_ROOT / raw).resolve(),
        (_PROJECT_ROOT / "src" / raw).resolve(),
    ]

    unique: list[Path] = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    return unique


def _coerce_parse_mode(parse_mode: str | None) -> str | None:
    if parse_mode is None:
        return None
    normalized = parse_mode.strip().lower()
    if normalized in {"html", "htm"}:
        return "html"
    if normalized in {"markdown", "md"}:
        return "md"
    # MarkdownV2 in Bot API is not the same thing as Telethon's markdown parser.
    # Plain text is safer than silently mangling generated text.
    if normalized == "markdownv2":
        return None
    return parse_mode


def _post_parse_mode(post: PendingPost) -> str | None:
    metadata = post.metadata if isinstance(post.metadata, dict) else {}
    value = metadata.get("parse_mode") or metadata.get("telegram_parse_mode")
    if value is None:
        return None
    return _coerce_parse_mode(str(value))


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
    return (
        "<b>Generated post review</b>\n\n"
        f"<b>ID:</b> <code>{html.escape(post.id, quote=False)}</code>\n\n"
        "Use the preview messages above to validate the post before pressing a button."
    )


def _format_media_preview_caption(post: PendingPost) -> str:
    return f"<b>Media preview</b> for post <code>{html.escape(post.id, quote=False)}</code>"


async def _safe_answer(event, text: str | None = None, *, alert: bool = False) -> None:
    """
    Best-effort callback answer.

    Important UX rule for the demo:
    - publish buttons must be acknowledged immediately with a small grey toast;
    - never use alert=True for normal review actions;
    """
    try:
        if text is None:
            await event.answer(cache_time=0)
        else:
            await event.answer(text, alert=alert, cache_time=0)
    except Exception as exc:  # QueryIdInvalidError and similar callback-only failures.
        _log(f"callback answer ignored: {type(exc).__name__}: {exc}")


async def _safe_remove_buttons(event) -> None:
    try:
        await event.edit(buttons=None)
    except Exception as exc:
        # Button cleanup is nice-to-have. Never report this as a post publishing failure.
        _log(f"button cleanup ignored: {type(exc).__name__}: {exc}")


@dataclass(slots=True)
class _LockState:
    action: str
    started_at: float
    completed: bool = False


class PendingPostStorage:
    """
    Tiny local pending-post storage for a small review team.

    It wraps the existing JSON PendingPostStore, adds:
    - a semantic in-process lock per post id;
    - best-effort fallback reads from old cwd-relative pending dirs;
    - TTL cleanup for old JSON pending files and stale locks.
    """

    def __init__(self, root_dir: str | Path, *, ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS) -> None:
        self.root_dir = _resolve_project_path(root_dir)
        self.store = PendingPostStore(self.root_dir)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._candidate_dirs = _legacy_candidate_dirs(root_dir)
        self._guard = threading.RLock()
        self._locks: dict[str, _LockState] = {}
        self.cleanup_old()

    def save(self, post: PendingPost) -> None:
        self.cleanup_old()
        self.store.save(post)

    def load(self, post_id: str) -> PendingPost:
        self.cleanup_old()
        try:
            return self.store.load(post_id)
        except FileNotFoundError as primary_exc:
            for candidate_dir in self._candidate_dirs:
                if candidate_dir == self.root_dir:
                    continue
                candidate = PendingPostStore(candidate_dir)
                try:
                    post = candidate.load(post_id)
                except FileNotFoundError:
                    continue
                # Heal path drift: copy the pending JSON into the canonical project-root dir.
                try:
                    self.store.save(post)
                except Exception as exc:
                    _log(f"could not migrate pending post {post_id}: {type(exc).__name__}: {exc}")
                return post
            raise primary_exc

    def delete(self, post_id: str) -> None:
        for candidate_dir in self._candidate_dirs:
            try:
                PendingPostStore(candidate_dir).delete(post_id)
            except Exception as exc:
                _log(f"pending delete ignored for {candidate_dir}: {type(exc).__name__}: {exc}")

    def try_acquire(self, post_id: str, action: str) -> tuple[bool, str]:
        now = time.time()
        with self._guard:
            self._cleanup_locks_locked(now)
            current = self._locks.get(post_id)
            if current is not None:
                if current.completed:
                    return False, "already handled"
                return False, "already processing"
            self._locks[post_id] = _LockState(action=action, started_at=now)
            return True, ""

    def mark_completed(self, post_id: str) -> None:
        with self._guard:
            state = self._locks.get(post_id)
            if state is None:
                self._locks[post_id] = _LockState(action="completed", started_at=time.time(), completed=True)
            else:
                state.completed = True

    def release(self, post_id: str) -> None:
        with self._guard:
            self._locks.pop(post_id, None)

    def cleanup_old(self) -> None:
        now = time.time()
        cutoff = now - self.ttl_seconds
        for candidate_dir in self._candidate_dirs:
            if not candidate_dir.exists():
                continue
            for path in candidate_dir.glob("*.json"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError as exc:
                    _log(f"pending cleanup ignored for {path}: {type(exc).__name__}: {exc}")
        with self._guard:
            self._cleanup_locks_locked(now)

    def _cleanup_locks_locked(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        stale = [post_id for post_id, state in self._locks.items() if state.started_at < cutoff]
        for post_id in stale:
            self._locks.pop(post_id, None)


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
        pending_ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
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
        self.review_chat_id = ids[0] if ids else None
        self.channel_id = channel_id

        self.pending = PendingPostStorage(pending_dir, ttl_seconds=pending_ttl_seconds)
        # Backward-compatible attribute name for old external code.
        self.store = self.pending.store

        self.session_name = str(_resolve_project_path(session_name))
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

        return TelegramClient(str(session_path), self.api_id, self.api_hash, **kwargs)

    def require_review_chat_ids(self) -> list[str | int]:
        if not self.review_chat_ids:
            raise RuntimeError(
                "TG_REVIEW_CHAT_ID or TG_REVIEW_CHAT_IDS is required for sending posts to review. "
                "Run the bot and send /id in each private chat or project group first."
            )
        return self.review_chat_ids

    def require_review_chat_id(self) -> str | int:
        return self.require_review_chat_ids()[0]

    def require_channel_id(self) -> str | int:
        if self.channel_id is None:
            raise RuntimeError("TG_CHANNEL_ID is required for publishing posts.")
        return self.channel_id

    async def start(self) -> None:
        _log("connecting to Telegram...")
        self.client = self.build_client()
        await self.client.start(bot_token=self.bot_token)
        self._register_handlers()
        _log("connected")

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
            _log("disconnected")

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
        _log(f"saving pending post {post.id} in {self.pending.root_dir}")
        self.pending.save(post)

        existing_images = post.existing_image_paths()
        review_text = _format_review_text(post)
        post_parse_mode = _post_parse_mode(post) or self.parse_mode

        review_chat_ids = self.require_review_chat_ids()
        _log(f"sending post {post.id} for review to {len(review_chat_ids)} chat(s); images={len(existing_images)}")

        for chat_id in review_chat_ids:
            buttons = _review_buttons(post.id)

            if existing_images:
                _log(f"review chat {chat_id}: sending media preview")
                await self.client.send_file(
                    chat_id,
                    file=existing_images if len(existing_images) > 1 else existing_images[0],
                    caption=_format_media_preview_caption(post),
                    parse_mode="html",
                )

            if post.text:
                _log(f"review chat {chat_id}: sending text preview")
                await self.client.send_message(chat_id, post.text, parse_mode=post_parse_mode)

            _log(f"review chat {chat_id}: sending action card")
            await self.client.send_message(chat_id, review_text, buttons=buttons, parse_mode="html")

    async def deliver_for_review(self, post: PendingPost) -> None:
        _log(f"deliver_for_review started for post {post.id}")
        await self.start()
        try:
            await self.send_for_review(post)
        finally:
            await self.stop()
        _log(f"deliver_for_review finished for post {post.id}")

    async def publish_full(self, post: PendingPost) -> None:
        assert self.client is not None
        existing_images = post.existing_image_paths()
        if existing_images:
            await self.client.send_file(
                self.require_channel_id(),
                file=existing_images if len(existing_images) > 1 else existing_images[0],
                caption=post.text or None,
                parse_mode=_post_parse_mode(post) or self.parse_mode,
            )
        else:
            await self.publish_text(post)

    async def publish_text(self, post: PendingPost) -> None:
        assert self.client is not None
        await self.client.send_message(
            self.require_channel_id(),
            post.text or "",
            parse_mode=_post_parse_mode(post) or self.parse_mode,
        )

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
            await _safe_answer(event, "Bad callback data", alert=False)
            return

        action, post_id = data.split(":", 1)
        if action not in KNOWN_ACTIONS:
            await _safe_answer(event, f"Unknown action for post {post_id}: {action}", alert=False)
            return

        # ACK FIRST, WORK LATER.
        #
        # This is the key demo requirement: the first reviewer must immediately see
        # Telegram's small grey toast ("Sent" / "Rejected"), not a blocking alert.  Do this before disk IO,
        # before the local lock, before loading the JSON, and before publishing media.
        ack_text = "Rejected" if action == ACTION_REJECT else "Sent"
        await _safe_answer(event, ack_text, alert=False)

        acquired, lock_message = self.pending.try_acquire(post_id, action)
        if not acquired:
            # The callback was already acknowledged with the neutral grey toast above.
            # Do not answer again with "already handled" text: Telegram clients may show
            # the second answer instead, which is exactly the bad demo UX we avoid.
            _log(f"callback {action} ignored for post {post_id}: {lock_message}")
            await _safe_remove_buttons(event)
            return

        try:
            try:
                post = self.pending.load(post_id)
            except FileNotFoundError:
                self.pending.release(post_id)
                await event.respond(
                    f"Pending post `{post_id}` was not found in `{self.pending.root_dir}`. "
                    "Check TG_PENDING_DIR and make sure the review bot was restarted with the new code.",
                    parse_mode="md",
                )
                return

            _log(f"callback {action} started for post {post.id}")

            if action == ACTION_POST:
                await self.publish_full(post)
                result_text = "published full post"
            elif action == ACTION_POST_TEXT:
                await self.publish_text(post)
                result_text = "published text only"
            elif action == ACTION_POST_IMAGE:
                await self.publish_image(post)
                result_text = "published image only"
            elif action == ACTION_REJECT:
                result_text = "rejected"
            else:  # defensive, action was checked above
                self.pending.release(post_id)
                await event.respond(f"Unknown action `{action}` for post `{post_id}`.", parse_mode="md")
                return

            self.pending.delete(post.id)
            self.pending.mark_completed(post.id)
            await _safe_remove_buttons(event)
            _log(f"callback {action} finished for post {post.id}: {result_text}")

        except (RPCError, OSError, RuntimeError, FileNotFoundError) as exc:
            self.pending.release(post_id)
            _log(f"callback {action} failed for post {post_id}: {type(exc).__name__}: {exc}")
            await event.respond(
                f"Action failed for `{post_id}`: `{type(exc).__name__}: {exc}`",
                parse_mode="md",
            )
        except Exception as exc:
            self.pending.release(post_id)
            _log(f"callback {action} failed for post {post_id}: {type(exc).__name__}: {exc}")
            await event.respond(
                f"Action failed for `{post_id}`: `{type(exc).__name__}: {exc}`",
                parse_mode="md",
            )
            raise

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
        print(f"Pending dir: {self.pending.root_dir}")
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

    ttl_seconds = _int_env("TG_PENDING_TTL_SECONDS", DEFAULT_PENDING_TTL_SECONDS)

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
        pending_ttl_seconds=ttl_seconds,
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