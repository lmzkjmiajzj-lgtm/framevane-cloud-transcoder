#!/usr/bin/env python3
import json
import math
import os
import re
import subprocess
import concurrent.futures
import urllib.parse
from pathlib import Path

MEDIA_BASE = "https://pub-8e7bcbf159e045318d67b51afdd06e43.r2.dev"
UPLOAD_BASE = "https://framevane.onehacker.space/__r2_put_bin"
SECRET = os.environ["R2_BRIDGE_SECRET"].strip()
SHARD_IDX = int(os.environ.get("SHARD_IDX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))

all_keys = [l.strip() for l in Path("keys.txt").read_text().splitlines() if l.strip()]
my_keys = [k for i, k in enumerate(all_keys) if i % SHARD_TOTAL == SHARD_IDX]
print(f"[SHARD {SHARD_IDX}/{SHARD_TOTAL}] Assigned {len(my_keys)} / {len(all_keys)} videos (workers={MAX_WORKERS})", flush=True)

tmp_root = Path("/tmp/fv_master")
tmp_root.mkdir(parents=True, exist_ok=True)


def measure_loudnorm_json(in_path: Path) -> dict | None:
    r = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-i", str(in_path),
            "-map", "0:a:0", "-af", "loudnorm=I=-16:TP=-2:LRA=11:print_format=json",
            "-vn", "-f", "null", "-"
        ],
        capture_output=True, text=True
    )
    blocks = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r.stderr, re.S)
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
        i_val = float(raw.get("input_i", "-99"))
        tp_val = float(raw.get("input_tp", "-99"))
        if not math.isfinite(i_val) or not math.isfinite(tp_val) or i_val <= -60.0 or tp_val <= -60.0:
            return None
        raw["_i"] = i_val
        raw["_tp"] = tp_val
        return raw
    except Exception:
        return None


def process_one(key: str) -> str:
    url = f"{MEDIA_BASE}/{key}"
    safe_name = key.replace("/", "_")
    in_path = tmp_root / f"in_{safe_name}"
    out_path = tmp_root / f"out_{safe_name}"
    try:
        dl = subprocess.run(
            ["curl", "-fsSL", "--max-time", "60", "-o", str(in_path), url],
            capture_output=True, text=True
        )
        if dl.returncode != 0 or not in_path.exists() or in_path.stat().st_size < 10000:
            return f"DL_ERR {key}: {dl.stderr[:80]}"

        orig_size = in_path.stat().st_size
        with open(in_path, "rb") as f:
            has_moov = b"moov" in f.read(65536)
        needs_video_encode = (orig_size > 4_800_000) or (not has_moov)

        audio_raw = measure_loudnorm_json(in_path)
        needs_audio_norm = (audio_raw is not None) and (audio_raw["_i"] < -21.5 or audio_raw["_i"] > -13.0 or audio_raw["_tp"] > -1.0)

        if not needs_video_encode and not needs_audio_norm:
            lufs_s = f"{audio_raw['_i']:.1f}LUFS" if audio_raw else "no_sig"
            return f"SKIP_OK {key} ({orig_size/1e6:.2f}MB, {lufs_s})"

        v_args = (
            ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-profile:v", "high", "-level", "4.0", "-maxrate", "2500k", "-bufsize", "5000k"]
            if needs_video_encode
            else ["-c:v", "copy"]
        )

        if audio_raw is not None:
            norm_filter = (
                f"loudnorm=I=-16:TP=-2:LRA=11:linear=true:"
                f"measured_I={audio_raw['input_i']}:measured_TP={audio_raw['input_tp']}:"
                f"measured_LRA={audio_raw['input_lra']}:measured_thresh={audio_raw['input_thresh']}:"
                f"offset={audio_raw['target_offset']}"
            )
            a_args = ["-af", norm_filter, "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
        else:
            a_args = ["-c:a", "copy"]

        ff = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
             "-i", str(in_path), *v_args, *a_args, "-movflags", "+faststart", str(out_path)],
            capture_output=True, text=True
        )
        if ff.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 10000:
            return f"FF_ERR {key}: {ff.stderr[:120]}"

        new_size = out_path.stat().st_size
        with open(out_path, "rb") as f:
            if b"moov" not in f.read(65536):
                return f"NO_MOOV {key}"

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

        mode = ("VID+AUD" if (needs_video_encode and needs_audio_norm) else ("VID" if needs_video_encode else "AUD_ONLY"))
        src_i = f"{audio_raw['_i']:.1f}" if audio_raw else "none"
        return f"OK_{mode} {key} {orig_size/1e6:.2f}MB->{new_size/1e6:.2f}MB (src={src_i}LUFS->-16LUFS)"
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
        if res.startswith("OK_"):
            ok += 1
        elif res.startswith("SKIP_"):
            skip += 1
        else:
            err += 1
        print(f"[{ok+skip+err}/{len(my_keys)}] {res}", flush=True)

print(f"[SHARD {SHARD_IDX}] DONE: ok={ok} skip={skip} err={err}", flush=True)
