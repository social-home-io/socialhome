"""Split the suite into N CI shards by recorded per-file duration.

``pytest --shard=K/N`` keeps only the test files assigned to shard ``K``
(1-based) and deselects the rest. Assignment is deterministic — every
process (each xdist worker collects on its own) computes the same split
from the same inputs:

* whole files, never individual tests, so a module's fixtures run in one
  shard and ``--dist=loadfile`` inside the shard keeps working;
* longest-processing-time-first bin packing on the per-file seconds in
  ``tests/shard_durations.json`` (ties broken by path); a file missing from
  it (a new test file) weighs the median of the known ones.

``--store-shard-durations=PATH`` writes this run's per-file seconds (setup
+ call + teardown of every test, summed) as JSON. CI uploads one per shard
and the ``test (3.14)`` job merges them into a ``shard-durations``
artifact; refresh ``tests/shard_durations.json`` from it when the shards
drift apart (the job log prints each shard's wall time).

Shards are checked for completeness elsewhere: every shard writes a
security manifest and ``tests/security_guard.py`` fails CI unless the union
covers every collected ``security`` test, and the merged coverage gate
fails if a shard's data is missing.
"""

from __future__ import annotations

import heapq
import json
import statistics
from collections import defaultdict
from pathlib import Path

import pytest

DEFAULT_DURATIONS = Path(__file__).resolve().parent / "shard_durations.json"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("sharding", "split the suite into CI shards")
    group.addoption(
        "--shard",
        metavar="K/N",
        default=None,
        help="run only shard K of N (1-based), split by per-file duration",
    )
    group.addoption(
        "--shard-durations",
        metavar="PATH",
        default=str(DEFAULT_DURATIONS),
        help="per-file durations JSON the split is computed from",
    )
    group.addoption(
        "--store-shard-durations",
        metavar="PATH",
        default=None,
        help="write this run's per-file durations (seconds) as JSON",
    )


def parse_shard(value: str) -> tuple[int, int]:
    try:
        k_str, n_str = value.split("/")
        k, n = int(k_str), int(n_str)
    except ValueError:
        raise pytest.UsageError(f"--shard expects K/N, got {value!r}") from None
    if not 1 <= k <= n:
        raise pytest.UsageError(f"--shard {value}: need 1 <= K <= N")
    return k, n


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def assign(files: list[str], weights: dict[str, float], n: int) -> dict[str, int]:
    """Map each file to a shard index ``0..n-1`` (LPT bin packing)."""
    known = [weights[f] for f in files if f in weights]
    default = statistics.median(known) if known else 1.0
    order = sorted(files, key=lambda f: (-weights.get(f, default), f))
    bins = [(0.0, i) for i in range(n)]
    heapq.heapify(bins)
    out: dict[str, int] = {}
    for f in order:
        load, i = heapq.heappop(bins)
        out[f] = i
        heapq.heappush(bins, (load + weights.get(f, default), i))
    return out


def _load_weights(path: str) -> dict[str, float]:
    p = Path(path)
    if not p.is_file():
        return {}
    return {str(k): float(v) for k, v in json.loads(p.read_text()).items()}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    value = config.getoption("shard")
    if value is None:
        return
    k, n = parse_shard(value)
    files = sorted({_file_of(item.nodeid) for item in items})
    shard_of = assign(files, _load_weights(config.getoption("shard_durations")), n)
    kept = [i for i in items if shard_of[_file_of(i.nodeid)] == k - 1]
    dropped = [i for i in items if shard_of[_file_of(i.nodeid)] != k - 1]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        return  # the controller sees every report
    path = config.getoption("store_shard_durations")
    if path is not None:
        config.pluginmanager.register(_DurationRecorder(path), "shard-durations")


class _DurationRecorder:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._seconds: dict[str, float] = defaultdict(float)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._seconds[_file_of(report.nodeid)] += report.duration

    def pytest_sessionfinish(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {f: round(s, 2) for f, s in sorted(self._seconds.items())}
        self._path.write_text(json.dumps(data, indent=1) + "\n")
