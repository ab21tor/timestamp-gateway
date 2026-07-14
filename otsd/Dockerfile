# otsd runtime image — Python + dependencies ONLY.
# The calendar CODE is the opentimestamps-server fork — https://github.com/ab21tor/opentimestamps-server,
# branch calendar-ops — mounted at /app at runtime (see docker-compose and the
# operator guide), so code-only changes deploy with a pull + restart, no rebuild.
#
# The fork checkout is a named build context ("fork") — only its
# requirements.txt is read at build time:
#   docker build -t otsd-local --build-context fork=/path/to/opentimestamps-server otsd/
# docker compose provides it automatically via additional_contexts and
# OTSD_FORK_PATH (Compose v2.17+ / BuildKit required).
# Known-good resolved versions: opentimestamps 0.4.5, python-bitcoinlib 0.11.2.
FROM python:3.11-slim@sha256:b27df5841f3355e9473f9a516d38a6783b6c8dfeacaf2d14a240f443b368ddb6

RUN apt-get update -qq && apt-get install -y -qq build-essential libleveldb-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=fork requirements.txt /app/requirements.txt
RUN pip install -q -r /app/requirements.txt \
    && pip install -q python-bitcoinlib==0.11.2

VOLUME /calendar
EXPOSE 14788

# Bitcoin RPC config: single env var BITCOIN_RPC_SERVICE_URL (full URL
# including credentials), sourced from gitignored .env — never on a
# command line, never in tracked files.
#
# "python /app/otsd", not "otsd": exec-form CMD resolves via PATH only, and
# the otsd script lives in the runtime mount, not on PATH. Binds localhost
# (otsd's default) — safe under any networking; docker-compose overrides
# with --rpc-address 0.0.0.0 for its internal bridge network.
CMD ["python", "/app/otsd", "--calendar", "/calendar", "--btc-conf-target", "12", "-v"]
