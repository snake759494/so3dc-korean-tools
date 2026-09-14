# -*- coding: utf-8 -*-
"""Dedupe name lists and split into translation batches for the glossary workflow."""
import io, json, sys, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
WS = r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2"
RAW = os.path.join(WS, r"work\full_ko\glossary_terms_raw.json")
OUT = os.path.join(WS, r"work\full_ko\glossary_batches.json")

NAME_CATS = ["item_name", "valuable", "symbology", "place_facility",
             "battle_skill", "enemy", "ic_short", "battle_db_other"]
BATCH = 110

def main():
    raw = json.load(open(RAW, encoding="utf-8"))
    seen = {}
    for cat in NAME_CATS:
        for key, text in raw.get(cat, {}).items():
            t = text.strip()
            if not t or len(t) > 18 or "\n" in t:
                continue  # sentences go to the main translation phase
            if t not in seen:
                seen[t] = cat
    terms = sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))
    batches = []
    for i in range(0, len(terms), BATCH):
        chunk = terms[i:i + BATCH]
        batches.append({
            "batch_id": len(batches),
            "terms": [{"jp": t, "cat": c} for t, c in chunk],
        })
    json.dump({"total_terms": len(terms), "batches": batches},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("unique name terms:", len(terms), "batches:", len(batches))
    from collections import Counter
    print(Counter(c for _, c in terms))

if __name__ == "__main__":
    main()
