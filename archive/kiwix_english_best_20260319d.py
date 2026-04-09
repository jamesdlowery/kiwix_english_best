#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kiwix English ZIM File Selector
Version: v20260319d   # Added automatic cumulative total size calculation (e.g. 1.21 TB)
Purpose: Recursively crawl https://download.kiwix.org/zim/, find English ZIM files,
         parse real dates & sizes, select latest + largest per variant,
         and report clean total download size in the saved file.
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
VERSION = "v20260319d"

BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Selector/{VERSION} (personal research)"
DELAY = 0.8
MAX_DEPTH = 4

ENGLISH_PATTERNS = [
    r'_en_', r'en_all', r'wikipedia_en_', r'wiktionary_en_', r'wikibooks_en_',
    r'wikivoyage_en_', r'wikiquote_en_', r'wikisource_en_', r'en_stackexchange',
    r'_english', r'en_simple', r'stackoverflow_en', r'english\.stackexchange'
]


# ==================== HELPER FUNCTIONS ====================
def is_english_zim(filename):
    filename_lower = filename.lower()
    return filename_lower.endswith('.zim') and any(re.search(pat, filename_lower) for pat in ENGLISH_PATTERNS)


def parse_size_to_bytes(size_str):
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
    return int(num)


def bytes_to_human(num_bytes):
    """Convert bytes to human-readable string (1.21 TB, 842 GB, etc.)."""
    if num_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}" if unit != 'B' else f"{int(num_bytes)} B"
        num_bytes /= 1024.0
    return f"{num_bytes:3.1f} PB"


def extract_group_key(filename):
    base = re.sub(r'_\d{4}-\d{2}(\.zim)?$', '', filename, flags=re.IGNORECASE)
    base = re.sub(r'\.zim$', '', base)
    return base.lower()


def parse_apache_pre_line(line):
    line = line.strip()
    if not line or line.startswith(('Name', 'Parent Directory', '<hr>')):
        return None

    match = re.match(
        r'^(?:\s*<img[^>]*>\s*)?'
        r'(?:<a href="([^"]+)">)?'
        r'([^<]+?)'
        r'(?:</a>)?'
        r'\s{2,}'
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}|-)?\s*'
        r'([\d.]+[KMGT]?B?|-)?\s*$',
        line, re.IGNORECASE
    )
    if not match:
        return None

    href, filename, date_str, size_str = match.groups()
    if not filename and href:
        filename = href.rsplit('/', 1)[-1] if '/' in href else href

    filename = filename.strip()
    href = href or filename
    return filename, href, (date_str or '-'), (size_str or '-')


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
            full_url = urljoin(url, href)

            if href.endswith('/') and not href.startswith('.'):
                fetch_directory(full_url, depth + 1, visited, collected)

            if is_english_zim(filename):
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
        selected.append(best)
        print(f"Selected for group '{key}': {best['filename']} "
              f"({best['date_str']} | {best['size_str']})", file=sys.stderr)

    return sorted(selected, key=lambda x: x['filename'])


# ==================== MAIN ====================
def main():
    print(f"Kiwix English ZIM Selector {VERSION}")
    print("Crawling https://download.kiwix.org/zim/ for English files...")
    print("Selecting latest + most comprehensive version per variant.\n")

    all_english = fetch_directory(BASE_URL)

    if not all_english:
        print("No English ZIM files found.")
        return

    best_files = select_best_per_group(all_english)
    best_urls = [f['url'] for f in best_files]

    print("\n=== SELECTED BEST English ZIM Download Links ===")
    print(f"Total selected: {len(best_files)}\n")

    for url in best_urls:
        print(url)

    save = input("\nSave list to 'kiwix_english_best.txt'? (y/n): ").strip().lower()
    if save == 'y':
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total_files = len(best_files)
        total_bytes = sum(f['size_bytes'] for f in best_files)
        total_human = bytes_to_human(total_bytes)
        total_gb = round(total_bytes / (1024**3), 1) if total_bytes else 0

        with open('kiwix_english_best.txt', 'w', encoding='utf-8') as f:
            f.write(f"# Kiwix English ZIM best/latest files - {VERSION}\n")
            f.write(f"# Generated: {now}\n")
            f.write(f"# Total selected files: {total_files}\n")
            f.write(f"# Total estimated download size: {total_human} ({total_gb} GB)\n")
            f.write("# Note: Sizes are as reported by the server; actual may vary slightly.\n\n")
            f.write("\n".join(best_urls) + "\n")
        print("Saved to kiwix_english_best.txt (with cumulative total size)")

if __name__ == "__main__":
    main()
