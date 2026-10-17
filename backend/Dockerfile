FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=prod ALLOW_DEMO=0
WORKDIR /app
# Local OCR for scanned remittance advices: nothing leaves the container. Drop this line and requirements-ocr.txt for a smaller image (scans are then refused with a clear message).
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-ocr.txt
COPY app app
COPY alembic.ini .
COPY migrations migrations
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && mkdir -p /data/exports /data/feeds && chown -R app /data
USER 10001
EXPOSE 8000
# API:    uvicorn app.main:app --host 0.0.0.0 --port 8000      (default)
# Worker: python -m app.cli worker                              (separate Deployment: FX fetch, inbox poll, bank sync, matching)
# Init:   python -m app.cli migrate                             (run once per release as a Job / init container; serialised by a PG advisory lock)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
