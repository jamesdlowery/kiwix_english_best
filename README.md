# Kiwix English Best

A Python script that automates the selection, downloading, and maintenance of the best available English-language [ZIM files](https://wiki.openzim.org/wiki/ZIM_file_format) from [download.kiwix.org](https://download.kiwix.org/zim/). ZIM files are compressed offline archives used by the [Kiwix](https://www.kiwix.org) reader to browse Wikipedia, Stack Exchange, Project Gutenberg, and hundreds of other resources without an internet connection.

## Features

- Crawls the Kiwix server and selects the best (newest, highest-quality) English ZIM for each content group
- Downloads via **BitTorrent** (libtorrent) where available, with **HTTP** fallback and resume support
- Verifies file integrity after every download
- Detects and downloads updates when newer versions appear on the server
- Cleans up older duplicate versions automatically
- Non-interactive **cron mode** (`-u` / `--update`) for automated scheduling

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
python3 kiwix_english_best.py
```

You'll see the interactive menu:

```
Kiwix English ZIM Tool v20260405a

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

## Cron / Automated Updates

Run non-interactively with `-u` or `--update`:

```bash
python3 kiwix_english_best.py -u
```

- Exits silently if the `zims/` directory doesn't exist (e.g. encrypted volume not mounted)
- Resumes any incomplete update session automatically
- Otherwise crawls for updates and downloads them

Example cron entry (every Sunday at 3:00 AM):

```
0 3 * * 0  python3 /path/to/kiwix_english_best.py -u >> /path/to/kiwix_cron.log 2>&1
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

Full user guide available in the repository in four formats:

- [`kiwix_english_best_user_guide.md`](kiwix_english_best_user_guide.md) — Markdown
- [`kiwix_english_best_user_guide.html`](kiwix_english_best_user_guide.html) — HTML
- [`kiwix_english_best_user_guide.pdf`](kiwix_english_best_user_guide.pdf) — PDF
- [`kiwix_english_best_user_guide.docx`](kiwix_english_best_user_guide.docx) — Word

## License

This project is released for personal use. No warranty is provided. Use responsibly and in accordance with [Kiwix's terms of service](https://www.kiwix.org/en/terms-of-service/).
