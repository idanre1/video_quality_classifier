# Quality codes used by `vqc --rename`

`vqc --rename` puts a quality prefix in front of every video file name:

```
RR_<tier>-<grade>_<original name>
10_1080p-good_Movie.mkv
```

| part | meaning |
|---|---|
| `RR` | rank from `01` (best) to `24` (worst). A file explorer sorting A→Z lists the best videos first. |
| `<tier>` | resolution class: how many pixels of real picture the video has |
| `<grade>` | compression quality: how much data each pixel gets, adjusted for the codec |
| `<original name>` | unchanged |

The rank is fixed: `10` always means 1080p/good, no matter what else is in your library. Resolution counts first and grade second, so any 1080p file ranks above any 720p file.

## All codes

| code | tier | grade | example |
|---|---|---|---|
| `01` | 2160p | excellent | `01_2160p-excellent_Movie.mkv` |
| `02` | 2160p | good | `02_2160p-good_Movie.mkv` |
| `03` | 2160p | fair | `03_2160p-fair_Movie.mkv` |
| `04` | 2160p | poor | `04_2160p-poor_Movie.mkv` |
| `05` | 1440p | excellent | `05_1440p-excellent_Movie.mkv` |
| `06` | 1440p | good | `06_1440p-good_Movie.mkv` |
| `07` | 1440p | fair | `07_1440p-fair_Movie.mkv` |
| `08` | 1440p | poor | `08_1440p-poor_Movie.mkv` |
| `09` | 1080p | excellent | `09_1080p-excellent_Movie.mkv` |
| `10` | 1080p | good | `10_1080p-good_Movie.mkv` |
| `11` | 1080p | fair | `11_1080p-fair_Movie.mkv` |
| `12` | 1080p | poor | `12_1080p-poor_Movie.mkv` |
| `13` | 720p | excellent | `13_720p-excellent_Movie.mkv` |
| `14` | 720p | good | `14_720p-good_Movie.mkv` |
| `15` | 720p | fair | `15_720p-fair_Movie.mkv` |
| `16` | 720p | poor | `16_720p-poor_Movie.mkv` |
| `17` | 480p | excellent | `17_480p-excellent_Movie.mkv` |
| `18` | 480p | good | `18_480p-good_Movie.mkv` |
| `19` | 480p | fair | `19_480p-fair_Movie.mkv` |
| `20` | 480p | poor | `20_480p-poor_Movie.mkv` |
| `21` | low | excellent | `21_low-excellent_Movie.mkv` |
| `22` | low | good | `22_low-good_Movie.mkv` |
| `23` | low | fair | `23_low-fair_Movie.mkv` |
| `24` | low | poor | `24_low-poor_Movie.mkv` |

## Tier: resolution

The tier is decided by pixel count, measured as a 16:9-equivalent height √(width × height × 9/16), so portrait phone videos, landscape videos and cinema crops are all judged fairly.

| tier | equivalent height | typical sizes |
|---|---|---|
| 2160p | ≥ 1800 | 3840x2160, 2160x3840 (phone 4K) |
| 1440p | ≥ 1300 | 2560x1440, 1440x3200 (QHD+ phone) |
| 1080p | ≥ 860 | 1920x1080, 1080x1920, 1080x2400 (FHD+ phone), 1920x800 |
| 720p | ≥ 580 | 1280x720, 720x1600 (HD+ phone) |
| 480p | ≥ 400 | 854x480, 720x576, 540x960 |
| low | < 400 | 640x360 and smaller |

- **Without `--deep`**: the tier comes from the size stored in the file.
- **With `--deep`**: the tier comes from the *effective* resolution measured from the picture itself. A 480p DVD rip upscaled to 1080p therefore gets a 480p code even though the file says 1080p. Use `--deep --rename` for the most honest codes.

## Grade: compression

The grade comes from `norm bpp`: bits per pixel (bitrate ÷ (width × height × fps)), multiplied by a codec factor (H.264 1.0, HEVC 1.6, AV1 2.0, Xvid 0.65, MPEG-2 0.5, …) and adjusted for resolution, so it means "H.264-equivalent bits per pixel at 1080p".

| grade | norm bpp | what it looks like |
|---|---|---|
| excellent | ≥ 0.15 | transparent, no visible compression |
| good | 0.08 – 0.15 | typical good streaming/encode quality |
| fair | 0.04 – 0.08 | visible softness or artifacts in motion/dark scenes |
| poor | < 0.04 | starved: blocking, smearing |

With `--deep`, **heavy noise or grain lowers the grade one step** (measured noise ≥ 5, e.g. `excellent` → `good`). Noise makes the encoder spend many more bits, so a noisy file looks better by bitrate than it is. Light film grain doesn't count.

## Renaming rules

| case | what happens |
|---|---|
| file already has a vqc prefix | the old prefix is replaced, never stacked; reruns only touch files whose quality changed |
| recheck already-renamed files | use `--deep --force --rename` to purge their old cached scores, rerun picture checks and replace any outdated prefixes |
| subtitles / sidecars with the same name (`Movie.srt`, `Movie.en.srt`, `Movie.nfo`) | renamed together with the video, so players still find them |
| new name already taken | file skipped, listed under **Errors** in the report |
| file can't be renamed (open in another program, name too long) | the video and its sidecars are left unchanged, listed under **Errors**; rerun `--rename` later |
| cache | follows the rename, so nothing is analyzed again |

## Undo

Every `--rename` run writes `vqc_undo_rename.sh` next to the report. It strips the prefix from every renamed file (and its sidecars), restoring the original names:

```bash
sh vqc_undo_rename.sh
```
