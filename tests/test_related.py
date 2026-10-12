import json

from fastapi.testclient import TestClient

from app.cdr import linked_uuids, related
from app.main import create_app
from tests.support import (
    CALL,
    DOMAIN,
    NOW,
    T0,
    T1,
    FakeVoip,
    MemoryRepo,
    cloud_us,
    make_leg,
    sample_details,
    sample_legs,
    settings_for,
)

OTHER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
BRIDGE = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
FURTHER = "ffffffff-ffff-ffff-ffff-ffffffffffff"
SHARED = "shared-sip-id"


def _event(uuid: str, sip: str, details: dict, apps: list | None = None):
    from app.models import DetailsRow

    return DetailsRow(
        uuid=uuid,
        sip_callid=sip,
        details=json.dumps(details),
        apps=json.dumps(apps or []),
    )


def test_linked_uuids_follow_originated_other_and_bridge_once():
    details = json.dumps(
        {
            "variable_originated_legs": f"{OTHER};sofia/gateway/trunk",
            "Other-Leg-Unique-ID": BRIDGE,
            "variable_originating_leg_uuid": OTHER,
        }
    )
    apps = json.dumps(
        [{"app": "uuid_bridge", "data": f"12ms: {BRIDGE}", "res": "SUCCESS"}]
    )
    assert linked_uuids(details, apps) == [OTHER, BRIDGE]


def test_the_same_sip_call_id_pulls_the_other_leg_and_its_links():
    seed = _event(
        CALL,
        SHARED,
        {
            "variable_domain_name": DOMAIN,
            "variable_sip_call_id": SHARED,
            "variable_originated_legs": f"{OTHER};sofia/gateway/trunk",
            "Channel-Call-UUID": CALL,
        },
    )
    other = _event(
        OTHER,
        SHARED,
        {
            "variable_domain_name": DOMAIN,
            "variable_sip_call_id": SHARED,
            "variable_originating_leg_uuid": FURTHER,
            "Channel-Call-UUID": OTHER,
        },
        [{"app": "uuid_bridge", "data": BRIDGE, "res": "SUCCESS"}],
    )
    legs = [
        make_leg(CALL, SHARED, T0, T1),
        make_leg(OTHER, SHARED, T0, T1, call_uuid=OTHER),
        make_leg(BRIDGE, "sip-bridge", T0, T1, call_uuid=BRIDGE),
        make_leg(FURTHER, "sip-further", T0, T1, call_uuid=FURTHER),
    ]
    repo = MemoryRepo([seed, other], legs)
    outcome = related(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain=DOMAIN,
        call_uuid=CALL,
        repo=repo,
        voip=FakeVoip(),
        now=NOW,
    )
    found = {item.leg.uuid for call in outcome.calls for item in call.legs}
    assert found == {OTHER, BRIDGE, FURTHER}
    assert CALL not in found


def _linked_repo():
    seed = _event(
        CALL,
        SHARED,
        {
            "variable_domain_name": DOMAIN,
            "variable_sip_call_id": SHARED,
            "variable_originated_legs": f"{OTHER};sofia/gateway/trunk",
            "Channel-Call-UUID": CALL,
        },
    )
    other = _event(
        OTHER,
        SHARED,
        {
            "variable_domain_name": DOMAIN,
            "variable_sip_call_id": SHARED,
            "variable_originating_leg_uuid": FURTHER,
            "Channel-Call-UUID": OTHER,
        },
        [{"app": "uuid_bridge", "data": BRIDGE, "res": "SUCCESS"}],
    )
    legs = [
        make_leg(CALL, SHARED, T0, T1),
        make_leg(OTHER, SHARED, T0, T1, call_uuid=OTHER),
        make_leg(BRIDGE, "sip-bridge", T0, T1, call_uuid=BRIDGE),
        make_leg(FURTHER, "sip-further", T0, T1, call_uuid=FURTHER),
    ]
    return MemoryRepo([seed, other], legs)


def _related(repo, call_uuid, listed=None):
    return related(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain=DOMAIN,
        call_uuid=call_uuid,
        repo=repo,
        voip=FakeVoip(),
        now=NOW,
        listed=listed,
    )


