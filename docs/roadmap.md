# Roadmap & Progress Tracker

## PHASE 0 - Foundations
- [x] Architecture review, decisions (ADR-0001..0009)
- [x] Project skeleton + logging (Milestone 1)
- [x] Clean git history, docs in place

## PHASE 1 - First vertical slice
- [x] Config, seeds, customer generator, CSV writer, manifest, tests
- [x] Configurable history span (ADR-0012)
- [x] Crash logging + exit codes; file-lock error message

## PHASE 2 - Relational domain
- [x] Supplier + product with FK (Milestone 3a)
- [x] Employee, order, order_item, payment with business rules (Milestone 3b)
- [x] Review entity - user-built, reviewed (Milestone 3c)
- [x] 500k-order stress run (intentional; informs Phase 7)

## PHASE 3 - Formats & layout - COMPLETE
- [x] Writer interface (ABC) + factory (ADR-0017) (Milestone 4a)
- [x] JSON / NDJSON writer (Milestone 4a)
- [x] Parquet writer, pinned native schema (ADR-0018) (Milestone 4b)
- [x] Hive-style partitioned output (ADR-0019) (Milestone 4c)
- [x] Atomic writes inside the Writer lifecycle (ADR-0020) (Milestone 4c)

## PHASE 4 - Impurity engine - COMPLETE (platform 1.0.0)
- [x] Engine skeleton + NULL injection (ADR-0021) (5a)
- [x] Duplicate + invalid-FK rules, fixed chain (ADR-0022) (5b)
- [x] Out-of-range + schema-drift rules (ADR-0023) (5c)

## PHASE 5 - Temporal & incremental
- [x] Window engine: daily/monthly keys, bucketing, event_date partitions (ADR-0024) (6a)
- [x] Fact/dimension split; snapshot vs windowed output (ADR-0024) (6a)
- [x] Temporal integrity across windows (inherited from ADR-0016) (6a)
- [x] Manifest v3: layout + files[] (ADR-0024) (6a)
- [ ] Incremental change stream: INSERT/UPDATE/DELETE (Milestone 6b)
- [ ] CDC-style change log format (Milestone 6b)

## PHASE 6 - Event streams (event_time vs ingestion_time, late/duplicate/out-of-order)
## PHASE 7 - Scale (per-window independent generation, streaming context, chunking, multiprocessing)
## PHASE 8 - Production readiness (packaging, CLI, metadata catalog, CI)
