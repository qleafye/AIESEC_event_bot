"""Resume filenames in Nextcloud must be unique per upload.

WebDAV PUT overwrites silently: two delegates with the same display name, or one delegate
re-submitting, used to replace each other's file while the stored deep-link kept looking
valid. telegram_id + timestamp in the name make every upload land in its own file."""
import re
from datetime import datetime

from handlers.registration import _resume_file_stem
from services.nextcloud import _safe_name

_NOW = datetime(2026, 8, 19, 14, 5, 9)


def test_stem_format_name_username_id_ts():
    data = {"full_name": "Иван Петров", "username": "@qleafye", "telegram_id": 424242}
    assert _resume_file_stem(data, now=_NOW) == "Иван Петров_qleafye_424242_20260819-140509"


def test_stem_without_username_does_not_double_id():
    for uname in ("-", "", None):
        data = {"full_name": "Иван Петров", "username": uname, "telegram_id": 424242}
        assert _resume_file_stem(data, now=_NOW) == "Иван Петров_424242_20260819-140509"


def test_same_name_different_users_get_different_files():
    a = {"full_name": "Иван Петров", "username": "-", "telegram_id": 1}
    b = {"full_name": "Иван Петров", "username": "-", "telegram_id": 2}
    assert _resume_file_stem(a, now=_NOW) != _resume_file_stem(b, now=_NOW)


def test_resubmission_gets_different_file():
    data = {"full_name": "Иван Петров", "username": "@x", "telegram_id": 1}
    t1 = datetime(2026, 8, 19, 14, 5, 9)
    t2 = datetime(2026, 8, 19, 14, 5, 10)
    assert _resume_file_stem(data, now=t1) != _resume_file_stem(data, now=t2)


def test_safe_name_preserves_uniqueness_segments():
    """_safe_name keeps [\\w.-]: digits, underscores, hyphen and cyrillic survive, so the
    telegram_id/timestamp suffix is not stripped before the PUT."""
    data = {"full_name": "Иван Петров", "username": "@qleafye", "telegram_id": 424242}
    remote = _safe_name(_resume_file_stem(data, now=_NOW) + ".pdf")
    assert remote == "Иван_Петров_qleafye_424242_20260819-140509.pdf"
    assert re.search(r"_424242_\d{8}-\d{6}\.pdf$", remote)
