import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import noise_diagnostics as diagnostics
import vqc


class NoiseDiagnosticTests(unittest.TestCase):
    def test_temporal_noise_and_fine_detail(self):
        rng = np.random.default_rng(42)
        y = (0.25 + rng.normal(0, 15 / 255, (256, 256))).astype(np.float32)
        u = np.zeros((128, 128), dtype=np.float32)
        baseline = (y, u, u)
        clean = diagnostics.temporal_pair(baseline, (y + 0.01 / 255, u, u))
        noisy = diagnostics.temporal_pair(
            (y + rng.normal(0, 3 / 255, y.shape), u, u),
            (y + rng.normal(0, 3 / 255, y.shape), u, u))
        chroma = diagnostics.temporal_pair(
            (y, u + rng.normal(0, 3 / 255, u.shape), u),
            (y, u + rng.normal(0, 3 / 255, u.shape), u))
        for result in (clean, noisy, chroma):
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["shift_yx"], [0, 0])
        self.assertLess(clean["scales"][0]["planes"]["Y"]["p90"], 0.01)
        self.assertGreater(clean["scales"][0]["coverage"], 0.9)
        self.assertGreater(noisy["scales"][0]["planes"]["Y"]["p90"], 3.5)
        self.assertEqual(chroma["scales"][0]["planes"]["Y"]["p90"], 0)
        self.assertGreater(chroma["scales"][0]["planes"]["U"]["p90"], 3.5)
        self.assertEqual(chroma["scales"][0]["planes"]["V"]["p90"], 0)
        self.assertGreater(chroma["scales"][0]["planes"]["U"]["bands"]["midtones"]["tiles"], 0)
        # Correlated low-frequency disturbance must remain visible when the scene itself is stable.
        coarse_a = rng.normal(0, 1 / 255, (64, 64)).repeat(4, axis=0).repeat(4, axis=1)
        coarse_b = rng.normal(0, 1 / 255, (64, 64)).repeat(4, axis=0).repeat(4, axis=1)
        correlated = diagnostics.temporal_pair((y + coarse_a, u, u), (y + coarse_b, u, u))
        self.assertEqual(correlated["status"], "ok", correlated)
        self.assertGreater(correlated["scales"][1]["planes"]["Y"]["p90"], 1)

    def test_encoded_detail_vs_temporal_noise(self):
        rng = np.random.default_rng(312)
        h, w = 192, 256
        y = np.clip(0.3 + rng.normal(0, 15 / 255, (h, w)), 0.1, 0.7)
        u = rng.normal(0, 8 / 255, (h // 2, w // 2))
        results = {}
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("clean-detail", "moving-detail", "luma-noise", "chroma-noise"):
                video, raw = Path(tmp) / f"{name}.mp4", Path(tmp) / f"{name}.yuv"
                with raw.open("wb") as out:
                    for i in range(16):
                        shift = 2 * i if name == "moving-detail" else 0
                        yy = np.roll(y, shift, axis=1).copy()
                        uu = np.roll(u, shift // 2, axis=1).copy()
                        if name == "luma-noise":
                            yy += rng.normal(0, 5 / 255, yy.shape)
                        elif name == "chroma-noise":
                            uu += rng.normal(0, 5 / 255, uu.shape)
                        for plane in (np.round(yy * 219 + 16), np.round(uu * 224 + 128),
                                      np.full(uu.shape, 128)):
                            out.write(plane.astype(np.uint8).tobytes())
                subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "yuv420p",
                                "-video_size", f"{w}x{h}", "-framerate", "5", "-i", str(raw),
                                "-c:v", "libx264", "-crf", "12", "-g", "10",
                                "-color_range", "tv", "-color_trc", "bt709", str(video)],
                               check=True, capture_output=True, timeout=30)
                result = diagnostics.analyze_noise(vqc.analyze(video), 2, vqc.ffprobe(video))
                results[name] = result["review_score"]
                if name in ("luma-noise", "chroma-noise"):
                    self.assertEqual(result["status"], "ok", result["reason"])
                    self.assertGreater(result["review_score"], 4, results)
                elif result["review_score"] is not None:
                    self.assertLess(result["review_score"], 2, results)
            for noisy in ("luma-noise", "chroma-noise"):
                self.assertGreater(results[noisy], 2 * max(results["clean-detail"] or 0,
                                                         results["moving-detail"] or 0), results)

    def test_high_resolution_fine_detail_translation(self):
        rng = np.random.default_rng(77)
        y = (0.3 + rng.normal(0, 15 / 255, (2160, 3840))).astype(np.float32)
        u = np.zeros((1080, 1920), dtype=np.float32)
        result = diagnostics.temporal_pair((y, u, u), (np.roll(y, 4, axis=1), u, u))
        self.assertEqual(result["status"], "ok", result["reason"])
        self.assertEqual(result["shift_yx"], [0, 4])
        for scale in result["scales"]:
            self.assertGreater(scale["coverage"], 0.9)
            self.assertEqual(scale["planes"]["Y"]["p90"], 0)
        half_shift = diagnostics.temporal_pair((y, u, u), ((y + np.roll(y, 1, axis=1)) / 2, u, u))
        self.assertEqual(half_shift["status"], "rejected", half_shift["reason"])
        self.assertTrue(all(s["planes"]["Y"]["p90"] is None for s in half_shift["scales"]))

    def test_translation_rejection_and_repeated_frames(self):
        rng = np.random.default_rng(24)
        y = (0.3 + rng.normal(0, 20 / 255, (256, 256))).astype(np.float32)
        u = (rng.normal(0, 8 / 255, (128, 128))).astype(np.float32)
        for dy, dx in ((4, -6), (-3, 5), (0, 0)):
            with self.subTest(dy=dy, dx=dx):
                shifted = np.roll(y, (dy, dx), axis=(0, 1)) + 0.01 / 255
                shifted_u = np.roll(u, (dy // 2, dx // 2), axis=(0, 1))
                result = diagnostics.temporal_pair((y, u, u), (shifted, shifted_u, shifted_u))
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["shift_yx"], [dy, dx])
                self.assertLess(result["scales"][0]["planes"]["Y"]["p90"], 0.01)
                self.assertEqual(result["chroma_measured"], dy % 2 == dx % 2 == 0)
                if result["chroma_measured"]:
                    self.assertLess(result["scales"][0]["planes"]["U"]["p90"], 0.01)
        repeated = diagnostics.temporal_pair((y, u, u), (y, u, u))
        self.assertEqual(repeated["status"], "repeated")
        for altered in (y + 10 / 255, rng.uniform(0.1, 0.6, y.shape).astype(np.float32)):
            result = diagnostics.temporal_pair((y, u, u), (altered, u, u))
            self.assertEqual(result["status"], "rejected", result)
        # A half-pixel translation must not turn a clean detailed image into a high noise score.
        half_shift = (y + np.roll(y, 1, axis=1)) / 2
        result = diagnostics.temporal_pair((y, u, u), (half_shift, u, u))
        self.assertEqual(result["status"], "rejected", result)
        self.assertTrue(all(s["planes"]["Y"]["p90"] is None for s in result["scales"]))
        local_motion = y.copy()
        local_motion[:128, :128] = np.roll(y[:128, :128], (7, 9), axis=(0, 1))
        local = diagnostics.temporal_pair((y, u, u), (local_motion, u, u))
        self.assertEqual(local["status"], "ok", local)
        self.assertLess(local["scales"][0]["coverage"], 0.85)
        self.assertLess(local["scales"][0]["planes"]["Y"]["p90"], 0.01)

    def test_frozen_noise_refresh_and_bad_timing_abstain(self):
        rng = np.random.default_rng(42)
        y = (0.25 + rng.normal(0, 3 / 255, (128, 128))).astype(np.float32)
        uv = np.full((64, 64), 32768, dtype=np.uint16)
        encoded = np.round(y * 56064 + 4096).astype(np.uint16)
        refreshed = np.round((y + rng.normal(0, 3 / 255, y.shape)) * 56064 + 4096).astype(np.uint16)
        row = {"path": "unused.mp4", "_vstream": 0, "width": 128, "height": 128,
               "fps": 10, "_duration_s": 0.4}
        info = {"streams": [{"index": 0, "pix_fmt": "yuv420p",
                             "color_transfer": "bt709", "color_range": "tv"}]}
        for times in ((0, 0.1, 0.2, 0.3), (0, 0.5, 1, 1.5)):
            burst = [({"pts_s": t, "requested_s": 0, "frame_type": "P"}, (p, uv, uv))
                     for t, p in zip(times, (encoded, encoded, encoded, refreshed))]
            with patch.object(diagnostics, "sample_burst", return_value=iter(burst)):
                result = diagnostics.analyze_noise(row, 1, info)
            self.assertEqual(result["status"], "insufficient evidence")
            self.assertIsNone(result["review_score"])
            if times[1] == 0.1:
                self.assertEqual(result["repeated_pairs"], 2)
            else:
                self.assertEqual(result["rejected_pairs"], 3)

    def test_gradients_black_floor_and_local_motion(self):
        axis = np.linspace(0.2, 0.5, 256, dtype=np.float32)
        gradient = np.tile(axis, (256, 1))
        u = np.zeros((128, 128), dtype=np.float32)
        result = diagnostics.temporal_pair((gradient, u, u), (gradient + 0.01 / 255, u, u))
        self.assertEqual(result["status"], "ok", result)
        self.assertLess(result["scales"][0]["planes"]["Y"]["p90"], 0.01)
        black = diagnostics.temporal_pair((np.zeros_like(gradient), u, u),
                                         (np.full_like(gradient, 0.001), u, u))
        self.assertEqual(black["status"], "rejected")
        for result in black["scales"]:
            self.assertEqual(result["eligible_tiles"], 0)
            self.assertIsNone(result["planes"]["Y"]["p90"])
        moving = gradient.copy()
        moving[64:128, 64:128] += 0.15
        result = diagnostics.temporal_pair((gradient, u, u), (moving, u, u))
        self.assertEqual(result["status"], "ok", result)
        self.assertLess(result["scales"][0]["coverage"], 0.96)
        self.assertLess(result["scales"][0]["planes"]["Y"]["p90"], 0.01)
        with self.assertRaisesRegex(ValueError, "unsupported color range"):
            diagnostics.normalize_plane(np.zeros((1, 1), dtype=np.uint16), "unknown")

    def test_unsupported_representation_does_not_decode(self):
        row = {"_vstream": 0, "width": 256, "height": 144}
        for transfer, color_range in (("smpte2084", "tv"), ("arib-std-b67", "tv"),
                                      ("unknown", "tv"), ("bt709", "unknown")):
            with self.subTest(transfer=transfer, color_range=color_range):
                info = {"streams": [{"index": 0, "pix_fmt": "yuv420p",
                                     "color_transfer": transfer, "color_range": color_range}]}
                with patch.object(diagnostics, "sample_burst") as decode:
                    result = diagnostics.analyze_noise(row, 8, info)
                decode.assert_not_called()
                self.assertEqual(result["status"], "unsupported")
                self.assertNotIn("review_score", result)
                self.assertTrue(result["reason"])

    def test_native_depth_and_range_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rng = np.random.default_rng(12)
            y = np.clip(0.25 + rng.normal(0, 3 / 255, (4, 96, 128)), 0, 1)
            uv = rng.normal(0, 3 / 255, (4, 48, 64))
            for depth, color_range in ((8, "tv"), (10, "tv"), (8, "pc"), (10, "pc")):
                unit = 2 ** (depth - 8)
                y_offset, y_span = (16 * unit, 219 * unit) if color_range == "tv" else (0, 2**depth - 1)
                c_span = 224 * unit if color_range == "tv" else 2**depth - 1
                dtype = np.uint8 if depth == 8 else "<u2"
                planes = [np.round(y * y_span + y_offset).astype(dtype),
                          np.round(uv * c_span + 128 * unit).astype(dtype),
                          np.full(uv.shape, 128 * unit, dtype=dtype)]
                raw, video = root / "input.yuv", root / f"{depth}-{color_range}.mkv"
                raw.write_bytes(b"".join(p[i].tobytes() for i in range(4) for p in planes))
                pixel_format = "yuv420p" if depth == 8 else "yuv420p10le"
                subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", pixel_format,
                                "-video_size", "128x96", "-framerate", "5", "-i", str(raw),
                                "-frames:v", "4", "-c:v", "ffv1", "-color_range", color_range,
                                "-color_trc", "bt709", str(video)], check=True, capture_output=True, timeout=30)
                row = vqc.analyze(video)
                result = diagnostics.analyze_noise(row, 1, vqc.ffprobe(video))
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["bit_depth"], depth)
                score = result["bursts"][0]["pairs"][0]["scales"][0]["planes"]
                for name, encoded, span, block in (("Y", planes[0], y_span, 32),
                                                    ("U", planes[1], c_span, 16)):
                    delta = (encoded[1].astype(float) - encoded[0].astype(float)) / span
                    tiles = delta.reshape(3, block, 4, block).transpose(0, 2, 1, 3)
                    center = np.median(tiles, axis=(-2, -1), keepdims=True)
                    expected = np.percentile(np.median(np.abs(tiles - center), axis=(-2, -1))
                                             * 1.4826 * 255, 90)
                    self.assertAlmostEqual(score[name]["p90"], expected, delta=0.05)
                sample = result["samples"][0]
                self.assertEqual(sample["pts_s"], 0)
                self.assertEqual(sample["frame_type"], "I")
                _, decoded = next(diagnostics.sample_burst(video, 0, 0, 128, 96, color_range, 30))
                normalized = diagnostics.normalize_plane(decoded[0], color_range)
                np.testing.assert_allclose(normalized, y[0], atol=0.5 / y_span + 1 / 1023)

    def test_cli_cache_upgrade_default_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video, cache, report = root / "video.mp4", root / "cache.jsonl", root / "report.md"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "color=c=gray:s=128x96:r=10:d=3", "-vf", "noise=alls=8:allf=t",
                            "-c:v", "libx264", "-g", "20", "-crf", "10",
                            "-color_range", "tv", "-color_trc", "bt709", str(video)],
                           check=True, capture_output=True, timeout=30)
            args = ["vqc", str(root), "--cache", str(cache), "-o", str(report), "--frames", "3", "-j", "1"]

            def run(extra):
                with patch("sys.argv", args + extra), patch.object(vqc, "in_uv_venv", return_value=True), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    vqc.main()

            run([])
            baseline = next(iter(vqc.load_cache(cache).values()))
            self.assertNotIn("_noise_diag", baseline)
            with cache.open("a") as out:
                out.write(json.dumps({**baseline, "_noise_diag": {
                    "signature": {"version": 1, "frames": 3},
                    "status": "ok", "review_score": 999}}) + "\n")
            with patch.object(vqc, "ensure_resdet", side_effect=AssertionError("network setup")):
                run(["--noise-diagnostics"])
            upgraded = next(iter(vqc.load_cache(cache).values()))
            self.assertEqual({k: v for k, v in upgraded.items() if k != "_noise_diag"}, baseline)
            result = upgraded["_noise_diag"]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["method"], "aligned-temporal")
            self.assertEqual(len(result["bursts"]), 3)
            self.assertEqual(result["unique_samples"], 12)
            self.assertTrue(any(s["frame_type"] in ("P", "B") for s in result["samples"]))
            for sample in result["samples"][::4]:
                self.assertGreaterEqual(sample["pts_s"], sample["requested_s"])
                self.assertLess(sample["pts_s"] - sample["requested_s"], 0.101)
            self.assertTrue((root / "report_noise.md").is_file())
            self.assertIn("temporal residual", (root / "report_noise.md").read_text())
            self.assertNotIn("spatial variation", (root / "report_noise.md").read_text())
            self.assertIn("Local temporal review", report.read_text())
            with patch.object(diagnostics, "analyze_noise", side_effect=AssertionError("cached")):
                run(["--noise-diagnostics"])
            with patch.object(diagnostics, "VERSION", diagnostics.VERSION + 1), \
                    patch.object(diagnostics, "analyze_noise", wraps=diagnostics.analyze_noise) as analyzer:
                run(["--noise-diagnostics"])
                analyzer.assert_called_once()
            run([])
            self.assertNotIn("Local temporal review", report.read_text())
            self.assertEqual(baseline["group"], next(iter(vqc.load_cache(cache).values()))["group"])
            with patch.object(diagnostics, "analyze_noise", side_effect=RuntimeError("decode failed")):
                run(["--noise-diagnostics", "--force"])
            failed = next(iter(vqc.load_cache(cache).values()))
            self.assertEqual(failed["_noise_diag"]["status"], "error")
            self.assertEqual(failed["group"], baseline["group"])
            self.assertIn("decode failed", report.read_text())

    def test_deep_diagnostics_does_not_download_or_recompute_cached_deep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video, cache, report = root / "video.mp4", root / "cache.jsonl", root / "report.md"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "color=c=gray:s=128x96:r=10:d=1", "-vf", "noise=alls=8:allf=t",
                            "-c:v", "libx264", "-color_range", "tv", "-color_trc", "bt709", str(video)],
                           check=True, capture_output=True, timeout=30)
            args = ["vqc", str(root), "--cache", str(cache), "-o", str(report),
                    "--frames", "1", "-j", "1", "--deep"]
            with patch.object(vqc, "in_uv_venv", return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with patch("sys.argv", args), patch.object(vqc, "ensure_resdet", return_value=None):
                    vqc.main()
                baseline = next(iter(vqc.load_cache(cache).values()))
                self.assertEqual(baseline["_noise_diag"]["status"], "ok")
                with patch("sys.argv", args + ["--noise-diagnostics"]), \
                        patch.object(vqc, "ensure_resdet", side_effect=AssertionError("download")), \
                        patch.object(vqc, "deep_analyze", side_effect=AssertionError("repeat deep")):
                    vqc.main()
                updated = next(iter(vqc.load_cache(cache).values()))
                self.assertEqual(updated, baseline)
                self.assertEqual(updated["_noise_diag"]["status"], "ok")

    def test_source_offset_and_short_black_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, color, duration, offset in (("offset", "gray", 3, 2), ("black", "black", 0.1, 0)):
                with self.subTest(name=name):
                    video = Path(tmp) / f"{name}.mp4"
                    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                    f"color=c={color}:s=128x96:r=10:d={duration}",
                                    "-vf", f"setpts=PTS+{offset}/TB", "-vsync", "0",
                                    "-c:v", "libx264", "-color_range", "tv",
                                    "-color_trc", "bt709", str(video)],
                                   check=True, capture_output=True, timeout=30)
                    row = vqc.analyze(video, offline=True)
                    with patch("subprocess.run", wraps=subprocess.run) as run:
                        result = diagnostics.analyze_noise(row, 3, vqc.ffprobe(video, offline=True))
                        vqc.deep_analyze(row, 1, None, Path(tmp) / f"{name}.jpg", offline=True)
                    for call in run.call_args_list:
                        cmd = call.args[0]
                        if ("-i" in cmd and str(video) in cmd) or "-show_streams" in cmd:
                            self.assertIn("-protocol_whitelist", cmd)
                            self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "file,pipe")
                        if "-copyts" in cmd:
                            self.assertLess(cmd.index("-threads"), cmd.index("-i"))
                            self.assertEqual(cmd[cmd.index("-threads") + 1], "1")
                    if offset:
                        self.assertEqual(result["start_time_s"], offset)
                        for sample in result["samples"][::4]:
                            self.assertAlmostEqual(sample["pts_s"] - offset, sample["actual_s"])
                            self.assertLess(abs(sample["actual_s"] - sample["requested_s"]), 0.101)
                    else:
                        self.assertEqual(result["status"], "insufficient evidence")
                        self.assertIsNone(result["review_score"])
                        self.assertEqual(result["unique_samples"], 1)
                        self.assertEqual(result["coverage"], 0)
                        self.assertEqual(result["measured_pairs"], 0)
                        self.assertEqual(len(result["bursts"]), 1)

    def test_diagnostic_report_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [{"path": str(root / f"video{i:04d}.mp4"), "width": 128, "height": 96,
                     "codec": "h264", "video_kbps": 1000, "norm_bpp": 0.1, "duration_min": 1,
                     "size_mb": 1, "group": "low/good", "_noise_diag": {
                         "status": "ok", "review_score": i, "coverage": 0.5, "reason": "experimental"}}
                    for i in range(201)]
            vqc.write_report(root / "noise.md", root, {"Diagnostic review": rows}, [], [], None, diagnostics=True)
            parts = sorted(root.glob("noise_part*.md"))
            self.assertEqual(len(parts), 2)
            for i, part in enumerate(parts):
                text = part.read_text()
                self.assertEqual(sum(line.startswith("| video") for line in text.splitlines()), 200 if i == 0 else 1)
                self.assertIn("temporal residual", text)


if __name__ == "__main__":
    unittest.main()
