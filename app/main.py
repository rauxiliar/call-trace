"""Read-only page that correlates one cloud's FreeSWITCH CDR with VoIPmonitor and Grafana."""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request
from markupsafe import Markup
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_internal_user
from app.i18n import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    LOCALES,
    _,
    get_locale,
    html_lang,
    js_messages,
    reset_locale,
    resolve_locale,
    set_locale,
)
from app.cdr import (
    DIRECTIONS,
    HANGUP_CAUSES,
    SearchInputError,
    SearchOutcome,
    browse,
    call_variables,
    checked_choice,
    checked_page,
    checked_search_id,
    correlate,
    dialplan_steps,
    listed_call_ids,
    optional_domain,
    related,
    parse_stamp,
)
from app.config import ConfigError, Settings, empty_settings, load_settings
from app.db import CdrReadError, InvalidDomain, LegFilters, PostgresCdrRepository
from app.grafana import as_utc
from app.tunnel import TunnelError, connect_domains_dsn, connect_dsn
from app.voipmonitor import VoipMonitor
from app.zones import utc_offset

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def format_stamp(value: datetime | None, zone_name: str = "UTC") -> str:
    if not isinstance(value, datetime):
        return "—"
    try:
        zone = ZoneInfo(zone_name or "UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    local = as_utc(value).astimezone(zone)
    name = getattr(zone, "key", None) or zone_name or "UTC"
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {utc_offset(name, local)}"


def dash(value: object) -> object:
    if value is None or value == "":
        return "—"
    return value


TEMPLATES.env.filters["dash"] = dash
TEMPLATES.env.globals["format_stamp"] = format_stamp
TEMPLATES.env.globals["_"] = _
TEMPLATES.env.globals["get_locale"] = get_locale
TEMPLATES.env.globals["html_lang"] = html_lang

_INPUT_ERRORS = {
    "invalid id": "O identificador é inválido.",
    "domain": "Escolhe um domínio.",
    "window": "Escolhe um intervalo até 24 horas.",
    "order": "O fim é anterior ao início.",
    "span": "O intervalo não pode passar de 24 horas.",
    "filter": "Cada filtro pode ter no máximo 64 caracteres.",
    "stamp": "A data é inválida.",
    "choice": "A direção, o hangup cause, o servidor ou o gateway não está na lista.",
    "page": "A página é inválida.",
    "page_cap": "Afina o intervalo ou os filtros.",
}


def _page_href(params: dict[str, str], page: int) -> str:
    query = {key: value for key, value in params.items() if str(value).strip()}
    query["page"] = str(page)
    return "/?" + urlencode(query)


def _page_links(params: dict[str, str], page: int, page_count: int) -> list[dict]:
    if page_count <= 1:
        return []
    wanted = {1, page_count}
    for number in range(page - 2, page + 3):
        if 1 <= number <= page_count:
            wanted.add(number)
    links: list[dict] = []
    previous = 0
    for number in sorted(wanted):
        if previous and number > previous + 1:
            links.append({"gap": True})
        links.append({
            "n": number,
            "href": _page_href(params, number),
            "current": number == page,
            "gap": False,
        })
        previous = number
    return links


def _minute_field(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%dT%H:%M")


def _input_message(exc: SearchInputError) -> str:
    code = exc.args[0] if exc.args else ""
    return _(_INPUT_ERRORS.get(str(code), "O pedido é inválido."))


def _safe_domains(repo) -> tuple[list[str], str]:
    try:
        return list(repo.list_domains()), ""
    except CdrReadError as exc:
        return [], _(str(exc))
    except Exception as exc:
        cause = exc.__cause__ or exc
        log.warning("domains failed: %s", cause.__class__.__name__)
        return [], _("Não foi possível ler a CDR deste cloud.")


def _safe_servers(repo) -> tuple[list[str], str]:
    try:
        return list(repo.list_servers()), ""
    except CdrReadError as exc:
        return [], _(str(exc))
    except Exception as exc:
        cause = exc.__cause__ or exc
        log.warning("servers failed: %s", cause.__class__.__name__)
        return [], _("Não foi possível ler os servidores deste cloud.")


def _safe_gateways(repo, domain: str) -> tuple[list[str], str]:
    try:
        return list(repo.list_gateways(domain)), ""
    except CdrReadError as exc:
        return [], _(str(exc))
    except Exception as exc:
        cause = exc.__cause__ or exc
        log.warning("gateways failed: %s", cause.__class__.__name__)
        return [], _("Não foi possível ler os gateways deste cloud.")


def _lang_redirect_target(request: Request) -> str:
    referer = request.headers.get("referer") or "/"
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return "/"
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return target


def _render(request: Request, context: dict, template: str = "index.html") -> HTMLResponse:
    context = {
        **context,
        "request": request,
        "locale": get_locale(),
        "i18n_js_json": Markup(json.dumps(js_messages(), ensure_ascii=False)),
    }
    names = [name for name in inspect.signature(TEMPLATES.TemplateResponse).parameters if name != "self"]
    if names and names[0] == "request":
        return TEMPLATES.TemplateResponse(request, template, context)
    return TEMPLATES.TemplateResponse(template, context)


def create_app(
    settings: Settings | None = None,
    repo_for=None,
    voip=None,
) -> FastAPI:
    config_error = None
    if settings is None:
        try:
            settings = load_settings()
        except ConfigError as exc:
            config_error = str(exc)
            settings = empty_settings()

    if repo_for is None:
        def repo_for(cloud):
            try:
                dsn = connect_dsn(cloud)
                domains_dsn = connect_domains_dsn(cloud)
            except TunnelError as exc:
                raise CdrReadError(_(str(exc))) from exc
            return PostgresCdrRepository(dsn, domains_dsn, cloud.timezone)

    if voip is None:
        voip = VoipMonitor.from_settings(settings)

    app = FastAPI(title="CDR Correlation")
    app.state.settings = settings
    app.state.config_error = config_error
    app.state.repo_for = repo_for
    app.state.voip = voip

    @app.middleware("http")
    async def locale_middleware(request: Request, call_next):
        token = set_locale(resolve_locale(request))
        try:
            return await call_next(request)
        finally:
            reset_locale(token)

    @app.get("/lang")
    def set_lang(request: Request, set: str = "") -> RedirectResponse:
        locale = set if set in LOCALES else "pt-PT"
        response = RedirectResponse(_lang_redirect_target(request), status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            locale,
            max_age=COOKIE_MAX_AGE,
            path="/",
            samesite="lax",
        )
        return response

    @app.get("/domains")
    def domains(
        cloud: str = "",
        _user: None = Depends(require_internal_user),
    ) -> dict:
        selected = cloud.strip()
        if app.state.config_error or selected not in app.state.settings.clouds:
            return {"domains": []}
        try:
            repo = app.state.repo_for(app.state.settings.clouds[selected])
        except CdrReadError as exc:
            return {"domains": [], "error": _(str(exc))}
        except Exception as exc:
            cause = exc.__cause__ or exc
            log.warning("domains failed: %s", cause.__class__.__name__)
            return {"domains": [], "error": _("Não foi possível ler a CDR deste cloud.")}
        names, error = _safe_domains(repo)
        payload: dict = {"domains": names}
        if error:
            payload["error"] = error
        return payload

    @app.get("/servers")
    def servers(
        cloud: str = "",
        _user: None = Depends(require_internal_user),
    ) -> dict:
        selected = cloud.strip()
        if app.state.config_error or selected not in app.state.settings.clouds:
            return {"servers": []}
        try:
            repo = app.state.repo_for(app.state.settings.clouds[selected])
        except CdrReadError as exc:
            return {"servers": [], "error": _(str(exc))}
        except Exception as exc:
            cause = exc.__cause__ or exc
            log.warning("servers failed: %s", cause.__class__.__name__)
            return {"servers": [], "error": _("Não foi possível ler os servidores deste cloud.")}
        names, error = _safe_servers(repo)
        payload: dict = {"servers": names}
        if error:
            payload["error"] = error
        return payload

    @app.get("/gateways")
    def gateways(
        cloud: str = "",
        domain: str = "",
        _user: None = Depends(require_internal_user),
    ) -> dict:
        selected = cloud.strip()
        if app.state.config_error or selected not in app.state.settings.clouds:
            return {"gateways": []}
        try:
            chosen = optional_domain(domain)
        except InvalidDomain:
            return {"gateways": []}
        if not chosen:
            return {"gateways": []}
        try:
            repo = app.state.repo_for(app.state.settings.clouds[selected])
        except CdrReadError as exc:
            return {"gateways": [], "error": _(str(exc))}
        except Exception as exc:
            cause = exc.__cause__ or exc
            log.warning("gateways failed: %s", cause.__class__.__name__)
            return {"gateways": [], "error": _("Não foi possível ler os gateways deste cloud.")}
        names, error = _safe_gateways(repo, chosen)
        payload: dict = {"gateways": names}
        if error:
            payload["error"] = error
        return payload

    @app.get("/related", response_class=HTMLResponse)
    def related_calls(
        request: Request,
        cloud: str = "",
        domain: str = "",
        call_uuid: str = "",
        listed: list[str] = Query(default=[]),
        _user: None = Depends(require_internal_user),
    ) -> HTMLResponse:
        selected = cloud.strip()
        message = ""
        outcome = SearchOutcome()
        zone = ""
        try:
            if app.state.config_error:
                message = _(app.state.config_error)
            elif selected not in app.state.settings.clouds:
                message = _("Cloud desconhecido.") if selected else _("Escolhe um cloud.")
            else:
                zone = app.state.settings.clouds[selected].timezone
                repo = app.state.repo_for(app.state.settings.clouds[selected])
                outcome = related(
                    cloud=app.state.settings.clouds[selected],
                    settings=app.state.settings,
                    domain=domain,
                    call_uuid=call_uuid,
                    repo=repo,
                    voip=app.state.voip,
                    listed=listed_call_ids(listed),
                )
        except InvalidDomain:
            message = _("O domínio só pode ter letras, dígitos, ponto, _ e -.")
        except SearchInputError as exc:
            message = _input_message(exc)
        except CdrReadError as exc:
            message = _(str(exc))
        except Exception as exc:
            cause = exc.__cause__ or exc
            log.warning("related failed: %s", cause.__class__.__name__)
            message = _("Não foi possível ler a CDR deste cloud.")
        if message:
            outcome = SearchOutcome(notices=[message])
        return _render(request, {"outcome": outcome, "tz": zone}, "related.html")

    @app.get("/cdr-detail", response_class=HTMLResponse)
    def cdr_detail(
        request: Request,
        cloud: str = "",
        uuid: str = "",
        _user: None = Depends(require_internal_user),
    ) -> HTMLResponse:
        selected = cloud.strip()
        message = ""
        variables: list[tuple[str, str]] = []
        steps: list[dict[str, str]] = []
        found = False
        try:
            leg_uuid = checked_search_id(uuid)
            if app.state.config_error:
                message = _(app.state.config_error)
            elif selected not in app.state.settings.clouds:
                message = _("Cloud desconhecido.") if selected else _("Escolhe um cloud.")
            else:
                repo = app.state.repo_for(app.state.settings.clouds[selected])
                row = repo.fetch_leg_detail(leg_uuid)
                if row is None:
                    message = _("Não há detalhe desta perna.")
                else:
                    found = True
                    variables = call_variables(row.details)
                    steps = dialplan_steps(row.apps, row.details)
        except SearchInputError as exc:
            message = _input_message(exc)
        except CdrReadError as exc:
            message = _(str(exc))
        except Exception as exc:
            cause = exc.__cause__ or exc
            log.warning("cdr detail failed: %s", cause.__class__.__name__)
            message = _("Não foi possível ler a CDR deste cloud.")
        return _render(
            request,
            {
                "found": found,
                "message": message,
                "variables": variables,
                "dialplans": steps,
            },
            "detail.html",
        )

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        cloud: str = "",
        domain: str = "",
        q: str = "",
        start: str = "",
        end: str = "",
        caller: str = "",
        destination: str = "",
        direction: str = "",
        hangup: str = "",
        server: str = "",
        gateway: str = "",
        page: str = "",
        _user: None = Depends(require_internal_user),
    ) -> HTMLResponse:
        # Login hook: app/auth.py. This repo has no internal login yet.
        form_error = ""
        domain_error = ""
        domains: list[str] = []
        servers: list[str] = []
        server_error = ""
        gateways: list[str] = []
        gateway_error = ""
        outcome: SearchOutcome | None = None
        selected = cloud.strip()
        zone = ""
        if selected in app.state.settings.clouds:
            zone = app.state.settings.clouds[selected].timezone
        searching = any(
            value.strip()
            for value in (q, domain, start, end, caller, destination, direction, hangup, server, gateway)
        )
        if form_error or app.state.config_error:
            form_error = "" if app.state.config_error else form_error
        elif searching and not selected:
            form_error = _("Escolhe um cloud.")
        elif searching and selected not in app.state.settings.clouds:
            form_error = _("Cloud desconhecido.")
        elif selected in app.state.settings.clouds:
            try:
                if searching and not domain.strip():
                    raise SearchInputError("domain")
                optional_domain(domain)
                checked_choice(direction, DIRECTIONS)
                checked_choice(hangup, HANGUP_CAUSES)
                page_number = checked_page(page) if searching and not q.strip() else 1
                filters = LegFilters(
                    start=parse_stamp(start, zone or "UTC"),
                    end=parse_stamp(end, zone or "UTC"),
                    caller_number=caller,
                    destination=destination,
                    direction=direction,
                    hangup_cause=hangup,
                    hostname=server,
                    gateway=gateway,
                )
            except InvalidDomain:
                form_error = _("O domínio só pode ter letras, dígitos, ponto, _ e -.")
            except SearchInputError as exc:
                form_error = _input_message(exc)
            else:
                chosen = app.state.settings.clouds[selected]
                try:
                    repo = app.state.repo_for(chosen)
                except CdrReadError as exc:
                    form_error = _(str(exc))
                    repo = None
                except Exception as exc:
                    cause = exc.__cause__ or exc
                    log.warning("search failed: %s", cause.__class__.__name__)
                    form_error = _("Não foi possível ler a CDR deste cloud.")
                    repo = None
                if repo is not None:
                    domains, domain_error = _safe_domains(repo)
                    servers, server_error = _safe_servers(repo)
                    if domain.strip():
                        gateways, gateway_error = _safe_gateways(repo, domain.strip())
                if (
                    repo is not None
                    and searching
                    and server.strip()
                    and (server_error or server.strip() not in servers)
                ):
                    form_error = server_error or _input_message(SearchInputError("choice"))
                if (
                    repo is not None
                    and searching
                    and not form_error
                    and gateway.strip()
                    and (gateway_error or gateway.strip() not in gateways)
                ):
                    form_error = gateway_error or _input_message(SearchInputError("choice"))
                if repo is not None and searching and not form_error:
                    try:
                        if q.strip():
                            outcome = correlate(
                                cloud=chosen,
                                settings=app.state.settings,
                                domain=domain,
                                search_id=q,
                                repo=repo,
                                voip=app.state.voip,
                                filters=filters,
                            )
                        else:
                            outcome = browse(
                                cloud=chosen,
                                settings=app.state.settings,
                                domain=domain,
                                filters=filters,
                                repo=repo,
                                voip=app.state.voip,
                                page=page_number,
                            )
                    except InvalidDomain:
                        form_error = _("O domínio só pode ter letras, dígitos, ponto, _ e -.")
                    except SearchInputError as exc:
                        form_error = _input_message(exc)
                    except CdrReadError as exc:
                        form_error = _(str(exc))
                    except Exception as exc:
                        cause = exc.__cause__ or exc
                        log.warning("search failed: %s", cause.__class__.__name__)
                        form_error = _("Não foi possível ler a CDR deste cloud.")

        kept = {
            "cloud": selected,
            "domain": domain,
            "start": _minute_field(start),
            "end": _minute_field(end),
            "caller": caller,
            "destination": destination,
            "direction": direction,
            "hangup": hangup,
            "server": server,
            "gateway": gateway,
        }
        page_first = ""
        page_prev = ""
        page_next = ""
        page_last = ""
        page_links: list[dict] = []
        if outcome and outcome.page_count > 1:
            if outcome.page > 1:
                page_first = _page_href(kept, 1)
                page_prev = _page_href(kept, outcome.page - 1)
            if outcome.has_next:
                page_next = _page_href(kept, outcome.page + 1)
                page_last = _page_href(kept, outcome.page_count)
            page_links = _page_links(kept, outcome.page, outcome.page_count)

        return _render(
            request,
            {
                "clouds": sorted(app.state.settings.clouds),
                "selected_cloud": selected,
                "domains": domains,
                "domain": domain,
                "q": q,
                "start": _minute_field(start),
                "end": _minute_field(end),
                "caller": caller,
                "destination": destination,
                "direction": direction,
                "hangup": hangup,
                "server": server,
                "servers": servers,
                "server_error": server_error,
                "gateway": gateway,
                "gateways": gateways,
                "gateway_error": gateway_error,
                "tz": zone,
                "zone_label": (
                    f"{zone} ({utc_offset(zone)})"
                    if zone
                    else _("Escolhe um cloud para ver a timezone.")
                ),
                "cloud_zones_json": Markup(
                    json.dumps(
                        {
                            cloud_id: f"{item.timezone} ({utc_offset(item.timezone)})"
                            for cloud_id, item in app.state.settings.clouds.items()
                        }
                    )
                ),
                "directions": DIRECTIONS,
                "hangup_causes": HANGUP_CAUSES,
                "kept": kept,
                "page_links": page_links,
                "page_first": page_first,
                "page_prev": page_prev,
                "page_next": page_next,
                "page_last": page_last,
                "config_error": _(app.state.config_error) if app.state.config_error else None,
                "form_error": form_error,
                "domain_error": domain_error,
                "outcome": outcome,
            },
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
