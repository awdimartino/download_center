FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DC_CONFIG_DIR=/config \
    DC_OUTPUT_DIR=/downloads

# ffmpeg does the MP3 encoding; gosu drops from root to the mapped user once
# the volumes have been prepared.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copied on its own so the dependency layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && groupadd -g 1000 downloader \
 && useradd -u 1000 -g downloader -d /app -s /usr/sbin/nologin downloader

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
