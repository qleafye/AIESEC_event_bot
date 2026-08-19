"""NEXTCLOUD_VERIFY_TLS defaults to True; self-signed goes through a CA bundle, not by
switching verification off. Each WebDAV PUT carries Basic Auth with the app-password plus
the resume bytes — insecure transport must be an explicit opt-out, never the shipped mode."""
import ssl

import pytest

from config import Settings, config
from services import nextcloud


def test_verify_tls_default_is_true():
    assert Settings.model_fields["NEXTCLOUD_VERIFY_TLS"].default is True
    assert Settings.model_fields["NEXTCLOUD_CA_BUNDLE"].default is None


def test_ssl_arg_default_uses_system_trust(monkeypatch):
    monkeypatch.setattr(config, "NEXTCLOUD_VERIFY_TLS", True)
    monkeypatch.setattr(config, "NEXTCLOUD_CA_BUNDLE", None)
    assert nextcloud._ssl_arg() is None  # aiohttp default = verification ON


def test_ssl_arg_explicit_opt_out(monkeypatch):
    monkeypatch.setattr(config, "NEXTCLOUD_VERIFY_TLS", False)
    monkeypatch.setattr(config, "NEXTCLOUD_CA_BUNDLE", None)
    assert nextcloud._ssl_arg() is False


def test_ssl_arg_ca_bundle_builds_verifying_context(monkeypatch, tmp_path):
    # Any self-signed cert will do to validate the wiring; generate one with stdlib-free
    # approach: reuse certifi's bundle if present, else skip.
    try:
        import certifi
    except ImportError:  # pragma: no cover
        pytest.skip("certifi not installed")
    monkeypatch.setattr(config, "NEXTCLOUD_VERIFY_TLS", True)
    monkeypatch.setattr(config, "NEXTCLOUD_CA_BUNDLE", certifi.where())
    ctx = nextcloud._ssl_arg()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_ssl_arg_bad_bundle_path_raises_not_silently_disables(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "NEXTCLOUD_VERIFY_TLS", True)
    monkeypatch.setattr(config, "NEXTCLOUD_CA_BUNDLE", str(tmp_path / "missing.pem"))
    with pytest.raises(OSError):
        nextcloud._ssl_arg()
