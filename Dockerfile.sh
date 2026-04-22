# Social Home core image — the household instance (§4).
#
# Entry point: ``python -m socialhome``.
# Ports: 8099 (HTTP/WebSocket) + 8124 (aiolibdatachannel signalling).
# Runtime: Python 3.14-slim + ffmpeg (video transcoding in SpacePosts
# + BazaarListing thumbnails) + libjpeg / libwebp (Pillow).
#
# Builds the frontend (client/) with pnpm so a stock image boots
# straight into the full web UI without a second build step.
#
# Published as ``ghcr.io/social-home-io/socialhome:{tag}`` by the
# ``docker-core`` job in .github/workflows/publish.yml.

FROM python:3.14-slim AS base

# System deps for Pillow + ffmpeg.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      libjpeg-turbo-progs \
      libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

# Non-root user up front so the data dir gets the right owner (the HA
# Supervisor mounts /data on the host; we still chown so standalone
# users don't accidentally write as root).
RUN groupadd --system --gid 10001 appuser && \
    useradd  --system --uid 10001 --gid appuser --create-home appuser

WORKDIR /app

# Install the Python package. Core image ships without the
# ``global-server`` extras — that lives in Dockerfile.gfs.
COPY pyproject.toml LICENSE ./
COPY socialhome/ socialhome/
RUN pip install --no-cache-dir .

# Build the frontend if present. Kept conditional so a minimal
# sub-image (built from a slimmer context) still works.
COPY client/ client/
RUN if [ -f client/package.json ]; then \
      npm install -g pnpm && \
      pnpm --dir client install --frozen-lockfile && \
      pnpm --dir client run build; \
    fi

# Reset ownership so the non-root runtime user can read app code +
# write to the persisted data volume without a chown at boot.
RUN mkdir -p /data && chown -R appuser:appuser /app /data

VOLUME /data
ENV SH_DATA_DIR=/data
ENV SH_MODE=standalone

EXPOSE 8099 8124

USER appuser

CMD ["python", "-m", "socialhome"]
