# Video-quality research

This document records quality dimensions, evidence, alternatives and selection rationale. It is not an implementation plan or a list of supported features.

- [Implemented checks and completed validation](SCORING_TESTS.md)
- [Implementation plan and progress](plan.md)
- [Filename conventions](QUALITY_CODES.md)
- [Usage and local operation](README.md)

## Evidence boundaries

The user reports severe deterioration in older-phone high-resolution low-light recordings. No private video was provided or examined. Public footage with synthetic degradations established some existing checks, but did not establish accuracy on sensor/ISP/compression pipelines.

Confirmed code mechanisms include isolated fast-seek samples, 8-bit deep-analysis conversion, luma-only spatial noise, lowest-noise block selection, median pooling, a one-step noise penalty and resolution-first ranking. The contribution of each to the reported problem is unverified.

Private evaluation of the first spatial diagnostic found high-variation false positives on clean, finely detailed high-resolution footage, even above 70% eligibility. This demonstrates that its selection rules did not separate detail from noise; coverage was not classification confidence. The replacement temporal experiment is described in the implemented-quality guide, without claiming real-phone accuracy or justifying new penalties.

## Quality coverage and blind spots

| Dimension | Research question / candidate evidence | Main confound |
|---|---|---|
| Compression and useful detail | Codec-adjusted bpp and resizing/detail-loss traces | Noise spends bitrate; sharpness is not overall quality |
| Fine and coarse spatial noise | Multi-scale luma/chroma variation in suitable tiles and brightness bands | Texture, denoising and clipping change the statistics |
| Crawling temporal noise | Consecutive-frame differences with alignment and spatial evidence | Motion, exposure changes and interpolation |
| Fixed-pattern / frozen noise | Spatial persistence and periodic refresh behaviour | Tiny differences can mean repeated noisy content, not clean content |
| Exposure pumping / flicker | Coherent brightness/colour changes over time | Fades, flashes and changing illumination may be intentional |
| Ghosting / temporal smearing | Persistent trails around motion boundaries | Camera processing, motion estimation and occlusions |
| Banding | Netflix CAMBI, a specialist no-reference metric | Scaling and viewing conditions; not a general quality score |
| Blocking / ringing / sharpening halos | Dedicated block and edge structure | Natural grids, scene edges and artistic sharpening |
| Freezes / repeated frames | FFmpeg freezedetect and content change over time | Intentional still scenes |
| Cadence / judder / frame-gap symptoms | PTS deltas combined with content motion | VFR itself is not a defect |
| Interlacing / telecine | FFmpeg idet and field consistency | Field structure alone does not establish poor presentation |
| Motion blur / shake / rolling shutter / EIS | Directional blur, trajectories and geometric consistency | Intentional camera movement and sub-pixel stabilization |
| Colour-detail loss / bleeding | Chroma edges and luma/chroma consistency | Legitimate subsampling and resampling |
| HDR / range / bit depth | Representation-aware or perceptually normalized measurements | Untagged sources, tone mapping and display dependence |
| Spatial and duration coverage | Regional measurements and representative windows | Sparse samples cannot establish affected duration |
| Display geometry / viewing scale | SAR, orientation and reference display scale | Native-pixel variation differs from displayed visibility |
| Content versus playback faults | Selected-file decode errors, timing, local player comparison | Offline files cannot explain every decoder/display problem |
| Overall ranking | Validated severity/coverage alongside resolution and compression | Resolution-first ordering may favour a damaged high-resolution copy |
| Artistic intent / true colour fidelity | Human/reference evidence | Cannot reliably infer intent or the original scene automatically |

Audio and lip-sync are separate from this picture-quality work.

## Alternatives and rationale

| Method | Evidence / trade-off | Selection rationale |
|---|---|---|
| Codec-adjusted bpp | Cheap library-wide compression proxy | Retained as a baseline, not perceptual ground truth |
| Round-trip SSIM + resdet | Controlled tests supported detail-loss and conventional-upscale detection | Retained for current scoring; neither measures all noise or quality |
| Spatial Immerkaer | Small NumPy estimator separated clean footage and synthetic strong grain | Retained as a comparison; high-frequency luma coverage is narrow |
| Multi-scale spatial Y/U/V | Cheaper than motion estimation; controlled correlated/chroma fixtures revealed disturbance, but private testing found clean fine-detail false positives | Rejected as the experimental noise-review ranking after local evaluation |
| signalstats YDIF/UDIF/VDIF | Adjacent-frame sample differences | Useful trivial temporal baseline; not noise-specific |
| tblend difference | Consecutive-frame difference image | Useful diagnostic visualization, not automatic severity |
| Robust motion-aware residuals | Stable detail can cancel after alignment, unlike spatial variation; motion, temporal processing and exposure remain confounds | Selected for the next local experiment after spatial specificity failed; usefulness still requires private validation |
| atadenoise / hqdn3d | Temporal or spatiotemporal denoising | Removed detail/motion can masquerade as removed noise; not default detectors |
| VBM4D / FastDVDnet | Established multi-frame denoisers | Denoising benchmarks do not validate reference-free quality scoring; heavier dependencies |
| Dense optical flow | Local alignment | Runtime, interpolation and low-light reliability require evidence before adoption |
| CAMBI | Established no-reference per-frame banding detector | Most concrete specialist candidate; availability, cost and viewing assumptions still need checking |
| FFmpeg blurdetect | Earlier trials tracked scene content more than blur | Not selected for scoring; not a universal rejection |
| FFmpeg blockdetect | Earlier trial weakly separated damage; colour cast scored above blocking | Not selected for scoring without dedicated validation |
| DOVER | Earlier four-scene trial favoured 720p upscales over originals | Not selected as the library classifier; technical/aesthetic distinction remains relevant |
| TOPIQ / MUSIQ | Earlier scores overlapped across scenes and favoured denoised copies | Not selected as universal library scores |
| Q-Align | Earlier resizing obscured resolution loss; about 100 seconds/crop on CPU in that trial | Not selected for large-library CPU operation |

