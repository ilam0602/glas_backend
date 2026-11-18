from datetime import timedelta
from types import SimpleNamespace

import google.auth.impersonated_credentials as imp_creds_mod

import server


class FakeComputeCreds:
    """Compute credentials before their first refresh: no sign_bytes, and
    service_account_email is the literal placeholder "default"."""

    def __init__(self, resolved_email="runtime-sa@proj.iam.gserviceaccount.com"):
        self.service_account_email = "default"
        self.refreshed = False
        self._resolved_email = resolved_email

    def refresh(self, request):
        self.refreshed = True
        self.service_account_email = self._resolved_email


class FakeBlob:
    def __init__(self):
        self.kwargs = None

    def generate_signed_url(self, **kwargs):
        self.kwargs = kwargs
        return "https://signed.example"


def _setup(monkeypatch, creds):
    captured = {}

    class FakeImpersonated:
        def __init__(self, source_credentials, target_principal, target_scopes):
            captured["principal"] = target_principal

    monkeypatch.setattr(server, "gcs_client", SimpleNamespace(_credentials=creds))
    monkeypatch.setattr(imp_creds_mod, "Credentials", FakeImpersonated)
    return captured


def test_cold_compute_creds_are_refreshed_before_impersonation(monkeypatch):
    creds = FakeComputeCreds()
    captured = _setup(monkeypatch, creds)

    blob = FakeBlob()
    url = server.generate_signed_url(
        blob, method="PUT", expiration=timedelta(minutes=30), headers={"X": "y"}
    )

    assert url == "https://signed.example"
    assert creds.refreshed
    assert captured["principal"] == "runtime-sa@proj.iam.gserviceaccount.com"
    assert blob.kwargs["method"] == "PUT"


def test_warm_compute_creds_are_not_refreshed(monkeypatch):
    creds = FakeComputeCreds()
    creds.service_account_email = "runtime-sa@proj.iam.gserviceaccount.com"
    captured = _setup(monkeypatch, creds)

    server.generate_signed_url(FakeBlob(), method="GET", expiration=timedelta(hours=1))

    assert not creds.refreshed
    assert captured["principal"] == "runtime-sa@proj.iam.gserviceaccount.com"


def test_creds_without_sa_email_raise_without_refresh(monkeypatch):
    # Local user ADC: no sign_bytes and no service_account_email at all —
    # must raise (so /media falls back to proxying) without a refresh attempt.
    class UserCreds:
        pass

    _setup(monkeypatch, UserCreds())

    import pytest

    with pytest.raises(RuntimeError):
        server.generate_signed_url(FakeBlob(), method="GET", expiration=timedelta(hours=1))


def test_unresolvable_default_email_raises(monkeypatch):
    creds = FakeComputeCreds(resolved_email="default")
    _setup(monkeypatch, creds)

    import pytest

    with pytest.raises(RuntimeError):
        server.generate_signed_url(FakeBlob(), method="GET", expiration=timedelta(hours=1))
    assert creds.refreshed
