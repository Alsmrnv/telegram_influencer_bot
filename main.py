from character_creation import get_or_create_character
from content_planning import build_weekly_publication_plan, run_content_schedule


def main():
    profile = get_or_create_character(
        concept="Сдержанный travel-аналитик, который системно исследует "
        "направления, сравнивает маршруты по бюджету, времени и комфорту и "
        "публикует практичные гиды."
    )
    # Сейчас функция работает только неделю
    # TODO: Нужно сделать так, чтобы функция работала много недель
    plan = build_weekly_publication_plan(profile)
    run_content_schedule(plan)
    print("Content schedule completed")

if __name__ == "__main__":
    main()
