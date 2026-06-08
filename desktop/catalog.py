#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
#
# Run with: uv run catalog.py [add|publish]
"""
Book Catalog Generator

Reads ISBNs from a file, looks up book metadata, generates an HTML catalog.
Supports manual entry for books without barcodes.

Usage:
    python catalog.py                  # Generate catalog from isbns.txt
    python catalog.py add              # Manually add a book
    python catalog.py publish          # SCP the catalog to remote server

Configuration:
    Edit config.json to set paths and publish target.
"""

import html as html_mod
import base64
import json
import os
import re
import sys
import subprocess
import shutil
import urllib.request
import urllib.parse
import urllib.error
import time
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"
LIBRARY_FILE = SCRIPT_DIR / "library.json"
OUTPUT_DIR = SCRIPT_DIR / "output"
COVERS_DIR = OUTPUT_DIR / "covers"

DEFAULT_CONFIG = {
    "isbns_file": os.path.expanduser(
        "~/Library/CloudStorage/Dropbox/Apps/book-scanner/isbns.txt"
    ),
    "publish_target": "user@example.com:/var/www/html/books/",
    "site_title": "My Library",
}


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    else:
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Created default config at {CONFIG_FILE}")
        print("Edit it to set your publish_target and isbns_file path.")
        return DEFAULT_CONFIG


def load_library():
    if LIBRARY_FILE.exists():
        with open(LIBRARY_FILE) as f:
            return json.load(f)
    return []


def save_library(library):
    with open(LIBRARY_FILE, "w") as f:
        json.dump(library, f, indent=2)


# ─── ISBN Lookup ─────────────────────────────────────────────────────────────


def lookup_isbn_openlibrary(isbn):
    """Look up a book by ISBN using Open Library API."""
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BookCatalog/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        key = f"ISBN:{isbn}"
        if key in data:
            entry = data[key]
            book = {
                "isbn": isbn,
                "title": entry.get("title", "Unknown"),
                "authors": [a["name"] for a in entry.get("authors", [])],
                "publishers": [p["name"] for p in entry.get("publishers", [])],
                "publish_date": entry.get("publish_date", ""),
                "pages": entry.get("number_of_pages", ""),
                "cover_url": entry.get("cover", {}).get("large", "")
                or entry.get("cover", {}).get("medium", ""),
                "subjects": [s["name"] for s in entry.get("subjects", [])][:5],
                "source": "openlibrary",
            }
            return book
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        pass
    return None


def lookup_isbn_google(isbn):
    """Fallback: Google Books API (no key needed for basic queries)."""
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BookCatalog/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        if data.get("totalItems", 0) > 0:
            item = data["items"][0]["volumeInfo"]
            title = item.get("title", "Unknown")
            subtitle = item.get("subtitle", "")
            if subtitle:
                title = title + ": " + subtitle
            book = {
                "isbn": isbn,
                "title": title,
                "authors": item.get("authors", []),
                "publishers": [item.get("publisher", "")] if item.get("publisher") else [],
                "publish_date": item.get("publishedDate", ""),
                "pages": item.get("pageCount", ""),
                "cover_url": item.get("imageLinks", {}).get("thumbnail", "").replace(
                    "http://", "https://"
                ),
                "subjects": item.get("categories", [])[:5],
                "source": "google",
            }
            return book
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        pass
    return None


