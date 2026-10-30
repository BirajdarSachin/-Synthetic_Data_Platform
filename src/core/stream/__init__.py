"""Event stream module (Milestone 6c, ADR-030).

Consumer-side streaming layer over the CDC journals written by Milestone 6b
(ADR-029). Two capabilities, one contract:

    replay  -- fold the journal chain (initial + deltas, ordered by
               (window, seq)) back into keyed state, and VERIFY the folded
               state against the snapshot trees the platform wrote.
    export  -- re-emit the same events as one append-only, offset-addressed
               stream per entity (Kafka-log-style envelope), deterministic
               and byte-identical on re-runs.

Zero imports from generator/writer/impurity internals (ADR-025 / ADR-029
consumer precedent): the stream layer speaks only to the documented layouts
data/cdc (journals), data/generated (snapshots) and its own data/streams.
"""
