#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiwix English ZIM Selector + Downloader + Update Checker
Version: v20260328g
EXCLUSION RULES:
- Gutenberg: keep ONLY gutenberg_en_all_*
- Wikipedia: keep ONLY wikipedia_en_all_maxi_*
- Wiktionary: keep wiktionary_en_all_nopic_* (only comprehensive English version)
- Drop all other _nopic_*, speedtest_*, wikivoyage_en_europe_*, etc.
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
        def set_postfix(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    tqdm = DummyTqdm

# ==================== CONFIGURATION ====================
VERSION = "v20260328g"
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
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
MAX_CONCURRENT_DOWNLOADS = 4
SPACE_SAFETY_BUFFER_GB = 10
TOTAL_BAR_REFRESH_SECONDS = 0.8   # Smoother updates
SHUTDOWN_GRACE_SECONDS = 2.0

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

stop_event = threading.Event()
active_downloads = {}
progress_lock = threading.Lock()
# FIX 9: Split into two counters so progress position and speed/ETA are
# calculated from independent sources and can't interfere with each other.
total_bytes_on_disk = 0      # seeded from partials at startup + all new bytes written;
                              # drives the progress bar position only
total_bytes_this_session = 0 # always starts at 0; only new bytes from this session;
                              # drives speed and ETA only
download_start_time = 0.0

# ==================== SIZE HANDLING ====================
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

def bytes_to_binary_human(b):
    if b == 0:
        return "0.00 B"
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
    for i, unit in enumerate(units):
        if b < 1024 or i == len(units) - 1:
            if unit == 'B':
                return f"{int(b):.2f} B"
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PiB"

# ==================== EXCLUSION & GROUPING ====================
def should_exclude(filename):
    lower = filename.lower()
    if 'speedtest' in lower: return True
    if lower.startswith('gutenberg_en_lcc-'): return True
    if lower.startswith('wikipedia_en_'):
        if '_all_maxi_' not in lower or '_simple_all_' in lower: return True
        return False
    if lower.startswith('wiktionary_en_all_nopic_'): return False
    if '_nopic_' in lower: return True
    if 'wikivoyage_en_europe' in lower: return True
    if '_simple_all_' in lower and 'wikipedia' not in lower: return True
    return False

def has_all_maxi(filename):
    return '_all_maxi_' in filename.lower()

def has_all_comprehensive(filename):
    lower = filename.lower()
    return '_all_' in lower and '_simple_all_' not in lower and '_nopic_' not in lower

def extract_group_key(fn):
    lower = fn.lower()
    if has_all_maxi(fn) or has_all_comprehensive(fn):
        return re.sub(r'_\d{4}-\d{2}.zim$', '', lower)
    if '_simple_all_' in lower:
        return re.sub(r'_\d{4}-\d{2}.zim$', '', lower)
    base = re.sub(r'_\d{4}-\d{2}(\.zim)?$', '', lower)
    base = re.sub(r'\.zim$', '', base)
    return base.strip()

def get_selection_priority(filename):
    if has_all_maxi(filename): return 300
    if has_all_comprehensive(filename): return 200
    return 100

# ==================== HELPERS ====================
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
    print(f"  Required (files + {SPACE_SAFETY_BUFFER_GB} GB buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
    print(f"  Available on zims: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
    print("  Proceeding may fail or leave the drive full.")
    return get_valid_choice("Proceed anyway? (y/n): ", ["y", "n"]) == "y"

# ==================== CRAWLER & PARSER (unchanged) ====================
def is_english_zim(filename):
    fn = filename.lower()
    return fn.endswith('.zim') and any(re.search(p, fn) for p in ENGLISH_PATTERNS)

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
    for f in all_files:
        if should_exclude(f['filename']):
            print(f"  Excluded unwanted file: {f['filename']}")
            continue
        groups[f['group_key']].append(f)
    selected = []
    for key, files in sorted(groups.items()):
        if not files: continue
        files.sort(key=lambda x: (
            -get_selection_priority(x['filename']),
            -x['size_bytes'],
            -x['date'].timestamp() if x['date'] != datetime.min else 0,
            x['filename'].lower()
        ))
        best = files[0]
        selected.append(best)
        if len(files) > 1:
            print(f"  Group '{key}': selected {best['filename']} ({bytes_to_binary_human(best['size_bytes'])}), discarded other variants")
    return sorted(selected, key=lambda x: x['filename'])

def filter_existing_files(best_list):
    existing = set()
    if os.path.exists(ZIM_SUBFOLDER):
        print(f"Scanning {ZIM_SUBFOLDER}/ for existing ZIM files...")
        for fname in os.listdir(ZIM_SUBFOLDER):
            if fname.endswith('.zim'):
                path = os.path.join(ZIM_SUBFOLDER, fname)
                if os.path.isfile(path):
                    size = os.path.getsize(path)
                    existing.add((fname, size))
                    print(f"  Found existing: {fname} ({bytes_to_binary_human(size)})")
    remaining = []
    skipped_count = 0
    for item in best_list:
        fname = item['filename']
        expected = item['size_bytes']
        tolerance = max(50 * 1024 * 1024, expected // 20)
        found = False
        for (ex_fname, ex_size) in list(existing):
            if ex_fname == fname and abs(ex_size - expected) <= tolerance:
                print(f"  Skipping already complete file (close enough): {fname} ({bytes_to_binary_human(ex_size)} vs {bytes_to_binary_human(expected)})")
                skipped_count += 1
                found = True
                break
        if found:
            continue
        remaining.append(item)
    print(f"\nFiltered: {skipped_count} files already exist (exact or close enough).")
    print(f"Remaining to download: {len(remaining)} files.\n")
    return remaining

def save_list(items, filename=LIST_FILE):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_bytes = sum(f['size_bytes'] for f in items)
    def check_torrent(item):
        torrent_url = item['url'] + '.torrent'
        try:
            r = requests.get(torrent_url, headers={'User-Agent': USER_AGENT}, stream=True, timeout=TORRENT_CHECK_TIMEOUT, allow_redirects=True)
            status = r.status_code
            reason = r.reason
            print(f"  {item['filename']} → {status} {reason} for {torrent_url}")
            return torrent_url if status == 200 else ''
        except Exception as e:
            print(f"  {item['filename']} → error for {torrent_url}: {e}")
            return ''
    print("Checking for torrent files...")
    torrent_urls = [check_torrent(item) for item in items]
    for item, t_url in zip(items, torrent_urls):
        item['torrent_url'] = t_url
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# Kiwix English ZIM best/latest files - {VERSION}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Total files: {len(items)}\n")
        f.write(f"# Total size: {bytes_to_binary_human(total_bytes)}\n")
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

def download_single(item, file_index, total_files, verbose=False):
    if stop_event.is_set():
        return False
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
        tolerance = max(50 * 1024 * 1024, expected_bytes // 20)
        if abs(on_disk - expected_bytes) <= tolerance:
            print(f"✓ Pre-existing file close enough ({bytes_to_binary_human(on_disk)} vs {bytes_to_binary_human(expected_bytes)}) — treating as complete")
            return True
    downloaded = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0
    if os.path.exists(partial_path):
        print(f"↻ Resuming {filename} from partial ({file_index}/{total_files})")
    else:
        print(f"↓ Downloading {filename} ({file_index}/{total_files})")
    success = False
    if torrent_url and LIBTORRENT_AVAILABLE:
        success = download_torrent(torrent_url, partial_path, expected_bytes, downloaded, filename, resume_path, verbose)
        if not success and not stop_event.is_set():
            print(f"  Torrent failed for {filename} — falling back to HTTP")
    if not success and not stop_event.is_set():
        success = download_http(http_url, partial_path, expected_bytes, downloaded, verbose)
    if success:
        # FIX 3: Rename .partial → .zim BEFORE the integrity check.
        # verify_zim_integrity moves failed files into corrupt/, so if we
        # pass it the .partial path and it fails, the good data vanishes.
        # Renaming first also ensures zimcheck sees the correct .zim extension.
        if os.path.exists(partial_path):
            os.rename(partial_path, target_path)
        if verify_zim_integrity(target_path, filename):
            if os.path.exists(resume_path):
                os.remove(resume_path)
            print(f"✓ Completed & verified: {filename} ({file_index}/{total_files})")
            return True
        else:
            print(f"  Integrity verification failed — download discarded")
            return False
    elif not stop_event.is_set():
        print(f"Failed: {filename} ({file_index}/{total_files})")
        return False
    return False

def download_http(original_url, target_path, expected_bytes, downloaded, verbose=False):
    # FIX 4: Use original_url as the immutable base; never mutate it across retries.
    if stop_event.is_set():
        return False
    retries = HTTP_RETRIES
    backoff = [5, 10, 20]
    mirror_index = 0
    global total_bytes_on_disk, total_bytes_this_session
    for attempt in range(retries):
        if stop_event.is_set():
            return False
        # FIX 4: Substitute mirror into the original URL each time, not the
        # already-substituted URL from the previous attempt.
        mirror_base = MIRRORS[mirror_index % len(MIRRORS)]
        url = original_url.replace("https://download.kiwix.org/zim/", mirror_base)
        mirror_index += 1
        try:
            headers = {'User-Agent': USER_AGENT}
            if downloaded > 0:
                headers['Range'] = f'bytes={downloaded}-'
            # FIX 1: Determine mode here, inside the loop, so a 416 reset of
            # `downloaded` is respected before we open the file.
            mode = 'ab' if downloaded > 0 else 'wb'
            # FIX 2: Split timeout into (connect, read). The read timeout only
            # applies between chunks, so 300 s gives slow mirrors plenty of
            # headroom without hanging forever on a dead connection.
            with requests.get(url, headers=headers, stream=True,
                              timeout=(30, 300)) as r:
                r.raise_for_status()
                written = 0
                with open(target_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if stop_event.is_set():
                            return False
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            # FIX 9: HTTP bytes are always fresh network bytes,
                            # so increment both counters.
                            total_bytes_on_disk += len(chunk)
                            total_bytes_this_session += len(chunk)
                            if verbose:
                                print(f"\r  HTTP written: {bytes_to_binary_human(written)}", end='')
                    if verbose:
                        print()
            final_size = os.path.getsize(target_path)
            tolerance = max(1024 * 1024, expected_bytes // 200)
            if expected_bytes > 0 and abs(final_size - expected_bytes) <= tolerance:
                return True
            else:
                return False
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 416:
                # FIX 1: Reset downloaded so the next attempt opens with 'wb'
                # and starts fresh, not 'ab' on a deleted file.
                downloaded = 0
                if os.path.exists(target_path):
                    os.remove(target_path)
                continue
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except Exception:
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
    return False

def download_torrent(torrent_url, target_path, expected_bytes, downloaded, expected_filename, resume_path, verbose=False):
    if not LIBTORRENT_AVAILABLE or stop_event.is_set():
        return False
    save_dir = os.getcwd()
    global total_bytes_on_disk, total_bytes_this_session
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
            except:
                pass
        # Human-readable labels for libtorrent state strings.
        STATE_LABELS = {
            'checking_files':       'Verifying existing data',
            'checking_resume_data': 'Checking resume data',
            'downloading_metadata': 'Fetching metadata',
            'downloading':          'Downloading',
            'finished':             'Finished',
            'seeding':              'Seeding',
            'allocating':           'Allocating',
        }
        CHECKING_STATES = {'checking_files', 'checking_resume_data',
                           'downloading_metadata', 'allocating'}

        h = ses.add_torrent(add_params)
        last_reported = 0
        while not h.is_seed():
            if stop_event.is_set():
                return False
            s = h.status()
            state_str = str(s.state)
            downloaded_this_tick = int(s.total_wanted_done - last_reported)
            if downloaded_this_tick > 0:
                last_reported = s.total_wanted_done
                # FIX 9: Always update the on-disk counter (drives progress
                # position) but only update the session counter when libtorrent
                # is genuinely pulling from the network (drives speed/ETA).
                # During checking phases total_wanted_done advances via disk
                # I/O, not network I/O, so crediting it to the session counter
                # would make speed read as hundreds of MB/s.
                total_bytes_on_disk += downloaded_this_tick
                if state_str not in CHECKING_STATES:
                    total_bytes_this_session += downloaded_this_tick
            with progress_lock:
                active_downloads[expected_filename] = {
                    'progress': s.progress * 100,
                    'rate': s.download_rate / 1024,
                    'peers': s.num_peers
                }
            if verbose:
                label = STATE_LABELS.get(state_str, state_str)
                if state_str in CHECKING_STATES:
                    # During verification phases, rate and peer count are
                    # meaningless (always 0) -- omit them to avoid confusion.
                    print(f"{expected_filename} - {label} | {s.progress*100:.1f}%")
                else:
                    print(f"{expected_filename} - {label} | {s.progress*100:.1f}%"
                          f" | {s.download_rate/1024:.1f} KB/s | Peers: {s.num_peers}")
            time.sleep(1)
        final_path = os.path.join(save_dir, info.name())
        if os.path.exists(final_path):
            on_disk_bytes = os.path.getsize(final_path)
            tolerance = max(50 * 1024 * 1024, expected_bytes // 20)
            diff = abs(on_disk_bytes - expected_bytes)
            if expected_bytes > 0 and diff <= tolerance:
                if final_path != target_path:
                    os.rename(final_path, target_path)
                if os.path.exists(resume_path):
                    os.remove(resume_path)
                return True
        return False
    except Exception as e:
        if not stop_event.is_set():
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

# ==================== LIVE TOTAL BAR WITH RELIABLE ETA ====================
def total_bar_refresh_thread(pbar, total_expected, stop_event, verbose):
    global total_bytes_on_disk, total_bytes_this_session, download_start_time
    last_print = 0
    while not stop_event.is_set():
        now = time.time()
        elapsed = now - download_start_time if download_start_time > 0 else 0.0

        # Progress position: total_bytes_on_disk includes the partial baseline
        # so the bar starts at the correct position on resume.
        pbar.n = min(total_bytes_on_disk, total_expected)

        if elapsed > 3.0:
            # FIX 10: Speed derived solely from total_bytes_this_session so it
            # reflects actual network throughput for this session only -- the
            # partial-file baseline is excluded entirely.
            speed = total_bytes_this_session / elapsed
            speed_str = f"{bytes_to_binary_human(int(speed))}/s"
            remaining = total_expected - total_bytes_on_disk
            eta_sec = remaining / speed if speed > 0 else 0
            eta_str = f"{int(eta_sec//3600):02d}:{int((eta_sec%3600)//60):02d}:{int(eta_sec%60):02d}"
            pbar.set_postfix_str(f"{speed_str}, ETA={eta_str}")
        else:
            pbar.set_postfix_str("ETA=?:??:??")

        pbar.refresh()

        if verbose and now - last_print > TOTAL_BAR_REFRESH_SECONDS:
            print(pbar)
            last_print = now

        time.sleep(0.4)  # Very smooth updates

def unified_progress_thread(stop_event, verbose):
    last_update = time.time()
    while not stop_event.is_set():
        if not verbose and time.time() - last_update >= 5:
            with progress_lock:
                if active_downloads:
                    parts = [f"{name[:25]} {data['progress']:.1f}% · {data['rate']:.1f} KB/s" 
                             for name, data in list(active_downloads.items())]
                    line = "Progress: " + " | ".join(parts)
                    print(f"\r{line:<140}", end='', flush=True)
            last_update = time.time()
        time.sleep(0.5)

def download_list(items, verbose=False):
    global active_downloads, total_bytes_on_disk, total_bytes_this_session, download_start_time
    active_downloads.clear()
    download_start_time = time.time()

    # FIX 9: Seed total_bytes_on_disk from partial files so the progress bar
    # starts at the correct position on resume. total_bytes_this_session is
    # always left at 0 so speed and ETA reflect only current network throughput.
    already_on_disk = 0
    for item in items:
        partial = os.path.join(ZIM_SUBFOLDER, f"{item['filename']}.partial")
        if os.path.exists(partial):
            already_on_disk += os.path.getsize(partial)
    total_bytes_on_disk = already_on_disk
    total_bytes_this_session = 0
    if already_on_disk > 0:
        print(f"Resuming: {bytes_to_binary_human(already_on_disk)} already on disk across partial files.")

    total_files = len(items)
    if total_files == 0:
        print("No files to download.")
        return

    total_bytes_expected = sum(item['size_bytes'] for item in items)

    print(f"\nStarting parallel download of {total_files} files "
          f"({bytes_to_binary_human(total_bytes_expected)} total, max {MAX_CONCURRENT_DOWNLOADS} concurrent)... (Ctrl+C to pause)\n")

    stop_event.clear()

    # FIX 11: Remove {remaining} from bar_format. tqdm computes {remaining} as
    # (total - n) / internal_rate; since internal_rate is derived from pbar.n
    # which includes the partial-file baseline, {remaining} is just as deflated
    # as the rate was. Our session-only ETA in the postfix is the only remaining
    # time estimate shown -- {elapsed} is kept as it is always honest.
    BAR_FMT = (
        "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
        "[{elapsed}{postfix}]"
    )
    pbar_total = tqdm(total=total_bytes_expected, desc="Total progress", unit='B',
                      unit_scale=True, unit_divisor=1024, disable=not TQDM_AVAILABLE,
                      leave=True, bar_format=BAR_FMT)

    refresh_thread = threading.Thread(target=total_bar_refresh_thread,
                                    args=(pbar_total, total_bytes_expected, stop_event, verbose),
                                    daemon=True)
    refresh_thread.start()

    if not verbose:
        progress_thread = threading.Thread(target=unified_progress_thread, args=(stop_event, verbose), daemon=True)
        progress_thread.start()

    try:
        def wrapped_download(item, idx):
            success = download_single(item, idx, total_files, verbose=verbose)
            if success:
                global total_bytes_on_disk
                with progress_lock:
                    # Clamp to expected so a completed file doesn't push the
                    # bar past 100% due to tolerance rounding.
                    total_bytes_on_disk = max(total_bytes_on_disk, total_bytes_expected)

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
            futures = [executor.submit(wrapped_download, item, i) for i, item in enumerate(items, 1)]
            for future in as_completed(futures):
                future.result()

    except KeyboardInterrupt:
        print("\n\nCtrl+C detected — shutting down gracefully...")
        stop_event.set()

    finally:
        stop_event.set()
        time.sleep(SHUTDOWN_GRACE_SECONDS)
        pbar_total.close()

        stats = get_summary_stats()
        print("\nPost-run summary:")
        print(f"  - Total files in list: {total_files}")
        print(f"  - Good / verified files: {stats['good_count']}")
        print(f"  - Corrupt files detected: {stats['corrupt_count']}")
        print(f"  - Total size on disk (good files): {stats['good_size']}")
        print(f"  - Corrupt files size: {stats['corrupt_size']}")
        print(f"  - Disk usage in ./zims/: {stats['total_folder_size']}")
        print(f"\nDownload session ended.")

def get_summary_stats():
    good_count = good_size = corrupt_count = corrupt_size = 0
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
        'good_size': bytes_to_binary_human(good_size),
        'corrupt_count': corrupt_count,
        'corrupt_size': bytes_to_binary_human(corrupt_size),
        'total_folder_size': bytes_to_binary_human(total_folder_size)
    }

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

def get_valid_choice(prompt, valid_options):
    valid_lower = {opt.lower(): opt for opt in valid_options}
    while True:
        answer = input(prompt).strip()
        answer_lower = answer.lower()
        if answer_lower in valid_lower:
            return answer_lower
        print(f"Invalid input. Please enter one of: {', '.join(valid_options)}")

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
        print("\nChecking for already downloaded files...")
        best = filter_existing_files(best)
        if not best:
            print("All best files are already present (or close enough) in the zims/ directory.")
            stats = get_summary_stats()
            print("\nCurrent disk summary:")
            print(f"  - Good / verified files: {stats['good_count']}")
            print(f"  - Total size on disk (good files): {stats['good_size']}")
            return
        total_size = sum(f['size_bytes'] for f in best)
        print(f"\nSelected {len(best)} best files that still need downloading ({bytes_to_binary_human(total_size)}):")
        for f in best:
            print(f"  {f['filename']} ({bytes_to_binary_human(f['size_bytes'])})")
        print(f"\nTotal to download: {len(best)} files ({bytes_to_binary_human(total_size)})")
        save_choice = get_valid_choice("\nSave list to file? (y/n): ", ["y", "n"])
        if save_choice == "y":
            best = save_list(best)
        required_gb = get_required_space_gb(best)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check for new download:")
        print(f"  Required (incl. buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
        print(f"  Available: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
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
        print(f"  Required (incl. buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
        print(f"  Available: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Resume cancelled due to insufficient space.")
                return
        verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
        verbose = (verbose_choice == "y")
        download_list(items_to_download, verbose=verbose)
    elif choice == "3":
        to_download, current_best = check_for_updates()
        if not to_download:
            return
        total_bytes = sum(f['size_bytes'] for f in to_download)
        print(f"\nFound {len(to_download)} updates/new groups ({bytes_to_binary_human(total_bytes)}):")
        for f in to_download:
            print(f"  {f['filename']} ({bytes_to_binary_human(f['size_bytes'])})")
        print(f"Total size: {bytes_to_binary_human(total_bytes)}")
        required_gb = get_required_space_gb(to_download)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check for updates:")
        print(f"  Required (incl. buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
        print(f"  Available: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
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

# ==================== VERSIONING INFORMATION (END OF SCRIPT) ====================
# Kiwix English ZIM Tool
# Version: v20260328g
# Last updated: 2026-03-28
#
# Changes in this version (fix double comma in Total progress bar):
#
# FIX 12 -- Removed the explicit ", " between {elapsed} and {postfix} in
#   bar_format. tqdm automatically prepends ", " before the postfix string,
#   so having ", " in bar_format as well produced a double comma
#   (e.g. "[25:04, , 2.45 MiB/s, ETA=37:30:20]"). Now reads cleanly as
#   "[25:04, 2.45 MiB/s, ETA=37:30:20]".
#
# Earlier fixes carried forward:
#   FIX 1  -- download_http mode ('ab'/'wb') determined inside retry loop
#   FIX 2  -- timeout changed to (30, 300) to survive slow large-file transfers
#   FIX 3  -- .partial renamed to .zim before integrity check
#   FIX 4  -- mirror URL substitution always starts from original URL
#   FIX 5  -- libtorrent state strings mapped to human-readable labels
#   FIX 6  -- KB/s and Peers suppressed during verification phases
#   FIX 9  -- split total_bytes_downloaded into on_disk (position) and
#             this_session (speed/ETA) counters
#   FIX 10 -- tqdm {rate} field suppressed; session-only rate injected via postfix
#   FIX 11 -- tqdm {remaining} field removed; ETA= postfix is sole time estimate

if __name__ == "__main__":
    main()
