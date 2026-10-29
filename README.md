# video_quality_classifier

Sorts a large video library by **actual picture quality**, not file size, and groups files of the same quality.

File size is misleading: old codecs (Xvid, MPEG-2) need far more data for the same picture, long videos are big regardless, and a high-bitrate file can still be an upscaled or blurry copy. `vqc` measures quality from stream metadata and, with `--deep`, from the frames themselves.

## Requirements

| need | why |
|---|---|
| [uv](https://docs.astral.sh/uv/) | `vqc` runs only inside its uv venv; plain `python3 vqc.py` refuses to start |
| ffmpeg (`ffprobe`, `ffmpeg`) | metadata probing and frame sampling |
| C compiler, `make`, `bash`, access to github.com (optional) | to build [resdet](https://github.com/0x09/resdet) automatically on the first `--deep` run (`sudo apt install build-essential`). No `pkg-config` or extra libraries needed. If the build fails, the warning names the failing step and the full log is in `<venv>/resdet-build.log` |

The only Python dependency is `tqdm` (progress bar), installed automatically by uv. The progress bar shows percent, ETA, files per second, how many came from the cache, the error count, and the file just finished.

## Run

| how | command |
|---|---|
| from the repo folder | `uv run vqc /path/to/library` |
| as a global command | `uv tool install /path/to/video_quality_classifier`, then `vqc /path/to/library` |
| after editing the code (global install) | `uv tool install --reinstall /path/to/video_quality_classifier` |

```bash
uv run vqc /lib                        # metadata only -> video_quality.md
uv run vqc /lib --deep                 # also detect blurry / upscaled files
uv run vqc /lib --deep -o ~/lib.md     # report somewhere else
uv run vqc /lib --deep --rename        # rename files best-first: 10_1080p-good_Movie.mkv
```

## Flags

| flag | default | description |
|---|---|---|
| `root` | required | library folder to scan (recursive) |
| `-o`, `--report PATH` | `video_quality.md` | Markdown report path |
| `-j`, `--jobs N` | min(16, 2 × cores) | files analysed in parallel; lower it on slow NAS shares |
| `--cache PATH` | `~/.cache/vqc/cache.jsonl` | results cache (see [Cache](#cache)) |
| `--ext LIST` | common video types | only these extensions, e.g. `mkv,mp4` |
| `--deep` | off | sample frames to measure effective resolution; groups use it instead of the nominal one |
| `--frames N` | 8 | frames sampled per file with `--deep` |
| `--rename` | off | prefix each file (and its subtitles/sidecars) with its quality rank so an A→Z sort lists the best first, e.g. `10_1080p-good_Movie.mkv`; writes `vqc_undo_rename.sh` next to the report. Codes explained in [QUALITY_CODES.md](QUALITY_CODES.md) |
| `--resdet PATH` | `<venv>/bin/resdet` | use this resdet binary instead of the auto-built one; a wrong path is an error |
| `-h`, `--help` | — | show help (works without uv) |

## Output

Each file gets a **group** `<tier>/<grade>`, e.g. `1080p/good`. The same group summary is printed to the terminal. The Markdown report has:

| section | content |
|---|---|
| header | library path, file count, total size, date |
| Options | the exact command, mode (**deep** with frames + resdet path, or **metadata only**), extensions, files found / from cache / analyzed now / errors, cache and snapshot paths |
| Summary | one row per group: files, size GB (best groups first) |
| one section per group | table of its files, best `norm bpp` first |
| Errors | files that couldn't be read, with the reason (only if any) |

| term / column | meaning |
|---|---|
| tier | `2160p` `1440p` `1080p` `720p` `480p` `low`, from pixel count, so it works for any orientation and aspect ratio (see [Tiers](#tiers)); with `--deep`, from the *effective* resolution |
| grade | from `norm bpp`: `excellent` ≥ 0.15, `good` ≥ 0.08, `fair` ≥ 0.04, `poor` below |
| file | path relative to the library folder |
| resolution | as displayed: phone videos stored rotated (iPhone) are shown upright, e.g. `1080x1920` |
| codec, Mb/s | from the video stream |
| norm bpp | bits per pixel (bitrate ÷ (width × height × fps)) × codec efficiency × (pixels / 1080p)^0.25, i.e. H.264-equivalent bpp at 1080p |
| effective | `--deep`: resolution the picture really has: resdet's exact source size if it finds an upscale, otherwise the round-trip SSIM estimate |
| detail | `--deep`: 1 − SSIM at 0.75× round trip; ~0.03–0.06 sharp 1080p camera footage, < ~0.015 upscaled/blurred |
| snapshot | `--deep`: embedded image of the sampled frame that decided the result: the whole frame scaled down, next to a 1:1 centre crop at the video's own pixels (blur and upscaling are visible there). Hover for the timestamp |
| min, MB | duration and file size |

## Tiers

The tier comes from the 16:9-equivalent height √(width × height × 9/16), so portrait, landscape, phone 20:9 and cinemascope crops are all judged by pixel count:

| tier | equivalent height | examples (either orientation) |
|---|---|---|
| 2160p | ≥ 1800 | 3840x2160, 2160x3840 phone 4K, 3840x1600 |
| 1440p | ≥ 1300 | 2560x1440, 1440x3200 / 1440x3088 (QHD+ phones), 1290x2796 |
| 1080p | ≥ 860 | 1920x1080, 1080x1920, 1080x2400 / 1080x2340 (FHD+ phones), 1170x2532, 1920x800, 1440x1080 |
| 720p | ≥ 580 | 1280x720, 720x1280, 720x1600 (HD+ phones), 750x1334, 1280x536 |
| 480p | ≥ 400 | 854x480, 640x480, 720x576, 540x960, 576x1024 |
| low | below | 640x360, 360x640 |

## How quality is measured

| check | mode | how | catches |
|---|---|---|---|
| bits per pixel | always | `ffprobe` bitrate ÷ pixels per second, × codec factor (H.264 1, HEVC 1.6, AV1 2.0, VP9 1.5, Xvid 0.65, MPEG-2 0.5, …) | starved / over-compressed encodes, old inefficient codecs |
| round-trip SSIM | `--deep` | downscale each sampled frame by 0.75, 0.5, 0.333 and back; if SSIM stays ~1, there was no detail above that height. The 2nd most detailed frame decides | upscales, soft focus, blur |
| resdet | `--deep` | frequency-spectrum analysis of the sampled frames | exact source height of bicubic/Lanczos/etc. upscales |

`--deep` costs about 6 CPU-seconds per file (fast seeks, not a full decode). resdet 2.4.4 is downloaded and built into the venv on the first `--deep` run (~1 s); if the build fails, `vqc` prints the fix and continues with SSIM only.

Codec factors, grade thresholds and `--deep` thresholds (`DEEP_SCALES`) are constants at the top of [vqc.py](vqc.py).

## Cache

| | |
|---|---|
| location | `~/.cache/vqc/cache.jsonl` (`$XDG_CACHE_HOME/vqc/` if set); override with `--cache` |
| key | absolute path + size + modification time, so one cache serves all your libraries |
| reuse | unchanged files are skipped; metadata-only results are upgraded when you later run `--deep`; a run without `--deep` ignores cached deep results |
| snapshots | `--deep` saves one small JPEG strip per file (~10–60 KB) to `snapshots/` next to the cache. The report embeds them from a `vqc_snapshots/` folder beside it that holds only symlinks into the cache, so nothing is copied (Markdown previews such as VS Code's only load images under the workspace or the report's folder) |
| reset | delete the `vqc` cache folder |

## Accuracy

Tested on Xiph SVT 1080p camera sequences (crowd_run, park_joy, ducks_take_off, old_town_cross), each encoded with x264 crf 14 (35–150 Mb/s) as these variants:

| variant | without `--deep` | with `--deep` |
|---|---|---|
| original | 1080p/excellent | 1080p (4/4) |
| upscaled from 720p | 1080p/excellent | 720p (4/4) |
| upscaled from 540p / 360p | 1080p/excellent | 480p (8/8) |
| Gaussian blur σ = 1 | 1080p/excellent | 720p (3/4), 480p (1/4) |
| denoised (hqdn3d) | 1080p/excellent | 1080p (4/4), not flagged |

resdet found all 12 upscales at the exact height, with no false positives on the originals.

## Limits

| case | effect |
|---|---|
| no `--deep` | metadata only; can't see upscales or blur |
| denoising / smoothing that keeps edges | not flagged |
| blocking or banding at low bitrate | only roughly covered by the bpp grade |
| very flat content (sky, water, flat animation), especially short clips | may be rated lower than it is |
| AI upscalers (Topaz etc.), grain added after upscaling | can hide an upscale from both checks |
| synthetic graphics (test patterns, screen recordings) | can mislead resdet |

## Other tools considered

Tested on the same clips before choosing the `--deep` method ([overview](https://www.forasoft.com/learn/video-quality/articles-vqm/open-source-no-reference-tools)):

| tool | result |
|---|---|
| ffmpeg `blurdetect` | score tracks content more than blur; ~3 s per sampled second |
| [DOVER](https://github.com/VQAssessment/DOVER) | ranked the 720p upscale *above* the original in 4/4 scenes |
| [pyiqa](https://github.com/chaofengc/IQA-PyTorch) TOPIQ / MUSIQ | right order within a scene, but scores overlap across scenes; rated denoised above originals |
| pyiqa Q-Align (top no-reference model in pyiqa's benchmark) | resizes to 448 px, so blind to resolution loss; ~100 s per crop on CPU |

The PyTorch models also need a GPU to be practical, and DOVER and pyiqa are licensed for non-commercial use.

## Sources

- [Streaming Learning Center: Power of .75 rule](https://streaminglearningcenter.com/blogs/five-checks-for-your-encoding-ladder.html): bitrate scales ~pixels^0.75; 0.1–0.15 bpp is typical for H.264.
- Wikipedia: [HEVC](https://en.wikipedia.org/wiki/High_Efficiency_Video_Coding), [AV1](https://en.wikipedia.org/wiki/AV1), [AVC](https://en.wikipedia.org/wiki/Advanced_Video_Coding): codec efficiency ratios.
- [video-quality-scanner](https://github.com/sisyphosloughs/video-quality-scanner): similar tool, groups by frame height only.
