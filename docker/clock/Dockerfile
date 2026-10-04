# Laptop only: keeps the Docker Desktop VM clock right (see start.sh). Started by the
# `laptop` Compose profile, never on the VPS.
FROM alpine:3.22
RUN apk add --no-cache chrony
COPY docker/clock/chrony.conf /etc/chrony/chrony.conf
COPY docker/clock/start.sh /usr/local/bin/kfs-clock
RUN chmod 0755 /usr/local/bin/kfs-clock
ENTRYPOINT ["/usr/local/bin/kfs-clock"]
