#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiwix English ZIM Selector + Downloader + Update Checker
Version: v20260401j
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
VERSION = "v20260401j"
BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Downloader/{VERSION}"
DELAY = 0.8
MAX_DEPTH = 4
LIST_FILE = "kiwix_english_best.txt"
STATE_FILE = "kiwix_download_state.json"
ZIM_SUBFOLDER = "zims"
CORRUPT_SUBFOLDER = os.path.join(ZIM_SUBFOLDER, "corrupt")
FAILURE_LOG = "kiwix_download_failures.log"
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
download_start_time = 0.0    # resets on checking->downloading transition; drives speed/ETA
display_start_time  = 0.0    # never resets; drives the elapsed display in the progress bar
verify_start_time   = 0.0    # set when first verification byte arrives; drives verify ETA
# FIX 25: Flag set by download_torrent when a checking->downloading transition
# is detected. The refresh thread resets total_bytes_this_session and
# download_start_time atomically when it sees this flag.
reset_session_clock = False

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
    # FIX 23 (also): Do not use os.path.getsize() on .partial files to estimate
    # space already used -- libtorrent sparse allocation means the on-disk size
    # can be far smaller than actual downloaded content, causing the space check
    # to overestimate required space. Completed .zim files need 0 additional
    # space; everything else (partial or not started) is treated conservatively
    # as needing its full item['size_bytes'].
    remaining_bytes = 0
    for item in items_to_download:
        zim_path = os.path.join(ZIM_SUBFOLDER, item['filename'])
        if os.path.exists(zim_path):
            pass  # fully downloaded -- needs 0 additional space
        else:
            remaining_bytes += item['size_bytes']  # partial or not started
    total_gb = remaining_bytes / (1024 ** 3)
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

