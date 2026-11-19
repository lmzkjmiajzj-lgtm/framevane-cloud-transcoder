#!/usr/bin/env python3
import os
import sys
import subprocess
import concurrent.futures
import urllib.parse
from pathlib import Path

MEDIA_BASE = "https://framevane-media.onehacker.space"
UPLOAD_BASE = "https://framevane.onehacker.space/__r2_put_bin"
SECRET = os.environ["R2_BRIDGE_SECRET"].strip()
SHARD_IDX = int(os.environ.get("SHARD_IDX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))

all_keys = [l.strip() for l in Path("keys.txt").read_text().splitlines() if l.strip()]
my_keys = [k for i, k in enumerate(all_keys) if i % SHARD_TOTAL == SHARD_IDX]
print(f"[SHARD {SHARD_IDX}/{SHARD_TOTAL}] Assigned {len(my_keys)} / {len(all_keys)} videos (workers={MAX_WORKERS})", flush=True)

tmp_root = Path("/tmp/fv_transcode")
tmp_root.mkdir(parents=True, exist_ok=True)


def already_fast(url: str) -> tuple[bool, int]:
    """Quick HEAD + 64KB Range check: skip if already <= 4.5MB AND moov atom is in first 64KB."""
    h = subprocess.run(
        ["curl", "-sS", "-I", "--max-time", "15", url],
        capture_output=True, text=True
    )
    cl = 0
    for line in h.stdout.splitlines():
        if line.lower().startswith("content-length:"):
            try:
                cl = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    if cl == 0:
        return (False, 0)
    if cl <= 4_500_000:
        r = subprocess.run(
            ["curl", "-sS", "-r", "0-65535", "--max-time", "15", url],
            capture_output=True
        )
        if b"moov" in r.stdout:
            return (True, cl)
    return (False, cl)


def process_one(key: str) -> str:
    url = f"{MEDIA_BASE}/{key}"
    safe_name = key.replace("/", "_")
    in_path = tmp_root / f"in_{safe_name}"
    out_path = tmp_root / f"out_{safe_name}"
    try:
        is_fast, orig_size = already_fast(url)
        if is_fast:
            return f"SKIP_FAST {key} ({orig_size/1e6:.2f}MB)"
        if orig_size == 0:
            return f"MISSING {key}"

        # Download from R2 at datacenter 1Gbps speed
        dl = subprocess.run(
            ["curl", "-fsSL", "--max-time", "60", "-o", str(in_path), url],
            capture_output=True, text=True
        )
        if dl.returncode != 0 or not in_path.exists() or in_path.stat().st_size < 10000:
            return f"DL_ERR {key}: {dl.stderr[:100]}"

        # Compress with libx264 CRF 24 + faststart (moov at byte 0) + crisp 128k AAC
        ff = subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(in_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "24",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
            "-maxrate", "2200k", "-bufsize", "4400k",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart",
            str(out_path)
        ], capture_output=True, text=True)
        if ff.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 10000:
            return f"FF_ERR {key}: {ff.stderr[:120]}"

        new_size = out_path.stat().st_size
        # Verify moov is in first 64KB
        with open(out_path, "rb") as f:
            head = f.read(65536)
        if b"moov" not in head:
            return f"NO_MOOV {key}"

        # Upload back to R2 via Worker /__r2_put_bin
        up_url = f"{UPLOAD_BASE}?key={urllib.parse.quote(key, safe='/')}"
        up = subprocess.run([
            "curl", "-fsSL", "--max-time", "60", "-X", "POST",
            "-H", f"X-Bridge-Secret: {SECRET}",
            "-H", "Content-Type: video/mp4",
            "--data-binary", f"@{out_path}",
            up_url
        ], capture_output=True, text=True)
        if up.returncode != 0 or '"ok":true' not in up.stdout:
            return f"UP_ERR {key}: {(up.stdout + up.stderr)[:120]}"

        return f"OK {key} {orig_size/1e6:.2f}MB -> {new_size/1e6:.2f}MB"
    except Exception as e:
        return f"EXC {key}: {e}"
    finally:
        for p in (in_path, out_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


ok = skip = err = 0
with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    for res in ex.map(process_one, my_keys):
        if res.startswith("OK "):
            ok += 1
        elif res.startswith("SKIP_FAST "):
            skip += 1
        else:
            err += 1
        print(f"[{ok+skip+err}/{len(my_keys)}] {res}", flush=True)

print(f"[SHARD {SHARD_IDX}] DONE: ok={ok} skip={skip} err={err}", flush=True)
