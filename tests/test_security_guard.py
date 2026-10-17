"""The release-blocker guard (``tests/security_guard.py``) fails a run that
skips, deselects or never runs a ``security``-marked test."""

from __future__ import annotations

import json

import pytest

from tests import security_guard

_PLUGIN = ("-p", "tests.security_guard", "-p", "no:cacheprovider")

_SECURITY_TESTS = """
import pytest

pytestmark = pytest.mark.security

def test_one():
    pass

def test_two():
    pass
"""


@pytest.fixture(autouse=True)
def _inner_ini(pytester):
    """The inner runs get their own rootdir; give pytest-asyncio (installed
    suite-wide) the loop scope it otherwise warns about."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest(*_PLUGIN, *args)


def test_all_security_tests_passing_is_green(pytester):
    pytester.makepyfile(test_sec=_SECURITY_TESTS)
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=2)


def test_a_skipped_security_test_fails_the_run(pytester):
    pytester.makepyfile(
        test_sec="""
        import pytest

        @pytest.mark.security
        @pytest.mark.skip(reason="flaky")
        def test_blocker():
            pass
        """
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        ["*1 security test(s) did not run to a pass*", "*test_blocker [[]skipped[]]*"]
    )


def test_a_skip_from_inside_a_security_test_fails_the_run(pytester):
    pytester.makepyfile(
        test_sec="""
        import pytest

        pytestmark = pytest.mark.security

        @pytest.fixture
        def needs_thing():
            pytest.skip("thing unavailable")

        def test_blocker(needs_thing):
            pass
        """
    )
    assert _run(pytester).ret == pytest.ExitCode.TESTS_FAILED


@pytest.mark.parametrize("strict", ["False", "True"])
def test_an_xfail_security_test_fails_the_run(pytester, strict):
    pytester.makepyfile(
        test_sec=f"""
        import pytest

        @pytest.mark.security
        @pytest.mark.xfail(strict={strict})
        def test_blocker():
            assert False
        """
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*test_blocker [[]xfailed[]]*"])


def test_a_skipped_ordinary_test_is_not_the_guards_business(pytester):
    pytester.makepyfile(
        test_plain="""
        import pytest

        @pytest.mark.skip(reason="not a release blocker")
        def test_plain():
            pass
        """
    )
    assert _run(pytester).ret == pytest.ExitCode.OK


def test_an_unmarked_protocol_test_is_a_usage_error(pytester, monkeypatch):
    monkeypatch.setattr(security_guard, "PROTOCOL_DIR", pytester.path / "protocol")
    pytester.mkpydir("protocol")
    (pytester.path / "protocol" / "test_wire.py").write_text(
        "def test_wire():\n    pass\n"
    )
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*must carry @pytest.mark.security*"])


def test_manifest_records_every_security_outcome(pytester):
    pytester.makepyfile(test_sec=_SECURITY_TESTS)
    manifest = pytester.path / "out" / "m.json"
    assert _run(pytester, f"--security-manifest={manifest}").ret == 0
    assert json.loads(manifest.read_text()) == {
        "test_sec.py::test_one": "passed",
        "test_sec.py::test_two": "passed",
    }


def test_verify_passes_when_the_manifests_cover_every_security_test(pytester):
    pytester.makepyfile(test_sec=_SECURITY_TESTS)
    out = pytester.path / "out"
    # Two "shards", each running one of the tests.
    _run(pytester, "-k", "one", f"--security-manifest={out}/a.json")
    _run(pytester, "-k", "two", f"--security-manifest={out}/b.json")
    result = _run(pytester, "--collect-only", f"--security-verify={out}")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["*all 2 security tests passed across 2*"])


def test_verify_fails_on_a_deselected_security_test(pytester):
    pytester.makepyfile(test_sec=_SECURITY_TESTS)
    out = pytester.path / "out"
    _run(pytester, "-k", "one", f"--security-manifest={out}/a.json")
    result = _run(pytester, "--collect-only", f"--security-verify={out}")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*1 of 2 security tests did not pass*", "*two*"])


def test_verify_fails_with_no_manifests(pytester):
    pytester.makepyfile(test_sec=_SECURITY_TESTS)
    result = _run(
        pytester, "--collect-only", f"--security-verify={pytester.path / 'none'}"
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_verify_requires_collect_only(pytester):
    pytester.makepyfile(test_sec=_SECURITY_TESTS)
    result = _run(pytester, f"--security-verify={pytester.path}")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
