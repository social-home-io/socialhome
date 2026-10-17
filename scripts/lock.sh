#!/usr/bin/env bash
# Regenerate, verify or export the Python dependency lock.
#
# ``uv.lock`` is the only committed lock: every package, every extra, every
# platform (a universal resolution). Nothing else is generated into the repo,
# so a plain ``uv lock`` — Dependabot's included — keeps it complete.
#
# The shipped images turn it into a hash-pinned requirements file at build
# time with ``uv export --frozen`` (see ``Dockerfile.sh`` / ``Dockerfile.gfs``,
# and the Home Assistant App, which fetches ``uv.lock`` + ``pyproject.toml``
# for a release tag). ``--export`` prints exactly that file, so you can see
# what an image installs:
#
#   core   core runtime: the core Docker image and the Home Assistant App
#   gfs    core + ``[global-server]``: the GFS image
#
# Usage:
#   scripts/lock.sh                      re-lock after editing pyproject.toml
#   scripts/lock.sh --upgrade-package X  move one package (incl. a transitive one)
#   scripts/lock.sh --upgrade            move everything the pins allow
#   scripts/lock.sh --check              CI: fail if uv.lock is out of date
#   scripts/lock.sh --export [core|gfs]  print an image's hashed requirements
#                                        (default: core) to stdout
set -euo pipefail

cd "$(dirname "$0")/.."

case "${1:-}" in
--check)
	exec uv lock --check
	;;
--export)
	case "${2:-core}" in
	core) extra=() ;;
	gfs) extra=(--extra global-server) ;;
	*)
		echo "usage: scripts/lock.sh --export [core|gfs]" >&2
		exit 2
		;;
	esac
	# Keep these flags identical to the ``uv export`` in Dockerfile.sh,
	# Dockerfile.gfs, CI's ``images`` job and the Home Assistant App's
	# builder stage.
	exec uv export --frozen --no-dev --no-emit-project --no-header \
		--format requirements-txt "${extra[@]}"
	;;
esac

uv lock "$@"
echo "Updated uv.lock"
