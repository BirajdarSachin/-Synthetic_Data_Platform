#!/usr/bin/env python3
"""bootstrap_milestone7c.py -- DQ expectation wiring (Milestone 7c, ADR-027).

House-style bootstrap:
  UPDATED_FILES -> logged, atomic overwrite for files the 7a/7b/7c deliveries
                 own. Byte-identical -> 'up-to-date' no-op; otherwise [UPDATE]
                 logs the old->new sha256 transition (git = review/rollback).
                 7c updates exactly ONE file: configs/dq_base.yaml (v4 -> v5).
  APPEND_FILES -> docs/decisions.md ADR-027, append-only, idempotent: skipped
                 when the ADR-027 marker is already present; the file is never
                 truncated (ADR-024/025/026 content preserved byte-for-byte)

What it delivers (Phase 7c -- CONFIG-ONLY, no harness code changes):
  configs/dq_base.yaml  v5: row_count wired for all six entities (volumes from
                        the accepted 7b clean drill); order_item quantity
                        range 1..100 (engine domain, guide section 6.3);
                        order.required = [order_id, status] (PROVISIONAL
                        null-column wiring -- confirm with the census
                        one-liner in the SUMMARY); order gains the second
                        real FK sales_rep_employee_id -> employee.
  docs/decisions.md     ADR-027 appended.

Hard precondition: the 7b harness must already be installed
(src/core/dq/runner.py knows 'pk_union') -- 7c only wires config ONTO it.
Run bootstrap_milestone7b.py first if that check fails.

After running this bootstrap:  python -m pytest tests/... -q  (expect 103 --
no test or code files are touched), then the census + two dq_main.py
commands printed in the SUMMARY section.

Exit codes: 0 = delivered/skipped cleanly, 1 = precondition or write failure,
            130 = interrupted (Ctrl+C).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

UPDATED_FILES: list[tuple[str, str, str]] = [
    (
        "configs/dq_base.yaml",
        r'''# =============================================================================
# dq_base.yaml -- DQ check configuration (Milestone 7c, v5 -- ADR-025/026/027)
#
# v5 is the WIRING milestone (ADR-027): zero code changes -- every new line is
# an expectation harvested from accepted drill evidence.
#
#   * row_count wired for ALL SIX entities. The accepted 7b clean drill pinned
#     the per-file volumes (customer 1000, order 5000, order_item 12000,
#     employee 20, review 2000, payment 3000); they are a contract now. If you
#     regenerate with different volumes, re-wire from data/dq/report.json
#     (rows_checked per file) -- the check fails loudly by design.
#   * order_item.ranges.quantity {min 1, max 100, type int} -- the engine's
#     declared domain for quantity (impurity guide section 6.3). This unmasks
#     the 65 out_of_range rows on data/corrupted (value_in_range's first live
#     proof on real dirt).
#   * order.required grows to [order_id, status] -- PROVISIONAL null-column
#     wiring (ADR-027 decision 3). The engine's null rule blanks one order
#     column (57 rows); status is the most likely target among the five real
#     columns (customer_id/order_date/order_id/sales_rep_employee_id/status,
#     all confirmed by the 7b evidence lines). Confirm with the census
#     one-liner printed by the bootstrap SUMMARY; if it names a different
#     column, swap it into required (one line, then re-run the drills).
#   * order.foreign_keys += sales_rep_employee_id -> employee -- the second
#     real FK, column proven by the 7b evidence line sales_rep_employee_id=10.
#     Empty cells stay skipped by fk_integrity (Law 2), so this is safe even
#     if the generator leaves some sales reps unset.
#   * order_item.product_id -> product stays UNWIRED: product has no
#     dq.entities entry yet (datasets=8 vs 6 configured) and the runner
#     fail-fasts on FK parents it cannot load. Wire both together later.
#
# ADR-026 pointed Parquet at 7c; this wiring milestone consumed the 7c slot,
# so Parquet input slides to 7d (still needs its reader-dependency ADR).
#
# Drills on this machine (quality root is data/corrupted per your [WRITER] log):
#   clean    .venv\Scripts\python.exe dq_main.py --tree data\generated  --mode clean
#   corrupt  .venv\Scripts\python.exe dq_main.py --tree data\corrupted --mode corrupt
#
# The harness is a consumer of output trees: it reads CSV files and this
# config only. Checks default to enabled=true when absent from dq.checks.
# =============================================================================

dq:
  checks:
    not_null:
      enabled: true          # required columns must be populated
    fk_integrity:
      enabled: true          # FK values must resolve within the same tree
    value_in_range:
      enabled: true          # numeric columns must sit inside declared ranges
    pk_unique:
      enabled: true          # primary keys must not repeat within a file
    duplicate_rows:
      enabled: true          # exact full-row copies within a file (7b)
    row_count:
      enabled: true          # per-file size bounds where configured (7b)
    pk_union:
      enabled: true          # cross-window PK union where opted in (7b)

  entities:
    # --- volumes below are the accepted 7b clean drill's rows_checked.
    customer:
      primary_key: customer_id
      required: [customer_id]
      row_count: {expected: 1000}

    order:
      primary_key: order_id
      required: [order_id, status]   # status: provisional null-column wiring -- confirm with the census one-liner
      foreign_keys:
        customer_id: customer
        sales_rep_employee_id: employee   # 7c: second real FK (proven by drill evidence)
      row_count: {expected: 5000}
      # pk_union: true               # only if keys must be unique ACROSS windows

    order_item:
      primary_key: order_item_id
      required: [order_item_id]
      ranges:
        quantity: {min: 1, max: 100, type: int}   # 7c: engine domain (guide section 6.3)
      row_count: {expected: 12000}
      # product_id -> product: parent entity not configured yet (datasets=8);
      # add the product entity to dq.entities first, then wire the FK here.

    employee:
      primary_key: employee_id
      required: [employee_id]
      row_count: {expected: 20}

    review:
      primary_key: review_id
      required: [review_id]
      row_count: {expected: 2000}

    payment:
      primary_key: payment_id
      required: [payment_id]
      row_count: {expected: 3000}

    # --- datasets=8: two entity directories are still unseen. When a drill
    # errors, the diagnostic prints the tree contents -- add the missing
    # entities here (same shape: primary_key + required + row_count from
    # data/dq/report.json), then wire order_item.product_id -> product.
''',
        "DQ check configuration (v5: expectations wired from accepted drill evidence)",
    ),
]

APPEND_FILES: list[tuple[str, str, str, str]] = [
    (
        "docs/decisions.md",
        "ADR-027",
        r'''---

## ADR-027 — DQ harness 7c: expectation wiring from accepted drill evidence

**Status:** Accepted (Milestone 7c — config-only; no harness code changes)

**Context.** The 7b drill went green on the real machine (103 tests; clean
tree 0 violations / 23020 rows; corrupted tree 732 violations / 23376 rows,
exit 0 both). That run closed the code backlog but left three checks without
live proof on real dirt (`not_null` and `value_in_range` never fired; only
`customer` had a `row_count` expectation), and the drill evidence itself
carried the missing facts: the `duplicate_rows` sample lines print full rows,
which leaked the real headers of `order` (`customer_id`, `order_date`,
`order_id`, `sales_rep_employee_id`, `status`) and `order_item` (`order_id`,
`order_item_id`, `product_id`, `quantity`, `unit_price`); the accepted clean
run pinned the per-file row counts; the impurity guide (§6.3) pins
`quantity`'s declared domain at 1..100. The one remaining unknown — WHICH
`order` column the engine's null rule blanks (57 rows) — is resolvable with a
one-line census command before any drill runs.

**Decision.**
1. **`row_count` is wired for all six entities** (`expected:` 1000 / 5000 /
   12000 / 20 / 2000 / 3000, taken verbatim from the accepted clean drill's
   `rows_checked`). Row counts become a contract: any regenerated volume
   change fails loudly, and the fix is re-wiring from `data/dq/report.json`.
   This is the coupling ADR-026's consequences section anticipated; it is now
   deliberate.
2. **`order_item.ranges.quantity = {min: 1, max: 100, type: int}`** mirrors
   the engine's declared domain for `quantity` (impurity guide §6.3), so the
   65 `out_of_range` rows on `data/corrupted` become visible to
   `value_in_range` — its first live proof.
3. **`order.required = [order_id, status]` is PROVISIONAL.** `status` is the
   most likely null-rule target among the five real `order` columns (the
   guide's null rationale is "optional string field at the source";
   `customer_id` is taken by `invalid_fk`, `order_id` is the key). The
   confirmation protocol is a census one-liner counting empty cells per
   column on the corrupted `order` file; if it names a different column, the
   fix is one line in this config. A wrong guess cannot break either drill:
   clean data is complete by construction, and the corrupt contract only
   needs >= 1 fired check.
4. **`order.foreign_keys += sales_rep_employee_id -> employee`.** Second real
   FK, column proven by drill evidence (`sales_rep_employee_id=10`); the
   parent `employee` is already configured with a header-validated
   `employee_id`. Empty FK cells stay `fk_integrity`-skipped (Law 2), so the
   check is safe even if the generator leaves some sales reps unset.
5. **`order_item.product_id -> product` stays UNWIRED.** `product` has no
   `dq.entities` entry (the log's `datasets=8` vs 6 configured entities), and
   the runner fail-fasts on FK parents it cannot load (ADR-025 header
   discipline). Wire the missing entities first, then this FK.
6. **No new check, no code change, `REPORT_VERSION` unchanged.** ADR-026
   pointed Parquet input at 7c; this wiring milestone consumed the 7c slot,
   so Parquet slides to 7d and still needs its reader-dependency ADR.

**Consequences.**
- The clean drill must stay at exactly 0 violations / 23020 rows: every wired
  expectation mirrors generator output by construction, so any clean-tree
  failure now indicates a real change in the generator, not harness noise.
- The corrupted drill is expected to rise from 732 to ~856 violations:
  +57 `not_null` (if `status` is the null column), +65 `value_in_range`,
  +2 `row_count` (order 5110 != 5000, order_item 12223 != 12000). If the
  census names a different null column, the floor is 799 until the one-line
  swap lands. The corrupt contract (ok iff >= 1 check fired) holds either
  way. Counts may drift a few above these numbers if the engine's duplicate
  rule copied already-corrupted rows (copies of dirty rows are equally dirty
  and each counts once).
- All four 7a checks now have live proof on real dirt after the next corrupt
  run, completing the harness's evidence chain against the impurity suite.
- Adding the two unseen entities (and `product`) follows the same shape:
  primary_key + required + row_count from the report, then FK wiring.''',
        "ADR-027 (append-only, idempotent by marker)",
    ),
]


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_preconditions(root: Path) -> bool:
    ok = True
    log("STARTUP", f"cwd={root}")
    log("STARTUP", f"python={sys.version.split()[0]}")
    for rel in ("main.py", "src", "docs", "tests"):
        p = root / rel
        if p.exists():
            log("CHECK", f"ok       {rel}")
        else:
            log("CHECK", f"MISSING  {rel} (expected in project root)")
            ok = False
    if (root / "configs").is_dir():
        log("CHECK", "ok       configs")
    else:
        log("CHECK", "note     configs/ absent -- 7c has nothing to install onto")
    if not (root / ".git").exists():
        log("CHECK", "warn     .git not found -- not fatal, but you lose rollback safety")
    runner = root / "src" / "core" / "dq" / "runner.py"
    if runner.is_file() and "pk_union" in runner.read_text(encoding="utf-8", errors="replace"):
        log("CHECK", "ok       7b harness present (runner.py knows pk_union)")
    else:
        log("CHECK", "MISSING  7b harness (src/core/dq/runner.py without pk_union)")
        log("CHECK", "         -> run bootstrap_milestone7b.py first; 7c only wires config onto it")
        ok = False
    return ok


def deliver_new(root: Path, rel: str, content: str, label: str, dry: bool) -> str:
    target = root / rel
    payload = content.encode("utf-8")
    digest = sha256_of(payload)
    log("CONFIG", f"{label}: {target}")
    if target.exists() and target.stat().st_size > 0:
        log("SKIP", f"{rel} already exists ({target.stat().st_size} bytes) -- never overwritten")
        return "skip"
    heal = target.exists()
    if heal:
        log("HEAL", f"{rel} exists but is EMPTY (crash artifact) -- healing")
    if dry:
        log("DRY-RUN", f"would {'heal' if heal else 'create'} {rel} ({len(payload)} bytes, sha256={digest[:16]}...)")
        return "healed" if heal else "created (dry-run)"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    log("HEAL" if heal else "WRITE", f"wrote {rel} ({len(payload)} bytes, sha256={digest[:16]}...)")
    return "healed" if heal else "created"


def deliver_update(root: Path, rel: str, content: str, label: str, dry: bool) -> str:
    """Logged, atomic overwrite for harness files this bootstrap owns.

    Missing or empty target -> created (same path as deliver_new).
    Byte-identical content -> no-op. Otherwise the old sha256 is logged
    before the new bytes land; git is the review/rollback path.
    """
    target = root / rel
    payload = content.encode("utf-8")
    if not target.exists() or target.stat().st_size == 0:
        return deliver_new(root, rel, content, label, dry)
    if target.read_bytes() == payload:
        log("SKIP", f"{rel} already up-to-date (sha256={sha256_of(payload)[:16]}...)")
        return "up-to-date"
    old_digest = sha256_of(target.read_bytes())
    digest = sha256_of(payload)
    log("CONFIG", f"{label}: {target}")
    if dry:
        log("DRY-RUN", f"would update {rel} ({target.stat().st_size} -> {len(payload)} bytes)")
        return "updated (dry-run)"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    log("UPDATE", f"updated {rel}: sha256 {old_digest[:16]}... -> {digest[:16]}... ({len(payload)} bytes)")
    return "updated"


def deliver_append(root: Path, rel: str, marker: str, content: str, label: str, dry: bool) -> str:
    target = root / rel
    log("CONFIG", f"{label}: {target}")
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if marker in existing:
            log("SKIP", f"{rel} already contains {marker} -- append skipped (idempotent)")
            return "skip"
        addition = content if existing.endswith("\n") or not existing else "\n" + content
        addition += "\n"
        if dry:
            log("DRY-RUN", f"would append {len(addition.encode('utf-8'))} bytes to {rel}")
            return "appended (dry-run)"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(addition)
        log("APPEND", f"appended {marker} to {rel} (nothing truncated)")
        return "appended"
    if dry:
        log("DRY-RUN", f"would create {rel} with {marker}")
        return "appended (dry-run)"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content + "\n", encoding="utf-8")
    log("WRITE", f"created {rel} with {marker}")
    return "appended"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Milestone 7c (DQ expectation wiring).")
    parser.add_argument("--dry-run", action="store_true", help="show actions without writing")
    args = parser.parse_args()

    log("STARTUP", "bootstrap_milestone7c.py -- DQ expectation wiring (ADR-027)")
    root = Path.cwd()
    if not check_preconditions(root):
        log("SUMMARY", "preconditions failed -- run this from the project root")
        return 1

    results: list[tuple[str, str]] = []
    try:
        for rel, content, label in UPDATED_FILES:
            results.append((rel, deliver_update(root, rel, content, label, args.dry_run)))
        for rel, marker, content, label in APPEND_FILES:
            results.append((rel, deliver_append(root, rel, marker, content, label, args.dry_run)))
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C) -- writes are atomic, nothing half-written")
        return 130
    except OSError as exc:
        log("SUMMARY", f"write failure: {exc}")
        return 1

    log("MANIFEST", "actions: " + ("; ".join(f"{r}={s}" for r, s in results) or "none"))
    if not args.dry_run:
        log("SUMMARY", "next steps:")
        log("SUMMARY", "  1. .venv\\Scripts\\python.exe -m pytest tests/test_dq_checks.py tests/test_dq_runner.py -q")
        log("SUMMARY", "     (expect 103 passed -- 7c touches NO code or test files)")
        log("SUMMARY", "  2. census: WHICH order column did the null rule blank? (expect ONE column with ~57 empties)")
        log("SUMMARY", "     .venv\\Scripts\\python.exe -c \"import csv;rows=list(csv.DictReader(open(r'data\\corrupted\\order\\reference_date=2026-01-15\\order_2026-01-15.csv',encoding='utf-8-sig')));[print(c, sum(1 for r in rows if r[c]=='')) for c in rows[0]]\"")
        log("SUMMARY", "     (if the ~57 column is NOT status: edit order.required in configs/dq_base.yaml to")
        log("SUMMARY", "      [order_id, <that column>] -- one line -- then re-run step 4)")
        log("SUMMARY", "  3. .venv\\Scripts\\python.exe dq_main.py --tree data\\generated --mode clean")
        log("SUMMARY", "     (MUST stay 0 violations / 23020 rows -- every v5 expectation mirrors the generator)")
        log("SUMMARY", "  4. .venv\\Scripts\\python.exe dq_main.py --tree data\\corrupted --mode corrupt")
        log("SUMMARY", "     (expect ~856 violations = 732 previous + 57 not_null + 65 value_in_range + 2 row_count;")
        log("SUMMARY", "      floor 799 if the null column is not status; either way ok=True, exit 0)")
        log("SUMMARY", "  5. git add -A && git commit -m \"feat(dq): milestone 7c -- wire confirmed expectations: row_count x6, quantity range 1..100, sales_rep FK, status required (ADR-027)\"")
    log("SUMMARY", f"done ({'dry-run' if args.dry_run else 'applied'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
