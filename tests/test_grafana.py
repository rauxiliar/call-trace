import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from app.cdr import correlate
from app.grafana import explore_url, logql
from tests.support import (
    CALL,
    EU_UID,
    LEG_B,
    NOW,
    SIP_B,
    T0,
    T1,
    US_UID,
    FakeVoip,
    MemoryRepo,
    cloud_eu_vm,
    cloud_us,
    sample_details,
    sample_legs,
    settings_for,
)


def decode_explore(url: str) -> dict:
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "o11y.broadvoice.io"
    assert parsed.path == "/explore"
    query = parse_qs(parsed.query)
    assert query["schemaVersion"] == ["1"]
    assert query["orgId"] == ["1"]
    assert "token" not in parsed.query.lower()
    panes = json.loads(query["panes"][0])
    pane = panes["logs"]
    return {
        "uid": pane["datasource"],
        "expr": pane["queries"][0]["expr"],
        "exprs": [query["expr"] for query in pane["queries"]],
        "query_uid": pane["queries"][0]["datasource"]["uid"],
        "query_type": pane["queries"][0]["datasource"]["type"],
        "from": int(pane["range"]["from"]),
        "to": int(pane["range"]["to"]),
    }


def test_logql_keeps_namespace_and_cloud_as_separate_filters():
    assert logql("us-gocontact-mt", CALL, "us-gocontact-mt") == [
        f'{{namespace="us-gocontact-mt"}} |= "{CALL}"',
        f'{{cloud="us-gocontact-mt"}} |= "{CALL}"',
    ]
    assert logql("", CALL, "example-eu") == [f'{{cloud="example-eu"}} |= "{CALL}"']
    assert logql("bad label", CALL, "example-us") == [f'{{cloud="example-us"}} |= "{CALL}"']
    assert logql("", CALL, "") == []
    assert logql("ns", 'say "hi"', "example-us") == [
        '{namespace="ns"} |= "say \\"hi\\""',
        '{cloud="example-us"} |= "say \\"hi\\""',
    ]


def test_logs_link_uses_call_uuid_region_and_call_window():
    outcome = correlate(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain="form.example",
        search_id=SIP_B,
        repo=MemoryRepo(sample_details(), sample_legs()),
        voip=FakeVoip(),
        now=NOW,
    )
    call = outcome.calls[0]
    decoded = decode_explore(call.logs_url)
    assert decoded["exprs"] == [
        f'{{namespace="us-gocontact-mt"}} |= "{CALL}"',
        f'{{cloud="example-us"}} |= "{CALL}"',
    ]
    assert SIP_B not in call.logs_url
    assert decoded["uid"] == US_UID
    assert decoded["query_uid"] == US_UID
    assert decoded["query_type"] == "loki"
    start = T0 - timedelta(minutes=5)
    end = T1 + timedelta(minutes=5)
    assert decoded["from"] == int(start.timestamp() * 1000)
    assert decoded["to"] == int(end.timestamp() * 1000)
    assert parse_qs(urlparse(call.logs_url).query)["timezone"] == ["America/Chicago"]

    leg_b = next(item for item in call.legs if item.leg.uuid == LEG_B)
    leg_decoded = decode_explore(leg_b.logs_url)
    assert leg_decoded["exprs"] == [
        f'{{namespace="us-gocontact-mt"}} |= "{LEG_B}"',
        f'{{cloud="example-us"}} |= "{LEG_B}"',
    ]
    assert leg_decoded["from"] == decoded["from"]
    assert leg_decoded["to"] == decoded["to"]
    assert SIP_B not in leg_b.logs_url


def test_explore_timezone_follows_the_cloud_and_keeps_the_utc_instant():
    from dataclasses import replace

    cloud = replace(cloud_us(), timezone="Europe/Lisbon")
    outcome = correlate(
        cloud=cloud,
        settings=settings_for(cloud),
        domain="form.example",
        search_id=SIP_B,
        repo=MemoryRepo(sample_details(), sample_legs()),
        voip=FakeVoip(),
    )
    url = outcome.calls[0].logs_url
    decoded = decode_explore(url)
    start = T0 - timedelta(minutes=5)
    end = T1 + timedelta(minutes=5)
    assert decoded["from"] == int(start.timestamp() * 1000)
    assert decoded["to"] == int(end.timestamp() * 1000)
    assert parse_qs(urlparse(url).query)["timezone"] == ["Europe/Lisbon"]


def test_vm_cloud_without_namespace_uses_cloud_on_loki_eu():
    cloud = cloud_eu_vm()
    outcome = correlate(
        cloud=cloud,
        settings=settings_for(cloud),
        domain="tenant.example",
        search_id=CALL,
        repo=MemoryRepo([], sample_legs()),
        voip=FakeVoip(),
        now=NOW,
    )
    decoded = decode_explore(outcome.calls[0].logs_url)
    assert decoded["exprs"] == [f'{{cloud="example-eu-vm"}} |= "{CALL}"']
    assert decoded["uid"] == EU_UID


def test_missing_selector_omits_the_link():
    cloud = cloud_us()
    bare = type(cloud)(
        id=cloud.id,
        region=cloud.region,
        namespace="",
        cluster="",
        dsn=cloud.dsn,
    )
    outcome = correlate(
        cloud=bare,
        settings=settings_for(bare),
        domain="tenant.example",
        search_id=CALL,
        repo=MemoryRepo([], sample_legs()),
        voip=FakeVoip(),
        now=NOW,
    )
    decoded = decode_explore(outcome.calls[0].logs_url)
    assert decoded["exprs"] == [f'{{cloud="example-us"}} |= "{CALL}"']
    assert all(
        decode_explore(item.logs_url)["exprs"] == [f'{{cloud="example-us"}} |= "{item.leg.uuid}"']
        for item in outcome.calls[0].legs
    )


def test_explore_url_has_no_token():
    url = explore_url(
        "https://o11y.broadvoice.io",
        US_UID,
        f'{{namespace="us-gocontact-mt"}} |= "{CALL}"',
        T0,
        T1,
    )
    assert "token" not in url.lower()
    assert "password" not in url.lower()
    assert url.startswith("https://o11y.broadvoice.io/explore?")
