from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DetailsRow:
    uuid: str
    sip_callid: str
    details: str
    apps: str = ""


@dataclass(frozen=True)
class Leg:
    uuid: str
    call_uuid: str
    sip_call_id: str
    direction: str
    caller_number: str
    destination: str
    hangup_cause: str
    start_stamp: datetime | None
    end_stamp: datetime | None
    billmsec: int | None
    hostname: str
    domain: str
    gateway: str = ""


@dataclass
class VoipInfo:
    caller: str = ""
    called: str = ""
    sip_response: str = ""
    duration: str = ""
    codec: str = ""
    mos: str = ""
    sensor: str = ""
    share_url: str = ""
    missing: bool = True
    error: str = ""
