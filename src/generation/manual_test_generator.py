from __future__ import annotations

from pathlib import Path

from generator import Generator


# ---------------------------------------------------------------------------
# Quick manual test configuration.
# Edit these constants if your local paths/models differ.
# ---------------------------------------------------------------------------
TEXT_PROVIDER = "hf_local"
TEXT_MODEL = "Qwen/Qwen3-14B"
PERSONA_PRESET = "balanced"
OUTPUT_PATH = Path(__file__).resolve().parent / "manual_api_test.png"
IMAGE_ASPECT = "2k"
IMAGE_STEPS = 50
IMAGE_SEED = 42


TEST_EVENT = {
    "event": {
        "frame": {
        "summary": "На спуске потерялась перчатка, и пришлось возвращаться к месту последнего привала.",
        "event_type": "lost_item",
        "date_hint": "вечер",
        "location": "тропа между приютом и лесом",
        "primary_action": "искать потерянную перчатку",
        "outcome": "перчатку нашли на камне у привала"
        },
        "observations": [
        {
            "text": "После привала спуск продолжился по узкой каменной тропе.",
            "category": "action",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "descent",
                "trail"
            ]
            }
        },
        {
            "text": "Через несколько минут одна рука стала заметно холоднее другой.",
            "category": "state",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "cold_hand"
            ]
            }
        },
        {
            "text": "Перчатки в кармане куртки оказалась только одна.",
            "category": "turning_point",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "missing_glove"
            ]
            }
        },
        {
            "text": "Рюкзак проверили на обочине, но внутри перчатки не было.",
            "category": "friction",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "backpack_check",
                "missing_item"
            ]
            }
        },
        {
            "text": "Пришлось подниматься обратно по уже пройденным ступеням.",
            "category": "action",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "return",
                "uphill"
            ]
            }
        },
        {
            "text": "На камне у места привала лежала тёмная перчатка.",
            "category": "object",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "glove",
                "stone"
            ]
            }
        },
        {
            "text": "Ткань перчатки успела стать влажной от камня.",
            "category": "physical_detail",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "wet_fabric"
            ]
            }
        },
        {
            "text": "После находки спуск пошёл быстрее, но сумерки стали ближе.",
            "category": "outcome",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "found",
                "dusk"
            ]
            }
        },
        {
            "text": "Остался неприятный осадок от лишнего подъёма на усталых ногах.",
            "category": "aftertaste",
            "source": "parsed_post",
            "metadata": {
            "tags": [
                "fatigue",
                "annoyance"
            ]
            }
        }
        ],
        "retrieved_context": [
        {
            "text": "Вечером на этом участке быстро холодает после захода солнца.",
            "category": "place_fact",
            "source": "retrieved_post"
        }
        ],
        "implied_mood": [
        "досада",
        "облегчение",
        "усталость"
        ],
        "timeline_context": [
        "перчатки снимали на предыдущем привале",
        "до леса оставалось около получаса ходьбы"
        ],
        "source_refs": [
        {
            "kind": "synthetic_parsed_post",
            "title": "Потерянная перчатка на спуске",
            "url": "",
            "channel": "manual_dataset_seed",
            "published_at": ""
        }
        ],
        "metadata": {
        "theme": "В горах",
        "density": "dense",
        "topic_bucket": "снаряжение и мелкие неудобства",
        "source_type": "synthetic_rag_event"
        }
    }
}


def _resolve_lora_path() -> str:
    candidates = [
        Path("image/loras/zimage_turbo_lora_a100/zimage_turbo_lora_a100.safetensors"),
        Path("image/output/zimage_turbo_lora_a100/zimage_turbo_lora_a100.safetensors"),
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "Не найден файл LoRA. Проверь один из путей:\n"
        + "\n".join(f"- {path}" for path in candidates)
    )


if __name__ == "__main__":
    lora_path = _resolve_lora_path()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    generator = Generator(
        lora_path=lora_path,
        text_provider_name=TEXT_PROVIDER,
        text_model_name=TEXT_MODEL,
        persona_preset=PERSONA_PRESET,
    )

    result = generator.run_full(
        TEST_EVENT,
        image_seed=IMAGE_SEED,
        aspect=IMAGE_ASPECT,
        num_inference_steps=IMAGE_STEPS,
        seed=IMAGE_SEED,
        output_path=OUTPUT_PATH,
    )

    print("IMAGE PROMPT:")
    print(result.image_prompt)
    print()
    print("POST TEXT:")
    print(result.post_text)
    print()
    print(f"IMAGE SAVED TO: {OUTPUT_PATH}")
