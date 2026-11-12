FROM python:3.12-alpine

WORKDIR /app

COPY server.py index.html ./
COPY assets ./assets

ENV DATA_DIR=/data \
    PORT=80 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Argentina/Buenos_Aires

RUN apk add --no-cache tzdata

VOLUME ["/data"]

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1/health || exit 1

CMD ["python", "server.py"]
