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
LOCAL_LATEST_ONLY=1
```

Suba os PDFs do Mac para essa pasta com `scp` ou `rsync`. O bot processa os arquivos novos aos poucos e ignora downloads ainda incompletos.
Com `LOCAL_LATEST_ONLY=1`, ele processa apenas o dia de relatorio mais recente encontrado na pasta local, em vez de ir caindo em dias antigos nos ciclos seguintes.

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

## VPS sem IMA OpenAPI: downloader humano

Para evitar a quota da OpenAPI do IMA, rode um segundo worker no VPS que usa Chromium/Playwright com perfil persistente. Ele baixa PDFs pelo fluxo visual do `https://ima.qq.com` para `WATCH_DIR`, e o servico principal continua resumindo a pasta local.

Exemplo de `.env`:

```bash
WATCH_DIR=/opt/research-inbox
IMA_CLIENT_ID=
IMA_API_KEY=
IMA_KNOWLEDGE_BASE_ID=
IMA_HUMAN_URL=https://ima.qq.com
IMA_HUMAN_CDP_URL=
IMA_HUMAN_PROFILE_DIR=/opt/ima-browser-profile
IMA_HUMAN_DOWNLOAD_DIR=/opt/research-inbox
IMA_HUMAN_MAX_DOWNLOADS_PER_CYCLE=3
IMA_HUMAN_POLL_INTERVAL_MINUTES=10
IMA_HUMAN_HEADLESS=1
IMA_HUMAN_FOLDER_PATHS=【爱分享】盈策系列|六、高盛、花旗、瑞银、摩根等外资研报🌏|LATEST_SCOPE;;【爱分享】盈策系列|七、中金、中信、华泰等内资研报🌈|LATEST_SCOPE;;【爱分享】盈策系列|五、调研会议纪要📅|LATEST_SCOPE;;【爱分享】盈策系列|四、红宝书🏆|LATEST_SCOPE
IMA_HUMAN_LATEST_SCOPE_DEPTH=4
IMA_HUMAN_LATEST_VISIBLE_FILES_ONLY=1
```

`IMA_HUMAN_FOLDER_PATHS` descreve a arvore visual do IMA. Use `|` entre niveis de pasta e `;;` entre varios caminhos. Use `LATEST_SCOPE` quando as subpastas nao seguem sempre o mesmo desenho: ele abre automaticamente o mes/dia mais recente que estiver visivel e para quando chega nos PDFs. Os tokens `LATEST_MONTH` e `LATEST_DAY` continuam disponiveis quando a estrutura e sempre fixa, por exemplo:

```text
【爱分享】盈策系列
六、高盛、花旗、瑞银、摩根等外资研报🌏
2026年6月
2026-06-24
```

Para varrer mais de um caminho no mesmo worker:

```bash
IMA_HUMAN_FOLDER_PATHS=【爱分享】盈策系列|六、高盛、花旗、瑞银、摩根等外资研报🌏|LATEST_SCOPE;;【爱分享】盈策系列|七、中金、中信、华泰等内资研报🌈|LATEST_SCOPE
```

Quando a tela ja mistura PDFs de varias datas no mesmo lugar, `IMA_HUMAN_LATEST_VISIBLE_FILES_ONLY=1` faz o downloader baixar apenas os arquivos com o dia de relatorio mais recente no nome.

### Mapeamento validado da base `【爱分享】盈策系列`

O alvo atual do MVP e a base `【爱分享】盈策系列` aberta no app IMA. A base `【爱分享】的财经资讯` tem estrutura parecida e apareceu em mapeamentos anteriores, mas nao deve ser usada como default enquanto a tela de trabalho estiver em `盈策系列`.

O MVP deve usar uma inbox limpa e com poucos downloads por ciclo. No Mac local:

```bash
WATCH_DIR=/Users/pedromaass/Documents/21:06/ima-research-bot/inbox
IMA_HUMAN_DOWNLOAD_DIR=/Users/pedromaass/Documents/21:06/ima-research-bot/inbox
IMA_HUMAN_MAX_DOWNLOADS_PER_CYCLE=3
DIGEST_MODE=1
SEND_TEXT_UPDATES=1
```

