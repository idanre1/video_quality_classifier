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

These checks are implemented locally with **NumPy**; ffmpeg decodes the sampled frames, while neither resdet nor an AI model computes these statistics.

- **Noise / grain:** the Immerkaer high-pass filter estimates noise strength in the lowest-noise 10% of eligible 32-pixel blocks to reduce contamination from scene detail.
- **Darkness:** the mean Y/luma value measures the centre crop's average brightness.
- **Crushed blacks:** the fraction of Y/luma values at or below 18 measures how much of the centre crop is near black.
- **Clipped highlights:** the fraction of Y/luma values at or above 233 measures how much of the centre crop is near white.
- **Saturation:** the mean of `sqrt((U - 128)^2 + (V - 128)^2)` measures chroma strength relative to neutral colour.

Values are combined using the median over sampled frames. Heavily clipped or too-small regions can leave noise unavailable.

| Check | Condition | Scoring effect |
|---|---|---|
| noisy | estimated noise sigma >= 3.5, in 8-bit levels | flag only |
| very noisy | noise sigma >= 5 | grade lowered exactly one step, never below poor |
| dark | mean centre-crop luma < 32 | flag only |
| crushed blacks | at least 15% of centre-crop luma values <= 18 | flag only |
| clipped highlights | at least 5% of centre-crop luma values >= 233 | flag only |
| saturated | mean chroma magnitude relative to neutral U/V = 128 is >= 30 | flag only |

Heavy-noise grades follow `excellent -> good -> fair -> poor`; a single run applies only one step, and repeated cached runs do not compound it.

With `--deep --rename`, these same flags become an optional `F-` letter segment in filenames; the letters add no additional scoring adjustments. See [picture-flag letters](QUALITY_CODES.md#picture-flag-letters) for the convention.

**Limitations:** intentional heavy film grain is penalised like unwanted noise. Night scenes may legitimately be dark. Vivid cartoons can trigger saturation flags. Colour manipulation cannot be established without a reference; a contrast/saturation boost may instead trigger crushed-black or clipping flags.

There is no separate reliable banding, colour-fidelity, motion-artifact or artistic-quality score. ffmpeg `blockdetect` was investigated but is not part of scoring.

For commands, report pagination and forced refresh, see [README.md](README.md). For filename prefixes and undo, see [QUALITY_CODES.md](QUALITY_CODES.md).

## Validation tests, distinct from scoring checks

Run the automated regressions with:

```bash
uv run python -m unittest -v test_vqc
```

[test_vqc.py](test_vqc.py) covers SSIM/resdet combination boundaries, controlled noise measurement, YUV picture statistics, flag thresholds, cached reporting and repeated renaming. Workflow tests use temporary files and controlled detector results rather than real video decoding. Force-refresh tests cover cache purging, fresh processing and failure handling. Report tests check page boundaries, a maximum of 200 pictures per file, complete row coverage and navigation.

The separate real-video calibration challenge used two Xiph 1080p camera sequences, crowd_run and park_joy, with 15 variants each: originals, mild/strong blur, 540p and 720p upscales, grain, grain after upscaling, sharpening after blur, darkness, dark noise, contrast/saturation changes, colour casts, strong denoising, and blocky low-bitrate material re-encoded at high bitrate.

The 30-video set was rerun with fresh ffmpeg/resdet analysis and its expected tiers, noise penalties and diagnostic flags checked. These measurements support the current thresholds on those scenes; they do not establish universal accuracy for an arbitrary library. The large calibration videos are not bundled with the repository.

### Measured outcomes

An earlier set used four Xiph SVT 1080p sequences (crowd_run, park_joy, ducks_take_off, old_town_cross), encoded with x264 CRF 14:

| Variant | Deep result |
|---|---|
| Original | 1080p, 4/4 |
| 720p upscaled to 1080p | 720p, 4/4 |
| 540p / 360p upscaled to 1080p | 480p, 8/8 |
| Gaussian blur sigma = 1 | 720p, 3/4; 480p, 1/4 |
| Light denoising with hqdn3d | 1080p, 4/4; not flagged |

resdet found all 12 conventional upscales at the exact source height, with no false positives on those originals.

The 30-video challenge used x264 CRF 16; all variants were excellent by bitrate alone:

| Variant | Deep result |
|---|---|
| Original | 1080p/excellent, no flags |
| Blur sigma = 1 / sigma = 2 | 720p / low |
| Strong blur then sharpening | 720p |
| Strong denoising | 720p |
| 720p bicubic/Lanczos / 540p upscales | 720p / 480p |
| 540p upscale then grain | 480p, grade reduced, very noisy |
| Heavy grain on a sharp original | 1080p, grade reduced, very noisy |
| Underexposure / underexposure plus noise | Dark and crushed blacks; added heavy noise also reduces the grade |
| Contrast/saturation boost and colour cast | 1080p; saturation and/or black/highlight flags, depending on the scene |
| CRF 44 blocky encode re-encoded at high bitrate | low; bitrate grade remains excellent |

The colour-boosted park_joy clip triggered dark/crushed-black flags, not saturation: this illustrates why these flags are diagnostics rather than proof of manipulation.

### Other methods investigated

| Method | Why it was not used |
|---|---|
| ffmpeg blurdetect | Tracked scene content more than blur |
| ffmpeg blockdetect | Weak separation; a colour-cast clip scored higher than real blocking |
| DOVER | Rated 720p upscales above originals in four tested scenes |
| pyiqa TOPIQ / MUSIQ | Scores overlapped across scenes and favoured denoised copies |
| pyiqa Q-Align | Resized to 448 pixels, obscuring resolution loss; about 100 seconds per crop on CPU |

The tested PyTorch models also require substantial compute and have non-commercial licensing restrictions.

### Sources

- [Streaming Learning Center: Power of .75 rule](https://streaminglearningcenter.com/blogs/five-checks-for-your-encoding-ladder.html): resolution normalisation and typical H.264 bpp.
- Wikipedia codec articles: [HEVC](https://en.wikipedia.org/wiki/High_Efficiency_Video_Coding), [AV1](https://en.wikipedia.org/wiki/AV1), [AVC](https://en.wikipedia.org/wiki/Advanced_Video_Coding).
- [resdet](https://github.com/0x09/resdet): resizing-trace detection.
- [Video quality tools overview](https://www.forasoft.com/learn/video-quality/articles-vqm/open-source-no-reference-tools).
- [video-quality-scanner](https://github.com/sisyphosloughs/video-quality-scanner): a related resolution-based library tool.
