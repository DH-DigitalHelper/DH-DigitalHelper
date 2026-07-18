from scraper.quality import evaluate


def test_rejects_none_and_empty():
    assert evaluate(None)[0] is False
    assert evaluate({"text": "", "markdown": ""})[0] is False


def test_rejects_too_short():
    ok, reason = evaluate({"text": "three short words", "markdown": "x"}, min_words=50)
    assert ok is False and "short" in reason


def test_rejects_nav_only_link_lists():
    md = "\n".join(f"- [Item {i}](https://x/{i})" for i in range(20))
    ok, reason = evaluate({"text": "a " * 60, "markdown": md}, min_words=50)
    assert ok is False and "boilerplate" in reason


def test_image_alt_text_does_not_trip_the_nav_gate():
    """_LINK_ANCHOR matches the [alt](url) inside an image's ![alt](url)."""
    md = "".join(
        f"![Ausfuehrliche Bildbeschreibung Nummer {i}](https://x/img{i}.png)\n"
        for i in range(20)
    )
    prose = "Dies ist ein echter Absatz mit nuetzlichem Inhalt. " * 12
    md += "\n" + prose
    doc = {"text": prose.strip(), "markdown": md}

    accepted, reason = evaluate(doc, min_words=50)

    assert accepted is True, f"image-heavy but prose-bearing page rejected: {reason}"


def test_accepts_real_prose():
    text = "This is a real paragraph of useful content. " * 10
    ok, reason = evaluate({"text": text, "markdown": text}, min_words=50)
    assert ok is True and reason == "ok"


def test_rejects_cookie_boilerplate_when_short_and_dominated():
    block = (
        "Diese Website verwendet Cookies. Wir verwenden Cookies um Ihre "
        "Erfahrung zu verbessern. Notwendige Cookies sind immer aktiv. "
        "Datenschutzeinstellungen anpassen. Cookie-Einstellungen öffnen. "
        "Alle akzeptieren. "
    )
    text = block * 3
    ok, reason = evaluate({"text": text, "markdown": text}, min_words=50)
    assert ok is False and reason == "boilerplate/cookie"


def test_rejects_login_wall_when_short_and_dominated():
    block = (
        "Bitte melden Sie sich an, um fortzufahren. Benutzername und Passwort "
        "eingeben. Angemeldet bleiben. Passwort vergessen? Zur Anmeldung nutzen "
        "Sie das Portal. "
    )
    text = block * 4
    ok, reason = evaluate({"text": text, "markdown": text}, min_words=50)
    assert ok is False and reason == "boilerplate/login"


def test_rejects_error_page_when_short_and_dominated():
    block = (
        "Seite nicht gefunden. Die angeforderte Seite wurde nicht gefunden. "
        "Fehler 404. Diese Seite existiert nicht mehr. Es wurden keine "
        "Einträge gefunden. "
    )
    text = block * 4
    ok, reason = evaluate({"text": text, "markdown": text}, min_words=50)
    assert ok is False and reason == "boilerplate/empty-state"


def test_accepts_long_page_that_only_mentions_boilerplate_terms():
    body = (
        "Das duale Studium an der DHBW verbindet wissenschaftliche Theorie eng "
        "mit betrieblicher Praxis. Studierende wechseln im Dreimonatsrhythmus "
        "zwischen Vorlesungen an der Hochschule und Praxisphasen im "
        "Partnerunternehmen. "
    )
    tail = (
        "Hinweis zum Datenschutz: Diese Website verwendet Cookies; "
        "Cookie-Einstellungen finden Sie im Footer. Für das Campus-Portal "
        "melden Sie sich mit Benutzername und Passwort an."
    )
    text = body * 8 + tail
    ok, reason = evaluate({"text": text, "markdown": text}, min_words=50)
    assert ok is True and reason == "ok"


def test_accepts_short_page_with_a_single_incidental_marker():
    text = (
        "Anmeldung zur Erstsemester-Infoveranstaltung am Campus Heidenheim. Ab "
        "sofort können sich Studieninteressierte für die Veranstaltung anmelden. "
        "Geboten werden Einblicke in die dualen Studienangebote, Vorträge sowie "
        "Gespräche mit Studierenden. Zur Anmeldung nutzen Sie bitte das Formular."
    )
    ok, reason = evaluate({"text": text, "markdown": text}, min_words=10)
    assert ok is True and reason == "ok"


def test_rejects_inline_link_nav_that_old_heuristic_missed():
    md = " ".join(f"[Studiengang {i}](https://x/{i})" for i in range(20))
    text = " ".join(f"Studiengang {i}" for i in range(20))
    ok, reason = evaluate({"text": text, "markdown": md}, min_words=10)
    assert ok is False and reason == "boilerplate/nav-only"
