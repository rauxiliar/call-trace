# CDR Correlation

Internal read-only page that joins one cloud's FreeSWITCH CDR with VoIPmonitor and opens logs in Grafana Explore.

It does not write to the databases, does not change the `cdrs` schema, and does not draw a SIP ladder or a player. The cloud is always the one chosen in the form.

## Configuration

Secrets stay on the server.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/clouds.example.yaml config/clouds.yaml
```

`.env` has `VOIPMONITOR_USER`, `VOIPMONITOR_PASSWORD`, `CF_ACCESS_CLIENT_ID`, and `CF_ACCESS_CLIENT_SECRET`. Those are the local GUI user and the Cloudflare Access service token. They never reach the browser.

`config/clouds.yaml` (gitignored) lists each cloud with `id`, `region` (`us` or `eu`), `namespace`, `timezone` (IANA name of the CDR clock, for example `America/Chicago`), `dsn` for the CDR database, and `domains_dsn` for the domains database (`public.v_domains`). The page filters and shows times in that timezone. VoIPmonitor stays on `America/Los_Angeles`. Both connections open with `default_transaction_read_only=on`. If the host is the same, they share the SSH tunnel.

On the cloud, `dsn` points straight at Postgres. On a laptop, the same, when the network can reach the database. If the database is only reachable through a jump host, add `ssh: user@bastion` on that cloud and one of: `ssh_key` with the private key path, or `ssh_password`. The password stays in the `ssh` process and does not appear on the command line. Without `ssh_password`, the app uses the key, the agent, or `~/.ssh/config`. Without an `ssh` line, there is no tunnel. VoIPmonitor still goes out over HTTPS.

`region: us` uses Loki-US. `region: eu` uses Loki-EU. The Logs link opens `{namespace="..."}` when the namespace exists, and another query `{cloud="<id>"}`. The two do not go in the same selector.

## Start

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

The form asks for the cloud. The domain comes from that cloud's list, starts empty, and is required to search. The identifier (call UUID or SIP Call-ID) stays optional. Without it, a window of up to 24 hours restricts the search. Without a window, the page shows the most recent legs, 25 at a time, up to 20 pages. Direction and hangup cause are lists. Caller and callee refine the read.

## Internal login

This repository has no web session. The route depends on `require_internal_user` in [`app/auth.py`](app/auth.py). When an internal login exists, that is where it applies. A new SSO is not planned.

## Tests

```bash
pytest
```
