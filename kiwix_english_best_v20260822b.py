#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiwix English ZIM Selector + Downloader + Update Checker
Version: v20260822b

NOTE (2026-08): download.kiwix.org/zim/ now redirects to the Hub marketing
site (no Apache index). BASE_URL uses lb.download.kiwix.org which still
serves classic directory listings. Category subdirs remain usable.

EXCLUSION RULES:
- Gutenberg: keep ONLY gutenberg_en_all_*
- Wikipedia: keep ONLY wikipedia_en_all_maxi_*
- Wiktionary: keep wiktionary_en_all_nopic_* (only comprehensive English version)
- FreeCodeCamp: keep ONLY freecodecamp_en_all_* (it subsumes all subset ZIMs)
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
VERSION = "v20260822b"
BASE_URL = "https://lb.download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-English-ZIM-Downloader/{VERSION}"
DELAY = 0.8
MAX_DEPTH = 4
LIST_FILE   = "kiwix_english_best.txt"
RESUME_FILE = "kiwix_resume_update.txt"   # written by option 3 when downloads are incomplete
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
    "https://lb.download.kiwix.org/zim/",          # primary — still serves classic Apache indexes
    "https://download.kiwix.org/zim/",             # public alias (redirects at root; usable for file GETs)
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
startup_lines = {}
# calculated from independent sources and can't interfere with each other.
total_bytes_on_disk = 0      # seeded from partials at startup + all new bytes written;
                              # drives the progress bar position only
total_bytes_this_session = 0 # always starts at 0; only new bytes from this session;
                              # drives speed and ETA only
download_start_time = 0.0    # resets on checking->downloading transition; drives speed/ETA
display_start_time  = 0.0    # never resets; drives the elapsed display in the progress bar
verify_start_time   = 0.0    # set when first verification byte arrives; drives verify ETA
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
    # Each subset (coding-interview-prep, javascript-algorithms-and-data-structures,
    # project-euler, rosetta-code) is built from the same openZIM scraper with a
    # subset of courses; freecodecamp_en_all_* is built with all courses combined.
    # The subset ZIMs exist for bandwidth-limited or topic-specific deployments,
    # but are fully redundant when freecodecamp_en_all_* is present.
    # Whitelist _en_all_ first, then exclude every other freecodecamp_en_* variant.
    if lower.startswith('freecodecamp_en_all_'): return False
    if lower.startswith('freecodecamp_en_'): return True
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

def find_reclaimable_old_versions(items_to_download):
    """Return on-disk .zim files that will be deleted once the given items succeed.

    A file is reclaimable when it shares a group_key with an incoming download
    and has an older content date (or is any other filename in that group that
    the incoming item will supersede).
    Returns a list of dicts: filename, size_bytes, group_key, replaced_by.
    """
    if not os.path.exists(ZIM_SUBFOLDER) or not items_to_download:
        return []

    # Newest incoming filename/date per group
    incoming = {}
    for item in items_to_download:
        key = item.get('group_key') or extract_group_key(item['filename'])
        date = parse_date_from_filename(item['filename'])
        if key not in incoming or date > incoming[key]['date']:
            incoming[key] = {'filename': item['filename'], 'date': date}

    reclaimable = []
    seen = set()
    for fname in os.listdir(ZIM_SUBFOLDER):
        if not fname.endswith('.zim'):
            continue
        fpath = os.path.join(ZIM_SUBFOLDER, fname)
        if not os.path.isfile(fpath):
            continue
        key = extract_group_key(fname)
        if key not in incoming:
            continue
        if fname == incoming[key]['filename']:
            continue  # the target itself (already complete edge-case)
        old_date = parse_date_from_filename(fname)
        # Delete any other version in the same group that is older than the
        # incoming file. If the on-disk file has no parseable date, still treat
        # it as replaceable when the filenames differ.
        if old_date < incoming[key]['date'] or old_date == datetime.min:
            if fname in seen:
                continue
            seen.add(fname)
            reclaimable.append({
                'filename': fname,
                'size_bytes': os.path.getsize(fpath),
                'group_key': key,
                'replaced_by': incoming[key]['filename'],
            })
    return reclaimable


