# Kiwix English Best

A Python script that automates the selection, downloading, and maintenance of the best available English-language [ZIM files](https://wiki.openzim.org/wiki/ZIM_file_format) from the [Kiwix download mirrors](https://lb.download.kiwix.org/zim/). ZIM files are compressed offline archives used by the [Kiwix](https://www.kiwix.org) reader to browse Wikipedia, Stack Exchange, Project Gutenberg, and hundreds of other resources without an internet connection.

**Current version:** `v20260822a`

## Features

- Crawls the Kiwix server and selects the best (newest, highest-quality) English ZIM for each content group
- Downloads via **BitTorrent** (libtorrent) where available, with **HTTP** fallback and resume support
- Verifies file integrity after every download
- Detects and downloads updates when newer versions appear on the server
- Cleans up older duplicate versions automatically
- Non-interactive **cron mode** (`-u` / `--update`) for automated scheduling
- Binary size reporting (KiB / MiB / GiB / TiB) throughout
- Tolerant “already complete” detection so near-identical on-disk sizes are not re-downloaded

## Requirements

| Package | Notes |
|---|---|
| Python 3.8+ | Required |
| `requests` | `pip install requests` |
| `tqdm` | `pip install tqdm` — optional, for progress bars |
| `libtorrent` | Optional — enables torrent downloads |
| `zimcheck` | Optional — enables deep ZIM integrity checking (HTTP downloads only) |

```bash
pip install requests tqdm
# Optional torrent support (Debian/Ubuntu):
sudo apt install python3-libtorrent
```

## Quick Start

```bash
python3 kiwix_english_best_v20260822a.py
```

You'll see the interactive menu:

```
Kiwix English ZIM Tool v20260822a

1) Generate / download new best English ZIMs list
2) Download / resume from existing best English ZIMs list
3) Check for updates (new and newer ZIMs; includes cleanup)
4) Cleanup ZIMs directory (remove older duplicate versions)
Choose (1/2/3/4):
```

ZIM files are stored in a `zims/` subdirectory relative to the script.

## Menu Options

| Option | Description |
|---|---|
| **1** | Crawl the server, select the best file per content group, save the list, and optionally download |
| **2** | Resume or complete downloads from the saved list; detects and resumes `.partial` files |
| **3** | Check for newer versions and new content groups; download updates with auto-cleanup of old versions |
| **4** | Preview and remove older duplicate versions from the `zims/` directory |

## Selection Rules (best-of-group)

The crawler keeps only the most comprehensive English variant in each content group:

| Category | Kept | Dropped |
|---|---|---|
| Wikipedia | `wikipedia_en_all_maxi_*` only | topic splits, `_mini_`, `_nopic_`, `_simple_all_`, `_top_*`, etc. |
| Gutenberg | `gutenberg_en_all_*` only | all `gutenberg_en_lcc-*` letter splits |
| Wiktionary | `wiktionary_en_all_nopic_*` (only comprehensive English option) | other nopic / partial variants |
| FreeCodeCamp | `freecodecamp_en_all_*` only | topic subset ZIMs |
| General | — | `speedtest_*`, `wikivoyage_en_europe_*`, most other `_nopic_*` |

## Server layout note (August 2026)

`https://download.kiwix.org/zim/` now redirects to the Kiwix Hub marketing site and no longer serves a classic Apache directory listing. This script therefore uses:

```
https://lb.download.kiwix.org/zim/
```

as its primary crawl base. Category subdirectories (`wikipedia/`, `gutenberg/`, etc.) remain fully available. If the layout changes again, the script prints a clear warning instead of silently returning zero files.

## Cron / Automated Updates

Run non-interactively with `-u` or `--update`:

```bash
python3 kiwix_english_best_v20260822a.py -u
```

- Exits silently if the `zims/` directory doesn't exist (e.g. encrypted volume not mounted)
- Resumes any incomplete update session automatically
- Otherwise crawls for updates and downloads them

Example cron entry (every Sunday at 3:00 AM):

```
0 3 * * 0  python3 /path/to/kiwix_english_best_v20260822a.py -u >> /path/to/kiwix_cron.log 2>&1
```

## File Reference

| File | Description |
|---|---|
| `zims/` | ZIM file storage directory |
| `zims/*.zim.partial` | Incomplete downloads — resumed automatically |
| `kiwix_english_best.txt` | Generated best-file list (Option 1) |
| `kiwix_resume_update.txt` | Incomplete update session resume file (Option 3) |
| `kiwix_download_failures.log` | Failure log with timestamps and error details |

## Documentation

Full user guide available in multiple formats:

- [`user_guide.md`](user_guide.md) — Markdown
- [`user_guide.docx`](user_guide.docx) — Microsoft Word
- [`user_guide.pdf`](user_guide.pdf) — PDF
- [`user_guide.odt`](user_guide.odt) — OpenDocument Text

## License

MIT — see [`LICENSE`](LICENSE) for details.
