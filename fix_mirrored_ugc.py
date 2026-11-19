#!/usr/bin/env python3
"""20-Shard Automated Selfie-Mirror Detector & Fixer for Framevane UGC Videos.

Detects horizontally mirrored product labels in `concepts/<slug>/ugc-film.mp4`
using dual-signal verification:
  1. Tesseract OCR (`--psm 6` + `--psm 11` TSV confidence >= 52) on 5 foreground
     frames (`orig` vs `hflip`) matched against Apple Vision ground-truth packshot
     tokens (`ugc_label_manifest.json`).
  2. OpenCV SIFT + RANSAC orientation-preserving homography (`det(H[:2,:2]) > 0`)
     against unmirrored Studio Commercial frames (`concepts/<slug>/film.mp4`).

When a video is proven mirrored with zero forward brand hits, automatically:
  - Runs `ffmpeg -vf hflip -c:v libx264 -preset fast -crf 22 -movflags +faststart -c:a copy`
    (preserving the canonical -16 LUFS EBU R128 audio track losslessly)
  - Uploads fixed `concepts/<slug>/ugc-film.mp4` to Direct R2
  - Re-extracts & uploads unmirrored `posters/concepts/<slug>/ugc-film.jpg` to Direct R2
"""
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import cv2
import numpy as np

