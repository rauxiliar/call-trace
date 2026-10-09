from pathlib import Path

import psycopg
from psycopg import sql

from datetime import timedelta

from app.db import (
    DETAILS_SQL,
    EVENTS_SQL,
    LEG_DETAIL_SQL,
    InvalidDomain,
    LegFilters,
    READ_ONLY_OPTIONS,
    connect,
    legs_by_any_id_query,
    legs_by_call_uuid_query,
    legs_matching_query,
)
from tests.support import T0


def _walk(node):
    if isinstance(node, sql.Composed):
        for part in node:
            yield from _walk(part)
    else:
        yield node


def _ident_strings(node) -> tuple[str, ...] | None:
    if not isinstance(node, sql.Identifier):
        return None
    value = getattr(node, "strings", None)
    if value is None:
        value = getattr(node, "_obj", None)
    return tuple(value)


def _sql_text(node) -> str:
    chunks: list[str] = []
    for part in _walk(node):
        if isinstance(part, sql.SQL):
            text = getattr(part, "string", None)
            if text is None:
                text = getattr(part, "_obj", "")
            chunks.append(text)
    return "".join(chunks)


def test_domain_table_is_a_quoted_identifier():
    query = legs_by_call_uuid_query("example.com")
    idents = [_ident_strings(part) for part in _walk(query)]
    assert ("freeswitch", "example.com") in idents
    text = _sql_text(query)
    assert "call_uuid = %s" in text
    assert "example.com" not in text


def test_any_id_query_keeps_values_as_parameters():
    query = legs_by_any_id_query("tenant.example")
    text = _sql_text(query)
    assert "uuid = %s OR call_uuid = %s OR sip_call_id = %s" in text
    assert "tenant.example" not in text
    assert ("freeswitch", "tenant.example") in [_ident_strings(part) for part in _walk(query)]


def test_injection_never_builds_sql():
    for bad in (
        'foo"; DROP TABLE freeswitch_details;--',
        "a b",
        "dom;drop",
        "x' OR '1'='1",
        "../etc",
    ):
        try:
            legs_by_call_uuid_query(bad)
        except InvalidDomain:
            pass
        else:
            raise AssertionError(bad)
        try:
            legs_by_any_id_query(bad)
        except InvalidDomain:
            pass
        else:
            raise AssertionError(bad)


def test_page_query_keeps_filters_as_parameters():
    filters = LegFilters(
        start=T0,
        end=T0 + timedelta(hours=1),
        direction="inbound",
        hangup_cause="NORMAL_CLEARING",
        hostname="fs-1",
    )
    query, params = legs_matching_query("tenant.example", filters, 25, 50)
    text = _sql_text(query)
    assert "ORDER BY start_stamp DESC LIMIT %s OFFSET %s" in text
    assert any(_ident_strings(part) == ("hostname",) for part in _walk(query))
    assert "inbound" not in text
    assert "NORMAL_CLEARING" not in text
    assert "fs-1" not in text
    assert "fs-1" in params
    assert "count(" not in text.lower()
    from app.db import legs_count_query

    count_query, count_params = legs_count_query("tenant.example", filters)
    count_text = _sql_text(count_query).lower()
    assert count_text.startswith("select count(*)")
    assert "inbound" not in count_text
    assert count_params[:2] == params[:2]
    assert params[-2:] == [26, 50]


def test_recent_query_has_no_where_and_the_count_stops_early():
    filters = LegFilters()
    query, params = legs_matching_query("tenant.example", filters, 25, 0)
    text = _sql_text(query).lower()
    assert "order by start_stamp desc limit %s offset %s" in text
    assert " where " not in text
    assert params == [26, 0]
    from app.db import legs_count_query

    count_query, count_params = legs_count_query("tenant.example", filters, cap=501)
    count_text = _sql_text(count_query).lower()
    assert "order by start_stamp desc limit %s" in count_text
    assert " where " not in count_text
    assert count_params == [501]


def test_domain_name_query_reads_v_domains():
    from app.db import V_DOMAINS_SQL

    lowered = V_DOMAINS_SQL.lower()
    assert "select domain_name from public.v_domains" in lowered
    assert "pg_tables" not in lowered


def test_gateway_query_reads_public_and_domain_gateways():
    from app.db import PUBLIC_GATEWAY_UUID, V_GATEWAYS_SQL

    lowered = V_GATEWAYS_SQL.lower()
    assert "from public.v_gateways" in lowered
    assert "join public.v_domains" in lowered
    assert PUBLIC_GATEWAY_UUID in V_GATEWAYS_SQL
    assert "d.domain_name = %s" in lowered
    assert "password" not in lowered
    assert "username" not in lowered
    assert "proxy" not in lowered


def test_gateway_filter_stays_a_parameter():
    filters = LegFilters(start=T0, end=T0 + timedelta(hours=1), gateway="Trunk-dabbf5")
    query, params = legs_matching_query("tenant.example", filters)
    text = _sql_text(query)
    assert any(_ident_strings(part) == ("gateway",) for part in _walk(query))
    assert "Trunk-dabbf5" not in text
    assert "Trunk-dabbf5" in params


def test_server_query_reads_enabled_freeswitch_hostnames():
    from app.db import V_SERVERS_SQL

    lowered = V_SERVERS_SQL.lower()
    assert "from public.v_servers" in lowered
    assert "state = 'enabled'" in lowered
    assert "server_type = 'freeswitch'" in lowered
    assert "password" not in lowered


def test_details_query_is_parameterized():
    assert "uuid = %s OR sip_callid = %s" in DETAILS_SQL
    lowered = DETAILS_SQL.lower()
    assert lowered.strip().startswith("select")
    detail = LEG_DETAIL_SQL.lower()
    assert "where uuid = %s" in detail
    assert "order by timestamp desc" in detail
    assert "limit 1" in detail
    events = EVENTS_SQL.lower()
    assert "uuid = %s or sip_callid = %s" in events
    assert "apps, details" in events
    for source in (lowered, detail, events):
        for word in ("insert ", "update ", "delete ", "drop ", "alter ", "truncate "):
            assert word not in source


def test_sources_only_select():
    root = Path(__file__).resolve().parents[1] / "app"
    banned = ("insert into", "update ", "delete from", "drop table", "alter table", "truncate ")
    for name in ("db.py", "cdr.py"):
        text = (root / name).read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in text, name


def test_connect_is_read_only(monkeypatch):
    captured: dict = {}

    def fake_connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    connect("postgres://calltrace_ro:secret@localhost/cdrs")
    assert captured["kwargs"]["options"] == READ_ONLY_OPTIONS
    assert "default_transaction_read_only=on" in captured["kwargs"]["options"]
