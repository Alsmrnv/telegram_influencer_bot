"""High-level orchestration for the synthetic blogger generation pipeline.

Order of operations:
1) load the text generator
2) generate post text + image prompt
3) unload the text model to free VRAM
4) generate the image from the prompt

This module is intentionally a thin wrapper around:
- text/utils.py public functions
- image/image_generator.py public ImageGenerator class
"""

from __future__ import annotations

import gc
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from image_generator import ImageGenerator


# ---------------------------------------------------------------------------
# Import bootstrap: support both the real repo layout
# src/agents/generation/... and this flattened shared-folder layout.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
for extra_path in (_HERE, _HERE / "text", _HERE / "image"):
    extra_path_str = str(extra_path)
    if extra_path_str not in sys.path:
        sys.path.insert(0, extra_path_str)

try:  # flattened layout
    import utils as text_utils  # type: ignore[import-untyped]
except ImportError:  # repo layout
    from src.agents.generation.text import utils as text_utils  # type: ignore[import-not-found]

# Do NOT import image_generator.py here.
# It imports diffusers/torch/bitsandbytes and may initialize CUDA/Triton at import time.
# The image generator is imported lazily only after the text model has been unloaded.


_DEFAULT_NEGATIVE = (
    "low resolution, low quality, 肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，"
    "过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"
)


def _import_image_generator_class():
    """Import ImageGenerator lazily after text VRAM is released."""
    try:  # flattened layout: src/agents/generation/image is on sys.path
        from image_generator import ImageGenerator  # type: ignore[import-not-found]
    except ImportError:  # repo layout
        from src.agents.generation.image.image_generator import ImageGenerator  # type: ignore[import-not-found]
    return ImageGenerator


@dataclass(slots=True)
class TextStageResult:
    post_text: str
    image_prompt: str


@dataclass(slots=True)
class FullGenerationResult:
    post_text: str
    image_prompt: str
    image: Image.Image
    output_path: str | None = None