MEDIA_BASE = "https://pub-8e7bcbf159e045318d67b51afdd06e43.r2.dev"
UPLOAD_BASE = "https://framevane.onehacker.space/__r2_put_bin"
SECRET = os.environ["R2_BRIDGE_SECRET"].strip()
SHARD_IDX = int(os.environ.get("SHARD_IDX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "20"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))

ALREADY_UNMIRRORED = {"truff-eec6d8"}
WEAK_TOKENS = {
    "USA", "NYC", "LAB", "LABS", "INC", "LLC", "LTD", "THE", "AND", "FOR",
    "WITH", "FROM", "THIS", "THAT", "YOUR", "PURE", "SKIN", "CARE", "BODY",
    "FACE", "HAIR", "HOME", "SHOP", "CLUB", "GOOD", "BEST", "LIFE", "LOVE",
}
SYM_CHARS = set("AHIMOTUVWXY08")

manifest = json.loads(Path("ugc_label_manifest.json").read_text())
all_ugc_keys = [
    l.strip() for l in Path("keys.txt").read_text().splitlines()
    if l.strip().endswith("/ugc-film.mp4")
]
my_keys = [k for i, k in enumerate(all_ugc_keys) if i % SHARD_TOTAL == SHARD_IDX]
print(f"[UNMIRROR SHARD {SHARD_IDX}/{SHARD_TOTAL}] Assigned {len(my_keys)} / {len(all_ugc_keys)} UGC videos", flush=True)

tmp_root = Path("/tmp/fv_unmirror")
tmp_root.mkdir(parents=True, exist_ok=True)


def clean_tokens(entry: dict) -> tuple[set[str], set[str]]:
    brand_toks = set()
    all_toks = set()
    for t in entry.get("brand_tokens", []):
        t = re.sub(r"[^A-Z0-9]", "", t.upper())
        if len(t) >= 3 and t not in WEAK_TOKENS and not (all(c in SYM_CHARS for c in t) and t == t[::-1]):
            brand_toks.add(t)
            all_toks.add(t)
    for t in entry.get("tokens", []):
        t = re.sub(r"[^A-Z0-9]", "", t.upper())
        if len(t) >= 4 and t not in WEAK_TOKENS and not (all(c in SYM_CHARS for c in t) and t == t[::-1]):
            all_toks.add(t)
    return brand_toks, all_toks


os.environ["OMP_THREAD_LIMIT"] = "1"
cv2.setNumThreads(1)

def tesseract_words(gray_img: np.ndarray, tmp_jpg: Path) -> set[str]:
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    enh = clahe.apply(gray_img)
    cv2.imwrite(str(tmp_jpg), enh, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    words = set()
    p = subprocess.run(
        ["tesseract", str(tmp_jpg), "stdout", "-l", "eng", "--oem", "1", "--psm", "6", "tsv"],
        capture_output=True,
        timeout=15,
    )
    if p.returncode == 0:
        for line in p.stdout.decode("utf-8", errors="ignore").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12:
                continue
            try:
                conf = float(parts[10])
            except ValueError:
                continue
            if conf < 52.0:
                continue
            w = re.sub(r"[^A-Z0-9]", "", parts[11].upper())
            if len(w) >= 3:
                words.add(w)
    return words


def score_ocr_words(found_words: set[str], brand_toks: set[str], all_toks: set[str]) -> tuple[int, set[str], set[str]]:
    score = 0
    matched = set()
    brand_hits = set()
    for tok in all_toks:
        hit = False
        if tok in found_words:
            hit = True
        elif len(tok) >= 5:
            for fw in found_words:
                if len(fw) >= 5 and (tok in fw or fw in tok):
                    hit = True
                    break
        if hit:
            matched.add(tok)
            pts = len(tok) * 2
            if tok in brand_toks:
                pts += 6
                brand_hits.add(tok)
            score += pts
    return score, matched, brand_hits


def sift_oriented_inliers(ref_grays: list[np.ndarray], target_gray: np.ndarray) -> int:
    sift = cv2.SIFT_create(nfeatures=1400)
    kp2, des2 = sift.detectAndCompute(target_gray, None)
    if des2 is None or len(kp2) < 12:
        return 0
    bf = cv2.BFMatcher(cv2.NORM_L2)
    best_inliers = 0
    for rg in ref_grays:
        kp1, des1 = sift.detectAndCompute(rg, None)
        if des1 is None or len(kp1) < 12:
            continue
        matches = bf.knnMatch(des1, des2, k=2)
        good = [m for pair in matches if len(pair) == 2 for m, n in [pair] if m.distance < 0.72 * n.distance]
        if len(good) < 12:
            continue
        pts1 = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        pts2 = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(pts1, pts2, cv2.RANSAC, 4.5)
        if H is None or mask is None:
            continue
        det = float(np.linalg.det(H[:2, :2]))
        # Orientation-preserving planar transform must have det > 0 and reasonable scale
        if 0.04 < det < 25.0:
            inl = int(mask.ravel().sum())
            if inl > best_inliers:
                best_inliers = inl
    return best_inliers


def extract_frames(video_path_or_url: str, timestamps: list[str], out_dir: Path, prefix: str) -> list[np.ndarray]:
    frames = []
    for idx, ts in enumerate(timestamps):
        out_p = out_dir / f"{prefix}_{idx}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-ss", ts, "-i", video_path_or_url, "-frames:v", "1", "-q:v", "2", str(out_p),
            ],
            capture_output=True,
            timeout=25,
        )
        if out_p.exists() and out_p.stat().st_size > 1500:
            img = cv2.imread(str(out_p), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                frames.append(img)
    return frames


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


def process_ugc(mp4_key: str) -> str:
    slug = mp4_key.split("/")[1]
    if slug in ALREADY_UNMIRRORED:
        return f"SKIP_KNOWN {slug}"

    work = tmp_root / slug
    work.mkdir(parents=True, exist_ok=True)
    try:
        ugc_path = work / "ugc.mp4"
        dl = subprocess.run(
            ["curl", "-fsSL", "--max-time", "45", "-o", str(ugc_path), f"{MEDIA_BASE}/{mp4_key}"],
            capture_output=True,
            text=True,
        )
        if dl.returncode != 0 or not ugc_path.exists() or ugc_path.stat().st_size < 20_000:
            return f"DL_ERR {slug}"

        ugc_frames = extract_frames(str(ugc_path), ["1.2", "4.5", "7.8"], work, "u")
        if not ugc_frames:
            return f"NO_FRAMES {slug}"

        entry = manifest.get(slug, {})
        brand_toks, all_toks = clean_tokens(entry)

        orig_total_score = 0
        flip_total_score = 0
        orig_matched_all = set()
        flip_matched_all = set()
        orig_brand_all = set()
        flip_brand_all = set()

        crops_orig = []
        crops_flip = []
        for g in ugc_frames:
            h, w = g.shape[:2]
            crop = g[int(h * 0.16):int(h * 0.96), :]
            crop_f = cv2.flip(crop, 1)
            crops_orig.append(crop)
            crops_flip.append(crop_f)

            if all_toks:
                w_o = tesseract_words(crop, work / "ocr_o.jpg")
                w_f = tesseract_words(crop_f, work / "ocr_f.jpg")
                so, mo, bo = score_ocr_words(w_o, brand_toks, all_toks)
                sf, mf, bf = score_ocr_words(w_f, brand_toks, all_toks)
                orig_total_score += so
                flip_total_score += sf
                orig_matched_all |= mo
                flip_matched_all |= mf
                orig_brand_all |= bo
                flip_brand_all |= bf

        # SIFT check when OCR is inconclusive on original
        orig_sift = 0
        flip_sift = 0
        if orig_total_score == 0:
            studio_url = f"{MEDIA_BASE}/concepts/{slug}/film.mp4"
            studio_frames = extract_frames(studio_url, ["1.2", "5.0"], work, "s")
            if studio_frames:
                for co, cf in zip(crops_orig[:3], crops_flip[:3]):
                    orig_sift = max(orig_sift, sift_oriented_inliers(studio_frames, co))
                    flip_sift = max(flip_sift, sift_oriented_inliers(studio_frames, cf))

        do_flip = False
        reason = ""
        if flip_total_score >= 12 and orig_total_score == 0:
            do_flip = True
            reason = f"ocr_clean(flip={sorted(flip_matched_all)},score={flip_total_score})"
        elif flip_total_score >= 22 and flip_total_score >= orig_total_score * 3.5 and len(orig_brand_all) == 0:
            do_flip = True
            reason = f"ocr_dom(flip={sorted(flip_matched_all)}/{flip_total_score},orig={sorted(orig_matched_all)}/{orig_total_score})"
        elif orig_total_score == 0 and flip_sift >= 22 and orig_sift <= 6:
            do_flip = True
            reason = f"sift_homography(flip_inl={flip_sift},orig_inl={orig_sift})"

        if not do_flip:
            return f"OK_ORIG {slug} (o_ocr={orig_total_score} f_ocr={flip_total_score} o_sift={orig_sift} f_sift={flip_sift})"

        # Perform lossless-audio horizontal flip + faststart
        fixed_mp4 = work / "ugc_fixed.mp4"
        ff = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", str(ugc_path),
                "-vf", "hflip",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-c:a", "copy",
                str(fixed_mp4),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if ff.returncode != 0 or not fixed_mp4.exists() or fixed_mp4.stat().st_size < 30_000:
            return f"FF_ERR {slug}: {ff.stderr[:120]}"

        if not upload_r2(mp4_key, fixed_mp4, "video/mp4"):
            return f"UP_MP4_ERR {slug}"

        # Re-extract & upload un-mirrored poster
        poster_key = f"posters/concepts/{slug}/ugc-film.jpg"
        poster_jpg = work / "poster.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-ss", "0.8", "-i", str(fixed_mp4),
                "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4",
                str(poster_jpg),
            ],
            capture_output=True,
            timeout=25,
        )
        if poster_jpg.exists() and poster_jpg.stat().st_size > 1500:
            upload_r2(poster_key, poster_jpg, "image/jpeg")

        return f"[MIRROR_FIXED] {slug} | {reason} | {fixed_mp4.stat().st_size // 1024}KB"
    except Exception as e:
        return f"EXC {slug}: {e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


fixed = ok_orig = err = 0
fixed_list = []
with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    for idx, res in enumerate(ex.map(process_ugc, my_keys), 1):
        if res.startswith("[MIRROR_FIXED]"):
            fixed += 1
            fixed_list.append(res)
            print(f"[{idx}/{len(my_keys)}] {res}", flush=True)
        elif res.startswith(("OK_ORIG", "SKIP_KNOWN")):
            ok_orig += 1
            if idx % 10 == 0:
                print(f"[{idx}/{len(my_keys)}] {res}", flush=True)
        else:
            err += 1
            print(f"[{idx}/{len(my_keys)}] {res}", flush=True)

print(
    f"[UNMIRROR SHARD {SHARD_IDX}] DONE: fixed={fixed} ok_orig={ok_orig} err={err}",
    flush=True,
)
for item in fixed_list:
    print(item, flush=True)
