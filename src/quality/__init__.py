"""Impurity engine - deliberately introduces configurable data problems.

Position (ADR-0021): a stream transform between generators and writers.
Generators and models are untouched; corruption never feeds generation
context (local corruption only). Absent quality config = clean output.
"""
