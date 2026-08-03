"""Sentience Governor — open-tier governance wrapper for enterprise AI agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sentience-governor")
except PackageNotFoundError:  # pragma: no cover — source tree without install
    __version__ = "0.0.0+unknown"