Check the exact software and model-weight licences before any future dependency is adopted. Earlier tested packages had compute/licensing constraints; these must not be generalized to every implementation.

## Independent review and decision history

| Milestone | What was chosen and why |
|---|---|
| Initial classifier | Practical codec-adjusted bitrate and SSIM/resdet, supported by controlled tests |
| Existing picture statistics | Small spatial NumPy checks and a one-step heavy-noise adjustment; no broad real-phone validation |
| Low-light research, 2026-11-01 | Investigate chroma and temporal disturbance rather than invent stronger penalties |
| Broader review, 2026-11-01 | Treat specialist defects separately from overall quality and artistic preference |
| Independent Opus review, 2026-11-01 | Verify sampling/range and test a cheaper spatial baseline before temporal complexity |
| Prototype selection, 2026-11-01 | Opt-in spatial diagnostics separate from scoring; temporal and ranking work remain conditional |
| Private evaluation, 2026-11-02 | Clean high-resolution detail ranked highly with >70% spatial eligibility; spatial selection was insufficiently specific |
| Replacement approved, 2026-11-02 | Remove experimental spatial ranking; try aligned consecutive-frame diagnostics while preserving older deep-scoring noise logic and all filename/scoring behaviour |

The independent review identified keyframe-biased sampling, black-floor selection, coarse chroma noise, motion-localized disturbance, periodic refresh, viewing scale, coverage, cache compatibility, privacy and runtime as concerns.

Qualifications remain important: keyframes are not universally cleaner; compression can suppress grain while adding other defects; whole-file decode or whole-GOP analysis is not automatically justified for every library entry; runtime estimates need measurements. Raw-sensor noise models cannot be directly applied to encoded gamma/ISP-processed MP4 values.

## Sources and evidence status

| Source | Supports / limits |
|---|---|
| [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html) | Official tool definitions verified through the browser after direct-fetch failure; not accuracy evidence for our classifier |
| [Netflix CAMBI](https://github.com/Netflix/vmaf/blob/master/resource/doc/cambi.md) | No-reference per-frame banding, bit-depth/scaling considerations; not temporal noise |
| [Foi sensor-noise research](https://webpages.tuni.fi/foi/sensornoise.html) | Signal-dependent/raw noise and clipping; not direct encoded-video calibration |
| [FastDVDnet paper](https://openaccess.thecvf.com/content_CVPR_2020/html/Tassano_FastDVDnet_Towards_Real-Time_Deep_Video_Denoising_Without_Flow_Estimation_CVPR_2020_paper.html), [implementation](https://github.com/m-tassano/fastdvdnet) | Multi-frame denoising; noise-level input and Gaussian benchmarks are not automatic quality scoring |
| [BM3D/VBM4D author page](https://webpages.tuni.fi/foi/GCF-BM3D/) | Spatiotemporal/random/fixed-pattern filtering; check licensing; FFmpeg bm3d is not temporal VBM4D |
| [Low-light temporal consistency](https://openaccess.thecvf.com/content/CVPR2021/html/Zhang_Learning_Temporal_Consistency_for_Low_Light_Video_Enhancement_From_Single_CVPR_2021_paper.html) | Single-image enhancement can yield unstable video; does not establish this user's cause |
| [Technical/aesthetic UGC study](https://openaccess.thecvf.com/content/ICCV2023/html/Wu_Exploring_Video_Quality_Assessment_on_User_Generated_Contents_from_Aesthetic_ICCV_2023_paper.html) | Technical distortions and aesthetic preference differ; dataset success is not universal ranking reliability |
| [Power of .75 rule](https://streaminglearningcenter.com/blogs/five-checks-for-your-encoding-ladder.html), [HEVC](https://en.wikipedia.org/wiki/High_Efficiency_Video_Coding), [AV1](https://en.wikipedia.org/wiki/AV1), [AVC](https://en.wikipedia.org/wiki/Advanced_Video_Coding) | Rough normalization/codec context, not perceptual truth |
| [Tools overview](https://www.forasoft.com/learn/video-quality/articles-vqm/open-source-no-reference-tools), [related scanner](https://github.com/sisyphosloughs/video-quality-scanner) | Background/context, not validation |

Still unverified: device-specific low-light FPS policies; candidate real-capture dataset licences (LIVE-VQC, KoNViD-1k, YouTube-UGC, CRVD, DRV); CAMBI in the installed build; sensor-noise attribution, subjective accuracy and usefulness on the private database. Conversion behaviour relevant to the implemented prototype is tested on controlled lossless SDR fixtures, not all codecs/pipelines.
