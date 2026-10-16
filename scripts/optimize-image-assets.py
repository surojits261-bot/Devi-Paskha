#!/usr/bin/env python3
"""Create optimized WebP copies of the festival PNGs and repoint index.html."""
from pathlib import Path
from PIL import Image, ImageOps
import os
import re

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "index.html"
MAX_BACKGROUND = (1920, 1080)
MAX_LOGO = (512, 512)
BACKGROUND_QUALITY = 84
LOGO_QUALITY = 90


def fmt_bytes(value):
    return f"{value / (1024 * 1024):.2f} MiB"


def prepare_image(source):
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        original_size = image.size
        is_logo = source.name.lower() == "logo.png"
        image.thumbnail(MAX_LOGO if is_logo else MAX_BACKGROUND, Image.Resampling.LANCZOS)

        has_alpha = "A" in image.getbands() or "transparency" in image.info
        image = image.convert("RGBA" if has_alpha else "RGB")
        profile = opened.info.get("icc_profile")

        target = source.with_suffix(".webp")
        options = {
            "format": "WEBP",
            "method": 6,
            "quality": LOGO_QUALITY if is_logo else BACKGROUND_QUALITY,
        }
        if is_logo:
            # Preserve the logo's sharp edges and transparency while keeping it compact.
            options["lossless"] = True
        if profile:
            options["icc_profile"] = profile
        image.save(target, **options)
        return {
            "source": source.relative_to(ROOT).as_posix(),
            "target": target.relative_to(ROOT).as_posix(),
            "original_bytes": source.stat().st_size,
            "webp_bytes": target.stat().st_size,
            "original_dimensions": original_size,
            "new_dimensions": image.size,
        }


def main():
    if not HTML_PATH.exists():
        raise SystemExit("index.html was not found at the repository root.")

    pngs = sorted(
        path for path in ROOT.rglob("*.png")
        if ".git" not in path.parts and "node_modules" not in path.parts
    )
    if not pngs:
        raise SystemExit("No PNG assets found; refusing to make an empty optimization commit.")

    report = [prepare_image(path) for path in pngs]
    html = HTML_PATH.read_text(encoding="utf-8")
    reference_count = 0

    # Replace exact repository-relative paths. Keep source PNGs for rollback.
    for item in report:
        source_path = item["source"]
        destination_path = item["target"]
        count = html.count(source_path)
        if count:
            html = html.replace(source_path, destination_path)
            reference_count += count

    HTML_PATH.write_text(html, encoding="utf-8")

    # Ensure every PNG backdrop reference that points to a known repository asset was replaced.
    unresolved = [item["source"] for item in report if item["source"] in html]
    if unresolved:
        raise SystemExit("Unconverted PNG references remain in index.html: " + ", ".join(unresolved))

    before = sum(item["original_bytes"] for item in report)
    after = sum(item["webp_bytes"] for item in report)
    saved = before - after
    percent = (saved / before * 100) if before else 0

    print(f"Converted {len(report)} PNG files; updated {reference_count} references in index.html.")
    print(f"Original PNG total: {fmt_bytes(before)}")
    print(f"New WebP total:     {fmt_bytes(after)}")
    print(f"Saved:              {fmt_bytes(saved)} ({percent:.1f}%)")
    for item in report:
        delta = item["original_bytes"] - item["webp_bytes"]
        rate = (delta / item["original_bytes"] * 100) if item["original_bytes"] else 0
        print(
            f'{item["source"]}: {item["original_dimensions"][0]}x{item["original_dimensions"][1]} '
            f'-> {item["new_dimensions"][0]}x{item["new_dimensions"][1]} | '
            f'{fmt_bytes(item["original_bytes"])} -> {fmt_bytes(item["webp_bytes"])} ({rate:.1f}% smaller)'
        )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write("## Devi Paksha image optimization\n\n")
            summary.write(f"- PNG files converted: **{len(report)}**\n")
            summary.write(f"- Updated HTML image references: **{reference_count}**\n")
            summary.write(f"- PNG total: **{fmt_bytes(before)}**\n")
            summary.write(f"- WebP total: **{fmt_bytes(after)}**\n")
            summary.write(f"- Saved: **{fmt_bytes(saved)} ({percent:.1f}%)**\n\n")
            summary.write("| Source | Dimensions | PNG | WebP | Saved |\n")
            summary.write("|---|---:|---:|---:|---:|\n")
            for item in report:
                delta = item["original_bytes"] - item["webp_bytes"]
                rate = (delta / item["original_bytes"] * 100) if item["original_bytes"] else 0
                summary.write(
                    f'| `{item["source"]}` | {item["original_dimensions"][0]}×{item["original_dimensions"][1]} '
                    f'→ {item["new_dimensions"][0]}×{item["new_dimensions"][1]} | '
                    f'{fmt_bytes(item["original_bytes"])} | {fmt_bytes(item["webp_bytes"])} | {rate:.1f}% |\n'
                )


if __name__ == "__main__":
    main()
