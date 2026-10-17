"""Release-blocker guard for ``security``-marked tests (spec §27).

``tests/protocol/`` and every other ``@pytest.mark.security`` test are a
release blocker: a run that silently drops one of them must not go green.
CI used to enforce that by running ``pytest tests/protocol/ -m security`` a
second time after the full suite, which re-ran ~3 100 tests the full run had
already passed. This plugin replaces the re-run with three checks that cover
the ways a security test can go missing without failing:

1. **Unmarked protocol test** (collection): every test collected under
   ``tests/protocol/`` must carry the ``security`` mark, so ``-m security``
   always selects the whole directory. A missing mark is a collection
   error, not a silent drop.
2. **Skipped / xfailed** (every run, including ``-m security`` locally): a
   ``security`` test that reports *skipped*, *xfailed* or *xpassed* — a
   ``skip`` / ``skipif`` / ``importorskip`` / ``xfail`` mark, a fixture
   that calls ``pytest.skip`` — fails the session with a list of them.
3. **Deselected / never run** (CI): each run can write a manifest of every
   ``security`` test it reported (``--security-manifest=PATH``). A final
   ``pytest --collect-only --security-verify=DIR`` collects the *whole*
   tree and fails unless every collected ``security`` test appears as
   ``passed`` in the union of the manifests under ``DIR``. That catches a
   shard that never ran, a ``-m`` / ``-k`` / ``--deselect`` in the CI
   command, a split plugin that drops a test, and a crashed worker.

Reports reach the controller process under ``pytest-xdist`` (``keywords``
travel with the serialised report), so the checks run there once; workers
skip them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SECURITY_MARK = "security"
PROTOCOL_DIR = Path(__file__).resolve().parent / "protocol"

#: Outcomes that count as "the release blocker did not actually pass".
_NOT_RUN = frozenset({"skipped", "xfailed", "xpassed"})


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("security-guard", "release-blocker security tests")
    group.addoption(
        "--security-manifest",
        metavar="PATH",
        default=None,
        help="write a JSON {nodeid: outcome} manifest of every "
        "security-marked test this run reported",
    )
    group.addoption(
        "--security-verify",
        metavar="DIR",
        default=None,
        help="with --collect-only: fail unless every collected "
        "security-marked test passed in the union of the *.json manifests "
        "under DIR",
    )


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        # xdist worker: the controller sees every report and owns the checks.
        return
    config.pluginmanager.register(SecurityGuard(config), "security-guard")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Check 1 — runs wherever collection runs (xdist workers included)."""
    unmarked = [
        item.nodeid
        for item in items
        if item.path.is_relative_to(PROTOCOL_DIR)
        and item.get_closest_marker(SECURITY_MARK) is None
    ]
    if unmarked:
        raise pytest.UsageError(
            "tests/protocol/ is a release blocker: every test there must "
            "carry @pytest.mark.security (add `pytestmark = "
            "pytest.mark.security` to the module). Unmarked:\n  "
            + "\n  ".join(unmarked)
        )


def _outcome(report: pytest.TestReport) -> str | None:
    """Map one phase report to a test-level outcome (``None`` = no verdict)."""
    if report.skipped:
        return "xfailed" if hasattr(report, "wasxfail") else "skipped"
    if report.failed:
        return "failed"
    if report.when == "call":
        return "xpassed" if hasattr(report, "wasxfail") else "passed"
    return None


#: How many node ids a failure message lists before summarising the rest.
_LIST_LIMIT = 50


def _listing(nodeids: list[str]) -> str:
    shown = "\n  ".join(nodeids[:_LIST_LIMIT])
    extra = len(nodeids) - _LIST_LIMIT
    return shown + (f"\n  ... and {extra} more" if extra > 0 else "")


#: Worse outcomes win when a test reports more than one phase verdict.
_RANK = {"passed": 0, "xpassed": 1, "xfailed": 2, "skipped": 3, "failed": 4}


class SecurityGuard:
    def __init__(self, config: pytest.Config) -> None:
        self._config = config
        self.outcomes: dict[str, str] = {}
        self._problems: list[str] = []
        self._verified: tuple[int, int] | None = None

    # ── 2. skipped / xfailed security tests ──────────────────────────────

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if SECURITY_MARK not in report.keywords:
            return
        outcome = _outcome(report)
        if outcome is None:
            return
        previous = self.outcomes.get(report.nodeid)
        if previous is None or _RANK[outcome] > _RANK[previous]:
            self.outcomes[report.nodeid] = outcome

    # ── 3. union of manifests covers every collected security test ───────

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        verify_dir = self._config.getoption("security_verify")
        if verify_dir is None:
            return
        if not self._config.getoption("collectonly"):
            raise pytest.UsageError("--security-verify needs --collect-only")
        expected = {
            item.nodeid
            for item in session.items
            if item.get_closest_marker(SECURITY_MARK) is not None
        }
        manifests = sorted(Path(verify_dir).glob("*.json"))
        passed: set[str] = set()
        for path in manifests:
            data = json.loads(path.read_text())
            passed.update(n for n, o in data.items() if o == "passed")
        missing = sorted(expected - passed)
        if not expected:
            self._problems.append("no security-marked tests were collected")
        elif missing:
            self._problems.append(
                f"{len(missing)} of {len(expected)} security tests did not "
                f"pass in any of {len(manifests)} manifest(s) under "
                f"{verify_dir}:\n  " + _listing(missing)
            )
        self._verified = (len(expected), len(manifests))

    # ── reporting ────────────────────────────────────────────────────────

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        not_run = sorted(n for n, o in self.outcomes.items() if o in _NOT_RUN)
        if not_run:
            self._problems.append(
                f"{len(not_run)} security test(s) did not run to a pass "
                "(skipped / xfailed / xpassed) — release blockers may not "
                "be skipped:\n  "
                + _listing([f"{n} [{self.outcomes[n]}]" for n in not_run])
            )
        manifest = self._config.getoption("security_manifest")
        if manifest is not None:
            path = Path(manifest)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.outcomes, indent=0, sort_keys=True))
        if self._problems and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        verified = self._verified
        if verified is not None and not self._problems:
            terminalreporter.write_line(
                f"security guard: all {verified[0]} security tests passed "
                f"across {verified[1]} manifest(s)"
            )
        for problem in self._problems:
            terminalreporter.section("security guard", sep="=", red=True)
            terminalreporter.write_line(problem, red=True)
