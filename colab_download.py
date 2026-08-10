#!/usr/bin/env python3
"""
colab_download.py — Robust & Fast Colab downloader.

Bypasses Google Colab API's ~2GB file download limitation (HTTP 500 error)
by streaming remote files in base64 chunks via `colab exec` with parallel workers.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB per slice
MAX_WORKERS = 8


def get_remote_file_size(session: str, remote_path: str) -> int:
    script = f"""import os, sys
p = '''{remote_path}'''
if not os.path.exists(p):
    print("NOT_FOUND")
else:
    print(os.path.getsize(p))
"""
    tmp_py = f"/tmp/get_size_{os.getpid()}.py"
    with open(tmp_py, "w") as f:
        f.write(script)
    try:
        res = subprocess.run(
            ["colab", "exec", "-s", session, "-f", tmp_py],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = res.stdout.strip()
        if "NOT_FOUND" in output or not output.isdigit():
            return -1
        return int(output)
    finally:
        if os.path.exists(tmp_py):
            os.remove(tmp_py)


def download_slice(session: str, remote_path: str, offset: int, size: int) -> bytes:
    script = f"""import sys, base64
p = '''{remote_path}'''
with open(p, 'rb') as f:
    f.seek({offset})
    data = f.read({size})
    sys.stdout.write(base64.b64encode(data).decode('ascii'))
"""
    tmp_py = f"/tmp/read_slice_{os.getpid()}_{offset}.py"
    with open(tmp_py, "w") as f:
        f.write(script)

    try:
        for attempt in range(1, 4):
            try:
                res = subprocess.run(
                    ["colab", "exec", "-s", session, "-f", tmp_py],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                b64_str = res.stdout.strip()
                if b64_str:
                    data = base64.b64decode(b64_str)
                    if len(data) == size or (size > 0 and len(data) > 0):
                        return data
            except Exception:
                pass
            time.sleep(1.0)
    finally:
        if os.path.exists(tmp_py):
            os.remove(tmp_py)

    raise RuntimeError(f"Failed to download slice offset {offset} (size {size})")


def download_colab_file(session: str, remote_path: str, local_path: Path) -> bool:
    print(f"==> Querying file size for {remote_path} on Colab session '{session}'...")
    total_size = get_remote_file_size(session, remote_path)
    if total_size <= 0:
        print(f"Error: Remote file missing or 0 bytes: {remote_path}")
        return False

    mb_total = total_size / (1024 * 1024)
    print(f"==> Downloading {remote_path} ({mb_total:.1f} MB) to {local_path}...")

    local_path.parent.mkdir(parents=True, exist_ok=True)
    temp_local = local_path.with_suffix(local_path.suffix + ".part")

    # Allocate file
    with open(temp_local, "wb") as f:
        f.truncate(total_size)

    slices: list[tuple[int, int]] = []
    offset = 0
    while offset < total_size:
        size = min(CHUNK_SIZE, total_size - offset)
        slices.append((offset, size))
        offset += size

    num_slices = len(slices)
    print(f"==> Downloading {num_slices} chunks using {MAX_WORKERS} parallel threads...")

    completed_bytes = 0
    t0 = time.time()

    def worker(off: int, sz: int) -> tuple[int, bytes]:
        data = download_slice(session, remote_path, off, sz)
        return off, data

    failed = False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker, off, sz): (off, sz) for off, sz in slices}
        with open(temp_local, "r+b") as out_f:
            for fut in as_completed(futures):
                try:
                    off, data = fut.result()
                    out_f.seek(off)
                    out_f.write(data)
                    completed_bytes += len(data)
                    elapsed = max(0.1, time.time() - t0)
                    speed_mb = (completed_bytes / (1024 * 1024)) / elapsed
                    pct = (completed_bytes / total_size) * 100
                    sys.stdout.write(f"\r==> Progress: {pct:.1f}% ({completed_bytes/(1024*1024):.1f}/{mb_total:.1f} MB) [{speed_mb:.1f} MB/s]   ")
                    sys.stdout.flush()
                except Exception as e:
                    print(f"\nWorker error: {e}")
                    failed = True
                    break

    print()
    if failed:
        if temp_local.exists():
            temp_local.unlink()
        return False

    temp_local.rename(local_path)
    print(f"✔ File successfully downloaded and saved to {local_path}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Colab Fast Downloader")
    parser.add_argument("session", help="Colab session ID/name")
    parser.add_argument("remote_path", help="Remote path on Colab")
    parser.add_argument("local_path", help="Local output file path")
    args = parser.parse_args()

    ok = download_colab_file(args.session, args.remote_path, Path(args.local_path))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
