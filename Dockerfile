# صورة RunPod الرسمية. الـLoRA والموديل الأساسي بيتحمّلوا من Hugging Face وقت التشغيل
# (مفيش أي ملف كبير في ريبو GitHub ولا في الـimage).
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app
USER root

ENV PYTHONUNBUFFERED=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    HF_HOME=/tmp/hf

# نفس بيئة التدريب الناجحة على الـPod: torch 2.8.0 + CUDA 12.8 (فيها triton 3.4.0 اللي bitsandbytes 0.50.2 اشتغل معاه).
RUN python -m pip install --no-cache-dir torch==2.8.0 --index-url https://download.pytorch.org/whl/cu128

# الصورة الأساسية جاية بـtorchvision/torchaudio مبنيين لـtorch 2.4، ومش متوافقين مع 2.8.0
# (كانوا بيسببوا: operator torchvision::nms does not exist). الموديل نصي ومش محتاجهم، فبنشيلهم.
RUN python -m pip uninstall -y torchvision torchaudio

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY config.py postprocess.py handler.py ./

CMD ["python", "-u", "handler.py"]
