# Decision Log (ADR)

Format: Decision / Why / Alternatives / Impact.
ADRs are immutable. To change a decision, write a new ADR that supersedes it.

## ADR-0001 - Sequential integer primary keys
- Decision: Entities use sequential int PKs (1, 2, 3, ...).
- Why: Deterministic, human-debuggable, naturally ordered (helps CDC later).
- Alternatives: UUIDs - realistic but hard to eyeball; addable later as extra columns (non-breaking).
- Impact: All entity models; simple downstream joins. PAYOFF: makes FK generation trivially deterministic (ADR-0014).

## ADR-0002 - Money as decimal
- Decision: Monetary values are decimal.Decimal in models; writers serialize with exactly 2 decimal places.
- Why: Floats corrupt money; warehouses expect DECIMAL semantics.
- Alternatives: Integer cents (precise, less readable); float (rejected).
- Impact: product.unit_price (Milestone 3a); payment.amount (3b); csv_writer serialization rules.

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
- Impact: Seed manager, manifest, future chunking/parallelism.

## ADR-0007 - Schema versioning
- Decision: Field definitions live in src/models/; each entity has SCHEMA_VERSION; additive change = minor bump; removal/type change = major bump requiring approval.
- Why: Downstream projects must detect schema change instead of silently breaking.
- Alternatives: None (silent drift unacceptable).
- Impact: All models; manifests record schema_version.

## ADR-0008 - Configuration format
- Decision: YAML files, loaded and validated into typed Python objects.
- Why: Human-readable, comment-friendly, git-diffable; config is itself a contract.
- Alternatives: JSON (no comments), TOML (fine, less common in DE tooling).
- Impact: config/ layout; config loader.

## ADR-0009 - Logging contract
- Decision: stdlib logging only (never print). One logger per module via logging.getLogger(__name__). Console = INFO; per-run file logs/run_<run_id>.log = DEBUG. Message format "[STAGE] message key=value". Stage tags: STARTUP, CONFIG, SEED, GENERATION, WRITER, MANIFEST, SUMMARY. configure_logging() called once, in main.py. Unhandled failures logged via run() wrapper with exit codes 0/1/130.
- Why: Logging conventions are a contract; crashes must reach the run log.
- Alternatives: print; structlog (deferred); loguru (external dep without need).
- Impact: Every module. Rules: no per-record INFO logging; lazy %-formatting; no secrets.

## ADR-0010 - Relative paths resolve against the project root
- Decision: All relative paths from configuration resolve against the project root (parent of src/), via src/core/paths.py.
- Why: A run must behave identically no matter where it was started from.
- Alternatives: Resolve against cwd (rejected); absolute paths required (rejected).
- Impact: src/core/paths.py; config loading; all writers and scripts.

## ADR-0011 - Output directory layout
- Decision: Generated datasets live in data/generated/ as {entity}_{date}.{format}; run manifests live in data/generated/_manifests/run_<run_id>.json.
- Why: One predictable layout is a contract downstream projects rely on.
- Alternatives: Manifests next to data files (pollutes globs); manifests under logs/ (mixes streams).
- Impact: manifest.py, main.py; all future phases keep this layout.

## ADR-0012 - Configurable history span (generation.history)
- Decision: Optional duration string ('30d', '4m', '2y', '1y6m'; y=365d, m=30d, d=1d; each unit once) parsed to history_days; default '3y' preserves pre-0.3.0 behavior; manifest records history_days.
- Why: History depth is a business decision; one knob for all future entities.
- Alternatives: Calendar-accurate months via dateutil (rejected: dependency); integer days (rejected: readability).
- Impact: config.py, all date-generating entities. Backward-compatible addition.

## ADR-0013 - Overwrite policy: rebuild-in-place
- Decision: Re-running with the same reference_date rebuilds (overwrites) the dataset files; the writer logs "[WRITER] overwriting existing file=..." whenever it overwrites. Older manifests remain historical records of past runs but may no longer match files on disk.
- Why: Matches how lakehouses treat partition overwrites; keeps the run -> tweak -> run loop fast; the latest manifest always describes the current files.
- Alternatives: Refuse to overwrite (safer, tedious); run_id in filenames (strongest audit but breaks ADR-0004 naming contract - would need formal approval and causes file accumulation).
- Impact: csv_writer logs overwrites. NOTE: adopted as the recommended default after being proposed; user may supersede with option B or C before Milestone 3b. Atomic writes (temp file + os.replace) planned as a Phase 3 writer upgrade.

## ADR-0014 - Generation order and FK-by-parent-id-range
- Decision: Entities are generated in dependency order (parents before children): customer, supplier -> product -> (3b: employee, order, order_item, payment). A child generator receives the parent record count in its constructor and picks FK values from its own seeded rng over 1..parent_count. Dataset composition rules (e.g. product requires supplier) are validated in main.py before any file is written.
- Why: With sequential PKs (ADR-0001), parent ids are exactly 1..N, so a child can guarantee referential integrity WITHOUT holding parent records in memory - deterministic and scale-friendly (works at 100M rows too).
- Alternatives: Pass actual parent id lists (memory-heavy at scale); post-hoc FK fixing (violates 'correct by construction'); global shared context object (premature - revisit if entities need parent ATTRIBUTES, not just ids).
- Impact: All future child generators; main.py generation blocks ordered by dependency; registry-based dispatch refactor planned for 3b when entity count grows.
