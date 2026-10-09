from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.cdr import SearchInputError, bounded_filters, browse, parse_stamp
from app.db import LegFilters
from app.main import create_app
from tests.support import (
    DOMAIN,
    NOW,
    T0,
    FakeVoip,
    MemoryRepo,
    cloud_us,
    leg_matches,
    make_leg,
    settings_for,
)


def _legs(count: int):
    return [
        make_leg(
            f"leg-{index}",
            f"sip-{index}",
            T0 + timedelta(minutes=index),
            T0 + timedelta(minutes=index, seconds=30),
            call_uuid=f"call-{index}",
        )
        for index in range(count)
    ]


def _window() -> LegFilters:
    return LegFilters(start=T0, end=T0 + timedelta(hours=23))


def test_browse_returns_one_page_and_does_not_expand_calls():
    repo = MemoryRepo([], _legs(26))
    outcome = browse(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain=DOMAIN,
        filters=_window(),
        repo=repo,
        voip=FakeVoip(),
        page=1,
        now=NOW,
    )
    shown = {item.leg.uuid for call in outcome.calls for item in call.legs}
    assert len(shown) == 25
    assert "leg-25" in shown
    assert "leg-0" not in shown
    assert outcome.has_next is True
    assert outcome.has_prev is False
    assert outcome.page_count == 2
    assert outcome.pages_capped is False
    assert not any(call[0] == "call_uuid" for call in repo.calls)
    assert ("match", DOMAIN, 25, 0) in repo.calls
    assert len(outcome.calls[0].legs[0].voip.caller) > 0


def test_second_page_is_the_remainder():
    repo = MemoryRepo([], _legs(26))
    outcome = browse(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain=DOMAIN,
        filters=_window(),
        repo=repo,
        voip=FakeVoip(),
        page=2,
        now=NOW,
    )
    shown = [item.leg.uuid for call in outcome.calls for item in call.legs]
    assert shown == ["leg-0"]
    assert outcome.has_prev is True
    assert outcome.has_next is False
    assert outcome.page_count == 2
    assert ("match", DOMAIN, 25, 25) in repo.calls


def test_without_a_window_the_newest_legs_are_paged():
    repo = MemoryRepo([], _legs(30))
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get("/", params={"cloud": "example-us", "domain": DOMAIN})
    assert page.status_code == 200
    assert "Escolhe um intervalo até 24 horas." not in page.text
    assert "leg-29" in page.text
    assert "leg-0" not in page.text
    assert "Página 1 de 2" in page.text
    assert ("count", DOMAIN, 501) in repo.calls


def test_half_a_window_is_still_refused():
    repo = MemoryRepo([], _legs(1))
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={"cloud": "example-us", "domain": DOMAIN, "start": "2026-07-01T07:00"},
    )
    assert "Escolhe um intervalo até 24 horas." in page.text
    assert not any(call[0] == "match" for call in repo.calls)


def test_pager_offers_the_page_count_and_a_jump():
    repo = MemoryRepo([], _legs(26))
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "start": "2026-07-01T07:00",
            "end": "2026-07-01T08:00",
        },
    )
    assert page.status_code == 200
    assert "Página 1 de 2" in page.text
    assert "Primeira" in page.text
    assert "Última" in page.text
    assert 'name="page"' in page.text
    assert page.text.count("<table>") == 1


def test_page_count_stops_at_twenty():
    repo = MemoryRepo([], _legs(501))
    outcome = browse(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain=DOMAIN,
        filters=_window(),
        repo=repo,
        voip=FakeVoip(),
        page=20,
        now=NOW,
    )
    assert outcome.page_count == 20
    assert outcome.pages_capped is True
    assert outcome.has_next is False
    assert "Afina o intervalo ou os filtros." in outcome.notices


def test_page_past_the_cap_is_refused():
    repo = MemoryRepo([], _legs(1))
    with pytest.raises(SearchInputError) as exc:
        browse(
            cloud=cloud_us(),
            settings=settings_for(cloud_us()),
            domain=DOMAIN,
            filters=_window(),
            repo=repo,
            voip=FakeVoip(),
            page=21,
            now=NOW,
        )
    assert exc.value.args[0] == "page_cap"
    assert repo.calls == []


