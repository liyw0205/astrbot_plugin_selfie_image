#!/usr/bin/env python3
"""Validate runtime-owned COS and leg prompt pool compatibility contracts."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from astrbot_plugin_selfie_image.cos.cos_looks_loader import (
    COS_JSON_LOOK_SETS,
    COS_JSON_POSES,
    COS_JSON_SCENES,
    validate_json_catalog,
)
from astrbot_plugin_selfie_image.cos.leg_focus import (
    LEGFOCUS_HOSIERY_STYLES,
    LEGFOCUS_OUTFIT_POOL,
    LEGFOCUS_POSE_POOL,
)


def main() -> int:
    issues = validate_json_catalog()
    if issues:
        for issue in issues:
            print(f"[prompt-pools] {issue}", file=sys.stderr)
        return 1
    print(
        "[prompt-pools] COS looks={looks} poses={poses} scenes={scenes}; "
        "legs outfits={outfits} hosiery={hosiery} poses={leg_poses}".format(
            looks=len(COS_JSON_LOOK_SETS),
            poses=len(COS_JSON_POSES),
            scenes=len(COS_JSON_SCENES),
            outfits=len(LEGFOCUS_OUTFIT_POOL),
            hosiery=len(LEGFOCUS_HOSIERY_STYLES),
            leg_poses=len(LEGFOCUS_POSE_POOL),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
