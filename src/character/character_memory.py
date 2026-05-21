from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_MEMORY_DIR = Path(__file__).resolve().parent / "memory_store"
_WORD_RE = re.compile(r"[A-Za-zА-Яа-я0-9_]+")


@dataclass(frozen=True)
class MemoryReference:
    published_at: str
    summary: str
    note: str


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "character"


def _character_id(character_profile: Mapping[str, object]) -> str:
    name = _safe_text(character_profile.get("name"))
    home_city = _safe_text(character_profile.get("home_city"))
    return _slugify(f"{name}_{home_city}")


def _memory_file(character_profile: Mapping[str, object]) -> Path:
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return _MEMORY_DIR / f"{_character_id(character_profile)}.jsonl"


def _tokenize(text: str) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(text) if len(token) > 2}


def _event_payload(event: str) -> dict[str, Any]:
    try:
        payload = json.loads(event)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"raw_event": event}


def _extract_tags(payload: Mapping[str, Any]) -> list[str]:
    tags: set[str] = set()
    for key in ("location", "result"):
        tags.update(_tokenize(_safe_text(payload.get(key))))
    for key in ("events", "actions", "observations", "facts"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                tags.update(_tokenize(_safe_text(item)))
        else:
            tags.update(_tokenize(_safe_text(value)))
    return sorted(tags)[:24]


def _build_summary(post_text: str, payload: Mapping[str, Any]) -> str:
    location = _safe_text(payload.get("location"))
    result = _safe_text(payload.get("result"))
    lead = _safe_text(post_text.split("\n", 1)[0])
    if len(lead) > 140:
        lead = lead[:137].rstrip() + "..."

    parts = [part for part in (location, result) if part]
    if parts:
        return f"{' - '.join(parts)}. {lead}".strip()
    return lead


def _load_entries(character_profile: Mapping[str, object]) -> list[dict[str, Any]]:
    file_path = _memory_file(character_profile)
    if not file_path.is_file():
        return []

    entries: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                entries.append(item)
    return entries


def register_published_post(
    character_profile: Mapping[str, object],
    *,
    post_text: str,
    source_event: str,
    published_at: datetime | None = None,
) -> None:
    payload = _event_payload(source_event)
    timestamp = (published_at or datetime.now()).replace(microsecond=0).isoformat()
    summary = _build_summary(post_text, payload)
    tags = _extract_tags(payload)
    digest = hashlib.sha1(post_text.encode("utf-8")).hexdigest()[:12]

    record = {
        "id": digest,
        "published_at": timestamp,
        "summary": summary,
        "post_text": _safe_text(post_text),
        "tags": tags,
    }

    file_path = _memory_file(character_profile)
    with file_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_memory_context(
    character_profile: Mapping[str, object],
    *,
    current_event: str,
    max_items: int = 3,
) -> str:
    entries = _load_entries(character_profile)
    if not entries:
        return ""

    current_payload = _event_payload(current_event)
    current_tokens = _tokenize(json.dumps(current_payload, ensure_ascii=False))
    if not current_tokens:
        current_tokens = _tokenize(current_event)

    scored: list[tuple[int, dict[str, Any]]] = []
    for item in entries[-80:]:
        tags = item.get("tags")
        if isinstance(tags, list):
            item_tokens = {str(t).lower() for t in tags if str(t).strip()}
        else:
            item_tokens = _tokenize(_safe_text(item.get("summary")))
        score = len(current_tokens & item_tokens)
        if score <= 0:
            continue
        scored.append((score, item))

    ranked = [item for _, item in sorted(scored, key=lambda row: row[0], reverse=True)]
    if not ranked:
        ranked = list(reversed(entries[-10:]))

    selected = ranked[: max(1, max_items)]
    if not selected:
        return ""

    # around 35% of posts get explicit callback prompt
    gate_source = f"{current_event}|{len(entries)}"
    gate = int(hashlib.md5(gate_source.encode("utf-8")).hexdigest()[:8], 16) % 100
    should_reference = gate < 35

    lines = []
    for item in selected:
        published_at = _safe_text(item.get("published_at"))
        summary = _safe_text(item.get("summary"))
        if not summary:
            continue
        lines.append(f"- [{published_at}] {summary}")

    if not lines:
        return ""

    if should_reference:
        hint = (
            "Можно добавить одну короткую ремарку-ссылку на один из прошлых постов "
            "(без дат и формального тона), если это звучит естественно."
        )
    else:
        hint = "Ремарка о прошлом посте не обязательна: пиши только если это органично."

    return "Память персонажа:\n" + "\n".join(lines) + f"\n\nПодсказка:\n{hint}"
