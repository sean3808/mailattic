FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cron \
        gettext-base \
        isync \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/mbsync-entrypoint.sh
COPY run_sync.sh /usr/local/bin/run-sync.sh
COPY mbsyncrc.template /opt/mailattic/mbsyncrc.template
COPY run_sync.py /opt/mailattic/run_sync.py
COPY openssl-legacy.cnf /etc/ssl/openssl-legacy.cnf

ENV OPENSSL_CONF=/etc/ssl/openssl-legacy.cnf

RUN chmod +x /usr/local/bin/mbsync-entrypoint.sh /usr/local/bin/run-sync.sh

CMD ["/usr/local/bin/mbsync-entrypoint.sh"]
