"""Opt-in motion-aligned temporal diagnostics; never changes quality scores."""

import json
import re
import subprocess
import tempfile
import time
from functools import lru_cache

import numpy as np

VERSION = 2
BURST_FRAMES = 4
SCALES = (1, 2, 4)
MAX_PIXELS = 3840 * 2160
TIME_BUDGET = 120
SDR_TRANSFERS = {"bt709", "smpte170m", "bt470bg", "gamma22", "gamma28", "iec61966-2-1"}


def cache_signature(frames):
    return {"version": VERSION, "frames": frames}


@lru_cache(maxsize=1)
def _pixel_depths():
    out = subprocess.run(["ffprobe", "-v", "error", "-show_pixel_formats", "-of", "json"],
                         check=True, capture_output=True, text=True, timeout=30)
    return {p["name"]: max(c["bit_depth"] for c in p.get("components", []))
            for p in json.loads(out.stdout)["pixel_formats"] if p.get("components")}


def normalize_plane(plane, color_range, chroma=False):
    """Input is 16-bit YUV code values, not linear-light sensor measurements."""
    if color_range == "tv":
        offset, span = (32768, 57344) if chroma else (4096, 56064)
    elif color_range == "pc":
        offset, span = (32768, 65535) if chroma else (0, 65535)
    else:
        raise ValueError(f"unsupported color range: {color_range}")
    return (plane.astype(np.float32) - offset) / span


