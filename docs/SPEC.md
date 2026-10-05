# Worker de push de notícias — especificação

> Documento de referência para o projeto **Python** que envia as notificações de notícias do app **Sistema Atalaia** (Flutter). É a fonte da verdade do contrato entre o worker e o app; mudanças aqui precisam ser refletidas no app (`lib/services/push/`) e vice-versa.
>
> Versão 3 · 02/10/2026 (política de frequência revista após o dry-run: 8/dia, 60 min, cotas por período)

## 1. Contexto

O app Sistema Atalaia (Android/iOS) mostra as notícias do portal **a8se.com** a partir do RSS público `https://a8se.com/rss.xml`. O usuário escolhe editorias em **Preferências de notícias** e pode ligar as notificações. O app **já está pronto e em produção nesse ponto**:

- assina tópicos do Firebase Cloud Messaging (FCM) conforme as preferências;
- exibe a notificação (Android: canal `news`; iOS: nativo) e, ao toque, abre a matéria no leitor nativo do app;
- projeto Firebase: **`app-radios-d4371`**.

Falta o **worker**: o processo que lê o RSS, decide **o que** e **quando** notificar, e publica no FCM.

## 2. Decisão de arquitetura

**Worker Python em uma VM Linux própria (24/7), agendado por `systemd`, publicando em tópicos do FCM via Firebase Admin SDK.**

```
a8se.com/rss.xml ──(a cada 2 min)──▶ worker (VM, systemd timer)
                                        │  SQLite: itens vistos, fila, envios
                                        │  política de frequência
                                        ▼
                               Firebase Admin SDK ──▶ FCM (tópicos) ──▶ aparelhos
```

Por que essa arquitetura:

| Critério | Worker em VM + tópicos FCM |
|---|---|
| **Custo** | Zero adicional: a VM já existe e o FCM é gratuito. Firebase Cloud Functions exigiria o plano pago (Blaze). |
| **Escala** | O trabalho do worker **não cresce com o número de usuários**: ele publica 1 mensagem por notícia num tópico, e o FCM entrega para 100 ou 1 milhão de aparelhos. Não há tokens nem usuários para iterar. |
| **Estabilidade** | Processo curto (roda, envia no máximo 1 notícia, termina) disparado por timer: sem vazamento de memória nem processo travado. Estado em SQLite local, com lock contra execuções sobrepostas. |
| **Controle** | Toda a política editorial (frequência, horários, prioridades) fica num lugar só, versionada e testável. |

Ponto fraco aceito: a VM é um ponto único de falha. Se ela cair, as notificações **param** (nenhuma notícia é enviada em dobro nem fora de hora). Isso se mitiga com monitoramento (seção 11).

Alternativas descartadas agora:
- **Firebase Cloud Functions agendada**: gerenciada, mas exige o plano Blaze, a política ficaria espalhada entre funções e Firestore, e não traz ganho real sobre a VM.
- **Envio por token individual**: exigiria ler usuários e tokens no Firestore e custaria leituras. Os tópicos resolvem o caso de "notícias por editoria" sem isso.

**Futuro (back-end do A8SE):** o ideal de longo prazo é o próprio CMS disparar o push na publicação (com um botão "enviar notificação" para a redação). Mantenha a **política** (seção 5) isolada da **fonte** (RSS), para trocar só a fonte depois.

## 3. O que sabemos do feed (medido em 29–30/09/2026)

- ~**200 itens**, cobrindo vários dias; `guid` = `link`; **sem `ETag`/`Last-Modified`** (compare o conteúdo).
- `pubDate` em RFC 822 com fuso `-0300` (ex.: `Tue, 29 Sep 2026 18:50:00 -0300`). Fuso de Sergipe: **`America/Maceio`** (UTC-3, sem horário de verão).
- **~2/3 dos itens são quadros de TV** (URL `https://a8se.com/tv-atalaia/<programa>/...`), **sem `content:encoded`** e muitas vezes sem imagem. **Não geram notificação** (seção 6).
- Matérias escritas: URL `https://a8se.com/noticias/<slug>/<aaaa>/<mm>/<dd>/<id>-<titulo>`, com `description` (linha fina), `content:encoded` (texto completo em HTML) e `media:content` (imagem 1200x630).
- O feed **codifica entidades HTML em dobro** (`&amp;apos;`, `&amp;amp;`). Aplique `html.unescape()` repetidamente até o texto parar de mudar.
- Às vezes a imagem é **AVIF** (URL terminando em `.avif`). **Não mande**: o Android não exibe AVIF em notificação.
- Namespaces: `media` = `http://search.yahoo.com/mrss/`, `content` = `http://purl.org/rss/1.0/modules/content/`.

## 4. Contrato com o app

### 4.1 Tópicos

