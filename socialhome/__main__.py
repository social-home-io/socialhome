"""Entry point: ``python -m socialhome``."""

from aiohttp import web

from .access_log import RedactingAccessLogger
from .app import create_app
from .config import Config

if __name__ == "__main__":
    cfg = Config.from_env()
    web.run_app(
        create_app(cfg),
        host=cfg.listen_host,
        port=cfg.listen_port,
        access_log_class=RedactingAccessLogger,
    )
