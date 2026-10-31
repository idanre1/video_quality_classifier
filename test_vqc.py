import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import vqc


class DetectorTests(unittest.TestCase):
    def test_force_refresh(self):
        for mode, failure in ((False, None), (True, None), (False, "probe"),
                              (True, "probe"), (True, "deep")):
            with self.subTest(deep=mode, failure=failure), tempfile.TemporaryDirectory() as tmp:
                library = Path(tmp, "library")
                library.mkdir()
                video = library / "09_1080p-excellent_Movie.mp4"
                video.write_bytes(b"fixture")
                subtitle = video.with_suffix(".srt")
                subtitle.write_text("subtitle")
                cache, report = Path(tmp, "cache.jsonl"), Path(tmp, "report.md")
                st = video.stat()
                key = f"{video}|{st.st_size}|{int(st.st_mtime)}"
                metadata = {
                    "path": str(video), "group": "1080p/excellent",
                    "tier": "1080p", "grade": "excellent",
                    "width": 1920, "height": 1080, "rotation": 0,
                    "norm_bpp": 0.2, "codec": "h264", "video_kbps": 10000,
                    "duration_min": 1, "size_mb": 10,
                }
                stale = [
                    {**metadata, "_key": key, "noise": 99, "eff_height": 1080},
                    {**metadata, "_key": f"{video}|1|1"},
                    {**metadata, "path": str(library / "Movie.mp4"),
                     "_key": f"{library / 'Movie.mp4'}|1|1"},
                    {**metadata, "path": str(library / "10_1080p-good_Movie.mp4"),
                     "_key": f"{library / '10_1080p-good_Movie.mp4'}|1|1"},
                ]
                unrelated = {"_key": f"{Path(tmp, 'other', 'Movie.mp4')}|1|1", "marker": "keep"}
                filtered = {"_key": f"{library / 'Movie.mkv'}|1|1", "marker": "keep"}
                cache.write_text("".join(json.dumps(r) + "\n" for r in stale + [unrelated, filtered]))

                def probe(path):
                    self.assertEqual(path, video)
                    self.assertEqual(vqc.load_cache(cache), {
                        unrelated["_key"]: unrelated, filtered["_key"]: filtered
                    })
                    if failure == "probe":
                        raise RuntimeError("forced probe failed")
                    return metadata.copy()

                def deep_result(row, frames, resdet, snap):
                    if failure == "deep":
                        raise RuntimeError("forced deep failed")
                    snap.write_bytes(b"fresh snapshot")
                    return {
                        "noise": 5, "ssim_eff_height": 360, "resdet_height": 810,
                        "eff_height": 360, "eff_tier": "low",
                        "snapshot": snap.name, "snapshot_t": 0,
                    }

                args = ["vqc", str(library), "--cache", str(cache), "-o", str(report),
                        "--force", "--ext", "mp4"]
                if mode:
                    args.append("--deep")
                if failure is None:
                    args.append("--rename")
                with patch("sys.argv", args), \
                        patch.object(vqc, "in_uv_venv", return_value=True), \
                        patch.object(vqc, "ensure_resdet", return_value=None), \
                        patch.object(vqc, "analyze", side_effect=probe) as analyze, \
                        patch.object(vqc, "deep_analyze", side_effect=deep_result) as deep, \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    vqc.main()
                analyze.assert_called_once()
                self.assertEqual(deep.call_count, int(mode and failure != "probe"))
                cached = vqc.load_cache(cache)
                self.assertEqual(cached[unrelated["_key"]], unrelated)
                self.assertEqual(cached[filtered["_key"]], filtered)
                self.assertNotIn(f"{video}|1|1", cached)
                self.assertNotIn(f"{library / 'Movie.mp4'}|1|1", cached)
                self.assertNotIn(f"{library / '10_1080p-good_Movie.mp4'}|1|1", cached)
                text = report.read_text()
                self.assertIn("0 from cache, 1 analyzed now", text)
                self.assertIn("| force | yes", text)
                if failure == "probe":
                    self.assertNotIn(key, cached)
                    self.assertIn("forced probe failed", text)
                else:
                    self.assertEqual(cached[key].get("noise"), 5 if mode and not failure else None)
                    if failure == "deep":
                        self.assertNotIn("eff_height", cached[key])
                        self.assertIn("forced deep failed", text)
                    elif mode:
                        self.assertTrue((library / "22_low-good_Movie.mp4").is_file())
                        self.assertTrue((library / "22_low-good_Movie.srt").is_file())
                        self.assertIn("## low/good", text)
                    else:
                        self.assertTrue(video.is_file())
                        self.assertTrue(subtitle.is_file())

    def test_cache_purge_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp, "cache.jsonl")
            video = Path(tmp, "Movie.mp4")
            row = {"_key": f"{video}|1|1", "path": str(video)}
            cache.write_text(json.dumps(row) + "\n")
            original = cache.read_bytes()
            with patch.object(vqc.os, "replace", side_effect=OSError("cache replace failed")):
                with self.assertRaisesRegex(OSError, "cache replace failed"):
                    vqc.purge_cache(cache, vqc.load_cache(cache), [video])
            self.assertEqual(cache.read_bytes(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [cache])

    def test_resolution_combination(self):
        for ssim, resdet, expected in (
            (360, 810, 360),
            (360, 675, 360),
            (810, 720, 720),
            (540, 540, 540),
            (432, 720, 720),
            (431, 720, 431),
            (360, None, 360),
        ):
            with self.subTest(ssim=ssim, resdet=resdet):
                self.assertEqual(vqc.combine_height(ssim, resdet), expected)

    def test_noise_measurement(self):
        clean = np.full((512, 512), 128, dtype=np.uint8)
        noisy = np.clip(
            clean + np.random.default_rng(42).normal(0, 10, clean.shape), 0, 255
        ).astype(np.uint8)
        self.assertEqual(vqc._noise_sigma(clean), 0)
        self.assertGreater(vqc._noise_sigma(noisy), vqc.NOISE_HEAVY)
        self.assertAlmostEqual(vqc._noise_sigma(noisy), 10, delta=2)
        self.assertIsNone(vqc._noise_sigma(np.zeros_like(clean)))
        self.assertIsNone(vqc._noise_sigma(clean[:16, :16]))

    def test_picture_stats_and_flags(self):
        y = np.full((512, 512), 16, dtype=np.uint8)
        u = np.full((256, 256), 168, dtype=np.uint8)
        v = np.full((256, 256), 128, dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "frames.y4m")
            path.write_bytes(
                b"YUV4MPEG2 W512 H512 F25:1 Ip A1:1 C420jpeg\nFRAME\n"
                + y.tobytes() + u.tobytes() + v.tobytes()
                + b"FRAME\n" + np.full_like(y, 235).tobytes() + u.tobytes() + v.tobytes()
            )
            stats = vqc._picture_stats(path)
        self.assertEqual(stats["luma"], 125.5)
        self.assertEqual(stats["crushed"], 0.5)
        self.assertEqual(stats["clipped"], 0.5)
        self.assertEqual(stats["saturation"], 40)
        self.assertEqual(
            vqc.picture_flags(stats), ["crushed blacks", "clipped highlights", "saturated"]
        )
        self.assertEqual(vqc.picture_flags({}), [])
        for noise, flags in (("", []), (3.49, []), (3.5, ["noisy"]),
                             (4.99, ["noisy"]), (5, ["very noisy"])):
            with self.subTest(noise=noise):
                self.assertEqual(vqc.picture_flags({"noise": noise}), flags)
        self.assertEqual(vqc.picture_flags({"luma": 31.9}), ["dark"])
        self.assertEqual(vqc.picture_flags({"luma": 32}), [])

    def test_deep_cache_report_and_rename(self):
        for original_grade, deep_grade in (
            ("excellent", "good"), ("good", "fair"), ("fair", "poor"), ("poor", "poor")
        ):
            with self.subTest(grade=original_grade), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                video = root / "Movie.mp4"
                video.write_bytes(b"fixture")
                (root / "Movie.srt").write_text("subtitle")
                report, cache = root / "report.md", root / "cache.jsonl"
                row = {
                    "path": str(video), "group": f"1080p/{original_grade}",
                    "tier": "1080p", "grade": original_grade,
                    "width": 1920, "height": 1080, "rotation": 0,
                    "norm_bpp": 0.2, "codec": "h264", "video_kbps": 10000,
                    "duration_min": 1, "size_mb": 10,
                }

                def deep_result(row, frames, resdet, snap):
                    snap.write_bytes(b"snapshot")
                    return {
                        "ssim_eff_height": 360, "resdet_height": 810,
                        "eff_height": 360, "eff_tier": "low",
                        "noise": 5, "luma": 20, "crushed": 0.2,
                        "clipped": 0.1, "saturation": 40,
                        "snapshot": snap.name, "snapshot_t": 0, "detail": 0.01,
                    }

                args = ["vqc", str(root), "--cache", str(cache), "-o", str(report), "--rename"]
                with patch.object(vqc, "in_uv_venv", return_value=True), \
                        patch.object(vqc, "ensure_resdet", return_value=None), \
                        patch.object(vqc, "analyze", return_value=row) as probe, \
                        patch.object(vqc, "deep_analyze", side_effect=deep_result) as deep, \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    for _ in range(2):
                        with patch("sys.argv", args + ["--deep"]):
                            vqc.main()
                        name = f"{vqc.quality_code('low', deep_grade)}_low-{deep_grade}_Movie"
                        self.assertTrue((root / f"{name}.mp4").is_file())
                        self.assertTrue((root / f"{name}.srt").is_file())
                        text = report.read_text()
                        self.assertIn(f"## low/{deep_grade}", text)
                        self.assertIn("very noisy, dark, crushed blacks, clipped highlights, saturated", text)
                        self.assertIn("| effective | detail | noise | flags | snapshot |", text)
                        self.assertIn("640x360", text)
                    self.assertEqual(probe.call_count, 1)
                    self.assertEqual(deep.call_count, 1)
                    with patch("sys.argv", args):
                        vqc.main()
                    name = f"{vqc.quality_code('1080p', original_grade)}_1080p-{original_grade}_Movie"
                    self.assertTrue((root / f"{name}.mp4").is_file())
                    self.assertTrue((root / f"{name}.srt").is_file())
                    self.assertIn(f"## 1080p/{original_grade}", report.read_text())
                    self.assertNotIn("| noise |", report.read_text())
                    self.assertEqual(probe.call_count, 1)
                    self.assertEqual(deep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
