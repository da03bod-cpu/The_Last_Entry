# Official RunPod PyTorch base image. Base model and LoRA adapter are loaded from
# RunPod Cached Models / Network Volume / Hugging Face at runtime (no secrets or large weights in image).
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app
USER root

ENV PYTHONUNBUFFERED=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    HF_HOME=/tmp/hf

# Install Tesseract OCR with Arabic and English language packs for scanned PDFs
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-ara \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Match the training environment on the Pod: torch 2.8.0 + CUDA 12.8 (with triton 3.4.0 for bitsandbytes 0.50.2)
RUN python -m pip install --no-cache-dir torch==2.8.0 --index-url https://download.pytorch.org/whl/cu128

# Remove incompatible base-image torchvision/torchaudio built for torch 2.4
RUN python -m pip uninstall -y torchvision torchaudio

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY config.py ingestion.py postprocess.py handler.py ./

CMD ["python", "-u", "handler.py"]