class Generator:
    """Top-level API for text+image generation.

    The text model is loaded only for the text stage and is explicitly unloaded
    before the image stage to avoid holding ~30 GB of VRAM unnecessarily.
    """

    def __init__(
        self,
        lora_path: str,
        *,
        text_provider_name: str = "hf_local",
        text_model_name: str | None = None,
        persona_preset: str = "balanced",
        profile_path: str | None = None,
        negative_prompt: str | None = None,
        lora_scale: float = 0.8,
        enable_persona_rewrite: bool = True,
    ) -> None:
        self._negative = negative_prompt if negative_prompt is not None else _DEFAULT_NEGATIVE
        self._lora_path = lora_path
        self._lora_scale = lora_scale
        self._text_provider_name = text_provider_name
        self._text_model_name = text_model_name or text_utils.DEFAULT_MODEL
        self._persona_preset = persona_preset
        self._profile_path = profile_path
        self._enable_persona_rewrite = enable_persona_rewrite
        self._image_generator: Any | None = None

    # ------------------------------------------------------------------
    # Text-stage lifecycle
    # ------------------------------------------------------------------
    def load_text_generator(self) -> Any:
        """Load/reuse the text generator in process memory."""
        return text_utils.load_model(
            provider_name=self._text_provider_name,
            model_name=self._text_model_name,
            persona_preset=self._persona_preset,
            profile_path=self._profile_path,
            enable_persona_rewrite=self._enable_persona_rewrite,
        )

    def unload_text_generator(self) -> None:
        """Unload the text generator and release CUDA memory."""
        text_utils.unload_model()

    def build_text_stage(
        self,
        event: Any,
        *,
        memory: Any = None,
        image_seed: int | None = None,
        generation_config: Any = None,
    ) -> TextStageResult:
        """Generate post text and image prompt while the text model is loaded."""
        self.load_text_generator()
        post_text = text_utils.generate_post(
            event,
            memory=memory,
            provider_name=self._text_provider_name,
            model_name=self._text_model_name,
            persona_preset=self._persona_preset,
            profile_path=self._profile_path,
            generation_config=generation_config,
        )
        image_prompt = text_utils.generate_image_prompt(
            event,
            provider_name=self._text_provider_name,
            model_name=self._text_model_name,
            persona_preset=self._persona_preset,
            profile_path=self._profile_path,
            seed=image_seed,
            anchor_text=post_text,
        )
        return TextStageResult(post_text=post_text, image_prompt=image_prompt)

    # ------------------------------------------------------------------
    # Image-stage lifecycle
    # ------------------------------------------------------------------
    def _get_image_generator(self) -> Any:
        if self._image_generator is None:
            ImageGenerator = _import_image_generator_class()
            self._image_generator = ImageGenerator(
                lora_path=self._lora_path,
                lora_scale=self._lora_scale,
            )
        return self._image_generator

    def unload_image_generator(self) -> None:
        """Best-effort cleanup for the image pipeline without editing image_generator.py."""
        if self._image_generator is None:
            return

        pipe = getattr(self._image_generator, "_pipe", None)
        if pipe is not None:
            try:
                del pipe
            except Exception:
                pass
        self._image_generator._pipe = None  # type: ignore[attr-defined]
        self._image_generator = None
        gc.collect()

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass

    def generate_image(
        self,
        prompt: str,
        *,
        negative_prompt: str | None = None,
        **image_kwargs: Any,
    ) -> Image.Image:
        generator = self._get_image_generator()
        return generator.generate(
            prompt=prompt,
            negative_prompt=negative_prompt if negative_prompt is not None else self._negative,
            **image_kwargs,
        )

    # ------------------------------------------------------------------
    # End-to-end API
    # ------------------------------------------------------------------
    def run_full(
        self,
        event: Any,
        *,
        memory: Any = None,
        image_seed: int | None = None,
        generation_config: Any = None,
        unload_image_after: bool = False,
        **image_kwargs: Any,
    ) -> FullGenerationResult:
        """Run the full pipeline in the required order.

        1. load text generator
        2. generate post text + image prompt
        3. unload text generator
        4. generate image
        """
        text_stage = self.build_text_stage(
            event,
            memory=memory,
            image_seed=image_seed,
            generation_config=generation_config,
        )

        # из-за размера моделек мы не можем держать в памяти обе одновременно
        self.unload_text_generator()

        try:
            image = self.generate_image(text_stage.image_prompt, **image_kwargs)
        finally:
            if unload_image_after:
                self.unload_image_generator()

        output_path = image_kwargs.get("output_path")
        if output_path is not None:
            output_path = str(output_path)

        return FullGenerationResult(
            post_text=text_stage.post_text,
            image_prompt=text_stage.image_prompt,
            image=image,
            output_path=output_path,
        )

    def run(
        self,
        event: Any,
        *,
        memory: Any = None,
        image_seed: int | None = None,
        generation_config: Any = None,
        **image_kwargs: Any,
    ) -> tuple[Image.Image, str]:
        """Backward-compatible wrapper: return (image, post_text)."""
        result = self.run_full(
            event,
            memory=memory,
            image_seed=image_seed,
            generation_config=generation_config,
            **image_kwargs,
        )
        return result.image, result.post_text


# ---------------------------------------------------------------------------
# Convenience module-level helpers
# ---------------------------------------------------------------------------
def unload_text_model() -> None:
    text_utils.unload_model()


def unload_image_model(generator: Generator) -> None:
    generator.unload_image_generator()


def generate_all(
    event: Any,
    *,
    lora_path: str,
    memory: Any = None,
    image_seed: int | None = None,
    text_provider_name: str = "hf_local",
    text_model_name: str | None = None,
    persona_preset: str = "balanced",
    profile_path: str | None = None,
    negative_prompt: str | None = None,
    lora_scale: float = 0.8,
    unload_image_after: bool = False,
    **image_kwargs: Any,
) -> FullGenerationResult:
    """Stateless convenience wrapper around the Generator class."""
    generator = Generator(
        lora_path=lora_path,
        text_provider_name=text_provider_name,
        text_model_name=text_model_name,
        persona_preset=persona_preset,
        profile_path=profile_path,
        negative_prompt=negative_prompt,
        lora_scale=lora_scale,
    )
    return generator.run_full(
        event,
        memory=memory,
        image_seed=image_seed,
        unload_image_after=unload_image_after,
        **image_kwargs,
    )
