"""``research/data`` — read-only daily loader + contract reference data.

Daily bars are read from a COPY of the api's on-disk LEAN-format bars (the same
format ``services/data/bar_sync.py`` writes). This package NEVER imports
``bar_sync`` (it pulls ``ib_async``, unavailable in the research/CI venv) and
NEVER mounts the live ``trading_lean_data`` volume writable — it reads an
immutable copy (design §4.2, R1). The on-disk format is re-implemented here from
the documented spec, not imported.
"""

from __future__ import annotations