def lookup_isbn_isbnsearch(isbn):
    """Fallback: scrape isbnsearch.org for books not found elsewhere."""
    url = f"https://www.isbnsearch.org/isbn/{isbn}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BookCatalog/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode("utf-8", errors="replace")

        # Extract title (first heading-like text)
        title = ""
        # Try <title> tag first
        title_match = re.search(r'<title[^>]*>(.+?)</title>', html, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = title_match.group(1).strip()
            # Remove site suffix
            title = re.sub(r'\s*[-|]\s*[Ii][Ss][Bb][Nn].*$', '', title).strip()
        # Also try <h1> which often has the book title
        if not title or len(title) < 3:
            h1_match = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.IGNORECASE | re.DOTALL)
            if h1_match:
                title = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()

        title = html_mod.unescape(title)

        # Extract authors
        authors = []
        author_match = re.search(r'(?:Authors?)\s*:?\s*</[^>]+>\s*([^<]+)', html, re.IGNORECASE)
        if not author_match:
            author_match = re.search(r'(?:Authors?|By)\s*[:\s]+([^\n<]+)', html)
        if author_match:
            raw = html_mod.unescape(author_match.group(1).strip())
            authors = [a.strip() for a in re.split(r'[;,]', raw) if a.strip()]

        publisher_match = re.search(r'Publisher\s*:?\s*</[^>]+>\s*([^<]+)', html, re.IGNORECASE)
        if not publisher_match:
            publisher_match = re.search(r'Publisher\s*[:\s]+([^\n<]+)', html)
        publisher = html_mod.unescape(publisher_match.group(1).strip()) if publisher_match else ""

        date_match = re.search(r'Published\s*:?\s*</[^>]+>\s*([^<]+)', html, re.IGNORECASE)
        if not date_match:
            date_match = re.search(r'Published\s*[:\s]+([^\n<]+)', html)
        pub_date = date_match.group(1).strip() if date_match else ""

        if title:
            return {
                "isbn": isbn, "title": title, "authors": authors,
                "publishers": [publisher] if publisher else [],
                "publish_date": pub_date, "pages": "", "cover_url": "",
                "subjects": [], "source": "isbnsearch",
            }
    except (urllib.error.URLError, OSError):
        pass
    return None


def lookup_isbn(isbn):
    """Try Open Library first, then ISBNSearch, then Google Books.
    
    ISBNSearch is more reliable for exact titles than Google Books,
    which often returns truncated or incorrect titles for multi-volume sets.
    Google Books is last resort since it has the most data but worst accuracy.
    """
    book = lookup_isbn_openlibrary(isbn)
    if book:
        return book
    time.sleep(0.5)
    book = lookup_isbn_isbnsearch(isbn)
    if book:
        return book
    time.sleep(0.5)
    book = lookup_isbn_google(isbn)
    if book:
        return book
    return None


# ─── Edit ────────────────────────────────────────────────────────────────────


def edit_library():
    """Search and edit books in the library."""
    library = load_library()
    if not library:
        print("Library is empty.")
        return

    print(f"\n── Edit Library ({len(library)} books) ──")
    query = input("Search (title/author/ISBN): ").strip().lower()
    if not query:
        return

    # Find matches
    matches = []
    for i, book in enumerate(library):
        searchable = " ".join([
            book.get("title", ""),
            " ".join(book.get("authors", [])),
            book.get("isbn", ""),
        ]).lower()
        if query in searchable:
            matches.append((i, book))

    if not matches:
        print(f"  No books matching '{query}'.")
        return

    # Show matches
    print(f"\n  Found {len(matches)} match(es):\n")
    for idx, (i, book) in enumerate(matches):
        authors = ", ".join(book.get("authors", ["Unknown"]))
        print(f"  [{idx + 1}] {book.get('title', '?')} — {authors}")

    choice = input(f"\n  Edit which? [1-{len(matches)}, or Enter to cancel]: ").strip()
    if not choice or not choice.isdigit():
        return
    choice_idx = int(choice) - 1
    if choice_idx < 0 or choice_idx >= len(matches):
        return

    lib_idx, book = matches[choice_idx]
    print(f"\n  Editing: {book.get('title', '?')}")
    print(f"  (Press Enter to keep current value)\n")

    new_title = input(f"  Title [{book.get('title', '')}]: ").strip()
    new_authors = input(f"  Authors [{', '.join(book.get('authors', []))}]: ").strip()
    new_pub = input(f"  Publisher [{', '.join(book.get('publishers', []))}]: ").strip()
    new_year = input(f"  Year [{book.get('publish_date', '')}]: ").strip()
    new_cover = input(f"  Cover image path [{book.get('local_cover', 'none')}]: ").strip()

    if new_title:
        library[lib_idx]["title"] = new_title
    if new_authors:
        library[lib_idx]["authors"] = [a.strip() for a in new_authors.split(",")]
    if new_pub:
        library[lib_idx]["publishers"] = [new_pub]
    if new_year:
        library[lib_idx]["publish_date"] = new_year
    if new_cover and os.path.exists(new_cover):
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(new_cover)[1] or ".jpg"
        title_slug = library[lib_idx]["title"].replace(" ", "_")[:30]
        cover_filename = f"{title_slug}{ext}"
        shutil.copy2(new_cover, COVERS_DIR / cover_filename)
        library[lib_idx]["local_cover"] = cover_filename
        print(f"  📷 Cover updated: {cover_filename}")

    save_library(library)
    print(f"\n  ✅ Updated: {library[lib_idx]['title']}")


