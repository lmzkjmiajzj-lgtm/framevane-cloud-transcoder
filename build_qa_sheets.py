#!/usr/bin/env python3
"""Build 37 high-clarity 20-brand (40-video) QA contact sheets on 1-Gbps runner."""
import concurrent.futures
import json
import os
import subprocess
import urllib.parse
from pathlib import Path
from PIL import Image, ImageDraw

MEDIA_BASE = "https://pub-8e7bcbf159e045318d67b51afdd06e43.r2.dev"
UPLOAD_BASE = "https://framevane.onehacker.space/__r2_put_bin"
SECRET = os.environ["R2_BRIDGE_SECRET"].strip()

all_keys = [l.strip() for l in Path("keys.txt").read_text().splitlines() if l.strip()]
ugc_slugs = sorted({k.split("/")[1] for k in all_keys if k.endswith("/ugc-film.mp4")})
print(f"[QA SHEETS] Building sheets for {len(ugc_slugs)} 2-video brands ({len(ugc_slugs)*2} videos)", flush=True)

tmp_posters = Path("/tmp/fv_qa_posters")
tmp_sheets = Path("/tmp/fv_qa_sheets")
tmp_posters.mkdir(parents=True, exist_ok=True)
tmp_sheets.mkdir(parents=True, exist_ok=True)


def fetch_pair(slug: str) -> tuple[str, Path, Path]:
    p_s = tmp_posters / f"{slug}_s.jpg"
    p_u = tmp_posters / f"{slug}_u.jpg"
    subprocess.run(
        ["curl", "-fsSL", "--retry", "4", "--retry-delay", "1", "--max-time", "25", "-o", str(p_s), f"{MEDIA_BASE}/posters/concepts/{slug}/film.jpg?v=qa2"],
        capture_output=True,
    )
    subprocess.run(
        ["curl", "-fsSL", "--retry", "4", "--retry-delay", "1", "--max-time", "25", "-o", str(p_u), f"{MEDIA_BASE}/posters/concepts/{slug}/ugc-film.jpg?v=qa2"],
        capture_output=True,
    )
    return slug, p_s, p_u


START_SHEET = int(os.environ.get("START_SHEET", "18"))
print(f"[QA SHEETS] Downloading posters (max_workers=4) starting from sheet {START_SHEET}...", flush=True)
target_slugs = ugc_slugs[START_SHEET * 20:]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
    sub_pairs = list(ex.map(fetch_pair, target_slugs))
print(f"[QA SHEETS] Downloaded {len(sub_pairs)} pairs. Rendering 20-brand sheets...", flush=True)

TILE_W, TILE_H = 160, 284
BRANDS_PER_ROW = 4
ROWS = 5
BATCH = BRANDS_PER_ROW * ROWS

sheet_manifest = []
for rel_idx in range(0, len(sub_pairs), BATCH):
    b_idx = START_SHEET * BATCH + rel_idx
    chunk = sub_pairs[rel_idx:rel_idx + BATCH]
    sheet_num = b_idx // BATCH
    canvas = Image.new("RGB", (TILE_W * BRANDS_PER_ROW * 2, TILE_H * ROWS), (16, 18, 17))
    draw = ImageDraw.Draw(canvas)
    chunk_meta = []

    for i, (slug, p_s, p_u) in enumerate(chunk):
        r = i // BRANDS_PER_ROW
        c = i % BRANDS_PER_ROW
        x0 = c * (TILE_W * 2)
        y0 = r * TILE_H

        for sub_i, (p_img, tag, color) in enumerate([(p_s, "S", (147, 197, 253)), (p_u, "U", (134, 239, 172))]):
            tx = x0 + sub_i * TILE_W
            if p_img.exists() and p_img.stat().st_size > 800:
                try:
                    im = Image.open(p_img).convert("RGB").resize((TILE_W, TILE_H))
                    canvas.paste(im, (tx, y0))
                except Exception:
                    draw.rectangle([tx, y0, tx + TILE_W - 1, y0 + TILE_H - 1], fill=(80, 20, 20))
            else:
                draw.rectangle([tx, y0, tx + TILE_W - 1, y0 + TILE_H - 1], fill=(80, 20, 20))
                draw.text((tx + 12, y0 + 160), "MISSING", fill=(255, 100, 100))

            draw.rectangle([tx, y0, tx + TILE_W - 1, y0 + 20], fill=(0, 0, 0))
            lbl = f"#{b_idx + i + 1} {tag}:{slug[:19]}"
            draw.text((tx + 4, y0 + 4), lbl, fill=color)

        # Separator border between brand pairs
        draw.line([(x0 + TILE_W * 2 - 1, y0), (x0 + TILE_W * 2 - 1, y0 + TILE_H)], fill=(255, 255, 255), width=2)
        draw.line([(x0, y0 + TILE_H - 1), (x0 + TILE_W * 2, y0 + TILE_H - 1)], fill=(255, 255, 255), width=2)
        chunk_meta.append({"index": b_idx + i + 1, "slug": slug, "row": r + 1, "col": c + 1})

    sheet_name = f"sheet_{sheet_num:02d}.jpg"
    sheet_path = tmp_sheets / sheet_name
    canvas.save(sheet_path, quality=68, optimize=True)

    r2_key = f"qa/sheets/{sheet_name}"
    up_url = f"{UPLOAD_BASE}?key={urllib.parse.quote(r2_key, safe='/')}"
    up = subprocess.run(
        [
            "curl", "-fsSL", "--max-time", "45", "-X", "POST",
            "-H", f"X-Bridge-Secret: {SECRET}",
            "-H", "Content-Type: image/jpeg",
            "--data-binary", f"@{sheet_path}",
            up_url,
        ],
        capture_output=True,
        text=True,
    )
    ok = up.returncode == 0 and '"ok":true' in up.stdout
    print(f"[SHEET {sheet_num:02d}/36] {sheet_name} ({sheet_path.stat().st_size // 1024}KB) -> upload={ok}", flush=True)
    sheet_manifest.append({"sheet": sheet_name, "r2_key": r2_key, "brands": chunk_meta})

if START_SHEET == 0:
    man_path = tmp_sheets / "manifest.json"
    man_path.write_text(json.dumps(sheet_manifest, indent=2))
    subprocess.run(
        [
            "curl", "-fsSL", "--max-time", "30", "-X", "POST",
            "-H", f"X-Bridge-Secret: {SECRET}",
            "-H", "Content-Type: application/json",
            "--data-binary", f"@{man_path}",
            f"{UPLOAD_BASE}?key=qa/sheets/manifest.json",
        ],
        capture_output=True,
    )
print(f"[QA SHEETS] ALL {len(sheet_manifest)} SHEETS DONE!", flush=True)
