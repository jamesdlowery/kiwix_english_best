#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kiwix English ZIM File Selector + Downloader
Version: v20260319e   # Added startup menu + full resume-capable downloader
Purpose: Crawl Kiwix, select best English ZIMs, save list, and optionally
         download (or resume) files with automatic partial-file detection.
Last updated: 2026-03-19
"""

import urllib.request
from urllib.parse import urljoin
import time
import re
import sys
import os
from datetime import datetime
from collections import defaultdict

# ==================== CONFIGURATION ====================
VERSION = "v20260319e"

BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Selector/{VERSION} (personal research)"
DELAY = 0.8
MAX_DEPTH = 4
LIST_FILE = "kiwix_english_best.txt"

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
            time.sleep(DELAY)
            return collected

        for line in pre_match.group(1).splitlines():
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


def select_best_per_group(all_files):
    groups = defaultdict(list)
    for f in all_files:
        groups[f['group_key']].append(f)

    selected = []
    for key, files in sorted(groups.items()):
        if not files:
            continue
        files.sort(key=lambda x: (-x['date'].timestamp(), -x['size_bytes']))
        selected.append(files[0])
        print(f"Selected for group '{key}': {files[0]['filename']} "
              f"({files[0]['date_str']} | {files[0]['size_str']})", file=sys.stderr)

    return sorted(selected, key=lambda x: x['filename'])


# ==================== SAVE / LOAD ====================
def save_list(best_files):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_files = len(best_files)
    total_bytes = sum(f['size_bytes'] for f in best_files)
    total_human = bytes_to_human(total_bytes)
    total_gb = round(total_bytes / (1024**3), 1) if total_bytes else 0

    with open(LIST_FILE, 'w', encoding='utf-8') as f:
        f.write(f"# Kiwix English ZIM best/latest files - {VERSION}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Total selected files: {total_files}\n")
        f.write(f"# Total estimated download size: {total_human} ({total_gb} GB)\n")
        f.write("# Format: filename|size_bytes|url\n\n")
        for item in best_files:
            f.write(f"{item['filename']}|{item['size_bytes']}|{item['url']}\n")
    print(f"Saved to {LIST_FILE} (with full resume info)")


def load_list_from_file():
    if not os.path.exists(LIST_FILE):
        print(f"Error: {LIST_FILE} not found.")
        return []
    items = []
    with open(LIST_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                fn, sz, url = parts
                items.append({
                    'filename': fn,
                    'size_bytes': int(sz),
                    'url': url
                })
    return items


# ==================== DOWNLOAD WITH RESUME ====================
def download_file(url, filename, expected_bytes):
    local_path = filename  # downloads to current directory

    if os.path.exists(local_path):
        local_size = os.path.getsize(local_path)
        if local_size == expected_bytes:
            print(f"✓ Already complete: {filename}")
            return
        if local_size > expected_bytes:
            print(f"⚠ {filename} is larger than expected – skipping.")
            return
        print(f"↻ Resuming {filename} ({bytes_to_human(local_size)} / {bytes_to_human(expected_bytes)})")
        headers = {'Range': f'bytes={local_size}-', 'User-Agent': USER_AGENT}
        mode = 'ab'
    else:
        print(f"↓ Downloading {filename} ({bytes_to_human(expected_bytes)})")
        headers = {'User-Agent': USER_AGENT}
        mode = 'wb'

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            with open(local_path, mode) as f:
                while True:
                    chunk = response.read(128 * 1024)  # 128 KB chunks
                    if not chunk:
                        break
                    f.write(chunk)
        final_size = os.path.getsize(local_path)
        if final_size == expected_bytes or expected_bytes == 0:
            print(f"✓ Completed: {filename}")
        else:
            print(f"⚠ Size mismatch for {filename} (got {final_size} bytes)")
    except Exception as e:
        print(f"Error downloading {filename}: {e}")


def download_list(items):
    print(f"\nStarting download of {len(items)} file(s)...\n")
    for item in items:
        download_file(item['url'], item['filename'], item['size_bytes'])
    print("\nAll downloads finished!")


# ==================== MAIN ====================
def main():
    print(f"Kiwix English ZIM Selector {VERSION}\n")
    print("1) Crawl Kiwix and generate a new best list")
    print("2) Load existing list and download / resume")
    mode = input("Choose (1 or 2): ").strip()

    if mode == "1":
        # === GENERATE NEW LIST ===
        print("\nCrawling Kiwix ZIM directory...")
        all_english = fetch_directory(BASE_URL)
        if not all_english:
            print("No English ZIM files found.")
            return

        best_files = select_best_per_group(all_english)
        print(f"\n=== SELECTED BEST FILES ({len(best_files)}) ===")
        for f in best_files:
            print(f["url"])

        if input("\nSave list to file? (y/n): ").strip().lower() == 'y':
            save_list(best_files)
            if input("Start downloading the files now? (y/n): ").strip().lower() == 'y':
                download_list(best_files)
    else:
        # === LOAD EXISTING LIST & DOWNLOAD/RESUME ===
        print(f"\nLoading {LIST_FILE}...")
        best_files = load_list_from_file()
        if not best_files:
            return
        print(f"Loaded {len(best_files)} files from list.")
        download_list(best_files)


if __name__ == "__main__":
    main()
