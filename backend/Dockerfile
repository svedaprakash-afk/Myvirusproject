FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=prod ALLOW_DEMO=0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && mkdir -p /data/exports /data/feeds && chown -R app /data
USER 10001
EXPOSE 8000
# API:    uvicorn app.main:app --host 0.0.0.0 --port 8000      (default)
# Worker: python -m app.cli worker                              (separate Deployment: FX fetch, inbox poll, bank sync, matching)
# Init:   python -m app.cli init-db                             (run once per release as a Job / init container)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
