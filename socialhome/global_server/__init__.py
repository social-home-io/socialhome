"""Global Federation Server (GFS) — optional relay for space federation (§24)."""

from .domain import ClusterNode, GfsInstance, GfsSpace, GfsSubscriber
from .rtc_transport import RtcSession
from .server import create_gfs_app

__all__ = [
    "ClusterNode",
    "GfsInstance",
    "GfsSpace",
    "GfsSubscriber",
    "RtcSession",
    "create_gfs_app",
]
