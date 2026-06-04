#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=wrong-import-position
"""Apply or preview a QwenPaw product bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from qwenpaw.product import apply_product_bundle, load_product_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_path", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    bundle = load_product_bundle(args.bundle_path)
    result = apply_product_bundle(bundle, dry_run=args.dry_run)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
