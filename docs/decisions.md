# Decision Log (ADR)

Format: Decision / Why / Alternatives / Impact.
ADRs are immutable. To change a decision, write a new ADR that supersedes it.

## ADR-0001 - Sequential integer primary keys
- Decision: Sequential int PKs (1, 2, 3, ...).
- Why: Deterministic, debuggable, ordered (helps CDC later).
- Alternatives: UUIDs (harder to eyeball; addable later as extra columns).
- Impact: All models; enables ADR-0014.

## ADR-0002 - Money as decimal
- Decision: decimal.Decimal in models; writers serialize with exactly 2 decimal places.
- Why: Floats corrupt money; warehouses expect DECIMAL semantics.
- Alternatives: Integer cents; float (rejected).
- Impact: product.unit_price, order_item.unit_price, payment.amount; serialization.py.

## ADR-0003 - Time conventions
- Decision: UTC ISO-8601 timestamps; plain dates; "now" injected from config, never the real clock.
- Why: Determinism; foundation for late/out-of-order events.
- Alternatives: Real clock (non-reproducible).
- Impact: All timestamped entities; Phases 5-6.

## ADR-0004 - Naming conventions
- Decision: Entities singular snake_case; columns snake_case; files {entity}_{date}.{format}.
- Why: One unambiguous convention.
- Alternatives: Plural names.
- Impact: All datasets, files, future tables.

## ADR-0005 - Internal representation
- Decision: Frozen dataclasses; generators yield records; pandas only inside writers (Phase 3+).
- Why: Dependency-free, testable, streaming core.
- Alternatives: pandas core (rejected).
- Impact: All generators and writers.

## ADR-0006 - Reproducibility
- Decision: Master seed + SHA-256 child seeds per entity; only random.Random instances; manifest per run.
- Why: Global seeding breaks under reordering/parallelism.
- Alternatives: random.seed() global (rejected).
- Impact: Seed manager; future per-window seeds (Phase 5).

## ADR-0007 - Schema versioning
- Decision: Models own SCHEMA_VERSION; additive = minor; removal/type change = major + approval.
- Why: Downstream projects must detect change.
- Alternatives: None (silent drift unacceptable).
- Impact: All models; manifests.

## ADR-0008 - Configuration format
- Decision: YAML validated into typed frozen dataclasses.
- Why: Readable, comment-friendly, diffable.
- Alternatives: JSON, TOML.
- Impact: config/ + loader.

## ADR-0009 - Logging contract
- Decision: stdlib logging; per-module loggers; console INFO, per-run file DEBUG; "[STAGE] message key=value"; stage tags STARTUP/CONFIG/SEED/GENERATION/WRITER/MANIFEST/SUMMARY; crashes logged via run() wrapper, exit codes 0/1/130.
- Why: Contracts, greppability, forensics.
- Alternatives: print; structlog (deferred).
- Impact: Every module.

## ADR-0010 - Relative paths resolve against the project root
- Decision: paths.py anchors all relative paths at the project root.
- Why: Identical behavior from any cwd.
- Alternatives: cwd-relative (rejected).
- Impact: Config, writers, scripts.

## ADR-0011 - Output directory layout
- Decision: data/generated/{entity}_{date}.{format}; manifests in data/generated/_manifests/.
- Why: Predictable contract; manifests out of data globs.
- Alternatives: Manifests beside data (rejected).
- Impact: Writers, main.py.

## ADR-0012 - Configurable history span (generation.history)
- Decision: Optional duration string parsed to history_days; default '3y'; manifest records it.
- Why: History depth is a business decision.
- Alternatives: dateutil (rejected: dependency).
- Impact: config.py; all date-generating entities.

## ADR-0013 - Overwrite policy: rebuild-in-place
- Decision: Re-running overwrites dataset files; writer logs "[WRITER] overwriting existing file=...". Old manifests remain historical records.
- Why: Matches lakehouse partition-overwrite behavior; keeps run -> tweak -> run fast.
- Alternatives: Refuse overwrite (tedious); run_id in filenames (breaks ADR-0004).
- Impact: Writer base class. Atomic writes planned later in Phase 3.

## ADR-0014 - Generation order and FK-by-parent-id-range
- Decision: Parents generated before children; child generators receive parent counts and pick FKs from 1..parent_count with their own seeded rng; composition rules validated in main.py.
- Why: Sequential PKs make integrity correct by construction, memory-light, scale-friendly.
- Alternatives: Passing parent id lists for id-only FKs (memory-heavy); post-hoc repair (rejected).
- Impact: All child generators; main.py ordering.

## ADR-0015 - Generation context (parent attributes)
- Decision: When a rule needs parent ATTRIBUTES (not just ids), main.py extracts explicit tuples/dicts from generated parent records and passes them to the child generator constructor. Generators never import each other.
- Why: Rules like order_date >= registration_date are impossible from id ranges alone; explicit wiring keeps generators decoupled and testable.
- Alternatives: Generators reading other generators (coupling); global registry object (premature); post-hoc repair (violates correct-by-construction).
- Impact: main.py holds parent lists in memory (fine at thousands of rows; Phase 7 will stream). Context consumers: order (customer dates), order_item (product prices), payment (completed orders + totals), review (purchases with dates).

## ADR-0016 - Business rules at generation time
- Decision: Rules are encoded in generators, not checked afterwards: order_date >= customer registration_date; every order >= 1 item; item unit_price = product unit_price; quantity 1..5; payment only for completed orders, <= 1 per order, payment_date >= order_date, amount = item total; reviews reference completed purchases, <= 1 per customer/product pair, anchored to the latest purchase, review_date strictly after it and <= reference_date; same-day (reference_date) purchases are ineligible. Impossible requests raise ValueError loudly.
- Why: Data valid by construction is the platform's core value; downstream projects practice VALIDATING these rules.
- Alternatives: Separate repair layer (deferred to Phase 4 impurity engine, which breaks these rules under controlled config).
- Impact: order/order_item/payment/review generators; tests assert each rule.

## ADR-0017 - Writer interface, NDJSON format, shared serialization
- Decision: src/writers/base.py defines Writer (ABC). write() is a template method owning the file lifecycle (empty-stream rejection, overwrite log, locked-file message, count/bytes log); subclasses implement only _write_records and may override _open_file. A factory get_writer(format) in writers/__init__ maps format strings to implementations; main.py never imports concrete writers. JSON output is NDJSON: one JSON object per line, UTF-8, ensure_ascii=False. Serialization rules (dates -> ISO-8601, Decimal -> 2dp strings) live in one module (serialization.py) used by every writer.
- Why: The second format (JSON) made the promised abstraction earn its place (deferred since Milestone 2). The template method removes four duplicated concerns before they drift. NDJSON streams line by line - flat memory, line-readable by Spark/pandas/stream consumers, truncation-tolerant - unlike a single JSON array which forces whole-file parsing. One serialization module keeps ADR-0002/0003 rules identical across formats.
- Alternatives: Single JSON array (rejected: no streaming); pandas.to_json (rejected: pandas stays out of core, ADR-0005); per-writer serialization (rejected: drift risk); typing.Protocol instead of ABC (equally valid; ABC chosen for a clear contract and shared hooks).
- Impact: config accepts output.format: csv|json; file extension follows format via ADR-0004; future writers (parquet, avro, db) implement Writer; atomic writes will land inside the shared lifecycle and benefit every format at once.
