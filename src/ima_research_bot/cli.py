import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Settings
from .connectors.ima_tencent import ImaTencentConfig, ImaTencentConnector
from .pipeline import ResearchPipeline
from .radar import ImaRadar


@dataclass
class RuntimeState:
    running: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: str = ""
    pending_radar: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(prog="ima-research-bot")
    parser.add_argument(
        "command",
        choices=["run-once", "serve", "digest", "radar", "ima-list-kb", "ima-list-knowledge"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings.from_env()

    if args.command in {"ima-list-kb", "ima-list-knowledge"}:
        ima = ImaTencentConnector(
            ImaTencentConfig(
                base_url=settings.ima_base_url,
                client_id=settings.ima_client_id,
                api_key=settings.ima_api_key,
            )
        )
        if args.command == "ima-list-kb":
            print(json.dumps(ima.search_knowledge_bases(""), ensure_ascii=False, indent=2))
            return
        if not settings.ima_knowledge_base_id:
            raise SystemExit("Set IMA_KNOWLEDGE_BASE_ID in .env before ima-list-knowledge")
        print(json.dumps(ima.list_knowledge(settings.ima_knowledge_base_id), ensure_ascii=False, indent=2))
        return

    if args.command == "radar":
        print(ImaRadar(settings).run())
        return

    pipeline = ResearchPipeline(settings)
    pipeline_lock = threading.Lock()
    runtime = RuntimeState()

    def locked_run_once() -> None:
        if not pipeline_lock.acquire(blocking=False):
            logging.info("pipeline already running; skipping overlapping run")
            return
        try:
            runtime.running = True
            runtime.started_at = _now_iso()
            runtime.last_error = ""
            pipeline.run_once()
        except Exception as exc:
            runtime.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            runtime.running = False
            runtime.finished_at = _now_iso()
            pipeline_lock.release()
        if runtime.pending_radar:
            runtime.pending_radar = False
            _run_manual_digest(pipeline, pipeline_lock, runtime)

    if args.command == "run-once":
        locked_run_once()
        return

    if args.command == "digest":
        pipeline.send_manual_digest()
        return

    collector_enabled = os.getenv("COLLECTOR_ENABLED", "1") != "0"
    if not collector_enabled:
        logging.warning("collector disabled by COLLECTOR_ENABLED=0; Telegram chat commands remain available")

    scheduler = BackgroundScheduler(timezone="UTC")
    if collector_enabled:
        scheduler.add_job(
            locked_run_once,
            "interval",
            minutes=settings.poll_interval_minutes,
            next_run_time=None,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    logging.info("scheduler started; interval=%s minutes", settings.poll_interval_minutes)
    if os.getenv("TELEGRAM_COMMANDS_ENABLED", "1") != "0":
        try:
            pipeline.telegram.delete_webhook()
            logging.info("telegram webhook cleared for polling command loop")
        except Exception as exc:
            logging.warning("failed to clear telegram webhook before polling: %s", exc)
        threading.Thread(
            target=_telegram_command_loop,
            args=(pipeline, pipeline_lock, runtime),
            daemon=True,
        ).start()
        logging.info("telegram command loop started")
    if collector_enabled:
        locked_run_once()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()


def _telegram_command_loop(
    pipeline: ResearchPipeline,
    pipeline_lock: threading.Lock,
    runtime: RuntimeState,
) -> None:
    offset = 0
    allowed_chat_id = str(pipeline.settings.telegram_chat_id)
    if not pipeline.telegram.enabled:
        logging.warning("telegram command loop disabled because TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return
    while True:
        try:
            updates = pipeline.telegram.get_updates(offset=offset, timeout=30)
            if updates:
                logging.info("telegram command loop received %s update(s)", len(updates))
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id") or "")
                if allowed_chat_id and chat_id != allowed_chat_id:
                    logging.warning("ignoring telegram command from chat_id=%s; allowed_chat_id=%s", chat_id, allowed_chat_id)
                    continue
                text = str(message.get("text") or "").strip().lower()
                if not text:
                    continue
                logging.info("handling telegram command %s from chat_id=%s", text.split()[0], chat_id)
                _handle_telegram_command(text, pipeline, pipeline_lock, runtime)
        except Exception as exc:
            runtime.last_error = f"{type(exc).__name__}: {exc}"
            logging.exception("telegram command loop failed; retrying")
            time.sleep(10)


def _handle_telegram_command(
    text: str,
    pipeline: ResearchPipeline,
    pipeline_lock: threading.Lock,
    runtime: RuntimeState,
) -> None:
    command = text.split()[0].split("@", 1)[0]
    if command in {"/start", "start", "/help", "help"}:
        pipeline.telegram.send_text(
            "Bot online. Comandos: /ask, /status, /run, /radar, /text, /audio, /budget."
        )
        return
    if command in {"/ask", "ask", "/chat", "chat"}:
        question = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        _answer_telegram_question(question, pipeline, runtime)
        return
    if command in {"/status", "status"}:
        state = "rodando" if runtime.running else "ocioso"
        persisted_status = pipeline.state.runtime_value("current_status") or state
        detail = [
            f"Status: {state}.",
            f"Estado interno: {persisted_status}.",
            f"Inicio do ciclo: {runtime.started_at or 'n/a'}.",
            f"Fim do ultimo ciclo: {runtime.finished_at or 'n/a'}.",
            f"Radar pendente: {'sim' if runtime.pending_radar else 'nao'}.",
            f"Candidatos no ultimo ciclo: {pipeline.state.runtime_value('last_candidate_count') or 'n/a'}.",
            f"Processados no ultimo ciclo: {pipeline.state.runtime_value('last_processed_count') or 'n/a'}.",
            f"Gasto OpenAI hoje: ${pipeline.budget.spent_today():.4f}.",
        ]
        last_error = runtime.last_error or pipeline.state.runtime_value("last_error") or ""
        if last_error:
            detail.append(f"Ultimo erro: {last_error[:500]}.")
        detail.append("Comandos: /run, /radar, /text, /audio, /budget, /status.")
        pipeline.telegram.send_text("\n".join(detail))
        return
    if command in {"/run", "run", "/now", "now"}:
        if os.getenv("COLLECTOR_ENABLED", "1") == "0":
            pipeline.telegram.send_text("Coleta IMA esta pausada por COLLECTOR_ENABLED=0. O chat continua online.")
            return
        if not pipeline_lock.acquire(blocking=False):
            pipeline.telegram.send_text("Ja tem um ciclo rodando. Vou ignorar /run para nao sobrepor.")
            return
        try:
            runtime.running = True
            runtime.started_at = _now_iso()
            runtime.last_error = ""
            pipeline.telegram.send_text("Rodando coleta agora.")
            pipeline.run_once()
            pipeline.telegram.send_text("Coleta manual terminou.")
        except Exception as exc:
            runtime.last_error = f"{type(exc).__name__}: {exc}"
            pipeline.telegram.send_text(f"Coleta manual falhou: {runtime.last_error[:500]}")
            raise
        finally:
            runtime.running = False
            runtime.finished_at = _now_iso()
            pipeline_lock.release()
        if runtime.pending_radar:
            runtime.pending_radar = False
            _run_manual_digest(pipeline, pipeline_lock, runtime)
        return
    if command in {"/radar", "radar", "/digest", "digest"}:
        if not pipeline_lock.acquire(blocking=False):
            runtime.pending_radar = True
            pipeline.telegram.send_text("Ciclo rodando. Deixei o /radar na fila e mando assim que terminar.")
            return
        try:
            _run_manual_digest_locked(pipeline, runtime)
        finally:
            pipeline_lock.release()
        return
    if command in {"/text", "text"}:
        pipeline.resend_last_text()
        return
    if command in {"/audio", "audio"}:
        pipeline.resend_last_audio()
        return
    if command in {"/budget", "budget"}:
        pipeline.telegram.send_text(pipeline.budget_status_text())
        return
    if not command.startswith("/"):
        _answer_telegram_question(text, pipeline, runtime)
        return
    pipeline.telegram.send_text(
        "Comando recebido. Use /ask, /run, /radar, /text, /audio, /budget ou /status."
    )


def _answer_telegram_question(
    question: str,
    pipeline: ResearchPipeline,
    runtime: RuntimeState,
) -> None:
    try:
        pipeline.telegram.send_text("Pensando com a memoria recente...")
        answer = pipeline.answer_chat_question(question)
        pipeline.telegram.send_text(answer or "Nao consegui montar uma resposta agora.")
    except Exception as exc:
        runtime.last_error = f"{type(exc).__name__}: {exc}"
        pipeline.telegram.send_text(f"Chat falhou: {runtime.last_error[:500]}")
        raise


def _run_manual_digest(
    pipeline: ResearchPipeline,
    pipeline_lock: threading.Lock,
    runtime: RuntimeState,
) -> None:
    if not pipeline_lock.acquire(blocking=False):
        runtime.pending_radar = True
        return
    try:
        _run_manual_digest_locked(pipeline, runtime)
    finally:
        pipeline_lock.release()


def _run_manual_digest_locked(pipeline: ResearchPipeline, runtime: RuntimeState) -> None:
    try:
        runtime.running = True
        runtime.started_at = _now_iso()
        runtime.last_error = ""
        pipeline.telegram.send_text("Montando radar da memoria recente.")
        ok = pipeline.send_manual_digest()
        if not ok:
            pipeline.telegram.send_text("Nao consegui montar radar agora; veja o log do servico.")
    except Exception as exc:
        runtime.last_error = f"{type(exc).__name__}: {exc}"
        pipeline.telegram.send_text(f"Radar falhou: {runtime.last_error[:500]}")
        raise
    finally:
        runtime.running = False
        runtime.finished_at = _now_iso()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
