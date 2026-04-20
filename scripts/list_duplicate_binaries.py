#!/usr/bin/env python3
"""List repos that have binaries with duplicate base names."""

import json
from pathlib import Path

METADATA_PATH = Path(__file__).resolve().parent.parent / "data" / "metadata.json"


def main():
    meta = json.loads(METADATA_PATH.read_text())

    for repo_name, info in sorted(meta["repos"].items()):
        if not info.get("binaries"):
            continue

        # Check each variant for duplicate base names
        first_variant = True
        for variant, bins in info["binaries"].items():
            seen: dict[str, list[str]] = {}
            for b in bins:
                # Strip _N suffix to get the base name
                parts = b.rsplit("_", 1)
                base = parts[0] if len(parts) == 2 and parts[1].isdigit() else b
                seen.setdefault(base, []).append(b)

            dupes = {k: v for k, v in seen.items() if len(v) > 1}
            if dupes and first_variant:
                first_variant = False
                print(f"{repo_name}:")
                for base, names in sorted(dupes.items()):
                    print(f"  {base} ({len(names)})")


if __name__ == "__main__":
    main()
