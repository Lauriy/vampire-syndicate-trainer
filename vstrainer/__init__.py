"""Memory trainer for Vampire Syndicate: Gangs of Moonfall."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vstrainer")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0+unknown"
