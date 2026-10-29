#!/usr/bin/env python3
"""Classify a video library by real quality (not file size) using ffprobe.

Quality signal = H.264-equivalent bits-per-pixel, normalised for resolution:
    bpp      = video_bitrate / (width * height * fps)
    eq_bpp   = bpp * CODEC_EFFICIENCY[codec]           # newer codecs do more per bit
    norm_bpp = eq_bpp * (pixels / 1080p_pixels) ** 0.25 # "0.75 power rule": bitrate ~ pixels^0.75
Files are grouped by <resolution tier>/<grade>, e.g. 1080p/good.

--deep also measures *effective* resolution from sampled frames, so high-bitrate but
blurry or upscaled files are grouped by the detail they really contain:
  * round-trip SSIM: downscale a frame by k and scale it back; if almost nothing is lost,
    the frame holds no detail above k * height (catches upscales, soft focus, heavy blur).
  * resdet (https://github.com/0x09/resdet, auto-built into the uv venv on first --deep run):
    exact source height of upscales.
"""
import argparse
import errno
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv", ".asf", ".flv", ".webm", ".mpg", ".mpeg",
    ".m2ts", ".mts", ".ts", ".vob", ".3gp", ".ogv", ".divx", ".xvid", ".rm", ".rmvb", ".f4v",
}

# Relative to H.264 = 1.0. Sources: HEVC 25-50% saving vs AVC, AV1 ~50% vs x264,
# AVC 50%+ vs MPEG-2 (Wikipedia codec articles). Older codecs are rough estimates.
CODEC_EFFICIENCY = {
    "av1": 2.0, "hevc": 1.6, "vvc": 2.4, "vp9": 1.5, "h264": 1.0, "vp8": 0.9,
    "vc1": 0.8, "wmv3": 0.75, "wmv2": 0.55, "wmv1": 0.5, "theora": 0.7,
    "mpeg4": 0.65, "msmpeg4v3": 0.6, "msmpeg4v2": 0.55, "msmpeg4v1": 0.5,
    "h263": 0.5, "flv1": 0.5, "rv40": 0.7, "rv30": 0.6, "mpeg2video": 0.5, "mpeg1video": 0.4,
    "prores": 1.0, "dnxhd": 1.0, "mjpeg": 0.25,
}
DEFAULT_EFFICIENCY = 0.6

# (name, min 16:9-equivalent height = sqrt(w*h*9/16)). Pixel count is orientation/aspect neutral:
# 1920x800 scope -> 929 (1080p), 1080x2400 phone -> 1207 (1080p), 720x1600 phone -> 805 (720p).
RES_TIERS = [("2160p", 1800), ("1440p", 1300), ("1080p", 860), ("720p", 580), ("480p", 400), ("low", 0)]
# Thresholds on norm_bpp (H.264-equivalent at 1080p).
GRADES = [("excellent", 0.15), ("good", 0.08), ("fair", 0.04), ("poor", 0.0)]
REF_PIXELS = 1920 * 1080

# --deep: (scale, SSIM threshold). If the round trip through this scale keeps SSIM above the
# threshold, the frame has no real detail above it. Calibrated on SVT 1080p sequences encoded
# at high bitrate: native frames scored <= 0.968 at 0.75, while every 720p/540p/360p upscale and
# blur scored >= 0.988.
DEEP_SCALES = [(0.75, 0.98), (0.5, 0.985), (0.333, 0.95)]
DEEP_CROP = (0.7, 0.6)  # centre crop (w, h fractions) so letterbox/pillarbox bars don't dilute SSIM
# resdet reports DCT/compression artifacts at 6/8 and 7/8 of the height; ignore those unless strong.
RESDET_ARTIFACT_FRACS, RESDET_ARTIFACT_MIN_CONF = (6 / 8, 7 / 8), 0.66
RESDET_VERSION = "2.4.4"
RESDET_URL = f"https://github.com/0x09/resdet/archive/refs/tags/{RESDET_VERSION}.tar.gz"
# Keys are absolute path + size + mtime, so one cache serves every library.
DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache", "vqc", "cache.jsonl")

def ffprobe(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "ffprobe failed")
    return json.loads(out.stdout)


