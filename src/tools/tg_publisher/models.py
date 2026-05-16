from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
import json
import time
import uuid


@dataclass(slots=True)
class PendingPost:
    id: str
    text: str
    image_paths: list[str]
    created_at: float = 0.0
    metadata: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        text: str,
        image_path: str | Path | None = None,
        image_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "PendingPost":
        paths: list[str] = []
        if image_paths:
            paths.extend(str(path) for path in image_paths if path)
        elif image_path:
            paths.append(str(image_path))

        return cls(
            id=uuid.uuid4().hex,
            text=text.strip(),
            image_paths=paths,
            created_at=time.time(),
            metadata=metadata or {},
        )

    @property
    def image_path(self) -> str | None:
        """Backward-compatible accessor for older one-image code."""
        return self.image_paths[0] if self.image_paths else None

    def existing_image_paths(self) -> list[str]:
        return [str(Path(path)) for path in self.image_paths if Path(path).is_file()]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Keep the legacy key so old pending JSON readers do not break immediately.
        payload["image_path"] = self.image_path
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PendingPost":
        raw_paths = payload.get("image_paths")
        if isinstance(raw_paths, list):
            image_paths = [str(path) for path in raw_paths if path]
        else:
            legacy_path = payload.get("image_path")
            image_paths = [str(legacy_path)] if legacy_path else []

        return cls(
            id=str(payload["id"]),
            text=str(payload.get("text", "")),
            image_paths=image_paths,
            created_at=float(payload.get("created_at", 0.0)),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )


class PendingPostStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, post_id: str) -> Path:
        safe_id = "".join(ch for ch in post_id if ch.isalnum() or ch in {"_", "-"})
        return self.root_dir / f"{safe_id}.json"

    def save(self, post: PendingPost) -> None:
        self.path_for(post.id).write_text(
            json.dumps(post.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, post_id: str) -> PendingPost:
        path = self.path_for(post_id)
        if not path.exists():
            raise FileNotFoundError(f"Pending post not found: {post_id}")
        return PendingPost.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def delete(self, post_id: str) -> None:
        path = self.path_for(post_id)
        if path.exists():
            path.unlink()
