# -*- coding: utf-8 -*-
"""Deterministic plan post-processor for the 4 format-fix units.

Applied AFTER build_full_plan.py so that `build_full_plan -> plan_fixups` is
reproducible.  Fixes two classes of unit the raw inventory delivered in a shape
the fail-closed encoder rejects:

1. Variant-marker units (text_key 9de782ba3371, d804565000d3)
   The inventory extracted a 9380 character-name that sits inside a body
   `8880 <9380 name> 8980` construct (color span, NO 8780 speaker delimiter) as
   speaker_korean.  The encoder sees that construct as a body with two positional
   controls (8880 -> ⟦1⟧, 8980 -> ⟦2⟧) and NO speaker construct, and the original
   first body line is exactly those two markers (the 9380 name is dropped).
   Fix: reshape into body-marker form -- prepend a first line "⟦1⟧<name>⟦2⟧" and
   clear speaker_korean.  markers become [1,2] and the literal Korean name sits
   between them, matching test_variant_speaker_pattern_as_body_markers.

2. Dynamic-token units (text_key 6737081170be, 8d7bf7e3f9cc)
   speaker_korean carried a literal ⟦문자열#1c⟧ registry-string token.  In the
   original segment that 9280 registry-string insert lives in the PREFIX
   (`MEMORY CARD差込口[9280]の`), before the 8780 speaker delimiter -- it is NOT a
   body positional control the encoder exposes as a ⟦n⟧ marker, so it cannot be
   mapped to a positional marker.  Rather than drop the whole unit, we keep the
   header (prefix + speaker glyph の, including the 9280 insert) verbatim via
   keep_speaker=True and translate the body (the substantive message).  This
   preserves the registry insert and translates as much as the invariants allow.

Both fixes were verified end-to-end against the real original segments with
so3_full_patch.rebuild_container (full decode-back proof) -- see probe_fix.py.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = os.path.dirname(os.path.abspath(__file__))
INVENTORY = os.path.join(FULL_KO, "inventory_containers.json")
PLAN = os.path.join(FULL_KO, "patch_plan_full.json")
LOG = os.path.join(FULL_KO, "plan_fixups_log.json")

# Variant-marker units are structural (the encoder rejects them with a positional
# marker mismatch); they carry NO bracket token, so they must be addressed by
# their known text_keys.  The inventory's variant-speaker pattern is fixed at
# exactly these two unique units (see MASTER_PLAN "화자 변형 패턴").
VARIANT_KEYS = {"9de782ba3371", "d804565000d3"}
# Dynamic-token units are detected structurally: any speaker_korean carrying a
# ⟦...⟧ token that is not a valid inline marker (⟦G:hex⟧).  A ⟦문자열#NN⟧
# registry-string insert lives in the segment PREFIX (before the 8780 speaker
# delimiter), so the encoder never exposes it as a body ⟦n⟧ marker; we preserve
# the whole header (prefix + speaker glyph, including the 9280 insert) verbatim
# via keep_speaker and translate the body.
_VALID_INLINE = re.compile(r"^G:[0-9a-fA-F]{8}$")             # speaker fields
_VALID_BODY = re.compile(r"^(P|[0-9]+|G:[0-9a-fA-F]{8})$")    # body text


def _has_bad_bracket(s: str | None, valid: re.Pattern = _VALID_INLINE) -> bool:
    if not s:
        return False
    for m in re.finditer(r"⟦([^⟧]*)⟧", s):
        if not valid.match(m.group(1)):
            return True
    stripped = re.sub(r"⟦[^⟧]*⟧", "", s)
    return "⟦" in stripped or "⟧" in stripped


def _variant_locations(inventory_path: str) -> set[tuple[str, int]]:
    """(file_sha256, msgid) whose text_key is a known variant-marker unit."""
    ic = json.load(open(inventory_path, encoding="utf-8"))
    conts = ic["containers"] if isinstance(ic, dict) else ic
    out: set[tuple[str, int]] = set()
    for c in conts:
        sha = c["file_sha256"]
        for m in c.get("messages", []):
            if m.get("text_key") in VARIANT_KEYS:
                out.add((sha, int(m["id"])))
    return out


def apply_fixups(unique: dict, inventory_path: str = INVENTORY) -> tuple[dict, list[dict]]:
    """Rewrite the plan's `unique` dict in place; return (unique, log entries)."""
    log: list[dict] = []

    # 1. dynamic-token / registry-insert speakers (pattern-based, catches all).
    for sha, container in unique.items():
        for mid, entry in list(container.items()):
            if _has_bad_bracket(entry.get("speaker_korean")):
                container[mid] = {"korean": entry["korean"], "keep_speaker": True}
                log.append({"file_sha": sha[:12], "msgid": int(mid),
                            "action": "dynamic_token_keep_speaker",
                            "dropped_speaker": entry.get("speaker_korean")})

    # 2. variant speaker-as-body-markers (key-based, the two known units).
    for sha, mid in sorted(_variant_locations(inventory_path)):
        container = unique.get(sha)
        entry = container.get(str(mid)) if container else None
        if entry is None:
            log.append({"file_sha": sha[:12], "msgid": mid,
                        "action": "skip", "why": "variant unit not in plan"})
            continue
        speaker = entry.get("speaker_korean")
        korean = entry.get("korean")
        if not speaker or "\n" in speaker or "⟦" in speaker or "⟧" in speaker:
            log.append({"file_sha": sha[:12], "msgid": mid,
                        "action": "skip", "why": f"unexpected speaker {speaker!r}"})
            continue
        container[str(mid)] = {"korean": f"⟦1⟧{speaker}⟦2⟧\n{korean}"}
        log.append({"file_sha": sha[:12], "msgid": mid,
                    "action": "variant_marker_reshape",
                    "new_first_line": f"⟦1⟧{speaker}⟦2⟧"})

    return unique, log


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Plan fixups (defaults = disc 1; pass explicit paths for disc 2)")
    ap.add_argument("--plan", default=PLAN)
    ap.add_argument("--inventory", default=INVENTORY)
    ap.add_argument("--log", default=LOG)
    args = ap.parse_args()
    plan_path, log_path = args.plan, args.log
    doc = json.load(open(plan_path, encoding="utf-8"))
    unique = doc.get("unique", {})
    unique, log = apply_fixups(unique, inventory_path=args.inventory)
    doc["unique"] = unique
    json.dump(doc, open(plan_path, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    applied = [e for e in log if e["action"] != "skip"]
    by_action: dict[str, int] = {}
    for e in log:
        by_action[e["action"]] = by_action.get(e["action"], 0) + 1
    print(f"plan_fixups: {len(applied)} entries fixed, {len(log) - len(applied)} skipped")
    print("  by action:", by_action)
    for e in log:
        print(" ", e["file_sha"], e["msgid"], "->", e["action"])
    # safety net: no invalid bracket tokens must survive anywhere in the plan
    residual = sum(1 for c in unique.values() for entry in c.values()
                   if _has_bad_bracket(entry.get("speaker_korean"), _VALID_INLINE)
                   or _has_bad_bracket(entry.get("korean"), _VALID_BODY))
    print("  residual invalid-bracket entries:", residual)
    assert residual == 0, "invalid bracket tokens remain after fixups"
    print("-> wrote", plan_path, "and", log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
