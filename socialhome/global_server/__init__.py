"""Global Federation Server (GFS) — the directory and opaque channel relay for space federation (§24)."""

from .domain import ClusterNode, GfsInstance, GfsSpace
from .rtc_transport import RtcSession
from .server import create_gfs_app

__all__ = [
    "ClusterNode",
    "GfsInstance",
    "GfsSpace",
    "RtcSession",
    "create_gfs_app",
]