Ramos PDF prioritarios para o MVP:

```bash
IMA_HUMAN_FOLDER_PATHS=【爱分享】盈策系列|六、高盛、花旗、瑞银、摩根等外资研报🌏|LATEST_SCOPE;;【爱分享】盈策系列|七、中金、中信、华泰等内资研报🌈|LATEST_SCOPE;;【爱分享】盈策系列|五、调研会议纪要📅|LATEST_SCOPE;;【爱分享】盈策系列|四、红宝书🏆|LATEST_SCOPE
```

Estrutura esperada desses ramos:

- `六、高盛、花旗、瑞银、摩根等外资研报🌏`: mes atual -> pasta do dia -> PDFs.
- `七、中金、中信、华泰等内资研报🌈`: mes atual -> pasta do dia -> PDFs.
- `五、调研会议纪要📅`: pasta mensal mais recente -> PDFs/notas visiveis.
- `四、红宝书🏆`: pasta mensal mais recente -> PDFs.

Ramos que devem entrar depois, porque misturam imagens, planilhas, notas ou feeds tematicos:

- `二、彭博社、路透社等外媒新闻🪐`
- `三、财联社VIP、脱水日报🌺`
- `八、每日复盘数据📈`
- `📚投资书籍➕数据📊`
- `大摩闭门会、洪灏、沙利文、年报等🍄`
- `九、题材概念产业库☎️`

Setup no VPS:

```bash
cd /opt/ima-research-bot
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium --with-deps
mkdir -p /opt/research-inbox /opt/ima-browser-profile
```

Primeiro login, usando Xvfb/noVNC ou uma sessao grafica equivalente:

```bash
ima-research-bot ima-human-login
```

Depois de escanear QR/login e confirmar que a knowledge base aparece, pare o comando com `Ctrl+C`. O perfil fica salvo em `IMA_HUMAN_PROFILE_DIR`.
Confirme que a janela do Playwright mostra `个人知识库` / `共享知识库` e a pasta desejada. Se aparecer apenas a home publica com `打开电脑版` ou um modal para baixar o Tencent IMA, esse perfil ainda nao esta autenticado para a coleta web.

Teste um ciclo:

```bash
ima-research-bot ima-human-download
ima-research-bot run-once
```

Para deixar automatico:

```bash
sudo cp systemd/ima-human-downloader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ima-human-downloader
sudo systemctl restart ima-research-bot
sudo journalctl -u ima-human-downloader -f
```

O downloader e conservador por padrao: tenta abrir a pasta/data mais recente visivel, baixa no maximo tres PDFs por ciclo, salva downloads temporarios em `/opt/research-inbox/.downloads` e para com log claro se aparecer login, captcha ou mudanca grande na UI do IMA.

### Usar o app desktop IMA aberto no Mac

A pagina real do app desktop aparece como `chrome://allknowledge/` com conteudo `chrome-extension://...`. Ela nao e a mesma coisa que `https://ima.qq.com`, que pode cair numa tela publica pedindo para abrir/baixar o cliente. Para controlar a pagina do app com Playwright, abra o IMA com CDP:

```bash
osascript -e 'quit app "ima.copilot"'
open -na /Applications/ima.copilot.app --args --remote-debugging-port=9222
```

Depois configure:

```bash
IMA_HUMAN_CDP_URL=http://127.0.0.1:9222
IMA_HUMAN_FOLDER_PATHS=【爱分享】盈策系列|六、高盛、花旗、瑞银、摩根等外资研报🌏|LATEST_SCOPE;;【爱分享】盈策系列|七、中金、中信、华泰等内资研报🌈|LATEST_SCOPE;;【爱分享】盈策系列|五、调研会议纪要📅|LATEST_SCOPE;;【爱分享】盈策系列|四、红宝书🏆|LATEST_SCOPE
```

Nesse modo, `ima-human-download` tenta reutilizar a aba viva do app IMA em vez de criar uma aba web nova.

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
