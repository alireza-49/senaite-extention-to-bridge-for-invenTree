# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BRIDGE_CONFIG=/app/config.yml

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge ./bridge

RUN useradd --uid 10001 --create-home bridge \
    && mkdir -p /data \
    && chown -R bridge:bridge /data
USER bridge

VOLUME ["/data"]
EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=5)"

ENTRYPOINT ["python", "-m", "bridge"]
CMD ["run"]
