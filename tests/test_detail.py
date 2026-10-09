import json

from fastapi.testclient import TestClient

from app.cdr import call_variables, dialplan_steps
from app.main import create_app
from app.models import DetailsRow
from tests.support import DSN, PASSWORD, FakeVoip, MemoryRepo, cloud_us, settings_for

LEG = "leg-detail-1"
APPS = json.dumps(
    [
        {"app": "set", "data": "12ms: domain_name=tenant.example", "res": "true"},
        {"app": "bridge", "data": "400ms: sofia/internal/100", "res": "SUCCESS"},
    ]
)


def _row(apps: str = APPS) -> DetailsRow:
    return DetailsRow(
        uuid=LEG,
        sip_callid="sip-detail",
        apps=apps,
        details=json.dumps(
            {
                "Unique-ID": LEG,
                "variable_domain_name": "tenant.example",
                "variable_accountcode": "acme <script>",
                "running_apps": json.dumps(
                    [{"app": "hangup", "data": "1ms: ", "res": ""}]
                ),
            }
        ),
    )


def test_variables_drop_the_prefix_and_ignore_other_headers():
    names = dict(call_variables(_row().details))
    assert names["domain_name"] == "tenant.example"
    assert names["accountcode"] == "acme <script>"
    assert "Unique-ID" not in names
    assert list(names) == sorted(names)


def test_dialplans_prefer_the_apps_column():
    steps = dialplan_steps(_row().apps, _row().details)
    assert [step["app"] for step in steps] == ["set", "bridge"]
    assert steps[0]["data"].startswith("12ms:")
    assert steps[1]["res"] == "SUCCESS"


def test_dialplans_fall_back_to_running_apps_in_the_event():
    steps = dialplan_steps("", _row("").details)
    assert steps[0]["app"] == "hangup"


def test_detail_endpoint_renders_one_leg_and_escapes_values():
    repo = MemoryRepo([_row()], [])
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get("/cdr-detail", params={"cloud": "example-us", "uuid": LEG})
    assert page.status_code == 200
    assert "Dialplans" in page.text
    assert "bridge" in page.text
    assert "resposta" not in page.text
    assert "SUCCESS" not in page.text
    assert "domain_name" in page.text
    assert "acme &lt;script&gt;" in page.text
    assert "<script>" not in page.text
    assert PASSWORD not in page.text
    assert DSN not in page.text
    assert ("leg_detail", LEG) in repo.calls


def test_missing_detail_says_so_without_listing_the_page():
    repo = MemoryRepo([], [])
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get("/cdr-detail", params={"cloud": "example-us", "uuid": LEG})
    assert "Não há detalhe desta perna." in page.text
    assert "Dialplans" not in page.text
