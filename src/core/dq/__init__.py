"""Data Quality (DQ) harness — Milestone 7a.

Consumer-side checks over generated output trees (data/generated, data/quality).
See docs/decisions.md ADR-025: the harness reads files + config only; it never
imports generator, writer, or impurity internals.
"""
