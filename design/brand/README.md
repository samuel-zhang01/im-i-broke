# Am I Broke? — ledger-folio identity

The open-ledger outline becomes a question mark on its right-hand page. It connects
the original Ledger identity with the current Am I Broke? name. The existing
charcoal, terracotta and warm-ivory palette and live-text wordmark are retained.

## Assets

- [App-icon master](ledger-folio-master.png), 1254 × 1254 PNG.
- [Favicon master](favicon-master.png), 1254 × 1254 PNG.
- UI and tab icons: `frontend/public/assets/brand/mark-{16,32,48,64,128}-v1.png`.
- Phone icons: `frontend/public/assets/brand/app-{180,192,512}-v1.png`.
- Multi-resolution fallback: `frontend/public/favicon.ico` (16, 32, 48 and 64 px).
- Home-screen metadata: `frontend/public/assets/brand/site-v1.webmanifest`.

Regenerate exports from the repository root with `bash scripts/export_brand_icons.sh`.
It requires ImageMagick (`magick` or `convert`). Versioned asset names are deliberate:
production caches `/assets/` immutably. Use a new version when replacing artwork.

The UI mark is decorative next to visible brand text. Fixed dimensions prevent
layout shifts. All assets are served locally; the manifest adds shortcut metadata
only, without a service worker or offline financial-data cache. Existing auth
rules already allow `/assets/` and `/favicon.ico`, so the mark also works before
sign-in when the backend directly serves the built frontend.
