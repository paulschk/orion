FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      ca-certificates curl gnupg sqlite3 tzdata \
 && rm -rf /var/lib/apt/lists/*

# Incus command-line client. The panel drives the *host* Incus daemon through
# the unix socket that is bind-mounted into this container, so we need the
# client only - no daemon, no privileges.
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc \
 && printf 'deb [signed-by=/etc/apt/keyrings/zabbly.asc] https://pkgs.zabbly.com/incus/stable bookworm main\n' \
      > /etc/apt/sources.list.d/zabbly-incus-stable.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends incus-client \
 && rm -rf /var/lib/apt/lists/*

ENV INCUS_DIR=/var/lib/incus

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --upgrade pip \
 && /venv/bin/pip install --no-cache-dir -r requirements.txt

COPY orion ./orion
RUN mkdir -p /caddy

ENV PATH="/venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "orion.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
