# Checks used in video scoring

`vqc` assigns a **resolution tier** and a **grade**, not a single perceptual-quality score. Resolution ranks first: every 1080p group ranks above every 720p group. The bitrate grade is a compression heuristic, not proof that the picture is good.

## 1. Stream metadata: always run

`ffprobe` selects the largest video stream, excluding attached cover pictures, and reads its dimensions, frame rate, codec and bitrate. Bitrate comes from stream metadata, then stream tags, then a container estimate with known audio bitrate subtracted.

```text
bpp = video bitrate / (width * height * fps)
norm_bpp = bpp * codec efficiency * (width * height / (1920 * 1080)) ** 0.25
```

Codec efficiency makes the result approximately H.264-equivalent: H.264 = 1.0, HEVC = 1.6, AV1 = 2.0, VP9 = 1.5, MPEG-2 = 0.5. The full table is in [vqc.py](vqc.py).

| Grade | Minimum norm_bpp |
|---|---:|
| excellent | 0.15 |
| good | 0.08 |
| fair | 0.04 |
| poor | below 0.04 |

The nominal resolution tier uses the 16:9-equivalent height `sqrt(width * height * 9 / 16)`. This works across landscape, portrait and cinema crops.

| Tier | Minimum equivalent height |
|---|---:|
| 2160p | 1800 |
| 1440p | 1300 |
| 1080p | 860 |
| 720p | 580 |
| 480p | 400 |
| low | below 400 |

**What this catches:** low-bitrate encodes and differences in codec efficiency.

**What it misses:** blur, upscaling, noise and damage already baked into a high-bitrate re-encode. Noise can inflate bitrate and make this grade misleading.

## 2. Frame sampling: `--deep`

By default, eight frames are sought at evenly spaced points between roughly 5% and 95% of the video's duration. Videos lasting at most two seconds, or with unknown duration, use one frame at time zero. `--frames N` changes the requested sample count.

Frames are decoded to 8-bit YUV 4:2:0. The SSIM and picture-stat checks use a centre region about 70% of the width and 60% of the height to reduce the influence of black bars. This samples the video; it does not inspect every scene. HDR footage is not evaluated in its original HDR luminance space.

## 3. Round-trip SSIM: effective resolution

Each sampled centre crop is downscaled with area filtering and enlarged back with bicubic filtering. SSIM compares it with the original crop. If almost nothing changes, there was little detail above the smaller resolution.

| Downscale factor | SSIM threshold |
|---|---:|
| 0.75 | 0.980 |
| 0.50 | 0.985 |
| 0.333 | 0.950 |

The second-most-detailed sample at each scale decides the result, or the only sample for a one-frame video. Scales are tested in order; the check stops at the first threshold that is not met. The last passing scale sets the estimated effective height; if none passes, the nominal height remains.

**Scoring effect:** changes the resolution tier, not the bitrate grade. The report's `detail` value is `1 - SSIM` at the 0.75 round trip.

**What this catches:** upscaling, soft focus, strong blur and some strong denoising or blocking.

**Limitations:** grain can look like detail. Flat animation, skies, short clips and synthetic graphics may look lower-resolution than they really are. Blocking is inferred from detail loss, not directly measured.

## 4. resdet: resizing traces

resdet analyses the frequency spectrum of the sampled frames to estimate the original source height of an upscale. It uses the whole sampled frames, not the SSIM centre crop.

Candidates outside plausible bounds are ignored, along with weak candidates near 6/8 and 7/8 of the stored height that commonly represent compression artifacts.

The combination rule is:

```text
if resdet found a height and SSIM height < 0.6 * resdet height:
    use SSIM height
else:
    use resdet height if available, otherwise SSIM height
```

This prevents resizing traces from overriding severe detail loss caused by later blur or blocking. The effective width is scaled by the same ratio as the height, then the usual pixel-count tier thresholds apply.

**What resdet catches:** many conventional bicubic/Lanczos upscales, sometimes even with grain added afterward.

**What it cannot judge:** blur, exposure, noise, colour correctness or general perceptual quality. On native footage it may find no resizing trace. AI upscaling and synthetic patterns can defeat or mislead it. If resdet cannot be built, a warning is emitted and SSIM still runs.

## 5. Picture statistics: noise and diagnostic flags

Values are combined using the median over sampled frames. Noise is estimated with the Immerkaer filter on 32-pixel blocks; the flattest 10% of eligible blocks reduce contamination by scene detail. Heavily clipped or too-small regions can leave noise unavailable.

| Check | Condition | Scoring effect |
|---|---|---|
| noisy | estimated noise sigma >= 3.5, in 8-bit levels | flag only |
| very noisy | noise sigma >= 5 | grade lowered exactly one step, never below poor |
| dark | mean centre-crop luma < 32 | flag only |
| crushed blacks | at least 15% of centre-crop luma values <= 18 | flag only |
| clipped highlights | at least 5% of centre-crop luma values >= 233 | flag only |
| saturated | mean chroma magnitude relative to neutral U/V = 128 is >= 30 | flag only |

Heavy-noise grades follow `excellent -> good -> fair -> poor`; a single run applies only one step, and repeated cached runs do not compound it.

**Limitations:** intentional heavy film grain is penalised like unwanted noise. Night scenes may legitimately be dark. Vivid cartoons can trigger saturation flags. Colour manipulation cannot be established without a reference; a contrast/saturation boost may instead trigger crushed-black or clipping flags.

There is no separate reliable banding, colour-fidelity, motion-artifact or artistic-quality score. ffmpeg `blockdetect` was investigated but is not part of scoring.

## Running fresh checks on already-renamed files

```bash
uv run vqc /path/to/test/library --deep --force --rename
```

`--force` purges scanned videos' old cache entries before processing, including older size/mtime keys and names with previous vqc prefixes. Other videos' results stay cached. Fresh results are written back normally. `--rename` replaces an existing quality prefix instead of stacking another one; without it, names remain unchanged.

`--force` alone reruns metadata checks only. Add `--deep` to rerun picture checks. It does not change thresholds or overwrite conflicting rename targets. Avoid running concurrent processes against the same cache while forcing a refresh.

## Validation tests, distinct from scoring checks

Run the automated regressions with:

```bash
uv run python -m unittest -v test_vqc
```

[test_vqc.py](test_vqc.py) covers SSIM/resdet combination boundaries, controlled noise measurement, YUV picture statistics, flag thresholds, cached reporting and repeated renaming. Workflow tests use temporary files and controlled detector results rather than real video decoding. Force-refresh tests cover cache purging, fresh processing and failure handling.

The separate real-video calibration challenge used two Xiph 1080p camera sequences, crowd_run and park_joy, with 15 variants each: originals, mild/strong blur, 540p and 720p upscales, grain, grain after upscaling, sharpening after blur, darkness, dark noise, contrast/saturation changes, colour casts, strong denoising, and blocky low-bitrate material re-encoded at high bitrate.

The 30-video set was rerun with fresh ffmpeg/resdet analysis and its expected tiers, noise penalties and diagnostic flags checked. These measurements support the current thresholds on those scenes; they do not establish universal accuracy for an arbitrary library. The large calibration videos are not bundled with the repository. See [README.md](README.md#accuracy) for the measured outcomes.
