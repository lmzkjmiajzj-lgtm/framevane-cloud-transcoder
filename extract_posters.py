#!/usr/bin/env python3
import os
import subprocess
import concurrent.futures
import urllib.parse
from pathlib import Path

MEDIA_BASE = "https://pub-8e7bcbf159e045318d67b51afdd06e43.r2.dev"
UPLOAD_BASE = "https://framevane.onehacker.space/__r2_put_bin"
SECRET = os.environ["R2_BRIDGE_SECRET"].strip()
SHARD_IDX = int(os.environ.get("SHARD_IDX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))

all_keys = [l.strip() for l in Path("keys.txt").read_text().splitlines() if l.strip()]
my_keys = [k for i, k in enumerate(all_keys) if i % SHARD_TOTAL == SHARD_IDX]
print(f"[POSTER SHARD {SHARD_IDX}/{SHARD_TOTAL}] Assigned {len(my_keys)} / {len(all_keys)} videos", flush=True)

tmp_root = Path("/tmp/fv_posters")
tmp_root.mkdir(parents=True, exist_ok=True)


def make_poster(mp4_key: str) -> str:
    # concepts/<slug>/film.mp4 -> posters/concepts/<slug>/film.jpg
    poster_key = "posters/" + mp4_key[:-4] + ".jpg"
    poster_url = f"{MEDIA_BASE}/{poster_key}"
    # Skip if already uploaded
    h = subprocess.run(["curl", "-sS", "-I", "--max-time", "10", poster_url], capture_output=True, text=True)
    if h.stdout.startswith("HTTP/1.1 200") or h.stdout.startswith("HTTP/2 200"):
        return f"SKIP {poster_key}"

    mp4_url = f"{MEDIA_BASE}/{mp4_key}"
    out_jpg = tmp_root / (mp4_key.replace("/", "_") + ".jpg")
    try:
        # Grab crisp frame at 0.8s (past any 0.0s fade, clear product/creator hero frame)
        ff = subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", "0.8", "-i", mp4_url,
            "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4",
            str(out_jpg)
        ], capture_output=True, text=True, timeout=30)
        if ff.returncode != 0 or not out_jpg.exists() or out_jpg.stat().st_size < 1500:
            # Fallback to 0.1s
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-ss", "0.1", "-i", mp4_url,
                "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4",
                str(out_jpg)
            ], capture_output=True, text=True, timeout=30)
        if not out_jpg.exists() or out_jpg.stat().st_size < 1500:
            return f"FF_ERR {mp4_key}"

        up_url = f"{UPLOAD_BASE}?key={urllib.parse.quote(poster_key, safe='/')}"
        up = subprocess.run([
            "curl", "-fsSL", "--max-time", "30", "-X", "POST",
            "-H", f"X-Bridge-Secret: {SECRET}",
            "-H", "Content-Type: image/jpeg",
            "--data-binary", f"@{out_jpg}",
            up_url
        ], capture_output=True, text=True)
        if up.returncode != 0 or '"ok":true' not in up.stdout:
            return f"UP_ERR {poster_key}: {(up.stdout + up.stderr)[:80]}"
        return f"OK {poster_key} ({out_jpg.stat().st_size // 1024}KB)"
    except Exception as e:
        return f"EXC {mp4_key}: {e}"
    finally:
        out_jpg.unlink(missing_ok=True)


ok = skip = err = 0
with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    for res in ex.map(make_poster, my_keys):
        if res.startswith("OK "):
            ok += 1
        elif res.startswith("SKIP "):
            skip += 1
        else:
            err += 1
        if (ok + skip + err) % 15 == 0 or res.startswith(("FF_ERR", "UP_ERR", "EXC")):
            print(f"[{ok+skip+err}/{len(my_keys)}] {res}", flush=True)

print(f"[POSTER SHARD {SHARD_IDX}] DONE: ok={ok} skip={skip} err={err}", flush=True)
