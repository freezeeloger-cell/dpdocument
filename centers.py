"""
Зарубежные центры ДП «Документ» (с pasport.org.ua/centers, окт. 2026). Украина исключена намеренно.
subdomain -> (страна, город, адрес). Страница очереди: https://<subdomain>.pasport.org.ua/solutions/e-queue
"""

CENTERS: dict[str, tuple[str, str, str]] = {
    # Польша
    "warszawa":       ("🇵🇱 Польша", "Варшава", "Al. Jerozolimskie, 179"),
    "wroclaw":        ("🇵🇱 Польша", "Вроцлав", "plac Grunwaldzki 22"),
    "gdansk":         ("🇵🇱 Польша", "Гданьск", "Aleja Grunwaldzka, 415"),
    "krakow":         ("🇵🇱 Польша", "Краков", "Pawia 5"),
    # Чехия, Словакия, Словения
    "prague":         ("🇨🇿 Чехия", "Прага", "Michelská 1552/58"),
    "bratislava":     ("🇸🇰 Словакия", "Братислава", "Prievozská 5434/6A"),
    "ljubljana":      ("🇸🇮 Словения", "Любляна", "Leskoškova cesta, 12"),
    # Германия
    "berlin":         ("🇩🇪 Германия", "Берлин", "Am Treptower Park 14"),
    "cologne":        ("🇩🇪 Германия", "Кёльн", "Händelstraße 25-29"),
    "munich":         ("🇩🇪 Германия", "Мюнхен", "Heinrich-Wieland-Straße 5"),
    # Испания
    "barcelona":      ("🇪🇸 Испания", "Барселона", "Av. del Segle XXI, 6, Viladecans"),
    "valencia":       ("🇪🇸 Испания", "Валенсия", "Av. de Pius XII, 2, local 117"),
    "madrid":         ("🇪🇸 Испания", "Мадрид", "Blvr. de José Prat, 35, Vicálvaro"),
    # Италия
    "milan":          ("🇮🇹 Италия", "Милан", "Via Eugenio Curiel, 25, Rozzano"),
    # Болгария, Молдова
    "varna":          ("🇧🇬 Болгария", "Варна", "бул. Владислав Варненчик, 186 (Mall Varna)"),
    "chisinau":       ("🇲🇩 Молдова", "Кишинёв", "Bd. Stefan cel Mare si Sfant, 8"),
    # Бельгия, Великобритания, Канада
    "kortrijk":       ("🇧🇪 Бельгия", "Кортрейк", "Kloosterstraat 9, Marke"),
    "london":         ("🇬🇧 Великобритания", "Лондон", "8-15 Dereham Place, EC2A 3HJ"),
    "toronto":        ("🇨🇦 Канада", "Торонто", "99 Six Point Rd, Etobicoke"),
}

COUNTRIES: list[str] = list(dict.fromkeys(c for c, _, _ in CENTERS.values()))


def by_country(country: str) -> dict[str, tuple[str, str, str]]:
    return {k: v for k, v in CENTERS.items() if v[0] == country}


def title(sub: str) -> str:
    c, city, _ = CENTERS[sub]
    return f"{city} ({c.split(' ', 1)[1]})"


def url(sub: str) -> str:
    return f"https://{sub}.pasport.org.ua/solutions/e-queue"
