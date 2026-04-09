#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiwix ZIM Selector + Downloader + Update Checker
Version: v20260324e   # Full language names (sorted alphabetically) + refined detection
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
VERSION = "v20260324e"
BASE_URL = "https://download.kiwix.org/zim/"
USER_AGENT = f"Kiwix-ZIM-Downloader/{VERSION}"
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

# Comprehensive code → full name mapping (covers all common Kiwix ISO codes)
LANGUAGE_MAP = {
    'en': 'English', 'fr': 'French', 'es': 'Spanish', 'de': 'German', 'it': 'Italian',
    'pt': 'Portuguese', 'ru': 'Russian', 'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean',
    'ar': 'Arabic', 'af': 'Afrikaans', 'am': 'Amharic', 'ang': 'Old English', 'ast': 'Asturian',
    'ay': 'Aymara', 'az': 'Azerbaijani', 'ba': 'Bashkir', 'be': 'Belarusian', 'bg': 'Bulgarian',
    'bn': 'Bengali', 'bo': 'Tibetan', 'br': 'Breton', 'bs': 'Bosnian', 'ca': 'Catalan',
    'ce': 'Chechen', 'ckb': 'Central Kurdish', 'cs': 'Czech', 'cv': 'Chuvash', 'cy': 'Welsh',
    'da': 'Danish', 'diq': 'Zaza', 'dsb': 'Lower Sorbian', 'dv': 'Dhivehi', 'el': 'Greek',
    'eo': 'Esperanto', 'et': 'Estonian', 'eu': 'Basque', 'fa': 'Persian', 'fi': 'Finnish',
    'fo': 'Faroese', 'fy': 'Western Frisian', 'ga': 'Irish', 'gd': 'Scottish Gaelic',
    'gl': 'Galician', 'gn': 'Guarani', 'got': 'Gothic', 'gu': 'Gujarati', 'gv': 'Manx',
    'ha': 'Hausa', 'he': 'Hebrew', 'hi': 'Hindi', 'hsb': 'Upper Sorbian', 'hu': 'Hungarian',
    'hy': 'Armenian', 'ia': 'Interlingua', 'id': 'Indonesian', 'ie': 'Interlingue',
    'ig': 'Igbo', 'ilo': 'Ilocano', 'io': 'Ido', 'is': 'Icelandic', 'jv': 'Javanese',
    'ka': 'Georgian', 'kk': 'Kazakh', 'kl': 'Kalaallisut', 'km': 'Khmer', 'kn': 'Kannada',
    'ku': 'Kurdish', 'kv': 'Komi', 'kw': 'Cornish', 'ky': 'Kyrgyz', 'la': 'Latin',
    'lb': 'Luxembourgish', 'li': 'Limburgish', 'lmo': 'Lombard', 'ln': 'Lingala',
    'lo': 'Lao', 'lt': 'Lithuanian', 'lv': 'Latvian', 'mg': 'Malagasy', 'mi': 'Māori',
    'mk': 'Macedonian', 'ml': 'Malayalam', 'mn': 'Mongolian', 'mr': 'Marathi',
    'ms': 'Malay', 'mt': 'Maltese', 'my': 'Burmese', 'na': 'Nauru', 'nah': 'Nahuatl',
    'nap': 'Neapolitan', 'nds': 'Low German', 'ne': 'Nepali', 'new': 'Newari',
    'nl': 'Dutch', 'nn': 'Norwegian Nynorsk', 'no': 'Norwegian', 'nov': 'Novial',
    'oc': 'Occitan', 'or': 'Odia', 'os': 'Ossetian', 'pa': 'Punjabi', 'pdc': 'Pennsylvania German',
    'pms': 'Piedmontese', 'pnb': 'Western Punjabi', 'pl': 'Polish', 'ps': 'Pashto',
    'qu': 'Quechua', 'rm': 'Romansh', 'ro': 'Romanian', 'rw': 'Kinyarwanda',
    'sa': 'Sanskrit', 'sc': 'Sardinian', 'sco': 'Scots', 'sd': 'Sindhi', 'se': 'Northern Sami',
    'sh': 'Serbo-Croatian', 'si': 'Sinhala', 'sk': 'Slovak', 'sl': 'Slovenian',
    'so': 'Somali', 'sq': 'Albanian', 'sr': 'Serbian', 'srn': 'Sranan Tongo',
    'su': 'Sundanese', 'sv': 'Swedish', 'sw': 'Swahili', 'ta': 'Tamil', 'te': 'Telugu',
    'tg': 'Tajik', 'th': 'Thai', 'ti': 'Tigrinya', 'tk': 'Turkmen', 'tl': 'Tagalog',
    'tn': 'Tswana', 'to': 'Tongan', 'tpi': 'Tok Pisin', 'tr': 'Turkish', 'ts': 'Tsonga',
    'tt': 'Tatar', 'tum': 'Tumbuka', 'tw': 'Twi', 'ty': 'Tahitian', 'ug': 'Uyghur',
    'uk': 'Ukrainian', 'ur': 'Urdu', 'uz': 'Uzbek', 've': 'Venda', 'vi': 'Vietnamese',
    'vo': 'Volapük', 'wa': 'Walloon', 'war': 'Waray', 'wo': 'Wolof', 'xal': 'Kalmyk',
    'xh': 'Xhosa', 'yi': 'Yiddish', 'yo': 'Yoruba', 'za': 'Zhuang', 'zea': 'Zealandic',
    'zu': 'Zulu',
}

