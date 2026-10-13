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
- [x] Employee generator (Milestone 3b)
- [x] Order generator (FK customer + employee, temporal rule) (Milestone 3b)
- [x] Order item generator (FK order + product, >= 1 per order) (Milestone 3b)
- [x] Payment generator (completed orders, amount = order total) (Milestone 3b)
- [x] Review generator (completed purchases, unique customer/product pairs, post-purchase dates)
- [x] main.py composition validation + context wiring (ADR-0015) (Milestone 3b)
- [x] Business rules by construction + tests (ADR-0016), including review purchase eligibility

## PHASE 3 - Formats & layout (writer interface, JSON, Parquet, Hive partitions, atomic writes)
## PHASE 4 - Impurity engine (nulls, duplicates, invalid FKs, schema drift - breaks ADR-0016 rules on purpose)
## PHASE 5 - Temporal & incremental (window engine, granularity, INSERT/UPDATE/DELETE, CDC-style)
## PHASE 6 - Event streams (event_time vs ingestion_time, late/duplicate/out-of-order)
## PHASE 7 - Scale (streaming context, chunking, multiprocessing)
## PHASE 8 - Production readiness (packaging, CLI, metadata catalog)
