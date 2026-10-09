# Correlação CDR

Página interna, só de leitura, que junta a CDR do FreeSWITCH de um cloud com o VoIPmonitor e abre os logs no Grafana Explore.

Não escreve nas bases, não altera o esquema `cdrs` e não desenha a escada SIP nem um player. O cloud é sempre o que estiver escolhido no formulário.

## Configuração

Segredos ficam no servidor.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/clouds.example.yaml config/clouds.yaml
```

`.env` tem `VOIPMONITOR_USER`, `VOIPMONITOR_PASSWORD`, `CF_ACCESS_CLIENT_ID` e `CF_ACCESS_CLIENT_SECRET`. São o utilizador local da GUI e o service token do Cloudflare Access. Não vão para o browser.

`config/clouds.yaml` (gitignored) lista cada cloud com `id`, `region` (`us` ou `eu`), `namespace`, `timezone` (nome IANA do relógio da CDR, por exemplo `America/Chicago`), `dsn` da base das CDRs e `domains_dsn` da base dos domínios (`public.v_domains`). A página filtra e mostra as horas nessa timezone. O VoIPmonitor continua em `America/Los_Angeles`. As duas ligações abrem com `default_transaction_read_only=on`. Se o host for o mesmo, partilham o túnel SSH.

Na cloud, o `dsn` aponta diretamente ao Postgres. No laptop, o mesmo, quando a rede chega à base. Se a base só for alcançável por uma máquina intermédia, acrescenta `ssh: user@bastion` nesse cloud e uma de duas: `ssh_key` com o caminho da chave privada, ou `ssh_password`. A password fica no processo do `ssh` e não aparece na linha de comandos. Sem `ssh_password`, a app usa a chave, o agent ou o `~/.ssh/config`. Sem a linha `ssh`, não há túnel. O VoIPmonitor continua a sair por HTTPS.

`region: us` usa o Loki-US. `region: eu` usa o Loki-EU. O link Logs abre `{namespace="..."}` quando o namespace existe, e outra query `{cloud="<id>"}`. As duas não vão no mesmo seletor.

## Arranque

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

O formulário pede o cloud. O domínio vem da lista dessa cloud, começa vazio e é obrigatório para pesquisar. O identificador (call UUID ou SIP Call-ID) continua opcional. Sem ele, um intervalo até 24 horas restringe a pesquisa. Sem intervalo, a página mostra as pernas mais recentes, 25 de cada vez, até 20 páginas. Direção e hangup cause são listas. Origem e destino afinam a leitura.

## Login interno

Este repositório não tem sessão web. A rota depende de `require_internal_user` em [`app/auth.py`](app/auth.py). Quando existir um login interno, é aí que se aplica. Não está previsto um SSO novo.

## Testes

```bash
pytest
```