# ─── Assign Covers ─────────────────────────────────────────────────────────────


def assign_covers(config):
    """Assign loose cover photos from the phone to books in the library."""
    import base64

    covers_file = Path(config["isbns_file"]).expanduser().parent / "loose_covers.json"
    if not covers_file.exists():
        print("No loose covers to assign. Take cover photos on your phone first.")
        return

    with open(covers_file) as f:
        try:
            loose_covers = json.load(f)
        except json.JSONDecodeError:
            print("Error reading loose_covers.json")
            return

    if not loose_covers:
        print("No unassigned cover photos.")
        return

    library = load_library()
    if not library:
        print("Library is empty. Run catalog.py first to populate it.")
        return

    print(f"\n── Assign Covers ({len(loose_covers)} photo(s) available) ──\n")

    assigned = 0
    remaining = []
    for i, cover_entry in enumerate(loose_covers):
        print(f"  Cover photo {i + 1}/{len(loose_covers)} (taken {time.strftime('%Y-%m-%d %H:%M', time.localtime(cover_entry['timestamp'] / 1000))})")

        # Save to temp file and open in Preview
        cover_data = cover_entry.get("cover", "")
        if cover_data and cover_data.startswith("data:image"):
            import tempfile
            header, b64data = cover_data.split(",", 1)
            if b64data:
                ext = ".jpg" if "jpeg" in header else ".png"
                tmp_path = Path(tempfile.gettempdir()) / f"cover_preview_{i}{ext}"
                with open(tmp_path, "wb") as tf:
                    tf.write(base64.b64decode(b64data))
                subprocess.run(["open", str(tmp_path)])
            else:
                print("    ⚠️  Empty cover image — skipping.")
                remaining.append(cover_entry)
                continue
        else:
            print("    ⚠️  Invalid/empty cover data — skipping.")
            remaining.append(cover_entry)
            continue

        query = input("  Assign to which book? (search, or Enter to skip): ").strip().lower()
        if not query:
            remaining.append(cover_entry)
            continue

        matches = []
        for j, book in enumerate(library):
            searchable = " ".join([book.get("title", ""), " ".join(book.get("authors", [])), book.get("isbn", "")]).lower()
            if query in searchable:
                matches.append((j, book))

        if not matches:
            print(f"    No match for '{query}'. Skipping.")
            remaining.append(cover_entry)
            continue

        for idx, (j, book) in enumerate(matches):
            print(f"    [{idx + 1}] {book.get('title', '?')} — {', '.join(book.get('authors', []))}")

        choice = input(f"    Which? [1-{len(matches)}]: ").strip()
        if not choice or not choice.isdigit() or int(choice) < 1 or int(choice) > len(matches):
            remaining.append(cover_entry)
            continue

        lib_idx = matches[int(choice) - 1][0]
        cover_data = cover_entry["cover"]
        if cover_data and cover_data.startswith("data:image"):
            COVERS_DIR.mkdir(parents=True, exist_ok=True)
            header, b64data = cover_data.split(",", 1)
            ext = ".jpg" if "jpeg" in header else ".png"
            title_slug = library[lib_idx]["title"].replace(" ", "_")[:30]
            filename = f"{title_slug}{ext}"
            with open(COVERS_DIR / filename, "wb") as cf:
                cf.write(base64.b64decode(b64data))
            library[lib_idx]["local_cover"] = filename
            print(f"    ✅ Cover assigned: {filename}")
            assigned += 1
        else:
            print("    ⚠️ Invalid cover data. Skipping.")
            remaining.append(cover_entry)

    save_library(library)

    # Update the loose covers file (remove assigned ones)
    with open(covers_file, "w") as f:
        json.dump(remaining, f, indent=2)

    print(f"\n  {assigned} cover(s) assigned. {len(remaining)} remaining.")


