# Quality codes used by `vqc --rename`

`vqc --rename` puts a quality prefix in front of every video file name:

```
RR_<tier>-<grade>_<original name>
10_1080p-good_Movie.mkv

RR_<tier>-<grade>_F-<letters>_<original name>  # deep mode, when flags are present
22_low-good_F-VB_Movie.mkv
```

| part | meaning |
|---|---|
| `RR` | rank from `01` (best) to `24` (worst). A file explorer sorting A→Z lists the best videos first. |
| `<tier>` | resolution class: how many pixels of real picture the video has |
| `<grade>` | codec-adjusted compression grade, with noise penalties in deep mode |
| `F-<letters>` | optional picture flags from `--deep`, explained below |
| `<original name>` | unchanged |

The rank is fixed: `10` always means 1080p/good, no matter what else is in your library. Resolution counts first and grade second, so any 1080p file ranks above any 720p file.

## Picture-flag letters

`--deep --rename` adds `F-` followed by the detected flag letters, always ordered **N/V, D, B, H, S**. With no flags, the entire `F-` segment is omitted.

| Letter | Flag | Condition |
|---|---|---|
| `N` | noisy | Spatial sigma >=3.5 and <5, or temporal coverage >20% and residual >=1.726 and <=3 |
| `V` | very noisy | Spatial sigma >=5, or temporal coverage >20% and residual >3; replaces `N`, never both |
| `D` | dark | Average luma < 32 |
| `B` | crushed blacks | At least 15% of centre pixels have luma <= 18 |
| `H` | clipped highlights | At least 5% of centre pixels have luma >= 233 |
| `S` | saturated | Mean chroma magnitude >= 30 |

For example, `22_low-good_F-VB_Movie.mp4` is very noisy with crushed blacks, while `10_1080p-good_F-DS_Movie.mp4` is dark and saturated. Temporal noise requires a successful, current-version measurement. It lowers the grade one step for `N`, two for temporal `V`; the old spatial-heavy-noise penalty remains one step. Use the larger penalty, never their sum, with `poor` as the floor. The flag is the stronger of spatial/temporal evidence, so a spatial-only `V` still means a one-step penalty. Letters themselves add no further penalty.

`F-` separates flags from titles such as `N_Movie.mp4`. A segment matching `F-<flag letters>_` immediately after the tier/grade is reserved for vqc. Legacy prefixes without flags remain supported.

Reruns replace the whole prefix, including old flags. Metadata-only `--rename` removes flag letters rather than treating cached picture statistics as fresh measurements. Subtitles receive the same letters as their video, and undo removes the complete prefix.

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

## How the code is decided

The tier uses nominal resolution without `--deep`, and measured effective resolution with it. The grade uses codec-adjusted bitrate; noise can lower it by up to two steps in deep mode. Temporal checks now run automatically with `--deep`. Cached/forced reruns recompute from the original metadata grade, never an already penalized grade. Neither label guarantees a particular visual appearance.

The formulas, thresholds and limitations live in [SCORING_TESTS.md](SCORING_TESTS.md). Installation, forced refresh and report pages live in [README.md](README.md).

## Renaming rules

| case | what happens |
|---|---|
| file already has a vqc prefix | the old prefix and flags are replaced, never stacked; reruns only touch files whose quality or flags changed |
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
