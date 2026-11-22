# The Jesse cross check (spec 0033 AC-11): laptop only, Compose profile `jesse`, never deployed.
# Jesse needs Python 3.13 or older (our stack runs 3.14), so it gets its own image.
FROM python:3.13-slim-bookworm

ARG JESSE_VERSION=3.6.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "jesse==${JESSE_VERSION}" \
    && useradd --uid 10001 --create-home jesse \
    # `jesse run` installs the dashboard editor's language server here at first start
    && install -d -o jesse -g jesse /usr/local/lib/python3.13/site-packages/jesse/lsp

COPY docker/jesse/entrypoint.sh /usr/local/bin/jesse-entrypoint
COPY apps/lab/jesse/ /jesse/
USER jesse
WORKDIR /home/jesse/project
ENTRYPOINT ["jesse-entrypoint"]
CMD ["jesse", "run"]
