# Kiwix English Best — User Guide

**Version:** v20260403a  
**Last Updated:** April 3, 2026

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
5. [Non-Interactive Update Mode (Cron Jobs)](#non-interactive-mode)
6. [File Reference](#file-reference)
7. [Download Methods](#download-methods)
8. [Progress Display](#progress-display)
9. [Integrity Verification](#integrity-verification)
10. [Tips and Troubleshooting](#tips-and-troubleshooting)

---

## Overview

**Kiwix English Best** is a Python script that automates the selection, downloading, and maintenance of the best available English-language ZIM files from [download.kiwix.org](https://download.kiwix.org/zim/). ZIM files are compressed offline archives used by the [Kiwix](https://www.kiwix.org) reader to browse Wikipedia, Stack Exchange, Project Gutenberg, and hundreds of other resources without an internet connection.

The script:

- Crawls the Kiwix server to find the best (newest, highest-quality) English ZIM file for each content group
- Downloads files using BitTorrent (via libtorrent) where available, with HTTP as a fallback
- Supports resuming interrupted downloads
- Verifies file integrity after download
- Detects and downloads updates when newer versions are available on the server
- Cleans up older duplicate versions to save disk space

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
python3 kiwix_english_best.py
```

You will see the main menu:

```
Kiwix English ZIM Tool v20260403a

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
2. Selects the best (newest date, then largest) file per content group, excluding unwanted variants (nopic, mini, lcc-*, etc.)
3. Saves the complete best list to `kiwix_english_best.txt` — this always happens, even if all files are already present on disk
4. Checks which files from the list are not yet downloaded
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
3. Filters out files already present on disk (using a size-tolerance check)
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
    Keep   : wikipedia_en_all_maxi_2026-03.zim
    Delete : wikipedia_en_all_maxi_2026-02.zim (115.00 GiB)

  Group 'devdocs_en_axios':
    Keep   : devdocs_en_axios_2026-02.zim
    Delete : devdocs_en_axios_2025-10.zim (407.00 KiB)

  2 file(s) will be deleted, freeing 115.00 GiB.

Proceed with deletion? (y/n):
```

---

## Non-Interactive Update Mode (Cron Jobs) {#non-interactive-mode}

The script supports a fully non-interactive update mode suitable for automated scheduling via cron or any task scheduler.

### Usage

```bash
python3 kiwix_english_best.py -u
# or
python3 kiwix_english_best.py --update
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
0 3 * * 0  python3 /path/to/kiwix_english_best.py -u >> /path/to/kiwix_cron.log 2>&1
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
wikipedia_en_all_maxi_2026-03.zim|115.00 GiB|123456789012|https://download.kiwix.org/...|https://...torrent
```

---

## Download Methods

### Torrent (Preferred)

When a `.torrent` file is available on the Kiwix server for a ZIM file, the script downloads via BitTorrent using libtorrent. This is faster and more reliable for large files, as it distributes the load across multiple peers.

The progress display shows seeds, peers, and distributed copies:

```
wikipedia_en_all_maxi_2026-03.zim - Downloading  |  45.2% |  10.5 MB/s | Seeds: 24/26 | Copies: 24.05
```

Torrent downloads are **piece-verified** by libtorrent, so the ZIM header check is performed instead of running `zimcheck` (which would be redundant and slow).

### HTTP (Fallback)

If no torrent is available, or if the torrent download fails, the script falls back to direct HTTP download from the Kiwix mirrors. Resume is supported via HTTP Range requests — interrupted downloads pick up from the byte offset already on disk.

---

## Progress Display

### Verbose Mode

Each active download gets its own line, updated in real time. A total progress bar appears below:

```
coreyms_en_python-tutorials_2026-04.zim - Downloading  |  78.4% |  733.5 KB/s | HTTP
www.ready.gov_en_2024-12.zim - Downloading              |  98.3% |  6503.4 KB/s | HTTP
Total progress:  86%|#########5 | 4.53G/5.30G [00:29:07, 2.66 MiB/s, ETA=00:04:55]
```

When a file completes, its status changes to `Completed` before disappearing from the display.

### Non-Verbose Mode

A single updating block shows all active files plus the total bar, rewritten in place without scrolling.

### Post-Run Summary

After every download session, a summary is printed:

```
Post-run summary:
  - Total files in list: 3
  - Good / verified files: 585
  - Corrupt files detected: 0
  - Total size on disk (good files): 1.10 TiB
  - Corrupt files size: 0.00 B
  - Disk usage in ./zims/: 1.10 TiB
```

If any downloads were incomplete, they are listed with bytes on disk and bytes remaining:

```
  ⚠ Incomplete download(s) — 1 file(s) not fully downloaded:
    www.ready.gov_en_2024-12.zim.partial (2.26 GiB on disk, 37.85 MiB remaining)

    → Run option 3 to resume these downloads.
```

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

## Tips and Troubleshooting

### A download failed — how do I resume it?

Run **Option 3**. If a resume file exists, you'll be offered the choice to resume the incomplete session. The `.partial` file in `zims/` will be picked up automatically.

### The server crawl is very slow

The script scans each subdirectory of `download.kiwix.org/zim/` in sequence. This is normal — the server has hundreds of directories. The scan typically takes 1–3 minutes.

### I see "Found 0 update(s)" but I know there are new files

Check that your on-disk files have dates in their filenames (e.g. `_2026-02`). Option 3 compares content dates parsed from filenames. Files without a recognisable date pattern will not be compared correctly.

### Torrent downloads show 0 seeds

Wait a minute or two — peers are discovered gradually. If seeds remain at 0 for several minutes, the torrent may be poorly seeded. The script will fall back to HTTP if the torrent stalls.

### A file ended up in zims/corrupt/

The file failed integrity verification. You can attempt to re-download it by running Option 3, which will detect that the completed `.zim` is missing for that group and offer it as an update. Alternatively, delete the corrupt file manually and run Option 2 to re-download it.

### How do I see what files are scheduled for download?

Open `kiwix_english_best.txt` in any text editor. Each line lists a filename, its human-readable size, byte size, HTTP URL, and torrent URL.
