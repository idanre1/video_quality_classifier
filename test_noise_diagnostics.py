import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import noise_diagnostics as diagnostics
import vqc


class NoiseDiagnosticTests(unittest.TestCase):
    def test_correlated_and_chroma_noise(self):
        rng = np.random.default_rng(42)
        y = np.full((256, 256), 0.25, dtype=np.float32)
        u = np.zeros((128, 128), dtype=np.float32)
        coarse = rng.normal(0, 3 / 255, (32, 32)).astype(np.float32)
        for _ in range(3):
            coarse = np.repeat(np.repeat(coarse, 2, axis=0), 2, axis=1)
        for _ in range(3):
            p = np.pad(coarse, 1, mode="reflect")
            coarse = (4 * p[1:-1, 1:-1] + p[:-2, 1:-1] + p[2:, 1:-1]
                      + p[1:-1, :-2] + p[1:-1, 2:]) / 8
        clean = diagnostics.spatial_stats(y, u, u)
        noisy = diagnostics.spatial_stats(y + coarse, u, u)
        chroma = diagnostics.spatial_stats(y, u + coarse[::2, ::2], u)
        self.assertLess(vqc._noise_sigma(((y + coarse) * 219 + 16).astype(np.uint8)), 0.2)
        for scale in range(3):
            self.assertEqual(clean[scale]["planes"]["Y"]["p90"], 0)
            self.assertGreater(noisy[scale]["planes"]["Y"]["p90"], 1.5)
            self.assertEqual(chroma[scale]["planes"]["Y"]["p90"], 0)
            self.assertGreater(chroma[scale]["planes"]["U"]["p90"], 1.5)
            self.assertEqual(chroma[scale]["planes"]["V"]["p90"], 0)
            self.assertGreater(noisy[scale]["coverage"], 0.9)
            self.assertGreater(noisy[scale]["planes"]["Y"]["bands"]["midtones"]["tiles"], 0)
            self.assertGreater(chroma[scale]["planes"]["U"]["regions"]["top-left"]["p90"], 1)

    def test_gradients_and_black_floor(self):
        axis = np.linspace(0.2, 0.5, 256, dtype=np.float32)
        gradient = np.tile(axis, (256, 1))
        u = np.zeros((128, 128), dtype=np.float32)
        for result in diagnostics.spatial_stats(gradient, u, u):
            self.assertLess(result["planes"]["Y"]["p90"], 0.01)
        for result in diagnostics.spatial_stats(np.zeros_like(gradient), u, u):
            self.assertEqual(result["eligible_tiles"], 0)
            self.assertIsNone(result["planes"]["Y"]["p90"])
        with self.assertRaisesRegex(ValueError, "unsupported color range"):
            diagnostics.normalize_plane(np.zeros((1, 1), dtype=np.uint16), "unknown")

    def test_unsupported_representation_does_not_decode(self):
        row = {"_vstream": 0, "width": 256, "height": 144}
        for transfer, color_range in (("smpte2084", "tv"), ("arib-std-b67", "tv"),
                                      ("unknown", "tv"), ("bt709", "unknown")):
            with self.subTest(transfer=transfer, color_range=color_range):
                info = {"streams": [{"index": 0, "pix_fmt": "yuv420p",
                                     "color_transfer": transfer, "color_range": color_range}]}
                with patch.object(diagnostics, "sample_frame") as decode:
                    result = diagnostics.analyze_noise(row, 8, info)
                decode.assert_not_called()
                self.assertEqual(result["status"], "unsupported")
                self.assertNotIn("review_score", result)
                self.assertTrue(result["reason"])

    def test_native_depth_and_range_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rng = np.random.default_rng(12)
            y = np.clip(0.25 + rng.normal(0, 3 / 255, (96, 128)), 0, 1)
            uv = rng.normal(0, 3 / 255, (48, 64))
            scores = []
            for depth, color_range in ((8, "tv"), (10, "tv"), (8, "pc"), (10, "pc")):
                unit = 2 ** (depth - 8)
                y_offset, y_span = (16 * unit, 219 * unit) if color_range == "tv" else (0, 2**depth - 1)
                c_span = 224 * unit if color_range == "tv" else 2**depth - 1
                dtype = np.uint8 if depth == 8 else "<u2"
                planes = [np.round(y * y_span + y_offset).astype(dtype),
                          np.round(uv * c_span + 128 * unit).astype(dtype),
                          np.full(uv.shape, 128 * unit, dtype=dtype)]
                raw, video = root / "input.yuv", root / f"{depth}-{color_range}.mkv"
                raw.write_bytes(b"".join(p.tobytes() for p in planes))
                pixel_format = "yuv420p" if depth == 8 else "yuv420p10le"
                subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", pixel_format,
                                "-video_size", "128x96", "-framerate", "5", "-i", str(raw),
                                "-frames:v", "1", "-c:v", "ffv1", "-color_range", color_range,
                                "-color_trc", "bt709", str(video)], check=True, capture_output=True, timeout=30)
                row = vqc.analyze(video)
                result = diagnostics.analyze_noise(row, 1, vqc.ffprobe(video))
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["bit_depth"], depth)
                score = result["samples"][0]["scales"][0]["planes"]
                scores.append((score["Y"]["p90"], score["U"]["p90"]))
                sample = result["samples"][0]
                self.assertEqual(sample["pts_s"], 0)
                self.assertEqual(sample["frame_type"], "I")
                _, decoded = diagnostics.sample_frame(video, 0, 0, 128, 96, color_range, 30)
                normalized = diagnostics.normalize_plane(decoded[0], color_range)
                np.testing.assert_allclose(normalized, y, atol=0.5 / y_span + 1 / 1023)
            for plane in (0, 1):
                # Quantization at 8 bits changes robust percentiles even with exact range conversion.
                self.assertLess(max(s[plane] for s in scores) - min(s[plane] for s in scores), 0.4, scores)

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
            with patch.object(vqc, "ensure_resdet", side_effect=AssertionError("network setup")):
                run(["--noise-diagnostics"])
            upgraded = next(iter(vqc.load_cache(cache).values()))
            self.assertEqual({k: v for k, v in upgraded.items() if k != "_noise_diag"}, baseline)
            result = upgraded["_noise_diag"]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["unique_samples"], 3)
            self.assertTrue(any(s["frame_type"] in ("P", "B") for s in result["samples"]))
            for sample in result["samples"]:
                self.assertGreaterEqual(sample["pts_s"], sample["requested_s"])
                self.assertLess(sample["pts_s"] - sample["requested_s"], 0.101)
            self.assertTrue((root / "report_noise.md").is_file())
            self.assertIn("spatial variation", (root / "report_noise.md").read_text())
            self.assertIn("Local spatial review", report.read_text())
            with patch.object(diagnostics, "analyze_noise", side_effect=AssertionError("cached")):
                run(["--noise-diagnostics"])
            with patch.object(diagnostics, "VERSION", diagnostics.VERSION + 1), \
                    patch.object(diagnostics, "analyze_noise", wraps=diagnostics.analyze_noise) as analyzer:
                run(["--noise-diagnostics"])
                analyzer.assert_called_once()
            run([])
            self.assertNotIn("Local spatial review", report.read_text())
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
                with patch("sys.argv", args + ["--noise-diagnostics"]), \
                        patch.object(vqc, "ensure_resdet", side_effect=AssertionError("download")), \
                        patch.object(vqc, "deep_analyze", side_effect=AssertionError("repeat deep")):
                    vqc.main()
                updated = next(iter(vqc.load_cache(cache).values()))
                self.assertEqual({k: v for k, v in updated.items() if k != "_noise_diag"}, baseline)
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
                        for sample in result["samples"]:
                            self.assertAlmostEqual(sample["pts_s"] - offset, sample["actual_s"])
                            self.assertLess(abs(sample["actual_s"] - sample["requested_s"]), 0.101)
                    else:
                        self.assertEqual(result["status"], "insufficient evidence")
                        self.assertIsNone(result["review_score"])
                        self.assertEqual(result["unique_samples"], 1)
                        self.assertEqual(result["coverage"], 0)
                        self.assertEqual(sum("duplicate" in s.get("reason", "") for s in result["samples"]), 2)

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
                self.assertIn("spatial variation", text)


if __name__ == "__main__":
    unittest.main()
