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

## Recommended Offline Resource Libraries

Several popular prepper and survival-focused websites stand out for offering extensive collections of free downloadable materials — PDFs, manuals, guides, checklists, and military/survival documents. These are often hosted directly or curated to avoid sketchy third-party links, and many emphasize offline storage (e.g. on USB drives).

Here are some of the most frequently recommended sites with large collections of downloadable content:

- **[TruePrepper](https://trueprepper.com)** — Features a dedicated "Free Survival PDFs, Manuals, & Downloads" library with hundreds of hosted files, including military manuals, checklists, and survival guides. They host everything themselves for safe, direct downloads and regularly update the collection.

- **[City Prepping](https://cityprepping.com)** — Offers a "Prepper's Free PDF Library" section with categorized free PDFs, including emergency quick guides, survival manuals, traditional skills resources, and links to more extensive collections. A solid starting point for building an offline electronic library.

- **[The Prepared](https://theprepared.com)** — A highly regarded site for rational, research-based prepper advice, with checklists, guides, and downloadable resources. Community-focused and often recommended for practical, no-nonsense materials.

Other notable mentions from prepper communities include:

- Sites aggregating military and government manuals, such as those linked from **[Off Grid Survival](https://offgridsurvival.com)** or Seasoned Citizen Prepper archives, which point to large collections of free PDFs (e.g. Army Ranger Handbooks, FEMA guides).
- **[Reddit r/PrepperFileShare](https://www.reddit.com/r/PrepperFileShare/)** — Community-driven shares where users post and discuss torrents, direct links, and massive e-libraries of survival PDFs.
- Older but still referenced collections like **Survivor Library** and various forum download sections (e.g. [survivalistboards.com](https://www.survivalistboards.com)), though availability can vary — always verify current links.

---

## License

MIT — see [`LICENSE`](LICENSE) for details.

**[Ready.gov](https://www.ready.gov)** also offers free official government preparedness PDFs, including basic emergency plans and guides.

Many preppers recommend downloading these resources now and storing them offline, as internet access may not always be reliable in an emergency. Always cross-check for the latest versions, as some older links evolve or move over time.
