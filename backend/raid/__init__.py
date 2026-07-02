"""RAID Omega — rebuilt trading architecture.

This package holds the deterministic, typed, multi-strategy engine that replaces the
single LLM-string-parsing pipeline. It is built ALONGSIDE the legacy engine
(worker/brain/executor/scanner) and is not wired into the live loop until the Phase 5
cutover, so the running paper bot stays untouched and every phase is reversible.

Design rules (from the rebuild spec):
  * No prose parsing in the financial path — strict typed candidates only.
  * Malformed candidates are REJECTED, never silently repaired.
  * AI has zero authority over prices, sizes, risk, or execution.
  * Fail closed when state is uncertain.
"""

__version__ = "0.1.0"
