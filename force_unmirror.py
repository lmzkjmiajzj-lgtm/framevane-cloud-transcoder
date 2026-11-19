#!/usr/bin/env python3
"""Targeted 1-Gbps lossless-audio horizontal un-mirror + poster regenerator."""
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

MEDIA_BASE = "https://pub-8e7bcbf159e045318d67b51afdd06e43.r2.dev"
UPLOAD_BASE = "https://framevane.onehacker.space/__r2_put_bin"
SECRET = os.environ["R2_BRIDGE_SECRET"].strip()

raw_slugs = os.environ.get("SLUGS", "").strip()
if not raw_slugs and Path("force_flip_slugs.txt").is_file():
    raw_slugs = Path("force_flip_slugs.txt").read_text()
slugs = [s.strip() for s in raw_slugs.replace(",", "\n").splitlines() if s.strip() and not s.strip().startswith("#")]
print(f"[FORCE UNMIRROR] Processing {len(slugs)} slugs: {slugs}", flush=True)

tmp_root = Path("/tmp/fv_force_unmirror")
tmp_root.mkdir(parents=True, exist_ok=True)


def upload_r2(key: str, file_path: Path, content_type: str) -> bool:
    up_url = f"{UPLOAD_BASE}?key={urllib.parse.quote(key, safe='/')}"
    up = subprocess.run(
        [
            "curl", "-fsSL", "--max-time", "60", "-X", "POST",
            "-H", f"X-Bridge-Secret: {SECRET}",
            "-H", f"Content-Type: {content_type}",
            "--data-binary", f"@{file_path}",
            up_url,
        ],
        capture_output=True,
        text=True,
    )
    return up.returncode == 0 and '"ok":true' in up.stdout


for slug in slugs:
    work = tmp_root / slug
    work.mkdir(parents=True, exist_ok=True)
    mp4_key = f"concepts/{slug}/ugc-film.mp4"
    poster_key = f"posters/concepts/{slug}/ugc-film.jpg"
    in_mp4 = work / "in.mp4"
    out_mp4 = work / "out.mp4"
    out_jpg = work / "poster.jpg"

    dl = subprocess.run(
        ["curl", "-fsSL", "--max-time", "45", "-o", str(in_mp4), f"{MEDIA_BASE}/{mp4_key}"],
        capture_output=True,
        text=True,
    )
    if dl.returncode != 0 or not in_mp4.exists() or in_mp4.stat().st_size < 20_000:
        print(f"[ERR] Failed to download {mp4_key}", flush=True)
        continue

    ff = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(in_mp4),
            "-vf", "hflip",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "copy",
            str(out_mp4),
        ],
        capture_output=True,
        text=True,
    )
    if ff.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size < 20_000:
        print(f"[ERR] FFmpeg failed on {slug}: {ff.stderr}", flush=True)
        continue

    ok_mp4 = upload_r2(mp4_key, out_mp4, "video/mp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", "0.8", "-i", str(out_mp4),
            "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4",
            str(out_jpg),
        ],
        capture_output=True,
    )
    ok_jpg = out_jpg.exists() and upload_r2(poster_key, out_jpg, "image/jpeg")
    print(f"[FIXED] {slug} | mp4={ok_mp4} ({out_mp4.stat().st_size // 1024}KB) | poster={ok_jpg}", flush=True)
    shutil.rmtree(work, ignore_errors=True)
