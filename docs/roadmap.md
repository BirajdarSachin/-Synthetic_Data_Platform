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

## PHASE 4 - Impurity engine
- [x] Engine skeleton: stream transform, quality config, guard, seeds (ADR-0021) (Milestone 5a)
- [x] NULL injection rule + format behavior (CSV empty cell, JSON null, Parquet typed null) (Milestone 5a)
- [ ] Duplicate record rule (Milestone 5b)
- [ ] Invalid FK reference rule (Milestone 5b)
- [ ] Schema drift (missing/extra columns) (Milestone 5c)
- [ ] Out-of-range / invalid values (Milestone 5c)

## PHASE 5 - Temporal & incremental (window engine, granularity, INSERT/UPDATE/DELETE, CDC-style)
## PHASE 6 - Event streams (event_time vs ingestion_time, late/duplicate/out-of-order)
## PHASE 7 - Scale (streaming context, chunking, multiprocessing)
## PHASE 8 - Production readiness (packaging, CLI, metadata catalog)
