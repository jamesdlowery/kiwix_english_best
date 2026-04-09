#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiwix English ZIM Selector + Downloader + Update Checker
Version: v20260325b   # FIXED: single space warning prompt (no double-prompt on resume)
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
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import shutil
import threading

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
VERSION = "v20260325b"
BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Downloader/{VERSION}"
DELAY = 0.8
MAX_DEPTH = 4
LIST_FILE = "kiwix_english_best.txt"
STATE_FILE = "kiwix_download_state.json"
ZIM_SUBFOLDER = "zims"
CORRUPT_SUBFOLDER = os.path.join(ZIM_SUBFOLDER, "corrupt")
CHUNK_SIZE = 1024 * 1024
TORRENT_CHECK_TIMEOUT = 10
HTTP_TIMEOUT = 120
HTTP_RETRIES = 3
RESUME_SAVE_INTERVAL = 30
MAX_CONCURRENT_DOWNLOADS = 4
SPACE_SAFETY_BUFFER_GB = 10
PROGRESS_UPDATE_INTERVAL = 5

MIRRORS = [
    "https://download.kiwix.org/zim/",
    "https://ny.mirror.driftle.ss/kiwix/zim/",
    "https://de.mirror.driftle.ss/kiwix/zim/",
    "https://fr.mirror.driftle.ss/kiwix/zim/",
    "https://uk.mirror.driftle.ss/kiwix/zim/",
    "https://us.mirror.driftle.ss/kiwix/zim/"
]

ENGLISH_PATTERNS = [
    r'_en_', r'en_all', r'wikipedia_en_', r'wiktionary_en_', r'wikibooks_en_',
    r'wikivoyage_en_', r'wikiquote_en_', r'wikisource_en_', r'en_stackexchange',
    r'_english', r'en_simple', r'stackoverflow_en', r'english\.stackexchange'
]

UPDATE_SIZE_THRESHOLD = 1.05

zimcheck_warning_shown = False

# Shared progress state for unified line (quiet mode)
active_downloads = {}
progress_lock = threading.Lock()

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
    base = re.sub(r'\.zim$', '', base).lower()
    variants = r'(_maxi|_nopic|_nopics|_minimal|_min|_lite|_noimages|_nopictures|_lowres|_noimg)?'
    base = re.sub(variants + r'$', '', base, flags=re.I)
    return base.strip()

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

def get_free_space_gb(path):
    stat = os.statvfs(path)
    free_bytes = stat.f_bavail * stat.f_frsize
    return free_bytes / (1024 ** 3)

def get_required_space_gb(items_to_download):
    total_bytes = sum(item['size_bytes'] for item in items_to_download)
    total_gb = total_bytes / (1024 ** 3)
    buffer_gb = max(SPACE_SAFETY_BUFFER_GB, total_gb * 0.1)
    return total_gb + buffer_gb

def confirm_proceed(required_gb, available_gb):
    print(f"\nWARNING: Insufficient free space detected!")
    print(f"  Required (files + {SPACE_SAFETY_BUFFER_GB} GB buffer): ~{required_gb:.1f} GB")
    print(f"  Available on {ZIM_SUBFOLDER}: ~{available_gb:.1f} GB")
    print("  Proceeding may fail or leave the drive full.")
    return get_valid_choice("Proceed anyway? (y/n): ", ["y", "n"]) == "y"

def get_summary_stats():
    good_count = 0
    good_size = 0
    corrupt_count = 0
    corrupt_size = 0

    for file in os.listdir(ZIM_SUBFOLDER):
        if file.endswith('.zim'):
            good_count += 1
            good_size += os.path.getsize(os.path.join(ZIM_SUBFOLDER, file))

    if os.path.exists(CORRUPT_SUBFOLDER):
        for file in os.listdir(CORRUPT_SUBFOLDER):
            if file.endswith('.zim'):
                corrupt_count += 1
                corrupt_size += os.path.getsize(os.path.join(CORRUPT_SUBFOLDER, file))

    total_folder_size = good_size + corrupt_size

    return {
        'good_count': good_count,
        'good_size': bytes_to_human(good_size),
        'corrupt_count': corrupt_count,
        'corrupt_size': bytes_to_human(corrupt_size),
        'total_folder_size': bytes_to_human(total_folder_size)
    }

