#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiwix English ZIM Selector + Downloader + Update Checker
Version: v20260320e   # Dual progress bars (total files + per-file bytes) + full feature set
Purpose: Crawl Kiwix, select best English ZIMs, download with nested tqdm progress,
         supports Ctrl+C pause + resume after reboot via state file,
         auto-cleanup of older versions after updates.
"""

import urllib.request
from urllib.parse import urljoin
import time
import re
import sys
import os
import json
from datetime import datetime
from collections import defaultdict

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False
    class DummyTqdm:
        def __init__(self, *args, **kwargs): pass
        def update(self, n): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    tqdm = DummyTqdm

# ==================== CONFIGURATION ====================
VERSION = "v20260320e"
BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Selector/{VERSION} (personal research)"
DELAY = 0.8
MAX_DEPTH = 4
LIST_FILE = "kiwix_english_best.txt"
STATE_FILE = "kiwix_download_state.json"
CHUNK_SIZE = 128 * 1024

ENGLISH_PATTERNS = [
    r'_en_', r'en_all', r'wikipedia_en_', r'wiktionary_en_', r'wikibooks_en_',
    r'wikivoyage_en_', r'wikiquote_en_', r'wikisource_en_', r'en_stackexchange',
    r'_english', r'en_simple', r'stackoverflow_en', r'english\.stackexchange'
]

UPDATE_SIZE_THRESHOLD = 1.05

# ==================== HELPER FUNCTIONS ====================
def is_english_zim(filename):
    fn = filename.lower()
    return fn.endswith('.zim') and any(re.search(p, fn) for p in ENGLISH_PATTERNS)

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
    return int(num * multipliers.get(unit[0], 1))

def bytes_to_human(b):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if b < 1024:
            return f"{b:3.1f} {unit}" if unit != 'B' else f"{int(b)} B"
        b /= 1024
    return f"{b:3.1f} PB"

def extract_group_key(fn):
    base = re.sub(r'_\d{4}-\d{2}(\.zim)?$', '', fn, flags=re.I)
    return re.sub(r'\.zim$', '', base).lower()

def parse_date_from_filename(filename):
    match = re.search(r'(\d{4}-\d{2})', filename)
    if match:
        try:
            return datetime.strptime(match.group(1) + '-01', '%Y-%m-%d')
        except:
            return datetime.min
    return datetime.min

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

    return sorted(selected, key=lambda x: x['filename'])

# ==================== SAVE / LOAD / STATE ====================
def save_list(items, filename=LIST_FILE):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_bytes = sum(f['size_bytes'] for f in items)
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# Kiwix English ZIM best/latest files - {VERSION}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Total files: {len(items)}\n")
        f.write(f"# Total size: {bytes_to_human(total_bytes)}\n")
        f.write("# Format: filename|size_bytes|url\n\n")
        for item in items:
            f.write(f"{item['filename']}|{item['size_bytes']}|{item['url']}\n")
    print(f"Saved list to {filename}")

def load_list(path=LIST_FILE):
    if not os.path.exists(path):
        return []
    items = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                fn, sz_str, url = parts
                try:
                    items.append({
                        'filename': fn,
                        'size_bytes': int(sz_str),
                        'url': url,
                        'group_key': extract_group_key(fn)
                    })
                except:
                    pass
    return items

def save_state(items):
    serializable = []
    for item in items:
        s_item = item.copy()
        if 'date' in s_item and isinstance(s_item['date'], datetime):
            s_item['date'] = s_item['date'].isoformat() if s_item['date'] != datetime.min else None
        serializable.append(s_item)

    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(serializable, f, indent=2)
    print(f"State saved to {STATE_FILE}")

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    for item in data:
        if 'date' in item and item['date']:
            try:
                item['date'] = datetime.fromisoformat(item['date'])
            except ValueError:
                item['date'] = datetime.min
        elif 'date' in item:
            item['date'] = datetime.min
    return data

# ==================== DOWNLOAD WITH DUAL PROGRESS BARS ====================
def download_file(item, file_index, total_files, pbar_total):
    url = item['url']
    filename = item['filename']
    expected_bytes = item['size_bytes']
    local_path = filename
    downloaded = 0

    if os.path.exists(local_path):
        local_size = os.path.getsize(local_path)
        if local_size == expected_bytes:
            print(f"✓ Already complete: {filename} ({file_index}/{total_files})")
            pbar_total.update(1)
            return True
        downloaded = local_size
        print(f"↻ Resuming {filename} ({file_index}/{total_files})")
        headers = {'Range': f'bytes={local_size}-', 'User-Agent': USER_AGENT}
        mode = 'ab'
    else:
        print(f"↓ Downloading {filename} ({file_index}/{total_files})")
        headers = {'User-Agent': USER_AGENT}
        mode = 'wb'

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            total_size = expected_bytes if expected_bytes > 0 else None

            with open(local_path, mode) as f:
                with tqdm(
                    desc=filename,
                    total=total_size,
                    initial=downloaded,
                    unit='B',
                    unit_scale=True,
                    unit_divisor=1024,
                    disable=not TQDM_AVAILABLE,
                    leave=False,
                    position=1,
                    dynamic_ncols=True,
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{rate_fmt}{postfix}]"
                ) as pbar_file:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        pbar_file.update(len(chunk))

        print(f"✓ Completed: {filename} ({file_index}/{total_files})")
        pbar_total.update(1)
        return True

    except KeyboardInterrupt:
        print(f"\n⏸ Paused: {filename} ({file_index}/{total_files})")
        raise
    except Exception as e:
        print(f"Error downloading {filename}: {e} ({file_index}/{total_files})")
        return False


def download_list(items):
    total = len(items)
    print(f"\nStarting download of {total} file(s)... (Ctrl+C to pause)\n")

    remaining = items[:]

    with tqdm(
        total=total,
        desc="Files",
        unit="file",
        disable=not TQDM_AVAILABLE,
        position=0,
        leave=True,
        dynamic_ncols=True,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]"
    ) as pbar_total:

        try:
            for i, item in enumerate(items, 1):
                success = download_file(item, i, total, pbar_total)
                if success:
                    remaining.remove(item)
                    save_state(remaining)
        except KeyboardInterrupt:
            print("\n⏸ Download paused. State saved.")
            save_state(remaining)
            return
        except Exception as e:
            print(f"Unexpected error: {e}")
            save_state(remaining)
            return

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    print(f"\n🎉 All {total} files completed!")


# ==================== CLEANUP ====================
def cleanup_old_versions(downloaded_items):
    print("\nCleaning up older versions...")
    for item in downloaded_items:
        group = item['group_key']
        new_date = parse_date_from_filename(item['filename'])
        for local_file in list(os.listdir('.')):
            if not local_file.endswith('.zim'):
                continue
            if extract_group_key(local_file) == group and local_file != item['filename']:
                old_date = parse_date_from_filename(local_file)
                if old_date < new_date:
                    try:
                        os.remove(local_file)
                        print(f"🗑 Removed old version: {local_file}")
                    except Exception as e:
                        print(f"Could not delete {local_file}: {e}")


# ==================== UPDATE CHECKER ====================
def check_for_updates():
    old_items = load_list()
    if not old_items:
        print("No previous list found. Run mode 1 first.")
        return None, None

    print("Re-crawling server for latest versions...")
    all_current = fetch_directory(BASE_URL)
    if not all_current:
        print("Crawl failed.")
        return None, None

    current_best = select_best_per_group(all_current)
    current_dict = {f['group_key']: f for f in current_best}
    old_dict = {item['group_key']: item for item in old_items}

    updates = []
    new_groups = []

    for old in old_items:
        key = old['group_key']
        if key not in current_dict:
            continue
        curr = current_dict[key]
        old_date = old.get('date', parse_date_from_filename(old['filename']))
        curr_date = curr['date']
        is_newer = (curr_date > old_date) or \
                   (curr['size_bytes'] > old['size_bytes'] * UPDATE_SIZE_THRESHOLD)
        if is_newer:
            updates.append(curr)

    for key, curr in current_dict.items():
        if key not in old_dict:
            new_groups.append(curr)

    to_download = updates + new_groups
    return to_download, current_best

# ==================== MAIN MENU ====================
def main():
    print(f"Kiwix English ZIM Tool {VERSION}\n")
    print("1) Generate new best list")
    print("2) Download / resume from existing list")
    print("3) Check for updates (includes new groups + auto-cleanup)")
    choice = input("Choose (1/2/3): ").strip()

    if choice == "1":
        print("\nGenerating new best list...")
        all_eng = fetch_directory(BASE_URL)
        if not all_eng:
            print("No English ZIM files found.")
            return

        best = select_best_per_group(all_eng)
        print(f"\nSelected {len(best)} best files:")
        for f in best:
            print(f"  {f['filename']}  ({f['size_str']})")

        save_choice = input("\nSave list to file? (y/n): ").strip().lower()
        if save_choice == 'y':
            save_list(best)
            print("List saved.")

        download_choice = input("Download the list now? (y/n): ").strip().lower()
        if download_choice == 'y':
            download_list(best)

    elif choice == "2":
        state = load_state()
        if state:
            print(f"Found saved download state with {len(state)} files remaining.")
            if input("Resume from saved state? (y/n): ").lower() == 'y':
                download_list(state)
                return

        items = load_list()
        if items:
            download_list(items)
        else:
            print("No list or state found.")

    elif choice == "3":
        to_download, current_best = check_for_updates()
        if not to_download:
            return

        print(f"\nFound {len(to_download)} updates/new groups:")
        total_bytes = sum(f['size_bytes'] for f in to_download)
        for f in to_download:
            print(f"  {f['filename']}  ({f['size_str']})")
        print(f"Total download size: {bytes_to_human(total_bytes)}")

        if input("\nSave updated list? (y/n): ").lower() == 'y':
            save_list(current_best)

        if input("Download updates/new files now? (y/n): ").lower() == 'y':
            download_list(to_download)
            cleanup_old_versions(to_download)

    else:
        print("Invalid choice.")

if __name__ == "__main__":
    main()
