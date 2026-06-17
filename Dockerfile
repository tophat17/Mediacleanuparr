# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="mediacleanuparr" \
      org.opencontainers.image.description="Prune Radarr & Sonarr libraries by TMDb audience rating, dry-run first." \
      org.opencontainers.image.source="https://github.com/tophat17/Mediacleanuparr"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    TZ=America/Edmonton \
    APP_PORT=8787 \
    CONFIG_DIR=/config \
    MEDIA_ROOTS=/media \
    DRY_RUN_ONLY=true \
    DELETE_FILES_ENABLED=false \
    MIN_RT_SCORE=50 \
    INCLUDE_MOVIES=true \
    INCLUDE_TV=true \
    INCLUDE_UNRATED=false

# gosu drops privileges to the PUID/PGID user; tini reaps zombies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tini ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/media"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('APP_PORT','8787')+'/api/health').status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
