"""Opt-in spatial disturbance diagnostics; never changes quality scores."""

import json
import re
import subprocess
import time
from functools import lru_cache

import numpy as np

VERSION = 1
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


def _residual(tiles):
    block = tiles.shape[-1]
    axis = np.arange(block, dtype=np.float32) - (block - 1) / 2
    mean = tiles.mean(axis=(-2, -1))
    sx = (tiles.mean(axis=-2) * axis).mean(axis=-1) / np.mean(axis ** 2)
    sy = (tiles.mean(axis=-1) * axis).mean(axis=-1) / np.mean(axis ** 2)
    plane = mean[..., None, None] + sx[..., None, None] * axis + sy[..., None, None] * axis[:, None]
    return tiles - plane


def spatial_stats(y, u, v):
    """Robust plane-detrended variation in luma-selected tiles, in 8-bit-equivalent levels."""
    results = []
    for scale in SCALES:
        if min(y.shape) >= 32:
            yt = _tiles(y, 32)
            residual = _residual(yt)
            levels = yt.mean(axis=(-2, -1))
            live = ((yt > 2 / 255) & (yt < 253 / 255)).mean(axis=(-2, -1)) >= 0.95
            quadrants = residual.reshape(*residual.shape[:2], 2, 16, 2, 16).mean(axis=(-3, -1))
            structure = quadrants.max(axis=(-2, -1)) - quadrants.min(axis=(-2, -1))
            eligible = live & (structure < 4 / 255)
            grid_h, grid_w = levels.shape
            metrics = {}
            for name, plane, block in (("Y", y, 32), ("U", u, 16), ("V", v, 16)):
                tiles = _tiles(plane, block)[:grid_h, :grid_w]
                delta = _residual(tiles)
                median = np.median(delta, axis=(-2, -1), keepdims=True)
                variation = np.median(np.abs(delta - median), axis=(-2, -1)) * 1.4826 * 255
                bands = {}
                for label, low, high in (("shadows", 0, 0.15), ("midtones", 0.15, 0.6),
                                         ("highlights", 0.6, 1.01)):
                    values = variation[eligible & (levels >= low) & (levels < high)]
                    bands[label] = {"tiles": int(values.size),
                                    "median": round(float(np.median(values)), 3) if values.size else None,
                                    "p90": round(float(np.percentile(values, 90)), 3) if values.size else None}
                values = variation[eligible]
                metrics[name] = {"median": round(float(np.median(values)), 3) if values.size else None,
                                 "p90": round(float(np.percentile(values, 90)), 3) if values.size else None,
                                 "bands": bands}
                regions = {}
                for label, ys, xs in (
                    ("top-left", slice(0, grid_h // 2), slice(0, grid_w // 2)),
                    ("top-right", slice(0, grid_h // 2), slice(grid_w // 2, None)),
                    ("bottom-left", slice(grid_h // 2, None), slice(0, grid_w // 2)),
                    ("bottom-right", slice(grid_h // 2, None), slice(grid_w // 2, None)),
                ):
                    mask = eligible[ys, xs]
                    values = variation[ys, xs][mask]
                    regions[label] = {"tiles": int(values.size),
                                      "p90": round(float(np.percentile(values, 90)), 3) if values.size else None}
                metrics[name]["regions"] = regions
            results.append({"scale": scale, "tiles": int(levels.size),
                            "eligible_tiles": int(eligible.sum()),
                            "coverage": round(float(eligible.mean()), 4), "planes": metrics})
        y, u, v = (_downsample(p) for p in (y, u, v))
    return results


def sample_frame(path, stream, requested, width, height, color_range, timeout):
    if color_range not in ("tv", "pc"):
        raise ValueError(f"unsupported color range: {color_range}")
    cmd = ["ffmpeg", "-v", "info", "-protocol_whitelist", "file,pipe", "-threads", "1",
           "-noautorotate", "-copyts", "-ss", str(requested),
           "-i", str(path), "-map", f"0:{stream}", "-an", "-sn", "-dn",
           "-vf", f"scale=in_range={color_range}:out_range={color_range}:flags=accurate_rnd+bitexact,"
                  "format=yuv420p16le,trim=end_frame=1,showinfo",
           "-frames:v", "1", "-vsync", "0", "-threads", "1", "-filter_threads", "1",
           "-f", "rawvideo", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    log = out.stderr.decode(errors="replace")
    if out.returncode:
        raise RuntimeError(log.strip().splitlines()[-1] if log.strip() else "diagnostic decode failed")
    match = re.search(r"n:\s*0\s+pts:\s*-?\d+\s+pts_time:([-\d.e+]+).*?"
                      r"sar:(\d+/\d+).*?s:(\d+)x(\d+).*?iskey:(\d+)\s+type:([IPB?])", log)
    if not match:
        raise RuntimeError("diagnostic decoder did not report frame timestamp/type")
    if (int(match[3]), int(match[4])) != (width, height):
        raise RuntimeError("diagnostic frame dimensions changed")
    cw, ch = (width + 1) // 2, (height + 1) // 2
    sizes = (width * height, cw * ch, cw * ch)
    if len(out.stdout) != sum(sizes) * 2:
        raise RuntimeError("diagnostic decoder did not return exactly one 16-bit frame")
    data = np.frombuffer(out.stdout, dtype="<u2")
    y, u, v = np.split(data, (sizes[0], sizes[0] + sizes[1]))
    metadata = {"requested_s": requested, "pts_s": float(match[1]), "frame_type": match[6],
                "keyframe": bool(int(match[5])), "sample_aspect_ratio": match[2]}
    return metadata, (y.reshape(height, width), u.reshape(ch, cw), v.reshape(ch, cw))


def analyze_noise(row, frames, info):
    start = time.monotonic()
    stream = next(s for s in info["streams"] if s.get("index") == row["_vstream"])
    result = {"signature": cache_signature(frames), "status": "unsupported", "samples": [],
              "range": stream.get("color_range", "unknown"),
              "transfer": stream.get("color_transfer", "unknown"),
              "pixel_format": stream.get("pix_fmt", "unknown"),
              "analysis_pixel_format": "yuv420p16le",
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
    last_request = max(0, duration - 1 / row["fps"] - 0.000001)
    times = [round(min(last_request, duration * (0.05 + 0.9 * (i + 0.5) / frames)), 6)
             for i in range(frames)] if duration > 0 else [0]
    seen = set()
    for requested in times:
        remaining = TIME_BUDGET - (time.monotonic() - start)
        if remaining <= 0:
            raise RuntimeError("diagnostic per-file time budget exceeded")
        sample, planes = sample_frame(row["path"], row["_vstream"], requested,
                                      row["width"], row["height"], result["range"], min(30, remaining))
        sample["actual_s"] = round(sample["pts_s"] - result["start_time_s"], 6)
        if sample["pts_s"] in seen:
            sample["reason"] = "duplicate decoded timestamp; excluded"
            result["samples"].append(sample)
            continue
        seen.add(sample["pts_s"])
        y, u, v = (normalize_plane(p, result["range"], i > 0) for i, p in enumerate(planes))
        sample["scales"] = spatial_stats(y, u, v)
        result["samples"].append(sample)
    scales = [s for sample in result["samples"] for s in sample.get("scales", [])]
    tiles = sum(s["tiles"] for s in scales)
    live = sum(s["eligible_tiles"] for s in scales)
    scores = [m["p90"] for s in scales for m in s["planes"].values() if m["p90"] is not None]
    result["status"] = "ok" if scores else "insufficient evidence"
    result["reason"] = ("Spatial variation only; motion, texture and intentional grain are not classified"
                        if scores else "no eligible unclipped, low-structure tiles")
    result["coverage"] = round(live / tiles, 4) if tiles else 0
    result["review_score"] = round(float(np.percentile(scores, 90)), 3) if scores else None
    result["evidence"] = "limited" if result["coverage"] < 0.25 else "usable spatial coverage"
    result["unique_samples"] = len(seen)
    result["elapsed_s"] = round(time.monotonic() - start, 3)
    if result["elapsed_s"] > TIME_BUDGET:
        raise RuntimeError("diagnostic per-file time budget exceeded")
    return result
