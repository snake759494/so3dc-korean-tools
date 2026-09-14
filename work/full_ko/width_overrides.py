# -*- coding: utf-8 -*-
"""Width-fit plan override module for the SO3 DC full-KO patch.

Runs in the plan pipeline AFTER plan_fixups.py and BEFORE fit_repair.py:

    build_full_plan.py -> plan_fixups.py -> width_overrides.py -> fit_repair.py

Purpose: replace the Korean text of the messages whose lines exceeded the
pixel-width budget (verify_full_iso.py check 7) with shorter Korean that fits,
recomputed with the verifier's own width engine (see width_oracle.py, which
reproduces check 7 exactly -- self-tested against the verifier's reported px).

Because it only *shortens* text (never grows it), running before fit_repair is
strictly capacity-safe: shorter text can only reduce a container's glyph/byte
footprint, so fit_repair re-checks capacity against text that is <= the text it
saw before, never more.

The concrete per-(file_sha, msgid) replacements live in
`width_overrides_data.json`, generated deterministically by gen_fixes.py from
the width oracle:
  * legit menu/dialogue/description text -> concise hand-written Korean
    (line count, ⟦P⟧ pages, every ⟦n⟧ marker, ⟦G:⟧ tokens and speaker handling
     preserved; glossary terms kept);
  * meaning-free decorative/garbled font-art (alien signage) -> trailing glyphs
    trimmed to fit;
  * proper nouns at their minimal form and pure runtime-substitution lines
    (dynamic-insert allowance alone exceeds the budget) are left as documented
    irreducibles (see width_irreducible.json) and are NOT in the data file, or
    carry their best (shortest sensible) form.

Every replacement preserves the ⟦n⟧/⟦P⟧/⟦G:⟧ marker set and the line count of
the text it replaces (asserted below).
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = os.path.dirname(os.path.abspath(__file__))
PLAN = os.path.join(FULL_KO, "patch_plan_full.json")
DATA = os.path.join(FULL_KO, "width_overrides_data.json")
LOG = os.path.join(FULL_KO, "width_overrides_log.json")

_TOKEN_RE = re.compile(r"⟦[^⟧]*⟧")


def _markers(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def apply_overrides(unique: dict, data_path: str = DATA) -> tuple[dict, list[dict]]:
    """Rewrite the plan's `unique` dict in place; return (unique, log entries)."""
    data = json.load(open(data_path, encoding="utf-8"))
    log: list[dict] = []
    for sha, mids in data.items():
        container = unique.get(sha)
        for mid, spec in mids.items():
            new_korean = spec["korean"]
            entry = container.get(mid) if container else None
            if entry is None:
                log.append({"file_sha": sha[:12], "msgid": int(mid),
                            "action": "skip", "why": "entry not in plan"})
                continue
            old_korean = entry.get("korean", "")
            # invariant: never change the marker set or the line count.
            if _markers(old_korean) != _markers(new_korean):
                log.append({"file_sha": sha[:12], "msgid": int(mid),
                            "action": "skip", "why": "marker set mismatch",
                            "old_markers": _markers(old_korean),
                            "new_markers": _markers(new_korean)})
                continue
            if old_korean.count("\n") != new_korean.count("\n"):
                log.append({"file_sha": sha[:12], "msgid": int(mid),
                            "action": "skip", "why": "line count mismatch"})
                continue
            entry["korean"] = new_korean          # preserve speaker_korean/keep_speaker
            log.append({"file_sha": sha[:12], "msgid": int(mid),
                        "action": "width_shorten",
                        "old_len": len(old_korean), "new_len": len(new_korean)})
    return unique, log


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Width overrides (defaults = disc 1; pass explicit paths for disc 2)")
    ap.add_argument("--plan", default=PLAN)
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--log", default=LOG)
    args = ap.parse_args()
    plan_path, log_path = args.plan, args.log
    doc = json.load(open(plan_path, encoding="utf-8"))
    unique = doc.get("unique", {})
    unique, log = apply_overrides(unique, data_path=args.data)
    doc["unique"] = unique
    json.dump(doc, open(plan_path, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    applied = [e for e in log if e["action"] == "width_shorten"]
    skipped = [e for e in log if e["action"] == "skip"]
    print(f"width_overrides: {len(applied)} messages shortened, {len(skipped)} skipped")
    for e in skipped:
        print("  SKIP", e["file_sha"], e["msgid"], "-", e.get("why"))
    print("-> wrote", plan_path, "and", log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
