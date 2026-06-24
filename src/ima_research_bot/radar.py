import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from openai import OpenAI

from .budget import DailyBudgetExceeded, OpenAIBudget, OpenAIPrices
from .config import Settings
from .connectors.ima_tencent import ImaTencentConfig, ImaTencentConnector
from .notifiers import TelegramNotifier, WeComNotifier
from .state import StateStore
from .tts import build_tts


@dataclass(frozen=True)
class RadarHit:
    query: str
    title: str
    media_id: str
    media_type: Optional[int]
    parent_folder_id: str
    highlight: str


class ImaRadar:
    DEFAULT_QUERIES = [
        "每日复盘",
        "AI",
        "半导体",
        "电力",
        "地产",
        "钢铁",
        "机器人",
        "算力",
        "PCB",
        "高盛",
        "花旗",
        "摩根",
    ]

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = StateStore(settings.state_db)
        self.budget = OpenAIBudget(
            self.state,
            settings.openai_daily_budget_usd,
            OpenAIPrices(
                text_input_per_1m=settings.openai_text_input_usd_per_1m,
                text_output_per_1m=settings.openai_text_output_usd_per_1m,
                tts_per_1k_chars=settings.openai_tts_usd_per_1k_chars,
            ),
        )
        self.ima = ImaTencentConnector(
            ImaTencentConfig(
                base_url=settings.ima_base_url,
                client_id=settings.ima_client_id,
                api_key=settings.ima_api_key,
            )
        )
        self.telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self.wecom = WeComNotifier(settings.wecom_webhook_url)
        self.tts = build_tts(settings, budget=self.budget)
        self.openai = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def run(self) -> str:
        if not self.settings.ima_knowledge_base_id:
            raise RuntimeError("Set IMA_KNOWLEDGE_BASE_ID in .env before radar")

        hits = self._collect_hits()
        try:
            report = self._build_report(hits)
        except DailyBudgetExceeded as exc:
            report = (
                "[OpenAI budget]\n\n"
                f"Daily limit reached: ${self.settings.openai_daily_budget_usd:.2f} UTC day.\n"
                f"Spent estimate today: ${self.budget.spent_today():.4f}.\n\n"
                f"{exc}"
            )
        if self.settings.send_text_updates:
            self.telegram.send_text(report)
            self.wecom.send_text(report)

        try:
            audio_path = self.tts.synthesize(report, "ima_radar")
        except DailyBudgetExceeded:
            audio_path = None
        if audio_path:
            self.telegram.send_audio(audio_path, caption="IMA Radar")

        return report

    def _collect_hits(self) -> list[RadarHit]:
        queries = _split_env("RADAR_QUERIES") or self.DEFAULT_QUERIES
        limit = int(os.getenv("RADAR_LIMIT_PER_QUERY", "8"))
        seen: set[str] = set()
        hits: list[RadarHit] = []

        for query in queries:
            payload = self.ima.search_knowledge(self.settings.ima_knowledge_base_id, query, cursor="")
            for row in (payload.get("info_list") or [])[:limit]:
                media_id = str(row.get("media_id") or "")
                if not media_id or media_id in seen:
                    continue
                seen.add(media_id)
                hits.append(
                    RadarHit(
                        query=query,
                        title=str(row.get("title") or ""),
                        media_id=media_id,
                        media_type=row.get("media_type"),
                        parent_folder_id=str(row.get("parent_folder_id") or ""),
                        highlight=str(row.get("highlight_content") or ""),
                    )
                )

        return hits

    def _build_report(self, hits: list[RadarHit]) -> str:
        if not hits:
            return "IMA Radar: nenhum resultado retornado pelas buscas configuradas."

        raw = "\n".join(
            (
                f"- query={hit.query} | type={hit.media_type} | title={hit.title}"
                + (f" | trecho={hit.highlight}" if hit.highlight else "")
            )
            for hit in hits
        )
        if not self.openai:
            return f"IMA Radar local, sem OpenAI configurado.\n\n{raw[:12000]}"

        prompt = f"""
Voce e um analista de research macro/mercado.
Voce recebeu resultados de BUSCA do IMA, nao o conteudo integral dos PDFs.
Use apenas titulos e trechos abaixo. Nao invente dados, price targets ou conclusoes que nao aparecam.
Quando a evidencia for so titulo, marque como "sinal fraco".

Gere um radar em {self.settings.summary_language} com:
1. Manchete executiva em 3 linhas
2. Clusters/temas dominantes
3. Sinais alarmantes ou fora da curva
4. Tickers/ativos/empresas citados nos titulos
5. Possiveis cadeias causais
6. Oportunidades tradables a verificar
7. Lacunas: o que exige leitura profunda depois
8. Lista curta dos documentos mais importantes para abrir no IMA manualmente

Data UTC: {datetime.now(timezone.utc).isoformat()}

Resultados de busca:
{raw[: self.settings.max_input_chars]}
"""
        self.budget.ensure_available(
            self.budget.estimate_text(len(prompt), output_tokens=2200),
            "daily radar",
        )
        response = self.openai.responses.create(
            model=self.settings.openai_model,
            input=prompt,
        )
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if input_tokens or output_tokens:
            self.budget.record_text(
                self.settings.openai_model,
                "daily_radar",
                input_tokens,
                output_tokens,
            )
        return response.output_text.strip()


def _split_env(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]
