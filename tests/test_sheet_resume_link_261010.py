"""Резюме ссылкой из развилки («🔗 Ссылка») попадает в колонку «Резюме (ссылка)»: своя колонка
«Резюме (ссылка на профиль)» живёт за выключенным по умолчанию тумблером, и на проде 77 ссылок
не было видно в таблице (10.10)."""
from handlers.reg.reg_schema import SHEET_COLUMNS


def _cell(data):
    fn = next(f for h, _g, f in SHEET_COLUMNS if h == "Резюме (ссылка)")
    return fn(data)


def test_link_resume_shows_in_resume_link_column():
    assert _cell({"resume_type": "link", "resume_link": "https://disk.yandex.ru/d/x"}) == "https://disk.yandex.ru/d/x"


def test_uploaded_file_link_wins_and_empty_is_dash():
    assert _cell({"resume_url": "https://cloud/s/a", "resume_link": "https://other"}) == "https://cloud/s/a"
    assert _cell({}) == "-"
