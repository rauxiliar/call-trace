"""Read-only CDR queries. Domain names are validated before they become identifiers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.models import DetailsRow, Leg
from app.zones import cdr_wall, from_cdr_wall

DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
LEG_COLUMNS = (
    "uuid",
    "call_uuid",
    "sip_call_id",
    "direction",
    "caller_number",
    "destination",
    "hangup_cause",
    "start_stamp",
    "end_stamp",
    "billmsec",
    "hostname",
)
DETAILS_SQL = """
SELECT uuid, sip_callid, details
FROM public.freeswitch_details
WHERE uuid = %s OR sip_callid = %s
"""
LEG_DETAIL_SQL = """
SELECT apps, details
FROM public.freeswitch_details
WHERE uuid = %s
ORDER BY timestamp DESC
LIMIT 1
"""
EVENTS_SQL = """
SELECT uuid, sip_callid, apps, details
FROM public.freeswitch_details
WHERE uuid = %s OR sip_callid = %s
ORDER BY timestamp DESC
"""
DOMAINS_SQL = """
SELECT tablename FROM pg_catalog.pg_tables
WHERE schemaname = 'freeswitch'
ORDER BY tablename
"""
V_DOMAINS_SQL = """
SELECT domain_name FROM public.v_domains
ORDER BY domain_name
"""
V_SERVERS_SQL = """
SELECT hostname FROM public.v_servers
WHERE state = 'enabled' AND server_type = 'freeswitch'
ORDER BY hostname
"""
PUBLIC_GATEWAY_UUID = "11111111-1111-1111-1111-111111111111"
V_GATEWAYS_SQL = f"""
SELECT DISTINCT g.gateway
FROM public.v_gateways AS g
LEFT JOIN public.v_domains AS d ON d.domain_uuid = g.domain_uuid
WHERE g.enabled = 'true'
  AND g.gateway IS NOT NULL
  AND btrim(g.gateway) <> ''
  AND (
    g.domain_uuid = '{PUBLIC_GATEWAY_UUID}'
    OR d.domain_name = %s
  )
