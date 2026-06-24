import logging
import os
import re
from pathlib import Path

from .budget import DailyBudgetExceeded, OpenAIBudget, OpenAIPrices
from .config import Settings
from .connectors import AListConfig, AListConnector, LocalFolderConnector
from .connectors.ima_tencent import ImaApiError, ImaKnowledgeConnector, ImaTencentConfig, ImaTencentConnector
from .extractors import TextExtractor
from .notifiers import TelegramNotifier, WeComNotifier
from .recency import report_day, row_report_timestamp
from .state import SourceItem, StateStore, StoredSummary
from .summarizer import Summarizer, Summary
from .tts import build_tts

logger = logging.getLogger(__name__)


class ResearchPipeline:
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
        self.connector = self._build_connector(settings)
        self.extractor = TextExtractor()
        self.summarizer = Summarizer(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            language=settings.summary_language,
            max_input_chars=settings.max_input_chars,
            budget=self.budget,
        )
        self.tts = build_tts(settings, budget=self.budget)
        self.telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self.wecom = WeComNotifier(settings.wecom_webhook_url)

    def run_once(self) -> None:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        self.state.set_runtime_value("current_status", "listing")
        try:
            items = self.connector.list_items()
        except ImaApiError as exc:
            if exc.is_quota_error:
                logger.warning("IMA API quota reached; waiting for next scheduled cycle: %s", exc.message)
                self.state.set_runtime_value("last_error", f"IMA quota: {exc.message}")
                self.state.set_runtime_value("current_status", "quota_wait")
                return
            raise
        except Exception:
            logger.exception("failed to list source items; waiting for next scheduled cycle")
            self.state.set_runtime_value("last_error", "failed to list source items; see journal")
            self.state.set_runtime_value("current_status", "list_failed")
            return
        logger.info("found %s candidate items", len(items))
        self.state.set_runtime_value("last_candidate_count", str(len(items)))

        processed = 0
        attempted = 0
        batch_summaries: list[Summary] = []
        batch_items: list[SourceItem] = []
        for item in items:
            if self.state.is_processed(item):
                self._cleanup_source_item(item)
                continue
            if self.settings.process_limit_per_run > 0 and attempted >= self.settings.process_limit_per_run:
                logger.info("process limit reached; remaining new items will be handled in later cycles")
                self._cleanup_source_item(item)
                continue
            attempted += 1
            logger.info("processing %s", item.title)
            self.state.set_runtime_value("current_status", f"processing: {item.title[:160]}")
            try:
                logger.info("extracting text from %s", item.title)
                text = self.extractor.extract(item.path)
                logger.info("extracted %s chars from %s", len(text), item.title)
                logger.info("summarizing %s", item.title)
                try:
                    summary = self.summarizer.summarize(item.title, text)
                except DailyBudgetExceeded as exc:
                    logger.warning("%s", exc)
                    self._notify_budget_stop(str(exc))
                    return
                source_note = _source_note([item])
                message = f"[Research] {summary.title}\n\n{source_note}\n\n{summary.text}"

                if self.settings.send_text_updates:
                    self._send_text_update(message)

                audio_path = None
                if not self.settings.digest_mode:
                    spoken_text = _prepend_spoken_source_note(summary.text, [item])
                    try:
                        audio_path = self.tts.synthesize(spoken_text, _safe_name(item.title))
                    except DailyBudgetExceeded as exc:
                        logger.warning("%s", exc)
                        self._notify_budget_stop(str(exc))
                if audio_path:
                    self._send_audio_update(audio_path, caption=_audio_caption(summary.title, [item]))

                self.state.record_summary(item, summary.text)
                self.state.mark_processed(item)
                batch_summaries.append(summary)
                batch_items.append(item)
                processed += 1
            except DailyBudgetExceeded:
                raise
            except Exception as exc:
                logger.exception("failed to process %s; continuing with next item", item.title)
                self.state.set_runtime_value(
                    "last_error",
                    f"failed to process {item.title[:120]}: {type(exc).__name__}: {exc}",
                )
            finally:
                self._cleanup_source_item(item)

        logger.info("processed %s new items", processed)
        self.state.set_runtime_value("last_processed_count", str(processed))
        self.state.set_runtime_value("current_status", "idle")
        if self.settings.batch_insights_enabled and batch_summaries:
            try:
                self._send_batch_insights(batch_summaries, batch_items)
            except Exception as exc:
                logger.exception("batch insight failed after processing items")
                self.state.set_runtime_value(
                    "last_error",
                    f"batch insight failed: {type(exc).__name__}: {exc}",
                )

    def _send_batch_insights(self, batch_summaries: list[Summary], batch_items: list[SourceItem]) -> None:
        context_scan_limit = int(
            os.getenv(
                "DIGEST_CONTEXT_SCAN_LIMIT",
                str(max(self.settings.recent_memory_limit * 5, 100)),
            )
        )
        stored_context = self.state.recent_summaries(context_scan_limit)
        recent_context = _select_cross_market_context(
            batch_summaries,
            stored_context,
            limit=self.settings.recent_memory_limit,
        )
        logger.info(
            "selected %s cross-market context summaries from %s stored summaries",
            len(recent_context),
            len(stored_context),
        )
        try:
            if self.settings.digest_mode:
                insight = self.summarizer.build_digest(batch_summaries, recent_context)
                title = "Hourly research digest"
                audio_name = "hourly_research_digest"
            else:
                insight = self.summarizer.connect_batch(batch_summaries, recent_context)
                title = "Radar incremental"
                audio_name = "radar_incremental"
        except DailyBudgetExceeded as exc:
            logger.warning("%s", exc)
            self._notify_budget_stop(str(exc))
            return
        if not insight.strip():
            return
        source_note = _source_note(batch_items)
        message = f"[{title}]\n\n{source_note}\n\n{insight}"
        self._store_last_digest(message)
        if self.settings.send_text_updates:
            self._send_text_update(message)
        spoken_text = _prepend_spoken_source_note(insight, batch_items)
        try:
            audio_path = self.tts.synthesize(spoken_text, audio_name)
        except DailyBudgetExceeded as exc:
            logger.warning("%s", exc)
            self._notify_budget_stop(str(exc))
            audio_path = None
        if audio_path:
            caption = _audio_caption(title, batch_items)
            self._store_last_audio(audio_path, caption)
            self._send_audio_update(audio_path, caption=caption)

    def send_manual_digest(self) -> bool:
        context_scan_limit = int(
            os.getenv(
                "DIGEST_CONTEXT_SCAN_LIMIT",
                str(max(self.settings.recent_memory_limit * 5, 100)),
            )
        )
        focus_limit = int(os.getenv("MANUAL_DIGEST_FOCUS_LIMIT", "10"))
        stored = self.state.recent_summaries(context_scan_limit)
        if not stored:
            self.telegram.send_text("Ainda nao tem resumos salvos para montar um radar.")
            return False

        focus_items = stored[: max(1, focus_limit)]
        focus = [Summary(title=item.title, text=item.summary) for item in focus_items]
        context = _select_cross_market_context(
            focus,
            stored[focus_limit:],
            limit=self.settings.recent_memory_limit,
        )
        logger.info(
            "manual digest selected %s focus summaries and %s context summaries from %s stored summaries",
            len(focus),
            len(context),
            len(stored),
        )
        try:
            insight = self.summarizer.build_digest(focus, context)
        except DailyBudgetExceeded as exc:
            logger.warning("%s", exc)
            self._notify_budget_stop(str(exc))
            return False
        if not insight.strip():
            return False

        title = "Manual research digest"
        message = f"[{title}]\n\n{insight}"
        self._store_last_digest(message)
        if self.settings.send_text_updates:
            self._send_text_update(message)

        spoken_text = "Manual radar from recent research memory. Now the summary.\n\n" + insight
        try:
            audio_path = self.tts.synthesize(spoken_text, "manual_research_digest")
        except DailyBudgetExceeded as exc:
            logger.warning("%s", exc)
            self._notify_budget_stop(str(exc))
            audio_path = None
        if audio_path:
            self._store_last_audio(audio_path, title)
            self._send_audio_update(audio_path, caption=title)
        return True

    def resend_last_text(self) -> bool:
        text = self.state.runtime_value("last_digest_text")
        if not text:
            self._send_text_update("Ainda nao tem digest salvo para reenviar.")
            return False
        self._send_text_update(text)
        return True

    def resend_last_audio(self) -> bool:
        raw_path = self.state.runtime_value("last_audio_path")
        if not raw_path:
            self._send_text_update("Ainda nao tem audio salvo para reenviar.")
            return False
        path = os.path.abspath(raw_path)
        if not os.path.exists(path):
            self._send_text_update("O ultimo audio salvo nao existe mais no disco.")
            return False
        self._send_audio_update(
            Path(path),
            caption=self.state.runtime_value("last_audio_caption") or "Research digest",
        )
        return True

    def budget_status_text(self) -> str:
        if not self.budget.enabled:
            return "OpenAI budget: sem limite diario configurado."
        spent = self.budget.spent_today()
        remaining = self.budget.remaining_today()
        return (
            "OpenAI budget\n"
            f"Data UTC: {self.budget.usage_date()}\n"
            f"Limite: ${self.settings.openai_daily_budget_usd:.2f}\n"
            f"Gasto estimado: ${spent:.4f}\n"
            f"Restante estimado: ${remaining:.4f}"
        )

    def _notify_budget_stop(self, detail: str) -> None:
        message = (
            "[OpenAI budget]\n\n"
            f"Daily limit reached: ${self.settings.openai_daily_budget_usd:.2f} UTC day.\n"
            f"Spent estimate today: ${self.budget.spent_today():.4f}.\n"
            "The bot will keep running and will try again on the next cycle/day.\n\n"
            f"{detail}"
        )
        self._send_text_update(message)

    def _store_last_digest(self, text: str) -> None:
        self.state.set_runtime_value("last_digest_text", text)

    def _store_last_audio(self, path: Path, caption: str) -> None:
        self.state.set_runtime_value("last_audio_path", str(path))
        self.state.set_runtime_value("last_audio_caption", caption)

    def _send_text_update(self, message: str) -> None:
        for name, notifier in (("telegram", self.telegram), ("wecom", self.wecom)):
            try:
                notifier.send_text(message)
            except Exception as exc:
                logger.warning("%s text notification failed: %s", name, exc)
                self.state.set_runtime_value(
                    "last_notification_error",
                    f"{name} text: {type(exc).__name__}: {exc}",
                )

    def _send_audio_update(self, path: Path, caption: str) -> None:
        try:
            self.telegram.send_audio(path, caption=caption)
        except Exception as exc:
            logger.warning("telegram audio notification failed: %s", exc)
            self.state.set_runtime_value(
                "last_notification_error",
                f"telegram audio: {type(exc).__name__}: {exc}",
            )

    def _cleanup_source_item(self, item: SourceItem) -> None:
        if item.source_id.startswith(("ima:", "alist:")):
            try:
                os.unlink(item.path)
            except FileNotFoundError:
                pass

    def _build_connector(self, settings: Settings):
        if settings.ima_client_id and settings.ima_api_key and settings.ima_knowledge_base_id:
            logger.info("using IMA knowledge base as source")
            return ImaKnowledgeConnector(
                client=ImaTencentConnector(
                    ImaTencentConfig(
                        base_url=settings.ima_base_url,
                        client_id=settings.ima_client_id,
                        api_key=settings.ima_api_key,
                    )
                ),
                knowledge_base_id=settings.ima_knowledge_base_id,
                cache_dir=settings.output_dir / "ima-cache",
                state_store=self.state,
            )
        if settings.alist_base_url and settings.alist_path and (
            settings.alist_token or settings.alist_username
        ):
            logger.info("using AList as source")
            return AListConnector(
                AListConfig(
                    base_url=settings.alist_base_url,
                    username=settings.alist_username,
                    password=settings.alist_password,
                    token=settings.alist_token,
                    path=settings.alist_path,
                ),
                state_store=self.state,
            )
        logger.warning("IMA is not fully configured; falling back to local WATCH_DIR")
        return LocalFolderConnector(settings.watch_dir)


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value[:120].strip("._") or "summary"