# ==================== CRAWLER ====================
def fetch_directory(url, depth=0, visited=None, collected=None):
    if collected is None: collected = []
    if visited is None: visited = set()
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
            if not parsed: continue

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
    VARIANT_SUFFIXES = [r'_maxi', r'_nopic', r'_nopics', r'_minimal', r'_min', r'_lite',
                        r'_noimages', r'_nopictures', r'_lowres', r'_noimg']

    for f in all_files:
        fn = f['filename'].lower()
        base = re.sub(r'_\d{4}-\d{2}(\.zim)?$', '', fn, flags=re.I)
        for suffix in VARIANT_SUFFIXES:
            base = re.sub(suffix + r'(\.zim)?$', '', base, flags=re.I)
        group_key = re.sub(r'\.zim$', '', base).strip()
        groups[group_key].append(f)

    selected = []
    for key, files in sorted(groups.items()):
        if not files: continue

        files.sort(key=lambda x: (
            -x['size_bytes'],
            -x['date'].timestamp() if x['date'] != datetime.min else 0,
            x['filename'].lower()
        ))

        best = files[0]
        selected.append(best)

        if len(files) > 1:
            discarded = [f['filename'] for f in files[1:]]
            print(f"  Group '{key}': selected {best['filename']} ({bytes_to_human(best['size_bytes'])}), "
                  f"discarded variants: {', '.join(discarded)}")

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
    torrent_urls = [check_torrent(item) for item in items]

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
            if line.startswith('#') or not line: continue
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
    if not os.path.exists(STATE_FILE): return None
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

