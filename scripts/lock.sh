#!/usr/bin/env bash
# Regenerate — or, with ``--check``, verify — the Python dependency lock.
#
#   uv.lock                            every package, every extra, every platform
#                                      (universal resolution; source of truth)
#   requirements/constraints.txt       core runtime: the core Docker image and the
#                                      Home Assistant App install exactly this set
#   requirements/constraints-gfs.txt   core + ``[global-server]``: the GFS image
#
# Both exports pin every package, transitive ones included, to an exact
# version with the sha256 of every published file. They are committed so the
# Home Assistant App can fetch the set for a release over
# ``raw.githubusercontent.com/social-home-io/socialhome/<tag>/requirements/…``.
#
# Usage:
#   scripts/lock.sh                      re-lock after editing pyproject.toml
#   scripts/lock.sh --upgrade-package X  move one package (incl. a transitive one)
#   scripts/lock.sh --upgrade            move everything the pins allow
#   scripts/lock.sh --check              CI: fail if anything is out of date
set -euo pipefail

cd "$(dirname "$0")/.."

# "<output file>:<extra uv export flags>"
targets=(
	"requirements/constraints.txt:"
	"requirements/constraints-gfs.txt:--extra global-server"
)

check=0
if [[ "${1:-}" == "--check" ]]; then
	check=1
	shift
fi

render() {
	# $1 = output path, rest = extra ``uv export`` flags.
	local out=$1
	shift
	printf '%s\n' \
		"# GENERATED from uv.lock by scripts/lock.sh — do not edit by hand." \
		"# Every package (transitive ones included) at its locked version, with" \
		"# the sha256 of each published file. Install with" \
		"#   uv pip install --require-hashes -r ${out}   (then the wheel with --no-deps)" \
		"# — see docs/testing.md, \"Dependency lock\"."
	uv export --frozen --no-emit-project --no-header --format requirements-txt "$@"
}

if ((check)); then
	uv lock --check
	status=0
	for spec in "${targets[@]}"; do
		out=${spec%%:*}
		# shellcheck disable=SC2086 # the extra flags are meant to word-split
		if ! diff -u "$out" <(render "$out" ${spec#*:}); then
			echo "::error file=${out}::${out} is out of date with uv.lock — run scripts/lock.sh" >&2
			status=1
		fi
	done
	exit "$status"
fi

uv lock "$@"
mkdir -p requirements
for spec in "${targets[@]}"; do
	out=${spec%%:*}
	tmp=$(mktemp)
	# shellcheck disable=SC2086 # the extra flags are meant to word-split
	render "$out" ${spec#*:} >"$tmp"
	chmod 644 "$tmp"
	mv "$tmp" "$out"
done
echo "Updated uv.lock, requirements/constraints.txt, requirements/constraints-gfs.txt"
