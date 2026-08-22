# Kiwix English Best — User Guide

**Version:** v20260822a  
**Last Updated:** August 22, 2026

---

## Table of Contents

1. [Overview](#overview)
2. [Requirements](#requirements)
3. [Getting Started](#getting-started)
4. [Menu Options](#menu-options)
   - [Option 1 — Generate / Download New Best English ZIMs List](#option-1)
   - [Option 2 — Download / Resume from Existing List](#option-2)
   - [Option 3 — Check for Updates](#option-3)
   - [Option 4 — Cleanup ZIMs Directory](#option-4)
5. [Selection Rules](#selection-rules)
6. [Non-Interactive Update Mode (Cron Jobs)](#non-interactive-mode)
7. [File Reference](#file-reference)
8. [Download Methods](#download-methods)
9. [Progress Display](#progress-display)
10. [Integrity Verification](#integrity-verification)
11. [Server Layout Note (August 2026)](#server-layout-note)
12. [Tips and Troubleshooting](#tips-and-troubleshooting)

---

## Overview

**Kiwix English Best** is a Python script that automates the selection, downloading, and maintenance of the best available English-language ZIM files from the Kiwix download mirrors. ZIM files are compressed offline archives used by the [Kiwix](https://www.kiwix.org) reader to browse Wikipedia, Stack Exchange, Project Gutenberg, FreeCodeCamp, and hundreds of other resources without an internet connection.

The script:

- Crawls the Kiwix server to find the best (newest, highest-quality) English ZIM file for each content group
- Downloads files using BitTorrent (via libtorrent) where available, with HTTP as a fallback
- Supports resuming interrupted downloads
- Verifies file integrity after download
- Detects and downloads updates when newer versions are available on the server
- Cleans up older duplicate versions to save disk space
- Reports all sizes in binary units (KiB / MiB / GiB / TiB) with two decimal places

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.8+ | Required |
| `requests` | `pip install requests` |
| `tqdm` | `pip install tqdm` (optional, for progress bars) |
| `libtorrent` | Optional; enables torrent downloads |
| `zimcheck` | Optional; enables deep ZIM integrity checking (HTTP downloads only) |

Install Python dependencies:

```bash
pip install requests tqdm
```

To enable torrent support, install libtorrent for your platform. On Debian/Ubuntu:

```bash
sudo apt install python3-libtorrent
```

---

## Getting Started

Run the script interactively:

```bash
python3 kiwix_english_best_v20260822a.py
```

You will see the main menu:

```
Kiwix English ZIM Tool v20260822a

1) Generate / download new best English ZIMs list
2) Download / resume from existing best English ZIMs list
3) Check for updates (new and newer ZIMs; includes cleanup)
4) Cleanup ZIMs directory (remove older duplicate versions)
Choose (1/2/3/4):
```

All ZIM files are stored in the `zims/` subdirectory relative to the script's location.

---

## Menu Options

### Option 1 — Generate / Download New Best English ZIMs List {#option-1}

**When to use:** First-time setup, or when you want to refresh the full best-file selection from the server.

**What it does:**

1. Crawls the Kiwix server to discover all available English ZIM files
2. Selects the best (newest date, then largest / highest priority) file per content group, applying the exclusion rules below
3. Saves the complete best list to `kiwix_english_best.txt` — this always happens, even if all files are already present on disk
4. Checks which files from the list are not yet downloaded (tolerant size match)
5. If files are missing, shows a space check and prompts to download

**Prompts:**

| Prompt | Description |
|---|---|
| `Download now? (y/n)` | Whether to start downloading the missing files |
| `Verbose output? (y/n)` | Verbose shows per-file progress lines; non-verbose shows a compact updating display |

**Note:** The list file (`kiwix_english_best.txt`) is always saved with the full best selection, regardless of what is already on disk. This file is used by Option 2 for resuming downloads.

---

### Option 2 — Download / Resume from Existing List {#option-2}

**When to use:** Resume a previously interrupted download session, or re-download missing files from an existing list.

**What it does:**

1. Checks whether `kiwix_resume_update.txt` exists (left by a failed Option 3 session) — if so, offers to resume those incomplete update downloads first
2. Otherwise loads `kiwix_english_best.txt`
3. Filters out files already present on disk (using a size-tolerance check: max of 50 MiB or 5% of expected size)
4. Shows a space check for remaining files
5. Downloads any incomplete or missing files, resuming `.partial` files where they exist

**Prompts:**

| Prompt | Description |
|---|---|
| `Resume these downloads? (y/n)` | Shown only if `kiwix_resume_update.txt` exists |
| `Verbose output? (y/n)` | Progress display mode |

**Tip:** If a download was interrupted, simply re-run Option 2. The script automatically detects `.partial` files and resumes from where it left off.

---

### Option 3 — Check for Updates {#option-3}

**When to use:** Periodically, to download newer versions of ZIM files and pick up newly available content groups.

**What it does:**

1. **Checks for an incomplete previous update session** — if `kiwix_resume_update.txt` exists, offers to resume it (skipping the server crawl entirely), or to run a fresh check
2. **Crawls the server** and compares the best available version of each group against what is on disk (using content date from filenames)
3. **Reports** the number of updates (newer versions of files you already have) and new groups (content you don't have yet), with sizes
4. **Checks for torrent availability** for each file to download
5. **Downloads** updates and new groups with per-file cleanup of the old version immediately after each new file completes verification
6. **Saves a resume file** (`kiwix_resume_update.txt`) if any downloads fail, so they can be resumed by running Option 3 again

**Prompts:**

| Prompt | Description |
|---|---|
| `r) Resume / n) Check for all updates` | Only shown if `kiwix_resume_update.txt` exists |
| `Download updates? (y/n)` | Whether to start downloading |
| `Verbose output? (y/n)` | Progress display mode |

**Important notes:**

- Option 3 uses **torrent downloads** where available, falling back to HTTP
- Cleanup of the old version happens **immediately** after each file is verified — so if the session is interrupted, already-completed updates are cleaned up
- If any downloads fail, `kiwix_resume_update.txt` is written automatically. Next time you run Option 3, you'll be offered the choice to resume it

---

### Option 4 — Cleanup ZIMs Directory {#option-4}

**When to use:** If you have accumulated older duplicate versions of ZIM files (e.g. after manually downloading without using Option 3's auto-cleanup).

**What it does:**

1. Scans `zims/` for files belonging to the same content group
2. For each group with multiple versions, shows which file will be **kept** (newest date) and which will be **deleted**
3. Shows the total space that will be freed
4. Asks for confirmation before deleting anything

**Confirmation prompt:**

```
Proceed with deletion? (y/n):
```

Answering `n` cancels without touching any files.

**Example output:**

```
Duplicate groups found: 2

  Group 'wikipedia_en_all_maxi':
    Keep   : wikipedia_en_all_maxi_2026-07.zim
    Delete : wikipedia_en_all_maxi_2026-04.zim (115.00 GiB)

  Group 'devdocs_en_axios':
    Keep   : devdocs_en_axios_2026-06.zim
    Delete : devdocs_en_axios_2026-02.zim (330.00 KiB)

  2 file(s) will be deleted, freeing 115.00 GiB.

Proceed with deletion? (y/n):
```

---

## Selection Rules

The crawler keeps only the most comprehensive English variant in each content group. Priority is:

1. `_all_maxi_` (highest)
2. Other comprehensive `_all_` forms
3. Everything else (only if no better option exists)

Hard exclusions:

| Rule | Effect |
|---|---|
| Wikipedia | Keep **only** `wikipedia_en_all_maxi_*`. Drop topic splits, `_mini_`, `_nopic_`, `_simple_all_`, `_top_*`, `_wp1_*`, etc. |
| Gutenberg | Keep **only** `gutenberg_en_all_*`. Drop all `gutenberg_en_lcc-*` letter splits. |
| Wiktionary | Keep `wiktionary_en_all_nopic_*` (the only comprehensive English option available). |
| FreeCodeCamp | Keep **only** `freecodecamp_en_all_*`. Drop all topic subset ZIMs. |
| Speedtest / diagnostics | Drop all `speedtest_*` files. |
| Other nopic | Drop most remaining `_nopic_*` variants. |
| Regional subsets | Drop `wikivoyage_en_europe_*` and similar partials. |

Within a group, files are ranked by: selection priority → content date (newer first) → size (larger first) → filename.

---

## Non-Interactive Update Mode (Cron Jobs) {#non-interactive-mode}

The script supports a fully non-interactive update mode suitable for automated scheduling via cron or any task scheduler.

### Usage

```bash
python3 kiwix_english_best_v20260822a.py -u
# or
python3 kiwix_english_best_v20260822a.py --update
```

### Behaviour

- **Exits silently** (exit code 0) if the `zims/` directory does not exist — this prevents cron from sending failure emails when the storage volume is not mounted (e.g. VeraCrypt volume not unlocked)
- If `kiwix_resume_update.txt` exists, resumes the incomplete session
- Otherwise crawls the server and downloads all updates and new groups
- Non-verbose output (clean for log files)
- Exits with code 1 if there is insufficient disk space or the server crawl fails

### Example Cron Entry

To run every Sunday at 3:00 AM and log output:

```bash
0 3 * * 0  python3 /path/to/kiwix_english_best_v20260822a.py -u >> /path/to/kiwix_cron.log 2>&1
```

**Important:** If your ZIM files are on an encrypted volume (e.g. VeraCrypt), the volume must already be mounted when the cron job runs. The script will exit gracefully if it is not.

---

## File Reference

| File | Description |
|---|---|
| `zims/` | Directory where all ZIM files are stored |
| `zims/*.zim` | Completed, verified ZIM files |
| `zims/*.zim.partial` | Incomplete downloads — safe to leave in place; will be resumed automatically |
| `zims/*.zim.fastresume` | Torrent resume data — deleted automatically on completion |
| `zims/corrupt/` | Files that failed integrity verification |
| `kiwix_english_best.txt` | Full best-file list generated by Option 1 |
| `kiwix_resume_update.txt` | Incomplete update session resume file — written by Option 3, deleted on full completion |
| `kiwix_download_failures.log` | Log of all download failures with timestamps and error details |

### List File Format

`kiwix_english_best.txt` and `kiwix_resume_update.txt` use a pipe-delimited format:

```
# Format: filename|size_human|size_bytes|url|torrent_url
wikipedia_en_all_maxi_2026-07.zim|115.00 GiB|123456789012|https://...|https://...torrent
```

Both old format (`filename|size_bytes|url|torrent_url`) and new format (`filename|size_human|size_bytes|url|torrent_url`) are accepted when loading a list.

---

## Download Methods

### Torrent (Preferred)

When a `.torrent` file is available on the Kiwix server for a ZIM file, the script downloads via BitTorrent using libtorrent. This is faster and more reliable for large files, as it distributes the load across multiple peers.

The progress display shows status, percentage, speed, and peer counts. Torrent downloads are **piece-verified** by libtorrent, so only a ZIM magic-header check is performed after completion (full `zimcheck` would be redundant and slow).

### HTTP (Fallback)

If no torrent is available, or if the torrent download fails, the script falls back to direct HTTP download from the configured mirrors. Resume is supported via HTTP Range requests — interrupted downloads pick up from the byte offset already on disk.

Default concurrency is **4** parallel downloads. Space checks include a safety buffer (default 10 GiB, or 10% of remaining required size, whichever is larger).

---

## Progress Display

### Verbose Mode

Each active download gets its own status line. A total progress bar tracks cumulative bytes downloaded versus the session total, with a byte-based ETA:

```
gutenberg_en_all_2025-11.zim - downloading | 12.4% | 5.2 MB/s | Peers: 11
khanacademy_en_all_2023-03.zim - downloading | 3.1% | 2.1 MB/s | Peers: 8
Total progress:  4%|▍ | 24.5G/609G [00:12:41, 780MB/s, ETA=01:15:22]
```

### Non-Verbose Mode

A single updating block shows active files plus the total bar, rewritten in place without scrolling.

### Post-Run Summary

After every download session, a summary is printed with good / corrupt counts and total disk usage under `./zims/`. Incomplete files (if any) are listed with bytes on disk and bytes remaining, plus a pointer to the appropriate resume option.

---

## Integrity Verification

After every successful download, the file is verified before being counted as complete:

| Download Method | Verification |
|---|---|
| Torrent | libtorrent piece-verification (all pieces checked during download); ZIM magic header check only after completion |
| HTTP | ZIM magic header check; `zimcheck -C -D` (structure and directory integrity) if `zimcheck` is installed |

**Note:** The `-I` flag (index check) is intentionally omitted from `zimcheck` for performance. Index integrity can be checked manually if needed:

```bash
zimcheck -I yourfile.zim
```

Files that fail verification are moved to `zims/corrupt/` and logged to `kiwix_download_failures.log`.

---

## Server Layout Note (August 2026)

As of August 2026, `https://download.kiwix.org/zim/` permanently redirects to the Kiwix Hub marketing site (`https://hub.kiwix.org/downloads/`), which does **not** provide a classic Apache directory listing.

This script therefore crawls:

```
https://lb.download.kiwix.org/zim/
```

which still serves the traditional index of category directories (`wikipedia/`, `gutenberg/`, `stack_exchange/`, `zimit/`, etc.). Individual file download URLs on `download.kiwix.org` continue to work.

If a future layout change removes the Apache `<pre>` listing from the configured base URL, the script prints:

```
WARNING: No Apache <pre> listing found at <url>
  The site layout may have changed. Check BASE_URL / mirrors.
```

and returns an empty result set instead of failing silently.

---

## Tips and Troubleshooting

### A download failed — how do I resume it?

- If the failure happened during an **Option 3** update session, re-run **Option 3**. You will be offered the chance to resume from `kiwix_resume_update.txt`.
- Otherwise re-run **Option 2**. The script detects `.partial` files and resumes automatically.

### The server crawl returns zero files

1. Confirm internet connectivity.
2. Confirm the script version is **v20260822a or later** (older versions still pointed at `download.kiwix.org/zim/`, which no longer has a parseable listing).
3. Look for the `WARNING: No Apache <pre> listing found…` message — that indicates a further site-layout change; update `BASE_URL` / `MIRRORS` in the script.

### The server crawl is very slow

The script scans each category subdirectory in sequence. This is normal — the server has many directories. A full scan typically takes one to a few minutes depending on network conditions.

### I see "Found 0 update(s)" but I know there are new files

Check that your on-disk files have dates in their filenames (e.g. `_2026-07`). Option 3 compares content dates parsed from filenames. Files without a recognisable `YYYY-MM` date pattern will not be compared correctly.

### Torrent downloads show 0 seeds / peers

Wait a minute or two — peers are discovered gradually. If activity remains at zero for several minutes, the torrent may be poorly seeded. The script will fall back to HTTP if the torrent path fails.

### A file ended up in zims/corrupt/

The file failed integrity verification. You can attempt to re-download it by running Option 3 (which will treat the missing completed `.zim` as needed) or by deleting the corrupt entry and running Option 2.

### How do I see what files are scheduled for download?

Open `kiwix_english_best.txt` in any text editor. Each line lists a filename, its human-readable size, byte size, HTTP URL, and torrent URL (when available).

### Space check says I need more room than expected

The required-space calculation treats incomplete / missing files as needing their full catalogued size, plus a safety buffer (default 10 GiB or 10% of remaining, whichever is larger). Completed files on disk contribute zero additional required space.

---

## License

MIT.