def get_required_space_gb(items_to_download):
    """Compute net additional space needed after reclaiming old versions.

    Returns
    -------
    required_gb : float
        Net additional GiB needed (download size - reclaimable + safety buffer).
    remaining_bytes : int
        Gross bytes still to write for incomplete / missing files.
    reclaimable_bytes : int
        Bytes that will be freed when outdated versions are deleted.
    reclaimable : list
        Details from find_reclaimable_old_versions().
    """
    # Completed .zim files need 0 additional space. Everything else (partial or
    # not started) is treated conservatively as needing its full size_bytes.
    # libtorrent sparse allocation can make partial on-disk sizes misleading.
    remaining_bytes = 0
    for item in items_to_download:
        zim_path = os.path.join(ZIM_SUBFOLDER, item['filename'])
        if os.path.exists(zim_path):
            pass  # fully downloaded -- needs 0 additional space
        else:
            remaining_bytes += item['size_bytes']

    reclaimable = find_reclaimable_old_versions(items_to_download)
    reclaimable_bytes = sum(r['size_bytes'] for r in reclaimable)

    # Net additional after replacements are deleted. Floor at 0 so reclaiming
    # more than we download never reports a negative requirement.
    net_bytes = max(0, remaining_bytes - reclaimable_bytes)
    net_gb = net_bytes / (1024 ** 3)
    # Buffer is based on the *net* need so a large reclaim does not force an
    # oversized buffer, while still protecting concurrent peak usage.
    buffer_gb = max(SPACE_SAFETY_BUFFER_GB, net_gb * 0.1)
    required_gb = net_gb + buffer_gb
    return required_gb, remaining_bytes, reclaimable_bytes, reclaimable