def test_a_call_already_listed_is_skipped_but_calls_beyond_it_remain():
    outcome = _related(_linked_repo(), CALL, listed={OTHER})
    found = {call.call_uuid for call in outcome.calls}
    assert found == {BRIDGE, FURTHER}
    assert outcome.listed_related == [OTHER]


def test_the_related_endpoint_omits_a_call_that_was_already_listed():
    repo = _linked_repo()
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    client = TestClient(app)
    found = client.get(
        "/related",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "call_uuid": CALL,
            "listed": OTHER,
        },
    )
    assert found.status_code == 200
    assert f'data-call="{OTHER}"' not in found.text
    assert f'data-call="{BRIDGE}"' in found.text
    assert f'data-call="{FURTHER}"' in found.text
    assert found.headers.get("X-Related-Listed") == OTHER


def test_when_every_linked_call_is_already_listed_the_search_reports_them():
    outcome = _related(
        _linked_repo(),
        OTHER,
        listed={CALL, OTHER, BRIDGE, FURTHER},
    )
    assert outcome.calls == []
    assert "Sem chamadas relacionadas." not in outcome.notices
    assert set(outcome.listed_related) == {CALL, BRIDGE, FURTHER}


def test_the_related_endpoint_reports_listed_related_in_a_header():
    repo = _linked_repo()
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    client = TestClient(app)
    found = client.get(
        "/related",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "call_uuid": OTHER,
            "listed": [CALL, OTHER, BRIDGE, FURTHER],
        },
    )
    assert found.status_code == 200
    assert f'data-call="{CALL}"' not in found.text
    assert f'data-call="{BRIDGE}"' not in found.text
    assert f'data-call="{FURTHER}"' not in found.text
    assert "Sem chamadas relacionadas." not in found.text
    reported = set(found.headers.get("X-Related-Listed", "").split(","))
    assert reported == {CALL, BRIDGE, FURTHER}


def test_invalid_domain_does_not_become_a_table_name():
    bad = "bad domain"
    seed = _event(
        CALL,
        SHARED,
        {
            "variable_domain_name": bad,
            "variable_originating_leg_uuid": OTHER,
            "Channel-Call-UUID": CALL,
        },
    )
    repo = MemoryRepo([seed], [make_leg(CALL, SHARED, T0, T1), make_leg(OTHER, "sip-other", T0, T1)])
    outcome = related(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain=DOMAIN,
        call_uuid=CALL,
        repo=repo,
        voip=FakeVoip(),
        now=NOW,
    )
    assert all(bad not in str(part) for call in repo.calls for part in call)
    assert not any(call[0] == "any" for call in repo.calls)
    assert outcome.calls == []
    assert "Sem chamadas relacionadas." in outcome.notices


def test_the_page_offers_related_calls_in_the_listing():
    other = _event(
        OTHER,
        SHARED,
        {
            "variable_domain_name": DOMAIN,
            "variable_sip_call_id": SHARED,
            "Channel-Call-UUID": OTHER,
        },
    )
    seed = _event(
        CALL,
        SHARED,
        {
            "variable_domain_name": DOMAIN,
            "variable_sip_call_id": SHARED,
            "Channel-Call-UUID": CALL,
        },
    )
    legs = sample_legs() + [make_leg(OTHER, SHARED, T0, T1, call_uuid=OTHER)]
    repo = MemoryRepo([*sample_details(), seed, other], legs, domains=[DOMAIN])
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    client = TestClient(app)
    page = client.get("/", params={"cloud": "example-us", "domain": DOMAIN, "q": CALL})
    assert page.status_code == 200
    assert 'class="related"' in page.text
    assert "A procurar chamadas relacionadas…" in page.text
    found = client.get(
        "/related",
        params={"cloud": "example-us", "domain": DOMAIN, "call_uuid": CALL},
    )
    assert found.status_code == 200
    assert OTHER in found.text
    assert 'class="leg' in found.text
    assert "Relacionadas" in found.text
    again = client.get(
        "/related",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "call_uuid": OTHER,
            "listed": [CALL, OTHER],
        },
    )
    assert again.status_code == 200
    assert f'data-call="{CALL}"' not in again.text
    assert f'data-call="{OTHER}"' not in again.text
    assert "Sem chamadas relacionadas." not in again.text
    assert again.headers.get("X-Related-Listed") == CALL
    assert "Mostrar todas" in page.text