def lookup_by_title_author(title, author):
    """Search by title and author when no ISBN is available."""
    query = urllib.parse.quote(f'intitle:{title} inauthor:{author}')
    url = f"https://www.googleapis.com/books/v1/volumes?q={query}&maxResults=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BookCatalog/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        if data.get("totalItems", 0) > 0:
            item = data["items"][0]["volumeInfo"]
            isbn = ""
            for ident in item.get("industryIdentifiers", []):
                if ident["type"] == "ISBN_13":
                    isbn = ident["identifier"]
                    break
                elif ident["type"] == "ISBN_10":
                    isbn = ident["identifier"]
            book = {
                "isbn": isbn,
                "title": item.get("title", title),
                "authors": item.get("authors", [author]),
                "publishers": [item.get("publisher", "")] if item.get("publisher") else [],
                "publish_date": item.get("publishedDate", ""),
                "pages": item.get("pageCount", ""),
                "cover_url": item.get("imageLinks", {}).get("thumbnail", "").replace(
                    "http://", "https://"
                ),
                "subjects": item.get("categories", [])[:5],
                "source": "google",
            }
            return book
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        pass
    return None


def download_cover(book, covers_dir):
    """Download cover image and return local filename."""
    if not book.get("cover_url"):
        return ""
    isbn = book.get("isbn", "manual")
    filename = f"{isbn or book['title'].replace(' ', '_')[:30]}.jpg"
    filepath = covers_dir / filename
    if filepath.exists():
        return filename
    try:
        req = urllib.request.Request(
            book["cover_url"], headers={"User-Agent": "BookCatalog/1.0"}
        )
        resp = urllib.request.urlopen(req, timeout=15)
        with open(filepath, "wb") as f:
            f.write(resp.read())
        return filename
    except (urllib.error.URLError, OSError):
        return ""


# ─── Manual Entry ────────────────────────────────────────────────────────────


def manual_add():
    """Interactively add a book without an ISBN barcode."""
    print("\n── Manual Book Entry ──")
    title = input("Title: ").strip()
    if not title:
        print("Title is required.")
        return

    author = input("Author: ").strip()
    if not author:
        print("Author is required.")
        return

    cover_path = input("Cover image path (optional, Enter to skip): ").strip()

    # Try to look up additional details
    print(f"Looking up '{title}' by {author}...")
    book = lookup_by_title_author(title, author)

    if book:
        print(f"  Found: {book['title']} ({book['publish_date']})")
        confirm = input("  Use this data? [Y/n] ").strip().lower()
        if confirm in ("", "y", "yes"):
            # Use looked-up data but override title/author with user input
            book["title"] = title
            if author:
                book["authors"] = [author]
        else:
            book = {
                "isbn": "",
                "title": title,
                "authors": [author],
                "publishers": [],
                "publish_date": "",
                "pages": "",
                "cover_url": "",
                "subjects": [],
                "source": "manual",
            }
    else:
        print("  Not found online. Using manual entry only.")
        book = {
            "isbn": "",
            "title": title,
            "authors": [author],
            "publishers": [],
            "publish_date": input("  Year published (optional): ").strip(),
            "pages": "",
            "cover_url": "",
            "subjects": [],
            "source": "manual",
        }

    # Handle local cover image
    if cover_path and os.path.exists(cover_path):
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(cover_path)[1] or ".jpg"
        cover_filename = f"{title.replace(' ', '_')[:30]}{ext}"
        shutil.copy2(cover_path, COVERS_DIR / cover_filename)
        book["local_cover"] = cover_filename
        print(f"  Cover copied: {cover_filename}")

    # Add to library
    library = load_library()

    # Check for duplicates
    for existing in library:
        if existing["title"].lower() == book["title"].lower():
            print(f"  ⚠️  '{title}' already in library. Adding anyway.")
            break

    library.append(book)
    save_library(library)
    print(f"  ✅ Added: {book['title']} by {', '.join(book['authors'])}")


# ─── Catalog Generation ──────────────────────────────────────────────────────


