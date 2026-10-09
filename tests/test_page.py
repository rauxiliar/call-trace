import html
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, load_settings
from app.main import create_app
from app.voipmonitor import VoipMonitor
from tests.support import (
    CALL,
    CF_ID,
    CF_SECRET,
    DSN,
    LEG_B,
    PASSWORD,
    SIP_B,
    US_UID,
    FakeVoip,
    MemoryRepo,
    cloud_us,
    sample_details,
    sample_legs,
    settings_for,
)
from tests.test_grafana import decode_explore


def _page(repo, voip=None):
    settings = settings_for(cloud_us())
    app = create_app(
        settings=settings,
        repo_for=lambda cloud: repo,
        voip=voip or FakeVoip(empty={SIP_B}),
    )
    return TestClient(app), settings


def explore_hrefs(page: str) -> list[str]:
    text = html.unescape(page)
    return re.findall(r'href="(https://o11y\.broadvoice\.io/explore\?[^"]+)"', text)


def test_page_shows_the_same_legs_and_hides_secrets():
    client, settings = _page(MemoryRepo(sample_details(), sample_legs()))
    by_uuid = client.get("/", params={"cloud": "example-us", "domain": "tenant.example", "q": CALL})
    by_sip = client.get("/", params={"cloud": "example-us", "domain": "ignored.example", "q": SIP_B})
    assert by_uuid.status_code == 200
    assert by_sip.status_code == 200
    for page in (by_uuid.text, by_sip.text):
        assert CALL in page
        assert LEG_B in page
        assert "sem CDR no VoIPmonitor" in page
        assert "VoIPmonitor" in page
        assert "o11y" in page
        assert "fs-1" in page
        assert "Servidor" in page
        assert page.count('class="go o11y"') == 1
        assert PASSWORD not in page
        assert DSN not in page
        assert CF_ID not in page
        assert CF_SECRET not in page
        assert settings.voipmonitor_user not in page or settings.voipmonitor_user == ""
        hrefs = explore_hrefs(page)
        exprs = {expr for href in hrefs for expr in decode_explore(href)["exprs"]}
        assert f'{{namespace="us-gocontact-mt"}} |= "{CALL}"' in exprs
        assert f'{{cloud="example-us"}} |= "{CALL}"' in exprs
        assert f'|= "{LEG_B}"' not in "".join(exprs)
        assert all(decode_explore(href)["uid"] == US_UID for href in hrefs)
        assert all("token" not in href.lower() for href in hrefs)
        assert SIP_B not in "".join(exprs)


def test_page_requires_a_domain_before_lookup():
    client, _settings = _page(MemoryRepo([], []))
    response = client.get("/", params={"cloud": "example-us", "q": SIP_B})
    assert "Escolhe um domínio." in response.text
    assert PASSWORD not in response.text
    assert DSN not in response.text


def test_unknown_cloud_and_bad_domain_do_not_open_the_database():
    def repo_for(cloud):
        raise AssertionError("database")

    app = create_app(settings=settings_for(cloud_us()), repo_for=repo_for, voip=FakeVoip())
    client = TestClient(app)
    unknown = client.get("/", params={"cloud": "other-cloud", "q": CALL})
    assert unknown.status_code == 200
    assert "Cloud desconhecido." in unknown.text
    injected = client.get(
        "/",
        params={"cloud": "example-us", "domain": 'foo"; DROP TABLE freeswitch_details;--', "q": CALL},
    )
    assert injected.status_code == 200
    assert "O domínio só pode ter letras, dígitos, ponto, _ e -." in injected.text


def test_voip_error_on_the_page_is_redacted():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["secret_header"] = request.headers.get("CF-Access-Client-Secret")
        return httpx.Response(200, json={"error": f"rejected {PASSWORD} {CF_SECRET}"})

    voip = VoipMonitor(
        "https://voipmon.broadvoice.com",
        "vm-user",
        PASSWORD,
        CF_ID,
        CF_SECRET,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client, _settings = _page(MemoryRepo([], sample_legs()), voip)
    response = client.get("/", params={"cloud": "example-us", "domain": "tenant.example", "q": CALL})
    assert response.status_code == 200
    assert PASSWORD not in response.text
    assert CF_SECRET not in response.text
    assert DSN not in response.text
    assert seen["secret_header"] == CF_SECRET
    assert "***" in response.text


def test_search_id_is_escaped():
    client, _settings = _page(MemoryRepo([], []))
    response = client.get("/", params={"cloud": "example-us", "q": "<script>alert(1)</script>"})
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text


def test_cloud_timezone_is_required_and_must_be_iana(tmp_path, monkeypatch):
    monkeypatch.delenv("VOIPMONITOR_PASSWORD", raising=False)
    path = tmp_path / "clouds.yaml"
    path.write_text(
        "clouds:\n"
        "  - id: x\n"
        "    region: us\n"
        f"    dsn: {DSN}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as missing:
        load_settings(path)
    assert str(missing.value) == "Cada cloud precisa de timezone."
    assert "dsn-secret-value" not in str(missing.value)

    path.write_text(
        "clouds:\n"
        "  - id: x\n"
        "    region: us\n"
        "    timezone: Mars/Base\n"
        f"    dsn: {DSN}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as invalid:
        load_settings(path)
    assert str(invalid.value) == "A timezone da cloud não é um nome IANA."
    assert "dsn-secret-value" not in str(invalid.value)
    assert "Mars/Base" not in str(invalid.value)


def test_config_errors_do_not_echo_the_dsn(tmp_path, monkeypatch):
    monkeypatch.delenv("VOIPMONITOR_PASSWORD", raising=False)
    path = tmp_path / "clouds.yaml"
    path.write_text(
        "clouds:\n"
        "  - id: x\n"
        "    region: mars\n"
        f"    dsn: {DSN}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_settings(path)
    assert "dsn-secret-value" not in str(exc.value)

    path.write_text(f"[\n  dsn: {DSN}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as broken:
        load_settings(path)
    assert "dsn-secret-value" not in str(broken.value)


def test_database_failure_hides_the_dsn():
    class Exploding:
        def fetch_details(self, search_id):
            raise RuntimeError(DSN)

        def fetch_legs_by_call_uuid(self, domain, call_uuid):
            raise RuntimeError(DSN)

        def fetch_legs_by_any_id(self, domain, search_id):
            raise RuntimeError(DSN)

    client, _settings = _page(Exploding())
    response = client.get("/", params={"cloud": "example-us", "domain": "tenant.example", "q": CALL})
    assert response.status_code == 200
    assert DSN not in response.text
    assert "Não foi possível ler a CDR deste cloud." in response.text


def test_stamps_render_in_the_cloud_timezone():
    client, _settings = _page(MemoryRepo(sample_details(), sample_legs()))
    page = client.get("/", params={"cloud": "example-us", "domain": "tenant.example", "q": CALL}).text
    assert "2026-07-01 07:00:00 UTC-05:00" in page
    assert "0.0 s" in page
