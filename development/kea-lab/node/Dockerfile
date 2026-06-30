# A single Kea node for the lab: kea-dhcp4 + kea-ctrl-agent (the REST API) in one
# container, sharing a UNIX control socket. Built from Debian's Kea packages
# (Kea 2.x), which include the open-source lease_cmds and ha hook libraries.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends kea curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/kea /var/lib/kea

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
