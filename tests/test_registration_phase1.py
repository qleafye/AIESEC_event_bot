"""Phase 1 registration pure-helper tests (QW-01 summary, QW-03 resume validation)."""
from handlers.registration import (
    _build_summary,
    _is_allowed_resume,
    _resume_too_large,
    _RESUME_MAX_BYTES,
)


# ── QW-01: summary builder ───────────────────────────────────────────────────

def test_summary_contains_full_name():
    out = _build_summary({"full_name": "Иван Петров", "email": "a@b.com"})
    assert "Иван Петров" in out


def test_summary_omits_absent_fields():
    out = _build_summary({"full_name": "Test User"})
    # An absent field (e.g. Email) is simply not rendered, never shown as a blank line.
    assert "Email" not in out


def test_summary_escapes_html_in_free_text():
    out = _build_summary({"full_name": "Test", "city": "<script>alert(1)</script>"})
    assert "<script>" not in out          # raw user HTML must not survive
    assert "&lt;script&gt;" in out        # it is escaped instead


def test_summary_marks_attached_resume():
    out = _build_summary({"full_name": "Test", "resume_file_id": "FILEID"})
    assert "Резюме" in out


# ── QW-03: resume file-type validation ───────────────────────────────────────

def test_resume_accepts_pdf():
    assert _is_allowed_resume("cv.pdf") is True


def test_resume_accepts_pdf_uppercase():
    assert _is_allowed_resume("cv.PDF") is True


def test_resume_accepts_docx():
    assert _is_allowed_resume("cv.docx") is True


def test_resume_rejects_exe():
    assert _is_allowed_resume("cv.exe") is False


def test_resume_rejects_empty():
    assert _is_allowed_resume("") is False


def test_resume_rejects_none():
    assert _is_allowed_resume(None) is False


# ── P0 audit T-dw1-01: resume size guard ─────────────────────────────────────

def test_resume_size_none_passes():
    assert _resume_too_large(None) is False


def test_resume_size_zero_passes():
    assert _resume_too_large(0) is False


def test_resume_size_at_limit_passes():
    assert _resume_too_large(_RESUME_MAX_BYTES) is False


def test_resume_size_over_limit_rejected():
    assert _resume_too_large(_RESUME_MAX_BYTES + 1) is True


def test_resume_size_small_passes():
    assert _resume_too_large(500) is False
