# Noise-diagnostic implementation plan and progress

**Approved scope:** temporal checks now run automatically with every `--deep` command. With coverage >20%, residual >=1.726 adds `N` and one grade penalty; residual >3 adds `V` and two grades. Keep the older spatial estimate and use the larger spatial/temporal penalty, never their sum. Diagnostic-only commands retain metadata grades.

Research and selection history live in [research.md](research.md). Current measurements and rejection rules live in [SCORING_TESTS.md](SCORING_TESTS.md). Usage is in [README.md](README.md).

## Phase 0: sampling and representation, retained

- Accurate seeks, decoded PTS, source-relative times, I/P/B type and keyframe flags.
- Explicit range-preserving conversion to 16-bit YUV 4:2:0 and normalized SDR code values.
- Unknown range/transfer, HDR, unsupported formats and sources above the 4K pixel bound abstain.
- Local-only source protocols, no model/resdet downloads, diagnostic cache versioning and explicit errors.

## Phase 1: spatial baseline, removed

Private evaluation found clean high-resolution fine detail near the top of the spatial-variation report despite >70% eligibility. Synthetic noise sensitivity did not establish sufficient specificity against detail.

The spatial residual estimator and its review score have been removed from the experimental path. The older deep-scoring spatial estimator remains as supplementary evidence.

## Phase 2: temporal diagnostic, privately evaluated

- Up to `--frames` four-frame consecutive bursts, with distinct PTS/frame-pair accounting.
- Global integer translation from phase correlation and pyramid refinement, with a native-patch retry when coarse averaging hides fine detail; no interpolated luma.
- Plausible-match tiles selected using unclipped content, local temporal change, coarse scene agreement and edge-correlated residual rejection.
- Separate Y/U/V temporal residuals at native, 2x and 4x scales; native reliability masks also constrain coarser scales; chroma omitted for odd-pixel translations rather than resampled.
- Cuts/ambiguous matches, exposure changes, irregular timing and identical repeats explicitly rejected/counted.
- At least two measured pairs per scored burst; per-pair/scale/channel/brightness detail remains in the private cache.
- Report columns explain temporal residual, reliable coverage and measured/rejected/repeated pairs; no universal severity thresholds.
- Version-1 spatial cache entries automatically upgrade to version 2 without discarding valid metadata/deep results.
- At most two workers, one four-frame local temporary decode file per worker, 30-second decode timeout and 120-second diagnostic per-file budget.

Private playback review reported that higher temporal residuals matched larger noise, including at 20-40% coverage. Four permission-cleared public samples were evaluated locally; all media/cache/reports are excluded from Git. The user subsequently approved deep-scoring integration under the rules above. This does not establish universal thresholds or prove that every temporal fluctuation is unwanted noise.

## Phase 3: approved deep-scoring integration

- Every deep run obtains/reuses temporal results; legacy deep caches upgrade without repeating valid picture checks.
- Single `N`/`V` flag chosen from the strongest spatial/temporal flag; unchanged D/B/H/S rules and prefix replacement.
- One/two-step temporal penalty combined by maximum with the old spatial one-step penalty, clamped at poor.
- Raw metadata grade retained in cache; forced/cached/rename reruns cannot accumulate penalties.
- Diagnostic-only and metadata-only grades remain unchanged, even if a cached temporal result exists.
- Failures/unsupported/insufficient evidence remain explicit and introduce no temporal penalty.

### Known boundaries

No fractional-pixel alignment, optical flow, rotation/zoom model, general freeze/cadence or periodic-refresh detector is implemented. Complex motion can cause false positives or abstention. Severe/coarse noise may be rejected by reliability rules. Persistent/frozen noise may have low residuals; repeated frames do not establish cleanliness.

CAMBI, ghosting, broad perceptual models and ranking changes remain outside this change.

## Privacy and compatibility contract

- Metadata-only commands behave as before; deep mode intentionally includes temporal scoring and may change grades, ordering and filename flags when renaming is requested.
- No private file/frame/path/metadata uploads or model downloads.
- Source probing/decoding in diagnostic mode allows only local file/pipe protocols.
- No new dependency; temporary decoded data is local and automatically closed/deleted, including failures.
- Omit `--rename` to leave video and sidecar filenames unchanged. Normal renaming still operates if explicitly requested.
- Diagnostic-only mode creates no persistent snapshots; combining `--deep` adds existing local snapshots.
- Cache/reports contain private paths and numerical results. Do not share them or image-bearing reports for debugging.
- Report pages retain the 200-video/picture limit.

## Local evaluation procedure

Start with a small mixed subset, especially the previous clean-detail false positives:

```bash
uv run vqc /path/to/subset --noise-diagnostics --frames 3 -j 1 -o temporal_trial.md
```

Default broader sampling is eight burst positions. Use `uv run vqc /path/to/library --deep -j 4` for deep scoring including temporal checks. Add `--rename` only when ready to update names. `--force` is not needed for temporal cache/scoring upgrades. Explicit `--noise-diagnostics` remains useful for review-only runs.

Open `temporal_trial_noise.md` and compare against playback:

1. Clean fine detail, both static and moving, should no longer dominate solely because of detail.
2. Known noisy low-light recordings should show useful temporal evidence, or explicit abstention rather than a confident clean result.
3. Check clean dark recordings, handheld motion, cuts/fades, intentional grain and compressed footage for false positives.
4. Inspect coverage, pair counts and rejection reasons before interpreting the residual number.

Do not compare the old spatial number with the new temporal number: they measure different quantities. Anonymous labels, residual/coverage/status and pair counts are sufficient feedback; no recordings or paths are needed.

## Validation checklist

- [x] Controlled stable detail, integer translation and injected luma/chroma/correlated-noise separation, including actual 4K detail and all-scale subpixel-motion abstention.
- [x] Encoded clean-detail/moving-detail controls versus independently noisy versions.
- [x] Subpixel/local motion, exposure/cut, black-floor, repeated/refresh and irregular-timing checks.
- [x] Real 8/10-bit full/limited-range residuals checked against quantization-aware expected values and normalized pixels.
- [x] Real consecutive timestamps/non-keyframes, source offsets and short-clip abstention.
- [x] Existing scoring/rename regressions, cache migration/errors and report pagination.
- [x] All 23 regression tests pass, including temporal scoring boundaries, nonstacking penalties and cached/forced rename workflows; Pylance diagnostics and documentation links checked.
- [x] Final synthetic high-resolution resource measurement after all rejection gates.
- [ ] Private usefulness, false positives, abstention and target-machine total memory/runtime.

Private temporal ordering and the proposed thresholds have been approved; wider false-positive/missed-case coverage remains unvalidated. The four ignored local fixtures were rechecked with deep scoring: both low-light inputs receive temporal `N` / one step, while both stabilization controls receive no noise penalty. No fixture videos were renamed.

Measurements on generated clips do not certify another codec, recording style or machine. FFmpeg child memory must be included when checking the user's total resource budget.

### Measured resource check

The final implementation analyzed a generated, tagged SDR 3840x2160 H.264 clip at 10 FPS using eight four-frame bursts: 32 unique frames, 24 measured pairs, 54.83 seconds and 507.2 MiB Python peak resident memory. Fixture generation was excluded from timing; FFmpeg child-process memory is excluded from that memory figure. This is one worker on a synthetic five-second clip, not a long-GOP, two-worker or real-library performance guarantee.
