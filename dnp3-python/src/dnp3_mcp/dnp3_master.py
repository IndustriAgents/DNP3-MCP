"""Compatibility import; implementation lives in adapters.dnp3_master."""

import sys

from .adapters import dnp3_master as _implementation

sys.modules[__name__] = _implementation
