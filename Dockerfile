# ICSD Panel hub image. The agent binaries are built here too and served
# by the hub at /dl/, so a managed server needs nothing but curl.
#
#   docker build -t icsd-panel .
ARG VERSION=2.0.0

FROM golang:1.24-alpine AS agent
ARG VERSION
WORKDIR /src
# Dependencies are vendored: the build needs no Go module proxy
# (proxy.golang.org is not reachable from every network).
COPY agent/ ./
RUN set -e; mkdir /dist; \
    for arch in amd64 arm64; do \
      CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -mod=vendor -trimpath \
        -ldflags "-s -w -X main.Version=$VERSION" -o /dist/icsd-agent-linux-$arch . ; \
    done; \
    cd /dist && sha256sum icsd-agent-linux-* > SHA256SUMS && echo "$VERSION" > VERSION

FROM python:3.12-slim
# X-Forwarded-For is trusted only from loopback and private networks (Caddy
# in the compose network, or a proxy on the host via Docker's gateway);
# uvicorn takes the right-most untrusted address, so a forged header is ignored.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ICSD_DATA_DIR=/data ICSD_DIST_DIR=/opt/icsd/dist \
    FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
WORKDIR /opt/icsd
COPY hub/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY hub/icsd_hub ./icsd_hub
COPY --from=agent /dist ./dist
RUN useradd --system --uid 10001 --home-dir /data icsd \
 && mkdir -p /data && chown icsd /data \
 && printf '#!/bin/sh\nexec python -m icsd_hub "$@"\n' > /usr/local/bin/icsd-hub \
 && chmod +x /usr/local/bin/icsd-hub
USER icsd
VOLUME /data
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/healthz', timeout=3)"
# One worker on purpose: live server state lives in this process.
CMD ["uvicorn", "icsd_hub.main:app", "--host", "0.0.0.0", "--port", "8088", \
     "--proxy-headers", "--no-server-header"]
