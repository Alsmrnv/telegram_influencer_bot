from character_creation import get_or_create_character


def main():
    profile = get_or_create_character(
        concept="Сдержанный travel-аналитик, который системно исследует "
        "направления, сравнивает маршруты по бюджету, времени и комфорту и "
        "публикует практичные гиды."
    )
    print(profile)


if __name__ == "__main__":
    main()
