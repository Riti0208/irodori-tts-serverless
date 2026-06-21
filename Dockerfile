# syntax=docker/dockerfile:1.7
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/hf-cache \
    TRANSFORMERS_CACHE=/app/hf-cache/transformers

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ffmpeg \
        git \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch from the CUDA 12.8 wheel index first so transitive resolution
# downstream picks the GPU build. Pin to 2.11.0 — newer PyPI 2.12 ships cu13
# default wheels and would be picked over the cu128 build under --extra-index-url.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        "torch==2.11.0" "torchaudio==2.11.0"

# Install the upstream server (pulls in Irodori-TTS itself, DACVAE, SilentCipher,
# transformers, etc.) plus the RunPod SDK. The PyPI project name is
# `irodori-tts-server` even though the Python module is `irodori_openai_tts`.
#
# `dacvae` must be specified explicitly here: Irodori-TTS lists it as a plain
# dependency and resolves the git URL via uv's `[tool.uv.sources]` block, which
# pip does not read. Without this pin pip looks for it on PyPI and fails.
RUN pip install --no-cache-dir \
        "dacvae @ git+https://github.com/facebookresearch/dacvae" \
        "irodori-tts-server @ git+https://github.com/Aratako/Irodori-TTS-Server.git" \
        "runpod>=1.7.0"

# Bake all model weights into the image so cold start does not hit HuggingFace.
# - 500M base checkpoint
# - DACVAE codec
# - LLM-jp tokenizer (text encoder embedding init)
# Use snapshot_download with explicit allow_patterns where possible to keep
# the image lean.
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('Aratako/Irodori-TTS-500M-v3', allow_patterns=['model.safetensors','*.json']); \
    snapshot_download('Aratako/Semantic-DACVAE-Japanese-32dim'); \
    snapshot_download('llm-jp/llm-jp-3-150m', allow_patterns=['*.json','*.txt','tokenizer*','spiece*','vocab*'])"

# Materialize a stable checkpoint path for IRODORI_CHECKPOINT (overrideable).
RUN python -c "from huggingface_hub import hf_hub_download; \
    import shutil, os; \
    src = hf_hub_download('Aratako/Irodori-TTS-500M-v3', 'model.safetensors'); \
    os.makedirs('/app/weights', exist_ok=True); \
    shutil.copy(src, '/app/weights/model.safetensors')"

COPY handler.py ./
COPY voices ./voices

# Speaker LoRA adapters (PEFT). Selected per-request via irodori.lora_adapter,
# e.g. {"irodori": {"lora_adapter": "/app/adapters/ema"}}. The upstream server
# caches a loaded adapter; pass "none"/"base"/null to fall back to the base model.
COPY adapters ./adapters

# Inference defaults for serverless GPU workers.
ENV IRODORI_MODEL_DEVICE=cuda \
    IRODORI_CODEC_DEVICE=cuda \
    IRODORI_MODEL_PRECISION=bf16 \
    IRODORI_CODEC_PRECISION=bf16 \
    IRODORI_PRELOAD=true \
    IRODORI_VOICES_DIR=/app/voices \
    IRODORI_ALLOW_NO_REF_VOICE=true \
    IRODORI_CHECKPOINT=/app/weights/model.safetensors

CMD ["python", "-u", "handler.py"]