def _source_note(items: list[SourceItem]) -> str:
    dates = sorted({day for item in items for day in [_item_report_day(item)] if day})
    source_names = sorted({_source_name(item) for item in items})
    lines = [
        "Data dos relatorios: " + (", ".join(dates) if dates else "nao detectada"),
        f"Documentos cruzados: {len(items)}",
        "Fonte: " + ", ".join(source_names),
    ]
    for idx, item in enumerate(items[:12], start=1):
        lines.append(f"{idx}. {item.title} ({_item_report_day(item) or 'data nao detectada'})")
    if len(items) > 12:
        lines.append(f"... mais {len(items) - 12} documentos")
    return "\n".join(lines)


def _prepend_spoken_source_note(text: str, items: list[SourceItem]) -> str:
    item_days = [_item_report_day(item) for item in items]
    dates = sorted({day for day in item_days if day})
    total = len(items)
    undated_count = sum(1 for day in item_days if not day)
    if dates and len(dates) == 1 and undated_count == 0:
        date_check = f"Date check: all {total} reports are from {dates[0]}."
    elif dates and len(dates) == 1:
        plural = "items" if undated_count != 1 else "item"
        date_check = f"Date check: detected reports are from {dates[0]}, with {undated_count} undated {plural}."
    elif dates:
        date_check = f"Date check: reports are mixed or partially missing dates; detected dates are {', '.join(dates)}."
    else:
        date_check = "Date check: report dates were not detected."
    intro = (
        f"{date_check} "
        f"Documents crossed: {total}. "
        "Now the summary."
    )
    return f"{intro}\n\n{text}"