def _num(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _rate(s):
    try:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else None
    except (AttributeError, ValueError):
        return _num(s)


def _tag_bitrate(stream):
    # mkvmerge writes per-stream stats as tags BPS / BPS-eng.
    tags = {k.upper(): v for k, v in (stream.get("tags") or {}).items()}
    return _num(tags.get("BPS") or tags.get("BPS-ENG"))


def tier_of(w, h):
    eq_h = (w * h * 9 / 16) ** 0.5
    return next(n for n, t in RES_TIERS if eq_h >= t)


def _rotation(v):
    """Display rotation in degrees counter-clockwise (phones store portrait video as rotated landscape)."""
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"]))) % 360
    return -int((v.get("tags") or {}).get("rotate", 0)) % 360


def analyze(path):
    info = ffprobe(path)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if not vids:
        raise RuntimeError("no video stream")
    v = max(vids, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
    w, h = v.get("width") or 0, v.get("height") or 0
    if not w or not h:
        raise RuntimeError("unknown dimensions")

    fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate")) or 25.0
    if fps > 240:
        fps = _rate(v.get("r_frame_rate")) or 30.0
    if fps > 240:
        fps = 30.0
    duration = _num(fmt.get("duration")) or _num(v.get("duration"))
    size = int(fmt.get("size") or os.path.getsize(path))

    vbr, src = _num(v.get("bit_rate")), "stream"
    if not vbr:
        vbr, src = _tag_bitrate(v), "tag"
    if not vbr:
        total = _num(fmt.get("bit_rate")) or (size * 8 / duration if duration else None)
        if total:
            audio = sum(_num(s.get("bit_rate")) or _tag_bitrate(s) or 0
                        for s in streams if s.get("codec_type") == "audio")
            vbr, src = max(total - audio, total * 0.5), "container"
    if not vbr:
        raise RuntimeError("cannot determine bitrate")

    codec = v.get("codec_name", "?")
    pixels = w * h
    bpp = vbr / (pixels * fps)
    norm_bpp = bpp * CODEC_EFFICIENCY.get(codec, DEFAULT_EFFICIENCY) * (pixels / REF_PIXELS) ** 0.25

    tier = tier_of(w, h)
    grade = next(n for n, t in GRADES if norm_bpp >= t)
    hdr = v.get("color_transfer") in ("smpte2084", "arib-std-b67")
    pix = v.get("pix_fmt", "")
    return {
        "path": str(path), "group": f"{tier}/{grade}", "tier": tier, "grade": grade,
        "norm_bpp": round(norm_bpp, 4), "bpp": round(bpp, 4), "codec": codec,
        "width": w, "height": h, "fps": round(fps, 3), "video_kbps": round(vbr / 1000),
        "bitrate_src": src, "bit_depth": 10 if "10" in pix or "12" in pix else 8,
        "hdr": hdr, "duration_min": round(duration / 60, 1) if duration else "",
        "size_mb": round(size / 2**20, 1), "rotation": _rotation(v),
        "_duration_s": duration or 0, "_vstream": v.get("index", 0),
    }


def _sample_frames(path, row, n, y4m):
    """Seek to n points (no full decode) and write one frame from each into a y4m file."""
    dur = row["_duration_s"]
    times = [dur * (0.05 + 0.9 * (i + 0.5) / n) for i in range(n)] if dur > 2 else [0]
    cmd, chains = ["ffmpeg", "-v", "error", "-y"], ""
    for i, t in enumerate(times):
        cmd += ["-noautorotate", "-noaccurate_seek", "-ss", f"{t:.2f}", "-t", "2", "-i", str(path)]
        chains += f"[{i}:{row['_vstream']}]trim=end_frame=1,setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{i}];"
    fc = chains + "".join(f"[v{i}]" for i in range(len(times))) + f"concat=n={len(times)}:v=1:a=0[out]"
    subprocess.run(cmd + ["-filter_complex", fc, "-map", "[out]", "-f", "yuv4mpegpipe", y4m],
                   check=True, capture_output=True, timeout=600)
    return times


def _roundtrip_ssim(y4m, w, h, tmp):
    """Per scale: SSIM of a frame vs. itself downscaled and upscaled back, for each sampled frame."""
    cw, ch = int(w * DEEP_CROP[0]) // 2 * 2, int(h * DEEP_CROP[1]) // 2 * 2
    n = len(DEEP_SCALES)
    fc = f"[0:v]crop={cw}:{ch},format=gray,split={2 * n}" + "".join(f"[o{i}][s{i}]" for i in range(n)) + ";"
    for i, (k, _) in enumerate(DEEP_SCALES):
        sw, sh = max(2, int(cw * k) // 2 * 2), max(2, int(ch * k) // 2 * 2)
        out = "[out]" if i == n - 1 else ",nullsink"
        fc += (f"[s{i}]scale={sw}:{sh}:flags=area,scale={cw}:{ch}:flags=bicubic[r{i}];"
               f"[o{i}][r{i}]ssim=stats_file={tmp}/ssim{i}.log{out};")
    subprocess.run(["ffmpeg", "-v", "error", "-i", y4m, "-filter_complex", fc.rstrip(";"), "-map", "[out]",
                    "-f", "null", "-"], check=True, capture_output=True, timeout=600)
    return [[float(x) for x in re.findall(r"All:([\d.]+)", Path(f"{tmp}/ssim{i}.log").read_text())]
            for i in range(n)]


def _resdet_height(resdet, y4m, h):
    out = subprocess.run([resdet, "-t", "y4m", "-v3", "-f", "auto", y4m],
                         capture_output=True, text=True, timeout=600).stdout.splitlines()
    best = None
    for tok in (out[1].split() if len(out) > 1 else []):
        cand, conf = tok.split(":")
        cand, conf = int(cand), float(conf)
        # Below h/6 is implausible as a real source and comes from periodic patterns (grids, test cards).
        if conf < 0 or cand >= h or cand < h / 6:
            continue
        if any(abs(cand - h * f) <= 2 for f in RESDET_ARTIFACT_FRACS) and conf < RESDET_ARTIFACT_MIN_CONF:
            continue
        if best is None or conf > best[1]:
            best = (cand, conf)
    return best[0] if best else None


SNAP_H, SNAP_CROP_W = 270, 480
TRANSPOSE = {90: "transpose=cclock,", 180: "hflip,vflip,", 270: "transpose=clock,"}


def deep_analyze(row, n_frames, resdet, snap):
    """snap: JPEG path for a strip of the deciding sampled frame: whole frame scaled down | 1:1 centre crop
    (shows real sharpness). Reuses the already decoded frames."""
    w, h = row["width"], row["height"]
    # The 2nd most detailed frame decides, so a few flat/black frames can't make a sharp video look soft.
    pick_i = lambda vals: sorted(range(len(vals)), key=vals.__getitem__)[min(1, len(vals) - 1)]
    pick = lambda vals: vals[pick_i(vals)]
    with tempfile.TemporaryDirectory(prefix="vqc_") as tmp:
        y4m = f"{tmp}/s.y4m"
        times = _sample_frames(row["path"], row, n_frames, y4m)
        per_scale = _roundtrip_ssim(y4m, w, h, tmp)
        rd_h = _resdet_height(resdet, y4m, h) if resdet else None
        i = pick_i(per_scale[0]) if per_scale[0] else 0
        sh = f"min({SNAP_H}\\,ih)"
        vf = (f"select=eq(n\\,{i}),{TRANSPOSE.get(row.get('rotation', 0), '')}split[a][b];"
              f"[a]scale=-2:'{sh}':flags=area[t];[b]crop='min({SNAP_CROP_W}\\,iw)':'{sh}',pad=iw+6:ih:6:0:white[c];"
              "[t][c]hstack")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", y4m, "-filter_complex", vf, "-frames:v", "1",
                        "-q:v", "3", str(snap)], check=True, capture_output=True, timeout=120)
    eff = 1.0
    for (k, thr), vals in zip(DEEP_SCALES, per_scale):
        if not vals or pick(vals) < thr:
            break
        eff = k
    # resdet's exact source height wins; SSIM's coarse estimate can read flat/bokeh content as too soft.
    eff_h = rd_h or round(h * eff)
    return {
        "detail": round(1 - pick(per_scale[0]), 4) if per_scale[0] else "",
        "ssim_eff_height": round(h * eff), "resdet_height": rd_h or "",
        "eff_height": eff_h, "eff_tier": tier_of(round(w * eff_h / h), eff_h),
        "snapshot": Path(snap).name, "snapshot_t": round(times[min(i, len(times) - 1)]),
    }


def scan(root, exts):
    for dirpath, _, files in os.walk(root):
        for f in files:
            if Path(f).suffix.lower() in exts:
                yield Path(dirpath, f)


def load_cache(p):
    cache = {}
    if p and p.exists():
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
                cache[r["_key"]] = r
            except (ValueError, KeyError):
                pass
    return cache


def _md(text):
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", str(text))


def write_report(path, root, groups, errors, options, snaps):
    """Markdown report: options, summary per group, then one table per group (best norm_bpp first).
    snaps: folder (relative to the report) holding --deep snapshots, or None."""
    total = sum(len(rs) for rs in groups.values())
    gb = sum(r["size_mb"] for rs in groups.values() for r in rs) / 1024
    out = ["# Video quality report", "", f"`{root}` · {total} files · {gb:.1f} GB · {time.strftime('%Y-%m-%d %H:%M')}",
           "", "## Options", "", "| option | value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in options]
    out += ["", "## Summary", "", "| group | files | size GB |", "|---|--:|--:|"]
    for g, rs in groups.items():
        out.append(f"| {g} | {len(rs)} | {sum(r['size_mb'] for r in rs) / 1024:.1f} |")
    deep = snaps is not None
    head = ["file", "resolution", "codec", "Mb/s", "norm bpp"] + (["effective", "detail", "snapshot"] if deep else []) \
        + ["min", "MB"]
    for g, rs in groups.items():
        out += ["", f"## {g}", "", "| " + " | ".join(head) + " |",
                "|---|---|---|--:|--:|" + ("--:|--:|---|" if deep else "") + "--:|--:|"]
        for r in rs:
            dw, dh = _display_size(r)
            cells = [_md(os.path.relpath(r["path"], root)), f"{dw}x{dh}", r["codec"],
                     f"{r['video_kbps'] / 1000:.1f}", r["norm_bpp"]]
            if deep:
                f, t = r.get("eff_height", 0) / r["height"], r.get("snapshot_t", 0)
                cells += [f"{round(dw * f)}x{round(dh * f)}" if f else "", r.get("detail", ""),
                          f'![frame at {t // 60}:{t % 60:02d}]({snaps}/{r["snapshot"]} "frame at {t // 60}:{t % 60:02d}")'
                          if r.get("snapshot") else ""]
            cells += [r["duration_min"], round(r["size_mb"])]
            out.append("| " + " | ".join(str(c) for c in cells) + " |")
    if errors:
        out += ["", "## Errors", "", "| file | error |", "|---|---|"]
        out += [f"| {_md(os.path.relpath(f, root))} | {_md(str(e).replace(f'{f}: ', ''))} |" for f, e in errors]
    Path(path).write_text("\n".join(out) + "\n")


def snap_name(key):
    return hashlib.sha1(key.encode()).hexdigest()[:16] + "_strip.jpg"


def quality_code(tier, grade):
    """2-digit rank, 01 = 2160p/excellent ... 24 = low/poor, so an A-Z sort lists the best first."""
    t = [n for n, _ in RES_TIERS].index(tier)
    g = [n for n, _ in GRADES].index(grade)
    return f"{t * len(GRADES) + g + 1:02d}"


PREFIX_RE = re.compile(r"^\d{2}_(%s)-(%s)_" % ("|".join(n for n, _ in RES_TIERS), "|".join(n for n, _ in GRADES)))


def progress(total, desc, initial=0):
    return tqdm(total=total, initial=initial, unit="file", dynamic_ncols=True, colour="green", desc=desc,
                smoothing=0.05, bar_format="{desc} {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                                           "[{elapsed}<{remaining}, {rate_fmt}{postfix}]")


def rename_files(rows, cache_path, snap_dir, undo_path):
    """Prefix each video (and same-stem sidecars such as .srt/.nfo) with RR_<tier>-<grade>_.
    Existing vqc prefixes are replaced, never stacked. Cache rows and snapshots follow the new names so
    nothing is re-analyzed. undo_path gets a script that strips the prefix from every file again.
    A file that cannot be renamed (e.g. open in a player on a Windows share) is rolled back and reported."""
    done = same = 0
    errors, undo, listings = [], [], {}
    try:
        with open(cache_path, "a") as cf, progress(len(rows), "rename") as bar:
            for r in rows:
                p = Path(r["path"])
                bar.set_postfix_str(f"renamed {done}, unchanged {same}, errors {len(errors)} · {p.name[:40]}",
                                    refresh=False)
                tier, grade = r["group"].split("/")
                pre = f"{quality_code(tier, grade)}_{tier}-{grade}_"
                new = p.with_name(pre + PREFIX_RE.sub("", p.name, count=1))
                if p.parent not in listings:  # list each folder once: listings are slow on network drives
                    listings[p.parent] = list(p.parent.iterdir())
                stem = p.stem.casefold() + "."  # players match subtitles case-insensitively on Windows
                moves = [(p, new)] + [(sc, sc.with_name(pre + PREFIX_RE.sub("", sc.name, count=1)))
                                      for sc in listings[p.parent] if sc != p and sc.name.casefold().startswith(stem)
                                      and sc.suffix.lower() not in VIDEO_EXTS]
                if new == p:
                    same += 1
                elif clash := [str(dst) for _, dst in moves if dst.exists() or dst.is_symlink()]:
                    errors.append((p, f"rename skipped, target exists: {', '.join(clash)}"))
                    moves = [(p, p)]
                else:
                    st = p.stat()
                    moved = []
                    try:
                        for src, dst in moves:
                            os.rename(src, dst)
                            moved.append((src, dst))
                    except OSError as e:
                        for src, dst in reversed(moved):
                            try:
                                os.rename(dst, src)
                            except OSError:
                                pass  # left renamed: the undo script below still covers it
                        moves = [(s_, d_) for s_, d_ in moved if d_.exists()] or [(p, p)]
                        hint = " Is it open in another program?" if e.errno in (errno.EACCES, errno.EPERM,
                                                                                    errno.EBUSY) else ""
                        errors.append((p, f"rename failed, file left unchanged: {e.strerror or e} "
                                          f"({Path(e.filename or p).name}).{hint}"))
                    else:
                        done += 1
                        new_key = f"{new}|{st.st_size}|{int(st.st_mtime)}"  # rename keeps size and mtime
                        r["path"] = str(new)
                        old_snap, new_snap = snap_dir / str(r.get("snapshot")), snap_dir / snap_name(new_key)
                        if r.get("snapshot") and old_snap.is_file():
                            # hard link: the old name stays cached too (undo hits the cache), no extra space
                            new_snap.unlink(missing_ok=True)
                            try:
                                os.link(old_snap, new_snap)
                            except OSError:
                                shutil.copy2(old_snap, new_snap)
                            r["snapshot"] = new_snap.name
                        cf.write(json.dumps({**r, "_key": new_key}) + "\n")
                        cf.flush()
                for _, dst in moves:
                    if PREFIX_RE.match(dst.name):
                        undo.append(f"mv -n -- {shlex.quote(str(dst))} "
                                    f"{shlex.quote(str(dst.with_name(PREFIX_RE.sub('', dst.name, count=1))))}")
                bar.update()
            bar.set_postfix_str(f"renamed {done}, unchanged {same}, errors {len(errors)}")
    finally:  # written even if something unexpected stops the loop, so every renamed file can be restored
        if undo:
            undo_path.write_text("#!/bin/sh\n# Generated by vqc --rename: restores the names without the quality prefix.\n"
                                 + "\n".join(undo) + "\n")
            undo_path.chmod(0o755)
        else:
            undo_path.unlink(missing_ok=True)
    return done, same, errors


def _display_size(r):
    w, h = r["width"], r["height"]
    return (h, w) if r.get("rotation", 0) in (90, 270) else (w, h)


def link_snapshots(report, snap_dir, rows):
    """Markdown previews (e.g. VS Code) only load images under the workspace or the report's folder, so
    each snapshot used by the report is symlinked into vqc_snapshots/ next to it; the JPEGs stay in the cache."""
    d = Path(report).resolve().parent / "vqc_snapshots"
    if d.is_symlink():  # older vqc versions linked the whole folder
        d.unlink()
    d.mkdir(exist_ok=True)
    for r in rows:
        if r.get("snapshot"):
            link, target = d / r["snapshot"], snap_dir / r["snapshot"]
            if link.is_symlink() or link.exists():
                if link.is_symlink() and os.readlink(link) == str(target) or (
                        not link.is_symlink() and link.stat().st_size == target.stat().st_size):
                    continue
                link.unlink()
            try:
                link.symlink_to(target)
            except OSError:  # e.g. report on a Windows network drive: no symlinks there, copy the small JPEG
                shutil.copy2(target, link)
    return d.name


def in_uv_venv():
    cfg = Path(sys.prefix, "pyvenv.cfg")
    return sys.prefix != sys.base_prefix and cfg.is_file() and re.search(r"^uv\s*=", cfg.read_text(), re.M)


def ensure_resdet():
    """Return the resdet binary inside this venv, building it there on first use (needs bash, cc, make)."""
    exe = Path(sys.prefix, "bin", "resdet")
    if exe.is_file():
        return str(exe)
    log = Path(sys.prefix, "resdet-build.log")
    print(f"resdet {RESDET_VERSION} not in venv; building it into {exe.parent} (one-time)...", file=sys.stderr)
    step = "download"
    log.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with urllib.request.urlopen(RESDET_URL, timeout=60) as resp:
                with tarfile.open(fileobj=io.BytesIO(resp.read()), mode="r:gz") as tf:
                    tf.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
            src = Path(tmp, f"resdet-{RESDET_VERSION}")
            # Only the built-in Y4M reader is needed: no pkg-config, no optional libs (whose versions can break
            # the build). "bash configure" also works when /tmp is mounted noexec.
            for step, cmd in (("configure", ["bash", "configure", "--disable-everything"]),
                              ("make", ["make", f"-j{os.cpu_count() or 2}"])):
                # -Wno-error: resdet builds with -Werror, which breaks on newer compilers' new warnings.
                env = {**os.environ, "CFLAGS": (os.environ.get("CFLAGS", "") + " -Wno-error").strip()}
                p = subprocess.run(cmd, cwd=src, capture_output=True, text=True, env=env)
                with open(log, "a") as fh:
                    fh.write(f"$ {' '.join(cmd)}\n{p.stdout}\n{p.stderr}\n")
                if p.returncode:
                    raise RuntimeError((p.stdout + p.stderr).strip()[-600:])
            shutil.copy2(src / "resdet", exe)
        return str(exe)
    except Exception as e:
        hint = {"download": f"check network/proxy access to {RESDET_URL}",
                "configure": "needs bash and a C compiler (sudo apt install build-essential)",
                "make": "needs make and a C compiler (sudo apt install build-essential)"}[step]
        print(f"WARNING: resdet build failed at {step}: {e}\n"
              f"  Hint: {hint}. Full log: {log}\n"
              "  --deep continues with round-trip SSIM only (upscale source heights are less exact).\n"
              "  Or build it yourself and pass --resdet /path/to/resdet", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="library folder to scan (recursive)")
    ap.add_argument("-o", "--report", type=Path, default=Path("video_quality.md"), help="Markdown report path")
    ap.add_argument("-j", "--jobs", type=int, default=min(16, (os.cpu_count() or 4) * 2), help="parallel ffprobe jobs")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                    help="results cache shared by all libraries; unchanged files are skipped (default: %(default)s)")
    ap.add_argument("--ext", help="comma-separated extensions to include (default: common video types)")
    ap.add_argument("--deep", action="store_true",
                    help="sample frames to measure effective resolution (finds blurry/upscaled files; slower)")
    ap.add_argument("--frames", type=int, default=8, help="frames sampled per file with --deep")
    ap.add_argument("--rename", action="store_true",
                    help="prefix files with their quality rank, e.g. 10_1080p-good_Movie.mkv (see QUALITY_CODES.md)")
    ap.add_argument("--resdet", help="path to a resdet binary for exact upscale detection "
                    "(default: <venv>/bin/resdet, built automatically on first --deep run)")
    a = ap.parse_args()

    if not in_uv_venv():
        sys.exit("vqc must run from its uv environment:\n"
                 "  cd /path/to/video_quality_classifier && uv run vqc /path/to/library ...\n"
                 "  or install once:  uv tool install /path/to/video_quality_classifier  ->  vqc /path/to/library ...")
    if a.resdet and not shutil.which(a.resdet):
        ap.error(f"--resdet {a.resdet}: not found or not executable")
    a.cache = a.cache.resolve()
    snap_dir = a.cache.parent / "snapshots"
    if a.deep:
        snap_dir.mkdir(parents=True, exist_ok=True)
        a.resdet = a.resdet or ensure_resdet()
        if a.resdet:
            print(f"resdet: {a.resdet}", file=sys.stderr)
    root = a.root.resolve()
    exts = {("." + e.strip(".").lower()) for e in a.ext.split(",")} if a.ext else VIDEO_EXTS
    files = list(scan(root, exts))
    print(f"Found {len(files)} video files under {root}", file=sys.stderr)

    cache = load_cache(a.cache)
    rows, errors, todo = [], [], []
    for f in files:
        st = f.stat()
        key = f"{f}|{st.st_size}|{int(st.st_mtime)}"
        cached = {k: v for k, v in cache[key].items() if k != "_key"} if key in cache else None
        if cached and "rotation" not in cached:  # from an older vqc version
            cached = None
        if cached and (not a.deep or (cached.get("snapshot") == snap_name(key) and (snap_dir / snap_name(key)).is_file())):
            rows.append(cached)
        else:
            todo.append((f, key, cached))
    n_cached = len(rows)

    def work(f, key, cached):
        r = cached or analyze(f)
        if a.deep:
            try:
                snap = snap_dir / snap_name(key)
                r = {**r, **deep_analyze(r, a.frames, a.resdet, snap)}
            except Exception as e:  # keep the metadata result if frame sampling fails
                return r, f"deep: {e}"
        return r, None

    a.cache.parent.mkdir(parents=True, exist_ok=True)
    bar = progress(len(files), "deep" if a.deep else "probe", initial=n_cached)
    bar.set_postfix_str(f"cached {n_cached}, errors 0")
    with bar, ThreadPoolExecutor(a.jobs) as pool, open(a.cache, "a") as cf:
        futs = {pool.submit(work, f, key, cached): (f, key) for f, key, cached in todo}
        for fut in as_completed(futs):
            f, key = futs[fut]
            try:
                r, err = fut.result()
                rows.append(r)
                cf.write(json.dumps({**r, "_key": key}) + "\n")
                cf.flush()
                if err:
                    errors.append((f, err))
            except Exception as e:  # keep going on corrupt/odd files
                errors.append((f, str(e)))
            bar.set_postfix_str(f"cached {n_cached}, errors {len(errors)} · {f.name[:40]}", refresh=False)
            bar.update()

    tier_rank = {n: i for i, (n, _) in enumerate(RES_TIERS)}
    grade_rank = {n: i for i, (n, _) in enumerate(GRADES)}
    # Without --deep, ignore deep results that may be in the cache so the report matches the options.
    for r in rows:  # recompute so cached rows follow the current tier rules
        r["tier"] = tier_of(r["width"], r["height"])
        if r.get("eff_height"):
            r["eff_tier"] = tier_of(round(r["width"] * r["eff_height"] / r["height"]), r["eff_height"])
    tier = lambda r: (a.deep and r.get("eff_tier")) or r["tier"]
    rows.sort(key=lambda r: (tier_rank[tier(r)], grade_rank[r["grade"]], -r["norm_bpp"]))
    groups = defaultdict(list)
    for r in rows:
        r["group"] = f"{tier(r)}/{r['grade']}"
        groups[r["group"]].append(r)
    rename_info = []
    if a.rename:
        undo = a.report.resolve().parent / "vqc_undo_rename.sh"
        done, same, rn_err = rename_files(rows, a.cache, snap_dir, undo)
        errors += rn_err
        rename_info = [("rename", f"{done} renamed, {same} already named, {len(rn_err)} skipped; "
                                  f"undo: `{undo}`")]
        print(f"Renamed {done} files ({same} already named, {len(rn_err)} skipped). Undo: {undo}", file=sys.stderr)
    options = [
        ("command", f"`vqc {shlex.join(sys.argv[1:])}`"),
        ("mode", f"**deep**: {a.frames} frames per file, round-trip SSIM + "
                 + (f"resdet (`{a.resdet}`)" if a.resdet else "**no resdet** (SSIM only)")
                 if a.deep else "**metadata only** (no `--deep`: upscaled/blurry files are not detected)"),
        ("extensions", " ".join(sorted(exts)) if a.ext else f"default ({len(exts)} video types)"),
        ("files", f"{len(files)} found: {n_cached} from cache, {len(todo)} analyzed now, {len(errors)} with errors"),
        ("cache", f"`{a.cache}`"),
    ] + ([("snapshots", f"`{snap_dir}`")] if a.deep else []) + rename_info
    write_report(a.report, root, groups, errors, options, link_snapshots(a.report, snap_dir, rows) if a.deep else None)

    print(f"\n{'group':<18}{'files':>7}{'size GB':>10}")
    for g, rs in groups.items():
        print(f"{g:<18}{len(rs):>7}{sum(r['size_mb'] for r in rs) / 1024:>10.1f}")
    print(f"\nReport: {a.report.resolve()}  ({len(rows)} files, {len(errors)} errors)")
    for f, e in errors[:20]:
        print(f"  FAILED {f}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
