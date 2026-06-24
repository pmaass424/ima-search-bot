from dataclasses import dataclass
from typing import Optional

from openai import OpenAI

from .budget import OpenAIBudget


@dataclass(frozen=True)
class Summary:
    title: str
    text: str


class Summarizer:
    def __init__(
        self,
        api_key: str,
        model: str,
        language: str,
        max_input_chars: int,
        budget: Optional[OpenAIBudget] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.language = language
        self.max_input_chars = max_input_chars
        self.budget = budget
        self.client = OpenAI(api_key=api_key) if api_key else None

    def summarize(self, title: str, text: str) -> Summary:
        text = text[: self.max_input_chars]
        if not text.strip():
            return Summary(title=title, text=f"Sem texto extraivel em {title}.")
        if not self.client:
            return self._fallback(title, text)

        prompt = f"""
Voce e um analista de research macro/mercado.
Resuma o documento abaixo em {self.language}, com foco em:
- tese central
- dados fora da curva
- setores/tickers citados
- implicacoes tradables
- riscos e o que verificar

Para audio: nao comece repetindo o titulo do relatorio; va direto ao resumo em {self.language}.

Titulo: {title}

Documento:
{text}
"""
        if self.budget:
            self.budget.ensure_available(
                self.budget.estimate_text(len(prompt), output_tokens=1800),
                "research summary",
            )
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
        )
        self._record_usage(response, "summary")
        return Summary(title=title, text=response.output_text.strip())

    def connect_batch(self, new_summaries: list[Summary], recent_context: list[Summary]) -> str:
        if not new_summaries:
            return ""

        new_block = "\n\n".join(
            f"### {item.title}\n{item.text}" for item in new_summaries
        )
        context_block = "\n\n".join(
            f"### {item.title}\n{item.text}" for item in recent_context
        )

        if not self.client:
            titles = "\n".join(f"- {item.title}" for item in new_summaries)
            return f"Radar incremental local, sem OpenAI configurado.\n\nNovos documentos processados:\n{titles}"

        prompt = f"""
Voce e um analista de research macro/mercado acompanhando uma base IMA ao vivo.
Conecte os NOVOS documentos abaixo com a MEMORIA recente. Use apenas as informacoes fornecidas.
Nao faca apenas um resumo dos NOVOS documentos. Cruze China/domestico com global/internacional sempre que houver memoria para isso.
Procure confirmacoes, contradicoes, relacoes lead-lag e checks de mercado que faltam.
Nao invente numeros, recomendacoes ou eventos. Quando uma conexao for especulativa, marque como hipotese.
Se a memoria recente nao trouxer base internacional suficiente, diga isso claramente.

Gere em {self.language}:
1. O que mudou agora
2. Leitura cruzada China/domestico vs global/internacional
3. Conexoes entre setores/empresas/temas
4. Sinais fortes vs sinais fracos
5. O que vale abrir manualmente no IMA
6. Proximas perguntas para investigar

NOVOS DOCUMENTOS:
{new_block[: self.max_input_chars]}

MEMORIA RECENTE:
{context_block[: self.max_input_chars // 2]}
"""
        if self.budget:
            self.budget.ensure_available(
                self.budget.estimate_text(len(prompt), output_tokens=1800),
                "batch insight",
            )
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
        )
        self._record_usage(response, "batch_insight")
        return response.output_text.strip()

    def build_digest(self, new_summaries: list[Summary], recent_context: list[Summary]) -> str:
        if not new_summaries:
            return ""

        new_block = "\n\n".join(
            f"### {item.title}\n{item.text}" for item in new_summaries
        )
        context_block = "\n\n".join(
            f"### {item.title}\n{item.text}" for item in recent_context
        )

        if not self.client:
            titles = "\n".join(f"- {item.title}" for item in new_summaries)
            return f"Hourly research digest.\n\nNew documents:\n{titles}"

        prompt = f"""
You are a concise institutional research analyst.
Create one spoken hourly digest in {self.language} from the NEW document summaries below.
Do not merely summarize the NEW documents. Cross them against RECENT MEMORY, especially China/domestic reports versus global/international reports.
Use recent memory to find confirmations, contradictions, lead-lag relationships, and missing-market checks.
Do not invent numbers, recommendations, or events.

Format for audio:
1. Start after the source/date confirmation that the pipeline prepends; do not repeat that metadata.
2. Give a one-sentence headline that reflects the cross-market read, not just the new batch.
3. Give the 3-5 most important cross-document, cross-market insights.
4. Do not read or enumerate report titles. Refer to sectors, tickers, firms, commodities, countries, and themes instead.
5. Only mention a report title if it is absolutely necessary to disambiguate a specific claim.
6. Explicitly say when China/domestic evidence confirms, conflicts with, or lacks enough international evidence.
7. Highlight sector/ticker/theme connections.
8. End with what is worth checking manually next.

Keep it tight and natural to listen to. Avoid markdown tables.

NEW SUMMARIES:
{new_block[: self.max_input_chars]}

RECENT MEMORY:
{context_block[: self.max_input_chars // 2]}
"""
        if self.budget:
            self.budget.ensure_available(
                self.budget.estimate_text(len(prompt), output_tokens=1600),
                "hourly digest",
            )
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
        )
        self._record_usage(response, "hourly_digest")
        return response.output_text.strip()

    def _record_usage(self, response, category: str) -> None:
        if not self.budget:
            return
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if input_tokens or output_tokens:
            self.budget.record_text(self.model, category, input_tokens, output_tokens)

    def _fallback(self, title: str, text: str) -> Summary:
        excerpt = " ".join(text.split())[:3500]
        return Summary(
            title=title,
            text=(
                f"Resumo local sem OpenAI configurado.\n\n"
                f"Documento: {title}\n\n"
                f"Trecho inicial:\n{excerpt}"
            ),
        )
