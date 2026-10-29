"""haos ingress UI harness — boot a fake Home Assistant around a real
``SH_MODE=haos`` backend and drive the built SPA through it with Chrome.

Subcommands:

* ``verify`` — boot everything, build (or reuse) the SPA bundle, run every
  browser check, write a report + screenshots, tear down, exit non-zero
  on any failure. This is what CI runs.
* ``up`` — boot the same stack and keep it running for manual poking (the
  printed ingress URL works in any browser); Ctrl-C tears it down.

Run with the project's Python (the one that has the backend's
dependencies), from anywhere::

    python .claude/skills/haos-ingress-ui/harness.py verify --out /tmp/ingress

The orchestrator itself uses the standard library only; the three
services it starts (fake Supervisor, Social Home backend, ingress proxy)
run under the same interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CHECKS = HERE / "checks"
STATIC = REPO / "socialhome" / "static"
CLIENT = REPO / "client"

#: The ingress token is opaque to the App; HA mints a random urlsafe one.
#: Mixed case + ``-`` / ``_`` so a path helper that lower-cases or
#: re-encodes it shows up as a violation.
INGRESS_TOKEN = "Xk9_ingress-TOKEN_abc123"
SUPERVISOR_TOKEN = "fake-supervisor-token"

#: Where to look for Chrome / Chromium when neither ``--chrome`` nor an
#: env var names one. Debian's launcher first (the devcontainer), then the
#: GitHub runner's preinstalled Google Chrome, then macOS.
CHROME_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
CHROME_ENV_VARS = ("CHROME_PATH", "PUPPETEER_EXECUTABLE_PATH")

#: SPA sources whose change makes ``socialhome/static`` stale.
BUILD_INPUTS = (
    "src",
    "public",
    "index.html",
    "package.json",
    "pnpm-lock.yaml",
    "vite.config.ts",
)


class HarnessError(RuntimeError):
    """A setup step failed — reported as such, not as a check failure."""


def log(msg: str) -> None:
    print(f"[haos-ingress] {msg}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, timeout_s: float, proc: subprocess.Popen, name: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise HarnessError(
                f"{name} exited with {proc.returncode} before it was ready"
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return
        except urllib.error.URLError, OSError:
            pass
        time.sleep(0.2)
    raise HarnessError(f"{name} not ready at {url} after {timeout_s:.0f}s")


def find_chrome(explicit: str | None) -> str:
    for cand in (explicit, *(os.environ.get(v) for v in CHROME_ENV_VARS)):
        if cand:
            if Path(cand).is_file() or shutil.which(cand):
                return shutil.which(cand) or cand
            raise HarnessError(f"Chrome not found at {cand!r}")
    for cand in CHROME_CANDIDATES:
        found = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if found:
            return found
    raise HarnessError(
        "no Chrome / Chromium found — pass --chrome or set CHROME_PATH "
        f"(looked for {', '.join(CHROME_CANDIDATES)})"
    )


def _newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for p in paths:
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
    return newest


def ensure_spa_build(mode: str) -> None:
    index = STATIC / "index.html"
    if mode == "never":
        if not index.is_file():
            raise HarnessError(f"{index} missing and --build=never")
        log("SPA build: reusing socialhome/static (--build=never)")
        return
    if mode == "auto" and index.is_file():
        newest_src = _newest_mtime([CLIENT / p for p in BUILD_INPUTS])
        if index.stat().st_mtime >= newest_src:
            log("SPA build: socialhome/static is newer than client/ sources — reusing")
            return
        log("SPA build: socialhome/static is stale — rebuilding")
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise HarnessError(
            "pnpm not found — needed to build the SPA (or pass --build=never)"
        )
    if not (CLIENT / "node_modules").is_dir():
        subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=CLIENT, check=True)
    # ``vite build`` only — the GFS bundle isn't served by the backend.
    subprocess.run([pnpm, "exec", "vite", "build"], cwd=CLIENT, check=True)


def ensure_node_deps() -> None:
    lock = CHECKS / "package-lock.json"
    stamp = CHECKS / "node_modules" / ".package-lock.json"
    if stamp.is_file() and stamp.stat().st_mtime >= lock.stat().st_mtime:
        return
    npm = shutil.which("npm")
    if npm is None:
        raise HarnessError("npm not found — needed for the puppeteer-core checks")
    log("installing check dependencies (npm ci)")
    subprocess.run(
        [npm, "ci", "--no-audit", "--no-fund"],
        cwd=CHECKS,
        check=True,
        env={**os.environ, "PUPPETEER_SKIP_DOWNLOAD": "1"},
    )


@dataclass
class Stack:
    """The three processes behind one ingress URL."""

    out: Path
    supervisor_port: int
    backend_port: int
    proxy_port: int
    python: str
    data_dir: Path = field(init=False)
    procs: list[tuple[str, subprocess.Popen]] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return f"/api/hassio_ingress/{INGRESS_TOKEN}"

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    @property
    def backend_url(self) -> str:
        return f"http://127.0.0.1:{self.backend_port}"

    @property
    def supervisor_url(self) -> str:
        return f"http://127.0.0.1:{self.supervisor_port}"

    def _spawn(
        self, name: str, args: list[str], env: dict[str, str] | None = None
    ) -> subprocess.Popen:
        logs = self.out / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fh = (logs / f"{name}.log").open("wb")
        proc = subprocess.Popen(
            [self.python, "-u", *args],
            cwd=REPO,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
            start_new_session=True,
        )
        fh.close()
        self.procs.append((name, proc))
        return proc

    def start(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="haos-ingress-data-"))
        toml = self.data_dir / "socialhome.toml"
        toml.write_text(
            "[server]\n"
            'listen_host = "127.0.0.1"\n'
            f"listen_port = {self.backend_port}\n"
            'log_level = "INFO"\n\n'
            "[storage]\n"
            f'data_dir = "{self.data_dir}"\n'
        )
        sup = self._spawn(
            "supervisor",
            [
                str(HERE / "fake_supervisor.py"),
                "--port",
                str(self.supervisor_port),
                "--token",
                SUPERVISOR_TOKEN,
            ],
        )
        wait_http(f"{self.supervisor_url}/_users", 20, sup, "fake supervisor")
        backend = self._spawn(
            "backend",
            [str(HERE / "run_backend.py")],
            env={
                "SH_MODE": "haos",
                "SH_CONFIG": str(toml),
                "SUPERVISOR_TOKEN": SUPERVISOR_TOKEN,
                "SUPERVISOR_URL": self.supervisor_url,
                "SH_FAKE_SUPERVISOR_CORE": f"{self.supervisor_url}/core",
                # The app catalog the `app-frame` check installs from.
                "SH_APPS_CATALOG_URL": f"{self.supervisor_url}/_apps/catalog.json",
            },
        )
        wait_http(
            f"{self.backend_url}/api/instance/config",
            90,
            backend,
            "Social Home backend",
        )
        proxy = self._spawn(
            "proxy",
            [
                str(HERE / "ingress_proxy.py"),
                "--port", str(self.proxy_port),
                "--backend", self.backend_url,
                "--token", INGRESS_TOKEN,
                "--users-url", f"{self.supervisor_url}/_users",
            ],
        )  # fmt: skip
        wait_http(f"{self.proxy_url}/_proxy/log", 20, proxy, "ingress proxy")

    def stop(self, keep_data: bool = False) -> None:
        for _name, proc in reversed(self.procs):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
        for _name, proc in reversed(self.procs):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        self.procs.clear()
        if not keep_data and getattr(self, "data_dir", None):
            shutil.rmtree(self.data_dir, ignore_errors=True)


def _make_stack(args: argparse.Namespace, out: Path) -> Stack:
    return Stack(
        out=out,
        supervisor_port=args.supervisor_port or free_port(),
        backend_port=args.backend_port or free_port(),
        proxy_port=args.proxy_port or free_port(),
        python=args.python,
    )


def _write_report(out: Path, results: dict, stack: Stack) -> int:
    checks = results.get("results", [])
    failed = [c for c in checks if not c["ok"]]
    lines = [
        "# haos ingress UI — verify report",
        "",
        f"Ingress URL: `{stack.proxy_url}{stack.prefix}/`",
        "",
        "| Check | Result |",
        "|---|---|",
        *(f"| {c['name']} | {'PASS' if c['ok'] else '**FAIL**'} |" for c in checks),
        "",
    ]
    for c in checks:
        for key, label in (
            ("skipped", "routes not walked (no seeded id for the param)"),
            ("unmocked", "HA calls the fake doesn't answer"),
        ):
            if c["detail"].get(key):
                lines += [
                    f"- {c['name']}: {label}: `{json.dumps(c['detail'][key])[:600]}`"
                ]
    lines.append("")
    for c in failed:
        lines += [
            f"## FAIL: {c['name']}",
            "",
            "```json",
            json.dumps(c["detail"], indent=2)[:6000],
            "```",
            "",
        ]
    if results.get("console"):
        lines += [
            "## Browser console (errors / warnings)",
            "",
            "```",
            *results["console"][:200],
            "```",
            "",
        ]
    (out / "report.md").write_text("\n".join(lines))
    return len(failed)


def _raise_on_sigterm(signum: int, frame: object) -> None:
    # CI cancellation / ``kill`` sends SIGTERM: unwind through the
    # ``finally`` blocks so no backend / proxy / Chrome is left behind.
    raise KeyboardInterrupt


def cmd_verify(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "screenshots").mkdir(parents=True)
    try:
        chrome = find_chrome(args.chrome)
        ensure_spa_build(args.build)
        ensure_node_deps()
    except (HarnessError, subprocess.CalledProcessError) as exc:
        log(f"SETUP FAILED: {exc}")
        return 2
    log(f"chrome: {chrome}")
    stack = _make_stack(args, out)
    started = time.monotonic()
    try:
        try:
            stack.start()
        except HarnessError as exc:
            log(f"SETUP FAILED: {exc} — see {out / 'logs'}")
            return 2
        log(
            f"stack up: {stack.proxy_url}{stack.prefix}/ (backend {stack.backend_url}, supervisor {stack.supervisor_url})"
        )
        env = {
            **os.environ,
            "INGRESS_PROXY": stack.proxy_url,
            "INGRESS_BACKEND": stack.backend_url,
            "INGRESS_SUPERVISOR": stack.supervisor_url,
            "INGRESS_PREFIX": stack.prefix,
            "INGRESS_OUT": str(out),
            "INGRESS_CHROME": chrome,
            "INGRESS_ROUTER_TS": str(CLIENT / "src" / "router.ts"),
            "INGRESS_ONLY": args.only or "",
        }
        node = subprocess.Popen(
            ["node", str(CHECKS / "ingress.mjs")],
            cwd=CHECKS,
            env=env,
            start_new_session=True,
        )
        stack.procs.append(
            ("checks", node)
        )  # torn down with the stack if we're interrupted
        returncode = node.wait()
    finally:
        stack.stop(keep_data=args.keep_data)
    results_file = out / "results.json"
    if not results_file.is_file():
        log(
            f"checks crashed (exit {returncode}) before writing results — see output above"
        )
        return 2
    results = json.loads(results_file.read_text())
    failed = _write_report(out, results, stack)
    total = len(results.get("results", []))
    log(
        f"{total - failed}/{total} checks passed in {time.monotonic() - started:.0f}s — report: {out / 'report.md'}"
    )
    if failed or returncode != 0:
        log(
            "FAILED: " + ", ".join(c["name"] for c in results["results"] if not c["ok"])
        )
        return 1
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ensure_spa_build(args.build)
    stack = _make_stack(args, out)
    try:
        stack.start()
        log(
            f"open {stack.proxy_url}/frame (HA-like iframe) or {stack.proxy_url}{stack.prefix}/"
        )
        log(
            f"switch user: {stack.proxy_url}/_proxy/user?name=bob|carol|none — logs in {out / 'logs'}"
        )
        signal.sigwait({signal.SIGINT, signal.SIGTERM})
    finally:
        stack.stop(keep_data=args.keep_data)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="haos ingress UI harness")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("verify", "up"):
        p = sub.add_parser(name)
        p.add_argument(
            "--out", default=str(Path(tempfile.gettempdir()) / "haos-ingress-ui")
        )
        p.add_argument("--build", choices=("auto", "always", "never"), default="auto")
        p.add_argument(
            "--python", default=sys.executable, help="interpreter with the backend deps"
        )
        p.add_argument(
            "--supervisor-port", type=int, default=0, help="0 = pick a free port"
        )
        p.add_argument(
            "--backend-port", type=int, default=0, help="0 = pick a free port"
        )
        p.add_argument("--proxy-port", type=int, default=0, help="0 = pick a free port")
        p.add_argument(
            "--keep-data", action="store_true", help="keep the backend data dir"
        )
        if name == "verify":
            p.add_argument(
                "--chrome", help="Chrome/Chromium binary (else $CHROME_PATH, then PATH)"
            )
            p.add_argument(
                "--only", help="comma-separated check names to run (setup always runs)"
            )
    args = parser.parse_args(argv)
    if args.cmd == "up":
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        return cmd_up(args)
    try:
        return cmd_verify(args)
    except KeyboardInterrupt:
        log("interrupted — stack torn down")
        return 130


if __name__ == "__main__":
    sys.exit(main())
