from diffusers import DiffusionPipeline
import torch

def generate_image(prompt: str, negative_prompt: str, lora_path: str):
    model_name = "Tongyi-MAI/Z-Image-Turbo"

    # Load the pipeline
    if torch.cuda.is_available():
        torch_dtype = torch.bfloat16
        device = "cuda"
    else:
        raise ValueError("CUDA is not available")

    print(f"Device: {device}")
    print(f"Loading model from {model_name}")

    pipe = DiffusionPipeline.from_pretrained(model_name, torch_dtype=torch_dtype).to(device)

    # LoRA
    pipe.load_lora_weights(lora_path, adapter_name="my_lora")
    pipe.set_adapters(["my_lora"], adapter_weights=[0.8])

    # Generate with different aspect ratios
    aspect_ratios = {
        "1:1": (1328, 1328),
        "16:9": (1280, 720),
        "9:16": (928, 1664),
        "4:3": (1472, 1104),
        "3:4": (1104, 1472),
        "3:2": (1584, 1056),
        "2:3": (1056, 1584),
        "2k": (2560, 1440),
    }

    width, height = aspect_ratios["2k"]

    image = pipe(
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        num_inference_steps=50,
        # true_cfg_scale=4.0,
        generator=torch.Generator(device="cuda").manual_seed(42)
    ).images[0]

    image.save("example.png")

if __name__ == "__main__":
    # prompt = "ohwx_borat_jeffrey_v1 near an ice hole. Cast my fishing rod. Frying a boot over the campfire."
    prompt = "ohwx_borat_jeffrey_v1 as a yandex intern"
    negative_prompt = "low resolution, low quality,肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"
    lora_path = "loras/zimage_turbo_lora_a100/zimage_turbo_lora_a100.safetensors"
    generate_image(prompt=prompt, negative_prompt=negative_prompt, lora_path=lora_path)