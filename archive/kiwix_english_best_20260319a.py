#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kiwix English ZIM File Selector
Version: v20260319a
Purpose: Recursively crawl https://download.kiwix.org/zim/, find English ZIM files,
         parse real dates & sizes from Apache <pre> listings, and select only the
         latest + largest (most comprehensive) version per variant/group.
Last updated: 2026-03-19
"""

import urllib.request
from urllib.parse import urljoin
import time
import re
import sys
from datetime import datetime
from collections import defaultdict

# ==================== CONFIGURATION ====================
VERSION = "v20260319a"  # <--- Version tracking line

BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Selector/{VERSION} (personal research)"
DELAY = 0.8          # Be polite to the server
MAX_DEPTH = 3        # Safety limit (root → categories → files)

# English detection patterns (add/remove as needed)
ENGLISH_PATTERNS = [
    r'_en_', r'en_all', r'wikipedia_en_', r'wiktionary_en_', r'wikibooks_en_',
    r'wikivoyage_en_', r'wikiquote_en_', r'wikisource_en_', r'en_stackexchange',
    r'_english', r'en_simple', r'stackoverflow_en', r'english\.stackexchange'
]


# ==================== HELPER FUNCTIONS ====================
def is_english_zim(filename):
    """Check if filename is a ZIM file and likely English."""
    filename_lower = filename.lower()
    return filename_lower.endswith('.zim') and any(re.search(pat, filename_lower) for pat in ENGLISH_PATTERNS)


def parse_size_to_bytes(size_str):
    """Convert '115G', '48M', '2.0K', etc. to integer bytes."""
    if not size_str or size_str in ('-', ''):
        return 0
    size_str = size_str.upper().strip()
    multipliers = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
    match = re.match(r'([\d.]+)\s*([KMGT]?B?)?', size_str)
    if not match:
        return 0
    num = float(match.group(1))
    unit = match.group(2) or ''
    for k, v in multipliers.items():
        if unit.startswith(k):
            return int(num * v)
    return int(num)  # plain bytes


def extract_group_key(filename):
    """Create a group key by stripping date and extension (e.g. wikipedia_en_all_maxi)."""
    base = re.sub(r'_\d{4}-\d{2}(\.zim)?$', '', filename, flags=re.IGNORECASE)
    base = re.sub(r'\.zim$', '', base)
    return base.lower()


def parse_apache_pre_line(line):
    """
    Parse one line from Apache <pre> directory listing.
    Returns (filename, href, date_str, size_str) or None.
    """
    line = line.strip()
    if not line or line.startswith(('Name', 'Parent Directory', '<hr>')):
        return None

    match = re.match(
        r'^(?:\s*<img[^>]*>\s*)?'
        r'(?:<a href="([^"]+)">)?'
        r'([^<]+?)'
        r'(?:</a>)?'
        r'\s+'
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}|-)'
        r'\s+'
        r'([\d.]+[KMGT]?B?|-)\s*$',
        line, re.IGNORECASE
    )
    if not match:
        return None

    href, filename, date_str, size_str = match.groups()

    if not filename and href:
        filename = href.split('/')[-1] if '/' in href else href

    filename = filename.strip()
    if not filename.endswith('.zim'):
        return None

    href = href or filename
    return filename.strip(), href.strip(), date_str.strip(), size_str.strip()


# ==================== CRAWLER ====================
def fetch_directory(url, depth=0, visited=None, collected=None):
    if collected is None:
        collected = []
    if visited is None:
        visited = set()
    if url in visited or depth > MAX_DEPTH:
        return collected

    visited.add(url)
    print(f"Scanning: {url} (depth {depth})", file=sys.stderr)

    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req) as response:
            html = response.read().decode('utf-8', errors='ignore')

        pre_match = re.search(r'<pre>(.*?)</pre>', html, re.DOTALL | re.IGNORECASE)
        if not pre_match:
            print(f"  No <pre> block found at {url}", file=sys.stderr)
            time.sleep(DELAY)
            return collected

        pre_content = pre_match.group(1)
        lines = pre_content.splitlines()

        for line in lines:
            parsed = parse_apache_pre_line(line)
            if not parsed:
                continue

            filename, href, date_str, size_str = parsed

            if not is_english_zim(filename):
                continue

            full_url = urljoin(url, href)

            date_obj = datetime.min
            if date_str != '-' and ' ' in date_str:
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
                except ValueError:
                    pass

            size_bytes = parse_size_to_bytes(size_str)

            collected.append({
                'url': full_url,
                'filename': filename,
                'group_key': extract_group_key(filename),
                'date': date_obj,
                'size_bytes': size_bytes,
                'size_str': size_str,
                'date_str': date_str
            })
            print(f"  Found: {filename} | {date_str} | {size_str}", file=sys.stderr)

            if href.endswith('/'):
                fetch_directory(full_url, depth + 1, visited, collected)

        time.sleep(DELAY)

    except Exception as e:
        print(f"Error at {url}: {e}", file=sys.stderr)

    return collected


# ==================== SELECT BEST VERSIONS ====================
def select_best_per_group(all_files):
    groups = defaultdict(list)
    for f in all_files:
        groups[f['group_key']].append(f)

    selected = []
    for key, files in sorted(groups.items()):
        if not files:
            continue
        files.sort(key=lambda x: (-x['date'].timestamp(), -x['size_bytes']))
        best = files[0]
        selected.append(best['url'])
        print(f"Selected for group '{key}': {best['filename']} "
              f"({best['date_str']} | {best['size_str']})", file=sys.stderr)

    return sorted(selected)


# ==================== MAIN ====================
def main():
    print(f"Kiwix English ZIM Selector {VERSION}")
    print("Crawling https://download.kiwix.org/zim/ for English files...")
    print("Selecting only the latest + most comprehensive version per variant.\n")
    print("This may take several minutes.\n")

    all_english = fetch_directory(BASE_URL)

    if not all_english:
        print("No English ZIM files found.")
        return

    best_urls = select_best_per_group(all_english)

    print("\n=== SELECTED BEST (Latest + Most Comprehensive) English ZIM Download Links ===")
    print(f"Total variants selected: {len(best_urls)}\n")

    for url in best_urls:
        print(url)

    save = input("\nSave list to 'kiwix_english_best.txt'? (y/n): ").strip().lower()
    if save == 'y':
        with open('kiwix_english_best.txt', 'w', encoding='utf-8') as f:
            f.write(f"# Kiwix English ZIM best files - {VERSION}\n")
            f.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("\n".join(best_urls) + "\n")
        print("Saved to kiwix_english_best.txt")


if __name__ == "__main__":
    main()