def detect_language(filename):
    fn = filename.lower()
    for code, full_name in LANGUAGE_MAP.items():
        if re.search(r'_' + code + r'_', fn) or re.search(r'_' + code + r'all_', fn):
            return full_name
    # Fallback for any 2-3 letter code
    iso_match = re.search(r'_(?:all_|maxi_|nopic_)?([a-z]{2,3})_', fn)
    if iso_match:
        code = iso_match.group(1)
        return LANGUAGE_MAP.get(code, code.upper())
    return 'Unknown'

# ==================== REMAINING CODE (full & unchanged except language improvements) ====================
# (All other functions are identical to the previous complete version)

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

def get_subcategory_tree(url):
    try:
        path = url.replace(BASE_URL, "").split('/')
        top = path[0] if path else "root"
        sub = path[1] if len(path) > 1 else ""
        if top == "stack_exchange" and sub:
            sub = sub.split('.')[0] + ".stackexchange.com"
        return top, sub if sub else top
    except:
        return "unknown", "unknown"

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

            date_obj = datetime.min
            if date_str != '-' and ' ' in date_str:
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
                except ValueError:
                    pass

            size_bytes = parse_size_to_bytes(size_str)
            lang = detect_language(filename)
            top_cat, sub_cat = get_subcategory_tree(full_url)

            collected.append({
                'url': full_url,
                'filename': filename,
                'group_key': extract_group_key(filename),
                'language': lang,
                'top_category': top_cat,
                'sub_category': sub_cat,
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
        if not files:
            continue
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

# ==================== LANGUAGE + TREE SELECTION (now using full names) ====================
def build_language_tree(items):
    lang_tree = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for item in items:
        lang = item.get('language', 'Unknown')
        top = item.get('top_category', 'unknown')
        sub = item.get('sub_category', '')
        lang_tree[lang][top][sub].append(item)
    return lang_tree

def print_language_tree(lang_tree):
    print("\nDetected languages (sorted alphabetically):")
    languages = sorted(lang_tree.keys())
    for i, lang in enumerate(languages, 1):
        total = sum(len(lang_tree[lang][top][sub]) for top in lang_tree[lang] for sub in lang_tree[lang][top])
        print(f"  {i}) {lang} ({total} files)")
    print("  a) All languages")

def select_languages(lang_tree):
    print_language_tree(lang_tree)
    sel = input("\nSelect language numbers (comma-separated) or 'a' for all: ").strip().lower()

    languages = sorted(lang_tree.keys())
    if sel == 'a':
        return languages
    try:
        indices = [int(x.strip()) - 1 for x in sel.split(',') if x.strip().isdigit()]
        selected = [languages[i] for i in indices if 0 <= i < len(languages)]
        return selected if selected else languages
    except:
        print("Invalid selection. Using all languages.")
        return languages

def build_category_tree_for_languages(items, selected_langs):
    tree = defaultdict(lambda: defaultdict(list))
    for item in items:
        if item.get('language') in selected_langs:
            top = item.get('top_category', 'unknown')
            sub = item.get('sub_category', '')
            tree[top][sub].append(item)
    return tree

def print_category_tree(tree):
    print("\nDetected subcategories (tree view):")
    top_list = sorted(tree.keys())
    for i, top in enumerate(top_list, 1):
        subs = sorted(tree[top].keys())
        count = sum(len(tree[top][s]) for s in subs)
        print(f"  {i}) {top} ({count} files)")
        for j, sub in enumerate(subs, 1):
            if sub and sub != top:
                sub_count = len(tree[top][sub])
                print(f"     └─ {sub} ({sub_count} files)")
    print("  a) All subcategories")

def select_from_tree(tree):
    print_category_tree(tree)
    sel = input("\nSelect category numbers (comma-separated) or 'a' for all: ").strip().lower()

    top_list = sorted(tree.keys())
    if sel == 'a':
        selected_items = []
        for top in top_list:
            for sub in tree[top]:
                selected_items.extend(tree[top][sub])
        return selected_items

    try:
        indices = [int(x.strip()) - 1 for x in sel.split(',') if x.strip().isdigit()]
        selected_items = []
        for idx in indices:
            if 0 <= idx < len(top_list):
                top = top_list[idx]
                for sub in tree[top]:
                    selected_items.extend(tree[top][sub])
        return selected_items
    except:
        print("Invalid selection. Using all subcategories.")
        selected_items = []
        for top in top_list:
            for sub in tree[top]:
                selected_items.extend(tree[top][sub])
        return selected_items

# ==================== QUICK PICK ====================
def quick_pick_best_english(all_files):
    print("\nQuick pick: Selecting best/most inclusive English ZIM files only...")
    english_files = [f for f in all_files if f.get('language') == 'English']
    best = select_best_per_group(english_files)
    print(f"Quick pick selected {len(best)} best English files.")
    return best

# ==================== SAVE / LOAD (unchanged) ====================
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
        f.write(f"# Kiwix best/latest files - {VERSION}\n")
        f.write(f"# Generated: {now}\n")
        f.write(f"# Total files: {len(items)}\n")
        f.write(f"# Total size: {bytes_to_human(total_bytes)}\n")
        f.write("# Format: filename|size_bytes|url|torrent_url|language|top_category|sub_category\n\n")
        for item in items:
            f.write(f"{item['filename']}|{item['size_bytes']}|{item['url']}|{item.get('torrent_url', '')}|{item.get('language', 'Unknown')}|{item.get('top_category', 'unknown')}|{item.get('sub_category', '')}\n")

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
                    lang = parts[4] if len(parts) >= 5 else 'Unknown'
                    top_cat = parts[5] if len(parts) >= 6 else 'unknown'
                    sub_cat = parts[6] if len(parts) >= 7 else ''
                    items.append({
                        'filename': fn,
                        'size_bytes': sz,
                        'url': http_url,
                        'torrent_url': torrent_url,
                        'language': lang,
                        'top_category': top_cat,
                        'sub_category': sub_cat,
                        'group_key': extract_group_key(fn)
                    })
                except ValueError:
                    pass
    return items

# ==================== INTEGRITY & DOWNLOAD (full) ====================
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
            print("  WARNING: 'zimcheck' not found — skipping full integrity check")
            print("  Install with: sudo apt install zim-tools")
            zimcheck_warning_shown = True
        try:
            with open(target_path, 'rb') as f:
                if f.read(4) == b'\x04\x00\x00\x00':
                    print(f"  Basic ZIM header check passed for {filename}")
                    return True
            return False
        except:
            return False
    except Exception as e:
        print(f"  Integrity check error for {filename}: {e}")
        return False

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
            print(f"✓ Pre-existing file close enough — treating as complete")
            return True

    downloaded = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0

    if os.path.exists(partial_path):
        print(f"↻ Resuming {filename} from partial")
    else:
        print(f"↓ Downloading {filename} ({file_index}/{total_files})")

    success = False
    if torrent_url and LIBTORRENT_AVAILABLE:
        if verbose:
            print(f"  Trying torrent for {filename}")
        success = download_torrent(torrent_url, partial_path, expected_bytes, downloaded, filename, resume_path, verbose)
    if not success:
        print(f"  Using HTTP for {filename}")
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
            range_header = f'bytes={downloaded}-' if downloaded > 0 else None
            if range_header:
                headers['Range'] = range_header

            if verbose:
                print(f"  HTTP attempt {attempt+1}/{retries} (mirror: {mirror_base})")

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
                print(f"  HTTP success (size within tolerance)")
                return True
            else:
                print(f"  HTTP size mismatch: got {final_size} B, expected {expected_bytes} B")
                return False

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 416:
                print("  416 Range Not Satisfiable — retrying full download")
                downloaded = 0
                if os.path.exists(target_path):
                    os.remove(target_path)
                continue
            print(f"  HTTP error on mirror {mirror_base}: {e}")
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except requests.exceptions.Timeout:
            print(f"  HTTP timeout on mirror {mirror_base}")
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
        except Exception as e:
            print(f"  HTTP download error on mirror {mirror_base}: {e}")
            time.sleep(backoff[attempt] if attempt < len(backoff) else 30)
    print("  HTTP failed after all retries")
    return False

def download_torrent(torrent_url, target_path, expected_bytes, downloaded, expected_filename, resume_path, verbose=False):
    if not LIBTORRENT_AVAILABLE:
        print("libtorrent not available.")
        return False

    save_dir = os.getcwd()
    if verbose:
        print(f"  Saving torrent to {save_dir}")

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
            except:
                if verbose:
                    print("  Failed to load resume data — starting fresh")

        h = ses.add_torrent(add_params)

        if verbose:
            print(f"Torrent: {info.name()} started")

        last_resume_save = time.time()
        last_print = time.time()
        while not h.is_seed():
            s = h.status()
            if verbose or time.time() - last_print > PROGRESS_UPDATE_INTERVAL:
                print(f"\rTorrent progress: {expected_filename} {s.progress*100:.1f}% | {s.download_rate/1024:.1f} KB/s | Peers: {s.num_peers}", end='')
                last_print = time.time()
            time.sleep(1)

        print(f"\nTorrent reported completed: {expected_filename}")

        final_path = os.path.join(save_dir, info.name())
        if os.path.exists(final_path):
            on_disk_bytes = os.path.getsize(final_path)
            print(f"  On-disk verification: {bytes_to_human(on_disk_bytes)}")
            tolerance = max(50 * 1024 * 1024, expected_bytes // 20)
            diff = abs(on_disk_bytes - expected_bytes)
            if expected_bytes > 0 and diff <= tolerance:
                if final_path != target_path:
                    os.rename(final_path, target_path)
                if verbose:
                    print(f"  Size within tolerance → torrent success")
                if os.path.exists(resume_path):
                    os.remove(resume_path)
                return True
            else:
                print(f"  Size mismatch! Got {on_disk_bytes} B, expected {expected_bytes} B")
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

# ==================== PARALLEL DOWNLOAD ====================
def download_list(items, verbose=False):
    total_files = len(items)
    if total_files == 0:
        print("No files to download.")
        return

    items_to_download = []
    for item in items:
        target_path = os.path.join(ZIM_SUBFOLDER, item['filename'])
        if not os.path.exists(target_path) or os.path.getsize(target_path) != item['size_bytes']:
            items_to_download.append(item)

    if not items_to_download:
        print("All files already complete.")
        stats = get_summary_stats()
        print("\nPost-run summary (no new downloads):")
        print(f"  - Total files in list: {total_files}")
        print(f"  - Good / verified files: {stats['good_count']}")
        print(f"  - Corrupt files detected: {stats['corrupt_count']}")
        print(f"  - Total size on disk (good files): {stats['good_size']}")
        print(f"  - Corrupt files size: {stats['corrupt_size']}")
        print(f"  - Disk usage in ./zims/: {stats['total_folder_size']}")
        return

    required_gb = get_required_space_gb(items_to_download)
    available_gb = get_free_space_gb(ZIM_SUBFOLDER)

    print(f"\nSpace check:")
    print(f"  Files to download: {len(items_to_download)}")
    print(f"  Required (incl. buffer): ~{required_gb:.1f} GB")
    print(f"  Available: ~{available_gb:.1f} GB")

    if available_gb < required_gb:
        if not confirm_proceed(required_gb, available_gb):
            print("Download cancelled due to insufficient space.")
            return

    total_bytes_expected = sum(item['size_bytes'] for item in items)

    print(f"\nStarting parallel download of {total_files} files "
          f"({bytes_to_human(total_bytes_expected)} total, max {MAX_CONCURRENT_DOWNLOADS} concurrent)...\n")

    remaining = items[:]
    completed = 0

    with tqdm(total=total_bytes_expected, desc="Total progress", unit='B', unit_scale=True, unit_divisor=1024,
              disable=not TQDM_AVAILABLE, leave=True) as pbar_total:

        def wrapped_download(item, idx):
            success = download_single(item, idx, total_files, verbose)
            if success:
                nonlocal completed
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

# ==================== CLEANUP & UPDATE ====================
def cleanup_old_versions(downloaded_items):
    print("\nCleaning up older versions...")
    for item in downloaded_items:
        group = item['group_key']
        new_date = parse_date_from_filename(item['filename'])
        for local_file in list(os.listdir(ZIM_SUBFOLDER)):
            if not local_file.endswith('.zim'):
                continue
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
        if key not in current_dict:
            continue
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

# ==================== VALIDATED INPUT ====================
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
    print(f"Kiwix ZIM Downloader {VERSION}\n")
    print("1) Generate new download list")
    print("2) Download / resume from existing list")
    print("3) Check for updates (includes new groups + auto-cleanup)")

    choice = get_valid_choice("Choose (1/2/3): ", ["1", "2", "3"])

    if choice == "1":
        print("\nGenerating new download list...")
        all_files = fetch_directory(BASE_URL)
        if not all_files:
            print("No ZIM files found.")
            return

        print(f"\nFound {len(all_files)} total ZIM files.")

        print("\nHow would you like to refine the list?")
        print("  Q) Quick pick — best/most inclusive English ZIM files only")
        print("  R) Refine manually (choose languages + subcategories)")
        refine = get_valid_choice("Choose (Q/R): ", ["q", "r"])

        if refine == "q":
            final_list = quick_pick_best_english(all_files)
        else:
            lang_tree = build_language_tree(all_files)
            selected_langs = select_languages(lang_tree)
            filtered = []
            for lang in selected_langs:
                for top in lang_tree[lang]:
                    for sub in lang_tree[lang][top]:
                        filtered.extend(lang_tree[lang][top][sub])
            tree = build_category_tree_for_languages(filtered, selected_langs)
            final_list = select_from_tree(tree)

        print(f"\nFinal list contains {len(final_list)} files.")

        save_choice = get_valid_choice("\nSave list to file? (y/n): ", ["y", "n"])
        if save_choice == "y":
            final_list = save_list(final_list)

        required_gb = get_required_space_gb(final_list)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check:")
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
            download_list(final_list, verbose=verbose)

    elif choice == "2":
        items = load_list()
        if not items:
            print("No list found.")
            return

        verbose_choice = get_valid_choice("Verbose output (detailed progress)? (y/n): ", ["y", "n"])
        verbose = (verbose_choice == "y")

        download_list(items, verbose=verbose)

    elif choice == "3":
        to_download, current_best = check_for_updates()
        if not to_download:
            return

        print(f"\nFound {len(to_download)} updates/new groups:")
        for f in to_download:
            print(f"  {f['filename']} ({f['size_str']})")

        required_gb = get_required_space_gb(to_download)
        available_gb = get_free_space_gb(ZIM_SUBFOLDER)
        print(f"\nSpace check:")
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
