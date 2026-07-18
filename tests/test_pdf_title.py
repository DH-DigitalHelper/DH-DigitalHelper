from scraper import pdf_title


def test_clean_trims_and_keeps_real_titles():
    assert pdf_title.clean("  Prüfungsordnung der DHBW  ") == "Prüfungsordnung der DHBW"


def test_clean_rejects_empty_and_none():
    assert pdf_title.clean(None) is None
    assert pdf_title.clean("") is None
    assert pdf_title.clean("   ") is None


def test_clean_rejects_placeholders():
    assert pdf_title.clean("untitled") is None
    assert pdf_title.clean("Unbenannt") is None


def test_clean_rejects_office_tool_artifacts():
    assert pdf_title.clean("Microsoft Word - Modulhandbuch.docx") is None


def test_clean_rejects_bare_filenames():
    assert pdf_title.clean("scan0001.pdf") is None
    assert pdf_title.clean("Modul.docx") is None


def test_from_url_derives_title_from_basename():
    url = "https://www.dhbw.de/fileadmin/Modulhandbuch_WI.pdf"
    assert pdf_title.from_url(url) == "Modulhandbuch WI"


def test_from_url_url_decodes_and_strips_extension():
    url = "https://www.dhbw.de/dateien/Amtliche%20Bekanntmachung.pdf"
    assert pdf_title.from_url(url) == "Amtliche Bekanntmachung"


def test_from_url_turns_hyphens_into_spaces():
    assert pdf_title.from_url("https://x/pruefungs-ordnung.pdf") == "pruefungs ordnung"


def test_from_url_returns_none_without_a_basename():
    assert pdf_title.from_url("https://www.dhbw.de/a/b/") is None
    assert pdf_title.from_url("https://www.dhbw.de/") is None
