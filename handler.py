"""RunPod Serverless worker for Irodori-TTS-Server.

Wraps the upstream FastAPI app via Starlette TestClient so we reuse all of the
OpenAI-compatible request handling, voice resolution, chunking, and audio
encoding without re-implementing it.

Input shape (event["input"]):
    {
        "model": "irodori-tts",            # optional, default
        "input": "テキスト",                  # required
        "voice": "none" | "<file-stem>",    # optional, see ref_wav_b64 below
        "response_format": "wav"|"mp3"|...,  # optional, default wav
        "speed": 1.0,                       # optional, 0.25..4.0
        "irodori": { ... },                 # optional, model hyperparams
        # Helpers added on top of the upstream API:
        "ref_wav_b64": "<base64>",           # optional, raw audio bytes
        "ref_wav_url":  "https://...",       # optional, fetched at request time
    }

Output shape:
    success: {"audio_b64": "...", "format": "wav", "sample_rate": 48000,
              "seed": 1234, "headers": {"X-Irodori-Total-To-Decode": "..."}}
    error:   {"error": {...}, "status_code": 4xx|5xx}
"""
from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("handler")

# Configure the upstream server BEFORE importing the app so settings cache picks
# these up. cuda + bf16 keep the 500M model comfortable on a 24GB GPU.
os.environ.setdefault("IRODORI_MODEL_DEVICE", "cuda")
os.environ.setdefault("IRODORI_CODEC_DEVICE", "cuda")
os.environ.setdefault("IRODORI_MODEL_PRECISION", "bf16")
os.environ.setdefault("IRODORI_CODEC_PRECISION", "bf16")
os.environ.setdefault("IRODORI_PRELOAD", "true")
os.environ.setdefault("IRODORI_VOICES_DIR", "/app/voices")
# Allow text-only synthesis when caller passes voice="none"
os.environ.setdefault("IRODORI_ALLOW_NO_REF_VOICE", "true")

# If the model checkpoint was baked into the image, use it directly.
_local_ckpt = Path("/app/weights/model.safetensors")
if _local_ckpt.is_file() and "IRODORI_CHECKPOINT" not in os.environ:
    os.environ["IRODORI_CHECKPOINT"] = str(_local_ckpt)
    log.info("using baked-in checkpoint: %s", _local_ckpt)

import runpod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from irodori_openai_tts.app import app  # noqa: E402

CLIENT = TestClient(app)

# Trigger the lifespan startup hook (model preload if IRODORI_PRELOAD=true).
# TestClient enters the lifespan context lazily on first request, so a /health
# call here forces preload during cold start instead of on first user request.
_warmup_resp = CLIENT.get("/health")
log.info("warmup /health -> %s", _warmup_resp.status_code)


def _materialize_ref_wav(inp: dict[str, Any]) -> str | None:
    """If the caller supplied ref_wav_b64 or ref_wav_url, write to a temp file
    and return the path. Otherwise return None.
    """
    if b64 := inp.pop("ref_wav_b64", None):
        fd, path = tempfile.mkstemp(suffix=".wav", dir="/tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(b64))
        log.info("ref_wav_b64 materialized at %s (%d bytes)", path, os.path.getsize(path))
        return path
    if url := inp.pop("ref_wav_url", None):
        fd, path = tempfile.mkstemp(suffix=".wav", dir="/tmp")
        with os.fdopen(fd, "wb") as out:
            with urllib.request.urlopen(url, timeout=30) as resp:
                out.write(resp.read())
        log.info("ref_wav_url fetched -> %s (%d bytes)", path, os.path.getsize(path))
        return path
    return None


def handler(event: dict[str, Any]) -> dict[str, Any]:
    inp = event.get("input")
    if not isinstance(inp, dict):
        return {"error": {"message": "event.input must be an object"}, "status_code": 400}
    if not inp.get("input"):
        return {"error": {"message": "field 'input' (text) is required"}, "status_code": 400}

    # Copy so we can pop helper-only keys without mutating the caller's payload
    body = dict(inp)
    ref_wav_path = _materialize_ref_wav(body)
    if ref_wav_path is not None:
        irodori_opts = dict(body.get("irodori") or {})
        irodori_opts["ref_wav"] = ref_wav_path
        body["irodori"] = irodori_opts
        # Caller did not need to specify voice; the upstream resolver accepts
        # ref_wav directly in the irodori options.
        body.setdefault("voice", "none")

    body.setdefault("model", os.environ.get("IRODORI_MODEL_NAME", "irodori-tts"))
    body.setdefault("response_format", "wav")
    body.setdefault("voice", "none")

    try:
        resp = CLIENT.post("/v1/audio/speech", json=body)
    except Exception as exc:  # noqa: BLE001 — surface as JSON
        log.exception("synthesis failed")
        return {"error": {"message": f"upstream exception: {exc!r}"}, "status_code": 500}

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = {"detail": resp.text}
        return {"error": detail, "status_code": resp.status_code}

    audio_b64 = base64.b64encode(resp.content).decode("ascii")
    return {
        "audio_b64": audio_b64,
        "format": body["response_format"],
        "seed": resp.headers.get("X-Irodori-Seed"),
        "total_to_decode": resp.headers.get("X-Irodori-Total-To-Decode"),
        "bytes": len(resp.content),
    }


if __name__ == "__main__":
    if "--local-test" in sys.argv:
        # Smoke test without runpod runtime
        test_event = {
            "input": {
                "input": "こんにちは、これはローカルテストです。",
                "voice": "none",
                "response_format": "wav",
                "irodori": {"num_steps": 16, "seed": 42},
            }
        }
        result = handler(test_event)
        out = result.get("audio_b64", "")
        log.info("local-test result: %d bytes audio, seed=%s", len(out) * 3 // 4, result.get("seed"))
        sys.exit(0 if result.get("audio_b64") else 1)
    runpod.serverless.start({"handler": handler})
