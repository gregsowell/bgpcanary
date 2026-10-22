# bgpcanary container image: Alpine Linux with Python and bgpcanary, running as an unprivileged user.
# How to run it: docs/DOCKER.md.   Build it yourself:  docker build -t bgpcanary .
# The base image is pinned by digest. Refresh the digest before each release (docs/RELEASING.md).
ARG BASE=python:3.13-alpine3.24@sha256:bf12ecc0bea3f171d55145dd02c78b5eeb09f30ee2ac9cc2b4c9e4e8f5e30741

FROM ${BASE} AS build
WORKDIR /opt/bgpcanary
COPY requirements.txt ./
# as the installer does it: exact versions, only files whose hashes are in requirements.txt, only pre-built wheels
RUN python -m venv venv \
 && venv/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary :all: \
      -r requirements.txt \
 && venv/bin/pip uninstall --disable-pip-version-check -q -y pip
COPY deploy/install.sh deploy/docker/fetch-vendor.py /tmp/
COPY deploy/config.example.toml deploy/
COPY bgpcanary bgpcanary
# the front-end libraries, checked against the installer's checksums; byte-compiled now, as the image is read-only
RUN rm -rf bgpcanary/static/vendor \
 && python /tmp/fetch-vendor.py /tmp/install.sh bgpcanary/static/vendor \
 && find bgpcanary -name __pycache__ -prune -exec rm -rf {} + \
 && venv/bin/python -m compileall -q --invalidation-mode unchecked-hash bgpcanary

FROM ${BASE}
COPY deploy/docker/bgpcanary-admin /usr/local/bin/
RUN apk upgrade --no-cache \
 && apk add --no-cache tini \
 && chmod 755 /usr/local/bin/bgpcanary-admin \
 && addgroup -S -g 10001 bgpcanary \
 && adduser -S -D -H -u 10001 -G bgpcanary -h /var/lib/bgpcanary -s /sbin/nologin bgpcanary \
 && mkdir -p /var/lib/bgpcanary \
 && chown bgpcanary:bgpcanary /var/lib/bgpcanary \
 && chmod 750 /var/lib/bgpcanary
COPY --from=build /opt/bgpcanary /opt/bgpcanary
ENV PATH=/opt/bgpcanary/venv/bin:$PATH \
    PYTHONPATH=/opt/bgpcanary PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BGPCANARY_CONTAINER=1 BGPCANARY_CONFIG=/var/lib/bgpcanary/config.toml
WORKDIR /opt/bgpcanary
USER 10001:10001
VOLUME ["/var/lib/bgpcanary"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "-m", "bgpcanary.container", "healthcheck"]
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "-m", "bgpcanary.container"]
LABEL org.opencontainers.image.title="bgpcanary" \
      org.opencontainers.image.description="An early warning for your BGP routes: visibility, hijack, leak and upstream monitoring for networks with their own ASN" \
      org.opencontainers.image.source="https://github.com/gregsowell/bgpcanary" \
      org.opencontainers.image.documentation="https://github.com/gregsowell/bgpcanary/blob/main/docs/DOCKER.md" \
      org.opencontainers.image.licenses="FSL-1.1-ALv2"
