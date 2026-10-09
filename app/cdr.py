"""Correlate one chosen cloud's FreeSWITCH CDR with VoIPmonitor and Grafana."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Protocol

from app.config import Cloud, Settings
from app.db import PAGE_SIZE, DomainTableMissing, InvalidDomain, LegFilters, checked_domain
from app.grafana import as_utc, explore_url, loki_uid, logql
from app.models import DetailsRow, Leg, VoipInfo
from app.voipmonitor import display_voip

FIVE_MINUTES = timedelta(minutes=5)
SEVEN_DAYS = timedelta(days=7)
MAX_WINDOW = timedelta(hours=24)
MAX_ID_LEN = 256
MAX_FILTER_LEN = 64
MAX_PAGE = 20
RELATED_CAP = 40
LISTED_CAP = 200
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
DIRECTIONS = ("inbound", "outbound", "local")
HANGUP_CAUSES = (
    "NONE",
    "UNALLOCATED_NUMBER",
    "NO_ROUTE_TRANSIT_NET",
    "NO_ROUTE_DESTINATION",
    "CHANNEL_UNACCEPTABLE",
    "CALL_AWARDED_DELIVERED",
    "NORMAL_CLEARING",
    "USER_BUSY",
    "NO_USER_RESPONSE",
    "NO_ANSWER",
    "SUBSCRIBER_ABSENT",
    "CALL_REJECTED",
    "NUMBER_CHANGED",
    "REDIRECTION_TO_NEW_DESTINATION",
    "EXCHANGE_ROUTING_ERROR",
    "DESTINATION_OUT_OF_ORDER",
    "INVALID_NUMBER_FORMAT",
    "FACILITY_REJECTED",
    "RESPONSE_TO_STATUS_ENQUIRY",
    "NORMAL_UNSPECIFIED",
    "NORMAL_CIRCUIT_CONGESTION",
    "NETWORK_OUT_OF_ORDER",
    "NORMAL_TEMPORARY_FAILURE",
    "SWITCH_CONGESTION",
    "ACCESS_INFO_DISCARDED",
    "REQUESTED_CHAN_UNAVAIL",
    "PRE_EMPTED",
    "FACILITY_NOT_SUBSCRIBED",
    "OUTGOING_CALL_BARRED",
    "INCOMING_CALL_BARRED",
    "BEARERCAPABILITY_NOTAUTH",
    "BEARERCAPABILITY_NOTAVAIL",
    "SERVICE_UNAVAILABLE",
    "BEARERCAPABILITY_NOTIMPL",
    "CHAN_NOT_IMPLEMENTED",
    "FACILITY_NOT_IMPLEMENTED",
    "SERVICE_NOT_IMPLEMENTED",
    "INVALID_CALL_REFERENCE",
    "INCOMPATIBLE_DESTINATION",
    "INVALID_MSG_UNSPECIFIED",
    "MANDATORY_IE_MISSING",
    "MESSAGE_TYPE_NONEXIST",
    "WRONG_MESSAGE",
    "IE_NONEXIST",
    "INVALID_IE_CONTENTS",
    "WRONG_CALL_STATE",
    "RECOVERY_ON_TIMER_EXPIRE",
    "MANDATORY_IE_LENGTH_ERROR",
    "PROTOCOL_ERROR",
    "INTERWORKING",
    "SUCCESS",
    "ORIGINATOR_CANCEL",
    "CRASH",
    "SYSTEM_SHUTDOWN",
    "LOSE_RACE",
    "MANAGER_REQUEST",
    "BLIND_TRANSFER",
    "ATTENDED_TRANSFER",
    "ALLOTTED_TIMEOUT",
    "USER_CHALLENGE",
    "MEDIA_TIMEOUT",
    "PICKED_OFF",
    "USER_NOT_REGISTERED",
    "PROGRESS_TIMEOUT",
    "INVALID_GATEWAY",
    "GATEWAY_DOWN",
    "INVALID_URL",
    "INVALID_PROFILE",
    "NO_PICKUP",
    "SRTP_READ_ERROR",
    "BOWOUT",
    "BUSY_EVERYWHERE",
    "DECLINE",
    "DOES_NOT_EXIST_ANYWHERE",
    "NOT_ACCEPTABLE",
    "UNWANTED",
    "NO_IDENTITY",
    "BAD_IDENTITY_INFO",
    "UNSUPPORTED_CERTIFICATE",
    "INVALID_IDENTITY",
    "STALE_DATE",
    "REJECT_ALL",
)
DOMAIN_KEYS = ("variable_domain_name", "variable_domain")
CALL_UUID_KEYS = ("Channel-Call-UUID",)


class SearchInputError(ValueError):
    pass


class CdrRepository(Protocol):
    def fetch_details(self, search_id: str) -> list[DetailsRow]: ...

    def fetch_events(self, search_id: str) -> list[DetailsRow]: ...

    def fetch_legs_by_call_uuid(self, domain: str, call_uuid: str) -> list[Leg]: ...

    def fetch_legs_by_any_id(self, domain: str, search_id: str) -> list[Leg]: ...


class VoipClient(Protocol):
    def get_voip_calls(self, call_id: str, start: datetime, end: datetime) -> dict[str, Any]: ...

    def get_share_url(self, call_id: str) -> dict[str, Any]: ...


@dataclass
class LegView:
    leg: Leg
    voip: VoipInfo
    logs_url: str | None


@dataclass
class CallView:
    call_uuid: str
    logs_url: str | None
    legs: list[LegView]


@dataclass
class SearchOutcome:
    cdr_not_found: bool = False
    notices: list[str] = field(default_factory=list)
    calls: list[CallView] = field(default_factory=list)
    voip_only: list[VoipInfo] = field(default_factory=list)
    page: int = 1
    page_count: int = 0
    pages_capped: bool = False
    has_prev: bool = False
    has_next: bool = False


def checked_search_id(value: str) -> str:
    text = value.strip()
    if not text or "\x00" in text or len(text) > MAX_ID_LEN:
        raise SearchInputError("invalid id")
    return text


def optional_domain(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return checked_domain(text)


def checked_filter_text(value: str) -> str:
    text = value.strip()
    if "\x00" in text or len(text) > MAX_FILTER_LEN:
        raise SearchInputError("filter")
    return text


def checked_choice(value: str, allowed: tuple[str, ...]) -> str:
    text = value.strip()
    if text and text not in allowed:
        raise SearchInputError("choice")
    return text


def checked_page(value: str) -> int:
    text = value.strip()
    if not text:
        return 1
    if not text.isdigit():
        raise SearchInputError("page")
    page = int(text)
    if page < 1:
        raise SearchInputError("page")
    if page > MAX_PAGE:
        raise SearchInputError("page_cap")
    return page


def _on_minute(moment: datetime) -> datetime:
    return as_utc(moment).replace(second=0, microsecond=0)


def parse_stamp(value: str, zone_name: str = "UTC") -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SearchInputError("stamp") from None
    wall = parsed.replace(second=0, microsecond=0)
    if wall.tzinfo is None:
        wall = wall.replace(tzinfo=ZoneInfo(zone_name or "UTC"))
    return as_utc(wall)


def checked_window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    if start is None or end is None:
        raise SearchInputError("window")
    start_utc = _on_minute(start)
    end_utc = _on_minute(end)
    if end_utc < start_utc:
        raise SearchInputError("order")
    if end_utc - start_utc > MAX_WINDOW:
        raise SearchInputError("span")
    return start_utc, end_utc


def _through_end_minute(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return _on_minute(moment).replace(second=59)


def bounded_filters(filters: LegFilters, *, require_window: bool) -> LegFilters:
    if require_window:
        start, end = checked_window(filters.start, filters.end)
    else:
        start = _on_minute(filters.start) if filters.start else None
        end = _on_minute(filters.end) if filters.end else None
        if start and end and end < start:
            raise SearchInputError("order")
    return LegFilters(
        start=start,
        end=_through_end_minute(end),
        caller_number=checked_filter_text(filters.caller_number),
        destination=checked_filter_text(filters.destination),
        direction=checked_choice(filters.direction, DIRECTIONS),
        hangup_cause=checked_choice(filters.hangup_cause, HANGUP_CAUSES),
        hostname=checked_filter_text(filters.hostname),
        gateway=checked_filter_text(filters.gateway),
    )


def _parse_details(raw: str) -> Any:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return None
    return parsed


def _search_keys(node: Any, keys: tuple[str, ...], depth: int = 0) -> str | None:
    if not isinstance(node, dict) or depth > 2:
        return None
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in node.values():
        if isinstance(value, dict):
            found = _search_keys(value, keys, depth + 1)
            if found:
                return found
    return None


def event_domain_and_call_uuid(details: str) -> tuple[str | None, str | None]:
    parsed = _parse_details(details)
    return _search_keys(parsed, DOMAIN_KEYS), _search_keys(parsed, CALL_UUID_KEYS)


def _dedupe(legs: list[Leg]) -> list[Leg]:
    seen: set[tuple[str, str, datetime | None]] = set()
    unique: list[Leg] = []
    for leg in legs:
        key = (leg.domain, leg.uuid, leg.end_stamp)
        if key in seen:
            continue
        seen.add(key)
        unique.append(leg)
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    unique.sort(
        key=lambda leg: (
            as_utc(leg.start_stamp) if leg.start_stamp else far_future,
            leg.uuid,
        )
    )
    return unique


def _span(legs: list[Leg]) -> tuple[datetime, datetime] | None:
    starts = [as_utc(leg.start_stamp) for leg in legs if leg.start_stamp]
    ends = [as_utc(leg.end_stamp) for leg in legs if leg.end_stamp]
    if not starts and not ends:
        return None
    start = min(starts) if starts else min(ends)
    end = max(ends) if ends else max(starts)
    return start - FIVE_MINUTES, end + FIVE_MINUTES


def _safe_http(url: str | None) -> str:
    if not url:
        return ""
    if url.startswith(("https://", "http://")):
        return url
    return ""


def _add_notice(notices: list[str], message: str) -> None:
    if message not in notices:
        notices.append(message)


def _by_call_uuid(repo: CdrRepository, domain: str, call_uuid: str, filters: LegFilters | None) -> list[Leg]:
    if filters is not None and filters.any():
        return repo.fetch_legs_by_call_uuid(domain, call_uuid, filters)
    return repo.fetch_legs_by_call_uuid(domain, call_uuid)


def _by_any_id(repo: CdrRepository, domain: str, search_id: str, filters: LegFilters | None) -> list[Leg]:
    if filters is not None and filters.any():
        return repo.fetch_legs_by_any_id(domain, search_id, filters)
    return repo.fetch_legs_by_any_id(domain, search_id)


def _legs_from_details(
    rows: list[DetailsRow],
    form_domain: str | None,
    repo: CdrRepository,
    notices: list[str],
    filters: LegFilters | None = None,
) -> list[Leg]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        event_domain, call_uuid = event_domain_and_call_uuid(row.details)
        chosen: str | None = None
        if event_domain:
            try:
                chosen = checked_domain(event_domain)
            except InvalidDomain:
                chosen = None
        if chosen is None:
            chosen = form_domain
        if not chosen:
            _add_notice(notices, "O evento não tem um domínio válido; as pernas não foram lidas.")
            continue
        if not call_uuid:
            _add_notice(notices, "O evento não inclui Channel-Call-UUID.")
            continue
        key = (chosen, call_uuid)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)

    legs: list[Leg] = []
    for domain, call_uuid in pairs:
        try:
            legs.extend(_by_call_uuid(repo, domain, call_uuid, filters))
        except DomainTableMissing:
            _add_notice(notices, f"A tabela do domínio {domain} não existe neste cloud.")
    return legs


def _legs_from_domain(
    domain: str,
    search_id: str,
    repo: CdrRepository,
    notices: list[str],
    filters: LegFilters | None = None,
) -> list[Leg]:
    try:
        matched = _by_any_id(repo, domain, search_id, filters)
    except DomainTableMissing:
        _add_notice(notices, f"A tabela do domínio {domain} não existe neste cloud.")
        return []
    call_uuids: list[str] = []
    seen: set[str] = set()
    for leg in matched:
        if leg.call_uuid and leg.call_uuid not in seen:
            seen.add(leg.call_uuid)
            call_uuids.append(leg.call_uuid)
    if not call_uuids:
        return matched
    expanded: list[Leg] = []
    for call_uuid in call_uuids:
        try:
            expanded.extend(_by_call_uuid(repo, domain, call_uuid, filters))
        except DomainTableMissing:
            _add_notice(notices, f"A tabela do domínio {domain} não existe neste cloud.")
    return expanded or matched


def _logs_link(
    settings: Settings,
    cloud: Cloud,
    identifier: str,
    start: datetime,
    end: datetime,
    zone_name: str = "UTC",
) -> str | None:
    exprs = logql(cloud.namespace, identifier, cloud.id)
    uid = loki_uid(settings, cloud.region)
    if not exprs or not uid:
        return None
    return explore_url(settings.grafana_base_url, uid, exprs, start, end, zone_name)


def _voip_for(
    call_id: str,
    start: datetime,
    end: datetime,
    voip: VoipClient,
    cache: dict[tuple[str, datetime, datetime], VoipInfo],
) -> VoipInfo:
    key = (call_id, start, end)
    cached = cache.get(key)
    if cached is not None:
        return cached
    found = voip.get_voip_calls(call_id, start, end)
    error = str(found.get("error") or "")
    rows = found.get("rows") or []
    info = display_voip(rows[0] if rows else None)
    info.missing = not rows
    info.error = error
    if rows and not error:
        shared = voip.get_share_url(call_id)
        info.share_url = _safe_http(shared.get("url"))
        if shared.get("error") and not info.share_url:
            info.error = str(shared["error"])
    cache[key] = info
    return info


def _window_for(
    legs: list[Leg],
    call_span: tuple[datetime, datetime] | None,
    now: datetime,
    notices: list[str],
) -> tuple[datetime, datetime]:
    own = _span(legs)
    if own:
        return own
    if call_span:
        return call_span
    _add_notice(
        notices,
        "Sem carimbos na CDR; a janela VoIPmonitor foi alargada para 7 dias.",
    )
    return now - SEVEN_DAYS, now


def call_variables(details: str) -> list[tuple[str, str]]:
    parsed = _parse_details(details)
    if not isinstance(parsed, dict):
        return []
    rows: list[tuple[str, str]] = []
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.startswith("variable_") or value is None:
            continue
        rows.append((key.removeprefix("variable_"), _detail_text(value)))
    rows.sort(key=lambda item: item[0])
    return rows


def dialplan_steps(apps: str, details: str) -> list[dict[str, str]]:
    parsed = _parse_details(apps) if apps.strip() else None
    if parsed is None:
        event = _parse_details(details)
        if isinstance(event, dict):
            parsed = event.get("running_apps")
            if isinstance(parsed, str):
                parsed = _parse_details(parsed)
    if isinstance(parsed, dict):
        nested = parsed.get("application")
        if nested is None:
            nested = parsed.get("applications")
        parsed = nested
    if not isinstance(parsed, list):
        return []
    steps: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        response = item.get("res")
        if response is None:
            response = item.get("response")
        steps.append(
            {
                "app": _detail_text(item.get("app")),
                "data": _detail_text(item.get("data")),
                "res": _detail_text(response),
            }
        )
    return steps


def _detail_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _build_calls(
    legs: list[Leg],
    cloud: Cloud,
    settings: Settings,
    voip: VoipClient,
    now: datetime,
    notices: list[str],
) -> list[CallView]:
    groups: dict[str, list[Leg]] = {}
    order: list[str] = []
    for leg in legs:
        call_uuid = leg.call_uuid or leg.uuid
        if call_uuid not in groups:
            groups[call_uuid] = []
            order.append(call_uuid)
        groups[call_uuid].append(leg)

    cache: dict[tuple[str, datetime, datetime], VoipInfo] = {}
    calls: list[CallView] = []
    for call_uuid in order:
        group = groups[call_uuid]
        call_span = _span(group)
        if call_span is None:
            call_span = (now - SEVEN_DAYS, now)
            _add_notice(
                notices,
                "Sem carimbos na CDR; a janela de logs e do VoIPmonitor foi alargada para 7 dias.",
            )
        logs_url = _logs_link(settings, cloud, call_uuid, call_span[0], call_span[1], cloud.timezone)
        leg_views: list[LegView] = []
        for leg in group:
            if leg.sip_call_id:
                siblings = [item for item in group if item.sip_call_id == leg.sip_call_id]
                window = _window_for(siblings, call_span, now, notices)
                info = _voip_for(leg.sip_call_id, window[0], window[1], voip, cache)
            else:
                info = VoipInfo(missing=True)
            leg_logs = _logs_link(settings, cloud, leg.uuid, call_span[0], call_span[1], cloud.timezone)
            leg_views.append(LegView(leg=leg, voip=info, logs_url=leg_logs))
        calls.append(CallView(call_uuid=call_uuid, logs_url=logs_url, legs=leg_views))
    return calls


def _uuids_in(value: Any) -> list[str]:
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_uuids_in(item))
        return found
    if not isinstance(value, str):
        return []
    return [match.group(0) for match in _UUID_RE.finditer(value)]


def _unique_uuids(values: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(value)
    return found


def linked_uuids(details: str, apps: str) -> list[str]:
    """Leg UUIDs named by one CDR event, the same links call-insights followed."""
    parsed = _parse_details(details)
    found: list[str] = []
    if isinstance(parsed, dict):
        found.extend(_uuids_in(parsed.get("variable_originated_legs")))
        for key in (
            "Other-Leg-Unique-ID",
            "variable_other_loopback_leg_uuid",
            "variable_originating_leg_uuid",
        ):
            found.extend(_uuids_in(parsed.get(key)))
    for step in dialplan_steps(apps, details):
        if step.get("app") != "uuid_bridge":
            continue
        found.extend(_uuids_in(step.get("data")))
    return _unique_uuids(found)


def resolved_domain(details: str, fallback: str | None) -> str | None:
    parsed = _parse_details(details)
    raw = _search_keys(parsed, DOMAIN_KEYS) if isinstance(parsed, dict) else None
    if not raw:
        return fallback
    try:
        return checked_domain(raw)
    except InvalidDomain:
        return None


def _event_sip(event: DetailsRow) -> str:
    parsed = _parse_details(event.details)
    raw = ""
    if isinstance(parsed, dict):
        value = parsed.get("variable_sip_call_id")
        if isinstance(value, str):
            raw = value.strip()
    if not raw:
        raw = event.sip_callid.strip()
    if not raw or "\x00" in raw or len(raw) > MAX_ID_LEN:
        return ""
    return raw


def listed_call_ids(values: list[str]) -> set[str]:
    chosen: set[str] = set()
    for value in values[:LISTED_CAP]:
        try:
            chosen.add(checked_search_id(value).lower())
        except SearchInputError:
            continue
    return chosen


def _enqueue(pending: list[str], seen: set[str], value: str) -> None:
    if not value or value in seen or value in pending:
        return
    pending.append(value)


def related(
    *,
    cloud: Cloud,
    settings: Settings,
    domain: str | None,
    call_uuid: str,
    repo: CdrRepository,
    voip: VoipClient,
    now: datetime | None = None,
    listed: set[str] | None = None,
) -> SearchOutcome:
    form_domain = optional_domain(domain)
    if not form_domain:
        raise SearchInputError("domain")
    seed_id = checked_search_id(call_uuid)
    listed_ids = {item.lower() for item in (listed or set())}
    moment = as_utc(now or datetime.now(timezone.utc))
    notices: list[str] = []
    try:
        seed_legs = repo.fetch_legs_by_call_uuid(form_domain, seed_id)
    except DomainTableMissing:
        _add_notice(notices, f"A tabela do domínio {form_domain} não existe neste cloud.")
        return SearchOutcome(notices=notices)
    seed_uuids = {leg.uuid.lower() for leg in seed_legs}
    seed_uuids.add(seed_id.lower())
    pending: list[str] = []
    seen: set[str] = set()
    found: list[Leg] = []
    found_ids: set[str] = set()
    _enqueue(pending, seen, seed_id)
    for leg in seed_legs:
        _enqueue(pending, seen, leg.uuid)
        _enqueue(pending, seen, leg.sip_call_id)

    def add_leg(leg_domain: str | None, leg_uuid: str) -> None:
        if len(found) >= RELATED_CAP or not leg_domain or leg_uuid.lower() in seed_uuids:
            return
        if leg_uuid.lower() in found_ids:
            return
        try:
            matched = repo.fetch_legs_by_any_id(leg_domain, leg_uuid)
        except DomainTableMissing:
            _add_notice(notices, f"A tabela do domínio {leg_domain} não existe neste cloud.")
            return
        chosen = next((leg for leg in matched if leg.uuid.lower() == leg_uuid.lower()), None)
        if chosen is None or chosen.uuid.lower() in seed_uuids or chosen.uuid.lower() in found_ids:
            return
        if (chosen.call_uuid or chosen.uuid).lower() in listed_ids:
            return
        found.append(chosen)
        found_ids.add(chosen.uuid.lower())

    while pending and len(found) < RELATED_CAP:
        current = pending.pop(0)
        seen.add(current)
        for event in repo.fetch_events(current):
            leg_domain = resolved_domain(event.details, form_domain)
            sip = _event_sip(event)
            _enqueue(pending, seen, sip)
            if event.uuid:
                add_leg(leg_domain, event.uuid)
            for leg_uuid in linked_uuids(event.details, event.apps):
                _enqueue(pending, seen, leg_uuid)
                add_leg(leg_domain, leg_uuid)
                if len(found) >= RELATED_CAP:
                    break

    legs = _dedupe(found)
    if not legs:
        _add_notice(notices, "Sem chamadas relacionadas.")
        return SearchOutcome(notices=notices)
    calls = _build_calls(legs, cloud, settings, voip, moment, notices)
    return SearchOutcome(notices=notices, calls=calls)


def correlate(
    *,
    cloud: Cloud,
    settings: Settings,
    domain: str | None,
    search_id: str,
    repo: CdrRepository,
    voip: VoipClient,
    now: datetime | None = None,
    filters: LegFilters | None = None,
) -> SearchOutcome:
    search_id = checked_search_id(search_id)
    form_domain = optional_domain(domain)
    if not form_domain:
        raise SearchInputError("domain")
    active = bounded_filters(filters, require_window=False) if filters and filters.any() else None
    moment = as_utc(now or datetime.now(timezone.utc))
    notices: list[str] = []
    details = repo.fetch_details(search_id)

    if details:
        legs = _legs_from_details(details, form_domain, repo, notices, active)
    else:
        legs = _legs_from_domain(form_domain, search_id, repo, notices, active)

    legs = _dedupe(legs)
    if not legs:
        _add_notice(notices, "Nenhuma perna encontrada.")
        return SearchOutcome(notices=notices)

    calls = _build_calls(legs, cloud, settings, voip, moment, notices)
    return SearchOutcome(notices=notices, calls=calls)


def browse(
    *,
    cloud: Cloud,
    settings: Settings,
    domain: str | None,
    filters: LegFilters,
    repo: CdrRepository,
    voip: VoipClient,
    page: int = 1,
    now: datetime | None = None,
) -> SearchOutcome:
    form_domain = optional_domain(domain)
    if not form_domain:
        raise SearchInputError("domain")
    if page < 1 or page > MAX_PAGE:
        raise SearchInputError("page" if page < 1 else "page_cap")
    needs_window = filters.start is not None or filters.end is not None
    narrowed = bounded_filters(filters, require_window=needs_window)
    moment = as_utc(now or datetime.now(timezone.utc))
    notices: list[str] = []
    offset = (page - 1) * PAGE_SIZE
    recent_cap = None if needs_window else MAX_PAGE * PAGE_SIZE + 1
    try:
        total = repo.count_legs(form_domain, narrowed, recent_cap)
        matched, _more = repo.fetch_legs_matching(form_domain, narrowed, PAGE_SIZE, offset)
    except DomainTableMissing:
        _add_notice(notices, f"A tabela do domínio {form_domain} não existe neste cloud.")
        return SearchOutcome(notices=notices, page=page, has_prev=page > 1)
    page_count = min(MAX_PAGE, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 0
    pages_capped = total > MAX_PAGE * PAGE_SIZE
    has_next = page < page_count
    if pages_capped and page == MAX_PAGE:
        _add_notice(notices, "Afina o intervalo ou os filtros.")

    legs = _dedupe(matched)
    if not legs:
        _add_notice(notices, "Nenhuma perna encontrada.")
        return SearchOutcome(
            notices=notices,
            page=page,
            page_count=page_count,
            pages_capped=pages_capped,
            has_prev=page > 1,
            has_next=has_next,
        )
    calls = _build_calls(legs, cloud, settings, voip, moment, notices)
    return SearchOutcome(
        notices=notices,
        calls=calls,
        page=page,
        page_count=page_count,
        pages_capped=pages_capped,
        has_prev=page > 1,
        has_next=has_next,
    )
