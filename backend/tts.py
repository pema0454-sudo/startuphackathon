"""
tts.py — Nepali voice confirmation via Azure TTS (spec §11 / §11B).

On claim approval we generate a REAL audio file (Azure Cognitive Services
Speech, ne-NP voice) — that part is genuinely real, not simulated. What is
simulated is *delivery*: nothing is dialled or texted to an actual phone.
We save the audio + a short SMS-length text to the claim record and mark
it "Sandbox Dispatch" — the same honest, product-grade labelling pattern
as a payment platform's "Test Mode" (never claimed as a real send).

If AZURE_TTS_KEY isn't set, or the call fails, we degrade to text-only:
the SMS text still gets generated (it's just Python string formatting) and
the claim is still marked Confirmed, but voice_file stays None and the
trace/UI say so plainly. The approval flow never blocks on TTS.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import requests

AZURE_TTS_KEY = os.getenv("AZURE_TTS_KEY", "").strip()
AZURE_TTS_REGION = os.getenv("AZURE_TTS_REGION", "centralindia").strip()
NEPALI_VOICE = os.getenv("AZURE_TTS_VOICE", "ne-NP-HemkalaNeural")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
AUDIO_DIR = FRONTEND_DIR / "static" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_URL = f"https://{AZURE_TTS_REGION}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
TTS_URL = f"https://{AZURE_TTS_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return phone or "unknown"
    return phone[: -6] + "••••••" + digits[-2:]


def build_sms_text(farmer_name: str, claim_id, payout: float, insurer_name: str = "Bema Farm") -> str:
    first = (farmer_name or "Kisan").split(" ")[0]
    text = f"{insurer_name}: {first}, timro claim #{claim_id} file bhayo. NPR {payout:,.0f} payout review hudai cha."
    return text[:150]


def _ssml(text_ne: str) -> str:
    return (
        '<speak version="1.0" xml:lang="ne-NP">'
        f'<voice xml:lang="ne-NP" xml:gender="Female" name="{NEPALI_VOICE}">{text_ne}</voice>'
        "</speak>"
    )


def generate_voice(claim_id, farmer_name: str, payout: float) -> dict:
    """Returns {"voice_file": "/static/audio/xxx.mp3" | None, "engine": "azure-tts" | "unavailable",
    "sms_text": str}."""
    sms_text = build_sms_text(farmer_name, claim_id, payout)
    ne_line = (
        f"नमस्ते {farmer_name}, तपाईंको बिमा दाबी नम्बर {claim_id} दर्ता भयो। "
        f"रु {payout:,.0f} भुक्तानी समीक्षामा छ। धन्यवाद, बेमा फार्म।"
    )

    if not AZURE_TTS_KEY:
        return {"voice_file": None, "engine": "unavailable", "sms_text": sms_text,
                "note": "AZURE_TTS_KEY not set — text-only fallback"}

    try:
        token_resp = requests.post(
            TOKEN_URL, headers={"Ocp-Apim-Subscription-Key": AZURE_TTS_KEY}, timeout=10
        )
        token_resp.raise_for_status()
        token = token_resp.text

        audio_resp = requests.post(
            TTS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-16khz-64kbitrate-mono-mp3",
                "User-Agent": "bema-farm",
            },
            data=_ssml(ne_line).encode("utf-8"),
            timeout=15,
        )
        audio_resp.raise_for_status()

        filename = f"claim-{claim_id}-{uuid.uuid4().hex[:8]}.mp3"
        path = AUDIO_DIR / filename
        with open(path, "wb") as f:
            f.write(audio_resp.content)

        return {"voice_file": f"/static/audio/{filename}", "engine": "azure-tts", "sms_text": sms_text}
    except Exception as e:  # noqa: BLE001
        return {
            "voice_file": None,
            "engine": "unavailable",
            "sms_text": sms_text,
            "note": f"Azure TTS call failed: {e}",
        }
