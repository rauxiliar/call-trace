"""Cloud list and server-side secrets. DSNs and API passwords never reach the browser."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from app.tunnel import TunnelError, checked_ssh, checked_ssh_key

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GRAFANA = "https://o11y.broadvoice.io"
DEFAULT_LOKI = {
    "us": "f4a61dda-25fc-4558-a129-71ca84325cad",
    "eu": "f83d2b12-ce21-4c51-a27f-f60c1683fde4",
}
DEFAULT_VOIPMONITOR_URL = "https://voipmon.broadvoice.com"


class ConfigError(Exception):
    """Safe to show: the message does not include secrets."""


@dataclass(frozen=True)
class Cloud:
    id: str
    region: str
    namespace: str
    cluster: str
    dsn: str
    domains_dsn: str = ""
    timezone: str = "America/Chicago"
    ssh: str = ""
    ssh_key: str = ""
    ssh_password: str = ""


@dataclass(frozen=True)
class Settings:
    grafana_base_url: str
    loki_uids: dict[str, str]
    clouds: dict[str, Cloud]
    voipmonitor_url: str
    voipmonitor_user: str
    voipmonitor_password: str
    cf_access_client_id: str
    cf_access_client_secret: str


def empty_settings() -> Settings:
    return Settings(
        grafana_base_url=DEFAULT_GRAFANA,
        loki_uids=dict(DEFAULT_LOKI),
        clouds={},
        voipmonitor_url=DEFAULT_VOIPMONITOR_URL,
        voipmonitor_user="",
        voipmonitor_password="",
        cf_access_client_id="",
        cf_access_client_secret="",
    )


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def checked_cloud_timezone(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ConfigError("Cada cloud precisa de timezone.")
    try:
        ZoneInfo(text)
    except Exception:
        raise ConfigError("A timezone da cloud não é um nome IANA.") from None
    return text


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    config_path = path or Path(_env("CLOUDS_CONFIG") or (REPO_ROOT / "config" / "clouds.yaml"))
    if not config_path.is_file():
        raise ConfigError(
            "Cria config/clouds.yaml a partir de config/clouds.example.yaml."
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("config/clouds.yaml inválido.") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config/clouds.yaml inválido.")

    loki_uids = dict(DEFAULT_LOKI)
    loki = raw.get("loki") or {}
    if isinstance(loki, dict):
        for region in ("us", "eu"):
            uid = str(loki.get(region) or "").strip()
            if uid:
                loki_uids[region] = uid

    clouds: dict[str, Cloud] = {}
    entries = raw.get("clouds") or []
    if not isinstance(entries, list):
        raise ConfigError("config/clouds.yaml inválido.")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("config/clouds.yaml inválido.")
        cloud_id = str(entry.get("id") or "").strip()
        region = str(entry.get("region") or "").strip().lower()
        dsn = str(entry.get("dsn") or "").strip()
        domains_dsn = str(entry.get("domains_dsn") or "").strip()
        ssh = str(entry.get("ssh") or "").strip()
        ssh_key = str(entry.get("ssh_key") or "").strip()
        ssh_password = str(entry.get("ssh_password") or "")
        if not cloud_id or not dsn or region not in loki_uids:
            raise ConfigError("Cada cloud precisa de id, region (us ou eu) e dsn.")
        timezone = checked_cloud_timezone(entry.get("timezone"))
        if ssh:
            try:
                ssh = checked_ssh(ssh)
                if ssh_key:
                    ssh_key = checked_ssh_key(ssh_key, REPO_ROOT)
            except TunnelError as exc:
                raise ConfigError(str(exc)) from exc
        elif ssh_key or ssh_password:
            raise ConfigError("A chave ou a password SSH pedem um destino ssh.")
        clouds[cloud_id] = Cloud(
            id=cloud_id,
            region=region,
            namespace=str(entry.get("namespace") or "").strip(),
            cluster=str(entry.get("cluster") or "").strip(),
            dsn=dsn,
            domains_dsn=domains_dsn,
            timezone=timezone,
            ssh=ssh,
            ssh_key=ssh_key,
            ssh_password=ssh_password,
        )

    base = str(raw.get("grafana_base_url") or DEFAULT_GRAFANA).strip() or DEFAULT_GRAFANA
    return Settings(
        grafana_base_url=base,
        loki_uids=loki_uids,
        clouds=clouds,
        voipmonitor_url=(_env("VOIPMONITOR_URL") or DEFAULT_VOIPMONITOR_URL).rstrip("/"),
        voipmonitor_user=_env("VOIPMONITOR_USER"),
        voipmonitor_password=_env("VOIPMONITOR_PASSWORD"),
        cf_access_client_id=_env("CF_ACCESS_CLIENT_ID"),
        cf_access_client_secret=_env("CF_ACCESS_CLIENT_SECRET"),
    )
