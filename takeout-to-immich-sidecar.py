#!/usr/bin/env python3
"""Convert Google Photos Takeout supplemental metadata JSON files to Immich XMP sidecars.

Expected pair:
    IMG_1234.jpg
    IMG_1234.jpg.supplemental-metadata.json

Generated sidecar (Immich preferred naming):
    IMG_1234.jpg.xmp

Requires ExifTool to be installed and available as `exiftool`.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
    ".tif", ".tiff", ".dng", ".mp4", ".mov", ".m4v", ".avi",
    ".mkv", ".3gp", ".mts", ".m2ts",
}

JSON_SUFFIXES = (
    ".supplemental-metadata.json",
    ".json",  # compatibility with older Takeout exports
)


def locate_json(media_path: Path) -> Path | None:
    """Find the Google metadata file belonging to a media file."""
    for suffix in JSON_SUFFIXES:
        candidate = Path(str(media_path) + suffix)
        if candidate.is_file():
            return candidate
    return None


def timestamp_to_exif(value) -> str | None:
    try:
        timestamp = int(value)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y:%m:%d %H:%M:%S"
        ) + "+00:00"
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def create_xmp(json_path: Path, media_path: Path, overwrite: bool, dry_run: bool):
    xmp_path = Path(str(media_path) + ".xmp")

    if xmp_path.exists() and not overwrite:
        return "skipped", media_path, f"XMP already exists: {xmp_path}"

    try:
        with json_path.open("r", encoding="utf-8-sig") as handle:
            metadata = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return "error", media_path, f"Failed to read JSON: {exc}"

    tags = []

    description = metadata.get("description")
    if isinstance(description, str) and description.strip():
        tags.extend([
            f"-XMP-dc:Description={description.strip()}",
            f"-XMP-tiff:ImageDescription={description.strip()}",
        ])

    photo_taken = metadata.get("photoTakenTime") or {}
    formatted_date = timestamp_to_exif(photo_taken.get("timestamp"))
    if formatted_date:
        tags.extend([
            f"-XMP-exif:DateTimeOriginal={formatted_date}",
            f"-XMP-photoshop:DateCreated={formatted_date}",
            f"-XMP-xmp:CreateDate={formatted_date}",
        ])

    geo = metadata.get("geoDataExif") or metadata.get("geoData") or {}
    try:
        latitude = float(geo.get("latitude", 0) or 0)
        longitude = float(geo.get("longitude", 0) or 0)
        altitude = float(geo.get("altitude", 0) or 0)
    except (TypeError, ValueError):
        latitude = longitude = altitude = 0.0

    if latitude != 0.0 or longitude != 0.0:
        tags.extend([
            f"-XMP-exif:GPSLatitude={abs(latitude)}",
            f"-XMP-exif:GPSLatitudeRef={'N' if latitude >= 0 else 'S'}",
            f"-XMP-exif:GPSLongitude={abs(longitude)}",
            f"-XMP-exif:GPSLongitudeRef={'E' if longitude >= 0 else 'W'}",
        ])
        if altitude:
            tags.extend([
                f"-XMP-exif:GPSAltitude={abs(altitude)}",
                f"-XMP-exif:GPSAltitudeRef={0 if altitude >= 0 else 1}",
            ])

    if not tags:
        return "skipped", media_path, "No Immich-supported metadata found"

    if dry_run:
        return "dry-run", media_path, f"Would create: {xmp_path}"

    command = [
        "exiftool",
        "-charset", "filename=UTF8",
        "-overwrite_original",
        "-o", str(xmp_path),
        *tags,
        str(media_path),
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if not xmp_path.is_file():
            return "error", media_path, "ExifTool reported success, but no XMP file was created"
        return "created", media_path, result.stdout.strip() or str(xmp_path)
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        return "error", media_path, message


def collect_pairs(directory: Path):
    pairs = []
    media_without_json = 0

    for root, _, files in os.walk(directory):
        for filename in files:
            media_path = Path(root) / filename
            if media_path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            json_path = locate_json(media_path)
            if json_path:
                pairs.append((json_path, media_path))
            else:
                media_without_json += 1

    return pairs, media_without_json


def process_directory(directory: Path, workers: int, overwrite: bool, dry_run: bool):
    if not directory.is_dir():
        print(f"Error: directory does not exist: {directory}", file=sys.stderr)
        return 2

    if not dry_run and shutil.which("exiftool") is None:
        print("Error: ExifTool was not found in PATH.", file=sys.stderr)
        return 2

    pairs, media_without_json = collect_pairs(directory)
    total = len(pairs)
    print(f"Found metadata/media pairs: {total}")
    print(f"Media files without a matching metadata JSON: {media_without_json}")

    counts = {"created": 0, "skipped": 0, "error": 0, "dry-run": 0}
    errors = []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(create_xmp, json_path, media_path, overwrite, dry_run)
            for json_path, media_path in pairs
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            status, media_path, message = future.result()
            counts[status] += 1
            if status == "error":
                errors.append((media_path, message))
            print(f"Processed {index}/{total}", end="\r", flush=True)

    if total:
        print()
    print(
        f"Done. Created: {counts['created']}, "
        f"skipped: {counts['skipped']}, "
        f"dry-run: {counts['dry-run']}, errors: {counts['error']}"
    )

    if errors:
        print("\nErrors:", file=sys.stderr)
        for media_path, message in errors:
            print(f"- {media_path}: {message}", file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert Google Takeout *.supplemental-metadata.json files "
            "into Immich-compatible <media.ext>.xmp sidecars."
        )
    )
    parser.add_argument("directory", type=Path, help="Directory containing the extracted Google Takeout")
    parser.add_argument(
        "-n", "--num-workers", type=int, default=4,
        help="Number of parallel ExifTool processes (default: 4)",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing XMP sidecars",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be processed without creating files",
    )
    args = parser.parse_args()
    raise SystemExit(process_directory(
        args.directory.resolve(), args.num_workers, args.overwrite, args.dry_run
    ))


if __name__ == "__main__":
    main()
