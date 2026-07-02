FROM debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df

RUN apt-get update \
    && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/tor/timestamp_gateway \
    && chown -R debian-tor:debian-tor /var/lib/tor

COPY torrc /etc/tor/torrc

USER debian-tor

EXPOSE 9050

CMD ["tor", "-f", "/etc/tor/torrc"]
