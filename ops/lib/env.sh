#!/usr/bin/env bash
# Shared configuration loader for the ops/ scripts. Source it, call
# load_env, and only THEN resolve settings with ${VAR:-default}: every
# setting is read after the gitignored .env has been loaded, never before.
# (2026-09-15 review: wallet-balance-check.sh captured WALLET_STATUS_PATH
# before sourcing .env, so a path changed there was ignored and /health kept
# reading a file nothing wrote to; upgrade-all-proofs.sh never read .env at
# all; backup-live-state.sh ignored BACKUP_STATUS_PATH.)
#
# Precedence, lowest to highest: the script's own defaults (applied by the
# caller after load_env returns), the invoking environment, then
# $REPO/.env — .env wins over the environment on purpose: it is the
# deployment's one configuration file, and a unit file or shell that still
# exports an older value must not override it silently. Files named in
# OPS_EXTRA_ENV_FILES (colon-separated) are sourced after .env, later ones
# winning; a file that is absent or unreadable is skipped, and
# ENV_LOADED lists the ones that were read (space-separated) so a caller can
# fail loudly when nothing was.
#
# Sourcing tolerates unfilled placeholder lines (an <angle-bracket> host
# errors as shell): the error prints, the rest of the file still loads.
load_env() {
  REPO="${REPO:-/home/gateway/timestamp-gateway}"
  ENV_LOADED=""
  local file
  for file in "$REPO/.env" $(printf '%s' "${OPS_EXTRA_ENV_FILES:-}" | tr ':' ' '); do
    [ -n "$file" ] && [ -f "$file" ] && [ -r "$file" ] || continue
    set -a
    # shellcheck disable=SC1090
    . "$file" || true
    set +a
    ENV_LOADED="${ENV_LOADED:+$ENV_LOADED }$file"
  done
}
