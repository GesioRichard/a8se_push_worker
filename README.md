# a8se-push

Worker que lê o RSS do a8se.com, escolhe quais notícias viram notificação e publica em tópicos do Firebase Cloud Messaging para o app **Sistema Atalaia**. A especificação completa (contrato com o app, política de frequência, algoritmo) está em [docs/SPEC.md](docs/SPEC.md).

- `a8se-push run`: uma execução (disparada pelo timer a cada 2 min).
- `a8se-push send --url <url> [--urgent] [--force]`: envio manual de uma matéria que está no feed.
- `a8se-push status`: envios do dia, fila, último envio e se a próxima execução pode enviar.

## Desenvolvimento

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                            # sem rede; precisa passar antes de qualquer deploy

cp deploy/config.example.toml config.dev.toml     # mode = "dry-run"
# em config.dev.toml, troque state_db para "./var/state.db"
a8se-push run --config ./config.dev.toml          # 1ª execução: seed (não envia nada)
a8se-push status --config ./config.dev.toml
```

`config.dev.toml` e `var/` ficam fora do Git. Sem `GOOGLE_APPLICATION_CREDENTIALS`, o worker usa os apelidos padrão (`esporte → esportes`) e avisa no log. Em `dry-run` ele nunca chama o FCM.

Para as fixtures, `tests/fixtures/rss_2026-09-30.xml` é uma cópia real de `https://a8se.com/rss.xml`.

## Instalação na VM (Ubuntu 24.04 / Debian 12)

Precisa de Python 3.11 ou mais novo. Ubuntu 24.04 (3.12) e Debian 12 (3.11) já vêm com uma versão adequada. No Ubuntu 22.04, instale `python3.11 python3.11-venv` e use `python3.11` no lugar de `python3` no passo 2.

Todos os comandos abaixo rodam como root (`sudo -i`).

### 1. Pacotes, usuário e diretórios

```bash
apt update && apt install -y python3 python3-venv git curl

useradd --system --no-create-home --home-dir /var/lib/a8se-push --shell /usr/sbin/nologin a8se-push
install -d -o root -g a8se-push -m 750 /etc/a8se-push
install -d -o root -g root -m 755 /opt/a8se-push
```

`/var/lib/a8se-push` é criado pelo systemd (`StateDirectory`) na primeira execução.

### 2. Código e venv

```bash
git clone <URL-do-repositório> /opt/a8se-push/src
python3 -m venv /opt/a8se-push/venv
/opt/a8se-push/venv/bin/pip install --upgrade pip
/opt/a8se-push/venv/bin/pip install "/opt/a8se-push/src[dev]"

# testes na própria VM (sem rede)
cd /opt/a8se-push/src && /opt/a8se-push/venv/bin/pytest -q
```

### 3. Configuração

```bash
install -o root -g a8se-push -m 640 /opt/a8se-push/src/deploy/config.example.toml /etc/a8se-push/config.toml
install -o root -g a8se-push -m 640 /opt/a8se-push/src/deploy/a8se-push.env.example /etc/a8se-push/a8se-push.env
install -o root -g root -m 755 /opt/a8se-push/src/deploy/a8se-push-cli.sh /usr/local/bin/a8se-push
```

Confira que `/etc/a8se-push/config.toml` está com `mode = "dry-run"`.

### 4. Conta de serviço (segredo)

Crie uma conta **dedicada** ao worker (SPEC §13):

