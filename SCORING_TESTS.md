# Implemented quality checks and validation

**Status: implemented behaviour only.** This document describes [vqc.py](vqc.py), [noise_diagnostics.py](noise_diagnostics.py) and their tests. Broader areas and alternatives are in [research.md](research.md); implementation phases, progress and remaining gates are in [plan.md](plan.md).

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

The implementation uses fast, non-accurate seeking and takes one decoded frame per position. It does not record the actual decoded timestamps or frame types for analysis, and the requested sample times may differ from the decoded frame times. These isolated samples are not consecutive-frame bursts.

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

In the existing scoring path, the noise filter excludes luma values <= 1 and >= 254; it does not adapt that eligibility mask to a limited-range black floor near 16. Its noise is measured only on Y, and quiet-block selection can hide other regions. The separate opt-in spatial diagnostic below does not replace this estimator. Temporal noise and calibrated affected-duration estimates are not implemented.

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

There is no separate banding, blocking, ringing, freeze, cadence, flicker, ghosting, colour-fidelity or artistic-quality score. HDR transfer metadata is recorded, but there is no HDR-aware noise or perceptual-quality analysis. See [research coverage](research.md#quality-coverage-and-blind-spots) for the broader gaps and candidate methods.

For commands, report pagination and forced refresh, see [README.md](README.md). For filename prefixes and undo, see [QUALITY_CODES.md](QUALITY_CODES.md).

## 6. Experimental spatial diagnostics: opt-in, not scoring

`--noise-diagnostics` runs a separate local NumPy/ffmpeg path. It does not change the old sampler, noise estimate, grades, groups or filename flags.

| Step | Implemented behaviour |
|---|---|
| Accurate sampling | Seek to up to `--frames` positions, trim one frame, record requested/source-relative time, actual PTS, frame type and keyframe flag; exclude duplicate PTS |
| Representation | Inspect source pixel depth/range/transfer/SAR; explicitly preserve range while converting to 16-bit YUV 4:2:0, then normalize code values |
| Supported input | Tagged SDR, known `tv`/`pc` range, 8-16-bit YUV/grayscale, at most 3840 x 2160 pixels in either orientation |
| Unsupported input | Unknown transfer/range, HDR or other unsupported representation is reported explicitly, not assigned zero disturbance |
| Spatial selection | Whole-frame 32x32 luma tiles (16x16 corresponding chroma), at native, 2x and 4x area-averaged scales |
| Eligibility | At least 95% of luma pixels between normalized 2/255 and 253/255; quadrant-mean residual spread below 4/255 after planar detrending |
| Variation | Fit/remove each tile's planar gradient; measure median absolute residual deviation x 1.4826 x 255 separately for Y/U/V |
| Distributions | Median/p90, shadow/midtone/highlight bands, four spatial regions and eligible-tile coverage per sample/scale/channel |
| Review ordering | 90th percentile of available sample/scale/channel p90 values; descriptive evidence coverage, not calibrated confidence |
| Bounds | One frame at a time per diagnostic worker; at most two diagnostic workers, single-threaded diagnostic decoding/filtering, 30-second decode timeout and 120-second per-file diagnostic budget |

The source precision is not reduced to 8 bits, but the analysis uses 4:2:0 chroma and native luma geometry, not every source's original chroma layout. No HDR linearization, temporal differencing, motion compensation, noise-versus-texture classifier or display-scale perceptual model is implemented.

Flat black/white regions do not establish cleanliness: if no eligible tiles exist, the status is **insufficient evidence**. Other statuses are **ok**, **unsupported** and **error**. Eligibility coverage is a fraction of measured tiles, not the fraction of the video's duration affected by noise.

The separate `_noise.md` report is ordered for review and has at most 200 videos per page. The private local cache stores detailed distributions and the algorithm-version/sample-count signature. Metadata/deep caches are upgraded only when needed; incompatible or failed diagnostic entries are not silently reused as successful results.

High spatial variation can be genuine texture or intentional grain. Conversely, the luma structure gate can reject severely disturbed areas; low eligibility must be inspected, not treated as a confident clean result. This prototype is evidence for local review, not a validated sensor-noise or general quality score.

Diagnostics introduce no dependency or model download. Source probing and decoding are restricted to local file/pipe protocols in this mode, including existing deep sampling when combined with diagnostics. When combined with `--deep`, only an installed/explicit resdet is used. Original files stay unchanged unless the user separately requests normal `--rename`.

## Validation tests, distinct from scoring checks

Run the automated regressions with:

```bash
uv run python -m unittest -v test_vqc test_noise_diagnostics
```

[test_vqc.py](test_vqc.py) covers SSIM/resdet combination boundaries, controlled noise measurement, YUV picture statistics, flag thresholds, cached reporting and repeated renaming. Workflow tests use temporary files and controlled detector results rather than real video decoding. Force-refresh tests cover cache purging, fresh processing and failure handling. Report tests check page boundaries, a maximum of 200 pictures per file, complete row coverage and navigation.

[test_noise_diagnostics.py](test_noise_diagnostics.py) adds controlled correlated-luma and chroma-only noise, smooth gradients, black-floor rejection, explicit unsupported representation, lossless real 8/10-bit full/limited-range decoding, actual PTS/non-keyframes and offset timestamps, duplicate exclusion on a one-frame black clip, local-protocol/decoder-thread restrictions, default-score preservation, cache upgrades/version changes/failures and diagnostic pagination. The controlled range test allows 0.4 8-bit-equivalent levels of p90 variation across quantized versions of the same perturbation; it also checks decoded normalized pixel values. This is not a codec-independent accuracy guarantee.

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
