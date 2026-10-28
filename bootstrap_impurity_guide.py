#!/usr/bin/env python3
"""bootstrap_impurity_guide.py -- create docs/impurity_guide.md.

House-style bootstrap (NEW_FILES semantics):
  * create-if-missing; NEVER overwrite an existing non-empty file
  * empty crash artifacts are healed ([HEAL])
  * --dry-run supported; nothing is written in dry-run mode
  * UPDATED_FILES: none -- this is a doc-only bootstrap, nothing existing is modified

What it delivers:
  docs/impurity_guide.md -- Impurity Engine Operator Guide for the current build
  (platform 1.0.0 + window engine, post-Milestone 6a). Operator-level only:
  how to trigger each corruption condition (null / invalid_fk / out_of_range /
  drift / duplicate) via config, run, and validate. No platform code changes.

Exit codes: 0 = created/healed/skipped, 1 = precondition or write failure,
            130 = interrupted (Ctrl+C).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

GUIDE_CONTENT = r'''# Impurity Engine — Operator Guide

> **Build:** platform 1.0.0 + window engine (post-Milestone 6a). Everything in this guide works on the current checkout — **no new code is needed**.
> **Purpose:** teach you, step by step, how to *operate* the impurity engine — how to switch it on, how to trigger each corruption condition from config, what a run produces, and how to prove it did what you expected.
> **Contract:** config-driven, deterministic, and safe. A quality run can never touch your clean baseline, and every corrupted dataset is byte-for-byte reproducible.

---

## 0. TL;DR — the cheat sheet

Five rule types, one fixed chain, one config block. You trigger corruption by adding a `quality:` block with rules to an entity's config and enabling it. Nothing else changes: same generators, same writers, same entry point, same commands.

| # | Rule | Minimal trigger | What it does to a record | How you detect it afterwards |
|---|------|-----------------|--------------------------|------------------------------|
| 1 | `null` | `type: null`, `rate:`, `columns:` | Sets the listed columns to `None` | Empty field (CSV) / `null` (NDJSON, Parquet) |
| 2 | `invalid_fk` | `type: invalid_fk`, `rate:`, `columns:` | Replaces a foreign key with an impossible id | Anti-join against the parent entity |
| 3 | `out_of_range` | `type: out_of_range`, `rate:`, `columns:` | Pushes a value past its declared domain | Predicate (e.g. `quantity < 0`) |
| 4 | `drift` | `type: drift`, `rate:`, `columns:` | Shifts a value progressively along the stream | Compare early-vs-late row averages |
| 5 | `duplicate` | `type: duplicate`, `rate:` | Emits an exact copy of the final corrupted record | Full-row duplicate count |

Facts that never change, no matter how you configure the engine:

- **Chain order is fixed in code:** `null → invalid_fk → out_of_range → drift → duplicate`. The order of `rules:` entries in YAML is irrelevant — the engine always runs the chain in this order.
- **Only `duplicate` changes row counts.** The first four rules transform records in place; `duplicate` appends extra rows.
- **Schema never changes.** Parquet schemas are pinned from dataclass annotations (`arrow_schema_for`); corruption writes nulls, never new types.
- **Primary keys never change.** A corrupted row keeps its PK; a duplicate repeats its PK. In windowed mode, the PK union across window files stays `1..N`.
- **Parent entities are never modified.** Corrupting `orders` never touches `customers` — corruption is *local* to the entity being written.
- **The clean baseline is untouchable.** Quality output goes to `data/quality/...`; a startup guard refuses any quality write into (or under) `data/generated`.
- **Determinism holds.** Same master seed + same config ⇒ byte-identical corrupted output, verified by per-file SHA-256 in the manifest.

---

## 1. What the impurity engine is (and is not)

The impurity engine is a **stream transform** that sits between your generators and your writers. Generators yield clean records one at a time; when impurity is enabled, each record passes through a fixed chain of corruption rules before it reaches the writer. Because the transform is streaming, memory behaviour is unchanged — you can corrupt a 10-million-row entity exactly as cheaply as a clean one, and corruption costs you no extra buffering.

The engine is a **producer of realistic bad data, not a detector**. Its job is to create the dirt that downstream Data Engineering projects will then have to survive: ETL validation layers, dedup pipelines, referential-integrity checks, data-quality tests, and monitoring all need realistic failures to chew on. That is why the rules map one-to-one onto classic real-world failure modes — optional fields left blank at the source (`null`), broken references from late-arriving or miscoded dimensions (`invalid_fk`), impossible values from unit errors and manual overrides (`out_of_range`), slowly-lying measures from drifty sensors or business logic changes (`drift`), and the same row ingested twice (`duplicate`).

It is also, deliberately, **boring to operate**. There is no plugin system, no rule ordering to reason about, no randomness you cannot reproduce. You configure five rule types with rates and columns; the engine handles ordering, seeding, isolation, and safety. If you ever feel you need a *new rule type* or a *different chain order*, that is not an operator change — it is a new pattern that needs an ADR (and possibly the §26 breaking-change protocol), because it changes output bytes and therefore manifest checksums. See §13.

Finally, the engine respects the platform's core promise: **realism by construction, determinism by design**. Every draw comes from per-rule, per-entity, per-window child RNGs derived by SHA-256 from the master seed. Nothing uses global random state, so two runs with the same config and seed produce identical corrupted files — which means you can treat corrupted datasets as first-class test fixtures, not as one-off accidents.

## 2. Where it sits in the pipeline

```
                    ┌── clean run (quality.enabled: false or block absent) ──────────────────────┐
                    │                                                                            │
  generators  ──────┤  writer  ──►  data/generated/{entity}/...     (clean baseline — sacred)    │
  (yield one        │                                                                            │
   record at a      ├── quality run (quality.enabled: true) ─────────────────────────────────────┤
   time)            │                                                                            │
                    │  QUALITY TRANSFORM  ──►  writer  ──►  data/quality/{entity}/...             │
                    │  (fixed chain:                                                             │
                    │   null → invalid_fk → out_of_range → drift → duplicate)                    │
                    └────────────────────────────────────────────────────────────────────────────┘
```

Three things this diagram tells you as an operator:

1. **Impurity is a run-level mode, driven entirely by config.** You do not pass extra CLI flags and you do not call different entry points. You flip `quality.enabled` in the YAML and run the exact same command you always run. The engine, the guard, and the output routing react to config alone.
2. **The clean baseline is a separate output root.** Clean runs keep writing `data/generated/...` exactly as before. Quality runs write the *transformed* stream to `data/quality/...`. A startup guard makes it structurally impossible for a quality run to overwrite the baseline, so you can keep both trees side by side and diff them.
3. **Corruption is local.** All context a rule needs — parent id ranges, declared domains, clean value distributions — is extracted from the *clean* records flowing through the stream, before any rule has touched them. A corrupted record never becomes context for another rule, and no other entity's data is read or modified. This is what keeps corruption cheap, debuggable, and reproducible.

## 3. Determinism: the seed tree

The platform already derives one child seed per entity from the master seed. The impurity engine extends the same scheme with two more derivations, so every rule's every draw is pinned:

| What | Seed derivation (SHA-256 over the literal string) |
|------|----------------------------------------------------|
| Entity stream (clean) | `"{master}:{entity}"` |
| Quality rule, snapshot mode | `"{master}:{entity}:quality:{rule}"` |
| Quality rule, windowed mode | `"{master}:{entity}:{window}:quality:{rule}"` |

where `{master}` is the master seed from config, `{entity}` is the entity key (e.g. `orders`), `{rule}` is the rule type name (e.g. `null`), and `{window}` is the window label (e.g. `2026-10-24` for a daily bucket). The hex digest is used as the child seed for that rule's own `random.Random` instance — never global state, matching the platform rule since Milestone 1.

Practical consequences you can rely on:

- **Rule independence.** The `null` rule on `orders` uses a different RNG than the `duplicate` rule on `orders`. Enabling or disabling one rule does not shift the draws of another — a corrupted file is stable under small config edits.
- **Config order does not matter.** Because seeds are keyed by rule *name*, listing rules in a different order in YAML changes nothing.
- **Window isolation.** Each window's corruption pattern is derived from that window's label, so `2026-10-23` and `2026-10-24` get independent (but individually reproducible) dirt. Editing one window's history span or bucketing never perturbs another window's corruption.
- **Manifest-provable reproducibility.** Every quality run writes a manifest with per-file SHA-256 checksums (§9). Run twice with the same config and seed, and the checksums match. This is your go-to proof when wiring corrupted data into a downstream project.

## 4. The fixed chain: order, locality, and why it matters

Every record passes through the stages in this order — always, regardless of how your YAML lists them:

```
clean record
   │
   ▼
 1. null          blanks listed columns (value → None)
   │
   ▼
 2. invalid_fk    replaces listed FK columns with impossible ids
   │
   ▼
 3. out_of_range  pushes listed numeric/date columns past their declared domain
   │
   ▼
 4. drift         shifts listed measure columns progressively along the stream
   │
   ▼
 5. duplicate     with probability rate, emits an exact copy of the FINAL record
   │
   ▼
corrupted record(s) → writer → data/quality/...
```

**Why this order, and why it is frozen:**

- **`null` runs first** so a blanked field is definitively blank. If a column was hit by the `null` rule, later value-based rules (`out_of_range`, `drift`) skip it — there is no value left to push or shift. This gives you predictable interaction semantics when you stack rules on the same column (§7).
- **`invalid_fk` runs before the value rules** so reference corruption is decided while the record is still structurally intact. The impossible id is drawn from a reserved band *past* the parent's clean PK range (derived from clean context), so an invalid FK is unambiguous — it can never collide with a real parent.
- **`duplicate` runs last** so a duplicate is an exact copy of the *fully corrupted* record — all earlier rules already applied. That is the realistic failure: the same bad row ingested twice, not one clean copy plus one dirty copy. It also means duplicates are detectable by simple full-row comparison.
- **The chain being frozen is a contract.** If the order were configurable, the same config could produce different bytes across platform versions and downstream projects could silently break. Instead, the order is code (an ADR-level decision, recorded in `docs/decisions.md`), and operators compose behaviour through *rates and columns*, never through ordering.

**Row counts — the one rule that moves the needle.** `null`, `invalid_fk`, `out_of_range`, and `drift` are in-place transforms: they never add or remove rows. `duplicate` is the only row-count changer: each record has probability `rate` of being emitted twice, so expected row count is `N × (1 + rate)`. Actual counts vary within Bernoulli noise (± a few standard deviations at these sample sizes); the exact number is logged by the writer layer and recorded in the manifest. Remember the writer's standing rule: empty streams are rejected — if a quality run would write zero rows, it fails loudly rather than producing a silently empty file.

**Locality — corruption never leaks.** Rules read context (parent counts, domains, distributions) only from clean records *of the entity being written*, extracted upstream of the transform. Two operator-visible guarantees follow: (1) corrupting a fact entity leaves its dimensions byte-identical, so you can mix clean dimensions with corrupted facts in a downstream test; (2) no rule's decision depends on another rule's output, which is what makes the seed tree in §3 sufficient for full reproducibility.

## 5. Configuring impurity: the `quality:` block

Impurity is configured exactly like everything else in the platform: in YAML, validated at startup, logged under `[CONFIG]`. The block is named `quality:` — the same word the engine uses everywhere (log tag `[QUALITY]`, output root `data/quality`, manifest section). The column names in the examples below follow the Milestone-3/4 relational schema (e.g. `orders`: `order_id`, `customer_id`, `order_date`, `total_amount`, `quantity`, `coupon_code`); swap in your entity's exact field names where they differ.

### 5.1 Canonical block (annotated)

```yaml
# inside an entity's config (example: orders)
quality:
  enabled: true                 # run-level switch. false or absent = clean run
  output:
    root: data/quality         # where corrupted output goes (default data/quality)
  rules:                        # list of rules — ORDER HERE IS IRRELEVANT,
    - type: null                #   the engine always runs the fixed chain
      rate: 0.03                #   probability per record, per listed column
      columns: [coupon_code]    #   which columns the rule may touch
    - type: invalid_fk
      rate: 0.005
      columns: [customer_id]    #   must be FK fields of this entity
    - type: out_of_range
      rate: 0.002
      columns: [quantity]       #   numeric/date fields with a declared domain
    - type: drift
      rate: 0.01
      columns: [total_amount]   #   measure columns (decimals, counts)
    - type: duplicate           #   whole-record rule: no `columns` key
      rate: 0.01
```

### 5.2 Keys, one table

| Key | Where | Required | Meaning |
|-----|-------|----------|---------|
| `quality` | entity block | no | Absent ⇒ clean run for that entity. |
| `quality.enabled` | quality block | yes (when block present) | `true` ⇒ this run writes corrupted output to the quality root. `false` ⇒ clean run. |
| `quality.output.root` | quality block | no | Where the corrupted stream is written. Default `data/quality`. **Guard:** must not be `data/generated` or inside it — startup fails otherwise (§9). |
| `quality.rules` | quality block | yes (when enabled) | List of rule objects. May be empty ⇒ run behaves like a clean run into the quality root. |
| `type` | rule | yes | One of `null`, `invalid_fk`, `out_of_range`, `drift`, `duplicate`. Anything else ⇒ startup validation error. |
| `rate` | rule | yes | Probability in `[0.0, 1.0]`. Required — no silent defaults, matching the platform's no-hidden-behaviour rule. Out-of-range ⇒ startup validation error. |
| `columns` | rule | except `duplicate` | Field names the rule may touch. A name that is not a field of the entity's record ⇒ startup validation error. Duplicate rules: omit it — the rule copies whole records. |

### 5.3 Enabling and disabling — the two-config pattern

Because impurity is config-driven, the cleanest operator habit is **two config files** derived from the same base:

```text
configs\
  base.yaml               # the clean generator config you already run
  base_quality.yaml       # copy of base + quality: blocks on the entities you want dirty
```

To produce a corrupted dataset: run the platform exactly as you always do, pointing at `base_quality.yaml`. To produce a clean baseline: point at `base.yaml`. Same command shape, same seeds, same manifest machinery — only the config differs, which is precisely the reproducibility story you want when a downstream project consumes both trees.

To disable impurity temporarily, set `quality.enabled: false` (or comment the block out). To disable one rule, delete its entry — because rule seeds are keyed by rule name, removing the `drift` rule does not perturb the `null` rule's draws, and vice versa. This makes A/B comparisons trivially stable: with vs without one rule, all other corruption identical.

### 5.4 What startup validates (fail-fast list)

All of these are caught before a single record is generated, logged under `[CONFIG]`, and exit the run with code `1`:

- unknown rule `type`;
- missing `rate`, or `rate` outside `[0.0, 1.0]`;
- missing `columns` for a rule that needs them, or a column name that is not a field of the entity's record;
- `columns` supplied to a `duplicate` rule;
- quality output root set to (or under) `data/generated` — the clean-baseline guard;
- `invalid_fk` declared on a column that is not a foreign key of the entity;
- `invalid_fk` on an entity whose parent has zero rows in this run (there would be no clean range to violate) — generate parents first.

## 6. Rule reference — triggering each corruption condition

Each section follows the house teaching shape: what / why / config / minimal trigger / worked example / what you will see / how to validate. All snippets are complete — paste them into an entity's `quality:` block and run.

---

### 6.1 `null` — blank out optional fields

**What.** For each record, for each listed column, with probability `rate`, the value is replaced with `None`. Serialization is the normal path: empty field in CSV, `null` in NDJSON, null in Parquet — with the pinned Parquet schema untouched.

**Why.** Real sources have optional fields that are sometimes, but not always, populated: coupon codes, delivery notes, middle names, discount reasons. Downstream parsers must handle nulls gracefully; this rule gives them something to chew on.

**Config keys.**

| Key | Meaning |
|-----|---------|
| `type: null` | selects the rule |
| `rate` | probability per record **per listed column** (each column draws independently) |
| `columns` | fields that may be blanked |

**Minimal trigger.**

```yaml
quality:
  enabled: true
  rules:
    - type: null
      rate: 0.03
      columns: [coupon_code]
```

**Worked example.** A clean record and the same record after the `null` draw hit `coupon_code`:

```text
clean:      order_id=42   customer_id=7   total_amount=129.90   coupon_code=SAVE10
corrupted:  order_id=42   customer_id=7   total_amount=129.90   coupon_code=(empty)
```

Note what did *not* happen: the PK is intact, other columns are intact, row count is unchanged.

**What you will see.** `[CONFIG]` logs the effective rule set at startup; the `[QUALITY]` summary at the end of the run logs per-rule counters (records affected vs records seen) for the `null` rule.

**How to validate.** Count blank cells in the column (CSV; empty string = None):

```text
.venv\Scripts\python.exe -c "import csv;rows=list(csv.DictReader(open(r'data\quality\orders\reference_date=2026-10-24\orders_2026-10-24.csv',encoding='utf-8')));print('rows',len(rows),'nulls',sum(1 for r in rows if r['coupon_code']==''))"
```

Expected: `nulls` ≈ `rate × rows` (within noise). Repeat across two runs — identical counts, identical rows affected.

---

### 6.2 `invalid_fk` — break a reference, unambiguously

**What.** For each record, for each listed FK column, with probability `rate`, the foreign key is replaced with an id drawn from a **reserved impossible band** just past the parent's clean PK range. If the parent's valid ids are `1..P`, corrupted values live strictly above `P`. They therefore cannot collide with a real parent — an anti-join detects them cleanly, with zero false positives.

**Why.** Real pipelines ingest facts whose dimension row has not arrived yet, or points at the wrong source key. Warehouse tests (and humans) need to find these rows; a *reserved* invalid id makes that trivial and deterministic.

**Config keys.**

| Key | Meaning |
|-----|---------|
| `type: invalid_fk` | selects the rule |
| `rate` | probability per record **per listed FK column** |
| `columns` | FK fields of this entity (validated at startup) |

**Minimal trigger.**

```yaml
quality:
  enabled: true
  rules:
    - type: invalid_fk
      rate: 0.005
      columns: [customer_id]
```

**Worked example** (`customers` has 500 rows ⇒ valid `customer_id` is `1..500`):

```text
clean:      order_id=43   customer_id=7      total_amount=89.50
corrupted:  order_id=43   customer_id=7xx    total_amount=89.50    (7xx > 500: reserved band)
```

**What you will see.** Startup `[CONFIG]` confirms the parent range it derived from clean context; end-of-run `[QUALITY]` counters for the rule.

**How to validate.** Anti-join orders against customers (adjust file paths to your run):

```text
.venv\Scripts\python.exe -c "import csv;valid={r['customer_id'] for r in csv.DictReader(open(r'data\quality\customers\reference_date=2026-10-24\customers_2026-10-24.csv',encoding='utf-8'))};rows=list(csv.DictReader(open(r'data\quality\orders\reference_date=2026-10-24\orders_2026-10-24.csv',encoding='utf-8')));bad=[r for r in rows if r['customer_id'] not in valid];print('rows',len(rows),'bad_fk',len(bad),'max_fk',max((int(r['customer_id']) for r in bad),default=0))"
```

Expected: `bad_fk` ≈ `rate × rows`, and every bad id is strictly greater than the real parent max.

---

### 6.3 `out_of_range` — push a value past its domain

**What.** For each record, for each listed column, with probability `rate`, the value is moved **outside its declared domain** — past the bounds the generator/domain definition declares for that field (numeric bounds, date spans). The push is deterministic given the rule's seed; direction and magnitude stay plausible for the field's type (a quantity goes negative or impossibly high; a date leaves its allowed span).

**Why.** The classic trio of real-world causes — unit mix-ups (grams vs kilograms), manual keying errors, and upstream logic bugs — all produce *impossible* values rather than merely unusual ones. Downstream range checks must catch these; this rule manufactures them.

**Config keys.**

| Key | Meaning |
|-----|---------|
| `type: out_of_range` | selects the rule |
| `rate` | probability per record **per listed column** |
| `columns` | numeric or date fields with a declared domain |

**Minimal trigger.**

```yaml
quality:
  enabled: true
  rules:
    - type: out_of_range
      rate: 0.002
      columns: [quantity]
```

**Worked example** (`quantity` domain is `1..100`):

```text
clean:      order_id=44   quantity=3     total_amount=59.70
corrupted:  order_id=44   quantity=-3    total_amount=59.70    (outside 1..100)
```

**What you will see.** `[CONFIG]` logs the domains it will enforce violations against; `[QUALITY]` end-of-run counters.

**How to validate.** Predicate count:

```text
.venv\Scripts\python.exe -c "import csv;rows=list(csv.DictReader(open(r'data\quality\orders\reference_date=2026-10-24\orders_2026-10-24.csv',encoding='utf-8')));bad=[r for r in rows if not (1<=int(r['quantity'])<=100)];print('rows',len(rows),'out_of_range',len(bad))"
```

Expected: `out_of_range` ≈ `rate × rows` (records where `quantity` was `None` from the `null` rule are skipped — see §7).

---

### 6.4 `drift` — make a measure lie slowly

**What.** For selected measure columns, values are shifted by an amount that **grows with stream position**: early rows are (almost) clean, later rows are progressively off, bounded so magnitudes remain plausible for the field. It is not noise — it is a slow, reproducible bias along the run, derived from the drift rule's seed and the field's clean distribution.

**Why.** Real systems rarely fail loudly. Sensors calibrate away, FX rates lag, a business rule quietly rounds differently — the measure looks fine per-row and wrong in aggregate. Drift is the rule that defeats naive per-record validation and forces aggregate monitoring, which is exactly the muscle a warehouse project needs to build.

**Config keys.**

| Key | Meaning |
|-----|---------|
| `type: drift` | selects the rule |
| `rate` | probability per record **per listed column** of drifting that record's value |
| `columns` | measure fields (decimals, counts) |

**Minimal trigger.**

```yaml
quality:
  enabled: true
  rules:
    - type: drift
      rate: 0.01
      columns: [total_amount]
```

**Worked example** (schematic — magnitudes depend on the field's clean distribution):

```text
row  1/200000   total_amount: 129.90 → 129.90   (start of stream ≈ clean)
row  90k/200000 total_amount: 119.00 → 116.63   (mid-stream, small bias)
row 199k/200000 total_amount:  99.50 →  92.14   (late stream, larger bias)
```

**What you will see.** `[QUALITY]` end-of-run counters as usual. The effect shows up in aggregates, not in single rows.

**How to validate.** Compare early vs late decile means. Save this as a scratch diagnostic (not platform code), e.g. `scratch\check_drift.py`:

```python
import csv
from pathlib import Path

p = Path(r"data\quality\orders\reference_date=2026-10-24\orders_2026-10-24.csv")
vals = [float(r["total_amount"]) for r in csv.DictReader(open(p, encoding="utf-8")) if r["total_amount"] != ""]
k = len(vals) // 10
print("rows used:", len(vals))
for name, seg in (("first decile", vals[:k]), ("last decile", vals[-k:])):
    print(f"{name}: mean={sum(seg)/len(seg):.2f}")
```

Expected: the two means differ by more than the clean run's would (run the same script on `data\generated\...` as the control). Reproducibility: same config + seed ⇒ same decile means, run after run.

---

### 6.5 `duplicate` — the same row, twice

**What.** With probability `rate`, a record is emitted **twice**: the second emission is a byte-identical copy of the final, fully corrupted record (all four earlier rules already applied). Because it runs last in the chain, duplicates are exact copies — detectable by simple full-row comparison, and guaranteed to repeat the same PK.

**Why.** At-least-once delivery, retries, and replayed batches all manifest as identical rows. Dedup logic, PK-uniqueness tests, and `row_count` reconciliations need realistic duplicate volume to be worth anything.

**Config keys.**

| Key | Meaning |
|-----|---------|
| `type: duplicate` | selects the rule |
| `rate` | probability per record of emitting one extra copy |
| `columns` | **omit** — whole-record rule; supplying it is a startup error |

**Minimal trigger.**

```yaml
quality:
  enabled: true
  rules:
    - type: duplicate
      rate: 0.01
```

**Worked example:**

```text
...rows 41, 42, 43 (43 hit by null on coupon_code)...
emitted:  row 41 (corrupted)
          row 42 (corrupted)
          row 43 (corrupted, coupon_code empty)     ← original
          row 43 (corrupted, coupon_code empty)     ← exact duplicate, same PK
```

**What you will see.** Row count above `N`: expected `N × (1 + rate)`, logged by the writer and recorded in the manifest. `[QUALITY]` counters report duplicates emitted.

**How to validate.** Full-row duplicate count (scratch diagnostic, e.g. `scratch\check_dupes.py`):

```python
import csv
from collections import Counter
from pathlib import Path

p = Path(r"data\quality\orders\reference_date=2026-10-24\orders_2026-10-24.csv")
rows = list(csv.DictReader(open(p, encoding="utf-8")))
counts = Counter(tuple(r.items()) for r in rows)
dupes = sum(c - 1 for c in counts.values() if c > 1)
print("rows", len(rows), "distinct", len(counts), "extra copies", dupes)
```

Expected: `extra copies` ≈ `rate × N`. Run it on `data\generated\...` as the control: zero.

## 7. Combining rules: interaction, rates, and presets

### 7.1 How rules interact (the three laws)

**Law 1 — order is fixed, YAML order is cosmetic.** The engine runs `null → invalid_fk → out_of_range → drift → duplicate` no matter how you list rules in config. Two configs that differ only in `rules:` order produce byte-identical output.

**Law 2 — `null` wins first; `None` is sticky.** If the `null` rule blanks a column in a given record, the value-based rules (`out_of_range`, `drift`) skip that column for that record — there is no value left to corrupt. `invalid_fk` is unaffected by this because it runs before the value rules and replaces ids, not values. Practical consequence: if you stack `null` and `out_of_range` on the same column, `out_of_range`'s *effective* rate applies only to records the `null` rule missed, so its observed count is slightly below `rate × N`.

**Law 3 — only `duplicate` changes row counts; it copies the final state.** The first four rules are in-place transforms. `duplicate` runs last and copies whatever the record has become. There is no such thing as a "clean duplicate" of a corrupted row — by construction.

### 7.2 Expected counts (so your validation is honest)

| Rule | Expected affected rows | Notes |
|------|------------------------|-------|
| `null` | `rate × N` per column | columns draw independently |
| `invalid_fk` | `rate × N` per column | never collides with real parents |
| `out_of_range` | ≤ `rate × N` per column | minus records the `null` rule already blanked (Law 2) |
| `drift` | ≤ `rate × N` per column | same Law-2 interaction; effect is aggregate, not per-row |
| `duplicate` | `rate × N` extra rows | final row count ≈ `N × (1 + rate)` |

All counts are Bernoulli draws, so expect a little noise around the expectation; the exact realized counts are in the run log and the manifest, and they are identical across runs with the same config and seed.

### 7.3 Copy-paste presets

**Preset A — "CRM-lite dirt" (start here).** Mild, realistic, easy to eyeball:

```yaml
quality:
  enabled: true
  rules:
    - type: null
      rate: 0.02
      columns: [coupon_code]
    - type: invalid_fk
      rate: 0.005
      columns: [customer_id]
    - type: duplicate
      rate: 0.01
```

**Preset B — "drift & friends" (aggregate-monitoring drills).** Measures lie slowly; per-row checks stay green:

```yaml
quality:
  enabled: true
  rules:
    - type: drift
      rate: 0.02
      columns: [total_amount]
    - type: out_of_range
      rate: 0.001
      columns: [quantity]
```

**Preset C — "load-test chaos" (for DQ pipelines, not for demos).** Everything at once, loud:

```yaml
quality:
  enabled: true
  rules:
    - type: null
      rate: 0.10
      columns: [coupon_code, delivery_note]
    - type: invalid_fk
      rate: 0.02
      columns: [customer_id]
    - type: out_of_range
      rate: 0.01
      columns: [quantity]
    - type: drift
      rate: 0.05
      columns: [total_amount]
    - type: duplicate
      rate: 0.03
```

Rates in the real world are small. If a preset produces dirt you cannot explain row-by-row, lower the rates until you can — realism you cannot audit is worse than no realism at all.

## 8. Impurity + windows: event-date bucketing, `mode: full | latest`

The window engine (Milestone 6a) buckets one deterministic clean stream into files by business date: `{entity}/event_date={window}/{entity}_{window}.{fmt}`, with the PK union across window files equal to `1..N` and temporal integrity inherited from the clean rules (ADR-0016). Impurity composes with this cleanly because the pipeline order is **bucket first, corrupt second**: the stream is sliced into its window files, and the quality chain runs per window file.

What that means operationally:

- **Per-window seeds.** Each window's corruption uses `"{master}:{entity}:{window}:quality:{rule}"` (§3). Windows get independent, individually reproducible dirt — so "yesterday is dirtier than today" experiments are one config away, and editing one window's span never perturbs another's corruption.
- **`mode: full`.** Every window file in the span is written, each with its own corruption pattern applied. Use this when a downstream project ingests the whole history.
- **`mode: latest`.** Only the newest window's file is written — corrupted with that window's seed. Use it for incremental-ingestion drills where the downstream loads just the latest slice.
- **Invariants preserved.** Rules never change PKs, so the PK union across window files remains `1..N` (duplicates repeat a PK *within* the same window file — exactly the condition your dedup tests exercise). Pinned Parquet schemas are unchanged.
- **Invariants deliberately broken.** Corrupted window files can violate the business rules the clean baseline upholds: nulls where values are required, FKs that resolve nowhere, ranges that are impossible. That is the point — these files are the training set for your DQ tests, while `data/generated` remains the yardstick.

Quality output paths under windowed mode mirror the clean layout, one level up the tree:

```text
data/quality/{entity}/event_date={window}/{entity}_{window}.{fmt}
data/generated/{entity}/event_date={window}/{entity}_{window}.{fmt}   <- clean, untouched
```

## 9. Output layout, the clean-baseline guard, and manifests

### 9.1 Layout

| Run | Mode | Path |
|-----|------|------|
| clean | snapshot | `data/generated/{entity}/reference_date={date}/{entity}_{date}.{fmt}` |
| clean | windowed | `data/generated/{entity}/event_date={window}/{entity}_{window}.{fmt}` |
| quality | snapshot | `data/quality/{entity}/reference_date={date}/{entity}_{date}.{fmt}` |
| quality | windowed | `data/quality/{entity}/event_date={window}/{entity}_{window}.{fmt}` |

Writers behave identically in both trees: overwrite-with-log, atomic same-directory `.tmp` + `os.replace` writes, empty-stream rejection, count/bytes logging.

### 9.2 The guard

At startup of a quality run, the engine resolves the quality output root and refuses the run — logged, exit code `1` — if that root is `data/generated` or any path inside it. The clean baseline is the platform's yardstick; structurally, no quality run can write there, no matter how the config is (mis)shaped. If you ever see the guard fire, fix `quality.output.root` — the default `data/quality` is almost certainly what you want.

### 9.3 Manifests

Quality runs emit manifests in the current v3 shape — `manifest_version`, `layout` (`snapshot` | `windowed`), and a `files` list with a per-file `sha256` — under the quality root, alongside the same seed/versions metadata clean manifests record. Three operator uses:

1. **Reproducibility proof.** Run the same quality config twice; the `files[].sha256` values match byte-for-byte.
2. **Change detection.** Any config edit that changes output bytes (rates, columns, seeds) changes checksums — a visible, diffable contract change instead of a silent one.
3. **Lineage.** The manifest records which rules and seeds produced the tree, so a downstream project can cite exactly how its "dirty" fixture was made.

## 10. Running impure generations (step by step)

**Step 1 — pick or create the quality config.** Copy your working clean config and add the `quality:` block (§5, presets in §7.3):

```text
copy configs\base.yaml configs\base_quality.yaml
notepad configs\base_quality.yaml
```

**Step 2 — run it.** Same entry point, same command shape you already use for clean runs — just point it at the quality config:

```text
.venv\Scripts\python.exe main.py
```

(If your `main.py` reads a fixed config path, point it at `configs\base_quality.yaml` the same way you do for any config change — impurity introduces **no** new CLI flags, entry points, or steps.)

**Step 3 — read the log.** A representative quality run (counts and paths will vary):

```text
[STARTUP] run started, cwd=C:\...\datagen, config=configs\base_quality.yaml
[CONFIG]   quality enabled for orders (root=data/quality)
[CONFIG]   rule chain (fixed): null -> invalid_fk -> out_of_range -> drift -> duplicate
[CONFIG]   orders: null(rate=0.03, cols=[coupon_code]) invalid_fk(rate=0.005, cols=[customer_id]) ...
[SEED]     orders:quality:null  <- sha256(master:orders:quality:null)
[GENERATION] orders: 200000 clean records streamed
[QUALITY]  orders rule=null        affected=6031 / 200000
[QUALITY]  orders rule=invalid_fk  affected=1004 / 200000
[QUALITY]  orders rule=out_of_range affected=371 / 200000
[QUALITY]  orders rule=drift       affected=2015 / 200000
[QUALITY]  orders rule=duplicate   emitted=2022 extra rows
[WRITER]   wrote data/quality/orders/... (202022 rows, sha256=...)
[MANIFEST] manifest v3 written (layout=snapshot, files=1)
[SUMMARY]  run finished, exit code 0
```

This is the milestone log format you already know (ADR-0009 tags, per-run log file under `logs\`); the only new vocabulary is the `[QUALITY]` block.

**Step 4 — validate** with §11, then commit the config so the corrupted dataset is a reproducible fixture:

```text
git add configs\base_quality.yaml docs\impurity_guide.md
git commit -m "feat(config): enable impurity preset A on orders (guide §7.3)"
```

**Exit codes** are unchanged: `0` success, `1` validation/write failure (including every fail-fast case in §5.4 and the §9.2 guard), `130` interrupted.

## 11. Validation playbook

Run these after any quality run. Paths use the snapshot layout — swap `reference_date=...` for `event_date=...` in windowed mode. Control every check against `data\generated\...`, which must always come back clean.

| Rule | Check | Expected on quality output | Expected on clean baseline |
|------|-------|----------------------------|----------------------------|
| `null` | blank-cell count (§6.1 one-liner) | ≈ `rate × N` per column | 0 (unless the field is optional-and-empty by design) |
| `invalid_fk` | anti-join vs parent (§6.2 one-liner) | ≈ `rate × N`; all bad ids > parent max | 0 |
| `out_of_range` | domain predicate (§6.3 one-liner) | ≤ `rate × N` | 0 |
| `drift` | decile means (§6.4 scratch script) | early vs late means differ beyond clean-run noise | means indistinguishable |
| `duplicate` | full-row counts (§6.5 scratch script) | ≈ `rate × N` extra copies | 0 |
| schema | open Parquet, compare schema to pinned | identical (nulls only) | identical |
| reproducibility | diff `files[].sha256` across two runs | identical | identical |

**The five-minute acceptance drill** (worth running after any engine upgrade):

1. Run the clean config. Run the quality config. Run the quality config again.
2. One-liner checks from §6.1–6.3 on the quality output — counts near expectation.
3. Scratch scripts from §6.4–6.5 — drift visible, duplicates present.
4. Same checks on `data\generated\...` — all clean.
5. Compare the two quality manifests' checksums — identical.

If all five pass, the engine is behaving exactly as contracted.

## 12. Troubleshooting

| Symptom (log says) | Cause | Fix |
|--------------------|-------|-----|
| `unknown rule type: ...` | typo in `type:` | use exactly one of `null`, `invalid_fk`, `out_of_range`, `drift`, `duplicate` |
| `rate must be in [0.0, 1.0]` | missing or out-of-range `rate` | set an explicit probability; there are no defaults by design |
| `column ... is not a field of ...` | typo, or rule attached to the wrong entity | match the entity's record field names (§5 note) |
| `columns not allowed for duplicate` | `columns` key on a duplicate rule | remove it — duplicate is whole-record |
| `... is not a foreign key of ...` | `invalid_fk` on a non-FK column | point it at a real FK column |
| guard refusal: quality root inside `data/generated` | `quality.output.root` misconfigured | restore `data/quality` (§9.2) |
| `invalid_fk` parent range empty | parent entity has 0 rows this run | generate parents first (FK-by-parent-range needs real parents) |
| run exits 1 with empty-stream rejection | windowed `mode: latest` with an empty latest window, or span misconfigured | widen the window span or use `mode: full` |
| duplicate count far from `rate × N` | Bernoulli noise at small N | check the manifest/logged exact count; larger N tightens the spread |
| YAML rule order seemed to change results | it cannot — see §7.1 Law 1 | look for another config diff (rate, columns, seed) |
| a key in this guide does not match your checkout | checkout predates the post-6a engine this guide documents | `git log --oneline` and confirm you are past Milestone 6a before following §5 |

## 13. Extending the engine — and why this guide forbids shortcuts

The engine's surface is deliberately tiny: five rule types, one frozen chain, three config keys per rule. When you feel the need to go beyond it, apply the platform's standard **instance vs new pattern** test:

- **Instance-level change (config only, no ADR):** new rates, new columns, enabling/disabling rules, new preset config files, per-window rate tuning. This is your daily operator surface — it changes *what* is corrupted, never *how corruption works*.
- **New pattern (ADR required):** a sixth rule type, a different chain order, a new seed-derivation scheme, a second output tree, a per-rule parameter that changes output bytes. These change the platform's contract: same config + seed would produce different bytes than before, silently breaking every downstream fixture. That is exactly what the ADR log (`docs/decisions.md`) and the §26 breaking-change protocol exist for — the change gets documented, versioned, and announced, never smuggled in.

So: tune rates and columns freely; treat chain and rule inventory as platform contracts. If a milestone genuinely needs a sixth rule, it will arrive as a milestone proposal with an ADR — the same way `invalid_fk` and the window engine did.

## 14. Quick reference card

**Rules** (chain order fixed: `null → invalid_fk → out_of_range → drift → duplicate`):

```yaml
- { type: null,         rate: 0.03, columns: [a, b] }   # blanks values
- { type: invalid_fk,   rate: 0.005, columns: [fk] }    # impossible id > parent max
- { type: out_of_range, rate: 0.002, columns: [x] }     # value past declared domain
- { type: drift,        rate: 0.01,  columns: [m] }     # position-tied measure bias
- { type: duplicate,    rate: 0.01 }                     # exact copy of final record
```

**Seeds:** entity `{master}:{entity}` · rule `{master}:{entity}:quality:{rule}` · windowed rule `{master}:{entity}:{window}:quality:{rule}` (all SHA-256).

**Paths:** quality snapshot `data/quality/{entity}/reference_date={date}/{entity}_{date}.{fmt}` · quality windowed `data/quality/{entity}/event_date={window}/{entity}_{window}.{fmt}` · clean baseline always `data/generated/...` (guarded).

**Log tags:** `[STARTUP] [CONFIG] [SEED] [GENERATION] [QUALITY] [WRITER] [MANIFEST] [SUMMARY]` — new vocabulary is the `[QUALITY]` per-rule counters.

**Exit codes:** `0` ok · `1` config/guard/write failure · `130` interrupted.

**Golden rules:** config order never matters · only `duplicate` changes row counts · PKs and schemas never change · parents are never modified · clean baseline is unreachable by design · same config + seed ⇒ identical bytes, provable via manifest checksums.

'''

# (relative path, content, label)
NEW_FILES: list[tuple[str, str, str]] = [
    (
        "docs/impurity_guide.md",
        GUIDE_CONTENT,
        "Impurity Engine Operator Guide (1.0.0 + window engine)",
    ),
]

UPDATED_FILES: list[tuple[str, str]] = []  # none -- doc-only bootstrap


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_preconditions(root: Path) -> bool:
    """Verify we are in the project root and warn about engine presence."""
    ok = True
    log("STARTUP", f"cwd={root}")
    log("STARTUP", f"python={sys.version.split()[0]}")

    for rel in ("main.py", "src", "docs"):
        p = root / rel
        if p.exists():
            log("CHECK", f"ok       {rel} -> {p}")
        else:
            log("CHECK", f"MISSING  {rel} (expected in project root)")
            ok = False

    if not (root / ".git").exists():
        log("CHECK", "warn     .git not found -- not fatal, but you lose rollback safety")
    else:
        log("CHECK", "ok       .git found")

    src = root / "src"
    if src.exists():
        quality_hits = sorted(
            str(p.relative_to(root))
            for p in src.rglob("*quality*")
            if p.is_file()
        )
        if quality_hits:
            shown = ", ".join(quality_hits[:3]) + (" ..." if len(quality_hits) > 3 else "")
            log("CHECK", f"ok       quality engine module(s): {shown}")
        else:
            log(
                "CHECK",
                "warn     no *quality* module found under src/ -- this guide targets "
                "the post-Milestone-5c engine; verify your checkout is past 6a",
            )
    return ok


def deliver(root: Path, rel: str, content: str, label: str, dry: bool) -> str:
    """NEW_FILES semantics: create-if-missing, heal-empty, never overwrite."""
    target = root / rel
    payload = content.encode("utf-8")
    digest = sha256_of(payload)
    log("CONFIG", f"{label}: {target}")

    if target.exists() and target.stat().st_size > 0:
        log(
            "SKIP",
            f"{rel} already exists ({target.stat().st_size} bytes) -- "
            "never overwritten; delete or rename it first to regenerate",
        )
        return "skip"

    heal = target.exists()  # exists but empty -> crash artifact
    action = "HEAL" if heal else "WRITE"
    if heal:
        log("HEAL", f"{rel} exists but is EMPTY (crash artifact) -- healing")

    if dry:
        log("DRY-RUN", f"would {'heal' if heal else 'create'} {rel} ({len(payload)} bytes, sha256={digest[:16]}...)")
        return "healed" if heal else "created (dry-run)"

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    log(action, f"wrote {rel} ({len(payload)} bytes, sha256={digest[:16]}...)")
    return "healed" if heal else "created"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap docs/impurity_guide.md (doc-only).")
    parser.add_argument("--dry-run", action="store_true", help="show actions without writing")
    args = parser.parse_args()

    log("STARTUP", "bootstrap_impurity_guide.py -- Impurity Engine Operator Guide")
    root = Path.cwd()

    if not check_preconditions(root):
        log("SUMMARY", "preconditions failed -- run this from the project root (dir containing main.py, src, docs)")
        return 1

    results: list[tuple[str, str]] = []
    try:
        for rel, content, label in NEW_FILES:
            results.append((rel, deliver(root, rel, content, label, args.dry_run)))
        for rel, _ in UPDATED_FILES:  # none today; kept for house-convention completeness
            log("SKIP", f"UPDATED_FILES entry {rel} -- none defined in this bootstrap")
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C) -- nothing left half-written (writes are atomic)")
        return 130
    except OSError as exc:
        log("SUMMARY", f"write failure: {exc}")
        return 1

    log("MANIFEST", "actions: " + ("; ".join(f"{r}={s}" for r, s in results) or "none"))
    if args.dry_run:
        log("SUMMARY", "dry-run complete -- no files were written (exit 0)")
    else:
        log("SUMMARY", "done. Next steps:")
        log("SUMMARY", "  1. git add docs/impurity_guide.md")
        log("SUMMARY", "  2. git commit -m \"docs: add impurity engine operator guide (post-6a)\"")
        log("SUMMARY", "  3. read section 7.3 for copy-paste presets, section 11 for the acceptance drill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