1. [Google Cloud Console](https://console.cloud.google.com/) → projeto **app-radios-d4371** → *IAM e administrador* → *Contas de serviço* → *Criar conta de serviço*. Nome: `a8se-push-worker`.
2. Papéis: **Firebase Cloud Messaging API Admin** e **Firebase Remote Config Viewer**. Nenhum outro.
3. Na conta criada → *Chaves* → *Adicionar chave* → *JSON*. O download acontece uma vez só.
4. Copie o JSON direto para a VM, sem passar por repositório, e-mail ou chat:

   ```bash
   # na sua máquina
   scp a8se-push-worker-XXXX.json usuario@vm:/tmp/sa.json
   # na VM, como root
   install -o a8se-push -g a8se-push -m 600 /tmp/sa.json /etc/a8se-push/service-account.json
   shred -u /tmp/sa.json
   ```
5. Apague a cópia local do JSON depois de confirmar que o worker funciona.

Para o `dry-run` a conta é opcional: sem ela, o worker usa os apelidos padrão e registra um aviso. Ela é obrigatória a partir do modo `test`.

### 5. systemd

```bash
cp /opt/a8se-push/src/deploy/a8se-push.service /opt/a8se-push/src/deploy/a8se-push.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start a8se-push.service          # 1ª execução manual: faz o seed
journalctl -u a8se-push -n 20 --no-pager   # deve mostrar {"event": "seeded", ...}
systemctl enable --now a8se-push.timer
systemctl list-timers a8se-push.timer
```

### 6. Monitoramento (healthchecks.io)

1. Crie uma conta gratuita em [healthchecks.io](https://healthchecks.io/) e um *check* com **Period = 2 minutes** e **Grace = 15 minutes**.
2. Configure as integrações de aviso (e-mail, WhatsApp, Telegram).
3. Cole a URL de ping (`https://hc-ping.com/<uuid>`) em `healthcheck_url` no `/etc/a8se-push/config.toml`.

O worker pinga a URL ao fim de toda execução bem-sucedida e pinga `<url>/fail` a partir da 5ª falha seguida de leitura do RSS (valor em `feed.failure_alert_threshold`).

## Operação

```bash
sudo a8se-push status
sudo a8se-push send --url "https://a8se.com/noticias/.../244700-..." --urgent   # plantão
sudo a8se-push send --url "..."             # normal; respeita a política
sudo a8se-push send --url "..." --force     # normal ignorando a política (não vale para urgente)
journalctl -u a8se-push -f                  # log ao vivo (JSON por linha)
```

Regras do envio manual:
- A matéria precisa estar no RSS e ser matéria escrita (`/noticias/`, com texto). Quadros de TV nunca são enviados, nem com `--force`.
- `--urgent` publica em `news_<slug>` e `news_urgente`, ignora o silêncio e o intervalo global, mas nunca passa de `urgent_max_per_day`.
- Em `production`, pede confirmação `[s/N]`.

### Atualizar a versão

```bash
cd /opt/a8se-push/src && git pull
/opt/a8se-push/venv/bin/pip install "/opt/a8se-push/src[dev]"
/opt/a8se-push/venv/bin/pytest -q
```

O timer pega a nova versão na próxima execução. O estado (`state.db`) é preservado, então o deploy não reenvia nada.

### Relatório do dry-run

Envios simulados por dia e horário (precisa de `jq`):

```bash
journalctl -u a8se-push --since "-48h" -o cat \
  | jq -r 'select(.event=="would_send") | "\(.local_time)  \(.slug)  \(.title)"'
```

Tente também `sudo a8se-push status` ao longo do dia.

## Roteiro até produção (SPEC §12)

O modo só é trocado por quem responde pelo projeto, editando `/etc/a8se-push/config.toml`.

1. **48 h em `dry-run`**: o relatório acima mostra de 4 a 6 envios por dia, nenhum entre 22h e 7h e nenhum de `/tv-atalaia/`.
2. **24 h em `test`** (`mode = "test"`, com a conta de serviço instalada): um aparelho com build de debug do app e notificações ligadas recebe as notificações em `news_teste`. Confira que uma nova da mesma editoria substitui a anterior e que o toque abre a matéria certa.
3. **Urgente em `test`**: `sudo a8se-push send --url "..." --urgent` chega ao aparelho de debug.
4. **`production`**: só com o aceite dos itens acima e a confirmação explícita do responsável.

Ao trocar de modo, as contagens do dia recomeçam, porque cada modo conta só os próprios envios.
