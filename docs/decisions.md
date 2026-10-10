# Decision Log (ADR)

Format: Decision / Why / Alternatives / Impact.
ADRs are immutable. To change a decision, write a new ADR that supersedes it.

## ADR-0001 - Sequential integer primary keys
- Decision: Entities use sequential int PKs (1, 2, 3, ...).
- Why: Deterministic, human-debuggable, naturally ordered (helps CDC later).
- Alternatives: UUIDs - realistic but hard to eyeball; addable later as extra columns (non-breaking).
- Impact: All entity models; simple downstream joins.

## ADR-0002 - Money as decimal
- Decision: Monetary values are decimal.Decimal in models; writers serialize with 2 decimal places.
- Why: Floats corrupt money; warehouses expect DECIMAL semantics.
- Alternatives: Integer cents (precise, less readable); float (rejected).
- Impact: Order/payment schemas; writer formatting.

## ADR-0003 - Time conventions
- Decision: Timestamps are UTC ISO-8601; business dates are plain dates; "now" is injected from config, never the real clock.
- Why: Determinism, no timezone bugs, foundation for late/out-of-order events.
- Alternatives: Real clock (non-reproducible, rejected).
- Impact: Every timestamped entity; incremental and event phases.

## ADR-0004 - Naming conventions
- Decision: Entities singular snake_case (customer, order, order_item); columns snake_case; files {entity}_{date}.{format}.
- Why: One unambiguous convention across models, files, and future tables.
- Alternatives: Plural names (equally valid - we pick once).
- Impact: All datasets, file names, warehouse table names.

## ADR-0005 - Internal representation
- Decision: Records are plain dataclasses; generators yield records one at a time; pandas allowed only inside writers (Phase 3+).
- Why: Dependency-free, testable core that streams to any scale without rewriting.
- Alternatives: pandas core (slow per-row, heavy coupling - rejected).
- Impact: All generators and writers.

## ADR-0006 - Reproducibility
- Decision: One master seed in config; child seeds derived per entity; only random.Random instances (no global random state); every run writes a manifest.
- Why: Global seeding breaks under code reordering and parallelism; manifests make runs auditable.
- Alternatives: random.seed() global state (fragile - rejected).
- Impact: Seed manager (Milestone 2), manifest, future chunking/parallelism.

## ADR-0007 - Schema versioning
- Decision: Field definitions live in src/models/; each entity has SCHEMA_VERSION; additive change = minor bump; removal/type change = major bump requiring approval.
- Why: Downstream projects must detect schema change instead of silently breaking.
- Alternatives: None (silent drift unacceptable).
- Impact: All models; manifests record schema_version.

## ADR-0008 - Configuration format
- Decision: YAML files, loaded and validated into typed Python objects.
- Why: Human-readable, comment-friendly, git-diffable; config is itself a contract.
- Alternatives: JSON (no comments), TOML (fine, less common in DE tooling).
- Impact: config/ layout; config loader (Milestone 2).

## ADR-0009 - Logging contract
- Decision: stdlib logging only (never print). One logger per module via logging.getLogger(__name__). Console = INFO; per-run file logs/run_<run_id>.log = DEBUG. Message format "[STAGE] message key=value". Stage tags: STARTUP, CONFIG, SEED, GENERATION, WRITER, MANIFEST, SUMMARY. configure_logging() called once, in main.py.
- Why: Logging conventions are a contract; consistent tags make runs greppable; per-run files pair with the future manifest via run_id.
- Alternatives: print (no levels/files/structure); structlog/JSON logging (deferred until machine-parsing is needed); loguru (external dep without need).
- Impact: Every module from Milestone 2 on. Rules: no per-record INFO logging; lazy %-formatting; no secrets.

## ADR-0010 - Relative paths resolve against the project root
- Decision: All relative paths from configuration resolve against the project root (parent of src/), via src/core/paths.py. The cwd is logged at startup for forensics but never used for resolution.
- Why: A run must behave identically no matter where it was started from; resolving against cwd causes "files landed in the wrong place" bugs.
- Alternatives: Resolve against cwd (rejected: behavior varies by launch location); require absolute paths (rejected: not portable).
- Impact: src/core/paths.py; config loading; all future writers and scripts.

## ADR-0011 - Output directory layout
- Decision: Generated datasets live in data/generated/ as {entity}_{date}.{format}; run manifests live in data/generated/_manifests/run_<run_id>.json.
- Why: One predictable layout is a contract downstream projects rely on; a separate _manifests/ folder keeps metadata out of data-file glob patterns.
- Alternatives: Manifests next to data files (pollutes globs); manifests under logs/ (mixes human logs with machine metadata).
- Impact: src/metadata/manifest.py, main.py; all future phases keep this layout.
