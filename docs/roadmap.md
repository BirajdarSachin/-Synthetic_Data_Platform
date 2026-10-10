# Roadmap & Progress Tracker

## PHASE 0 - Foundations
- [x] Architecture review, decisions D1-D8 (ADR-0001..0008)
- [x] Project skeleton + logging (Milestone 1)
- [x] Clean git history, docs in place

## PHASE 1 - First vertical slice
- [x] YAML config loading (+ CONFIG logging)
- [x] Seed manager (+ SEED logging)
- [x] Customer generator (+ GENERATION logging)
- [x] CSV writer (+ WRITER logging with output paths)
- [x] Run manifest (+ MANIFEST / SUMMARY logging)
- [ ] Unit + reproducibility tests (tick after pytest passes)

## PHASE 2 - Relational domain (Milestone 3+)
- [ ] Product generator
- [ ] Order generator (references customer)
- [ ] Order item generator (references order + product)
- [ ] Payment generator (references order)
- [ ] Referential integrity tests

## PHASE 3 - Formats & layout (writer interface, JSON, Parquet)
## PHASE 4 - Impurity engine (nulls, duplicates, invalid FKs, schema drift)
## PHASE 5 - Temporal & incremental (daily/monthly, INSERT/UPDATE/DELETE, CDC-style)
## PHASE 6 - Event streams (event_time vs ingestion_time, late/duplicate/out-of-order)
## PHASE 7 - Scale (chunking, multiprocessing)
## PHASE 8 - Production readiness (packaging, CLI, metadata catalog)
