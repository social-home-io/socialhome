# Social Home core image — the household instance (§4).
#
# Entry point: ``python -m socialhome``.
# Ports: 8099 (HTTP/WebSocket) + 8124 (aiolibdatachannel signalling)
#        + 8100 (household dashboard, own port; off until an admin enables it).
# Runtime: Python 3.14-slim + ffmpeg (video transcoding in SpacePosts
# + BazaarListing thumbnails) + libjpeg / libwebp (Pillow).
#
# Installs from a pre-built wheel — the SPA bundle in
# ``socialhome/static/`` is already baked in by the publish workflow's
# ``build`` job (or by a local ``pnpm --dir client run build`` +
# ``python -m build``). The runtime image therefore needs no node /
# pnpm at all, which keeps the multi-arch build cheap on QEMU.
#
# Published as ``ghcr.io/social-home-io/socialhome:{tag}`` by the
# ``docker-core`` job in .github/workflows/publish.yml.

FROM python:3.14-slim AS base

# uv replaces pip for the wheel install — ~10× faster, and ships static
# via the official ``ghcr.io/astral-sh/uv`` image so we don't depend on an
# extra apt / pip-bootstrap layer. Pinned (not ``:latest``) so a rebuild of
# the same tag uses the same installer — keep it equal to the
# ``setup-uv`` version in .github/workflows/ci.yml.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /usr/local/bin/

# System deps for Pillow + ffmpeg.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      libjpeg-turbo-progs \
      libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

# Non-root user up front so a host-mounted ``/data`` volume ends up
# with the right owner.
RUN groupadd --system --gid 10001 hsuser && \
    useradd  --system --uid 10001 --gid hsuser --create-home hsuser

WORKDIR /app

# Pre-built wheel from the publish workflow's ``build`` job. The
# job captures the exact PEP 427 filename
# (``socialhome-{version}-py3-none-any.whl``) and passes it via
# ``--build-arg WHEEL`` so we keep the version-bearing filename
# uv requires. SPA bundle lives inside under ``socialhome/static/``.
#
# Dependencies come from the lock, not from a fresh resolve:
# ``uv export --frozen`` turns the committed ``uv.lock`` into a requirements
# file (core runtime) that pins every package, transitive ones included,
# with the sha256 of each published file — the same flags as
# ``scripts/lock.sh --export core``, which prints it locally. ``--frozen``
# reads the lock as-is (no resolve, no network). Step 1 installs exactly
# that set — ``--require-hashes`` refuses any file whose hash isn't listed.
# Step 2 adds the wheel itself with ``--no-deps`` (it was built in the same
# workflow, so it has no published hash), and ``uv pip check`` fails the
# build if the lock and the wheel's own requirements ever disagree.
# ``--system`` installs into the base image's site-packages directly
# (no venv); ``--no-cache`` keeps the layer size down.
ARG WHEEL
COPY pyproject.toml uv.lock /tmp/lock/
COPY dist/${WHEEL} /tmp/${WHEEL}
RUN uv export --no-cache --frozen --no-dev --no-emit-project --no-header \
      --format requirements-txt --project /tmp/lock \
      > /tmp/requirements.txt && \
    uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt && \
    uv pip install --system --no-cache --no-deps "/tmp/${WHEEL}" && \
    uv pip check --system && \
    rm -rf /tmp/lock /tmp/requirements.txt /tmp/${WHEEL}

RUN mkdir -p /data && chown -R hsuser:hsuser /data

VOLUME /data
ENV SH_DATA_DIR=/data
ENV SH_MODE=standalone

EXPOSE 8099 8100 8124

USER hsuser

CMD ["python", "-m", "socialhome"]