def test_direction_outside_the_list_does_not_open_the_database():
    def repo_for(cloud):
        raise AssertionError("database")

    app = create_app(settings=settings_for(cloud_us()), repo_for=repo_for, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "start": "2026-07-01T12:00",
            "end": "2026-07-01T13:00",
            "direction": "sideways",
        },
    )
    assert page.status_code == 200
    assert "A direção, o hangup cause, o servidor ou o gateway não está na lista." in page.text
    assert 'name="direction"' in page.text
    assert "inbound" in page.text
    assert "NORMAL_CLEARING" in page.text


def test_server_filter_keeps_only_that_hostname():
    legs = [
        make_leg("on-fs", "sip-a", T0, T0 + timedelta(seconds=10)),
        make_leg("other-fs", "sip-b", T0, T0 + timedelta(seconds=10), hostname="fs-2"),
    ]
    repo = MemoryRepo([], legs)
    repo.servers = ["fs-1", "fs-2"]
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "start": "2026-07-01T07:00",
            "end": "2026-07-01T08:00",
            "server": "fs-1",
        },
    )
    assert page.status_code == 200
    assert "on-fs" in page.text
    assert "other-fs" not in page.text
    assert ">fs-1</option>" in page.text
    assert ">fs-2</option>" in page.text


def test_server_outside_the_enabled_list_does_not_search():
    repo = MemoryRepo([], _legs(1))
    repo.servers = ["fs-1"]
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "start": "2026-07-01T07:00",
            "end": "2026-07-01T08:00",
            "server": "kamailio",
        },
    )
    assert page.status_code == 200
    assert "A direção, o hangup cause, o servidor ou o gateway não está na lista." in page.text
    assert not any(call[0] == "match" for call in repo.calls)


def test_gateway_filter_keeps_only_that_gateway():
    legs = [
        make_leg("on-gw", "sip-a", T0, T0 + timedelta(seconds=10), gateway="Trunk-dabbf5"),
        make_leg("other-gw", "sip-b", T0, T0 + timedelta(seconds=10), gateway="LCR1_LAX"),
    ]
    repo = MemoryRepo([], legs)
    repo.gateways = ["Trunk-dabbf5", "LCR1_LAX"]
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "start": "2026-07-01T07:00",
            "end": "2026-07-01T08:00",
            "gateway": "Trunk-dabbf5",
        },
    )
    assert page.status_code == 200
    assert "on-gw" in page.text
    assert "other-gw" not in page.text
    assert ">Trunk-dabbf5</option>" in page.text
    assert ">LCR1_LAX</option>" in page.text
    assert 'id="gateway" disabled' not in page.text


def test_gateway_outside_the_domain_list_does_not_search():
    repo = MemoryRepo([], _legs(1))
    repo.gateways = ["Trunk-dabbf5"]
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "start": "2026-07-01T07:00",
            "end": "2026-07-01T08:00",
            "gateway": "other-trunk",
        },
    )
    assert page.status_code == 200
    assert "A direção, o hangup cause, o servidor ou o gateway não está na lista." in page.text
    assert not any(call[0] == "match" for call in repo.calls)


def test_gateway_select_stays_disabled_until_a_domain_is_chosen():
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: MemoryRepo([], []), voip=FakeVoip())
    page = TestClient(app).get("/").text
    assert 'id="gateway" disabled' in page
    assert "A carregar os gateways…" in page


def test_gateways_endpoint_needs_a_domain():
    repo = MemoryRepo([], [])
    repo.gateways = ["Trunk-dabbf5"]
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    client = TestClient(app)
    missing = client.get("/gateways", params={"cloud": "example-us"})
    assert missing.json() == {"gateways": []}
    assert not any(call[0] == "gateways" for call in repo.calls)
    found = client.get("/gateways", params={"cloud": "example-us", "domain": DOMAIN})
    assert found.json() == {"gateways": ["Trunk-dabbf5"]}
    assert ("gateways", DOMAIN) in repo.calls