def _downsample(plane):
    h, w = plane.shape
    return plane[:h // 2 * 2, :w // 2 * 2].reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))


def _tiles(plane, block):
    h, w = plane.shape
    return plane[:h // block * block, :w // block * block].reshape(
        h // block, block, w // block, block).transpose(0, 2, 1, 3)


def _overlap(a, b, dy, dx):
    h, w = a.shape
    return (a[max(0, -dy):min(h, h - dy), max(0, -dx):min(w, w - dx)],
            b[max(0, dy):min(h, h + dy), max(0, dx):min(w, w + dx)])


def _translation(a, b):
    """Estimate integer translation with phase correlation and pyramid refinement."""
    difference = b - a
    if np.percentile(np.abs(difference - np.median(difference)), 90) < 0.05 / 255:
        return (0, 0), "mostly unchanged luma structure"
    aa, bb = [a], [b]
    while (max(aa[-1].shape) > 256 or len(aa) < 3) and min(aa[-1].shape) >= 32:
        aa.append(_downsample(aa[-1]))
        bb.append(_downsample(bb[-1]))
    x, y = aa[-1], bb[-1]
    flat_coarse = max(float(x.std()), float(y.std())) < 2 / 255
    if flat_coarse:
        # Coarse averaging can erase real fine detail: retry at native resolution before abstaining.
        h, w = a.shape
        ys, xs = slice(max(0, h // 2 - 256), h // 2 + 256), slice(max(0, w // 2 - 256), w // 2 + 256)
        aa, bb = [a[ys, xs]], [b[ys, xs]]
        x, y = aa[0], bb[0]
    window = np.outer(np.hanning(x.shape[0]), np.hanning(x.shape[1]))
    cross = np.fft.rfft2((y - y.mean()) * window) * np.conj(np.fft.rfft2((x - x.mean()) * window))
    cross /= np.maximum(np.abs(cross), 1e-12)
    corr = np.fft.irfft2(cross, s=x.shape)
    peak = np.unravel_index(np.argmax(corr), corr.shape)
    rest = corr.copy()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            rest[(peak[0] + dy) % x.shape[0], (peak[1] + dx) % x.shape[1]] = 0
    if corr[peak] < max(8 * np.sqrt(np.mean(rest ** 2)), 1.4 * rest.max()):
        if flat_coarse and max(float(x.std()), float(y.std())) < 8 / 255:
            return (0, 0), "low-contrast field; translation unobservable"
        return None, "ambiguous global alignment"
    shift = [int(p if p <= n // 2 else p - n) for p, n in zip(peak, x.shape)]
    for level in range(len(aa) - 2, -1, -1):
        shift = [v * 2 for v in shift]
        candidates = []
        stride = max(1, max(aa[level].shape) // 256)
        for dy in range(shift[0] - 1, shift[0] + 2):
            for dx in range(shift[1] - 1, shift[1] + 2):
                p, q = _overlap(aa[level], bb[level], dy, dx)
                if min(p.shape) < 32:
                    continue
                delta = q[::stride, ::stride] - p[::stride, ::stride]
                candidates.append((float(np.median(np.abs(delta - np.median(delta)))), dy, dx))
        if not candidates:
            return None, "insufficient alignment overlap"
        _, *shift = min(candidates)
    if any(abs(v) > 32 for v in shift):
        return None, "global translation exceeds 32-pixel search bound"
    return tuple(shift), "integer global translation"


def _plane_stats(delta, eligible, levels):
    median = np.median(delta, axis=(-2, -1), keepdims=True)
    variation = np.median(np.abs(delta - median), axis=(-2, -1)) * 1.4826 * 255
    values = variation[eligible]
    result = {"tiles": int(values.size),
              "median": round(float(np.median(values)), 3) if values.size else None,
              "p90": round(float(np.percentile(values, 90)), 3) if values.size else None,
              "bands": {}}
    for label, low, high in (("shadows", 0, 0.15), ("midtones", 0.15, 0.6),
                             ("highlights", 0.6, 1.01)):
        values = variation[eligible & (levels >= low) & (levels < high)]
        result["bands"][label] = {
            "tiles": int(values.size),
            "p90": round(float(np.percentile(values, 90)), 3) if values.size else None}
    return result


def _structure_match(a, b):
    block = a.shape[-1] // 8
    small = [p.reshape(*p.shape[:2], 8, block, 8, block).mean(axis=(-3, -1)) for p in (a, b)]
    x, y = (p - p.mean(axis=(-2, -1), keepdims=True) for p in small)
    sx, sy = (np.sqrt(np.mean(p ** 2, axis=(-2, -1))) for p in (x, y))
    corr = np.mean(x * y, axis=(-2, -1)) / np.maximum(sx * sy, 1e-12)
    return (np.maximum(sx, sy) < 2 / 255) | (corr >= 0.8)


def _coarse_mask(native, shape, scale):
    h, w = shape
    return native[:h * scale, :w * scale].reshape(h, scale, w, scale).mean(axis=(1, 3)) >= 0.75


def temporal_pair(previous, current):
    """Measure temporal residuals only in regions with plausible alignment."""
    if all(np.array_equal(a, b) for a, b in zip(previous, current)):
        return {"status": "repeated", "reason": "identical decoded frames; not evidence of cleanliness",
                "scales": []}
    shift, alignment = _translation(previous[0], current[0])
    result = {"status": "rejected", "alignment": alignment, "scales": []}
    if shift is None:
        result["reason"] = alignment
        return result
    dy, dx = shift
    result["shift_yx"] = [dy, dx]
    a, b = _overlap(previous[0], current[0], dy, dx)
    delta = b - a
    exposure = float(np.median(delta))
    result["exposure_delta"] = round(exposure * 255, 3)
    if abs(exposure) > 2 / 255:
        result["reason"] = "coherent exposure change; excluded from noise evidence"
        return result
    coarse = delta
    for _ in range(3):
        coarse = _downsample(coarse)
    if not coarse.size or np.median(np.abs(coarse)) > 12 / 255:
        result["reason"] = "scene cut or unreliable global match"
        return result
    h, w = a.shape
    pairs = [(a[:h // 2 * 2, :w // 2 * 2], b[:h // 2 * 2, :w // 2 * 2])]
    # Do not interpolate chroma: half-chroma-pixel shifts can invent detail residuals.
    chroma_aligned = dy % 2 == 0 and dx % 2 == 0
    for old, new in zip(previous[1:], current[1:]):
        if chroma_aligned:
            p, q = _overlap(old, new, dy // 2, dx // 2)
            pairs.append((p[:h // 2, :w // 2], q[:h // 2, :w // 2]))
        else:
            pairs.append(None)
    result["chroma_measured"] = chroma_aligned
    native_masks = {}
    for scale in SCALES:
        a, b = pairs[0]
        if min(a.shape) < 32:
            break
        yt, bt = _tiles(a, 32), _tiles(b, 32)
        levels = yt.mean(axis=(-2, -1))
        live = ((yt > 2 / 255) & (yt < 253 / 255)
                & (bt > 2 / 255) & (bt < 253 / 255)).mean(axis=(-2, -1)) >= 0.95
        residual = bt - yt
        blocks = residual.reshape(*residual.shape[:2], 4, 8, 4, 8).mean(axis=(-3, -1))
        local_change = np.max(np.abs(blocks), axis=(-2, -1))
        # Edge-correlated residuals indicate misalignment rather than random noise.
        smooth = (yt + bt) / 2
        gradients = (np.diff(smooth, axis=-1, prepend=smooth[..., :1]),
                     np.diff(smooth, axis=-2, prepend=smooth[..., :1, :]))
        centered = residual - residual.mean(axis=(-2, -1), keepdims=True)
        correlations = []
        for grad in gradients:
            grad = grad - grad.mean(axis=(-2, -1), keepdims=True)
            denom = np.sqrt(np.sum(centered ** 2, axis=(-2, -1))
                            * np.sum(grad ** 2, axis=(-2, -1)))
            correlations.append(np.abs(np.sum(centered * grad, axis=(-2, -1)))
                                / np.maximum(denom, 1e-12))
        edge_like = np.maximum(*correlations) >= 0.25
        edge_like &= np.sqrt(np.mean(centered ** 2, axis=(-2, -1))) > 0.05 / 255
        eligible = live & (local_change < 3 / 255) & ~edge_like & _structure_match(yt, bt)
        if scale > 1:
            eligible &= _coarse_mask(native_masks["Y"], levels.shape, scale)
        metrics = {}
        for name, pair, block in zip(("Y", "U", "V"), pairs, (32, 16, 16)):
            if pair is None:
                continue
            ta, tb = (_tiles(p, block)[:levels.shape[0], :levels.shape[1]] for p in pair)
            delta = tb - ta
            channel_eligible = eligible
            if name != "Y":
                broad = delta.reshape(*delta.shape[:2], 4, 4, 4, 4).mean(axis=(-3, -1))
                channel_eligible = (eligible & (np.max(np.abs(broad), axis=(-2, -1)) < 3 / 255)
                                    & _structure_match(ta, tb))
            if scale == 1:
                native_masks[name] = channel_eligible
            elif name != "Y":
                channel_eligible &= _coarse_mask(native_masks[name], levels.shape, scale)
            metrics[name] = _plane_stats(delta, channel_eligible, levels)
        result["scales"].append({"scale": scale, "tiles": int(eligible.size),
                                 "eligible_tiles": int(eligible.sum()),
                                 "coverage": round(float(eligible.mean()), 4), "planes": metrics})
        pairs = [tuple(_downsample(p) for p in pair) if pair else None for pair in pairs]
    result["status"] = "ok" if any(m["p90"] is not None for s in result["scales"]
                                  for m in s["planes"].values()) else "rejected"
    result["reason"] = ("aligned temporal residual; not calibrated noise severity" if result["status"] == "ok"
                        else "no reliable unclipped tiles after motion/structure rejection")
    return result


def sample_burst(path, stream, requested, width, height, color_range, timeout):
    """Decode consecutive frames to a temporary local file, reading one frame at a time."""
    if color_range not in ("tv", "pc"):
        raise ValueError(f"unsupported color range: {color_range}")
    cmd = ["ffmpeg", "-v", "info", "-protocol_whitelist", "file,pipe", "-threads", "1",
           "-noautorotate", "-copyts", "-ss", str(requested),
           "-i", str(path), "-map", f"0:{stream}", "-an", "-sn", "-dn",
           "-vf", f"scale=in_range={color_range}:out_range={color_range}:flags=accurate_rnd+bitexact,"
                  f"format=yuv420p16le,trim=end_frame={BURST_FRAMES},showinfo",
           "-frames:v", str(BURST_FRAMES), "-vsync", "0", "-threads", "1", "-filter_threads", "1",
           "-f", "rawvideo", "-"]
    cw, ch = (width + 1) // 2, (height + 1) // 2
    sizes = (width * height, cw * ch, cw * ch)
    frame_bytes = sum(sizes) * 2
    with tempfile.TemporaryFile() as raw:
        out = subprocess.run(cmd, stdout=raw, stderr=subprocess.PIPE, timeout=timeout)
        log = out.stderr.decode(errors="replace")
        if out.returncode:
            raise RuntimeError(log.strip().splitlines()[-1] if log.strip() else "diagnostic decode failed")
        matches = list(re.finditer(
            r"n:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:([-\d.e+]+).*?"
            r"sar:(\d+/\d+).*?s:(\d+)x(\d+).*?iskey:(\d+)\s+type:([IPB?])", log))
        if not matches or len(matches) > BURST_FRAMES or raw.tell() != len(matches) * frame_bytes:
            raise RuntimeError("diagnostic decoder returned inconsistent frame data/timestamps")
        raw.seek(0)
        for i, match in enumerate(matches):
            if int(match[1]) != i or (int(match[4]), int(match[5])) != (width, height):
                raise RuntimeError("diagnostic frame sequence or dimensions changed")
            data = np.frombuffer(raw.read(frame_bytes), dtype="<u2")
            y, u, v = np.split(data, (sizes[0], sizes[0] + sizes[1]))
            metadata = {"requested_s": requested, "pts_s": float(match[2]), "frame_type": match[7],
                        "keyframe": bool(int(match[6])), "sample_aspect_ratio": match[3]}
            yield metadata, (y.reshape(height, width), u.reshape(ch, cw), v.reshape(ch, cw))


def analyze_noise(row, frames, info):
    start = time.monotonic()
    stream = next(s for s in info["streams"] if s.get("index") == row["_vstream"])
    result = {"signature": cache_signature(frames), "method": "aligned-temporal", "status": "unsupported",
              "samples": [], "bursts": [], "range": stream.get("color_range", "unknown"),
              "transfer": stream.get("color_transfer", "unknown"),
              "pixel_format": stream.get("pix_fmt", "unknown"), "analysis_pixel_format": "yuv420p16le",
              "width": row["width"], "height": row["height"],
              "start_time_s": float(info.get("format", {}).get("start_time") or 0),
              "fps": row.get("fps"), "duration_s": row.get("_duration_s"),
              "sample_aspect_ratio": stream.get("sample_aspect_ratio", "unknown")}
    if result["transfer"] not in SDR_TRANSFERS:
        result["reason"] = "HDR or unknown transfer: no validated diagnostic normalization"
        return result
    if result["range"] not in ("tv", "pc"):
        result["reason"] = "unknown color range: no diagnostic normalization"
        return result
    if not result["pixel_format"].startswith(("yuv", "yuva", "nv", "p0", "gray")):
        result["reason"] = "diagnostics require a YUV or grayscale source"
        return result
    if row["width"] * row["height"] > MAX_PIXELS:
        result["reason"] = "source exceeds the prototype's 4K pixel budget"
        return result
    depth = _pixel_depths().get(result["pixel_format"])
    if depth is None or not 8 <= depth <= 16:
        result["reason"] = "unsupported source bit depth"
        return result
    result["bit_depth"] = depth
    duration = row["_duration_s"]
    last_request = max(0, duration - BURST_FRAMES / row["fps"] - 0.000001)
    times = sorted({round(min(last_request, duration * (0.05 + 0.9 * (i + 0.5) / frames)), 6)
                    for i in range(frames)}) if duration > 0 else [0]
    seen_samples, seen_pairs = set(), set()
    scores, all_pairs = [], []
    for requested in times:
        remaining = TIME_BUDGET - (time.monotonic() - start)
        if remaining <= 0:
            raise RuntimeError("diagnostic per-file time budget exceeded")
        burst = {"requested_s": requested, "pairs": []}
        previous, previous_sample = None, None
        for sample, planes in sample_burst(row["path"], row["_vstream"], requested,
                                           row["width"], row["height"], result["range"], min(30, remaining)):
            if time.monotonic() - start > TIME_BUDGET:
                raise RuntimeError("diagnostic per-file time budget exceeded")
            sample["actual_s"] = round(sample["pts_s"] - result["start_time_s"], 6)
            if sample["pts_s"] not in seen_samples:
                result["samples"].append(sample)
                seen_samples.add(sample["pts_s"])
            current = tuple(normalize_plane(p, result["range"], i > 0) for i, p in enumerate(planes))
            if previous is not None:
                key = (previous_sample["pts_s"], sample["pts_s"])
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    gap = key[1] - key[0]
                    if gap <= 0 or gap > 2.5 / row["fps"]:
                        pair = {"status": "rejected", "reason": "nonconsecutive or irregular frame timing",
                                "scales": []}
                    else:
                        pair = temporal_pair(previous, current)
                    pair["pts_s"] = list(key)
                    burst["pairs"].append(pair)
            previous, previous_sample = current, sample
        all_pairs.extend(burst["pairs"])
        # Require two measured pairs per burst; one exceptional transition cannot rank a video.
        measured = [p for p in burst["pairs"] if p["status"] == "ok"]
        if len(measured) >= 2:
            values = [max(m["p90"] for s in p["scales"] for m in s["planes"].values()
                          if m["p90"] is not None) for p in measured]
            burst["review_score"] = round(float(np.median(values)), 3)
            scores.append(burst["review_score"])
        result["bursts"].append(burst)
    scales = [s for p in all_pairs for s in p["scales"]]
    tiles = sum(s["tiles"] for s in scales)
    live = sum(s["eligible_tiles"] for s in scales)
    # Rejected/duplicate pairs never count as measured coverage.
    attempted_tiles = len(all_pairs) * sum((row["height"] // (32 * scale))
                                          * (row["width"] // (32 * scale)) for scale in SCALES)
    result["coverage"] = round(live / max(tiles, attempted_tiles), 4) if attempted_tiles else 0
    result["review_score"] = round(float(np.percentile(scores, 90)), 3) if scores else None
    result["measured_pairs"] = sum(p["status"] == "ok" for p in all_pairs)
    result["rejected_pairs"] = sum(p["status"] == "rejected" for p in all_pairs)
    result["repeated_pairs"] = sum(p["status"] == "repeated" for p in all_pairs)
    result["status"] = "ok" if scores else "insufficient evidence"
    reasons = sorted({p["reason"] for p in all_pairs if p["status"] != "ok"})
    result["reason"] = ("Temporal fluctuation only; motion/grain/compression can still confound it"
                        if scores else "fewer than two reliable pairs per burst")
    if reasons:
        result["reason"] += "; " + "; ".join(reasons)
    result["evidence"] = "limited temporal coverage" if result["coverage"] < 0.25 else "measured temporal coverage"
    result["unique_samples"] = len(seen_samples)
    result["elapsed_s"] = round(time.monotonic() - start, 3)
    if result["elapsed_s"] > TIME_BUDGET:
        raise RuntimeError("diagnostic per-file time budget exceeded")
    return result
