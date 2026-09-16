#!/usr/bin/env python3
"""Compatibility wrapper for the consolidated media prompt script.

The former implementation duplicated parser loading, downloading, and frame
extraction. Keep the old filename as a thin command-name compatibility layer.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    target = Path(__file__).with_name("video_prompt_frames.py")
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
