# IMA Research Bot

Pipeline para manter uma base de research em Cloudflare R2, puxar novidades do IMA via OpenAPI, resumir documentos e entregar atualizacoes por Telegram ou WeCom.

## Arquitetura

```text
Base fixa local -> storage-upload-local -> Cloudflare R2
IMA OpenAPI     -> ima-sync-r2          -> Cloudflare R2
Cloudflare R2   -> run-once/serve       -> resumo/audio/Telegram
SQLite          -> estado, dedupe, memoria e orçamento
```

O R2 e a fonte canonica dos arquivos. A VPS roda o bot, usa disco apenas como cache temporario e guarda metadados/estado em SQLite.

A coleta do IMA e feita por OpenAPI ou outro metodo HTTP automatizavel.

## Setup

```bash
cd /opt/ima-search-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
cp .env.example .env
```

Edite `.env` com:

- credenciais R2: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`;
- credenciais IMA: `IMA_CLIENT_ID`, `IMA_API_KEY`, `IMA_KNOWLEDGE_BASE_ID`;
- entrega: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`;
- resumo: `OPENAI_API_KEY`.

## Subir a Base Fixa para R2

No Mac ou na VPS, a partir de uma pasta local com PDFs:

```bash
ima-research-bot storage-upload-local "/path/da/base-fixa"
```

Para testar com poucos arquivos:

```bash
STORAGE_UPLOAD_LIMIT=5 ima-research-bot storage-upload-local "/path/da/base-fixa"
```

Ver estatisticas locais registradas no SQLite:

```bash
ima-research-bot storage-stats
```

## Sincronizar Novos do IMA para R2

O sync lista poucos candidatos recentes no IMA, baixa no maximo `IMA_R2_MAX_UPLOADS`, sobe para R2 e registra dedupe no SQLite:

```bash
ima-research-bot ima-sync-r2
```

Configuracao conservadora recomendada:

```bash
IMA_MAX_PAGES=1
IMA_MAX_ITEMS=10
IMA_R2_MAX_UPLOADS=1
IMA_LATEST_DATE_FOLDER=1
IMA_LATEST_ONLY=1
IMA_TARGET_DAYS_AGO=0
IMA_TARGET_UTC_OFFSET_HOURS=8
```

Para uma data especifica:

```bash
IMA_TARGET_REPORT_DATE=2026-06-27 ima-research-bot ima-sync-r2
```

## Processar R2

Processar uma rodada:

```bash
ima-research-bot run-once
```

Rodar como loop:

```bash
ima-research-bot serve
```

No modo `serve`, o scheduler processa documentos do R2 a cada `POLL_INTERVAL_MINUTES`. O coletor IMA fica separado: rode `ima-sync-r2` por cron/systemd timer antes do processamento, ou em outro servico.

## Comandos

```bash
ima-research-bot ima-list-kb
ima-research-bot ima-list-knowledge
ima-research-bot ima-sync-r2
ima-research-bot storage-upload-local /path/da/base
ima-research-bot storage-stats
ima-research-bot run-once
ima-research-bot serve
ima-research-bot digest
ima-research-bot radar
```

## Systemd

Use `systemd/ima-research-bot.service` como base:

```bash
sudo cp systemd/ima-research-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ima-research-bot
sudo journalctl -u ima-research-bot -f
```

Um timer separado deve chamar `ima-research-bot ima-sync-r2` para alimentar o R2 com novidades do IMA.