ORDER BY g.gateway
"""
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
PAGE_SIZE = 25
_TEXT_FILTERS = (
    ("caller_number", "caller_number"),
    ("destination", "destination"),
    ("direction", "direction"),
    ("hangup_cause", "hangup_cause"),
    ("hostname", "hostname"),
    ("gateway", "gateway"),
)


class InvalidDomain(ValueError):
    """Domain failed the identifier allowlist. Safe to show a generic message."""


class DomainTableMissing(Exception):
    def __init__(self, domain: str) -> None:
        self.domain = domain
        super().__init__(domain)


class CdrReadError(Exception):
    """Message is safe to show and does not include the DSN."""


def checked_domain(domain: str) -> str:
    if not isinstance(domain, str) or DOMAIN_RE.fullmatch(domain) is None:
        raise InvalidDomain("invalid domain")
    return domain


def allowed_domains(names: list[Any]) -> list[str]:
    return [name for name in names if isinstance(name, str) and DOMAIN_RE.fullmatch(name)]


def _cdr_wall(moment: datetime, zone: str) -> datetime:
    return cdr_wall(moment, zone)


@dataclass(frozen=True)
class LegFilters:
    start: datetime | None = None
    end: datetime | None = None
    caller_number: str = ""
    destination: str = ""
    direction: str = ""
    hangup_cause: str = ""
    hostname: str = ""
    gateway: str = ""

    def any(self) -> bool:
        return any(
            (
                self.start,
                self.end,
                self.caller_number.strip(),
                self.destination.strip(),
                self.direction.strip(),
                self.hangup_cause.strip(),
                self.hostname.strip(),
                self.gateway.strip(),
            )
        )

    def sql_clause(self, zone: str) -> tuple[sql.Composed | None, list[Any]]:
        parts: list[sql.Composed] = []
        params: list[Any] = []
        if self.start is not None:
            parts.append(sql.SQL("start_stamp >= %s"))
            params.append(_cdr_wall(self.start, zone))
        if self.end is not None:
            parts.append(sql.SQL("start_stamp <= %s"))
            params.append(_cdr_wall(self.end, zone))
        for attr, column in _TEXT_FILTERS:
            value = getattr(self, attr).strip()
            if value:
                parts.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
                params.append(value)
        if not parts:
            return None, []
        return sql.SQL(" AND ").join(parts), params


def _select_legs(domain: str, where: sql.SQL) -> sql.Composed:
    domain = checked_domain(domain)
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in LEG_COLUMNS)
    table = sql.Identifier("freeswitch", domain)
    return sql.SQL("SELECT {columns} FROM {table} WHERE {where}").format(
        columns=columns,
        table=table,
        where=where,
    )


def legs_by_call_uuid_query(domain: str) -> sql.Composed:
    return _select_legs(domain, sql.SQL("call_uuid = %s"))


def legs_by_any_id_query(domain: str) -> sql.Composed:
    return _select_legs(
        domain,
        sql.SQL("uuid = %s OR call_uuid = %s OR sip_call_id = %s"),
    )


def _with_filters(
    query: sql.Composed,
    params: tuple[Any, ...],
    filters: LegFilters | None,
    zone: str,
) -> tuple[sql.Composed, list[Any]]:
    if filters is None or not filters.any():
        return query, list(params)
    clause, extra = filters.sql_clause(zone)
    if clause is None:
        return query, list(params)
    return sql.SQL("{} AND {}").format(query, clause), [*params, *extra]


def legs_matching_query(
    domain: str,
    filters: LegFilters,
    limit: int = PAGE_SIZE,
    offset: int = 0,
    zone: str = "America/Chicago",
) -> tuple[sql.Composed, list[Any]]:
    clause, params = filters.sql_clause(zone)
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in LEG_COLUMNS)
    table = sql.Identifier("freeswitch", checked_domain(domain))
    if clause is None:
        query = sql.SQL(
            "SELECT {columns} FROM {table} ORDER BY start_stamp DESC LIMIT %s OFFSET %s"
        ).format(columns=columns, table=table)
        return query, [limit + 1, offset]
    query = sql.SQL(
        "SELECT {columns} FROM {table} WHERE {where} ORDER BY start_stamp DESC LIMIT %s OFFSET %s"
    ).format(columns=columns, table=table, where=clause)
    return query, [*params, limit + 1, offset]


def legs_count_query(
    domain: str,
    filters: LegFilters,
    zone: str = "America/Chicago",
    cap: int | None = None,
) -> tuple[sql.Composed, list[Any]]:
    clause, params = filters.sql_clause(zone)
    table = sql.Identifier("freeswitch", checked_domain(domain))
    if cap is None:
        if clause is None:
            raise ValueError("missing filters")
        query = sql.SQL("SELECT count(*) FROM {table} WHERE {where}").format(
            table=table,
            where=clause,
        )
        return query, params
    if clause is None:
        query = sql.SQL(
            "SELECT count(*) FROM (SELECT 1 FROM {table} ORDER BY start_stamp DESC LIMIT %s) AS recent"
        ).format(table=table)
        return query, [cap]
    query = sql.SQL(
        "SELECT count(*) FROM (SELECT 1 FROM {table} WHERE {where} ORDER BY start_stamp DESC LIMIT %s) AS recent"
    ).format(table=table, where=clause)
    return query, [*params, cap]


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, options=READ_ONLY_OPTIONS, connect_timeout=10)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _details_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _billmsec(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def leg_from_mapping(row: dict[str, Any], domain: str, zone: str) -> Leg:
    return Leg(
        uuid=_text(row.get("uuid")),
        call_uuid=_text(row.get("call_uuid")),
        sip_call_id=_text(row.get("sip_call_id")),
        direction=_text(row.get("direction")),
        caller_number=_text(row.get("caller_number")),
        destination=_text(row.get("destination")),
        hangup_cause=_text(row.get("hangup_cause")),
        start_stamp=from_cdr_wall(row.get("start_stamp"), zone),
        end_stamp=from_cdr_wall(row.get("end_stamp"), zone),
        billmsec=_billmsec(row.get("billmsec")),
        hostname=_text(row.get("hostname")),
        domain=domain,
    )


class PostgresCdrRepository:
    def __init__(self, dsn: str, domains_dsn: str = "", timezone: str = "America/Chicago") -> None:
        self._dsn = dsn
        self._domains_dsn = domains_dsn
        self._timezone = timezone

    def fetch_details(self, search_id: str) -> list[DetailsRow]:
        try:
            with connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(DETAILS_SQL, (search_id, search_id))
                    rows = cur.fetchall()
        except Exception as exc:
            raise CdrReadError("Não foi possível ler a CDR deste cloud.") from exc
        return [
            DetailsRow(
                uuid=_text(row.get("uuid")),
                sip_callid=_text(row.get("sip_callid")),
                details=_details_text(row.get("details")),
            )
            for row in rows
        ]

    def fetch_leg_detail(self, uuid: str) -> DetailsRow | None:
        try:
            with connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(LEG_DETAIL_SQL, (uuid,))
                    row = cur.fetchone()
        except Exception as exc:
            raise CdrReadError("Não foi possível ler a CDR deste cloud.") from exc
        if not row:
            return None
        return DetailsRow(
            uuid=uuid,
            sip_callid="",
            details=_details_text(row.get("details")),
            apps=_details_text(row.get("apps")),
        )

    def fetch_events(self, search_id: str) -> list[DetailsRow]:
        try:
            with connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(EVENTS_SQL, (search_id, search_id))
                    rows = cur.fetchall()
        except Exception as exc:
            raise CdrReadError("Não foi possível ler a CDR deste cloud.") from exc
        return [
            DetailsRow(
                uuid=_text(row.get("uuid")),
                sip_callid=_text(row.get("sip_callid")),
                details=_details_text(row.get("details")),
                apps=_details_text(row.get("apps")),
            )
            for row in rows
        ]

    def list_domains(self) -> list[str]:
        if self._domains_dsn:
            dsn, query = self._domains_dsn, V_DOMAINS_SQL
            message = "Não foi possível ler os domínios deste cloud."
        else:
            dsn, query = self._dsn, DOMAINS_SQL
            message = "Não foi possível ler a CDR deste cloud."
        try:
            with connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    names = [row[0] for row in cur.fetchall()]
        except Exception as exc:
            raise CdrReadError(message) from exc
        return allowed_domains(names)

    def list_servers(self) -> list[str]:
        dsn = self._domains_dsn or self._dsn
        try:
            with connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(V_SERVERS_SQL)
                    names = [row[0] for row in cur.fetchall()]
        except Exception as exc:
            raise CdrReadError("Não foi possível ler os servidores deste cloud.") from exc
        return allowed_domains(names)

    def list_gateways(self, domain: str) -> list[str]:
        checked_domain(domain)
        dsn = self._domains_dsn or self._dsn
        try:
            with connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(V_GATEWAYS_SQL, (domain,))
                    names = [row[0] for row in cur.fetchall()]
        except InvalidDomain:
            raise
        except Exception as exc:
            raise CdrReadError("Não foi possível ler os gateways deste cloud.") from exc
        return allowed_domains(names)

    def fetch_legs_by_call_uuid(
        self,
        domain: str,
        call_uuid: str,
        filters: LegFilters | None = None,
    ) -> list[Leg]:
        query, params = _with_filters(
            legs_by_call_uuid_query(domain), (call_uuid,), filters, self._timezone
        )
        return self._fetch_legs(query, params, domain)

    def fetch_legs_by_any_id(
        self,
        domain: str,
        search_id: str,
        filters: LegFilters | None = None,
    ) -> list[Leg]:
        query, params = _with_filters(
            legs_by_any_id_query(domain),
            (search_id, search_id, search_id),
            filters,
            self._timezone,
        )
        return self._fetch_legs(query, params, domain)

    def fetch_legs_matching(
        self,
        domain: str,
        filters: LegFilters,
        limit: int = PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[list[Leg], bool]:
        query, params = legs_matching_query(domain, filters, limit, offset, self._timezone)
        rows = self._fetch_legs(query, params, domain)
        return rows[:limit], len(rows) > limit

    def count_legs(self, domain: str, filters: LegFilters, cap: int | None = None) -> int:
        query, params = legs_count_query(domain, filters, self._timezone, cap)
        checked_domain(domain)
        try:
            with connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
        except psycopg.errors.UndefinedTable as exc:
            raise DomainTableMissing(domain) from exc
        except InvalidDomain:
            raise
        except Exception as exc:
            raise CdrReadError("Não foi possível ler a CDR deste cloud.") from exc
        return int(row[0] if row else 0)

    def _fetch_legs(self, query: sql.Composed, params: list[Any] | tuple[Any, ...], domain: str) -> list[Leg]:
        checked_domain(domain)
        try:
            with connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
        except psycopg.errors.UndefinedTable as exc:
            raise DomainTableMissing(domain) from exc
        except InvalidDomain:
            raise
        except Exception as exc:
            raise CdrReadError("Não foi possível ler a CDR deste cloud.") from exc
        return [leg_from_mapping(row, domain, self._timezone) for row in rows]
