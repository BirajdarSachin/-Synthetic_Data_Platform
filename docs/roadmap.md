# Roadmap & Progress Tracker

## PHASE 0 - Foundations
- [x] Architecture review, decisions (ADR-0001..0009)
- [x] Project skeleton + logging (Milestone 1)
- [x] Clean git history, docs in place

## PHASE 1 - First vertical slice
- [x] YAML config loading (+ CONFIG logging)
- [x] Seed manager (+ SEED logging)
- [x] Customer generator (+ GENERATION logging)
- [x] CSV writer (+ WRITER logging with output paths)
- [x] Run manifest (+ MANIFEST / SUMMARY logging)
- [x] Unit + reproducibility tests
- [x] Configurable history span (ADR-0012)
- [x] Crash logging + exit codes; file-lock error message

## PHASE 2 - Relational domain
- [x] Supplier generator (Milestone 3a)
- [x] Product generator with FK to supplier (Milestone 3a)
- [x] Referential integrity tests (Milestone 3a)
- [ ] Employee generator (Milestone 3b)
- [ ] Order generator (FK customer + employee) (Milestone 3b)
- [ ] Order item generator (FK order + product) (Milestone 3b)
- [ ] Payment generator (FK order, Decimal money) (Milestone 3b)
- [ ] main.py dispatch refactor (registry) (Milestone 3b)
- [ ] Business rules (payment after order etc.) (Milestone 3b)

## PHASE 3 - Formats & layout (writer interface, JSON, Parquet, atomic writes)
## PHASE 4 - Impurity engine (nulls, duplicates, invalid FKs, schema drift)
## PHASE 5 - Temporal & incremental (daily/monthly, INSERT/UPDATE/DELETE, CDC-style)
## PHASE 6 - Event streams (event_time vs ingestion_time, late/duplicate/out-of-order)
## PHASE 7 - Scale (chunking, multiprocessing)
## PHASE 8 - Production readiness (packaging, CLI, metadata catalog)