def _audio_caption(title: str, items: list[SourceItem]) -> str:
    dates = sorted({day for item in items for day in [_item_report_day(item)] if day})
    date_text = dates[0] if len(dates) == 1 else (", ".join(dates[:2]) if dates else "date n/a")
    return f"{title} - {date_text} - {len(items)} docs"


def _item_report_day(item: SourceItem) -> str:
    row = {"name": item.title, "title": item.title, "path": item.source_id}
    return report_day(row_report_timestamp(row)) or ""


def _source_name(item: SourceItem) -> str:
    if item.source_id.startswith("alist:"):
        return "Quark/AList"
    if item.source_id.startswith("ima:"):
        return "IMA"
    if item.source_id.startswith("local:"):
        return "pasta local"
    return item.source_id.split(":", 1)[0] or "desconhecida"


def _select_cross_market_context(
    batch_summaries: list[Summary],
    stored_context: list[StoredSummary],
    limit: int,
) -> list[Summary]:
    if limit <= 0:
        return []

    batch_titles = {item.title for item in batch_summaries}
    batch_markets = [_market_bucket(item.title) for item in batch_summaries]
    batch_primary = _dominant_bucket(batch_markets)

    candidates = [
        item
        for item in stored_context
        if item.title not in batch_titles
    ]
    candidates.sort(
        key=lambda item: (
            _context_priority(batch_primary, _market_bucket(item.title)),
            _item_report_day_from_title(item.title) or "",
        ),
        reverse=True,
    )
    selected = candidates[:limit]
    return [Summary(title=item.title, text=item.summary) for item in selected]