| Tópico | Quem assina |
|---|---|
| `news_<slug>` | Aparelhos que seguem a editoria `<slug>` **e** ligaram as notificações. |
| `news_urgente` | Aparelhos com notificações ligadas e "Plantão e notícias urgentes" ativo (padrão: ativo). |
| `news_teste` | **Só builds de debug** do app, com notificações ligadas. Use no modo `test` (seção 9). |

- **Slug** = segmento depois de `/noticias/` na URL (`/noticias/policia/...` → `policia`). Para URLs fora desse padrão, o app usa o texto do `<category>` sem acentos, minúsculo, com `-` no lugar de espaços e pontuação.
- **Apelidos**: o parâmetro `news_categories` do **Firebase Remote Config** é uma lista JSON; cada entrada tem `slug` e, opcionalmente, `aliases` (outros slugs que contam como ele). O worker precisa aplicar o mesmo mapeamento `alias → slug` para publicar no tópico canônico. Sem o parâmetro publicado, o padrão do app é `esporte → esportes`. Leia o template com o Admin SDK (`firebase_admin.remote_config`) no início de cada execução, com cache local de 1 h.
- Nome de tópico só aceita `[a-zA-Z0-9-_.~%]`. Qualquer outro caractere vira `-`.

### 4.2 Mensagem

```json
{
  "condition": "'news_policia' in topics",
  "notification": {
    "title": "Três celulares roubados são recuperados em Aracaju",
    "body": "Aparelhos foram localizados em uma residência no bairro São Conrado",
    "image": "https://img.dhost.cloud/.../1200x630/smart/https://cdn.a8se.com/images/...jpeg"
  },
  "data": {
    "type": "news",
    "guid": "https://a8se.com/noticias/policia/2026/09/28/244663-...",
    "url": "https://a8se.com/noticias/policia/2026/09/28/244663-...",
    "category": "policia"
  },
  "android": {
    "priority": "high",
    "ttl": "14400s",
    "notification": { "channel_id": "news", "tag": "news_policia" }
  },
  "apns": {
    "headers": { "apns-collapse-id": "news_policia", "apns-expiration": "<agora + 4h, epoch>" },
    "payload": { "aps": { "mutable-content": 1, "sound": "default" } }
  }
}
```

Regras do app (se violadas, o toque não abre a matéria):
- `data.type` = `"news"`.
- `data.url` **https** do domínio **a8se.com**. Outros domínios são ignorados.
- `data.guid` = `<guid>` do RSS. `data.category` = slug canônico (usado em métricas).
- Todos os valores de `data` são **strings**.

Decisões de conteúdo:
- `title` = título da matéria, limpo. `body` = `description` limpa, até **160 caracteres** (corte em limite de palavra + "…").
- `image` = `media:content` (nunca AVIF). No iOS a imagem ainda não aparece, porque o app não tem *Notification Service Extension*. `mutable-content` já prepara isso.
- **`tag` / `apns-collapse-id` = tópico da editoria**: uma notícia nova de Polícia **substitui** a anterior de Polícia na bandeja, em vez de empilhar.
- **TTL de 4 h**: notícia velha não é entregue a quem ficou offline.
- `condition` aceita até 5 tópicos. Uma matéria normal usa **1** (`news_<slug>`). Urgente usa `'news_<slug>' in topics || 'news_urgente' in topics`.

## 5. Política de frequência (anti-spam)

Objetivo: o usuário **nunca** ter motivo para desligar as notificações. Portais bem avaliados costumam ficar entre **3 e 6 notificações por dia**; acima disso, a taxa de desativação sobe rápido. O teto de 8 só é atingido em dias de muita notícia, e as cotas por período impedem que ele se concentre num só horário.

Limitação importante dos tópicos: o servidor **não sabe** quantas editorias cada pessoa segue. Por isso o controle mais forte é um **teto global**. Se o worker manda no máximo 6 por dia **no total**, ninguém recebe mais que 6, mesmo seguindo todas as editorias.

| Regra | Valor padrão | Por quê |
|---|---|---|
| Teto global por dia (normais) | **8** | Teto real por usuário. |
| Cotas por período (acumuladas) | **até 3 antes das 12h, até 6 antes das 18h** | Cobre manhã, tarde e noite. No dry-run de 01/10 o teto de 6 acabou às 13h e a tarde e a noite ficaram sem nada. O que um período não usa passa para o seguinte; a noite sempre tem pelo menos 2. |
| Intervalo mínimo entre dois envios quaisquer | **60 min** | Espalha ao longo do dia e acompanha o ritmo do feed, que é atualizado mais ou menos a cada hora. |
| Teto por editoria por dia | **3** | Uma editoria movimentada não monopoliza o dia. |
| Intervalo mínimo na mesma editoria | **2 h** | Evita "rajadas" de Polícia, por exemplo. |
| Horário de silêncio | **22:00–07:00** (America/Maceio) | Nada de normal à noite. O que surgir nesse período expira. |
| Idade máxima de um candidato | **60 min** desde o `pubDate` | Push é sobre o que está acontecendo agora. |
| Máximo por execução | **1** | Com o intervalo global, garante ritmo. |
| Urgente (`news_urgente`) | **máx. 2/dia, só manual** | Canal mais sensível: nunca automático. Pode furar o horário de silêncio e o intervalo global, mas não o próprio teto. |