def generate_catalog(library, config):
    """Generate a static HTML catalog with search, sort, and detail modals."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Download covers
    print("Downloading cover images...")
    for book in library:
        if not book.get("local_cover"):
            cover = download_cover(book, COVERS_DIR)
            if cover:
                book["local_cover"] = cover

    # Build JSON data for the frontend
    site_title = config.get("site_title", "My Library")
    books_json = []
    for i, book in enumerate(library):
        source_url = ""
        if book.get("source") == "openlibrary" and book.get("isbn"):
            source_url = f"https://openlibrary.org/isbn/{book['isbn']}"
        elif book.get("source") == "google" and book.get("isbn"):
            source_url = f"https://books.google.com/books?vid=ISBN{book['isbn']}"
        elif book.get("source") == "isbnsearch" and book.get("isbn"):
            source_url = f"https://isbnsearch.org/isbn/{book['isbn']}"

        books_json.append({
            "id": i,
            "title": book.get("title", "Unknown"),
            "authors": book.get("authors", ["Unknown"]),
            "publishers": book.get("publishers", []),
            "year": book.get("publish_date", ""),
            "pages": book.get("pages", ""),
            "subjects": book.get("subjects", []),
            "isbn": book.get("isbn", ""),
            "cover": f"covers/{book['local_cover']}" if book.get("local_cover") else "",
            "source_url": source_url,
        })

    books_data = json.dumps(books_json)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" href="favicon.ico" type="image/x-icon">
<title>{site_title}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Georgia', serif;
    background: #f5f1eb;
    color: #2c2c2c;
    max-width: 1200px;
    margin: 0 auto;
    padding: 16px;
}}
h1 {{ font-size: 1.8rem; margin-bottom: 4px; color: #1a1a1a; }}
.subtitle {{ color: #666; margin-bottom: 16px; font-size: 0.9rem; }}

/* Controls */
.controls {{
    position: sticky;
    top: 0;
    background: #f5f1eb;
    padding: 12px 0;
    z-index: 100;
    border-bottom: 1px solid #ddd;
    margin-bottom: 16px;
}}
.search-row {{
    display: flex;
    gap: 8px;
    margin-bottom: 8px;
}}
#search {{
    flex: 1;
    padding: 10px 14px;
    border: 1px solid #ccc;
    border-radius: 6px;
    font-size: 1rem;
    font-family: inherit;
}}
.sort-btns button {{
    padding: 6px 14px;
    border: 1px solid #ccc;
    background: #fff;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.85rem;
}}
.sort-btns button.active {{
    background: #2c2c2c;
    color: #fff;
    border-color: #2c2c2c;
}}

/* Alpha nav */
.alpha-nav {{
    display: flex;
    flex-wrap: wrap;
    gap: 2px;
    margin-bottom: 12px;
}}
.alpha-nav a {{
    padding: 4px 8px;
    font-size: 0.8rem;
    text-decoration: none;
    color: #555;
    background: #e8e0d4;
    border-radius: 3px;
    font-weight: 600;
}}
.alpha-nav a:hover {{ background: #2c2c2c; color: #fff; }}
.alpha-nav a.disabled {{ opacity: 0.3; pointer-events: none; }}

/* Grid */
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 16px;
}}
.card {{
    cursor: pointer;
    text-align: center;
    transition: transform 0.15s;
}}
.card:hover {{ transform: translateY(-3px); }}
.card img {{
    width: 100px;
    height: 140px;
    object-fit: cover;
    border-radius: 4px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
}}
.card .no-cover {{
    width: 100px;
    height: 140px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #e8e0d4;
    border-radius: 4px;
    font-size: 1.5rem;
}}
.card .card-title {{
    font-size: 0.8rem;
    margin-top: 6px;
    font-weight: 600;
    line-height: 1.2;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}}
.card .card-author {{
    font-size: 0.75rem;
    color: #666;
    font-style: italic;
}}

/* Letter header */
.letter-header {{
    grid-column: 1 / -1;
    font-size: 1.4rem;
    font-weight: bold;
    color: #1a1a1a;
    border-bottom: 2px solid #2c2c2c;
    padding: 8px 0 4px;
    margin-top: 12px;
}}

/* Modal */
.modal-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 1000;
    align-items: center;
    justify-content: center;
    padding: 24px;
}}
.modal-overlay.open {{ display: flex; }}
.modal {{
    background: #fff;
    border-radius: 12px;
    max-width: 500px;
    width: 100%;
    max-height: 80vh;
    overflow-y: auto;
    padding: 24px;
    position: relative;
}}
.modal-close {{
    position: absolute;
    top: 12px;
    right: 16px;
    font-size: 1.5rem;
    cursor: pointer;
    background: none;
    border: none;
    color: #999;
}}
.modal-cover {{
    text-align: center;
    margin-bottom: 16px;
}}
.modal-cover img {{
    max-height: 200px;
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}}
.modal h2 {{ font-size: 1.3rem; margin-bottom: 4px; }}
.modal .m-author {{ font-style: italic; color: #555; margin-bottom: 12px; }}
.modal .m-meta {{ font-size: 0.85rem; color: #666; line-height: 1.8; }}
.modal .m-meta span {{ display: block; }}
.modal .m-link {{
    display: inline-block;
    margin-top: 12px;
    padding: 8px 16px;
    background: #2c2c2c;
    color: #fff;
    border-radius: 4px;
    text-decoration: none;
    font-size: 0.85rem;
}}
.modal .m-link:hover {{ background: #444; }}
.modal .m-edit-cover {{
    display: inline-block;
    margin-top: 8px;
    margin-left: 8px;
    padding: 8px 16px;
    background: #7b68ee;
    color: #fff;
    border-radius: 4px;
    text-decoration: none;
    font-size: 0.85rem;
    cursor: pointer;
    border: none;
}}
#no-results {{
    display: none;
    text-align: center;
    padding: 40px;
    color: #999;
    font-size: 1.1rem;
    grid-column: 1 / -1;
}}
</style>
</head>
<body>

<h1>{site_title}</h1>
<p class="subtitle"><span id="book-count">{len(library)}</span> books</p>

<div class="controls">
    <div class="search-row">
        <input type="text" id="search" placeholder="Search by title or author..." autocomplete="off">
        <div class="sort-btns">
            <button id="sort-author" class="active" onclick="setSort('author')">Author</button>
            <button id="sort-title" onclick="setSort('title')">Title</button>
        </div>
    </div>
    <div class="alpha-nav" id="alpha-nav"></div>
</div>

<div class="grid" id="grid"></div>
<div id="no-results">No books match your search.</div>

<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
    <div class="modal" id="modal"></div>
</div>

<script>
var BOOKS = {books_data};
var currentSort = 'author';
var currentSearch = '';
var editingBookId = null;

function esc(s) {{
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function sortKey(book, mode) {{
    if (mode === 'author') {{
        var a = book.authors[0] || '';
        var parts = a.split(' ');
        return (parts[parts.length - 1] + ' ' + book.title).toLowerCase();
    }}
    return book.title.toLowerCase();
}}

function getFirstLetter(book, mode) {{
    if (mode === 'author') {{
        var a = book.authors[0] || '';
        var parts = a.split(' ');
        var last = parts[parts.length - 1] || '?';
        return last[0].toUpperCase();
    }}
    var t = book.title.replace(/^(the|a|an)\\s+/i, '');
    return (t[0] || '?').toUpperCase();
}}

function render() {{
    var filtered = BOOKS.filter(function(b) {{
        if (!currentSearch) return true;
        var q = currentSearch.toLowerCase();
        return b.title.toLowerCase().indexOf(q) !== -1 ||
               b.authors.join(' ').toLowerCase().indexOf(q) !== -1;
    }});

    filtered.sort(function(a, b) {{
        return sortKey(a, currentSort).localeCompare(sortKey(b, currentSort));
    }});

    // Build alpha nav
    var letters = {{}};
    filtered.forEach(function(b) {{
        var l = getFirstLetter(b, currentSort);
        if (/[A-Z]/.test(l)) letters[l] = true;
    }});
    var alphaHtml = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').map(function(l) {{
        var cls = letters[l] ? '' : ' disabled';
        return '<a href="#letter-' + l + '" class="' + cls + '">' + l + '</a>';
    }}).join('');
    document.getElementById('alpha-nav').innerHTML = alphaHtml;

    // Build grid
    var html = '';
    var lastLetter = '';
    filtered.forEach(function(b) {{
        var l = getFirstLetter(b, currentSort);
        if (l !== lastLetter && /[A-Z]/.test(l)) {{
            html += '<div class="letter-header" id="letter-' + l + '">' + l + '</div>';
            lastLetter = l;
        }}
        var cover = b.cover
            ? '<img src="' + b.cover + '" alt="Cover" loading="lazy">'
            : '<div class="no-cover">📖</div>';
        html += '<div class="card" onclick="openModal(' + b.id + ')">' +
            cover +
            '<div class="card-title">' + esc(b.title) + '</div>' +
            '<div class="card-author">' + esc(b.authors.join(', ')) + '</div></div>';
    }});

    document.getElementById('grid').innerHTML = html;
    document.getElementById('no-results').style.display = filtered.length ? 'none' : 'block';
    document.getElementById('book-count').textContent = filtered.length;
}}

function setSort(mode) {{
    currentSort = mode;
    document.getElementById('sort-author').className = mode === 'author' ? 'active' : '';
    document.getElementById('sort-title').className = mode === 'title' ? 'active' : '';
    render();
}}

document.getElementById('search').addEventListener('input', function(e) {{
    currentSearch = e.target.value;
    render();
}});

function openModal(id) {{
    var b = BOOKS.find(function(x) {{ return x.id === id; }});
    if (!b) return;
    editingBookId = id;
    var cover = b.cover
        ? '<img src="' + b.cover + '" alt="Cover">'
        : '<div style="font-size:4rem;padding:20px">📖</div>';
    var meta = '';
    if (b.publishers.length) meta += '<span>📚 ' + esc(b.publishers.join(', ')) + '</span>';
    if (b.year) meta += '<span>📅 ' + b.year + '</span>';
    if (b.pages) meta += '<span>📄 ' + b.pages + ' pages</span>';
    if (b.isbn) meta += '<span>ISBN: ' + b.isbn + '</span>';
    if (b.subjects.length) meta += '<span>🏷️ ' + esc(b.subjects.join(', ')) + '</span>';
    var link = b.source_url
        ? '<a class="m-link" href="' + b.source_url + '" target="_blank">View Source \u2197</a>'
        : '';
    document.getElementById('modal').innerHTML =
        '<button class="modal-close" onclick="closeModal()">&times;</button>' +
        '<div class="modal-cover">' + cover + '</div>' +
        '<h2>' + esc(b.title) + '</h2>' +
        '<p class="m-author">' + esc(b.authors.join(', ')) + '</p>' +
        '<div class="m-meta">' + meta + '</div>' +
        link;
    document.getElementById('modal-overlay').classList.add('open');
}}

function closeModal(e) {{
    if (!e || e.target === document.getElementById('modal-overlay')) {{
        document.getElementById('modal-overlay').classList.remove('open');
    }}
}}
document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') closeModal();
}});

render();
</script>
</body>
</html>"""

    output_file = OUTPUT_DIR / "index.html"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Catalog generated: {output_file}")
    print(f"   {len(library)} books, covers in {COVERS_DIR}/")
    return output_file



