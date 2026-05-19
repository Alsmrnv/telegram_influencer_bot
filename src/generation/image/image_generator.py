from __future__ import annotations

from pathlib import Path
from typing import Literal

import torch
from diffusers import DiffusionPipeline
from PIL import Image


AspectName = Literal["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2k"]

class ImageGenerator:
    ASPECT_RATIOS: dict[AspectName, tuple[int, int]] = {
        "1:1": (1328, 1328),
        "16:9": (1280, 720),
        "9:16": (928, 1664),
        "4:3": (1472, 1104),
        "3:4": (1104, 1472),
        "3:2": (1584, 1056),
        "2:3": (1056, 1584),
        "2k": (2560, 1440),
    }

    def __init__(
        self,
        lora_path: str,
        *,
        model_name: str = "Tongyi-MAI/Z-Image-Turbo",
        lora_scale: float = 0.8,
        adapter_name: str = "my_lora",
    ) -> None:
        self.lora_path = lora_path
        self.model_name = model_name
        self.lora_scale = lora_scale
        self.adapter_name = adapter_name
        self._pipe: DiffusionPipeline | None = None

    def _resolve_lora_path(self) -> str:
        p = Path(self.lora_path).expanduser()
        if p.is_file():
            return str(p.resolve())
        cwd = Path.cwd() / p
        if cwd.is_file():
            return str(cwd.resolve())
        here = Path(__file__).resolve().parent / p
        if here.is_file():
            return str(here.resolve())
        return str(p)

    def _load_pipeline(self) -> DiffusionPipeline:
        if not torch.cuda.is_available():
            raise ValueError("CUDA is not available")

        torch_dtype = torch.bfloat16
        device = "cuda"
        print(f"Device: {device}")
        print(f"Loading model from {self.model_name}")

        pipe = DiffusionPipeline.from_pretrained(self.model_name, torch_dtype=torch_dtype).to(device)
        lora_file = self._resolve_lora_path()
        pipe.load_lora_weights(lora_file, adapter_name=self.adapter_name)
        pipe.set_adapters([self.adapter_name], adapter_weights=[self.lora_scale])
        self._pipe = pipe
        return pipe

    @property
    def pipe(self) -> DiffusionPipeline:
        if self._pipe is None:
            self._load_pipeline()
        assert self._pipe is not None
        return self._pipe

    def generate(
        self,
        prompt: str,
        negative_prompt: str,
        *,
        aspect: AspectName = "1:1",
        width: int | None = None,
        height: int | None = None,
        num_inference_steps: int = 50,
        guidance_scale: float = 0.0,
        seed: int = 42,
        output_path: str | Path | None = "example.png",
    ) -> Image.Image:
        if width is not None and height is not None:
            w, h = int(width), int(height)
        else:
            w, h = self.ASPECT_RATIOS[aspect]
        image = self.pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=w,
            height=h,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            generator=torch.Generator(device="cuda").manual_seed(seed),
        ).images[0]
        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path)
        return image


if __name__ == "__main__":
    prompt = "ohwx_borat_jeffrey_v1 in a bikini, huge breasts, high detail"
    negative_prompt = (
        "low resolution, low quality,肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"
    )
    lora_path = "loras/zimage_turbo_lora_a100/zimage_turbo_lora_a100.safetensors"
    gen = ImageGenerator(lora_path=lora_path)
    gen.generate(prompt=prompt, negative_prompt=negative_prompt, output_path="example.png")
