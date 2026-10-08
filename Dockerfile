# Pinned by digest as well as tag, so a rebuild of one commit starts from the
# same image; Dependabot moves it forward (.github/dependabot.yml).
FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DC_CONFIG_DIR=/config \
    DC_OUTPUT_DIR=/downloads \
    BEETSDIR=/config/beets

# ffmpeg encodes the MP3s and gosu drops from root to the mapped user once the
# volumes are prepared. libchromaprint-tools provides fpcalc, which beets'
# optional chroma plugin uses to identify audio by acoustic fingerprint rather
# than by tags; it is installed, but the plugin is left off by default.
# rsgain measures ReplayGain from the Library panel (app/replaygain.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gosu libchromaprint-tools rsgain \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copied on its own so the dependency layer is cached across code changes.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY app ./app
# The one-off maintenance scripts, run with `docker exec` or `docker run
# --entrypoint python ... -m tools.<name>`; their usage says so.
COPY tools ./tools
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && groupadd -g 1000 downloader \
 && useradd -u 1000 -g downloader -d /app -s /usr/sbin/nologin downloader

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
