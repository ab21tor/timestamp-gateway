# otsd runtime image — Python + dependencies ONLY.
# The calendar CODE is the opentimestamps-server fork, mounted at /app at
# runtime (clone command and branch: operator guide, "Deploying the calendar
# (otsd)"), so code-only changes deploy with a pull + restart, no rebuild.
#
# The fork checkout is a named build context ("fork") — only its
# requirements.txt is read at build time:
#   docker build -t otsd-local --build-context fork=/path/to/opentimestamps-server otsd/
# docker compose provides it automatically via additional_contexts and
# OTSD_FORK_PATH (Compose v2.17+ / BuildKit required).
# Known-good resolved versions: opentimestamps 0.4.5, plyvel 1.5.1 (built
# from source against Debian 13's libleveldb 1.23), python-bitcoinlib 0.11.2.
# python:3.13-slim (mirroring fork ce8a63e): the 3.11 pin went
# with py-leveldb; plyvel reads the same LevelDB files, no calendar
# migration. A fork checkout older than ce8a63e still lists py-leveldb in
# its requirements.txt and will not build on this base.
#
# A COPY of this file lives at the fork's root (opentimestamps-server/
# Dockerfile) for the appliance shape, where the fork checkout is
# the build context and requirements.txt is read from "." instead of the
# named context. Keep the two in step: base digest, the bitcoinlib pin, CMD.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

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
# --btc-max-fee takes BTC: 0.0002 BTC = 20,000 sats. Never "fix" it to 20000.
# No -v: INFO is the production log level in every launch path — DEBUG
# ships commitment hexes and wallet outpoints into container logs.
# otsd's clean-exit path hangs off KeyboardInterrupt: SIGINT, not the
# docker-default SIGTERM, is what actually runs it.
STOPSIGNAL SIGINT
CMD ["python", "/app/otsd", "--calendar", "/calendar", "--btc-conf-target", "12", "--btc-max-fee", "0.0002"]
