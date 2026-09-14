# -*- coding: utf-8 -*-
"""Deterministic drop-to-fit pass for the capacity-failing archives.

For every capacity-failing archive (package reflow does not fit) we greedily drop
the translated message that most reduces its container's byte cost -- the message
whose Korean contributes the most UNIQUE local glyphs not shared by other kept
messages of the same container, tie-broken by text length then msgid -- and
re-simulate JUST that archive's failing package (via rebuild_archive on the cached
archive bytes) until it fits.  Dropped messages revert to Japanese and are logged
to coverage_report.json with reason="capacity".

Runs on the post-fixup plan (build_full_plan -> plan_fixups -> fit_repair).  Backs
up its input to patch_plan_full.prefix.json, then writes the repaired plan to
patch_plan_full.json.  Fail-closed: never weakens a patcher invariant; the only
lever is removing whole message translations (which are provably safe to revert).
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FULL_KO)
import so3_full_patch as P  # noqa: E402
from patch_hyda_dialogue import GLOBAL_CODE_MAP  # noqa: E402

ISO = Path(r"D:\ps2\Star Ocean Till the End of Time Director's Cut (Disc 1).iso")
PLAN = os.path.join(FULL_KO, "patch_plan_full.json")
PREFIX_BACKUP = os.path.join(FULL_KO, "patch_plan_full.prefix.json")
COVERAGE = os.path.join(FULL_KO, "coverage_report.json")
PROGRESS = os.path.join(FULL_KO, "fit_repair_progress.md")
SIM_REPORT = os.path.join(FULL_KO, "sim_report.json")
CATALOG = str(P.CONTAINER_CATALOG_PATH)
INVENTORY = os.path.join(FULL_KO, "inventory_containers.json")
UNITS = os.path.join(FULL_KO, "translation_units.jsonl")

GLOBAL_CHARS = set(GLOBAL_CODE_MAP)
FIXUP_CONTAINERS = {  # 12-char prefixes of the 4 format-fix containers
    "35822684140e", "5d9ebab56315", "e82d0ab97341", "430a3a5ab7e3", "68108b769384",
}


def load_unique() -> dict:
    return json.load(open(PLAN, encoding="utf-8"))["unique"]


def expand_streams(unique_int: dict[str, dict[int, object]]):
    """(archive,stream) -> shared tr dict, and (archive,stream) -> file_sha."""
    wanted = set(unique_int)
    stream_tr: dict[tuple[int, int], dict[int, object]] = {}
    stream_sha: dict[tuple[int, int], str] = {}
    arch_of_sha: dict[str, set[int]] = defaultdict(set)
    with open(CATALOG, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            sha = row["file_sha256"].lower()
            if sha in wanted:
                key = (int(row["archive_id"]), int(row["stream_id"]))
                stream_tr[key] = unique_int[sha]
                stream_sha[key] = sha
                arch_of_sha[sha].add(int(row["archive_id"]))
    return stream_tr, stream_sha, arch_of_sha


def jp_text_map() -> dict[tuple[str, int], str]:
    """(file_sha256, msgid) -> jp body excerpt, via inventory + units."""
    key_of: dict[tuple[str, int], str] = {}
    ic = json.load(open(INVENTORY, encoding="utf-8"))
    conts = ic["containers"] if isinstance(ic, dict) else ic
    for c in conts:
        sha = c["file_sha256"]
        for m in c.get("messages", []):
            if m.get("text_key"):
                key_of[(sha, int(m["id"]))] = m["text_key"]
    jp_of_key: dict[str, str] = {}
    with open(UNITS, encoding="utf-8") as f:
        for line in f:
            u = json.loads(line)
            jp_of_key[u["key"]] = (u.get("jp_body") or "").replace("\n", " ")
    return {km: jp_of_key.get(tk, "") for km, tk in key_of.items()}


# --- scoring ---------------------------------------------------------------

def container_unique_glyphs(tr: dict[int, object]) -> dict[int, set[str]]:
    """Per message: set of local (non-global) chars used by NO other kept msg."""
    per: dict[int, set[str]] = {}
    freq: dict[str, int] = defaultdict(int)
    for mid, e in tr.items():
        chars = set(P.strip_markers(e["korean"]))
        sp = e.get("speaker_korean")
        if sp:
            chars |= set(P.strip_markers(sp))
        chars -= GLOBAL_CHARS
        per[mid] = chars
        for ch in chars:
            freq[ch] += 1
    return {mid: {ch for ch in chars if freq[ch] == 1} for mid, chars in per.items()}


def pick_worst(subset: dict[tuple[int, int, int], dict[int, object]]):
    """(addr, msgid) of the highest-cost message across the given containers."""
    best = None
    best_key = None
    per_container_unique = {addr: container_unique_glyphs(tr) for addr, tr in subset.items()}
    for addr, tr in subset.items():
        uniq = per_container_unique[addr]
        for mid, e in tr.items():
            glyph_cost = len(uniq[mid])
            text_len = len(P.strip_markers(e["korean"]))
            sp = e.get("speaker_korean")
            if sp:
                text_len += len(P.strip_markers(sp))
            key = (glyph_cost, text_len, -mid)  # glyph desc, len desc, msgid asc
            if best_key is None or key > best_key:
                best_key = key
                best = (addr, mid, glyph_cost, text_len)
    return best


# --- per-archive repair ----------------------------------------------------

def archive_addresses(data: bytes, streams: list[tuple[int, int]], manifest):
    packages = P.parse_archive_layout(data)
    addr_stream: dict[tuple[int, int, int], tuple[int, int]] = {}
    for key in streams:
        src = int(manifest[key]["source_offset"])
        addr = P.locate_stream(packages, src)
        addr_stream[addr] = key
    return addr_stream


def repair_archive(archive_id, data, addr_stream, stream_tr, stream_sha,
                   drops: list, font):
    """Drop messages until the archive fits. Mutates shared tr dicts."""

    def active_slice():
        return {addr: stream_tr[key] for addr, key in addr_stream.items() if stream_tr[key]}

    # confirm it actually fails now (post-fixup live plan)
    try:
        P.rebuild_archive(data, active_slice(), font_path=font, archive_id=archive_id)
        return 0
    except P.FitError as exc:
        failing_pkg = exc.info.get("package")
    n_drop = 0
    while True:
        subset = {addr: tr for addr, tr in active_slice().items()
                  if failing_pkg is None or addr[0] == failing_pkg}
        if not subset or not any(subset.values()):
            # nothing left to drop in this package -> widen to whole archive
            subset = active_slice()
            if not any(subset.values()):
                raise P.FitError("archive still overflows with no translations left",
                                 archive=archive_id, package=failing_pkg)
        worst = pick_worst(subset)
        if worst is None:
            raise P.FitError("no droppable message", archive=archive_id, package=failing_pkg)
        addr, mid, gcost, tlen = worst
        key = addr_stream[addr]
        sha = stream_sha[key]
        entry = stream_tr[key].pop(mid)  # mutate the shared unique dict
        drops.append({"file_sha": sha, "msgid": mid, "archive": archive_id,
                      "glyph_cost": gcost, "text_len": tlen,
                      "korean_excerpt": P.strip_markers(entry["korean"])[:40]})
        n_drop += 1
        # re-test the failing package only (fast); fall through to full check on fit
        pkg_subset = {addr: tr for addr, tr in active_slice().items()
                      if failing_pkg is None or addr[0] == failing_pkg}
        try:
            if pkg_subset and any(pkg_subset.values()):
                P.rebuild_archive(data, pkg_subset, font_path=font, archive_id=archive_id)
        except P.FitError:
            continue  # same package still over -> drop again
        # failing package now fits; verify the whole archive (other packages)
        try:
            P.rebuild_archive(data, active_slice(), font_path=font, archive_id=archive_id)
            return n_drop
        except P.FitError as exc2:
            failing_pkg = exc2.info.get("package")
            continue


def main() -> int:
    import argparse
    import shutil
    global ISO, PLAN, PREFIX_BACKUP, COVERAGE, CATALOG, INVENTORY, UNITS
    ap = argparse.ArgumentParser(
        description="Drop-to-fit repair (defaults = disc 1; pass explicit paths for disc 2)")
    ap.add_argument("--iso", type=Path, default=ISO)
    ap.add_argument("--plan", default=PLAN)
    ap.add_argument("--prefix-backup", default=None,
                    help="default: <plan>.prefix.json next to the plan")
    ap.add_argument("--coverage", default=COVERAGE)
    ap.add_argument("--catalog", default=CATALOG)
    ap.add_argument("--inventory", default=INVENTORY)
    ap.add_argument("--units", default=UNITS)
    ap.add_argument("--stream-manifest", type=Path, default=P.STREAM_MANIFEST_PATH)
    args = ap.parse_args()
    ISO = Path(args.iso)
    PLAN = args.plan
    PREFIX_BACKUP = args.prefix_backup or (
        os.path.splitext(PLAN)[0] + ".prefix.json")
    COVERAGE = args.coverage
    CATALOG = args.catalog
    INVENTORY = args.inventory
    UNITS = args.units
    manifest_path = Path(args.stream_manifest)
    t0 = time.time()
    font = P.DEFAULT_FONT_PATH
    shutil.copyfile(PLAN, PREFIX_BACKUP)  # pre-repair backup
    unique = load_unique()
    total_planned = sum(len(tr) for tr in unique.values())
    unique_int = {sha: {int(m): v for m, v in tr.items()} for sha, tr in unique.items()}

    stream_tr, stream_sha, arch_of_sha = expand_streams(unique_int)
    manifest = P.load_stream_manifest(manifest_path)
    index = P.read_index(ISO)
    arch_streams: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for (a, s) in stream_tr:
        arch_streams[a].append((a, s))

    drops: list = []
    errors: list = []
    repaired = 0
    per_archive_drops: dict[int, int] = {}

    # Self-contained failure discovery: build the full plan and simulate it once.
    # (No dependency on any external sim_report file, which the final CLI sim
    # would clobber.)  The tr dicts are shared refs into unique_int, so repairs
    # below mutate the same objects we write out.
    print("building plan + simulating to find capacity failures ...", flush=True)
    full_plan = P.build_plan(ISO, stream_tr, manifest)
    sim = P.simulate(ISO, full_plan, font_path=font)
    cap = sorted({f["archive"] for f in sim["failures"]
                  if "does not fit" in f.get("error", "")})
    for f in sim["failures"]:
        if "does not fit" not in f.get("error", ""):
            errors.append({"archive": f["archive"], "error": f.get("error", "")[:200]})
    print(f"  sim: {sim['archives_failed']} failed "
          f"({len(cap)} capacity, {len(errors)} non-fit) in {time.time()-t0:.0f}s",
          flush=True)
    targets = [a for a in cap if a in arch_streams]
    print(f"target archives: {len(targets)} (planned total messages {total_planned})")
    with ISO.open("rb") as handle:
        for i, archive_id in enumerate(targets):
            start = index[archive_id] * P.SECTOR
            size = index[0x1800 + archive_id] * P.SECTOR
            handle.seek(start)
            data = handle.read(size)
            addr_stream = archive_addresses(data, arch_streams[archive_id], manifest)
            try:
                n = repair_archive(archive_id, data, addr_stream, stream_tr, stream_sha,
                                   drops, font)
            except Exception as exc:  # keep going; surface at the end
                errors.append({"archive": archive_id, "error": f"{type(exc).__name__}: {exc}"})
                print(f"  [{i+1}/{len(targets)}] archive {archive_id}: ERROR {exc}",
                      flush=True)
                continue
            per_archive_drops[archive_id] = n
            if n:
                repaired += 1
            print(f"  [{i+1}/{len(targets)}] archive {archive_id}: {n} drops "
                  f"({time.time()-t0:.0f}s elapsed)", flush=True)

    # write repaired plan back (unique dict, str msgids)
    new_unique = {sha: {str(m): v for m, v in tr.items()} for sha, tr in unique_int.items() if tr}
    json.dump({"unique": new_unique}, open(PLAN, "w", encoding="utf-8"), ensure_ascii=False)

    # coverage report
    jp = jp_text_map()
    dropped_by_key: dict[tuple[str, int], dict] = {}
    for d in drops:
        k = (d["file_sha"], d["msgid"])
        rec = dropped_by_key.setdefault(k, {
            "file_sha": d["file_sha"][:12], "msgid": d["msgid"], "reason": "capacity",
            "archive_samples": [], "jp_text_excerpt": jp.get(k, "")[:80],
            "korean_excerpt": d["korean_excerpt"]})
        if d["archive"] not in rec["archive_samples"]:
            rec["archive_samples"].append(d["archive"])
    dropped = sorted(dropped_by_key.values(), key=lambda r: (r["file_sha"], r["msgid"]))
    total_after = sum(len(tr) for tr in unique_int.values())
    coverage = {
        "total_messages_planned": total_planned,
        "total_messages_translated_after_repair": total_after,
        "dropped_count": len(dropped),
        # format units were all repaired in plan_fixups (0 dropped); the only
        # messages left untranslated here are capacity drops.
        "summary_by_reason": {"capacity": len(dropped), "format": 0},
        "fraction_translated": round(total_after / total_planned, 6),
        "fraction_dropped_capacity": round(len(dropped) / total_planned, 6),
        "format_partial_note": (
            "dynamic-token units (12 MEMORY CARD system messages in containers "
            "430a3a5ab7e3 / 68108b769384 / e82d0ab97341): body fully translated, "
            "header line (MEMORY CARD差込口[registry]の) kept verbatim to preserve the "
            "9280 registry-string insert (keep_speaker). No message dropped for format."),
        "dropped": dropped,
    }
    json.dump(coverage, open(COVERAGE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\nrepaired {repaired} archives, dropped {len(dropped)} unique messages "
          f"({sum(per_archive_drops.values())} drop-events) in {time.time()-t0:.0f}s")
    print(f"translated {total_after}/{total_planned} "
          f"({100.0*total_after/total_planned:.3f}%)")
    if errors:
        print(f"!! {len(errors)} archive(s) errored (non-fit):")
        for e in errors:
            print("   ", e["archive"], e["error"][:160])
    print("-> wrote", PLAN, "and", COVERAGE)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
