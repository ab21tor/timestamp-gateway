# OpenTimestamps calendar server (otsd)
#
# This container runs the operator-controlled OTS calendar backend.
# The gateway submits paid digests here; otsd aggregates them and anchors
# the aggregate root in Bitcoin via a configured Bitcoin Core RPC endpoint.
#
# OPERATOR CHECKLIST before starting:
#   1. A reachable Bitcoin Core node with RPC enabled (pruned is acceptable
#      for the server's role of submitting transactions; not for full verification).
#   2. A wallet loaded in Bitcoin Core with enough BTC to pay for OP_RETURN
#      anchoring transactions (typically a few thousand sats per block cycle).
#   3. The four BITCOIN_RPC_* env vars set in docker-compose.yml or .env.
#   4. /data mounted to a persistent Docker volume (tor_keys equivalent for otsd).
#
# Bitcoin wallet requirement:
#   otsd submits one Bitcoin transaction per aggregation cycle (roughly per block).
#   Each transaction costs normal on-chain fees. A small wallet (e.g. 100k sats)
#   is sufficient for extended operation at low submission volumes.
#
# Verify the current opentimestamps-server installation method and config format at:
#   https://github.com/opentimestamps/opentimestamps-server
# before production deployment. The interface (POST /digest) is stable;
# the startup flags and config file format should be verified against the current repo.

FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir opentimestamps-server

VOLUME /data

EXPOSE 14788

# otsd reads Bitcoin RPC settings from environment or a config file.
# Adjust the startup command after verifying the current CLI flags:
#   otsd --help
CMD ["otsd", "--path", "/data"]
