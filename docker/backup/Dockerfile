# The backup job (spec 0005 AC-19): nightly downsample, pg_dump, manifest, restic to B2.
# Build args:
#   GIT_COMMIT  the commit being built, shown by `docker inspect` (CI sets it to the sha)
# One stage, named runtime so CI builds every image with the same target.
FROM alpine:3.20 AS runtime

RUN apk add --no-cache bash jq restic postgresql16-client

# Same uid as the app images: the VPS secret files are owner 10001, mode 0400. Folders created
# here keep this owner when a named volume first mounts them (/staging), or a drill volume (/restore).
RUN adduser -D -H -u 10001 -h /staging app \
    && mkdir -p /staging /restore \
    && chown app:app /staging /restore

COPY --chmod=0755 docker/backup/run.sh /usr/local/bin/kfs-backup

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
LABEL org.opencontainers.image.revision=${GIT_COMMIT}

USER app
WORKDIR /staging
CMD ["kfs-backup", "loop"]
