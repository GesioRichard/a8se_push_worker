# CLAUDE.md — a8se-push (worker de notificações de notícias)

> Copie este arquivo para a raiz do projeto Python como `CLAUDE.md`, e `news_push_worker.md` (do repositório do app) para `docs/SPEC.md`.

## O que é este projeto

Worker em Python que lê o RSS do portal a8se.com, escolhe quais notícias viram notificação push e publica em **tópicos do Firebase Cloud Messaging** para o app **Sistema Atalaia** (Flutter, projeto Firebase `app-radios-d4371`). Roda numa VM Linux própria, disparado a cada 2 minutos por um `systemd` timer.

**A especificação completa está em `docs/SPEC.md`** (contrato com o app, política de frequência, algoritmo, esquema SQLite, configuração, operação e critérios de aceite). Leia antes de qualquer mudança. Quando houver conflito, a SPEC vence.

## Regras que não podem ser quebradas

1. **Contrato com o app** (SPEC §4): tópicos `news_<slug>`, `news_urgente`, `news_teste`; `data` com `type="news"`, `guid`, `url` (https, a8se.com) e `category`, **todos strings**; `android.notification.channel_id="news"`; `tag`/`apns-collapse-id` = tópico da editoria; TTL de 4 h. O app já está publicado e depende disso: não mude sem combinar uma versão nova do app.
2. **Nunca notificar quadros de TV** (URL `/tv-atalaia/`) nem itens sem `content:encoded`: o app não tem texto para mostrar.
3. **Política de frequência** (SPEC §5) é requisito de produto, não sugestão. Teto global de 6/dia, intervalo de 40 min, 3/dia e 2 h por editoria, silêncio das 22h às 7h (America/Maceio), candidatos com até 60 min. Tudo vem da configuração; não coloque números fixos no código.
4. **Primeira execução não envia nada** (*seed*). Um deploy nunca pode disparar notícias antigas.
5. **Modos** `dry-run` / `test` / `production`. Desenvolvimento e testes usam `dry-run` ou `test` (este publica **só** em `news_teste`). Nunca troque para `production` sem o aceite da SPEC §12 e a confirmação explícita do responsável.
6. **Segredos**: o JSON da conta de serviço fica em `/etc/a8se-push/`, fora do repositório. Nunca faça commit dele, não o imprima em logs e não o copie para outro lugar.
7. O worker **não acessa dados de usuários** (Firestore). Só FCM (envio) e Remote Config (leitura do parâmetro `news_categories`).

## Estrutura sugerida

```
a8se_push/
  feed.py        # download + parse do RSS (entidades em dobro, AVIF, slug, apelidos)
  policy.py      # regras de frequência/seleção - funções puras, testáveis
  store.py       # SQLite (items, sends)
  fcm.py         # montagem da mensagem + envio (Admin SDK); respeita o modo
  config.py      # leitura do config.toml
  cli.py         # `a8se-push run | send | status`
tests/           # pytest + fixtures de RSS reais, sem rede
deploy/          # a8se-push.service, a8se-push.timer, exemplo de config.toml
```

## Comandos

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest                          # a suíte precisa passar antes de qualquer deploy
a8se-push run --config ./config.dev.toml    # uma execução (use mode = "dry-run")
a8se-push status
```

## Como validar

- Política e mensagem são testadas com funções puras e relógio injetável (não use `datetime.now()` direto na lógica).
- Antes de ir para `production`: 48 h em `dry-run` na VM (4 a 6 envios/dia, nenhum de madrugada, nenhum de TV) e 24 h em `test` com um aparelho de debug do app.
