FROM python:3.13-slim

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Phase 17: the interpreter matches the verified environment (Python 3.13)
# and torch is pinned to the CPU wheel so the image never pulls a CUDA build.
# requirements.txt is fully pinned — a rebuild installs the same versions.
COPY requirements.txt .
RUN pip install --no-cache-dir "torch==2.14.0+cpu" --extra-index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /home/appuser/.cache/huggingface && \
    chown -R appuser:appuser /home/appuser/.cache && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
