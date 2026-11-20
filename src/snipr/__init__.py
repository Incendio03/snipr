"""Snipr: streamer VODs in, reviewed short vertical clips out."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("snipr")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
