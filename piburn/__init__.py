"""Tools for preparing Ubuntu Server microSD cards for Raspberry Pi clusters."""

from .cli import BurnError, main

__all__ = ["BurnError", "main"]
__version__ = "0.1.0"
