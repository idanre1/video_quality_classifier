# video_quality_classifier

Group a video library by resolution and compression quality rather than file size. Metadata checks account for codec efficiency; `--deep` also samples frames for blur, upscaling, noise and diagnostic picture flags. These are heuristics, not a universal perceptual-quality score.

## Documentation

| Guide | Details |
|---|---|
| [Implemented quality checks](SCORING_TESTS.md) | Current measurements, thresholds, NumPy picture statistics, limitations and completed validation |
| [Research](research.md) | Broader quality dimensions, alternatives, evidence and selection rationale |
| [Implementation plan](plan.md) | Approved prototype scope, phase progress, validation gates and private evaluation |
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
uv run vqc /lib --deep                  # picture checks + temporal noise flags/grade penalties
uv run vqc /lib --deep -o ~/lib.md       # choose the report location
uv run vqc /lib --deep --rename         # prefix videos and sidecars by quality
uv run vqc /lib --deep --force --rename # recheck already-renamed videos
uv run vqc /lib --noise-diagnostics -j 2 # experimental local temporal review; no new score penalties
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
| `--deep` | off | Effective resolution, picture stats and temporal noise; noise can lower grades by up to two steps |
| `--noise-diagnostics` | off | Temporal review without deep scoring; automatically included with `--deep` |
| `--frames N` | 8 | Sample positions: isolated picture-check frames plus four-frame temporal bursts with `--deep`; bursts only with diagnostics |
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

Tables show the file, display dimensions (including phone rotation), codec, bitrate, normalised bpp, duration and size. Deep reports add effective dimensions, detail, spatial noise, temporal residual/coverage, noise grade drop, flags and a snapshot. The grade drop is the requested penalty, clamped at `poor` in the final grade. Each snapshot is one image containing a scaled whole frame beside a 1:1 centre crop, with the sample timestamp in its tooltip. See [scoring details](SCORING_TESTS.md) for column interpretation.

Snapshots are exposed through `vqc_snapshots/` beside the reports, so Markdown previews can load them locally. Move that folder with the reports if needed; its symlinks still require the cached images.

### Experimental noise review

Both `--deep` and `--noise-diagnostics` write an additional `<report stem>_noise.md`, ordered by **temporal residual**, with pages of at most 200 videos. `--deep` now runs temporal checks automatically and uses the approved noise rules below. `--noise-diagnostics` alone remains a review-only command with metadata grades and no filename flags. The former spatial-variation diagnostic was removed after private testing showed clean fine detail ranking highly despite high eligibility.

| Temporal evidence (status `ok`, current algorithm version) | Flag with `--deep` | Grade penalty |
|---|---|---|
| Coverage **>20%**, residual **>=1.726 and <=3** | `N` noisy | One step |
| Coverage **>20%**, residual **>3** | `V` very noisy | Two steps |
| Lower coverage/residual, unavailable, unsupported, insufficient evidence or error | No temporal flag | No temporal penalty |

Use the larger of the temporal penalty and the old spatial-heavy-noise one-step penalty, **never their sum**, and never go below `poor`. The stronger spatial/temporal noise flag wins (`V` replaces `N`); therefore an old spatial `V` may still carry only a one-step penalty. Each run starts from the original metadata grade, so reruns do not compound penalties. These are user-approved library-specific thresholds, not universal calibration.

Existing valid temporal caches are reused; old deep caches missing temporal results are upgraded without repeating valid picture checks. `--force` is not required for this scoring upgrade. Add `--rename` only when ready to replace filename prefixes/flags.

The replacement compares adjacent frames within four-frame bursts, estimates integer camera translation, and measures Y/U/V residuals only in plausibly matched tiles. Stable fine detail cancels instead of contributing a spatial score. Cuts, exposure changes, unreliable local structure and exact repeats are excluded or reported separately. This does not denoise or modify videos.

| Report field | Interpretation |
|---|---|
| temporal residual | Robust changing-image signal, in 8-bit-equivalent levels; higher warrants playback review, with no validated good/bad threshold |
| reliable coverage | Accepted luma tiles across attempted pairs/scales, not affected duration or calibrated confidence |
| temporal flag | `N` / `V` under the rules above; informational only without `--deep` |
| pairs measured/rejected/repeated | Counts of usable comparisons, rejected comparisons and identical decoded frame pairs |
| status | `ok` means a measurement exists, not good quality; blank residual / insufficient evidence, unsupported or error never mean clean |

At least two measured pairs in one burst are required to produce a review score. Repeated/frozen noisy content can have little temporal change; no low score proves cleanliness. Complex/subpixel motion, changing lighting, intentional grain and compression remain confounds. Start with a small subset including your previous clean-detail false positives and known noisy clips.

Known-range, tagged SDR YUV/grayscale sources up to 4K pixel count are supported. Unknown transfer/range and HDR produce explicit unsupported status, not a clean result. The older spatial noise estimate remains available. Details, including frame timestamps/types, pair shifts/rejections/exposure deltas and channel/scale/brightness results, stay in the local cache.

Eight positions can decode up to 32 frames per video, with at most two diagnostic workers. Temporary decoded frames are local and automatically removed; allow approximately 100 MB temporary disk space per active 4K worker in addition to processing memory. This is slower than isolated spatial samples; use `--frames 3 -j 1` for a smaller first trial, with reduced sampling coverage.

`--noise-diagnostics` never downloads resdet or models, and restricts source probing/decoding to local file/pipe protocols. If combined with `--deep`, only an already-installed or explicitly provided resdet is used; otherwise deep scoring uses SSIM alone. Plain `--deep` retains its existing automatic resdet setup, while temporal decoding itself stays local. Omit `--rename` to keep filenames unchanged. See [plan.md](plan.md) for evaluation and gates.

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
uv run python -m unittest -v test_vqc test_noise_diagnostics
```

See [validation and calibration](SCORING_TESTS.md#validation-tests-distinct-from-scoring-checks) for what these tests prove and what they do not.
