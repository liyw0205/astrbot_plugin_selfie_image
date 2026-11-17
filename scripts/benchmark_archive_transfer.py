"""Compare synthetic JSON/base64 ingress with bounded archive transfer ingress."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import tracemalloc
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def benchmark(mode: str, size_mib: int) -> dict:
    from astrbot_plugin_selfie_image.features.archive_transfer import ArchiveTransferStore, CHUNK_SIZE

    with tempfile.TemporaryDirectory(prefix="selfie-transfer-bench-") as temporary:
        root = Path(temporary)
        source = root / "synthetic.bin"
        generator = random.Random(20261117)
        with source.open("wb") as output:
            for _ in range(size_mib):
                output.write(generator.randbytes(CHUNK_SIZE))
        request_file = root / "request.json"
        # Build the simulated wire payload outside the measured receiver path.
        if mode == "legacy":
            with source.open("rb") as incoming, request_file.open("wb") as output:
                output.write(b'{"content_base64":"')
                while chunk := incoming.read(3 * 256 * 1024):
                    output.write(base64.b64encode(chunk))
                output.write(b'"}')
        with ArchiveTransferStore(root / "transfers") as store:
            tracemalloc.start()
            started = time.perf_counter()
            if mode == "legacy":
                wire = request_file.read_bytes()
                body = json.loads(wire)
                content = base64.b64decode(body["content_base64"], validate=True)
                target = root / "decoded.zip"
                target.write_bytes(content)
                digest = hashlib.sha256(content).hexdigest()
            else:
                session = store.start_upload("backup", source.stat().st_size, source.name)
                offset = 0
                with source.open("rb") as incoming:
                    while chunk := incoming.read(CHUNK_SIZE):
                        # One realistic bridge JSON parse and base64 decode per chunk.
                        wire = json.dumps({"offset": offset, "content_base64": base64.b64encode(chunk).decode(),
                                           "crc32": f"{zlib.crc32(chunk):08x}"})
                        body = json.loads(wire)
                        decoded = base64.b64decode(body["content_base64"], validate=True)
                        result = store.append_upload(session["id"], body["offset"], decoded, body["crc32"])
                        offset = result["next_offset"]
                with store.consume_upload(session["id"]) as (_path, info):
                    digest = info["sha256"]
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        expected = hashlib.sha256()
        with source.open("rb") as incoming:
            while chunk := incoming.read(CHUNK_SIZE):
                expected.update(chunk)
        if expected.hexdigest() != digest:
            raise RuntimeError("synthetic transfer content mismatch")
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_bytes = int(rss if sys.platform == "darwin" else rss * 1024)
        except (ImportError, AttributeError):
            rss_bytes = None
        return {"mode": mode, "size_mib": size_mib, "seconds": round(elapsed, 4),
                "python_peak_bytes": peak, "process_peak_rss_bytes": rss_bytes, "verified": True,
                "scope": "Synthetic ingress/staging only; no ZIP validation, restore, browser or host. RSS includes setup. Bounded includes one chunk's sender JSON construction."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("legacy", "bounded", "matrix"), default="matrix")
    parser.add_argument("--size-mib", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.size_mib <= 128:
        parser.error("size-mib must be 1..128")
    if args.mode == "matrix":
        results = []
        for size in (16, 64, 128):
            for mode in ("legacy", "bounded"):
                completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                    "--mode", mode, "--size-mib", str(size)], check=True, text=True, capture_output=True)
                results.append(json.loads(completed.stdout))
                print(json.dumps(results[-1], sort_keys=True), flush=True)
    else:
        print(json.dumps(benchmark(args.mode, args.size_mib), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
