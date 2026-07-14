FROM debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df

# socat: the rpc-bridge compose service (--profile onion-rpc) reuses this
# image with a socat command instead of tor.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tor socat \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/tor/timestamp_gateway \
    && chown -R debian-tor:debian-tor /var/lib/tor \
    # tor refuses to start if DataDirectory or HiddenServiceDir is group/world
    # accessible; these modes survive the copy-up into the tor_keys volume.
    && chmod 700 /var/lib/tor /var/lib/tor/timestamp_gateway

COPY torrc /etc/tor/torrc

USER debian-tor

EXPOSE 9050

CMD ["tor", "-f", "/etc/tor/torrc"]
