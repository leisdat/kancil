"""Kancil — agent browser + DevTools for Termux.

Engines:
  static     pure Python, always works (no JS/screenshot)
  playwright real Chromium when installed (JS, screenshots, real storage)

Agent API:
  from kancil import Kancil
  b = Kancil(engine="static")
  b.open("https://example.com")   # -> {"success": True, ...}
"""
from .api import Kancil, ok, fail

__version__ = "3.7.0"
__all__ = ["Kancil", "ok", "fail", "__version__"]