Todos os valores ficam em configuração (seção 9), para ajustar sem mudar código. Revise depois de 2 a 3 semanas olhando o evento `news_push_open` no Firebase Analytics, que registra aberturas de notificação por editoria.

## 6. O que vira notificação

Um item do RSS é **candidato** se:
1. é matéria escrita: URL em `/noticias/` e `content:encoded` não vazio (quadro de TV, **não**);
2. o slug canônico está em `enabled_categories` (padrão: todas as editorias de notícia);
3. o `pubDate` tem no máximo 60 min e não está mais de 10 min no futuro;
4. o `guid` nunca foi visto antes;
5. o título, normalizado (minúsculo, sem acento e sem pontuação), não é igual ao de algo enviado nas últimas 24 h (republicação).

**Escolha** quando há mais de um candidato elegível: maior `priority` da editoria (configurável; ex.: Sergipe 3, Polícia 2, Política 2, Saúde 2, Brasil 1, Esportes 1…) e, no empate, o mais recente. Os outros **continuam na fila** até expirarem (60 min). Se couberem na janela seguinte, saem depois; se não, expiram. É o comportamento desejado: menos, e melhor.

## 7. Algoritmo de uma execução

Executada a cada **2 min** pelo `systemd` timer, com `flock` para nunca rodar duas ao mesmo tempo.

1. Baixar o RSS (timeout 20 s; 2 tentativas). Falhou: registra em log e sai com código 0 (a próxima execução tenta de novo).
2. Carregar os apelidos do Remote Config (cache de 1 h; se falhar, usa o último cache ou o padrão).
3. Para cada item **nunca visto**: gravar em `items` com status `pending` (se candidato) ou `skipped` com o motivo (`tv`, `no_body`, `old`, `category_disabled`, `duplicate_title`).
4. **Primeira execução (tabela vazia)**: gravar tudo como `seeded` e **não enviar nada**. Sem isso, o primeiro deploy dispara uma notícia antiga.
5. Marcar como `expired` os `pending` com mais de 60 min.
6. Se estiver no horário de silêncio, ou o teto global do dia ou a cota acumulada do período foi atingida, ou o último envio foi há menos de 60 min: **sair**.
7. Entre os `pending`, descartar os de editorias que atingiram o teto do dia ou enviaram há menos de 2 h; escolher 1 pela regra da seção 6.
8. Enviar. Sucesso: `sent` + registro em `sends` (id da mensagem FCM). Erro temporário (`UNAVAILABLE`, `INTERNAL`, timeout): mantém `pending` e tenta na próxima execução. Erro permanente (`INVALID_ARGUMENT`): `failed`, com log de erro.
9. Log estruturado (JSON por linha) de cada decisão relevante.

"Dia" = 00:00–23:59 em America/Maceio.

## 8. Estado (SQLite)

Arquivo: `/var/lib/a8se-push/state.db`.

```sql
CREATE TABLE items (
  guid           TEXT PRIMARY KEY,
  url            TEXT NOT NULL,
  title          TEXT NOT NULL,
  title_key      TEXT NOT NULL,        -- título normalizado (dedup)
  slug           TEXT NOT NULL,        -- canônico
  published_at   TEXT,                 -- ISO 8601 UTC
  first_seen_at  TEXT NOT NULL,        -- ISO 8601 UTC
  status         TEXT NOT NULL,        -- pending|sent|skipped|expired|failed|seeded
  reason         TEXT
);
CREATE TABLE sends (
  id             INTEGER PRIMARY KEY,
  guid           TEXT NOT NULL,
  slug           TEXT NOT NULL,
  urgent         INTEGER NOT NULL,
  sent_at        TEXT NOT NULL,        -- ISO 8601 UTC
  fcm_message_id TEXT,
  mode           TEXT NOT NULL         -- production|test
);
CREATE INDEX idx_items_status ON items(status);
CREATE INDEX idx_sends_sent_at ON sends(sent_at);
```

Limpeza: apagar `items` com mais de 30 dias (os `sends` ficam, para histórico).

## 9. Configuração

`/etc/a8se-push/config.toml` (sem segredos):