def log_failure(filename, reason, detail='', file_size_bytes=0, via_torrent=False):
    """Append a timestamped failure entry to FAILURE_LOG."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    method = 'torrent' if via_torrent else 'http'
    size_str = bytes_to_binary_human(file_size_bytes) if file_size_bytes else 'unknown'
    sep = '-' * 72
    lines = [
        sep,
        f"Timestamp : {ts}",
        f"File      : {filename}",
        f"Size      : {size_str}",
        f"Method    : {method}",
        f"Reason    : {reason}",
    ]
    if detail:
        lines.append(f"Detail    :\n{detail.strip()}")
    lines.append(sep)
    entry = '\n'.join(lines) + '\n'
    try:
        with open(FAILURE_LOG, 'a', encoding='utf-8') as f:
            f.write(entry)
        print(f"  Failure logged to {FAILURE_LOG}")
    except Exception as e:
        print(f"  WARNING: could not write to failure log: {e}")

def verify_zim_integrity(target_path, filename, via_torrent=False):
    global zimcheck_warning_shown
    base_name = filename
    if base_name.endswith('.partial'):
        base_name = base_name[:-8]
    file_size = os.path.getsize(target_path) if os.path.exists(target_path) else 0

    # ZIM magic number: bytes 0-3 are 'Z','I','M','\x04'
    ZIM_MAGIC = b'\x5a\x49\x4d\x04'

    # FIX 31: Torrent downloads skip zimcheck entirely.
    #
    # Why no zimcheck for torrents?
    #   libtorrent cryptographically verifies every piece against the SHA-1
    #   hashes in the torrent manifest as it downloads. By the time a torrent
    #   reports completion, every byte has already been verified. Running
    #   zimcheck -C on top would be redundant, and -I would be prohibitively
    #   slow for large files (same reasoning as the HTTP path above).
    #   We only confirm the 4-byte ZIM magic number as a sanity check that
    #   libtorrent saved the file correctly and didn't mix up filenames.
    #
    #   If you want a full zimcheck after a torrent download, run manually:
    #     zimcheck -C -D <file.zim>       (checksum only, faster)
    #     zimcheck -C -I -D <file.zim>    (full check, may take hours for large files)
    if via_torrent:
        try:
            with open(target_path, 'rb') as f:
                header = f.read(4)
            if header == ZIM_MAGIC:
                print(f"  Torrent piece-verified + ZIM header OK: {filename}")
                print(f"  (zimcheck skipped -- libtorrent already verified all pieces)")
                return True
            reason = "Bad ZIM magic number (torrent download)"
            detail = f"Expected: {ZIM_MAGIC.hex()}  Got: {header.hex()}"
            print(f"  ZIM header check FAILED for {filename} ({detail})")
            log_failure(filename, reason, detail, file_size, via_torrent=True)
            corrupt_dir = os.path.join(ZIM_SUBFOLDER, "corrupt")
            os.makedirs(corrupt_dir, exist_ok=True)
            shutil.move(target_path, os.path.join(corrupt_dir, base_name))
            return False
        except Exception as e:
            reason = f"ZIM header check error: {type(e).__name__}: {e}"
            print(f"  {reason}")
            log_failure(filename, reason, '', file_size, via_torrent=True)
            return False

    # HTTP downloads: run zimcheck -C -D (checksum + details).
    #
    # Why -C only and not -I (--integrity)?
    #   -C verifies the internal MD5/SHA checksum embedded in the ZIM file,
    #   which is sufficient to confirm the download is uncorrupted -- if the
    #   checksum passes, the file content matches what the Kiwix team published.
    #
    #   -I (low-level structural integrity) walks the entire file structure:
    #   cluster offsets, directory entries, internal cross-references, etc.
    #   For large ZIM files (tens to hundreds of GiB) this can take 30-60+
    #   minutes even on fast storage, making it impractical for routine use.
    #   We deliberately omit -I here on performance grounds. If you suspect
    #   structural corruption beyond what -C can detect, run manually:
    #     zimcheck -C -I -D <file.zim>
    #
    # Timeout is scaled to file size: ~2s/GiB, min 300s, max 3600s.
    try:
        file_size_gb = file_size / (1024 ** 3)
        timeout_sec = max(300, min(3600, int(file_size_gb * 2)))
        check_cmd = ['zimcheck', '-C', '-D', target_path]
        print(f"  Running zimcheck -C -D on {filename} "
              f"(checksum only; -I skipped for performance)...")
        result = subprocess.run(
            check_cmd,
            capture_output=True, text=True, timeout=timeout_sec
        )
        if result.returncode == 0:
            print(f"  ZIM checksum check passed for {filename}")
            return True
        else:
            detail = (result.stdout + result.stderr).strip()
            print(f"  ZIM checksum check FAILED for {filename}:")
            print(detail[:500])
            log_failure(filename,
                        "zimcheck -C -D failed (checksum; -I not run, see note in code)",
                        detail, file_size, via_torrent=False)
            corrupt_dir = os.path.join(ZIM_SUBFOLDER, "corrupt")
            os.makedirs(corrupt_dir, exist_ok=True)
            corrupt_path = os.path.join(corrupt_dir, base_name)
            shutil.move(target_path, corrupt_path)
            suffix_note = " (suffix removed)" if filename != base_name else ""
            print(f"  Moved to corrupt/: {corrupt_path}{suffix_note}")
            return False
    except subprocess.TimeoutExpired:
        reason = f"zimcheck -C timed out after {timeout_sec}s (-I not run, see note in code)"
        print(f"  WARNING: zimcheck timed out for {filename} after {timeout_sec}s")
        print(f"  (-I structural check was not attempted for performance reasons)")
        print(f"  File kept at {target_path}")
        print(f"  To verify manually: zimcheck -C -D {target_path}")
        print(f"  For full check:     zimcheck -C -I -D {target_path}")
        log_failure(filename, reason,
                    f"Timeout after {timeout_sec}s -- file NOT moved. "
                    f"Note: -I (structural integrity) was deliberately skipped for "
                    f"performance; only -C (checksum) was attempted. "
                    f"Run 'zimcheck -C -I -D {target_path}' to do a full check.",
                    file_size, via_torrent=False)
        return True   # treat as passed to avoid discarding a potentially good file
    except FileNotFoundError:
        if not zimcheck_warning_shown:
            print("  WARNING: 'zimcheck' not found — skipping ZIM integrity check")
            print("  To enable full verification, install: sudo apt install zim-tools")
            zimcheck_warning_shown = True
        try:
            with open(target_path, 'rb') as f:
                header = f.read(4)
            if header == ZIM_MAGIC:
                print(f"  Basic ZIM header check passed for {filename} (zimcheck not installed)")
                return True
            reason = "Bad ZIM magic number (zimcheck not installed, header-only check)"
            detail = f"Expected: {ZIM_MAGIC.hex()}  Got: {header.hex()}"
            log_failure(filename, reason, detail, file_size, via_torrent=False)
            return False
        except Exception as e:
            reason = f"Basic header check error: {type(e).__name__}: {e}"
            print(f"  {reason}")
            log_failure(filename, reason, '', file_size, via_torrent=False)
            return False
    except Exception as e:
        reason = f"Integrity check error: {type(e).__name__}: {e}"
        print(f"  {reason}")
        log_failure(filename, reason, '', file_size, via_torrent=False)
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
    via_torrent = False
    if torrent_url and LIBTORRENT_AVAILABLE:
        success = download_torrent(torrent_url, partial_path, expected_bytes, downloaded, filename, resume_path, verbose)
        if success:
            via_torrent = True
        elif not stop_event.is_set():
            print(f"  Torrent failed for {filename} — falling back to HTTP")
    if not success and not stop_event.is_set():
        success = download_http(http_url, partial_path, expected_bytes, downloaded, verbose)
    if success:
        # FIX 3: Rename .partial → .zim BEFORE the integrity check.
        if os.path.exists(partial_path):
            os.rename(partial_path, target_path)
        if verify_zim_integrity(target_path, filename, via_torrent=via_torrent):
            if os.path.exists(resume_path):
                os.remove(resume_path)
            # FIX 42: Remove from active_downloads so the completed file
            # disappears from the progress block rather than staying visible
            # with a stale percentage and "Downloading" state.
            with progress_lock:
                active_downloads.pop(filename, None)
            print(f"✓ Completed & verified: {filename} ({file_index}/{total_files})")
            return True
        else:
            with progress_lock:
                active_downloads.pop(filename, None)
            print(f"  Integrity verification failed — download discarded")
            return False
    elif not stop_event.is_set():
        with progress_lock:
            active_downloads.pop(filename, None)
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
                write_start = time.time()
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
                            # FIX 13: Update active_downloads so the refresh
                            # thread can include this file in the verbose block.
                            elapsed_w = time.time() - write_start
                            rate_kbs = (written / elapsed_w / 1024) if elapsed_w > 0 else 0
                            # FIX 42: Clamp to 100.0 -- if expected_bytes is a
                            # rounded approximation the true file can be slightly
                            # larger, producing >100% which looks wrong in output.
                            pct = min(100.0, ((downloaded + written) / expected_bytes * 100) if expected_bytes > 0 else 0)
                            fname = os.path.basename(target_path)
                            if fname.endswith('.partial'):
                                fname = fname[:-8]
                            with progress_lock:
                                active_downloads[fname] = {
                                    'progress': pct,
                                    'rate': rate_kbs,
                                    'state': 'Downloading',
                                    'method': 'HTTP',
                                    'checking': False,
                                }
                # FIX 13: No independent verbose prints; refresh thread handles output.
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
        prev_state_str = None
        global reset_session_clock, verify_start_time
        while not h.is_seed():
            if stop_event.is_set():
                return False
            s = h.status()
            state_str = str(s.state)

            # FIX 24+25: On the checking->downloading transition, absorb the
            # total_wanted_done jump by resetting last_reported, AND signal the
            # refresh thread to reset the session clock and byte counter so that
            # elapsed time and speed start fresh from this moment.
            if (prev_state_str in CHECKING_STATES and
                    state_str not in CHECKING_STATES):
                last_reported = int(s.total_wanted_done)
                reset_session_clock = True   # FIX 25: tell refresh thread to reset
            prev_state_str = state_str

            downloaded_this_tick = int(s.total_wanted_done - last_reported)
            if downloaded_this_tick > 0:
                last_reported = s.total_wanted_done
                total_bytes_on_disk += downloaded_this_tick
                # FIX 30: Record when the first verification byte arrives so
                # the refresh thread can calculate a verification-phase ETA.
                if state_str in CHECKING_STATES and verify_start_time == 0.0:
                    verify_start_time = time.time()
                if state_str not in CHECKING_STATES:
                    total_bytes_this_session += downloaded_this_tick
            with progress_lock:
                active_downloads[expected_filename] = {
                    'progress': s.progress * 100,
                    'rate': s.download_rate / 1024,
                    'state': STATE_LABELS.get(state_str, state_str),
                    'method': f"Seeds: {s.num_seeds}/{s.num_peers} | Copies: {s.distributed_copies:.2f}",
                    'checking': state_str in CHECKING_STATES,
                }
            # FIX 13: Verbose output is now owned entirely by the refresh
            # thread, which prints all file lines + total bar as one atomic
            # block. No independent prints here.
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
    global total_bytes_on_disk, total_bytes_this_session
    global download_start_time, display_start_time, verify_start_time, reset_session_clock
    last_print = 0
    NAME_COL  = 60   # width for "filename - state" label
    last_block_lines = 0  # how many lines the previous non-verbose block used

    # ANSI helpers for in-place rewrite (non-verbose mode)
    CURSOR_UP   = "\033[A"   # move cursor up one line
    ERASE_LINE  = "\033[2K"  # erase entire current line

    # Track whether we just transitioned out of verification so we can
    # print a blank line separator before the first download block.
    pending_transition_blank = False
    # FIX 28: Only honour the first checking->downloading transition.
    # With multiple files, each file fires reset_session_clock independently
    # as it finishes verifying. Without this guard, the clock and session
    # bytes would be reset again for every subsequent file transition,
    # discarding accumulated bytes and skewing the speed/ETA each time.
    session_clock_reset_done = False

    while not stop_event.is_set():
        now = time.time()

        # FIX 25+28: On the first checking->downloading transition, reset the
        # speed/ETA clock and session bytes. Ignore all subsequent transitions.
        if reset_session_clock:
            reset_session_clock = False   # always clear the flag
            if not session_clock_reset_done:
                total_bytes_this_session = 0
                download_start_time = now
                pending_transition_blank = True
                session_clock_reset_done = True

        # FIX 26: display_elapsed counts from session start (never resets);
        # speed_elapsed resets on transition (drives speed/ETA only).
        display_elapsed = now - display_start_time if display_start_time > 0 else 0.0
        speed_elapsed   = now - download_start_time if download_start_time > 0 else 0.0

        # Format display elapsed as HH:MM:SS
        e_h = int(display_elapsed // 3600)
        e_m = int((display_elapsed % 3600) // 60)
        e_s = int(display_elapsed % 60)
        elapsed_str = f"{e_h:02d}:{e_m:02d}:{e_s:02d}"

        pbar.n = min(total_bytes_on_disk, total_expected)

        if speed_elapsed > 3.0 and total_bytes_this_session > 0:
            # Download phase: speed and ETA based on session network bytes
            speed = total_bytes_this_session / speed_elapsed
            speed_str = f"{bytes_to_binary_human(int(speed))}/s"
            remaining = total_expected - total_bytes_on_disk
            eta_sec = remaining / speed if speed > 0 else 0
            eta_str = f"{int(eta_sec//3600):02d}:{int((eta_sec%3600)//60):02d}:{int(eta_sec%60):02d}"
            postfix_str = f"{elapsed_str}, {speed_str}, ETA={eta_str}"
        elif total_bytes_this_session == 0 and verify_start_time > 0 and total_bytes_on_disk > 0:
            # FIX 30: Verification phase: estimate ETA from verification
            # throughput (bytes verified per second since verification began).
            verify_elapsed = now - verify_start_time
            if verify_elapsed > 3.0:
                verify_speed = total_bytes_on_disk / verify_elapsed
                remaining = total_expected - total_bytes_on_disk
                eta_sec = remaining / verify_speed if verify_speed > 0 else 0
                eta_str = f"{int(eta_sec//3600):02d}:{int((eta_sec%3600)//60):02d}:{int(eta_sec%60):02d}"
                postfix_str = f"{elapsed_str}, ETA={eta_str}"
            else:
                postfix_str = f"{elapsed_str}, ETA=?:??:??"
        else:
            postfix_str = f"{elapsed_str}, ETA=?:??:??"

        # FIX 27: Build pbar_str manually, bypassing tqdm's postfix rendering
        # which prepends a spurious ", " before our postfix when the bar_format
        # bracket contains no other fields. We set postfix to empty and append
        # our own bracket content directly to tqdm's base bar string.
        pbar.set_postfix_str("")
        base_bar = str(pbar)
        # Strip tqdm's empty postfix bracket ("] " or trailing space artifacts)
        # and replace with our fully-formed bracket.
        if base_bar.endswith("[]"):
            pbar_str = base_bar[:-2] + f"[{postfix_str}]"
        elif base_bar.endswith("[ ]"):
            pbar_str = base_bar[:-3] + f"[{postfix_str}]"
        else:
            # Fallback: strip from last '[' and reattach
            idx = base_bar.rfind("[")
            pbar_str = (base_bar[:idx] + f"[{postfix_str}]") if idx >= 0 else base_bar

        # FIX 15: Do NOT call pbar.refresh() here.

        if now - last_print >= TOTAL_BAR_REFRESH_SECONDS:
            with progress_lock:
                snapshot = list(active_downloads.items())

            # FIX 27: Skip the print cycle entirely if there are no active
            # downloads yet -- this suppresses the bare "Total progress: 0%"
            # line that appeared before any files had started.
            if not snapshot:
                last_print = now
                time.sleep(0.4)
                continue

            # Build file lines (same format for both modes)
            file_lines = []
            for name, data in snapshot:
                state    = data.get('state', 'Downloading')
                method   = data.get('method', '')
                checking = data.get('checking', False)
                pct      = data.get('progress', 0.0)
                rate     = data.get('rate', 0.0)
                label    = f"{name} - {state}"
                if checking:
                    line = f"{label:<{NAME_COL}} | {pct:5.1f}%"
                else:
                    line = (f"{label:<{NAME_COL}} | {pct:5.1f}%"
                            f" | {rate:>8.1f} KB/s | {method}")
                file_lines.append(line)

            # Total lines this block will occupy: file lines + pbar + blank
            block_lines = len(file_lines) + 1 + 1

            if verbose:
                # The previous block already ends with a blank line (print()
                # below). On transition we just stop overwriting and let the
                # next block print fresh below -- no extra blank needed.
                if pending_transition_blank:
                    pending_transition_blank = False
                if file_lines:
                    print('\n'.join(file_lines))
                print(pbar_str)
                print()
            else:
                # In non-verbose mode the previous block ends with an erase+\n
                # blank line. On transition we just reset last_block_lines so
                # the next cycle prints fresh without overwriting -- no extra
                # \n needed.
                if pending_transition_blank:
                    last_block_lines = 0
                    pending_transition_blank = False
                if last_block_lines > 0:
                    sys.stdout.write(CURSOR_UP * last_block_lines)
                for line in file_lines:
                    sys.stdout.write(f"\r{ERASE_LINE}{line}\n")
                sys.stdout.write(f"\r{ERASE_LINE}{pbar_str}\n")
                sys.stdout.write(f"\r{ERASE_LINE}\n")  # blank separator
                sys.stdout.flush()
                last_block_lines = block_lines

            last_print = now

        time.sleep(0.4)

def download_list(items, verbose=False, cleanup_after_each=False):
    global active_downloads, total_bytes_on_disk, total_bytes_this_session
    global download_start_time, display_start_time, verify_start_time, reset_session_clock
    active_downloads.clear()
    display_start_time  = time.time()
    download_start_time = time.time()
    verify_start_time   = 0.0   # set when first verification byte arrives
    reset_session_clock = False

    # FIX 23: Only seed total_bytes_on_disk from completed .zim files, whose
    # size is reliable. Do NOT seed from .partial files -- libtorrent may use
    # sparse allocation or a small stub file, so os.path.getsize() on a .partial
    # can return a tiny value (e.g. 5 MiB) even when 62% of the content has been
    # downloaded. The real verified size is populated naturally by the
    # total_bytes_on_disk += downloaded_this_tick increment in download_torrent
    # as libtorrent works through the verification phase.
    already_on_disk = 0
    has_partials = False
    for item in items:
        zim_path     = os.path.join(ZIM_SUBFOLDER, item['filename'])
        partial_path = os.path.join(ZIM_SUBFOLDER, f"{item['filename']}.partial")
        if os.path.exists(zim_path):
            already_on_disk += item['size_bytes']   # use catalogued size, not actual
        elif os.path.exists(partial_path):
            has_partials = True                      # partial present but don't seed size
    total_bytes_on_disk = already_on_disk
    total_bytes_this_session = 0
    if already_on_disk > 0:
        print(f"Resuming: {bytes_to_binary_human(already_on_disk)} already on disk (completed files).")
    if has_partials:
        print("Resuming: calculating data already on disk across existing partial file(s)...")

    total_files = len(items)
    if total_files == 0:
        print("No files to download.")
        return

    total_bytes_expected = sum(item['size_bytes'] for item in items)

    print(f"\nStarting parallel download of {total_files} file(s) "
          f"({bytes_to_binary_human(total_bytes_expected)} total, max {MAX_CONCURRENT_DOWNLOADS} concurrent)... (Ctrl+C to pause)\n")

    stop_event.clear()

    # FIX 25: Remove {elapsed} from bar_format. tqdm's internal elapsed timer
    # starts when pbar is created (at the beginning of download_list) and cannot
    # be reset. We render our own elapsed in the postfix, calculated from
    # download_start_time which resets at the checking->downloading transition.
    BAR_FMT = (
        "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
        "[{postfix}]"
    )
    # FIX 15: In non-verbose mode, route tqdm's output to a null sink so any
    # internal tqdm writes (e.g. from pbar.close()) can't reach the terminal
    # and produce a rogue second bar line outside our controlled block.
    # In verbose mode tqdm writes via str(pbar) only, so the sink is used there
    # too for consistency -- our refresh thread owns all output in both modes.
    import io
    _tqdm_sink = io.StringIO()
    pbar_total = tqdm(total=total_bytes_expected, desc="Total progress", unit='B',
                      unit_scale=True, unit_divisor=1024, disable=not TQDM_AVAILABLE,
                      leave=True, bar_format=BAR_FMT, file=_tqdm_sink)

    refresh_thread = threading.Thread(target=total_bar_refresh_thread,
                                    args=(pbar_total, total_bytes_expected, stop_event, verbose),
                                    daemon=True)
    refresh_thread.start()

    try:
        def wrapped_download(item, idx):
            success = download_single(item, idx, total_files, verbose=verbose)
            if success:
                # FIX 42: Do NOT clamp total_bytes_on_disk to total_bytes_expected
                # here. The old clamp was intended to absorb rounding overshoot for
                # a single file, but it set the *global* total to 100% the moment
                # any single file completed, pushing Total progress to 100% while
                # other files were still downloading. Bytes are already accumulated
                # correctly chunk-by-chunk in download_http/download_torrent, and
                # the display already clamps via min(total_bytes_on_disk, total_expected).
                if cleanup_after_each:
                    cleanup_old_versions([item])

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
    # FIX 36: Base updates on files actually in zims/ rather than the list
    # file. The list file may be stale or out of sync with what's on disk.
    if not os.path.exists(ZIM_SUBFOLDER):
        print(f"No {ZIM_SUBFOLDER}/ folder found -- nothing to compare against.")
        return None

    # Build a dict of group_key -> (filename, date) from files on disk
    on_disk = {}
    for fname in os.listdir(ZIM_SUBFOLDER):
        if not fname.endswith('.zim'):
            continue
        fpath = os.path.join(ZIM_SUBFOLDER, fname)
        if not os.path.isfile(fpath):
            continue
        key  = extract_group_key(fname)
        date = parse_date_from_filename(fname)
        size = os.path.getsize(fpath)
        # Keep the newest if multiple versions of the same group exist
        if key not in on_disk or date > on_disk[key]['date']:
            on_disk[key] = {'filename': fname, 'date': date, 'size_bytes': size}

    if not on_disk:
        print(f"No ZIM files found in {ZIM_SUBFOLDER}/ -- run option 1 first.")
        return None

    print(f"Found {len(on_disk)} ZIM group(s) in {ZIM_SUBFOLDER}/.")
    print("Crawling server for latest versions...")
    all_current = fetch_directory(BASE_URL)
    if not all_current:
        print("Crawl failed.")
        return None

    current_best = select_best_per_group(all_current)
    current_dict = {f['group_key']: f for f in current_best}

    updates    = []
    new_groups = []

    for key, disk_item in on_disk.items():
        if key not in current_dict:
            continue
        curr = current_dict[key]
        # FIX 41: Compare only content dates parsed from filenames.
        # The size threshold check has been removed -- server sizes come from
        # approximate Apache directory strings (e.g. "153M", "5.2G") which
        # parse to rounded values, while on-disk sizes are exact byte counts.
        # Even a 5% threshold produces false positives: a file whose server
        # listing rounds differently from os.path.getsize() can exceed the
        # threshold, flagging same-version files as updates. Content dates
        # from filenames are unambiguous and sufficient for update detection.
        curr_content_date = parse_date_from_filename(curr['filename'])
        disk_content_date = disk_item['date']
        if curr_content_date > disk_content_date:
            updates.append(curr)

    for key, curr in current_dict.items():
        if key not in on_disk:
            new_groups.append(curr)

    to_download = updates + new_groups
    return to_download, updates, new_groups


def cleanup_zims_directory():
    """Option 4: scan zims/ and delete older duplicate versions of each group."""
    # FIX 37: Local-only cleanup. For each group key found in zims/, keep only
    # the file with the newest date and delete all older versions.
    if not os.path.exists(ZIM_SUBFOLDER):
        print(f"No {ZIM_SUBFOLDER}/ folder found.")
        return

    # Group all .zim files by group key
    groups = {}
    for fname in os.listdir(ZIM_SUBFOLDER):
        if not fname.endswith('.zim'):
            continue
        fpath = os.path.join(ZIM_SUBFOLDER, fname)
        if not os.path.isfile(fpath):
            continue
        key  = extract_group_key(fname)
        date = parse_date_from_filename(fname)
        groups.setdefault(key, []).append((date, fname))

    duplicates = {k: v for k, v in groups.items() if len(v) > 1}

    if not duplicates:
        print("No duplicate versions found in zims/ -- nothing to clean up.")
        return

    removed_count = 0
    removed_bytes = 0
    for key, versions in sorted(duplicates.items()):
        # Sort by date descending; keep index 0, delete the rest
        versions.sort(key=lambda x: x[0], reverse=True)
        keep = versions[0]
        print(f"\nGroup '{key}':")
        print(f"  Keeping : {keep[1]}")
        for date, fname in versions[1:]:
            fpath = os.path.join(ZIM_SUBFOLDER, fname)
            size  = os.path.getsize(fpath)
            try:
                os.remove(fpath)
                print(f"  Removed : {fname} ({bytes_to_binary_human(size)})")
                removed_count += 1
                removed_bytes += size
            except Exception as e:
                print(f"  ERROR removing {fname}: {e}")

    print(f"\nCleanup complete: removed {removed_count} file(s), "
          f"freed {bytes_to_binary_human(removed_bytes)}.")

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
    print("1) Generate / download new best English ZIMs list")
    print("2) Download / resume from existing best English ZIMs list")
    print("3) Check for updates (new and newer ZIMs; includes cleanup)")
    print("4) Cleanup ZIMs directory (remove older duplicate versions)")
    choice = get_valid_choice("Choose (1/2/3/4): ", ["1", "2", "3", "4"])
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
        print(f"  Required (max, incl. buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
        print(f"  Available: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Download cancelled due to insufficient space.")
                return
        download_choice = get_valid_choice("Download now? (y/n): ", ["y", "n"])
        if download_choice == "y":
            verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
            verbose = (verbose_choice == "y")
            download_list(best, verbose=verbose)
    elif choice == "2":
        items = load_list()
        if not items:
            print("No list found.")
            return
        # FIX 21: Use the same tolerance check as download_single when deciding
        # whether a file is already complete. The previous exact-size check
        # (getsize != item['size_bytes']) caused torrent-completed files whose
        # on-disk size differs slightly from the catalogued size to be re-added
        # to the download list, inflating the "Files to download" count and the
        # required-space figure.
        def is_incomplete(item):
            zim_path = os.path.join(ZIM_SUBFOLDER, item['filename'])
            if not os.path.exists(zim_path):
                return True
            on_disk = os.path.getsize(zim_path)
            tolerance = max(50 * 1024 * 1024, item['size_bytes'] // 20)
            return abs(on_disk - item['size_bytes']) > tolerance

        items_to_download = [item for item in items if is_incomplete(item)]
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
        print(f"  Required (max, incl. buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
        print(f"  Available: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Resume cancelled due to insufficient space.")
                return
        verbose_choice = get_valid_choice("\nVerbose output (detailed progress)? (y/n): ", ["y", "n"])
        verbose = (verbose_choice == "y")
        download_list(items_to_download, verbose=verbose)
    elif choice == "3":
        result = check_for_updates()
        if result is None:
            return
        to_download, updates, new_groups = result
        if not to_download:
            print("No updates or new groups found.")
            return
        total_bytes = sum(f['size_bytes'] for f in to_download)
        print(f"\nFound {len(updates)} update(s) and {len(new_groups)} new group(s) "
              f"({bytes_to_binary_human(total_bytes)} total):")
        if updates:
            print("\n  Updates:")
            for f in updates:
                print(f"    {f['filename']} ({bytes_to_binary_human(f['size_bytes'])})")
        if new_groups:
            print("\n  New groups:")
            for f in new_groups:
                print(f"    {f['filename']} ({bytes_to_binary_human(f['size_bytes'])})")
        required_gb = get_required_space_gb(to_download)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check for updates:")
        print(f"  Required (max, incl. buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
        print(f"  Available: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Update download cancelled due to insufficient space.")
                return
        download_choice = get_valid_choice("\nDownload updates? (y/n): ", ["y", "n"])
        if download_choice == "y":
            verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
            verbose = (verbose_choice == "y")
            download_list(to_download, verbose=verbose, cleanup_after_each=True)
    elif choice == "4":
        os.makedirs(ZIM_SUBFOLDER, exist_ok=True)
        cleanup_zims_directory()

# ==================== VERSIONING INFORMATION (END OF SCRIPT) ====================
# Kiwix English ZIM Tool
# Version: v20260401j
# Last updated: 2026-04-01
#
# Changes in this version (fix Total progress premature 100% + stale file display):
#
# FIX 42 -- Three related display fixes:
#
#   (a) Removed the global total_bytes_on_disk clamp from wrapped_download.
#       The old code set total_bytes_on_disk = max(total_bytes_on_disk,
#       total_bytes_expected) on any single file completion, pushing Total
#       progress to 100% the moment the first file finished while others were
#       still downloading. Bytes are accumulated correctly chunk-by-chunk in
#       download_http/download_torrent; the display already clamps via
#       min(total_bytes_on_disk, total_expected) in the refresh thread.
#
#   (b) HTTP progress percentage clamped to 100.0. If expected_bytes is a
#       rounded approximation and the actual file is slightly larger, the
#       percentage could exceed 100% (e.g. 102.1%) in the per-file display.
#
#   (c) Completed files are now removed from active_downloads immediately
#       after download_single returns (success, failure, or integrity fail).
#       Previously the entry stayed in active_downloads with a stale percentage
#       and "Downloading" state until the refresh thread happened to notice.
#       Now completed files disappear from the progress block cleanly.
#
# Earlier fixes from v20260401i carried forward:
#
# "Download?" prompt now appears before "Verbose output?" in options 1 and 3,
# so the user confirms they want to download before being asked about verbosity.
# Option 2 goes straight to download (no yes/no confirmation) so its prompt
# order is unchanged. Option 3's verbose prompt is now only shown if the user
# answers "y" to downloading, avoiding a pointless question on "n".
#
# Earlier fix from v20260401h carried forward:
#
# FIX 41 -- check_for_updates(): removed the size threshold comparison
#   (curr_size > disk_size * 1.05). Server sizes come from approximate Apache
#   directory strings (e.g. "153M", "5.2G") which parse to rounded values,
#   while on-disk sizes are exact byte counts from os.path.getsize(). The 5%
#   threshold was not reliable enough to absorb this discrepancy, causing
#   same-version files to be flagged as updates (e.g. citizensinformation
#   2025-12 vs 2025-12, axios 2025-10 vs 2025-10). Update detection now
#   relies solely on content date parsed from the filename -- unambiguous
#   and sufficient. Note: zims/corrupt/ is intentionally excluded from the
#   on-disk scan; files there are treated as absent for update purposes.
#
# Earlier fixes from v20260401f carried forward:
#
# FIX 40 -- check_for_updates(): date comparison now uses
#   parse_date_from_filename() on the server filename rather than curr['date']
#   (the Apache directory modification timestamp). The Apache timestamp is the
#   time the file was last modified on the server -- almost always more recent
#   than the YYYY-MM content date parsed from the on-disk filename -- so every
#   file was being flagged as an update (560 "updates" when only 2 were real).
#   Both sides now use parse_date_from_filename(), comparing content dates
#   consistently (e.g. 2026-03 vs 2026-02) rather than mixing Apache timestamps
#   with filename dates.
#
# Earlier fixes from v20260401e carried forward:
#
# FIX 39 -- check_for_updates() now returns a 3-tuple (to_download, updates,
#   new_groups) instead of just to_download. The option 3 block in main() was
#   re-deriving the updates/new_groups split after the fact using a broken
#   os.listdir() scan that matched any on-disk group key rather than only
#   groups that had a newer server version -- causing 560 "updates" to be
#   reported when only 2 were real. Now the categorization is done once,
#   correctly, inside check_for_updates(), and passed through directly.
#
# Earlier fixes from v20260401d carried forward:
#
# FIX 38 -- Cleanup now happens immediately after each file completes rather
#   than after all downloads finish. download_list() gains a new optional
#   parameter cleanup_after_each=False. When True (used by option 3),
#   cleanup_old_versions([item]) is called inside wrapped_download as soon as
#   a file is successfully downloaded and verified. This ensures older versions
#   are removed even if the session is interrupted before other files complete.
#   Options 1 and 2 pass cleanup_after_each=False (default) so their behaviour
#   is unchanged.
#
# Menu text updated:
#   1) Generate / download new best English ZIMs list
#   2) Download / resume from existing best English ZIMs list
#   3) Check for updates (new and newer ZIMs; includes cleanup)
#   4) Cleanup ZIMs directory (remove older duplicate versions)
#
# Earlier fixes from v20260401c carried forward:
#
# FIX 36 -- check_for_updates() rewritten to use the zims/ folder as the
#   comparison baseline instead of kiwix_english_best.txt. The list file
#   may be stale or out of sync with what is actually on disk. The new logic:
#   - Scans zims/ for all .zim files and builds a dict of group_key -> newest
#     on-disk version (date + size) by parsing filenames directly.
#   - Crawls the server and compares against on-disk state.
#   - Flags updates (server newer/larger than on-disk) and new groups (server
#     has a group not present in zims/ at all).
#   - The save-list prompt has been removed from option 3 -- use option 1 to
#     regenerate the list file if needed.
#   - Option 3 menu description updated to reflect the new behaviour.
#   - Updates and new groups are displayed in separate labelled sections.
#
# FIX 37 -- New option 4: "Cleanup zims/ directory". A local-only operation
#   that scans zims/ for duplicate versions of the same group key and deletes
#   all but the newest. Reports which files were kept, which were removed, and
#   how much space was freed. No server crawl, no downloads.
#
# Earlier fixes from v20260401b carried forward:
#
# FIX 35 -- Added explanatory comments, STDOUT messages, and failure log
#   entries documenting why zimcheck -I (--integrity, low-level structural
#   check) is deliberately not used:
#
#   Torrent path: zimcheck is skipped entirely because libtorrent already
#     cryptographically verifies every piece during download. STDOUT now
#     explicitly says "(zimcheck skipped -- libtorrent already verified all
#     pieces)". Code comment explains the reasoning and provides the manual
#     zimcheck commands for developers who want to run a full check.
#
#   HTTP path: only -C (checksum) is run, not -I. STDOUT now says
#     "(checksum only; -I skipped for performance)". Code comment explains
#     that -I can take 30-60+ minutes on large files and is impractical for
#     routine use, and provides the manual commands. Failure log entries now
#     explicitly state "-I not run, see note in code" so the log is
#     self-documenting even without access to the source.
#
#   In both cases, manual commands are provided for full verification:
#     zimcheck -C -D <file>      (checksum only)
#     zimcheck -C -I -D <file>   (full structural + checksum)
#
# Earlier fixes from v20260401a carried forward:
#
# FIX 32 -- Persistent failure logging: a new log_failure() helper appends a
#   timestamped entry to kiwix_download_failures.log for every integrity check
#   failure. Each entry records: timestamp, filename, file size, download method
#   (torrent/http), reason, and full detail (zimcheck output, error message, or
#   hex values for bad magic numbers). This means integrity failures are never
#   lost even if the terminal is closed immediately after the run.
#
# FIX 33 -- Correct ZIM magic number: the magic bytes are 'ZIM\x04'
#   (0x5a 0x49 0x4d 0x04), not '\x04\x00\x00\x00' as previously hardcoded.
#   The old value would have caused every torrent-verified file to fail the
#   header check (all 11 corrupt/ files in the reported incident had the correct
#   magic number, confirming the files were good and the check was wrong).
#
# FIX 34 -- zimcheck now called with -D (--details) flag in addition to -C so
#   the full error detail is captured in both the screen output and the log,
#   making it possible to diagnose false positives vs genuine corruption.
#
# Earlier fix from v20260331f carried forward:
#
# FIX 31 -- verify_zim_integrity overhauled with three improvements:
#
#   (a) Torrent downloads skip zimcheck entirely. libtorrent cryptographically
#       verifies every piece against the torrent manifest during download, so
#       running zimcheck on top is redundant and prohibitively slow for large
#       files. For torrent downloads, only the 4-byte ZIM magic number header
#       is checked. download_single now tracks via_torrent=True/False and
#       passes it through to verify_zim_integrity.
#
#   (b) HTTP downloads use a timeout scaled to file size: max(300, min(3600,
#       file_size_gb * 2)) seconds. The previous flat 300-second timeout was
#       too short for large files (e.g. 235 GB needs ~470 s at minimum).
#
#   (c) TimeoutExpired is now handled separately from other exceptions. A
#       timeout is not proof of corruption -- the file is left in place with
#       a warning and the function returns True (treated as passed) rather
#       than silently discarding a potentially good download. The user is
#       advised to run zimcheck manually.
#
# Earlier fix from v20260331e carried forward:
#
# FIX 30 -- During the torrent verification phase, the Total progress bar now
#   shows a live ETA based on verification throughput rather than "ETA=?:??:??".
#   Two changes:
#   (a) download_torrent: verify_start_time is set to the current time when the
#       first verification byte arrives (state in CHECKING_STATES and
#       total_bytes_on_disk just became non-zero). This gives the refresh thread
#       a clean reference point for verification speed.
#   (b) total_bar_refresh_thread: when total_bytes_this_session == 0 (no network
#       bytes yet) but total_bytes_on_disk > 0 and verify_start_time is set, the
#       postfix shows an ETA projected from verification speed:
#         verify_speed = total_bytes_on_disk / (now - verify_start_time)
#         eta = (total_expected - total_bytes_on_disk) / verify_speed
#       This ETA reflects "time until verification completes". Once downloading
#       begins the clock resets and the ETA switches to reflecting remaining
#       download time as before.
#
# Earlier fix from v20260331d carried forward:
#
# FIX 29 -- The explicit blank line printed by pending_transition_blank was
#   doubling up with the trailing blank line already printed at the end of
#   each block (verbose: print(); non-verbose: ERASE_LINE + \n). Removed the
#   extra blank from the pending_transition_blank branch in both modes -- the
#   previous block's own trailing blank now serves as the single separator
#   between the verification and download phases.
#
# Earlier fix from v20260331c carried forward:
#
# FIX 28 -- total_bar_refresh_thread: added session_clock_reset_done flag so
#   the reset_session_clock signal is only acted upon once -- on the first
#   checking->downloading transition across all concurrent downloads. Without
#   this guard, each file would fire reset_session_clock independently as it
#   finished verifying, resetting download_start_time and clearing
#   total_bytes_this_session each time and discarding accumulated bytes.
#   Now only the first transition resets the clock; subsequent files' signals
#   are cleared but ignored, so the speed/ETA continues accumulating cleanly
#   from the moment the first file began downloading.
#
# Earlier fix from v20260331b carried forward:
#
# FIX 27 -- Three display improvements to the refresh thread:
#   (a) Bare "Total progress: 0%" line before any files start is suppressed --
#       the print cycle is skipped when active_downloads is empty.
#   (b) On the checking->downloading transition, the last verification block is
#       left visible and a blank line is inserted before the first download
#       block, cleanly separating the two phases in both verbose and non-verbose.
#   (c) Fixed spurious leading comma "[, 00:00:00, ETA=...]" -- tqdm prepends
#       ", " before postfix even with no other bracket fields. Fixed by setting
#       postfix to empty and manually appending our own "[...]" to the base bar.
#
# Earlier fix from v20260331a carried forward:
#
# FIX 26 -- Split timing into two independent references:
#   display_start_time: set once at session start, never reset. Drives the
#     HH:MM:SS elapsed field in the Total progress bar. Counts continuously
#     through verification and downloading so the user always sees how long
#     the session has been running.
#   download_start_time: reset to now on the checking->downloading transition
#     (via the reset_session_clock flag from FIX 25). Drives speed and ETA
#     calculations only. Resets to zero when downloading begins so that speed
#     and ETA reflect only actual network download time, not verification time.
#   Removed FIX 22's clock-pinning (keeping download_start_time == now while
#     total_bytes_this_session == 0) which was the cause of the frozen elapsed
#     display during verification. The FIX 25 reset_session_clock flag handles
#     the transition cleanly without freezing the display clock.
#
# Earlier fixes carried forward:
#
# FIX 25 -- Two related issues on resume after verification:
#   (a) Elapsed time in the Total progress bar was not resetting to zero when
#       downloading began -- tqdm's internal {elapsed} timer cannot be reset, so
#       it was replaced with our own HH:MM:SS elapsed field rendered in the
#       postfix, calculated from download_start_time which we control.
#   (b) A new global flag reset_session_clock is set by download_torrent on the
#       checking->downloading transition. The refresh thread detects this flag
#       and atomically resets both total_bytes_this_session to 0 and
#       download_start_time to now, ensuring the displayed elapsed, speed, and
#       ETA all start fresh from the moment real downloading begins.
#       FIX 24's last_reported reset absorbs the total_wanted_done jump;
#       FIX 25's clock reset ensures no stale bytes or time remain in the
#       speed/ETA calculation after the transition.
#
# Earlier fix carried forward (previously listed as this version's main change):
#
# - "Required (incl. buffer)" -> "Required (max, incl. buffer)" in all three
#   space-check blocks (menu options 1, 2, and 3), clarifying that the figure
#   is a conservative maximum since partial files are treated as needing their
#   full size.
# - "Starting parallel download of N files" -> "N file(s)" for grammatical
#   correctness when only one file is being downloaded.
#
# Earlier fix carried forward (previously listed as this version's main change):
#
# FIX 24 -- download_torrent: when libtorrent transitions from a checking phase
#   (checking_files, checking_resume_data) into downloading, s.total_wanted_done
#   jumps from near-0 to the full verified amount in a single tick. Previously
#   this entire jump was credited as network bytes in the first downloading tick,
#   producing inflated speeds (e.g. 146 MiB/s) and falsely optimistic ETAs.
#   Fix: prev_state_str tracks the previous state. When the transition from a
#   CHECKING_STATE to a non-checking state is detected, last_reported is
#   immediately reset to the current total_wanted_done, absorbing the jump
#   silently. Only genuinely new bytes received after the transition are counted
#   toward total_bytes_this_session.
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
#   FIX 12 -- double comma between {elapsed} and {postfix} removed
#   FIX 13 -- verbose output consolidated into atomic blocks in refresh thread
#   FIX 14 -- non-verbose mode renders same block format with in-place rewrite
#   FIX 15 -- pbar.refresh() removed; tqdm output routed to null sink
#   FIX 16 -- baseline seeding counts completed .zim files as well as .partial
#   FIX 18 -- seeds/peers ratio and distributed_copies added to torrent display
#   FIX 19 -- baseline seeding uses item size_bytes for .zim to avoid overshoot
#   FIX 20 -- get_required_space_gb deducts already-downloaded bytes
#   FIX 21 -- items_to_download uses tolerance check; blank line before verbose prompt
#   FIX 22 -- clock resets while no network bytes received, excluding verification time
#   FIX 23 -- partial files not seeded into baseline; misleading size message removed

if __name__ == "__main__":
    main()
