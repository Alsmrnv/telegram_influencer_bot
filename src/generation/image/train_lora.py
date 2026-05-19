import subprocess
import os
import traceback

if __name__ == "__main__":
    print("Training LoRA...")
    print("Current directory:", os.getcwd())

    try:
        subprocess.run(["uv", "run", "python", "ai-toolkit/run.py", "lora-config/train_zimage_turbo.yaml"])
    except Exception as e:
        print("Error training LoRA:", e)
        print("Traceback:", traceback.format_exc())
        raise e

    print("LoRA trained successfully")