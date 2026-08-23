#!/usr/bin/env python3
"""One-shot lab PC inventory. Prints JSONL. Stdlib only."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from classify import probe_machine  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe this PC for lab GPU inventory")
    parser.add_argument("--cuda-test", action="store_true", help="run a tiny torch.cuda tensor")
    parser.add_argument(
        "--append",
        type=Path,
        help="append the JSON line to this file (default: print only)",
    )
    args = parser.parse_args()
    record = probe_machine(cuda_test=args.cuda_test)
    line = json.dumps(record, sort_keys=True)
    print(line)
    if args.append:
        args.append.parent.mkdir(parents=True, exist_ok=True)
        with args.append.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


if __name__ == "__main__":
    main()
