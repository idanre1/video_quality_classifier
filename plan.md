# Noise-diagnostic implementation plan and progress

**Approved scope:** phases 0-1, opt-in local diagnostics, with no new score/group/filename adjustments. Phase 2 remains conditional on baseline evidence and local evaluation.

Research, alternatives and sources live in [research.md](research.md). Current implemented behaviour and completed tests live in [SCORING_TESTS.md](SCORING_TESTS.md). This document tracks delivery and gates, not broad research.

## Phase 0: trustworthy sampling and representation

Implemented in the opt-in `--noise-diagnostics` path:

- Accurate seeks with requested times, decoded PTS, source-relative times, I/P/B type and keyframe flags.
- Source format/depth/range/transfer, dimensions, FPS, duration and sample aspect ratio.
- Explicit range-preserving conversion to 16-bit planar YUV; normalized SDR code values, not claimed sensor/linear-light measurements.
- Conservative unsupported status for unknown range/transfer, HDR, unsupported source formats and sources above the 4K pixel bound.
- Multiple positions using `--frames`; duplicate timestamps excluded, and insufficient evidence distinguished from cleanliness.
- One sample decoded/processed at a time per diagnostic worker, at most two concurrent workers, single-threaded diagnostic decoding/filtering, 30-second per-decode timeout and 120-second diagnostic per-file budget.
- No automatic resdet download when diagnostics are enabled. Existing installed resdet may still be used if `--deep` is also requested.

This does not modify the old fast-seek sampler used for existing scoring. Selected-file full-decode investigations, complete cadence analysis and dynamic display-aspect resampling remain outside the prototype.

**Gate:** verify range/depth equivalence, non-keyframe sampling, output/cache integration and measured resource use. The target-machine runtime and suitability across its codecs remain a local-evaluation gate.

## Phase 1: multi-scale spatial baseline

Implemented:

- Whole-frame tile measurements at native, 2x and 4x area-averaged scales, with no analysis resize to a different display resolution.
- Luma-selected eligible tiles: at least 95% unclipped pixels, and low large-scale luma structure after planar detrending.
- Separate Y/U/V robust residual variation; the 90th percentile retains disturbed eligible areas rather than selecting only the quietest tail.
- Per-sample scale/channel distributions, brightness bands, four spatial regions and eligible coverage in the cache.
- A local review report sorted by experimental spatial variation, with requested/actual timestamps and frame types available in cached details.
- Versioned diagnostic cache signature; old metadata/deep results can be upgraded without repeating a valid old deep result.
- Explicit unsupported/insufficient/error output. Errors retain existing metadata, but do not become successful diagnostic results.

The summary is a review-order heuristic, not noise sigma, calibrated severity, affected-video duration, a probability, or proof that texture/grain is unwanted. It does not alter the existing `N/V/D/B/H/S` flags.

**Gate:** controlled correlated-luma/chroma tests must beat the old spatial-luma baseline; clean gradients/black-floor and representation tests must behave correctly. General false-positive/abstention performance still needs local review. Do not automatically promote to temporal work.

## Phase 2: temporal diagnostic, not implemented

Only if phase 1 fails to discriminate the troublesome cases:

1. Decode short consecutive windows, recording actual PTS and frame types.
2. Start with global alignment and reliable tiles; reject cuts, poor matches and occlusions.
3. Measure Y/U/V residuals at several scales alongside spatial evidence.
4. Distinguish global exposure changes from local noise; retain flicker evidence rather than normalizing it away.
5. Investigate repeated/near-zero residuals and periodic refresh without assuming they mean cleanliness.
6. Report reliable static/moving coverage and explicit abstention.

**Gate:** improvement beyond phase 1 and trivial frame differences at acceptable cost and abstention. Local flow, CAMBI, ghosting, freeze/cadence, new penalties and ranking changes require further scope approval.

## Privacy and compatibility contract

- Default commands behave as before; diagnostic values never drive grades, groups or rename flags.
- No private file/frame/path/metadata is uploaded. All reports and snapshots stay local.
- Diagnostic-mode source probing and decoding allow only local file/pipe protocols, including existing deep sampling when combined with diagnostics.
- Diagnostics introduce no dependencies or model downloads.
- Do not run `--rename` for a read-only evaluation. If explicitly supplied, normal existing renaming still operates independently of diagnostics.
- Combining `--deep` adds existing frame checks and local snapshots; diagnostics alone does not create snapshots or build resdet.
- Cache entries contain private paths and numeric results. Do not share the cache or image-bearing reports for debugging.
- No export feature is implemented; voluntary aggregate-only feedback can be supplied manually.
- Respect the existing 200-video/picture page limit for diagnostic review pages.

## Local evaluation procedure

Run:

```bash
uv run vqc /path/to/library --noise-diagnostics --deep -j 2
```

Omit `--deep` to run diagnostics with metadata-only scoring and no snapshots. Add `--force` when intentionally purging/reprocessing cached videos; it is not needed for the first diagnostic cache upgrade.

Start with a manageable mixed subset: noisy dark recordings, clean dark footage, clean handheld motion, intentional grain, different devices/codecs and HDR/untagged examples. Use consistent playback settings.

Review the `_noise.md` report and its pages. High spatial variation is a candidate to inspect, not automatically poor quality. Check the reason and eligible coverage; unsupported or insufficient evidence is not a clean result.

Keep related recording sessions together if tuning and evaluation cases are separated. Recheck some labels for consistency. Compare old sigma/bitrate with the spatial review ordering; later temporal evidence must add useful discrimination.

Report severe-case precision/recall or pairwise agreement, false positives, top-ranked usefulness, abstention/coverage and runtime. Numeric subjective acceptance targets are not yet calibrated.

## Validation checklist

- [x] Complete existing regression suite plus diagnostic/cache/report tests.
- [x] Controlled correlated luma and chroma-only disturbance exceeds the old luma-only response.
- [x] Smooth gradients remain near zero; black-floor-only samples produce insufficient evidence.
- [x] Controlled 8/10-bit full/limited-range fixtures agree within quantization-aware tolerance.
- [x] Actual sample times and non-keyframes are verified on long-GOP footage.
- [x] Default results are unchanged; diagnostic cache upgrades, version changes, errors and pagination work.
- [x] Build includes the diagnostic module and no new dependency.
- [x] Measure synthetic high-resolution runtime and Python peak memory.
- [ ] Measure target-machine runtime and total process memory on representative recordings before database-wide use.
- [ ] User validates usefulness privately before temporal work or scoring changes.

### Measured resource check

A generated, tagged SDR 3840x2160 H.264 clip at 10 FPS was analyzed with the default eight samples: eight distinct P frames, 12.98 seconds, and 271.4 MiB Python peak resident memory. Fixture generation was excluded from timing; FFmpeg child-process memory is not included in that memory figure. This is a synthetic one-worker observation, not a performance guarantee or a two-worker/long-GOP stress test.