def test_stamps_drop_seconds_and_the_end_minute_is_included():
    filters = bounded_filters(
        LegFilters(
            start=parse_stamp("2026-07-01T11:55:30.5"),
            end=parse_stamp("2026-07-01T11:55:45"),
        ),
        require_window=True,
    )
    assert filters.start == datetime(2026, 7, 1, 11, 55, tzinfo=timezone.utc)
    assert filters.end == datetime(2026, 7, 1, 11, 55, 59, tzinfo=timezone.utc)
    inside = make_leg(
        "inside",
        "sip-inside",
        datetime(2026, 7, 1, 11, 55, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 1, 11, 55, 40, tzinfo=timezone.utc),
    )
    outside = make_leg(
        "outside",
        "sip-outside",
        datetime(2026, 7, 1, 11, 56, tzinfo=timezone.utc),
        datetime(2026, 7, 1, 11, 56, 10, tzinfo=timezone.utc),
    )
    assert leg_matches(inside, filters)
    assert not leg_matches(outside, filters)


def test_a_full_day_of_minutes_stays_inside_the_window():
    filters = bounded_filters(
        LegFilters(
            start=parse_stamp("2026-07-01T12:00:59"),
            end=parse_stamp("2026-07-02T12:00"),
        ),
        require_window=True,
    )
    assert filters.end == datetime(2026, 7, 2, 12, 0, 59, tzinfo=timezone.utc)
    with pytest.raises(SearchInputError) as exc:
        bounded_filters(
            LegFilters(
                start=parse_stamp("2026-07-01T12:00"),
                end=parse_stamp("2026-07-02T12:01"),
            ),
            require_window=True,
        )
    assert exc.value.args[0] == "span"


def test_listing_without_an_identifier_uses_minutes():
    repo = MemoryRepo([], [make_leg("leg-a", "sip-a", T0, T0 + timedelta(minutes=1))])
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "start": "2026-07-01T07:00:30",
            "end": "2026-07-01T08:00:45",
        },
    )
    assert page.status_code == 200
    assert 'step="60"' in page.text
    assert 'value="2026-07-01T07:00"' in page.text
    assert 'value="2026-07-01T08:00"' in page.text
    assert "(opcional)" in page.text
    assert "leg-a" in page.text
    assert 'value="2026-07-01T07:00:30"' not in page.text
    assert 'name="tz"' not in page.text
    assert "Limpar" in page.text
    assert 'href="/?cloud=example-us"' in page.text
    assert "America/Chicago (UTC-05:00)" in page.text
    assert "2026-07-01 07:00:00 UTC-05:00" in page.text


def test_cloud_clock_is_shown_on_the_page():
    filters = bounded_filters(
        LegFilters(
            start=parse_stamp("2026-07-01T06:55:30", "America/Chicago"),
            end=parse_stamp("2026-07-01T06:55:45", "America/Chicago"),
        ),
        require_window=True,
    )
    assert filters.start == datetime(2026, 7, 1, 11, 55, tzinfo=timezone.utc)
    assert filters.end == datetime(2026, 7, 1, 11, 55, 59, tzinfo=timezone.utc)
    repo = MemoryRepo([], [make_leg("leg-a", "sip-a", T0, T0 + timedelta(minutes=1))])
    app = create_app(settings=settings_for(cloud_us()), repo_for=lambda cloud: repo, voip=FakeVoip())
    page = TestClient(app).get(
        "/",
        params={
            "cloud": "example-us",
            "domain": DOMAIN,
            "tz": "Europe/Lisbon",
            "start": "2026-07-01T07:00",
            "end": "2026-07-01T08:00",
        },
    )
    assert "2026-07-01 07:00:00 UTC-05:00" in page.text
    assert "Europe/Lisbon" not in page.text
    assert "leg-a" in page.text
    assert 'data-uuid="leg-a"' in page.text
    assert "Clica para ver as variáveis" in page.text
    assert not any(call[0] == "leg_detail" for call in repo.calls)


def test_chicago_wall_clock_is_the_cloud_zone():
    from app.main import format_stamp
    from app.zones import cdr_wall, from_cdr_wall

    stored = datetime(2026, 10, 8, 4, 23, 18)
    assert format_stamp(from_cdr_wall(stored), "Europe/Lisbon") == "2026-10-08 10:23:18 UTC+01:00"
    filters = bounded_filters(
        LegFilters(
            start=parse_stamp("2026-10-08T10:00", "Europe/Lisbon"),
            end=parse_stamp("2026-10-08T11:00", "Europe/Lisbon"),
        ),
        require_window=True,
    )
    assert cdr_wall(filters.start) == datetime(2026, 10, 8, 4, 0)
    assert cdr_wall(filters.end) == datetime(2026, 10, 8, 5, 0, 59)
