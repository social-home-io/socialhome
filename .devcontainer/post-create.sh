#!/usr/bin/env bash
# One-time devcontainer bootstrap.
#
# Invoked by ``postCreateCommand`` in ``devcontainer.json`` after the
# image has been built and features have run. Kept as a script (instead
# of an inline ``&&``-chain in JSON) so the steps are readable, the
# shell handles quoting correctly, and ``set -e`` aborts on the first
# failure instead of silently continuing.

set -euo pipefail

# 1. chrome-devtools-mcp / Puppeteer launch ``/usr/bin/chromium``,
#    which is Debian's shell-wrapper launcher. It sources every
#    ``/etc/chromium.d/*.conf`` before exec-ing the real binary and
#    appends ``$CHROMIUM_FLAGS`` to argv, so dropping a tiny conf file
#    is the simplest way to add ``--no-sandbox`` (the container can't
#    ``CLONE_NEWUSER`` so Chromium's setuid sandbox can't init) and
#    ``--disable-dev-shm-usage`` (``/dev/shm`` is tiny in containers)
#    to every launch — including the one the MCP plugin spawns.
echo 'export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox --disable-dev-shm-usage"' \
	| sudo tee /etc/chromium.d/devcontainer.conf >/dev/null

# 2. Create / recreate the project venv at ``.venv/`` from the system
#    Python on PATH (whichever 3.14 the image ships).
uv venv .venv --seed

# 3. Install the Python project in editable mode + dev extras at exactly
#    the versions in ``uv.lock`` — the same set CI tests and the images
#    ship. ``--locked`` fails if the lock is stale (run
#    ``scripts/lock.sh``); ``--inexact`` keeps the pip/setuptools that
#    ``--seed`` put in the venv.
uv sync --locked --inexact --extra dev

# 4. Wire up the pre-commit hooks so ``git commit`` runs ruff + mypy +
#    frontend ESLint + tsc on staged files.
.venv/bin/pre-commit install

# 5. Install pnpm globally (Node feature ships npm; we use pnpm for
#    the frontend per ``client/package.json``).
npm install -g pnpm

# 6. Install the frontend deps so ``pnpm run dev`` works on first
#    launch without manual steps.
cd client && pnpm install
