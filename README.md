# IMA Research Bot

Pipeline para puxar research/prints/PDFs/tabelas direto do IMA/Tencent, resumir, gerar audio e mandar atualizacoes por Telegram ou WeCom.

## Fluxo

1. Ingestao: IMA/Tencent OpenAPI direto da knowledge base configurada em `IMA_KNOWLEDGE_BASE_ID`. Pasta local e apenas fallback/debug.
2. Extracao: PDF, TXT/MD e imagem. Imagem usa OCR local se `pytesseract` estiver instalado.
3. Resumo: OpenAI se `OPENAI_API_KEY` estiver configurada; fallback local simples se nao estiver.
4. Orçamento: `OPENAI_DAILY_BUDGET_USD` limita o gasto diario estimado em UTC.
5. Audio: OpenAI TTS se `TTS_PROVIDER=openai`; ElevenLabs continua disponivel com `TTS_PROVIDER=elevenlabs`.
5. Entrega: Telegram Bot API e/ou WeCom webhook.
6. Scheduler: roda a cada `POLL_INTERVAL_MINUTES`, processa poucos itens por ciclo e conecta o lote novo com a memoria recente.

## Setup

```bash
cd /Users/pedromaass/Documents/21:06/ima-research-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
cp .env.example .env
```

Edite `.env` com suas chaves. Para receber resumos e audio em ingles, use `SUMMARY_LANGUAGE=English`.

## Rodar uma vez

```bash
ima-research-bot run-once
```

## Testar IMA

Configure `IMA_CLIENT_ID` e `IMA_API_KEY` no `.env`, depois:

```bash
ima-research-bot ima-list-kb
```

Para listar itens de uma knowledge base especifica, coloque `IMA_KNOWLEDGE_BASE_ID` no `.env` e rode:

```bash
ima-research-bot ima-list-knowledge
```

Com `IMA_CLIENT_ID`, `IMA_API_KEY` e `IMA_KNOWLEDGE_BASE_ID` preenchidos, `run-once` e `serve` puxam direto do IMA. Se essas variaveis ficarem vazias, o bot usa `WATCH_DIR`.

## Rodar loop

```bash
ima-research-bot serve
```

No modo `serve`, o bot:

- consulta o IMA a cada `POLL_INTERVAL_MINUTES`;
- ou varre `WATCH_DIR` quando IMA nao esta configurado;
- ignora documentos ja processados usando `STATE_DB`;
- processa no maximo `PROCESS_LIMIT_PER_RUN` documentos novos por ciclo;
- no modo pasta local, ignora downloads incompletos (`.qkdownloading`, `.crdownload`, `.part`) e espera `LOCAL_FILE_MIN_AGE_SECONDS`;
- economiza chamadas do IMA com `IMA_MAX_PAGES`, `IMA_MAX_ITEMS` e `IMA_MAX_DOWNLOADS_PER_RUN`;
- se o IMA retorna limite diario, registra no log e espera o proximo ciclo sem derrubar o servico;
- salva os resumos recentes em SQLite;
- envia um `[Radar incremental]` conectando os documentos novos com a memoria recente.

## Modo Quark / pasta local

Quark nao envia evento em tempo real para o bot. O caminho estavel e deixar Quark baixar/sincronizar arquivos para uma pasta e fazer o bot varrer essa pasta em ciclos curtos.

Exemplo de `.env` no VPS:

```bash
WATCH_DIR=/opt/research-inbox
IMA_CLIENT_ID=
IMA_API_KEY=
IMA_KNOWLEDGE_BASE_ID=
POLL_INTERVAL_MINUTES=5
PROCESS_LIMIT_PER_RUN=3
LOCAL_FILE_MIN_AGE_SECONDS=120
LOCAL_MAX_ITEMS=200
```

Suba os PDFs do Mac para essa pasta com `scp` ou `rsync`. O bot processa os arquivos novos aos poucos e ignora downloads ainda incompletos.

## VPS

Use `systemd/ima-research-bot.service` como base. Copie para `/etc/systemd/system/`, ajuste caminho e usuario, depois:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ima-research-bot
```

Esse e o servico principal para deixar o bot vivo no VPS. Use `journalctl` para acompanhar:

```bash
sudo journalctl -u ima-research-bot -f
```

## Radar automatico no VPS

O comando `ima-research-bot radar` gera o radar macro/mercado a partir dos resultados de busca do IMA e envia para Telegram/WeCom. Para rodar automaticamente uma vez por dia:

```bash
sudo cp systemd/ima-research-radar.service /etc/systemd/system/
sudo cp systemd/ima-research-radar.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ima-research-radar.timer
```

O horario padrao e 09:00 em `Asia/Shanghai`. Para mudar, edite `OnCalendar` e/ou `TimeZone` em `/etc/systemd/system/ima-research-radar.timer`, rode `sudo systemctl daemon-reload` e reinicie o timer.

Para testar manualmente no VPS:

```bash
cd /opt/ima-research-bot
.venv/bin/python -m ima_research_bot radar
sudo systemctl start ima-research-radar.service
sudo systemctl status ima-research-radar.service --no-pager
```

## Observacoes

- Telegram e o caminho mais simples para MVP. O Bot API envia audio por `sendAudio` e mensagens por `sendMessage`.
- O audio e acionado automaticamente quando o provider escolhido esta configurado. Para OpenAI TTS, use `TTS_PROVIDER=openai`, `OPENAI_TTS_MODEL` e `OPENAI_TTS_VOICE`; o texto narrado segue `SUMMARY_LANGUAGE`.
- `OPENAI_DAILY_BUDGET_USD=2` aplica um teto diario local estimado. Quando o teto e atingido, o bot segue vivo e tenta novamente no proximo ciclo/dia.
- WeCom/WeChat Work funciona melhor via group robot webhook.
- WeChat pessoal e WeChat Official Account exigem fluxo mais chato de autorizacao.
- Nunca coloque chaves reais no git.
