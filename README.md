# Book Scanner

A PWA that uses your phone's camera to scan book barcodes (ISBN) and export
the list for building a library catalog.

## Usage

1. Open <https://rbowen.github.io/book-scanner/> in Chrome on Android
2. Grant camera permission when prompted
3. Point the camera at book barcodes — ISBNs are captured automatically
4. Hit **💾 Save** to download `isbns.txt` to your phone's Downloads folder
5. Transfer `isbns.txt` to your Mac via USB

## Features

- Native Barcode Detection API (Chrome Android) — no external libraries
- Deduplication — won't scan the same book twice
- Haptic feedback on successful scan
- Manual ISBN entry fallback
- Persistent list (survives page reload via localStorage)
- Offline-capable (service worker caches the app)
- Installable as a home screen app (PWA)

## Desktop companion

The `desktop/` directory (coming soon) contains a Python script that reads
`isbns.txt`, looks up each ISBN via Open Library, and generates a static
HTML catalog with cover images and metadata.

## Deployment

Push to GitHub and enable Pages (Settings → Pages → Deploy from branch: main).
The app will be served at `https://rbowen.github.io/book-scanner/`.
