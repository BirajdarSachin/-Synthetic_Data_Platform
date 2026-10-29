"""CDC — incremental change stream + change log (Milestone 6b).

Consumer-side module (ADR-025 precedent): reads windowed output trees and
config only; never imports generator, writer, or impurity internals.
See docs/decisions.md ADR-029 for the diff-based design, the JSONL event
contract, and the guards.
"""
