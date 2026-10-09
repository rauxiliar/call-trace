from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config import Cloud, Settings
from app.db import LegFilters
from app.grafana import as_utc
from app.models import DetailsRow, Leg

CALL = "751c7948-f598-47aa-a9ae-821848176a11"
LEG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LEG_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DOMAIN = "tenant.example"
SIP_A = "sip-a-id"
SIP_B = "sip-b-id"
SIP_C = "sip-c-id"
US_UID = "f4a61dda-25fc-4558-a129-71ca84325cad"
EU_UID = "f83d2b12-ce21-4c51-a27f-f60c1683fde4"
PASSWORD = "super-secret-vm-password"
DSN = "postgres://calltrace_ro:dsn-secret-value@10.9.9.9:5432/cdrs"
CF_ID = "cf-client-id-secret"
CF_SECRET = "cf-client-secret-value"
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 1, 12, 10, tzinfo=timezone.utc)
T0B = datetime(2026, 7, 1, 12, 1, tzinfo=timezone.utc)
T1B = datetime(2026, 7, 1, 12, 8, tzinfo=timezone.utc)
T0C = datetime(2026, 7, 1, 12, 2, tzinfo=timezone.utc)
T1C = datetime(2026, 7, 1, 12, 6, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def cloud_us() -> Cloud:
    return Cloud(
        id="example-us",
        region="us",
        namespace="us-gocontact-mt",
        cluster="o11y-us-gocontact-eks",
        dsn=DSN,
    )


def cloud_eu_vm() -> Cloud:
    return Cloud(
        id="example-eu-vm",
        region="eu",
        namespace="",
        cluster="o11y-eu-example",
        dsn=DSN,
    )


def settings_for(*clouds: Cloud) -> Settings:
    return Settings(
        grafana_base_url="https://o11y.broadvoice.io",
        loki_uids={"us": US_UID, "eu": EU_UID},
        clouds={cloud.id: cloud for cloud in clouds},
        voipmonitor_url="https://voipmon.broadvoice.com",
        voipmonitor_user="vm-user",
        voipmonitor_password=PASSWORD,
        cf_access_client_id=CF_ID,
        cf_access_client_secret=CF_SECRET,
    )


def make_leg(
    uuid: str,
    sip_call_id: str,
    start: datetime,
    end: datetime,
    domain: str = DOMAIN,
    call_uuid: str = CALL,
    billmsec: int | None = 42000,
    hostname: str = "fs-1",
    gateway: str = "",
) -> Leg:
    return Leg(
        uuid=uuid,
        call_uuid=call_uuid,
        sip_call_id=sip_call_id,
        direction="inbound" if uuid == call_uuid else "outbound",
        caller_number="351210000000",
        destination="351910000000",
        hangup_cause="NORMAL_CLEARING",
        start_stamp=start,
        end_stamp=end,
        billmsec=billmsec,
        hostname=hostname,
        domain=domain,
        gateway=gateway,
    )


def sample_legs(domain: str = DOMAIN) -> list[Leg]:
    return [
        make_leg(CALL, SIP_A, T0, T1, domain=domain, billmsec=0),
        make_leg(LEG_B, SIP_B, T0B, T1B, domain=domain),
        make_leg(LEG_C, SIP_C, T0C, T1C, domain=domain),
    ]


def sample_details(domain: str = DOMAIN) -> list[DetailsRow]:
    return [
        DetailsRow(
            uuid=CALL,
            sip_callid=SIP_A,
            details=json.dumps(
                {"variable_domain_name": domain, "Channel-Call-UUID": CALL}
            ),
        ),
        DetailsRow(
            uuid=LEG_B,
            sip_callid=SIP_B,
            details=json.dumps({"variable_domain": domain, "Channel-Call-UUID": CALL}),
        ),
        DetailsRow(
            uuid=LEG_C,
            sip_callid=SIP_C,
            details=json.dumps(
                {"variable_domain_name": domain, "Channel-Call-UUID": CALL}
            ),
        ),
    ]


def leg_matches(leg: Leg, filters: LegFilters | None) -> bool:
    if filters is None or not filters.any():
        return True
    if filters.start or filters.end:
        if leg.start_stamp is None:
            return False
        stamp = as_utc(leg.start_stamp)
        if filters.start and stamp < as_utc(filters.start):
            return False
        if filters.end and stamp > as_utc(filters.end):
            return False
    checks = (
        (filters.caller_number, leg.caller_number),
        (filters.destination, leg.destination),
        (filters.direction, leg.direction),
        (filters.hangup_cause, leg.hangup_cause),
        (filters.hostname, leg.hostname),
        (filters.gateway, leg.gateway),
    )
    for wanted, actual in checks:
        if wanted.strip() and actual != wanted.strip():
            return False
    return True


class MemoryRepo:
    def __init__(self, details: list[DetailsRow], legs: list[Leg], domains: list[str] | None = None) -> None:
        self.details = details
        self.legs = legs
        self.domains = list(domains or [])
        self.servers = ["fs-1"]
        self.gateways = ["Trunk-dabbf5"]
        self.calls: list[tuple] = []

    def list_domains(self) -> list[str]:
        self.calls.append(("domains",))
        return list(self.domains)

    def list_servers(self) -> list[str]:
        self.calls.append(("servers",))
        return list(self.servers)

    def list_gateways(self, domain: str) -> list[str]:
        self.calls.append(("gateways", domain))
        return list(self.gateways)

    def fetch_events(self, search_id: str) -> list[DetailsRow]:
        self.calls.append(("events", search_id))
        return [
            row
            for row in self.details
            if row.uuid == search_id or row.sip_callid == search_id
        ]

    def fetch_details(self, search_id: str) -> list[DetailsRow]:
        self.calls.append(("details", search_id))
        return [
            row
            for row in self.details
            if row.uuid == search_id or row.sip_callid == search_id
        ]

    def fetch_leg_detail(self, uuid: str) -> DetailsRow | None:
        self.calls.append(("leg_detail", uuid))
        for row in self.details:
            if row.uuid == uuid:
                return row
        return None

    def fetch_legs_by_call_uuid(self, domain: str, call_uuid: str, filters: LegFilters | None = None) -> list[Leg]:
        self.calls.append(("call_uuid", domain, call_uuid))
        return [
            leg
            for leg in self.legs
            if leg.domain == domain and leg.call_uuid == call_uuid and leg_matches(leg, filters)
        ]

    def fetch_legs_by_any_id(self, domain: str, search_id: str, filters: LegFilters | None = None) -> list[Leg]:
        self.calls.append(("any", domain, search_id))
        return [
            leg
            for leg in self.legs
            if leg.domain == domain
            and search_id in (leg.uuid, leg.call_uuid, leg.sip_call_id)
            and leg_matches(leg, filters)
        ]

    def fetch_legs_matching(
        self,
        domain: str,
        filters: LegFilters,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[Leg], bool]:
        self.calls.append(("match", domain, limit, offset))
        rows = [leg for leg in self.legs if leg.domain == domain and leg_matches(leg, filters)]
        rows.sort(
            key=lambda leg: as_utc(leg.start_stamp) if leg.start_stamp else datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        page = rows[offset:offset + limit]
        return page, len(rows) > offset + limit

    def count_legs(self, domain: str, filters: LegFilters, cap: int | None = None) -> int:
        self.calls.append(("count", domain, cap))
        total = sum(1 for leg in self.legs if leg.domain == domain and leg_matches(leg, filters))
        if cap is not None and total > cap:
            return cap
        return total


class FakeVoip:
    def __init__(self, empty: set[str] | None = None) -> None:
        self.empty = empty or set()
        self.calls: list[tuple] = []
        self.shares: list[str] = []

    def get_voip_calls(self, call_id: str, start: datetime, end: datetime) -> dict:
        self.calls.append((call_id, start, end))
        if call_id in self.empty:
            return {"rows": [], "error": None}
        return {
            "rows": [
                {
                    "caller": "100",
                    "called": "200",
                    "lastSIPresponseNum": 200,
                    "duration": 12,
                    "a_codec": "PCMU",
                    "mos_min_mult10": 43,
                    "id_sensor": 2,
                }
            ],
            "error": None,
        }

    def get_share_url(self, call_id: str) -> dict:
        self.shares.append(call_id)
        return {"url": f"https://voipmon.broadvoice.com/admin.php?share={call_id}"}
