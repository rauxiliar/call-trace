import json
from datetime import datetime

import httpx

from app.voipmonitor import VoipMonitor, display_voip
from tests.support import CF_ID, CF_SECRET, PASSWORD


def _client(handler) -> VoipMonitor:
    return VoipMonitor(
        "https://voipmon.broadvoice.com",
        "vm-user",
        PASSWORD,
        CF_ID,
        CF_SECRET,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_get_voip_calls_posts_the_gui_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["cf_id"] = request.headers.get("CF-Access-Client-Id")
        seen["cf_secret"] = request.headers.get("CF-Access-Client-Secret")
        return httpx.Response(
            200,
            json=[
                {
                    "caller": "100",
                    "called": "200",
                    "lastSIPresponseNum": 200,
                    "duration": 3,
                    "a_codec": "PCMA",
                    "a_mos_f1_mult10": 41,
                    "id_sensor": 7,
                }
            ],
        )

    result = _client(handler).get_voip_calls(
        "sip-a-id",
        datetime(2026, 7, 1, 11, 55),
        datetime(2026, 7, 1, 12, 15),
    )
    assert seen["url"] == "https://voipmon.broadvoice.com/php/api.php"
    assert seen["body"]["task"] == "getVoipCalls"
    assert seen["body"]["user"] == "vm-user"
    assert seen["body"]["password"] == PASSWORD
    assert seen["body"]["params"]["callId"] == "sip-a-id"
    assert seen["body"]["params"]["startTime"] == "2026-07-01 04:55:00"
    assert seen["body"]["params"]["startTimeTo"] == "2026-07-01 05:15:00"
    assert seen["cf_id"] == CF_ID
    assert seen["cf_secret"] == CF_SECRET
    assert result["rows"][0]["caller"] == "100"
    assert "error" not in result or result["error"] is None


def test_get_share_url_uses_the_sip_call_id():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["task"] == "getShareURL"
        assert body["params"] == {"callId": "sip-b-id"}
        return httpx.Response(200, text="https://voipmon.broadvoice.com/share/sip-b-id")

    result = _client(handler).get_share_url("sip-b-id")
    assert result["url"] == "https://voipmon.broadvoice.com/share/sip-b-id"
    assert result["error"] is None


def test_errors_drop_the_password_and_html_body():
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": f"login failed {PASSWORD}"})

    result = _client(denied).get_voip_calls(
        "x",
        datetime(2026, 7, 1, 0, 0),
        datetime(2026, 7, 1, 1, 0),
    )
    assert PASSWORD not in result["error"]
    assert "***" in result["error"]
    assert "body" not in result

    def html_block(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=f"<!doctype html><html>{CF_SECRET}{PASSWORD}</html>",
        )

    blocked = _client(html_block).get_share_url("x")
    assert CF_SECRET not in blocked["error"]
    assert PASSWORD not in blocked["error"]
    assert "Cloudflare Access" in blocked["error"]


def test_display_only_includes_fields_the_api_sent():
    info = display_voip({"caller": "100", "mos_min_mult10": 43})
    assert info.caller == "100"
    assert info.called == ""
    assert info.mos == "4.3"
    assert info.codec == ""


def test_gui_time_uses_los_angeles_including_winter():
    from app.voipmonitor import format_gui_time

    summer = format_gui_time(datetime(2026, 7, 1, 10, 55, 59))
    winter = format_gui_time(datetime(2026, 1, 15, 19, 0))
    assert summer == "2026-07-01 03:55:59"
    assert winter == "2026-01-15 11:00:00"
