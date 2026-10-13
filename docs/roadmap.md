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

## PHASE 3 - Formats & layout
- [x] Writer interface (ABC) + factory (ADR-0017) (Milestone 4a)
- [x] JSON / NDJSON writer (Milestone 4a)
- [x] Parquet writer (pandas + pyarrow, writers only) (Milestone 4b - user-driven)
- [ ] Hive-style partitioned output (Milestone 4c)
- [ ] Atomic writes inside the Writer lifecycle (Milestone 4c)

## PHASE 4 - Impurity engine (nulls, duplicates, invalid FKs, schema drift - breaks ADR-0016 rules on purpose)
## PHASE 5 - Temporal & incremental (window engine, granularity, INSERT/UPDATE/DELETE, CDC-style)
## PHASE 6 - Event streams (event_time vs ingestion_time, late/duplicate/out-of-order)
## PHASE 7 - Scale (streaming context, chunking, multiprocessing)
## PHASE 8 - Production readiness (packaging, CLI, metadata catalog)
