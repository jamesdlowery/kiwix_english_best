#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiwix English ZIM Selector + Downloader + Update Checker
Version: v20260322p   # UPDATED: 5% size tolerance rule (max(50 MiB, expected // 20))
"""

import urllib.request
from urllib.parse import urljoin
import time
import re
import sys
import os
import json
import requests
from datetime import datetime
from collections import defaultdict
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    import libtorrent as lt
    LIBTORRENT_AVAILABLE = True
except ImportError:
    LIBTORRENT_AVAILABLE = False

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
VERSION = "v20260322p"
BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Downloader/{VERSION}"
DELAY = 0.8
MAX_DEPTH = 4
LIST_FILE = "kiwix_english_best.txt"
STATE_FILE = "kiwix_download_state.json"
CHUNK_SIZE = 1024 * 1024  # 1 MiB
TORRENT_CHECK_TIMEOUT = 10
RESUME_SAVE_INTERVAL = 30  # seconds

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

    def check_torrent(item):
        torrent_url = item['url'] + '.torrent'
        try:
            headers = {'User-Agent': USER_AGENT}
            r = requests.get(torrent_url, headers=headers, stream=True, timeout=TORRENT_CHECK_TIMEOUT, allow_redirects=True)
            status = r.status_code
            reason = r.reason
            print(f"  {item['filename']} → {status} {reason} for {torrent_url}")
            return torrent_url if status == 200 else ''
        except requests.exceptions.RequestException as e:
            print(f"  {item['filename']} → error for {torrent_url}: {e}")
            return ''

    print("Checking for torrent files... (sequential with full status)")
    torrent_urls = []
    for item in items:
        t_url = check_torrent(item)
        torrent_urls.append(t_url)

    torrent_count = sum(1 for url in torrent_urls if url)
    print(f"Found {torrent_count} torrent links out of {len(items)} files.")

    for item, t_url in zip(items, torrent_urls):
        item['torrent_url'] = t_url

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# Kiwix English ZIM best/latest files - {VERSION}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Total files: {len(items)}\n")
        f.write(f"# Total size: {bytes_to_human(total_bytes)}\n")
        f.write("# Format: filename|size_bytes|url|torrent_url\n\n")
        for item in items:
            f.write(f"{item['filename']}|{item['size_bytes']}|{item['url']}|{item.get('torrent_url', '')}\n")

    print(f"Saved list to {filename}")
    return items

def load_list(path=LIST_FILE):
    if not os.path.exists(path):
        return []
    items = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            parts = [p.strip() for p in line.split('|') if p.strip()]
            if len(parts) >= 3:
                fn = parts[0]
                try:
                    sz = int(parts[1])
                    http_url = parts[2]
                    torrent_url = parts[3] if len(parts) >= 4 else ''
                    items.append({
                        'filename': fn,
                        'size_bytes': sz,
                        'url': http_url,
                        'torrent_url': torrent_url,
                        'group_key': extract_group_key(fn)
                    })
                except ValueError:
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

# ==================== DOWNLOAD SINGLE FILE ====================
def download_single(item, file_index, total_files):
    filename = item['filename']
    expected_bytes = item['size_bytes']
    http_url = item['url']
    torrent_url = item.get('torrent_url', '')

    partial_path = f"{filename}.partial"
    resume_path = f"{filename}.fastresume"

    if os.path.exists(filename) and os.path.getsize(filename) == expected_bytes:
        print(f"✓ Already complete: {filename} ({file_index}/{total_files})")
        return True

    target_path = partial_path if os.path.exists(partial_path) else filename
    downloaded = os.path.getsize(target_path) if os.path.exists(target_path) else 0

    if target_path == partial_path:
        print(f"↻ Resuming {filename} from partial ({file_index}/{total_files})")
    else:
        print(f"↓ Downloading {filename} ({file_index}/{total_files})")

    success = False

    if torrent_url and LIBTORRENT_AVAILABLE:
        print(f"  Trying torrent for {filename} (url: {torrent_url})...")
        success = download_torrent(torrent_url, target_path, expected_bytes, downloaded, filename, resume_path)
        if success:
            print(f"  Torrent succeeded for {filename}")
        else:
            print(f"  Torrent failed for {filename} — falling back to HTTP")
    else:
        if not torrent_url:
            print(f"  No torrent available for {filename}")
        if not LIBTORRENT_AVAILABLE:
            print(f"  libtorrent not installed/available — skipping torrent")

    if not success:
        print(f"  Using HTTP for {filename}...")
        success = download_http(http_url, target_path, expected_bytes, downloaded)

    if success:
        if target_path == partial_path:
            os.rename(partial_path, filename)
        # Clean up resume file on success
        if os.path.exists(resume_path):
            os.remove(resume_path)
        print(f"✓ Completed: {filename} ({file_index}/{total_files})")
        return True
    else:
        print(f"Failed: {filename} ({file_index}/{total_files})")
        return False

def download_http(url, target_path, expected_bytes, downloaded):
    try:
        headers = {'User-Agent': USER_AGENT}
        if downloaded > 0:
            headers['Range'] = f'bytes={downloaded}-'

        with requests.get(url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            mode = 'ab' if downloaded > 0 else 'wb'
            with open(target_path, mode) as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)

        final_size = os.path.getsize(target_path)
        if expected_bytes > 0 and final_size != expected_bytes:
            print(f"  HTTP size mismatch: got {final_size} B, expected {expected_bytes} B")
            return False
        return True

    except Exception as e:
        print(f"HTTP download error: {e}")
        return False

def download_torrent(torrent_url, target_path, expected_bytes, downloaded, expected_filename, resume_path):
    if not LIBTORRENT_AVAILABLE:
        print("libtorrent not available.")
        return False

    save_dir = os.getcwd()
    print(f"  Saving torrent to absolute directory: {save_dir}")

    try:
        r = requests.get(torrent_url, timeout=10)
        r.raise_for_status()
        torrent_file = f"{os.path.basename(target_path)}.torrent"
        torrent_path = os.path.join(save_dir, torrent_file)
        with open(torrent_path, 'wb') as f:
            f.write(r.content)

        ses = lt.session()
        ses.listen_on(6881, 6891)

        info = lt.torrent_info(torrent_path)
        add_params = {
            'ti': info,
            'save_path': save_dir,
            'storage_mode': lt.storage_mode_t(2),
        }

        # Load resume data if exists
        if os.path.exists(resume_path):
            try:
                with open(resume_path, 'rb') as f:
                    resume_data = lt.bdecode(f.read())
                add_params['resume_data'] = resume_data
                print(f"  Loaded resume data from {resume_path}")
            except Exception as e:
                print(f"  Failed to load resume data: {e} — starting fresh")

        h = ses.add_torrent(add_params)

        print(f"Torrent: {info.name()} started (expected: {bytes_to_human(expected_bytes)})")

        last_resume_save = time.time()
        while not h.is_seed():
            s = h.status()
            print(f"\r{expected_filename} - {s.state} | {s.progress*100:.1f}% | "
                  f"{s.download_rate/1024:.1f} KB/s | Peers: {s.num_peers}", end='')
            sys.stdout.flush()

            # Save resume data every 30 seconds
            if time.time() - last_resume_save > RESUME_SAVE_INTERVAL:
                try:
                    resume_data = lt.bencode(h.write_resume_data())
                    with open(resume_path, 'wb') as f:
                        f.write(resume_data)
                    last_resume_save = time.time()
                except:
                    pass

            time.sleep(1)

        print(f"\nTorrent reported completed: {expected_filename}")

        final_path = os.path.join(save_dir, info.name())
        if os.path.exists(final_path):
            on_disk_bytes = os.path.getsize(final_path)
            print(f"  On-disk verification: {bytes_to_human(on_disk_bytes)} (file: {info.name()})")
            
            # 5% tolerance rule: at least 50 MiB, or 5% of expected
            tolerance = max(50 * 1024 * 1024, expected_bytes // 20)
            diff = abs(on_disk_bytes - expected_bytes)
            
            if expected_bytes > 0 and diff <= tolerance:
                if final_path != target_path:
                    os.rename(final_path, target_path)
                    print(f"  Renamed {info.name()} → {expected_filename}")
                print(f"  Size within tolerance ({bytes_to_human(diff)} difference ≤ {bytes_to_human(tolerance)}) → torrent success")
                # Clean up resume file on success
                if os.path.exists(resume_path):
                    os.remove(resume_path)
                return True
            else:
                print(f"  Size mismatch! Got {on_disk_bytes} B, expected {expected_bytes} B (diff {bytes_to_human(diff)} > {bytes_to_human(tolerance)} allowed)")
                return False
        else:
            print("  File not found on disk after completion")
            return False

    except Exception as e:
        print(f"Torrent error: {type(e).__name__}: {str(e)}")
        return False
    finally:
        if 'torrent_path' in locals() and os.path.exists(torrent_path):
            try:
                os.remove(torrent_path)
            except:
                pass

# ==================== SEQUENTIAL DOWNLOAD ====================
def download_list(items):
    total_files = len(items)
    if total_files == 0:
        print("No files to download.")
        return

    total_bytes_expected = sum(item['size_bytes'] for item in items)

    print(f"\nStarting sequential download of {total_files} files "
          f"({bytes_to_human(total_bytes_expected)} total)... (Ctrl+C to pause)\n")

    remaining = items[:]

    with tqdm(
        total=total_bytes_expected,
        desc="Total progress",
        unit='B',
        unit_scale=True,
        unit_divisor=1024,
        disable=not TQDM_AVAILABLE,
        leave=True
    ) as pbar_total:
        completed = 0
        for i, item in enumerate(items, 1):
            success = download_single(item, i, total_files)
            if success:
                completed += 1
                remaining.remove(item)
                save_state(remaining)
                print(f"Files completed: {completed} / {total_files}\n")

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    print(f"\nAll {total_files} files completed!")

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
                        print(f"Removed old version: {local_file}")
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

# ==================== VALIDATED INPUT HELPER ====================
def get_valid_choice(prompt, valid_options):
    valid_lower = {opt.lower(): opt for opt in valid_options}
    while True:
        answer = input(prompt).strip()
        answer_lower = answer.lower()
        if answer_lower in valid_lower:
            return answer_lower
        print(f"Invalid input. Please enter one of: {', '.join(valid_options)}")

# ==================== MAIN MENU ====================
def main():
    print(f"Kiwix English ZIM Tool {VERSION}\n")
    print("1) Generate new best list")
    print("2) Download / resume from existing list")
    print("3) Check for updates (includes new groups + auto-cleanup)")

    choice = get_valid_choice("Choose (1/2/3): ", ["1", "2", "3"])

    if choice == "1":
        print("\nGenerating new best list...")
        all_eng = fetch_directory(BASE_URL)
        if not all_eng:
            print("No English ZIM files found.")
            return

        best = select_best_per_group(all_eng)
        print(f"\nSelected {len(best)} best files:")
        for f in best:
            print(f"  {f['filename']} ({f['size_str']})")

        save_choice = get_valid_choice("\nSave list to file? (y/n): ", ["y", "n"])
        if save_choice == "y":
            best = save_list(best)

        download_choice = get_valid_choice("Download now? (y/n): ", ["y", "n"])
        if download_choice == "y":
            download_list(best)

    elif choice == "2":
        items = load_list()
        if not items:
            print("No list found.")
            return
        download_list(items)

    elif choice == "3":
        to_download, current_best = check_for_updates()
        if not to_download:
            return

        print(f"\nFound {len(to_download)} updates/new groups:")
        total_bytes = sum(f['size_bytes'] for f in to_download)
        for f in to_download:
            print(f"  {f['filename']} ({f['size_str']})")
        print(f"Total size: {bytes_to_human(total_bytes)}")

        save_choice = get_valid_choice("\nSave updated list? (y/n): ", ["y", "n"])
        if save_choice == "y":
            current_best = save_list(current_best)

        download_choice = get_valid_choice("Download updates? (y/n): ", ["y", "n"])
        if download_choice == "y":
            download_list(to_download)
            cleanup_old_versions(to_download)

if __name__ == "__main__":
    main()
