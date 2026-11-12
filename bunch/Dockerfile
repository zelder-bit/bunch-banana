FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# git: room folders of the git kind are bare repositories on the hub (bunch/folders.py).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bunch ./bunch

# Run as an unprivileged user; /data holds the SQLite database (mounted as a volume).
RUN useradd --system --uid 10001 --no-create-home bunch && mkdir -p /data && chown bunch /data
USER bunch
ENV BUNCH_DB=/data/bunch.db

EXPOSE 8700
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=3)"

CMD ["uvicorn", "bunch.main:app", "--host", "0.0.0.0", "--port", "8700", "--proxy-headers", "--no-server-header"]
