"""``tests/sharding.py`` splits the suite into disjoint, complete shards."""

from __future__ import annotations

import json

import pytest

from tests import sharding

_PLUGIN = ("-p", "tests.sharding", "-p", "no:cacheprovider")


@pytest.fixture(autouse=True)
def _inner_ini(pytester):
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")


def test_assign_balances_by_weight_and_is_deterministic():
    weights = {"a.py": 10.0, "b.py": 6.0, "c.py": 5.0, "d.py": 4.0}
    files = sorted(weights)
    first = sharding.assign(files, weights, 2)
    assert first == sharding.assign(list(reversed(files)), weights, 2)
    loads = [0.0, 0.0]
    for f, shard in first.items():
        loads[shard] += weights[f]
    # Longest first onto the lightest shard: 10 | 6, then 5 -> 6, 4 -> 10.
    assert sorted(loads) == [11.0, 14.0]


def test_an_unknown_file_weighs_the_median():
    weights = {"a.py": 1.0, "b.py": 3.0, "c.py": 100.0}
    shard_of = sharding.assign(["a.py", "b.py", "c.py", "new.py"], weights, 2)
    # c.py alone on one shard; new.py (median 3.0) joins a.py and b.py.
    assert shard_of["new.py"] == shard_of["a.py"] == shard_of["b.py"]
    assert shard_of["c.py"] != shard_of["a.py"]


@pytest.mark.parametrize("bad", ["3", "0/2", "3/2", "a/b"])
def test_a_bad_shard_spec_is_a_usage_error(bad):
    with pytest.raises(pytest.UsageError):
        sharding.parse_shard(bad)


def test_shards_are_disjoint_and_cover_every_test(pytester):
    for name in ("one", "two", "three", "four", "five"):
        pytester.makepyfile(
            **{f"test_{name}": "def test_a():\n    pass\n\ndef test_b():\n    pass\n"}
        )
    seen: list[str] = []
    for k in (1, 2, 3):
        result = pytester.runpytest(*_PLUGIN, f"--shard={k}/3", "-q", "-rA")
        assert result.ret == pytest.ExitCode.OK
        seen += [
            line.split()[1] for line in result.outlines if line.startswith("PASSED ")
        ]
    assert sorted(seen) == sorted(
        f"test_{n}.py::test_{t}"
        for n in ("one", "two", "three", "four", "five")
        for t in "ab"
    )


def test_store_shard_durations_writes_per_file_seconds(pytester):
    pytester.makepyfile(test_x="def test_a():\n    pass\n")
    out = pytester.path / "d" / "durations.json"
    assert pytester.runpytest(*_PLUGIN, f"--store-shard-durations={out}").ret == 0
    data = json.loads(out.read_text())
    assert list(data) == ["test_x.py"]
    assert data["test_x.py"] >= 0
