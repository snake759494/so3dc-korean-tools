# -*- coding: utf-8 -*-
"""Merge all translated batches into a patch plan for so3_full_patch.py.

Plan format:  {"unique": {file_sha256: {str(msgid): {korean, speaker_korean}}}}
so3_full_patch expands each unique container to every on-disc occurrence.

Chain:
  tr_out/batch_*_ko.json  -> {text_key: {korean, speaker_korean}}
  inventory_containers.json -> per container(file_sha256): messages[{id, text_key, speaker}]
  => plan.unique[file_sha256][msgid] = translation, for every message whose
     text_key was translated.
"""
import argparse
import glob
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
FULL_KO = os.path.dirname(os.path.abspath(__file__))
TR_OUT = os.path.join(FULL_KO, "tr_out")
INVENTORY = os.path.join(FULL_KO, "inventory_containers.json")
OUT = os.path.join(FULL_KO, "patch_plan_full.json")


def main():
    ap = argparse.ArgumentParser(
        description="Merge translated batches into a patch plan (defaults = disc 1)")
    ap.add_argument("--tr-out-dir", default=TR_OUT)
    ap.add_argument("--inventory", default=INVENTORY)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    tr_out_dir, inventory_path, out_path = args.tr_out_dir, args.inventory, args.out

    # 1. text_key -> translation
    tr = {}
    dup_conflicts = 0
    for path in sorted(glob.glob(os.path.join(tr_out_dir, "batch_*_ko.json"))):
        doc = json.load(open(path, encoding="utf-8"))
        for t in doc["translations"]:
            k = t["key"]
            rec = {"korean": t["korean"]}
            if t.get("speaker_korean") not in (None, ""):
                rec["speaker_korean"] = t["speaker_korean"]
            if k in tr and tr[k] != rec:
                dup_conflicts += 1  # same text_key, different translation across batches
            tr[k] = rec
    print("translated text_keys:", len(tr), "cross-batch conflicts:", dup_conflicts)

    # 2. inventory containers
    ic = json.load(open(inventory_path, encoding="utf-8"))
    conts = ic["containers"] if isinstance(ic, dict) else ic

    unique = {}
    n_msg_planned = 0
    n_missing_tr = 0
    missing_keys = set()
    containers_touched = 0
    for c in conts:
        sha = c["file_sha256"]
        per = {}
        for m in c.get("messages", []):
            tk = m.get("text_key")
            if not tk:
                continue  # message carries no translatable JP unit
            t = tr.get(tk)
            if t is None:
                n_missing_tr += 1
                missing_keys.add(tk)
                continue
            per[str(m["id"])] = t
            n_msg_planned += 1
        if per:
            unique[sha] = per
            containers_touched += 1

    plan = {"unique": unique}
    json.dump(plan, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
    print("containers in plan:", containers_touched, "/", len(conts))
    print("messages planned:", n_msg_planned)
    print("messages w/ untranslated text_key:", n_missing_tr,
          "(distinct keys:", len(missing_keys), ")")
    print("-> " + out_path, "(%.1f MB)" % (os.path.getsize(out_path) / 1048576))
    if missing_keys:
        sample = list(missing_keys)[:10]
        print("  sample missing keys:", sample)


if __name__ == "__main__":
    main()
