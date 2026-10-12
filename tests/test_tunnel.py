import socket
import threading
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, Cloud, ConfigError, load_settings
from app.main import create_app
from app.tunnel import TUNNEL_FAILED, TunnelBook, TunnelError, connect_domains_dsn, connect_dsn
from tests.support import CALL, DSN, FakeVoip, MemoryRepo, cloud_us, settings_for


class ListeningProc:
    def __init__(self, argv, **kwargs) -> None:
        self.argv = argv
        self.returncode = None
        spec = argv[argv.index("-L") + 1]
        local_port = int(spec.split(":")[1])
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", local_port))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        self._closed = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                continue
            conn.close()

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.kill()

    def kill(self) -> None:
        self.returncode = -9
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def wait(self, timeout=None):
        self.kill()
        return self.returncode


def _cloud_with_ssh() -> Cloud:
    base = cloud_us()
    return Cloud(
        id=base.id,
        region=base.region,
        namespace=base.namespace,
        cluster=base.cluster,
        dsn=base.dsn,
        ssh="user@bastion.example",
    )


def test_direct_dsn_does_not_spawn_ssh():
    spawned = []

    def popen(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("ssh")

    assert connect_dsn(cloud_us(), TunnelBook(popen=popen)) == DSN
    assert spawned == []


def test_ssh_forwards_the_dsn_host_to_localhost():
    spawned = []

    def popen(argv, **kwargs):
        spawned.append(argv)
        return ListeningProc(argv)

    book = TunnelBook(popen=popen)
    local = connect_dsn(_cloud_with_ssh(), book)
    again = connect_dsn(_cloud_with_ssh(), book)
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[0] == "ssh"
    assert argv[1] == "-N"
    assert "ExitOnForwardFailure=yes" in argv
    assert "BatchMode=yes" in argv
    assert "ServerAliveInterval=30" in argv
    assert argv[-1] == "user@bastion.example"
    forward = argv[argv.index("-L") + 1]
    assert forward.startswith("127.0.0.1:")
    assert forward.endswith(":10.9.9.9:5432")
    parsed = urlsplit(local)
    assert parsed.hostname == "127.0.0.1"
    assert "dsn-secret-value" in local
    assert parsed.hostname != "10.9.9.9"
    assert again == local


def test_domains_database_reuses_the_tunnel():
    spawned = []

    def popen(argv, **kwargs):
        spawned.append(argv)
        return ListeningProc(argv)

    cloud = Cloud(
        id="example-us",
        region="us",
        namespace="ns",
        cluster="cl",
        dsn="postgres://calltrace_ro:dsn-secret-value@10.9.9.9:5432/cdrs",
        domains_dsn="postgres://calltrace_ro:dsn-secret-value@10.9.9.9:5432/fusionpbx",
        ssh="user@bastion.example",
    )
    book = TunnelBook(popen=popen)
    cdr = connect_dsn(cloud, book)
    domains = connect_domains_dsn(cloud, book)
    assert len(spawned) == 1
    assert urlsplit(cdr).path == "/cdrs"
    assert urlsplit(domains).path == "/fusionpbx"
    assert urlsplit(cdr).port == urlsplit(domains).port
    assert urlsplit(cdr).hostname == "127.0.0.1"


def test_a_dead_tunnel_is_opened_again():
    procs = []

    def popen(argv, **kwargs):
        proc = ListeningProc(argv)
        procs.append(proc)
        return proc

    book = TunnelBook(popen=popen)
    first = connect_dsn(_cloud_with_ssh(), book)
    procs[0].returncode = 1
    second = connect_dsn(_cloud_with_ssh(), book)
    assert len(procs) == 2
    assert urlsplit(first).hostname == "127.0.0.1"
    assert urlsplit(second).hostname == "127.0.0.1"
    assert first != second


def test_rejected_destination_never_spawns():
    spawned = []

    def popen(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("ssh")

    book = TunnelBook(popen=popen)
    cloud = Cloud(
        id="example-us",
        region="us",
        namespace="ns",
        cluster="cl",
        dsn=DSN,
        ssh="user@host;rm",
    )
    with pytest.raises(TunnelError) as exc:
        connect_dsn(cloud, book)
    assert spawned == []
    assert "dsn-secret-value" not in str(exc.value)
    assert ";" not in str(exc.value)


def test_ssh_failure_hides_the_password():
    class Dead:
        def poll(self):
            return 1

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 1

    book = TunnelBook(popen=lambda argv, **kwargs: Dead())
    with pytest.raises(TunnelError) as exc:
        connect_dsn(_cloud_with_ssh(), book)
    assert str(exc.value) == TUNNEL_FAILED
    assert "dsn-secret-value" not in str(exc.value)
    assert "bastion" not in str(exc.value)


def test_bad_ssh_in_config_hides_the_dsn(tmp_path):
    path = tmp_path / "clouds.yaml"
    path.write_text(
        "clouds:\n"
        "  - id: x\n"
        "    region: us\n"
        "    timezone: America/Chicago\n"
        f"    dsn: {DSN}\n"
        "    ssh: bad;rm\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_settings(path)
    assert "dsn-secret-value" not in str(exc.value)
    assert "Destino SSH inválido." in str(exc.value)


def test_example_config_has_no_ssh_secret():
    settings = load_settings(REPO_ROOT / "config" / "clouds.example.yaml")
    assert settings.clouds
    for cloud in settings.clouds.values():
        assert cloud.ssh_password == ""
        assert cloud.ssh_key == ""
        assert "CHANGE_ME" in cloud.dsn
        ZoneInfo(cloud.timezone)


def test_page_does_not_show_the_jump_host(monkeypatch):
    def boom(cloud, tunnels=None):
        raise TunnelError(TUNNEL_FAILED)

    monkeypatch.setattr("app.main.connect_dsn", boom)
    cloud = _cloud_with_ssh()
    app = create_app(settings=settings_for(cloud))
    page = TestClient(app).get("/", params={"cloud": cloud.id, "domain": "tenant.example", "q": CALL}).text
    assert page.count(TUNNEL_FAILED) == 1
    assert "dsn-secret-value" not in page
    assert "bastion" not in page
    assert "user@" not in page


def test_same_domain_and_server_error_shows_once():
    from app.db import CdrReadError

    class BrokenLists:
        def list_domains(self):
            raise CdrReadError(TUNNEL_FAILED)

        def list_servers(self):
            raise CdrReadError(TUNNEL_FAILED)

        def list_gateways(self, domain):
            return []

    cloud = _cloud_with_ssh()
    app = create_app(
        settings=settings_for(cloud),
        repo_for=lambda chosen: BrokenLists(),
    )
    page = TestClient(app).get("/", params={"cloud": cloud.id}).text
    assert page.count(TUNNEL_FAILED) == 1
    assert 'id="server-error" class="error" hidden' in page


def test_ssh_password_stays_out_of_the_command_line():
    seen = {}

    def popen(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env") or {}
        return ListeningProc(argv)

    cloud = Cloud(
        id="example-us",
        region="us",
        namespace="ns",
        cluster="cl",
        dsn=DSN,
        ssh="user@bastion.example",
        ssh_password="jump-secret-value",
    )
    local = connect_dsn(cloud, TunnelBook(popen=popen))
    assert "jump-secret-value" not in " ".join(seen["argv"])
    assert "BatchMode=yes" not in seen["argv"]
    assert "PreferredAuthentications=password,keyboard-interactive" in seen["argv"]
    assert seen["env"]["CALLTRACE_SSH_PASSWORD"] == "jump-secret-value"
    assert seen["env"]["SSH_ASKPASS"].endswith("ssh_askpass.py")
    assert urlsplit(local).hostname == "127.0.0.1"
    assert "jump-secret-value" not in local


def test_ssh_key_is_passed_as_its_own_argument(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("not-a-real-key\n", encoding="utf-8")
    seen = {}

    def popen(argv, **kwargs):
        seen["argv"] = argv
        return ListeningProc(argv)

    cloud = Cloud(
        id="example-us",
        region="us",
        namespace="ns",
        cluster="cl",
        dsn=DSN,
        ssh="user@bastion.example",
        ssh_key=str(key),
    )
    connect_dsn(cloud, TunnelBook(popen=popen))
    argv = seen["argv"]
    assert argv[argv.index("-i") + 1] == str(key)
    assert "IdentitiesOnly=yes" in argv
    assert "BatchMode=yes" in argv


def test_missing_ssh_key_hides_the_secret(tmp_path):
    path = tmp_path / "clouds.yaml"
    path.write_text(
        "clouds:\n"
        "  - id: x\n"
        "    region: us\n"
        "    timezone: America/Chicago\n"
        f"    dsn: {DSN}\n"
        "    ssh: user@bastion.example\n"
        "    ssh_key: /no/such/calltrace-key\n"
        "    ssh_password: jump-secret-value\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_settings(path)
    assert str(exc.value) == "Chave SSH não encontrada."
    assert "jump-secret-value" not in str(exc.value)
    assert "dsn-secret-value" not in str(exc.value)
    assert "calltrace-key" not in str(exc.value)


def test_page_hides_the_ssh_password():
    cloud = Cloud(
        id="example-us",
        region="us",
        namespace="us-gocontact-mt",
        cluster="cl",
        dsn=DSN,
        ssh="user@bastion.example",
        ssh_password="jump-secret-value",
    )
    app = create_app(
        settings=settings_for(cloud),
        repo_for=lambda _cloud: MemoryRepo([], []),
        voip=FakeVoip(),
    )
    page = TestClient(app).get("/", params={"cloud": cloud.id, "domain": "tenant.example", "q": CALL}).text
    assert "jump-secret-value" not in page
    assert "bastion" not in page


def test_form_lists_cloud_id_only():
    cloud = _cloud_with_ssh()
    app = create_app(
        settings=settings_for(cloud),
        repo_for=lambda _cloud: MemoryRepo([], []),
        voip=FakeVoip(),
    )
    page = TestClient(app).get("/").text
    assert cloud.id in page
    assert "bastion" not in page
    assert "dsn-secret-value" not in page
