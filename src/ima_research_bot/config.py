import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    watch_dir: Path
    state_db: Path
    output_dir: Path
    poll_interval_minutes: int
    process_limit_per_run: int
    recent_memory_limit: int
    batch_insights_enabled: bool
    digest_mode: bool
    openai_api_key: str
    openai_model: str
    openai_daily_budget_usd: float
    openai_text_input_usd_per_1m: float
    openai_text_output_usd_per_1m: float
    openai_tts_usd_per_1k_chars: float
    ima_client_id: str
    ima_api_key: str
    ima_base_url: str
    ima_knowledge_base_id: str
    alist_base_url: str
    alist_username: str
    alist_password: str
    alist_token: str
    alist_path: str
    tts_provider: str
    openai_tts_model: str
    openai_tts_voice: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model_id: str
    telegram_bot_token: str
    telegram_chat_id: str
    wecom_webhook_url: str
    send_text_updates: bool
    summary_language: str
    max_input_chars: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            watch_dir=Path(os.getenv("WATCH_DIR", ".")).expanduser(),
            state_db=Path(os.getenv("STATE_DB", "./state/research_bot.sqlite3")),
            output_dir=Path(os.getenv("OUTPUT_DIR", "./out")),
            poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "15")),
            process_limit_per_run=int(os.getenv("PROCESS_LIMIT_PER_RUN", "3")),
            recent_memory_limit=int(os.getenv("RECENT_MEMORY_LIMIT", "20")),
            batch_insights_enabled=os.getenv("BATCH_INSIGHTS_ENABLED", "1") != "0",
            digest_mode=os.getenv("DIGEST_MODE", "0") != "0",
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            openai_daily_budget_usd=float(os.getenv("OPENAI_DAILY_BUDGET_USD", "2")),
            openai_text_input_usd_per_1m=float(os.getenv("OPENAI_TEXT_INPUT_USD_PER_1M", "0.40")),
            openai_text_output_usd_per_1m=float(os.getenv("OPENAI_TEXT_OUTPUT_USD_PER_1M", "1.60")),
            openai_tts_usd_per_1k_chars=float(os.getenv("OPENAI_TTS_USD_PER_1K_CHARS", "0.03")),
            ima_client_id=os.getenv("IMA_CLIENT_ID", os.getenv("IMA_OPENAPI_CLIENTID", "")),
            ima_api_key=os.getenv("IMA_API_KEY", os.getenv("IMA_OPENAPI_APIKEY", "")),
            ima_base_url=os.getenv("IMA_BASE_URL", "https://ima.qq.com"),
            ima_knowledge_base_id=os.getenv("IMA_KNOWLEDGE_BASE_ID", ""),
            alist_base_url=os.getenv("ALIST_BASE_URL", ""),
            alist_username=os.getenv("ALIST_USERNAME", ""),
            alist_password=os.getenv("ALIST_PASSWORD", ""),
            alist_token=os.getenv("ALIST_TOKEN", ""),
            alist_path=os.getenv("ALIST_PATH", ""),
            tts_provider=os.getenv("TTS_PROVIDER", "elevenlabs"),
            openai_tts_model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            openai_tts_voice=os.getenv("OPENAI_TTS_VOICE", "coral"),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", ""),
            elevenlabs_model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            wecom_webhook_url=os.getenv("WECOM_WEBHOOK_URL", ""),
            send_text_updates=os.getenv("SEND_TEXT_UPDATES", "1") != "0",
            summary_language=os.getenv("SUMMARY_LANGUAGE", "English"),
            max_input_chars=int(os.getenv("MAX_INPUT_CHARS", "45000")),
        )
