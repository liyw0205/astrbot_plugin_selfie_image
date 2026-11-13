"""Offline reference-cache I/O benchmark; creates only temporary fixtures."""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import os
import platform
import runpy
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / "tests" / "conftest.py"))

from astrbot_plugin_selfie_image.core.providers import ImageReference
from astrbot_plugin_selfie_image.generation import generation_store


class ReadMeter:
    def __init__(self):
        self.read_calls = 0
        self.read_bytes = 0
        self.files = set()
        self.original_open = builtins.open

    def open(self, path, mode="r", *args, **kwargs):
        handle = self.original_open(path, mode, *args, **kwargs)
        if mode != "rb" or not Path(path).name.startswith("request_"):
            return handle
        meter = self

        class MeteredFile:
            def __enter__(self):
                handle.__enter__()
                return self

            def __exit__(self, *exc):
                return handle.__exit__(*exc)

            def read(self, *args):
                data = handle.read(*args)
                meter.read_calls += 1
                meter.read_bytes += len(data)
                meter.files.add(str(path))
                return data

            def __getattr__(self, name):
                return getattr(handle, name)

        return MeteredFile()


def reference_data(seed, size):
    head = b"\x89PNG\r\n\x1a\n" + seed.to_bytes(8, "big")
    return head + bytes([seed % 251]) * (size - len(head))


def baseline_method(ref):
    source = subprocess.run(
        ["git", "show", f"{ref}:generation/generation_store.py"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GenerationStoreMixin")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_save_or_reuse_reference_image")
    namespace = dict(vars(generation_store))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<reference-cache-baseline>", "exec"), namespace)
    return namespace[method.name]


def sample(args, method):
    with tempfile.TemporaryDirectory(prefix="selfie-reference-bench-") as temporary:
        store = generation_store.GenerationStoreMixin()
        store.data_dir = temporary
        store.generated_dir = str(Path(temporary) / "image_cache")
        cache = Path(store.generated_dir)
        cache.mkdir()
        for seed in range(args.files):
            (cache / f"request_legacy_{seed:04d}.png").write_bytes(reference_data(seed, args.bytes))
        cold_store = generation_store.GenerationStoreMixin()
        cold_store.data_dir = store.data_dir
        cold_store.generated_dir = store.generated_dir
        save = lambda seed: method(store, ImageReference(reference_data(seed, args.bytes), mime_type="image/png"))
        phases = {
            "cold_miss": lambda: save(args.files),
            "warm_misses": lambda: [save(args.files + 1 + offset) for offset in range(args.misses)],
            "deterministic_hit": lambda: save(args.files),
            "legacy_hit": lambda: save(0),
            "cold_legacy_hit": lambda: method(cold_store, ImageReference(reference_data(0, args.bytes), mime_type="image/png")),
        }
        result = {}
        for name, action in phases.items():
            meter = ReadMeter()
            start = time.perf_counter()
            with patch("builtins.open", meter.open):
                action()
            result[name] = {
                "milliseconds": round((time.perf_counter() - start) * 1000, 3),
                "read_calls": meter.read_calls,
                "files_read": len(meter.files),
                "read_bytes": meter.read_bytes,
            }
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="")
    parser.add_argument("--files", type=int, default=128)
    parser.add_argument("--bytes", type=int, default=128 * 1024)
    parser.add_argument("--misses", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.files < 1 or args.bytes < 16 or args.misses < 1 or args.repeats < 3:
        parser.error("files/misses must be positive, bytes >= 16, and repeats >= 3")
    method = baseline_method(args.baseline_ref) if args.baseline_ref else generation_store.GenerationStoreMixin._save_or_reuse_reference_image
    samples = [sample(args, method) for _ in range(args.repeats)]
    medians = {
        phase: {
            metric: statistics.median(sample[phase][metric] for sample in samples)
            for metric in samples[0][phase]
        }
        for phase in samples[0]
    }
    print(json.dumps({
        "implementation": args.baseline_ref or "worktree",
        "environment": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(), "cpu_count": os.cpu_count()},
        "fixture": {"legacy_files": args.files, "file_bytes": args.bytes, "warm_misses": args.misses, "repeats": args.repeats, "inflight_journal": True},
        "samples": samples,
        "medians": medians,
    }, indent=2))


if __name__ == "__main__":
    main()