# ─── Publish ─────────────────────────────────────────────────────────────────


def publish(config):
    """Publish the output directory via scp."""
    target = config.get("publish_target", "")
    if not target or target == "user@example.com:/var/www/html/books/":
        print("Error: Set 'publish_target' in config.json first.")
        print(f"  Config file: {CONFIG_FILE}")
        return

    if not OUTPUT_DIR.exists():
        print("Error: No output to publish. Run 'python catalog.py' first.")
        return

    print(f"Publishing to {target}...")
    cmd = ["scp", "-r", str(OUTPUT_DIR) + "/.", target]
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print("✅ Published successfully!")
    else:
        print(f"❌ Publish failed (exit code {result.returncode})")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    config = load_config()

    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "add":
            manual_add()
            # Regenerate catalog after adding
            library = load_library()
            if library:
                generate_catalog(library, config)
            return
        elif command == "edit":
            edit_library()
            library = load_library()
            if library:
                generate_catalog(library, config)
            return
        elif command == "covers":
            assign_covers(config)
            library = load_library()
            if library:
                generate_catalog(library, config)
            return
        elif command == "publish":
            publish(config)
            return
        else:
            print(f"Unknown command: {command}")
            print("Usage: python catalog.py [add|edit|covers|publish]")
            return

    # Default: process ISBNs and generate catalog
    isbns_file = Path(config["isbns_file"]).expanduser()
    if not isbns_file.exists():
        print(f"ISBN file not found: {isbns_file}")
        print("Scan some books first, or use 'python catalog.py add' for manual entry.")
        return

    # Read ISBNs
    with open(isbns_file) as f:
        isbns = [line.strip() for line in f if line.strip()]

    if not isbns:
        print("No ISBNs found in file.")
        return

    print(f"Found {len(isbns)} ISBN(s) to process.")

    # Load existing library
    library = load_library()
    existing_isbns = {b["isbn"] for b in library if b.get("isbn")}

    # Look up new ISBNs
    new_count = 0
    for isbn in isbns:
        if isbn in existing_isbns:
            print(f"  ⏭️  {isbn} — already in library")
            continue

        print(f"  🔍 Looking up {isbn}...", end=" ", flush=True)

        # Skip UPC codes (12 digits, not an ISBN)
        if len(isbn) == 12 and not isbn.startswith('978') and not isbn.startswith('979'):
            print(f"⚠️  UPC code (not an ISBN) — use manual entry for this book")
            continue

        book = lookup_isbn(isbn)
        if book:
            library.append(book)
            print(f"✅ {book['title']} by {', '.join(book['authors'])}")
            new_count += 1
        else:
            print("❌ Not found")
            library.append({
                "isbn": isbn,
                "title": f"Unknown (ISBN: {isbn})",
                "authors": ["Unknown"],
                "publishers": [],
                "publish_date": "",
                "pages": "",
                "cover_url": "",
                "subjects": [],
                "source": "not_found",
            })
            new_count += 1

        time.sleep(1)  # Rate limit

    # Process manual entries (from phone manual tab)
    manual_file = Path(config["isbns_file"]).expanduser().parent / "manual_books.json"
    if manual_file.exists():
        with open(manual_file) as f:
            try:
                manual_entries = json.load(f)
            except json.JSONDecodeError:
                manual_entries = []

        if manual_entries:
            print(f"\nProcessing {len(manual_entries)} manual entries...")
            for entry in manual_entries:
                title = entry.get("title", "")
                author = entry.get("author", "")

                # Check if already in library
                dup = False
                for existing in library:
                    if existing.get("title", "").lower() == title.lower():
                        print(f"  ⏭️  {title} — already in library")
                        dup = True
                        break
                if dup:
                    continue

                print(f"  🔍 Looking up '{title}' by {author}...", end=" ", flush=True)
                book = lookup_by_title_author(title, author)
                if not book:
                    book = {
                        "isbn": "", "title": title, "authors": [author],
                        "publishers": [], "publish_date": "", "pages": "",
                        "cover_url": "", "subjects": [], "source": "manual",
                    }
                    print("(manual only)")
                else:
                    print(f"✅ Found additional metadata")

                # Handle embedded cover photo from phone
                cover_data = entry.get("cover", "")
                if cover_data and cover_data.startswith("data:image"):
                    import base64
                    COVERS_DIR.mkdir(parents=True, exist_ok=True)
                    # Extract base64 data
                    header, b64data = cover_data.split(",", 1)
                    ext = ".jpg" if "jpeg" in header else ".png"
                    filename = f"{title.replace(' ', '_')[:30]}{ext}"
                    with open(COVERS_DIR / filename, "wb") as cf:
                        cf.write(base64.b64decode(b64data))
                    book["local_cover"] = filename
                    print(f"    📷 Saved cover: {filename}")

                library.append(book)
                new_count += 1
                time.sleep(1)

    save_library(library)
    print(f"\n{new_count} new book(s) added. Library total: {len(library)}")

    # Generate catalog
    generate_catalog(library, config)


if __name__ == "__main__":
    main()
