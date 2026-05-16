from character_creation import get_or_create_character
from content_planning import run_weekly_content_cycle, sleep_until_next_week_start


def main():
    profile = get_or_create_character(
        concept="Сдержанный travel-аналитик, который системно исследует "
        "направления, сравнивает маршруты по бюджету, времени и комфорту и "
        "публикует практичные гиды."
    )
    first_cycle = True
    while True:
        if not first_cycle:
            sleep_until_next_week_start()
        first_cycle = False
        run_weekly_content_cycle(profile)

if __name__ == "__main__":
    main()