def _dominant_bucket(buckets: list[str]) -> str:
    if not buckets:
        return "unknown"
    return max(set(buckets), key=buckets.count)


def _context_priority(batch_primary: str, candidate_bucket: str) -> int:
    if batch_primary == "china":
        order = {"international": 4, "global": 3, "other": 2, "china": 1, "unknown": 0}
    elif batch_primary in {"international", "global"}:
        order = {"china": 4, "global": 3, "other": 2, "international": 1, "unknown": 0}
    else:
        order = {"international": 4, "china": 3, "global": 2, "other": 1, "unknown": 0}
    return order.get(candidate_bucket, 0)


def _market_bucket(title: str) -> str:
    value = title.lower()
    if re.search(r"[\u4e00-\u9fff]", title):
        return "china"
    if re.search(
        r"\b(global|world|international|overseas|macro|fed|fomc|treasury|dollar|"
        r"nasdaq|s&p|europe|euro|japan|korea|taiwan|oil|gold|copper|crypto|"
        r"coinbase|nvidia|oracle)\b",
        value,
    ):
        return "international"
    if re.search(r"\b(us|u\.s\.|usa|uk|eu)\b", value):
        return "international"
    if re.search(r"[a-z]", value):
        return "global"
    return "other"


def _item_report_day_from_title(title: str) -> str:
    row = {"name": title, "title": title, "path": title}
    return report_day(row_report_timestamp(row)) or ""
