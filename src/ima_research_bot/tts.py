from pathlib import Path
from typing import Optional

from openai import OpenAI
import requests

from .budget import OpenAIBudget


class OpenAITTS:
    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        output_dir: Path,
        budget: Optional[OpenAIBudget] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.output_dir = output_dir
        self.budget = budget
        self.client = OpenAI(api_key=api_key) if api_key else None

    @property
    def enabled(self) -> bool:
        return bool(self.client and self.model and self.voice)

    def synthesize(self, text: str, basename: str) -> Optional[Path]:
        if not self.enabled:
            return None
        text = text[:9000]
        if self.budget:
            self.budget.ensure_available(self.budget.estimate_tts(len(text)), "text-to-speech")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"{basename}.mp3"
        response = self.client.audio.speech.create(
            model=self.model,
            voice=self.voice,
            input=text,
            instructions="Speak clearly in a professional financial research briefing style.",
        )
        out_path.write_bytes(response.read())
        if self.budget:
            self.budget.record_tts_estimate(self.model, len(text))
        return out_path


class ElevenLabsTTS:
    def __init__(self, api_key: str, voice_id: str, model_id: str, output_dir: Path) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_dir = output_dir

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.voice_id)

    def synthesize(self, text: str, basename: str) -> Optional[Path]:
        if not self.enabled:
            return None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"{basename}.mp3"
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        payload = {
            "text": text[:9000],
            "model_id": self.model_id,
        }
        response = requests.post(
            url,
            headers={
                "xi-api-key": self.api_key,
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        out_path.write_bytes(response.content)
        return out_path


def build_tts(settings, budget: Optional[OpenAIBudget] = None):
    provider = settings.tts_provider.strip().lower()
    if provider == "openai":
        return OpenAITTS(
            api_key=settings.openai_api_key,
            model=settings.openai_tts_model,
            voice=settings.openai_tts_voice,
            output_dir=settings.output_dir,
            budget=budget,
        )
    return ElevenLabsTTS(
        api_key=settings.elevenlabs_api_key,
        voice_id=settings.elevenlabs_voice_id,
        model_id=settings.elevenlabs_model_id,
        output_dir=settings.output_dir,
    )
