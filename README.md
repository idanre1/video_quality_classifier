# video_quality_classifier

Group a video library by resolution and compression quality rather than file size. Metadata checks account for codec efficiency; `--deep` also samples frames for blur, upscaling, noise and diagnostic picture flags. These are heuristics, not a universal perceptual-quality score.

## Documentation

| Guide | Details |
|---|---|
| [Scoring checks and tests](SCORING_TESTS.md) | Measurements, thresholds, NumPy picture statistics, limitations, calibration results and validation |
| [Quality codes and renaming](QUALITY_CODES.md) | All 24 filename codes, prefix replacement, subtitles, conflicts and undo |
| This README | Installation, commands, report pages, cache and network-drive operation |

## Requirements and installation

| Need | Purpose |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Runs `vqc` inside its uv environment; plain `python3 vqc.py` refuses to start |
| ffmpeg (`ffprobe`, `ffmpeg`) | Metadata probing and frame decoding |
| C compiler, `make`, `bash`, access to github.com (optional) | Builds [resdet](https://github.com/0x09/resdet) on the first deep run; on Debian/Ubuntu, `sudo apt install build-essential` |

uv installs NumPy and tqdm automatically. If resdet cannot be built, a warning identifies the failing step and the log at `<venv>/resdet-build.log`; processing continues with SSIM only. `--help` works without the uv environment.

| Usage | Command |
|---|---|
| From the repository | `uv run vqc /path/to/library` |
| Install a global command | `uv tool install /path/to/video_quality_classifier` |
| Update a global installation after editing | `uv tool install --reinstall /path/to/video_quality_classifier` |

```bash
uv run vqc /lib                         # metadata only
uv run vqc /lib --deep                  # picture checks too
uv run vqc /lib --deep -o ~/lib.md       # choose the report location
uv run vqc /lib --deep --rename         # prefix videos and sidecars by quality
uv run vqc /lib --deep --force --rename # recheck already-renamed videos
```

## Command-line options

| Option | Default | Purpose |
|---|---|---|
| `root` | required | Library folder to scan recursively |
| `-o`, `--report PATH` | `video_quality.md` | Markdown report or overview path |
| `-j`, `--jobs N` | min(16, 2 x cores) | Parallel analysis jobs; lower on slow network shares |
| `--cache PATH` | `~/.cache/vqc/cache.jsonl` | Shared results cache |
| `--force` | off | Purge scanned videos' old cache entries and reanalyse them |
| `--ext LIST` | common video types | Extension filter, e.g. `mkv,mp4` |
| `--deep` | off | Measure effective resolution, noise, brightness and colour flags |
| `--frames N` | 8 | Requested sample count per video with `--deep` |
| `--rename` | off | Apply the quality prefix, including `F-` picture-flag letters in deep mode; see [renaming rules](QUALITY_CODES.md#renaming-rules) |
| `--resdet PATH` | `<venv>/bin/resdet` | Override the auto-built binary; a missing/non-executable path is an error |
| `-h`, `--help` | | Show help |

Analysis and renaming have progress bars showing counts, rate, ETA and errors.

## Reports

The requested report contains the command/options, library totals, quality-group summary and any errors. Groups are ordered best-first, and files within a group by descending normalised bitrate.

**Every Markdown file contains at most 200 pictures.** Up to 200 videos are kept in one report. Larger scans produce:

```text
video_quality.md             # overview, full summary, errors and page links
video_quality_part001.md     # first 200 videos
video_quality_part002.md     # next 200 videos
...
```

Each detail page has at most 200 video rows, with links to the overview and adjacent pages. Pagination also applies to metadata-only reports. Existing page filenames are overwritten when reused; surplus pages from older, larger runs are not deleted and are no longer linked from the current overview.

Tables show the file, display dimensions (including phone rotation), codec, bitrate, normalised bpp, duration and size. Deep reports add effective dimensions, detail, noise, diagnostic flags and a snapshot. Each snapshot is one image containing a scaled whole frame beside a 1:1 centre crop, with the sample timestamp in its tooltip. See [scoring details](SCORING_TESTS.md) for column interpretation.

Snapshots are exposed through `vqc_snapshots/` beside the reports, so Markdown previews can load them locally. Move that folder with the reports if needed; its symlinks still require the cached images.

## Cache and forced refresh

| Topic | Behaviour |
|---|---|
| Location | `~/.cache/vqc/cache.jsonl`, or `$XDG_CACHE_HOME/vqc/cache.jsonl`; override with `--cache` |
| Key | Absolute video path, size and modification time |
| Reuse | Unchanged files are skipped; metadata entries are upgraded for deep runs; metadata-only runs ignore cached deep scoring |
| `--force` | Removes all cached size/mtime versions for scanned videos, including original names and previous vqc-prefixed names, before reanalysis; unrelated videos stay cached |
| Failed forced analysis | A failed probe does not restore the old result; a failed deep check keeps fresh metadata and reports an error |
| Images | Deep snapshots live in `snapshots/` beside the cache; old images are retained so previous reports can still reference them |
| Reset | Delete the `vqc` cache folder to reset everything |

`--force` alone reruns metadata checks. Add `--deep` for picture checks and `--rename` to update filenames. Avoid concurrent runs sharing the cache during a forced refresh.

## WSL / Windows network drives

| Topic | Guidance |
|---|---|
| Mapped drive | `/etc/fstab`: `Z: /mnt/z drvfs defaults,uid=1000,gid=1000 0 0`; `metadata` is not required |
| Direct share | `sudo mount -t drvfs '\\server\share' /mnt/z` avoids depending on a mapped drive letter |
| Empty `/mnt/z` | Windows may not have reconnected Z: when WSL started; check `mount \| grep /mnt/z` |
| Locked video or sidecar | Close players/preview windows and rerun; failed renames are reported and rolled back where possible |
| Report on the share | If symlinks are refused, snapshot JPEGs are copied beside the reports |
| Performance | Keep the cache on the Linux side and lower `--jobs` on slow shares |

## Development checks

```bash
uv run python -m unittest -v test_vqc
```

See [validation and calibration](SCORING_TESTS.md#validation-tests-distinct-from-scoring-checks) for what these tests prove and what they do not.
