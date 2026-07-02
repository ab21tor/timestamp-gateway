# otsd runtime image — Python + dependencies ONLY.
# The calendar CODE is a fork of opentimestamps-server, mounted at /app
# at runtime (see docker-compose and the operator guide). Build context
# must be the fork checkout so its requirements.txt is available:
#   docker build -t otsd-local -f otsd/Dockerfile /path/to/opentimestamps-server
# Known-good resolved versions: opentimestamps 0.4.5, python-bitcoinlib 0.11.2.
#
# Runs with --network host: otsd reaches Bitcoin RPC via a socat/Tor
# bridge on 127.0.0.1:18332 (deploy/socat-bitcoin-rpc.service.example),
# and the gateway reaches otsd on 127.0.0.1:14788.
FROM python:3.11-slim

RUN apt-get update -qq && apt-get install -y -qq build-essential libleveldb-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -q -r /app/requirements.txt \
    && pip install -q python-bitcoinlib==0.11.2

VOLUME /calendar
EXPOSE 14788

# Bitcoin RPC config: single env var BITCOIN_RPC_SERVICE_URL (full URL
# including credentials), sourced from gitignored .env — never on a
# command line, never in tracked files.
CMD ["otsd", "--calendar", "/calendar", "--btc-conf-target", "2", "-v"]