# ==================== INTEGRITY CHECK ====================
def verify_zim_integrity(target_path, filename):
    global zimcheck_warning_shown
    base_name = filename
    if base_name.endswith('.partial'):
        base_name = base_name[:-8]

    try:
        result = subprocess.run(['zimcheck', '-C', target_path], capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            print(f"  ZIM integrity check passed for {filename}")
            return True
        else:
            print(f"  ZIM integrity check FAILED for {filename}:")
            print(result.stderr.strip())
            corrupt_dir = os.path.join(ZIM_SUBFOLDER, "corrupt")
            os.makedirs(corrupt_dir, exist_ok=True)
            corrupt_path = os.path.join(corrupt_dir, base_name)
            shutil.move(target_path, corrupt_path)
            suffix_note = " (suffix removed)" if filename != base_name else ""
            print(f"  Moved corrupt file to {corrupt_path}{suffix_note}")
            return False
    except FileNotFoundError:
        if not zimcheck_warning_shown:
            print("  WARNING: 'zimcheck' not found — skipping ZIM integrity check")
            print("  To enable full verification, install: sudo apt install zim-tools")
            zimcheck_warning_shown = True
        try:
            with open(target_path, 'rb') as f:
                if f.read(4) == b'\x04\x00\x00\x00':
                    print(f"  Basic ZIM header check passed for {filename} (zimcheck not installed)")
                    return True
            return False
        except Exception as e:
            print(f"  Basic header check failed for {filename}: {e}")
            return False
    except Exception as e:
        print(f"  Integrity check error for {filename}: {e}")
        return False

# ==================== DOWNLOAD SINGLE FILE ====================
def download_single(item, file_index, total_files, verbose=False):
    filename = item['filename']
    expected_bytes = item['size_bytes']
    http_url = item['url']
    torrent_url = item.get('torrent_url', '')

    os.makedirs(ZIM_SUBFOLDER, exist_ok=True)
    target_path = os.path.join(ZIM_SUBFOLDER, filename)
    partial_path = os.path.join(ZIM_SUBFOLDER, f"{filename}.partial")
    resume_path = os.path.join(ZIM_SUBFOLDER, f"{filename}.fastresume")

    if os.path.exists(target_path):
        on_disk = os.path.getsize(target_path)
        if on_disk == expected_bytes:
            print(f"✓ Already complete: {filename} ({file_index}/{total_files})")
            return True
        elif abs(on_disk - expected_bytes) <= max(50 * 1024 * 1024, expected_bytes // 20):
            print(f"✓ Pre-existing file close enough ({bytes_to_human(on_disk)} vs {bytes_to_human(expected_bytes)}) — treating as complete")
            return True

    downloaded = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0

    if os.path.exists(partial_path):
        print(f"↻ Resuming {filename} from partial ({file_index}/{total_files})")
    else:
        print(f"↓ Downloading {filename} ({file_index}/{total_files})")

    success = False

    if torrent_url and LIBTORRENT_AVAILABLE:
        if verbose:
            print(f"  Trying torrent for {filename} (url: {torrent_url})...")
        success = download_torrent(torrent_url, partial_path, expected_bytes, downloaded, filename, resume_path, verbose)
        if not success:
            print(f"  Torrent failed for {filename} — falling back to HTTP")
    else:
        if not torrent_url and verbose:
            print(f"  No torrent available for {filename}")

    if not success:
        print(f"  Using HTTP for {filename}...")
        success = download_http(http_url, partial_path, expected_bytes, downloaded, verbose)

    if success:
        check_path = partial_path if os.path.exists(partial_path) else target_path
        if verify_zim_integrity(check_path, filename):
            if os.path.exists(partial_path):
                os.rename(partial_path, target_path)
            if os.path.exists(resume_path):
                os.remove(resume_path)
            print(f"✓ Completed & verified: {filename} ({file_index}/{total_files})")
            return True
        else:
            print(f"  Integrity verification failed — download discarded")
            return False
    else:
        print(f"Failed: {filename} ({file_index}/{total_files})")
        return False

def download_http(url, target_path, expected_bytes, downloaded, verbose=False):
    retries = HTTP_RETRIES
    backoff = [5, 10, 20]
    mirror_index = 0

    for attempt in range(retries):
        mirror_base = MIRRORS[mirror_index % len(MIRRORS)]
        url = url.replace("https://download.kiwix.org/zim/", mirror_base)
        mirror_index += 1

        try:
            headers = {'User-Agent': USER_AGENT}
            if downloaded > 0:
                headers['Range'] = f'bytes={downloaded}-'

            if verbose:
                print(f"  HTTP attempt {attempt+1}/{retries} (mirror: {mirror_base}) from {url}")

            with requests.get(url, headers=headers, stream=True, timeout=HTTP_TIMEOUT) as r:
                r.raise_for_status()
                mode = 'ab' if downloaded > 0 else 'wb'
                written = 0
                with open(target_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            if verbose:
                                print(f"\r  HTTP written: {bytes_to_human(written)}", end='')
                    if verbose:
                        print()

            final_size = os.path.getsize(target_path)
            tolerance = max(1024 * 1024, expected_bytes // 200)
            if expected_bytes > 0 and abs(final_size - expected_bytes) <= tolerance:
                print(f"  HTTP success (size within tolerance: diff {bytes_to_human(abs(final_size - expected_bytes))})")
                return True
            else:
                print(f"  HTTP size mismatch: got {final_size} B, expected {expected_bytes} B")
                return False

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 416:
                print("  416 Range Not Satisfiable — retrying full download (no range)")
                downloaded = 0
                if os.path.exists(target_path):
                    os.remove(target_path)
                continue
            print(f"  HTTP error on mirror {mirror_base}: {e}")
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except requests.exceptions.Timeout:
            print(f"  HTTP timeout on mirror {mirror_base} (attempt {attempt+1}/{retries})")
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except Exception as e:
            print(f"  HTTP download error on mirror {mirror_base}: {e}")
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
    print("  HTTP failed after all mirrors/retries")
    return False

def download_torrent(torrent_url, target_path, expected_bytes, downloaded, expected_filename, resume_path, verbose=False):
    if not LIBTORRENT_AVAILABLE:
        print("libtorrent not available.")
        return False

    save_dir = os.getcwd()
    if verbose:
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
        add_params = {'ti': info, 'save_path': save_dir, 'storage_mode': lt.storage_mode_t(2)}

        if os.path.exists(resume_path):
            try:
                with open(resume_path, 'rb') as f:
                    resume_data = lt.bdecode(f.read())
                add_params['resume_data'] = resume_data
                if verbose:
                    print(f"  Loaded resume data from {resume_path}")
            except Exception as e:
                if verbose:
                    print(f"  Failed to load resume data: {e} — starting fresh")

        h = ses.add_torrent(add_params)

        if verbose:
            print(f"Torrent: {info.name()} started (expected: {bytes_to_human(expected_bytes)})")

        last_resume_save = time.time()
        last_print = time.time()
        while not h.is_seed():
            s = h.status()

            with progress_lock:
                active_downloads[expected_filename] = {
                    'progress': s.progress * 100,
                    'rate': s.download_rate / 1024,
                    'peers': s.num_peers
                }

            if verbose or time.time() - last_print > PROGRESS_UPDATE_INTERVAL:
                print(f"\rTorrent progress: {expected_filename} {s.progress*100:.1f}% | {s.download_rate/1024:.1f} KB/s | Peers: {s.num_peers}", end='')
                last_print = time.time()

            time.sleep(1)

        print(f"\nTorrent reported completed: {expected_filename}")

        final_path = os.path.join(save_dir, info.name())
        if os.path.exists(final_path):
            on_disk_bytes = os.path.getsize(final_path)
            print(f"  On-disk verification: {bytes_to_human(on_disk_bytes)} (file: {info.name()})")
            tolerance = max(50 * 1024 * 1024, expected_bytes // 20)
            diff = abs(on_disk_bytes - expected_bytes)
            if expected_bytes > 0 and diff <= tolerance:
                if final_path != target_path:
                    os.rename(final_path, target_path)
                    if verbose:
                        print(f"  Renamed {info.name()} → {expected_filename}")
                if verbose:
                    print(f"  Size within tolerance ({bytes_to_human(diff)} difference ≤ {bytes_to_human(tolerance)}) → torrent success")
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
        with progress_lock:
            active_downloads.pop(expected_filename, None)
        if 'torrent_path' in locals() and os.path.exists(torrent_path):
            try:
                os.remove(torrent_path)
            except:
                pass

# ==================== UNIFIED PROGRESS THREAD (quiet mode) ====================
def unified_progress_thread(stop_event, verbose):
    last_update = time.time()
    while not stop_event.is_set():
        if not verbose and time.time() - last_update >= PROGRESS_UPDATE_INTERVAL:
            with progress_lock:
                if active_downloads:
                    parts = [f"{name[:25]} {data['progress']:.1f}% · {data['rate']:.1f} KB/s" 
                             for name, data in list(active_downloads.items())]
                    line = "Progress: " + " | ".join(parts)
                    print(f"\r{line:<140}", end='', flush=True)
            last_update = time.time()
        time.sleep(0.5)

# ==================== PARALLEL DOWNLOAD ====================
def download_list(items, verbose=False):
    global active_downloads
    active_downloads.clear()

    total_files = len(items)
    if total_files == 0:
        print("No files to download.")
        return

    total_bytes_expected = sum(item['size_bytes'] for item in items)

    print(f"\nStarting parallel download of {total_files} files "
          f"({bytes_to_human(total_bytes_expected)} total, max {MAX_CONCURRENT_DOWNLOADS} concurrent)... (Ctrl+C to pause)\n")

    stop_event = threading.Event()
    if not verbose:
        progress_thread = threading.Thread(target=unified_progress_thread, args=(stop_event, verbose), daemon=True)
        progress_thread.start()

    remaining = items[:]
    completed = 0

    with tqdm(total=total_bytes_expected, desc="Total progress", unit='B', unit_scale=True, unit_divisor=1024,
              disable=not TQDM_AVAILABLE, leave=True) as pbar_total:

        def wrapped_download(item, idx):
            nonlocal completed
            success = download_single(item, idx, total_files, verbose=verbose)
            if success:
                completed += 1
                if item in remaining:
                    remaining.remove(item)
                save_state(remaining)
                pbar_total.update(item['size_bytes'])
                print(f"Files completed: {completed} / {total_files}\n")

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
            futures = [executor.submit(wrapped_download, item, i) for i, item in enumerate(items, 1)]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"Exception in download thread: {e}")

    if not verbose:
        stop_event.set()

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    stats = get_summary_stats()
    print("\nPost-run summary:")
    print(f"  - Total files in list: {total_files}")
    print(f"  - Good / verified files: {stats['good_count']}")
    print(f"  - Corrupt files detected: {stats['corrupt_count']}")
    print(f"  - Total size on disk (good files): {stats['good_size']}")
    print(f"  - Corrupt files size: {stats['corrupt_size']}")
    print(f"  - Disk usage in ./zims/: {stats['total_folder_size']}")

    print(f"\nAll {total_files} files completed!")

# ==================== CLEANUP ====================
def cleanup_old_versions(downloaded_items):
    print("\nCleaning up older versions...")
    for item in downloaded_items:
        group = item['group_key']
        new_date = parse_date_from_filename(item['filename'])
        for local_file in list(os.listdir(ZIM_SUBFOLDER)):
            if not local_file.endswith('.zim'): continue
            if extract_group_key(local_file) == group and local_file != item['filename']:
                old_date = parse_date_from_filename(local_file)
                if old_date < new_date:
                    try:
                        os.remove(os.path.join(ZIM_SUBFOLDER, local_file))
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
        if key not in current_dict: continue
        curr = current_dict[key]
        old_date = old.get('date', parse_date_from_filename(old['filename']))
        curr_date = curr['date']
        is_newer = (curr_date > old_date) or (curr['size_bytes'] > old['size_bytes'] * UPDATE_SIZE_THRESHOLD)
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

        required_gb = get_required_space_gb(best)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check for new download:")
        print(f"  Required (incl. buffer): ~{required_gb:.1f} GB")
        print(f"  Available: ~{available_gb:.1f} GB")
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Download cancelled due to insufficient space.")
                return

        verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
        verbose = (verbose_choice == "y")

        download_choice = get_valid_choice("Download now? (y/n): ", ["y", "n"])
        if download_choice == "y":
            download_list(best, verbose=verbose)

    elif choice == "2":
        items = load_list()
        if not items:
            print("No list found.")
            return

        items_to_download = [item for item in items 
                             if not os.path.exists(os.path.join(ZIM_SUBFOLDER, item['filename'])) 
                             or os.path.getsize(os.path.join(ZIM_SUBFOLDER, item['filename'])) != item['size_bytes']]

        if not items_to_download:
            print("All files already complete — nothing to resume.")
            stats = get_summary_stats()
            print("\nPost-run summary (no new downloads):")
            print(f"  - Total files in list: {len(items)}")
            print(f"  - Good / verified files: {stats['good_count']}")
            print(f"  - Corrupt files detected: {stats['corrupt_count']}")
            print(f"  - Total size on disk (good files): {stats['good_size']}")
            print(f"  - Corrupt files size: {stats['corrupt_size']}")
            print(f"  - Disk usage in ./zims/: {stats['total_folder_size']}")
            return

        required_gb = get_required_space_gb(items_to_download)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check for resume:")
        print(f"  Files to download: {len(items_to_download)}")
        print(f"  Required (incl. buffer): ~{required_gb:.1f} GB")
        print(f"  Available: ~{available_gb:.1f} GB")
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Resume cancelled due to insufficient space.")
                return

        verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
        verbose = (verbose_choice == "y")

        download_list(items, verbose=verbose)

    elif choice == "3":
        to_download, current_best = check_for_updates()
        if not to_download:
            return

        print(f"\nFound {len(to_download)} updates/new groups:")
        total_bytes = sum(f['size_bytes'] for f in to_download)
        for f in to_download:
            print(f"  {f['filename']} ({f['size_str']})")
        print(f"Total size: {bytes_to_human(total_bytes)}")

        required_gb = get_required_space_gb(to_download)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check for updates:")
        print(f"  Required (incl. buffer): ~{required_gb:.1f} GB")
        print(f"  Available: ~{available_gb:.1f} GB")
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Update download cancelled due to insufficient space.")
                return

        save_choice = get_valid_choice("\nSave updated list? (y/n): ", ["y", "n"])
        if save_choice == "y":
            current_best = save_list(current_best)

        verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
        verbose = (verbose_choice == "y")

        download_choice = get_valid_choice("Download updates? (y/n): ", ["y", "n"])
        if download_choice == "y":
            download_list(to_download, verbose=verbose)
            cleanup_old_versions(to_download)

if __name__ == "__main__":
    main()
