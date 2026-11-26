"""Validate and compare a local execution profile draft; never publish it."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.configuration_studio import MAX_BYTES, compare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    args = parser.parse_args()

    def read(path):
        with path.open("r", encoding="utf-8") as stream:
            return stream.read(MAX_BYTES + 1)

    result = compare(read(args.base), read(args.draft))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
