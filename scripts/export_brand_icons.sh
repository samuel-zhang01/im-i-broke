#!/usr/bin/env bash
# Mechanical size/format exports from the generated, versioned brand artwork.
set -euo pipefail
project_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"
if command -v magick >/dev/null 2>&1; then
  exporter=(magick)
else
  exporter=(convert)
fi
destination=frontend/public/assets/brand
mkdir -p "$destination"
for size in 16 32 48 64 128; do
  "${exporter[@]}" design/brand/favicon-master.png -filter Lanczos \
    -resize "${size}x${size}" -strip "$destination/mark-${size}-v1.png"
done
for size in 180 192 512; do
  "${exporter[@]}" design/brand/ledger-folio-master.png -filter Lanczos \
    -resize "${size}x${size}" -strip "$destination/app-${size}-v1.png"
done
"${exporter[@]}" design/brand/favicon-master.png -strip \
  -define icon:auto-resize=64,48,32,16 frontend/public/favicon.ico
