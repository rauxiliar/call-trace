"""Grafana Explore links. The URL carries the LogQL and the time range, never a token."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from app.config import Settings

LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def escape_line_filter(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "")
        .replace("\r", "")
    )


def _label(name: str, value: str) -> str | None:
    if value and LABEL_RE.fullmatch(value):
        return f'{name}="{value}"'
    return None


def logql(namespace: str, identifier: str, cloud: str = "") -> list[str]:
    """One LogQL expression per label. namespace and cloud are not combined."""
    if not identifier:
        return []
    line = f'|= "{escape_line_filter(identifier)}"'
    expressions: list[str] = []
    namespace_label = _label("namespace", namespace)
    if namespace_label:
        expressions.append(f"{{{namespace_label}}} {line}")
    cloud_label = _label("cloud", cloud)
    if cloud_label:
        expressions.append(f"{{{cloud_label}}} {line}")
    return expressions


def loki_uid(settings: Settings, region: str) -> str | None:
    uid = settings.loki_uids.get(region, "").strip()
    return uid or None


def explore_url(
    base: str,
    datasource_uid: str,
    exprs: str | list[str],
    start: datetime,
    end: datetime,
    timezone_name: str = "UTC",
) -> str:
    expressions = [exprs] if isinstance(exprs, str) else list(exprs)
    start_ms = str(int(as_utc(start).timestamp() * 1000))
    end_ms = str(int(as_utc(end).timestamp() * 1000))
    queries = [
        {
            "refId": chr(ord("A") + index),
            "expr": expr,
            "queryType": "range",
            "datasource": {"type": "loki", "uid": datasource_uid},
            "editorMode": "code",
        }
        for index, expr in enumerate(expressions)
    ]
    panes = {
        "logs": {
            "datasource": datasource_uid,
            "queries": queries,
            "range": {"from": start_ms, "to": end_ms},
        }
    }
    query = urlencode(
        {
            "schemaVersion": "1",
            "panes": json.dumps(panes, separators=(",", ":")),
            "orgId": "1",
            "timezone": timezone_name or "UTC",
        }
    )
    return f"{base.rstrip('/')}/explore?{query}"
