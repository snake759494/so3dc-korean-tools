#!/usr/bin/env python3
"""v1.1.1 supplemental image apply for DISC 1 (John atlas triple-copy fix).

Applies the full 10-extent disc-1 image patch (the 8 v1.1.0 members plus the
two previously unpatched byte-identical John atlas copies 2255:38038 and
2256:38045) onto the v1.0.0 TEXT-patched ISO, then runs the independent
verifier.  Extents come from john_supplement_d1.json; sources are the existing
patched/*.slz repaints (John copies reuse 2254_38031).  Fail-closed throughout.

Usage (defaults shown):
  python john_supplement_d1_apply.py \
      --input  "D:\\ps2\\SO3_DC_Disc1_Korean_Full.iso" \
      --output "D:\\ps2\\SO3_DC_Disc1_Korean_Full_Img_v111.iso"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

WS = Path(r"C:\Users\Jay\Documents\Codex\2026-07-13\d-3-ps2\work\img_ko")
PLAN = WS / "john_supplement_d1.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path,
                    default=Path(r"D:\ps2\SO3_DC_Disc1_Korean_Full.iso"),
                    help="v1.0.0 TEXT-patched disc-1 ISO")
    ap.add_argument("--output", type=Path,
                    default=Path(r"D:\ps2\SO3_DC_Disc1_Korean_Full_Img_v111.iso"))
    ap.add_argument("--report", type=Path,
                    default=WS / "apply_image_report_v111.json")
    ap.add_argument("--verify-report", type=Path,
                    default=WS / "verify_image_report_v111.json")
    args = ap.parse_args()

    apply_cmd = [sys.executable, str(WS / "apply_image_patch.py"),
                 "--input", str(args.input), "--output", str(args.output),
                 "--transfer-plan", str(PLAN), "--report", str(args.report)]
    verify_cmd = [sys.executable, str(WS / "verify_image_patch.py"),
                  "--input", str(args.input), "--output", str(args.output),
                  "--transfer-plan", str(PLAN),
                  "--report", str(args.verify_report)]
    for cmd in (apply_cmd, verify_cmd):
        print("+", " ".join(cmd), flush=True)
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"FAILED (exit {rc}); stopping", file=sys.stderr)
            return rc
    print(json.dumps({"ok": True, "output": str(args.output),
                      "reports": [str(args.report), str(args.verify_report)]},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