```toml
mode = "test"                 # dry-run | test | production
feed_url = "https://a8se.com/rss.xml"
timezone = "America/Maceio"
firebase_project = "app-radios-d4371"

[policy]
max_per_day = 8
min_gap_minutes = 60
period_caps = [{ until = "12:00", max = 3 }, { until = "18:00", max = 6 }]  # acumuladas no dia
per_category_max_per_day = 3
per_category_min_gap_minutes = 120
quiet_hours = ["22:00", "07:00"]
candidate_max_age_minutes = 60
urgent_max_per_day = 2
ttl_hours = 4

[categories]
enabled = []                  # vazio = todas as editorias de notícia
[categories.priority]
sergipe = 3
policia = 2
politica = 2
eleicoes = 2
saude = 2
economia = 2
brasil = 1
esportes = 1
```

Modos:
- **`dry-run`**: faz tudo, mas não chama o FCM. Só registra o que enviaria.
- **`test`**: envia de verdade, mas **só para `news_teste`**. Recebem apenas os aparelhos com build de debug do app e notificações ligadas. Nenhum usuário real é atingido.
- **`production`**: envia para os tópicos reais. Só ative depois do critério de aceite (seção 12).

Segredo: `GOOGLE_APPLICATION_CREDENTIALS=/etc/a8se-push/service-account.json` (via `EnvironmentFile` do systemd).

## 10. Envio manual (urgente)

CLI no servidor, via SSH:

```bash
a8se-push send --url "https://a8se.com/noticias/.../244700-..." --urgent   # plantão
a8se-push send --url "..."                                                  # reenviar/forçar normal
a8se-push status                                                            # envios do dia, fila, último envio
```

- `--urgent` publica em `news_<slug> || news_urgente`, respeita `urgent_max_per_day` e ignora o horário de silêncio e o intervalo global.
- A notícia precisa estar no feed, porque o app abre a matéria a partir dele.
- Pede confirmação (`[s/N]`) antes de enviar em `production`.

## 11. Operação na VM

- Usuário de sistema `a8se-push` (sem shell). Código em `/opt/a8se-push` (venv), estado em `/var/lib/a8se-push`, config e segredo em `/etc/a8se-push` (`chmod 600` no JSON).
- `a8se-push.service` (`Type=oneshot`, `ExecStart=/usr/bin/flock -n /run/a8se-push.lock /opt/a8se-push/venv/bin/a8se-push run`) + `a8se-push.timer` (`OnBootSec=1min`, `OnUnitActiveSec=2min`, `Persistent=true`).
- Logs em journald (`journalctl -u a8se-push`), em JSON por linha.
- **Monitoramento**: ao fim de toda execução bem-sucedida, um GET num serviço de *heartbeat* (ex.: healthchecks.io, gratuito). Se ficar 15 min sem ping, ele avisa por e-mail/WhatsApp. Também é útil alertar quando houver 5 falhas seguidas de leitura do RSS.
- Backup: não é necessário. Se o `state.db` for perdido, a próxima execução faz o *seed* de novo (sem envios). O único efeito é o limite diário recomeçar do zero naquele dia.
- Dependências: Python 3.11+, `firebase-admin`, `httpx` (ou `requests`), stdlib `sqlite3`/`zoneinfo`/`xml.etree`/`html`, `pytest`.

## 12. Testes e critério de aceite

Testes automatizados (`pytest`, sem rede), com fixtures de RSS reais:
- parse: `guid`, slug pela URL, apelidos, entidades em dobro, AVIF descartado, TV descartado;
- política: silêncio, teto global, teto e intervalo por editoria, idade máxima, prioridade, expiração, *seed* na primeira execução;
- mensagem: formato exato da seção 4.2 (`condition`, `data` só com strings, `tag`, TTL); urgente com 2 tópicos;
- modo `test` só publica em `news_teste`; modo `dry-run` nunca chama o FCM.

Aceite antes de `production`:
1. 48 h em `dry-run` na VM: o log mostra ~4–6 envios/dia, nenhum de madrugada, nenhum de TV.
2. 24 h em `test` com um aparelho de debug: as notificações chegam, substituem a anterior da mesma editoria e abrem a matéria certa no app.
3. `a8se-push send --urgent` testado em modo `test`.

## 13. Segurança

- Conta de serviço **dedicada** ao worker no Google Cloud do projeto `app-radios-d4371`, com papéis **Firebase Cloud Messaging API Admin** e **Firebase Remote Config Viewer**. Nada além disso.
- O JSON nunca vai para o Git, para o app nem para logs.
- O worker não lê nem grava dados de usuários (Firestore). Os tópicos dispensam isso.

## 14. Fora de escopo (por enquanto)

- Push individual por usuário (os tokens já ficam em `users/{uid}/devices/{id}` no Firestore, para o futuro).
- Imagem no iOS (depende de uma extensão no app).
- Resumo diário ("as 5 principais de hoje").
- Envio disparado pelo CMS do A8SE (substituirá a leitura do RSS; a política continua).
