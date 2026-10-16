#!/usr/bin/env python3
"""Validate every audio file referenced by the Devi Paksha playlist."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
MIN_SUSPICIOUS_BYTES = 500_000
MIN_SUSPICIOUS_SECONDS = 30.0
PROBE_TIMEOUT = 45
DECODE_TIMEOUT = 120


def parse_tracks(html: str) -> list[dict]:
    match = re.search(r"const\s+tracks\s*=\s*\[([\s\S]*?)\n\s*\];", html)
    if not match:
        raise ValueError("Could not find the playlist array in index.html")
    pattern = re.compile(
        r'\{\s*id:\s*(\d+),\s*title:\s*"([^"]+)",\s*artist:\s*"([^"]+)",'
        r'\s*category:\s*"([^"]+)",\s*url:\s*"([^"]+)"\s*\}'
    )
    return [
        {"id": int(m.group(1)), "title": m.group(2), "artist": m.group(3),
         "category": m.group(4), "url": m.group(5)}
        for m in pattern.finditer(match.group(1))
    ]


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )


def check_external(track: dict) -> dict:
    request = urllib.request.Request(
        track["url"], headers={"Range": "bytes=0-15", "User-Agent": "DeviPaksha-Audio-QA/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            header = response.read(16)
            content_type = response.headers.get("Content-Type", "")
            ok = response.status in (200, 206) and bool(header)
            return {
                "id": track["id"], "title": track["title"], "url": track["url"],
                "status": "ok" if ok else "error", "http_status": response.status,
                "content_type": content_type, "received_header_bytes": len(header),
                "detail": "External source responded to a range request." if ok else "Empty or unexpected response."
            }
    except Exception as exc:
        return {
            "id": track["id"], "title": track["title"], "url": track["url"],
            "status": "warning", "detail": f"External source could not be probed: {type(exc).__name__}: {exc}"
        }


def main() -> int:
    if not INDEX.exists():
        print("ERROR: index.html is missing.", file=sys.stderr)
        return 2

    try:
        tracks = parse_tracks(INDEX.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    errors: list[dict] = []
    warnings: list[dict] = []
    passed: list[dict] = []
    local_paths: dict[str, dict] = {}
    external_results: list[dict] = []
    digest_to_paths: dict[str, list[str]] = defaultdict(list)

    for track in tracks:
        url = track["url"]
        if re.match(r"^https?://", url, flags=re.I):
            result = check_external(track)
            external_results.append(result)
            if result["status"] == "error":
                errors.append(result)
            elif result["status"] == "warning":
                warnings.append(result)
            continue

        path = (ROOT / url).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            errors.append({"id": track["id"], "title": track["title"], "url": url, "detail": "Path escapes repository root."})
            continue
        if not path.is_file():
            errors.append({"id": track["id"], "title": track["title"], "url": url, "detail": "Playlist file does not exist."})
            continue
        if path.stat().st_size == 0:
            errors.append({"id": track["id"], "title": track["title"], "url": url, "detail": "Audio file is empty."})
            continue
        local_paths.setdefault(str(path), track)

    for path_string, track in local_paths.items():
        path = Path(path_string)
        size = path.stat().st_size
        item = {"id": track["id"], "title": track["title"], "category": track["category"],
                "path": path.relative_to(ROOT).as_posix(), "size_bytes": size}

        if size < MIN_SUSPICIOUS_BYTES:
            item["warning"] = f"Very small MP3 ({size:,} bytes); duration and content deserve review."
            warnings.append(item.copy())

        try:
            probe = run([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,sample_rate,channels",
                "-show_entries", "format=duration,size,format_name",
                "-of", "json", str(path),
            ], PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            errors.append({**item, "detail": f"ffprobe timed out after {PROBE_TIMEOUT} seconds."})
            continue

        if probe.returncode != 0:
            errors.append({**item, "detail": f"ffprobe failed: {probe.stderr.strip()[:800]}"})
            continue

        try:
            metadata = json.loads(probe.stdout)
            streams = metadata.get("streams") or []
            duration = float((metadata.get("format") or {}).get("duration") or 0)
            codec = streams[0].get("codec_name") if streams else None
            item.update({
                "duration_seconds": round(duration, 2),
                "codec": codec,
                "sample_rate": streams[0].get("sample_rate") if streams else None,
                "channels": streams[0].get("channels") if streams else None,
            })
        except Exception as exc:
            errors.append({**item, "detail": f"Could not parse audio metadata: {exc}"})
            continue

        if not streams or not duration or duration <= 0:
            errors.append({**item, "detail": "No usable audio stream or a zero/invalid duration."})
            continue
        if codec not in ("mp3",):
            warnings.append({**item, "warning": f"Unexpected audio codec '{codec}' in an .mp3 file."})
        if duration < MIN_SUSPICIOUS_SECONDS:
            warnings.append({**item, "warning": f"Very short duration ({duration:.2f}s); confirm this is a full track."})

        try:
            decoded = run([
                "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                "-i", str(path), "-map", "0:a:0", "-f", "null", "-"
            ], DECODE_TIMEOUT)
        except subprocess.TimeoutExpired:
            errors.append({**item, "detail": f"Full decode timed out after {DECODE_TIMEOUT} seconds."})
            continue

        if decoded.returncode != 0:
            errors.append({**item, "detail": f"Full audio decode failed: {decoded.stderr.strip()[:1200]}"})
            continue

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest_to_paths[digest].append(str(path.relative_to(ROOT)))
        passed.append(item)

    duplicates = [paths for paths in digest_to_paths.values() if len(paths) > 1]
    for paths in duplicates:
        warnings.append({"detail": "Byte-identical audio files appear at multiple paths.", "paths": paths})

    result = {
        "playlist_track_count": len(tracks),
        "unique_local_files_checked": len(local_paths),
        "local_audio_passed": len(passed),
        "errors": errors,
        "warnings": warnings,
        "external_sources": external_results,
        "duplicate_file_groups": duplicates,
        "passed_files": passed,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as report:
            report.write("# Devi Paksha audio integrity check\n\n")
            report.write(f"- Playlist entries: **{len(tracks)}**\n")
            report.write(f"- Unique local files checked: **{len(local_paths)}**\n")
            report.write(f"- Local files fully decoded: **{len(passed)}**\n")
            report.write(f"- Errors: **{len(errors)}**\n")
            report.write(f"- Warnings: **{len(warnings)}**\n\n")
            if errors:
                report.write("## Errors\n")
                for error in errors:
                    report.write(f"- **{error.get('title', error.get('url', 'Track'))}** — {error.get('detail', 'Unknown error')}\n")
                report.write("\n")
            if warnings:
                report.write("## Warnings for manual review\n")
                for warning in warnings:
                    label = warning.get("title", warning.get("paths", ["File"])[0] if warning.get("paths") else "Track")
                    report.write(f"- **{label}** — {warning.get('warning', warning.get('detail', 'Review recommended'))}\n")
                report.write("\n")
            if external_results:
                report.write("## External sources\n")
                for external in external_results:
                    report.write(f"- **{external['title']}** — {external['status']}: {external.get('detail', '')}\n")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
