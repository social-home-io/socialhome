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

# uv replaces pip for the wheel install — ~10× faster, deterministic
# resolution, and ships static via the official ``ghcr.io/astral-sh/uv``
# image so we don't depend on an extra apt / pip-bootstrap layer.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

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
# ``--system`` installs into the base image's site-packages directly
# (no venv); ``--no-cache`` keeps the layer size down.
ARG WHEEL
COPY dist/${WHEEL} /tmp/${WHEEL}
RUN uv pip install --system --no-cache /tmp/${WHEEL} && \
    rm /tmp/${WHEEL}

RUN mkdir -p /data && chown -R hsuser:hsuser /data

VOLUME /data
ENV SH_DATA_DIR=/data
ENV SH_MODE=standalone

EXPOSE 8099 8100 8124

USER hsuser

CMD ["python", "-m", "socialhome"]
