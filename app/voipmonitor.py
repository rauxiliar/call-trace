"""VoIPmonitor GUI API. Same contract as ct-tools/.cursor/mcps/voipmonitor_server.py."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx

from app.config import Settings
from app.models import VoipInfo

VOIPMONITOR_ZONE = ZoneInfo("America/Los_Angeles")
ALLOWED_TASKS = frozenset({"getVoipCalls", "getShareURL"})
MAX_ROWS = 50
CALLER_KEYS = ("caller", "callername")
CALLED_KEYS = ("called",)
SIP_KEYS = ("lastSIPresponseNum", "lastSIPresponse")
DURATION_KEYS = ("duration", "connect_duration")
CODEC_KEYS = ("a_codec", "codec")
MOS_KEYS = ("a_mos_f1_mult10", "mos_min_mult10", "mos_min", "mos")
SENSOR_KEYS = ("id_sensor", "sensor")
ROW_KEYS = ("cdr", "results", "calls", "rows", "data")


def format_gui_time(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(VOIPMONITOR_ZONE).strftime("%Y-%m-%d %H:%M:%S")


def _present(row: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any] | None:
    for key in keys:
        if key not in row:
            continue
        value = row[key]
        if value is None or value == "":
            continue
        return key, value
    return None


def _text_value(value: Any) -> str:
    return str(value)


def display_voip(row: dict[str, Any] | None) -> VoipInfo:
    info = VoipInfo()
    if not row:
        return info
    caller = _present(row, CALLER_KEYS)
    called = _present(row, CALLED_KEYS)
    sip = _present(row, SIP_KEYS)
    duration = _present(row, DURATION_KEYS)
    codec = _present(row, CODEC_KEYS)
    mos = _present(row, MOS_KEYS)
    sensor = _present(row, SENSOR_KEYS)
    if caller:
        info.caller = _text_value(caller[1])
    if called:
        info.called = _text_value(called[1])
    if sip:
        info.sip_response = _text_value(sip[1])
    if duration:
        info.duration = _text_value(duration[1])
    if codec:
        info.codec = _text_value(codec[1])
    if mos:
        info.mos = _format_mos(mos[0], mos[1])
    if sensor:
        info.sensor = _text_value(sensor[1])
    return info


def _format_mos(key: str, value: Any) -> str:
    if "mult10" in key:
        try:
            return f"{float(value) / 10:.1f}"
        except (TypeError, ValueError):
            return _text_value(value)
    return _text_value(value)


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ROW_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if any(key in payload for key in ("caller", "called", "ID", "calldate")):
        return [payload]
    return []


def extract_url(payload: Any) -> str | None:
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith(("http://", "https://")):
            return text
        return None
    if isinstance(payload, dict):
        for key in ("url", "shareUrl", "share_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()
    return None


class VoipMonitor:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        cf_id: str,
        cf_secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = (url or "https://voipmon.broadvoice.com").rstrip("/")
        self._user = user.strip()
        self._password = password
        self._cf_id = cf_id.strip()
        self._cf_secret = cf_secret.strip()
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, client: httpx.Client | None = None) -> VoipMonitor:
        return cls(
            settings.voipmonitor_url,
            settings.voipmonitor_user,
            settings.voipmonitor_password,
            settings.cf_access_client_id,
            settings.cf_access_client_secret,
            client,
        )

    def get_voip_calls(self, call_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        payload = self._call(
            "getVoipCalls",
            {
                "startTime": format_gui_time(start),
                "startTimeTo": format_gui_time(end),
                "callId": call_id,
            },
        )
        if payload.get("error") and not extract_rows(payload):
            return {"rows": [], "error": self._redact(str(payload["error"]))}
        return {"rows": extract_rows(payload)[:MAX_ROWS], "error": None}

    def get_share_url(self, call_id: str) -> dict[str, Any]:
        payload = self._call("getShareURL", {"callId": call_id})
        url = extract_url(payload)
        if payload.get("error") and not url:
            return {"url": None, "error": self._redact(str(payload["error"]))}
        return {"url": url, "error": None}

    def _redact(self, text: str) -> str:
        for secret in (self._password, self._cf_secret, self._cf_id):
            if secret:
                text = text.replace(secret, "***")
        return text[:500]

    def _call(self, task: str, params: dict[str, Any]) -> dict[str, Any]:
        if task not in ALLOWED_TASKS:
            return {"error": f"Task {task} is not allowed."}
        if not self._user or not self._password:
            return {
                "error": (
                    "VOIPMONITOR_USER e VOIPMONITOR_PASSWORD têm de estar definidos no servidor."
                )
            }

        headers = {"Accept": "application/json"}
        if self._cf_id and self._cf_secret:
            headers["CF-Access-Client-Id"] = self._cf_id
            headers["CF-Access-Client-Secret"] = self._cf_secret

        body = {
            "task": task,
            "user": self._user,
            "password": self._password,
            "params": params,
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    f"{self._url}/php/api.php",
                    json=body,
                    headers=headers,
                    timeout=30.0,
                )
            else:
                response = httpx.post(
                    f"{self._url}/php/api.php",
                    json=body,
                    headers=headers,
                    timeout=30.0,
                )
        except httpx.HTTPError as exc:
            return {"error": f"Pedido ao VoIPmonitor falhou: {exc.__class__.__name__}."}

        content_type = response.headers.get("content-type", "")
        text = response.text
        if _looks_like_html(text, content_type):
            return {
                "error": (
                    "O Cloudflare Access bloqueou o pedido. "
                    "Confirma CF_ACCESS_CLIENT_ID e CF_ACCESS_CLIENT_SECRET no servidor."
                )
            }
        if response.status_code >= 400:
            return {
                "error": f"A API VoIPmonitor devolveu HTTP {response.status_code}.",
                "body": self._redact(text),
            }
        try:
            payload = response.json()
        except ValueError:
            stripped = text.strip()
            if stripped.startswith(("http://", "https://")):
                return {"url": stripped}
            return {"error": "A API VoIPmonitor não devolveu JSON.", "body": self._redact(stripped)}
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            return {"results": payload}
        return {"results": payload}


def _looks_like_html(text: str, content_type: str) -> bool:
    head = text.lstrip()[:200].lower()
    return (
        "text/html" in content_type
        or head.startswith("<!doctype")
        or head.startswith("<html")
    )