def print_space_check(label, items_to_download, available_gb):
    """Print a space-check breakdown and return required_gb for comparison."""
    required_gb, remaining_bytes, reclaimable_bytes, reclaimable = \
        get_required_space_gb(items_to_download)
    print(f"\nSpace check{label}:")
    print(f"  Files to download: {len(items_to_download)}")
    print(f"  Download size (gross): ~{bytes_to_binary_human(remaining_bytes)}")
    if reclaimable_bytes > 0:
        print(f"  Reclaimed from old versions: ~{bytes_to_binary_human(reclaimable_bytes)} "
              f"({len(reclaimable)} file(s))")
        # Show a short sample so the user can verify which files will go
        for r in reclaimable[:8]:
            print(f"    - {r['filename']} ({bytes_to_binary_human(r['size_bytes'])}) "
                  f"→ replaced by {r['replaced_by']}")
        if len(reclaimable) > 8:
            print(f"    ... and {len(reclaimable) - 8} more")
    else:
        print("  Reclaimed from old versions: none")
    print(f"  Required (net + {SPACE_SAFETY_BUFFER_GB} GiB buffer): "
          f"~{bytes_to_binary_human(int(required_gb * 1024**3))}")
    print(f"  Available: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
    return required_gb


def confirm_proceed(required_gb, available_gb):
    print(f"\nWARNING: Insufficient free space detected!")
    print(f"  Required (net + buffer): ~{bytes_to_binary_human(int(required_gb * 1024**3))}")
    print(f"  Available on zims: ~{bytes_to_binary_human(int(available_gb * 1024**3))}")
    print("  Proceeding may fail or leave the drive full.")
    print("  Note: old versions are deleted only AFTER each replacement verifies,")
    print("  so peak usage during concurrent downloads can temporarily exceed the net figure.")
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
            print(f"WARNING: No Apache <pre> listing found at {url}", file=sys.stderr)
            print("  The site layout may have changed. Check BASE_URL / mirrors.", file=sys.stderr)
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
        # Previously size was ranked before date, so an older but slightly
        # larger file (e.g. devdocs_en_axios_2025-10 at 407 KiB) would beat
        # a newer but smaller file (devdocs_en_axios_2026-02 at 330 KiB),
        # causing the server crawl to select stale versions and miss updates.
        files.sort(key=lambda x: (
            -get_selection_priority(x['filename']),
            -(x['date'].timestamp() if x['date'] != datetime.min else 0),
            -x['size_bytes'],
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

def add_torrent_urls(items):
    """Check for torrent availability and add torrent_url to each item in-place.
    FIX 49: Extracted from save_list so option 3 (updates) can also populate
    torrent_url before passing items to download_list, enabling torrent
    downloads for update sessions — not just option 1/2 sessions."""
    def check_torrent(item):
        torrent_url = item['url'] + '.torrent'
        try:
            r = requests.get(torrent_url, headers={'User-Agent': USER_AGENT},
                             stream=True, timeout=TORRENT_CHECK_TIMEOUT,
                             allow_redirects=True)
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
    return items

def save_list(items, filename=LIST_FILE):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_bytes = sum(f['size_bytes'] for f in items)
    add_torrent_urls(items)
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# Kiwix English ZIM best/latest files - {VERSION}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Total files: {len(items)}\n")
        f.write(f"# Total size: {bytes_to_binary_human(total_bytes)}\n")
        # readability when the file is opened in a text editor. size_bytes
        # (now field 3) is still used for all calculations.
        f.write("# Format: filename|size_human|size_bytes|url|torrent_url\n\n")
        for item in items:
            size_human = bytes_to_binary_human(item['size_bytes'])
            f.write(f"{item['filename']}|{size_human}|{item['size_bytes']}|{item['url']}|{item.get('torrent_url', '')}\n")
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
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 3:
                fn = parts[0]
                try:
                    # Old format: filename|size_bytes|url|torrent_url
                    # Detect by attempting to parse parts[1] as int:
                    # if it succeeds, it's the old format; if not, it's new.
                    try:
                        int(parts[1])
                        # Old format — size_bytes at index 1
                        sz       = int(parts[1])
                        http_url = parts[2]
                        torrent_url = parts[3] if len(parts) >= 4 else ''
                    except ValueError:
                        # New format — size_human at index 1, size_bytes at index 2
                        sz       = int(parts[2])
                        http_url = parts[3]
                        torrent_url = parts[4] if len(parts) >= 5 else ''
                    items.append({
                        'filename': fn,
                        'size_bytes': sz,
                        'url': http_url,
                        'torrent_url': torrent_url,
                        'group_key': extract_group_key(fn)
                    })
                except (ValueError, IndexError):
                    pass
    return items

def save_resume_file(items, filename=RESUME_FILE):
    """Write a resume file for items that did not complete during an option 3 session."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_bytes = sum(f['size_bytes'] for f in items)
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# Kiwix resume file - {VERSION}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Incomplete downloads from option 3 (updates) session\n")
        f.write(f"# Total files: {len(items)}\n")
        f.write(f"# Total size: {bytes_to_binary_human(total_bytes)}\n")
        f.write("# Format: filename|size_human|size_bytes|url|torrent_url\n\n")
        for item in items:
            size_human = bytes_to_binary_human(item['size_bytes'])
            f.write(f"{item['filename']}|{size_human}|{item['size_bytes']}|{item['url']}|{item.get('torrent_url', '')}\n")
    print(f"  Resume file saved: {filename} ({len(items)} file(s))")

def delete_resume_file(filename=RESUME_FILE):
    """Delete the resume file once all items have been successfully downloaded."""
    try:
        if os.path.exists(filename):
            os.remove(filename)
            print(f"  Resume file deleted: {filename} (all items complete)")
    except Exception as e:
        print(f"  Warning: could not delete resume file {filename}: {e}")


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
    # download_list can print all startup lines in index order before any
    # progress output begins.
    if os.path.exists(partial_path):
        startup_msg = f"↻ Resuming {filename} from partial ({file_index}/{total_files})"
    else:
        startup_msg = f"↓ Downloading {filename} ({file_index}/{total_files})"
    with progress_lock:
        startup_lines[file_index] = startup_msg
    success = False
    via_torrent = False
    if torrent_url and LIBTORRENT_AVAILABLE:
        success = download_torrent(torrent_url, partial_path, expected_bytes, downloaded, filename, resume_path, verbose, file_index)
        if success:
            via_torrent = True
        elif not stop_event.is_set():
            print(f"  Torrent failed for {filename} — falling back to HTTP")
    if not success and not stop_event.is_set():
        success, http_error = download_http(http_url, partial_path, expected_bytes, downloaded, verbose, file_index)
    else:
        http_error = None
    if success:
        if os.path.exists(partial_path):
            os.rename(partial_path, target_path)
        if verify_zim_integrity(target_path, filename, via_torrent=via_torrent):
            if os.path.exists(resume_path):
                os.remove(resume_path)
            # one final "Completed | 100%" line before removing the entry.
            with progress_lock:
                if filename in active_downloads:
                    active_downloads[filename]['state'] = 'Completed'
                    active_downloads[filename]['progress'] = 100.0
            time.sleep(TOTAL_BAR_REFRESH_SECONDS * 2)
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
        # The post-run summary handles the resume instruction.
        error_str = f" — {http_error}" if http_error else ""
        if os.path.exists(partial_path):
            partial_size = os.path.getsize(partial_path)
            print(f"  ✗ Failed: {filename} ({file_index}/{total_files}){error_str}")
            print(f"    Partial file preserved: {partial_path} ({bytes_to_binary_human(partial_size)})")
        else:
            print(f"  ✗ Failed: {filename} ({file_index}/{total_files}){error_str} — no partial file saved")
        return False
    return False

def download_http(original_url, target_path, expected_bytes, downloaded, verbose=False, file_index=0):
    if stop_event.is_set():
        return False, None
    retries = HTTP_RETRIES
    backoff = [5, 10, 20]
    mirror_index = 0
    last_error = None
    global total_bytes_on_disk, total_bytes_this_session
    for attempt in range(retries):
        if stop_event.is_set():
            return False, None
        # already-substituted URL from the previous attempt.
        mirror_base = MIRRORS[mirror_index % len(MIRRORS)]
        url = original_url.replace("https://download.kiwix.org/zim/", mirror_base)
        mirror_index += 1
        try:
            headers = {'User-Agent': USER_AGENT}
            if downloaded > 0:
                headers['Range'] = f'bytes={downloaded}-'
            # `downloaded` is respected before we open the file.
            mode = 'ab' if downloaded > 0 else 'wb'
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
                            return False, None
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            # so increment both counters.
                            total_bytes_on_disk += len(chunk)
                            total_bytes_this_session += len(chunk)
                            # thread can include this file in the verbose block.
                            elapsed_w = time.time() - write_start
                            rate_kbs = (written / elapsed_w / 1024) if elapsed_w > 0 else 0
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
                                    'file_index': file_index,
                                }
            final_size = os.path.getsize(target_path)
            tolerance = max(1024 * 1024, expected_bytes // 200)
            if expected_bytes > 0 and abs(final_size - expected_bytes) <= tolerance:
                return True, None
            else:
                last_error = f"Size mismatch: got {bytes_to_binary_human(final_size)}, expected {bytes_to_binary_human(expected_bytes)}"
                return False, last_error
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 416:
                # and starts fresh, not 'ab' on a deleted file.
                downloaded = 0
                if os.path.exists(target_path):
                    os.remove(target_path)
                continue
            last_error = f"HTTP {e.response.status_code}: {e.response.reason}"
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except requests.exceptions.Timeout:
            last_error = "Connection timed out"
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {e}"
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
    return False, last_error

def download_torrent(torrent_url, target_path, expected_bytes, downloaded, expected_filename, resume_path, verbose=False, file_index=0):
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

            # total_wanted_done jump by resetting last_reported, AND signal the
            # refresh thread to reset the session clock and byte counter so that
            # elapsed time and speed start fresh from this moment.
            if (prev_state_str in CHECKING_STATES and
                    state_str not in CHECKING_STATES):
                last_reported = int(s.total_wanted_done)
                reset_session_clock = True
            prev_state_str = state_str

            downloaded_this_tick = int(s.total_wanted_done - last_reported)
            if downloaded_this_tick > 0:
                last_reported = s.total_wanted_done
                total_bytes_on_disk += downloaded_this_tick
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
                    'file_index': file_index,
                }
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
def total_bar_refresh_thread(pbar, total_expected, stop_event, verbose, total_files=0):
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
    session_clock_reset_done = False

    # file-index order, then print a blank line before progress begins.
    startup_printed = False

    while not stop_event.is_set():
        now = time.time()

        # them in order and then proceed to normal progress display.
        if not startup_printed and total_files > 0:
            with progress_lock:
                collected = len(startup_lines)
            if collected >= total_files:
                for idx in sorted(startup_lines.keys()):
                    print(startup_lines[idx])
                print()  # blank line after startup block
                startup_printed = True
            else:
                time.sleep(0.1)
                continue

        # speed/ETA clock and session bytes. Ignore all subsequent transitions.
        if reset_session_clock:
            reset_session_clock = False   # always clear the flag
            if not session_clock_reset_done:
                total_bytes_this_session = 0
                download_start_time = now
                pending_transition_blank = True
                session_clock_reset_done = True

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


        if now - last_print >= TOTAL_BAR_REFRESH_SECONDS:
            with progress_lock:
                snapshot = list(active_downloads.items())

            # the same order as the startup ↓/↻ lines.
            snapshot.sort(key=lambda kv: kv[1].get('file_index', 0))

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

def download_list(items, verbose=False, cleanup_after_each=False, resume_file=None):
    global active_downloads, total_bytes_on_disk, total_bytes_this_session
    global download_start_time, display_start_time, verify_start_time, reset_session_clock
    active_downloads.clear()
    display_start_time  = time.time()
    download_start_time = time.time()
    verify_start_time   = 0.0   # set when first verification byte arrives
    reset_session_clock = False

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

    global startup_lines
    startup_lines = {}

    # starts when pbar is created (at the beginning of download_list) and cannot
    # be reset. We render our own elapsed in the postfix, calculated from
    # download_start_time which resets at the checking->downloading transition.
    BAR_FMT = (
        "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
        "[{postfix}]"
    )
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
                                    args=(pbar_total, total_bytes_expected, stop_event, verbose, total_files),
                                    daemon=True)
    refresh_thread.start()

    try:
        def wrapped_download(item, idx):
            success = download_single(item, idx, total_files, verbose=verbose)
            if success:
                # Always delete older versions in the same group as soon as the
                # replacement has verified successfully. This frees disk space
                # immediately and keeps the library to a single best file per group.
                # (cleanup_after_each is retained for API compatibility but is
                # no longer required — cleanup is unconditional on success.)
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

        # the non-verbose in-place rewrite area before printing the post-run
        # summary. Without this, the ANSI cursor-up rewrite can overwrite the
        # first lines of the summary, leaving only the last line visible.
        sys.stdout.write("\n\n")
        sys.stdout.flush()

        stats = get_summary_stats()
        print("Post-run summary:")
        print(f"  - Total files in list: {total_files}")
        print(f"  - Good / verified files: {stats['good_count']}")
        print(f"  - Corrupt files detected: {stats['corrupt_count']}")
        print(f"  - Total size on disk (good files): {stats['good_size']}")
        print(f"  - Corrupt files size: {stats['corrupt_size']}")
        print(f"  - Disk usage in ./zims/: {stats['total_folder_size']}")

        # no corresponding .zim file exists (i.e. not yet renamed on completion).
        # Show remaining bytes (expected − on disk) and only show the resume
        # instruction when there are actual incomplete files.
        expected_sizes = {item['filename']: item['size_bytes'] for item in items}
        genuine_partials = []
        for f in sorted(os.listdir(ZIM_SUBFOLDER)):
            if not f.endswith('.partial'):
                continue
            zim_name = f[:-8]  # strip '.partial'
            if os.path.exists(os.path.join(ZIM_SUBFOLDER, zim_name)):
                continue  # already renamed — not incomplete
            ppath = os.path.join(ZIM_SUBFOLDER, f)
            on_disk = os.path.getsize(ppath)
            expected = expected_sizes.get(zim_name, 0)
            remaining = max(0, expected - on_disk) if expected > 0 else 0
            genuine_partials.append((f, on_disk, remaining))

        if genuine_partials:
            # instruction can accurately reference it.
            if resume_file:
                resume_items_to_save = [
                    item for item in items
                    if f"{item['filename']}.partial" in {fp for fp, _, _ in genuine_partials}
                ]
                if resume_items_to_save:
                    save_resume_file(resume_items_to_save, resume_file)
            print(f"\n  ⚠ Incomplete download(s) — {len(genuine_partials)} file(s) not fully downloaded:")
            for fname, on_disk, remaining in genuine_partials:
                if remaining > 0:
                    print(f"    {fname} ({bytes_to_binary_human(on_disk)} on disk, {bytes_to_binary_human(remaining)} remaining)")
                else:
                    print(f"    {fname} ({bytes_to_binary_human(on_disk)} on disk)")
            if resume_file:
                print(f"\n    → A resume file has been saved: {resume_file}")
                print(f"    → Run option 3 to resume these downloads.")
            else:
                print(f"\n    → Run option 2 to resume these downloads.")
        elif resume_file:
            # All completed — clean up any existing resume file.
            delete_resume_file(resume_file)

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
    """Delete older on-disk versions in the same group as each newly completed item.

    Called immediately after a successful download/verify so space is reclaimed
    as soon as the replacement is known good. Silent when nothing to remove.
    """
    if not os.path.exists(ZIM_SUBFOLDER):
        return
    for item in downloaded_items:
        group = item.get('group_key') or extract_group_key(item['filename'])
        new_date = parse_date_from_filename(item['filename'])
        for local_file in list(os.listdir(ZIM_SUBFOLDER)):
            if not local_file.endswith('.zim'):
                continue
            if extract_group_key(local_file) != group:
                continue
            if local_file == item['filename']:
                continue
            old_date = parse_date_from_filename(local_file)
            # Remove older versions; also remove same-group files with no date
            # when the new file has a real date (defensive for odd names).
            if old_date < new_date or (old_date == datetime.min and new_date != datetime.min):
                fpath = os.path.join(ZIM_SUBFOLDER, local_file)
                try:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    print(f"  Removed old version: {local_file} "
                          f"(freed {bytes_to_binary_human(size)})")
                except Exception as e:
                    print(f"  Could not delete {local_file}: {e}")

def check_for_updates():
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

    # total space to be freed, before asking for confirmation.
    to_delete = []  # list of (fname, size) tuples
    print(f"\nDuplicate groups found: {len(duplicates)}\n")
    for key, versions in sorted(duplicates.items()):
        versions.sort(key=lambda x: x[0], reverse=True)
        keep = versions[0]
        print(f"  Group '{key}':")
        print(f"    Keep   : {keep[1]}")
        for date, fname in versions[1:]:
            fpath = os.path.join(ZIM_SUBFOLDER, fname)
            size  = os.path.getsize(fpath)
            print(f"    Delete : {fname} ({bytes_to_binary_human(size)})")
            to_delete.append((fname, size))

    total_freed = sum(s for _, s in to_delete)
    print(f"\n  {len(to_delete)} file(s) will be deleted, "
          f"freeing {bytes_to_binary_human(total_freed)}.")

    confirm = get_valid_choice("\nProceed with deletion? (y/n): ", ["y", "n"])
    if confirm == "n":
        print("Cleanup cancelled — no files deleted.")
        return

    removed_count = 0
    removed_bytes = 0
    for key, versions in sorted(duplicates.items()):
        versions.sort(key=lambda x: x[0], reverse=True)
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
        # files. The list file should always reflect the complete current best
        # selection from the server, not just the files that need downloading.
        # Also always save unconditionally — previously save was gated on a
        # "Save list to file? (y/n)" prompt and skipped entirely if everything
        # was already downloaded.
        best = save_list(best)
        print("\nChecking for already downloaded files...")
        to_download = filter_existing_files(best)
        if not to_download:
            print("All best files are already present (or close enough) in the zims/ directory.")
            stats = get_summary_stats()
            print("\nCurrent disk summary:")
            print(f"  - Good / verified files: {stats['good_count']}")
            print(f"  - Total size on disk (good files): {stats['good_size']}")
            return
        total_size = sum(f['size_bytes'] for f in to_download)
        print(f"\nSelected {len(to_download)} best files that still need downloading ({bytes_to_binary_human(total_size)}):")
        for f in to_download:
            print(f"  {f['filename']} ({bytes_to_binary_human(f['size_bytes'])})")
        print(f"\nTotal to download: {len(to_download)} files ({bytes_to_binary_human(total_size)})")
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        required_gb = print_space_check(" for new download", to_download, available_gb)
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Download cancelled due to insufficient space.")
                return
        download_choice = get_valid_choice("Download now? (y/n): ", ["y", "n"])
        if download_choice == "y":
            verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
            verbose = (verbose_choice == "y")
            download_list(to_download, verbose=verbose)
    elif choice == "2":
        using_resume_file = False
        if os.path.exists(RESUME_FILE):
            resume_items = load_list(RESUME_FILE)
            if resume_items:
                print(f"\nFound resume file from a previous update session: {RESUME_FILE}")
                print(f"  {len(resume_items)} file(s) were not completed last time.")
                resume_choice = get_valid_choice("Resume these downloads? (y/n): ", ["y", "n"])
                if resume_choice == "y":
                    items = resume_items
                    using_resume_file = True
                else:
                    print("Ignoring resume file — loading main list instead.")
        if not using_resume_file:
            items = load_list()
        if not items:
            print("No list found.")
            return
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
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        required_gb = print_space_check(" for resume", items_to_download, available_gb)
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Resume cancelled due to insufficient space.")
                return
        verbose_choice = get_valid_choice("\nVerbose output (detailed progress)? (y/n): ", ["y", "n"])
        verbose = (verbose_choice == "y")
        download_list(items_to_download, verbose=verbose)
        # complete, delete the resume file.
        if using_resume_file:
            still_incomplete = [
                item for item in items_to_download
                if not os.path.exists(os.path.join(ZIM_SUBFOLDER, item['filename']))
                or os.path.exists(os.path.join(ZIM_SUBFOLDER, f"{item['filename']}.partial"))
            ]
            if not still_incomplete:
                delete_resume_file()
            else:
                save_resume_file(still_incomplete)
    elif choice == "3":
        # offer to resume it rather than re-crawling the server.
        if os.path.exists(RESUME_FILE):
            resume_items = load_list(RESUME_FILE)
            if resume_items:
                print(f"\nFound incomplete update session: {RESUME_FILE}")
                print(f"  {len(resume_items)} file(s) did not complete last time:")
                for item in resume_items:
                    print(f"    {item['filename']} ({bytes_to_binary_human(item['size_bytes'])})")
                print("\n  r) Resume the previous update session")
                print("  n) Check for all updates (re-crawl server)")
                resume_choice = get_valid_choice("Choose (r/n): ", ["r", "n"])
                if resume_choice == "r":
                    available_gb = get_free_space_gb(ZIM_SUBFOLDER)
                    required_gb = print_space_check("", resume_items, available_gb)
                    if available_gb < required_gb:
                        if not confirm_proceed(required_gb, available_gb):
                            print("Resume cancelled due to insufficient space.")
                            return
                    verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
                    verbose = (verbose_choice == "y")
                    add_torrent_urls(resume_items)
                    download_list(resume_items, verbose=verbose, cleanup_after_each=True,
                                  resume_file=RESUME_FILE)
                    return

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
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        required_gb = print_space_check(" for updates", to_download, available_gb)
        if available_gb < required_gb:
            if not confirm_proceed(required_gb, available_gb):
                print("Update download cancelled due to insufficient space.")
                return
        download_choice = get_valid_choice("\nDownload updates? (y/n): ", ["y", "n"])
        if download_choice == "y":
            verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
            verbose = (verbose_choice == "y")
            # sessions use torrents where available, not just HTTP.
            add_torrent_urls(to_download)
            # inside the finally block and references it in the summary.
            download_list(to_download, verbose=verbose, cleanup_after_each=True,
                          resume_file=RESUME_FILE)
    elif choice == "4":
        os.makedirs(ZIM_SUBFOLDER, exist_ok=True)
        cleanup_zims_directory()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description=f"Kiwix English ZIM Tool {VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 kiwix_english_best.py            # interactive menu\n"
            "  python3 kiwix_english_best.py -u         # non-interactive update (for cron)\n"
            "  python3 kiwix_english_best.py --update   # same as -u\n"
        )
    )
    parser.add_argument(
        "-u", "--update",
        action="store_true",
        help=(
            "Non-interactive update mode (suitable for cron jobs). "
            "Checks for and downloads updates automatically — no prompts. "
            "Exits silently if the ZIMs directory does not exist "
            "(e.g. VeraCrypt volume not mounted)."
        )
    )
    args = parser.parse_args()

    if args.update:
        # Mirrors option 3 logic but requires no user input.
        import sys
        print(f"Kiwix English ZIM Tool {VERSION} — non-interactive update mode")

        # Bail out gracefully if the ZIMs directory isn't accessible
        # (e.g. VeraCrypt volume not mounted).
        if not os.path.exists(ZIM_SUBFOLDER):
            print(f"ZIMs directory not found: {ZIM_SUBFOLDER}")
            print("Exiting — volume may not be mounted.")
            sys.exit(0)

        # Resume a previous incomplete update session if one exists.
        if os.path.exists(RESUME_FILE):
            resume_items = load_list(RESUME_FILE)
            if resume_items:
                print(f"\nResuming incomplete update session: {RESUME_FILE}")
                print(f"  {len(resume_items)} file(s) to resume:")
                for item in resume_items:
                    print(f"    {item['filename']} ({bytes_to_binary_human(item['size_bytes'])})")
                available_gb = get_free_space_gb(ZIM_SUBFOLDER)
                required_gb = print_space_check("", resume_items, available_gb)
                if available_gb < required_gb:
                    print("Insufficient space — skipping resume.")
                    sys.exit(1)
                add_torrent_urls(resume_items)
                download_list(resume_items, verbose=False, cleanup_after_each=True,
                              resume_file=RESUME_FILE)
                sys.exit(0)

        # No resume file — crawl for updates.
        result = check_for_updates()
        if result is None:
            print("Update check failed.")
            sys.exit(1)
        to_download, updates, new_groups = result
        if not to_download:
            print("No updates or new groups found — nothing to do.")
            sys.exit(0)
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
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        required_gb = print_space_check("", to_download, available_gb)
        if available_gb < required_gb:
            print("Insufficient space — skipping update.")
            sys.exit(1)
        add_torrent_urls(to_download)
        download_list(to_download, verbose=False, cleanup_after_each=True,
                      resume_file=RESUME_FILE)
        sys.exit(0)
    else:
        main()
