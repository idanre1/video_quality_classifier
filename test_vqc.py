import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import vqc


class DetectorTests(unittest.TestCase):
    def test_flag_prefix_replacement_and_undo(self):
        for initial in ("N_Movie.mp4", "10_1080p-good_N_Movie.mp4",
                        "10_1080p-good_F-S_N_Movie.mp4"):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                video = root / initial
                video.write_bytes(b"video")
                subtitle = video.with_suffix(".en.SRT")
                subtitle.write_text("subtitle")
                cache, undo = root / "cache.jsonl", root / "undo.sh"
                for stats, deep, segment in (
                    ({"noise": 3.5}, True, "F-N_"),
                    ({"noise": 5}, True, "F-V_"),
                    ({"noise": 5, "luma": 20, "crushed": 0.2,
                      "clipped": 0.1, "saturation": 40}, True, "F-VDBHS_"),
                    ({}, True, ""),
                    ({"noise": 5, "luma": 20}, False, ""),
                    ({"noise": 3.5, "crushed": 0.2}, True, "F-NB_"),
                ):
                    row = {"path": str(video), "group": "1080p/good", **stats}
                    with contextlib.redirect_stderr(io.StringIO()):
                        done, same, errors = vqc.rename_files(
                            [row], cache, root, undo, deep=deep
                        )
                        self.assertEqual(errors, [])
                        self.assertEqual(done + same, 1)
                        video = root / f"10_1080p-good_{segment}N_Movie.mp4"
                        subtitle = root / f"10_1080p-good_{segment}N_Movie.en.SRT"
                        self.assertEqual(row["path"], str(video))
                        self.assertTrue(video.is_file())
                        self.assertTrue(subtitle.is_file())
                        self.assertEqual(vqc.PREFIX_RE.sub("", video.name, count=1), "N_Movie.mp4")
                        self.assertEqual(vqc.rename_files([row], cache, root, undo, deep=deep),
                                         (0, 1, []))
                result = subprocess.run(["sh", str(undo)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((root / "N_Movie.mp4").is_file())
                self.assertTrue((root / "N_Movie.en.SRT").is_file())
                self.assertEqual(len(list(root.glob("*.mp4"))), 1)

    def test_flag_prefix_cache_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache.jsonl"
            names = ("Movie.mp4", "10_1080p-good_Movie.mp4",
                     "10_1080p-good_F-N_Movie.mp4", "10_1080p-good_F-VDBHS_Movie.mp4")
            rows = [{"_key": f"{root / name}|1|1", "path": str(root / name)} for name in names]
            unrelated = {"_key": f"{root / 'N_Movie.mp4'}|1|1", "path": str(root / "N_Movie.mp4")}
            cache.write_text("".join(json.dumps(row) + "\n" for row in rows + [unrelated]))
            retained = vqc.purge_cache(cache, vqc.load_cache(cache), [root / names[-1]])
            self.assertEqual(retained, {unrelated["_key"]: unrelated})
            self.assertEqual(vqc.load_cache(cache), retained)

    def test_report_pagination(self):
        for total, deep in ((0, True), (199, True), (200, True), (201, True),
                            (400, True), (401, True), (401, False)):
            with self.subTest(total=total, deep=deep), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                report = root / "quality report.md"
                groups = {}
                expected_files = []
                for i in range(total):
                    group = ("1080p/good", "720p/fair", "low/poor")[min(2, i * 3 // total)]
                    filename = f"video{i:04d}.mp4"
                    expected_files.append(filename)
                    groups.setdefault(group, []).append({
                        "path": str(root / filename), "width": 1920, "height": 1080,
                        "codec": "h264", "video_kbps": 10000, "norm_bpp": 0.1,
                        "size_mb": 1024, "duration_min": 1,
                        "snapshot": f"{i}.jpg", "snapshot_t": 0,
                    })
                vqc.write_report(report, root, groups, [(root / "bad.mp4", "test error")],
                                 [("mode", "deep" if deep else "metadata")],
                                 "vqc_snapshots" if deep else None)
                overview = report.read_text()
                self.assertIn(f"· {total} files · {total:.1f} GB", overview)
                self.assertIn("test error", overview)
                for group, rows in groups.items():
                    self.assertIn(f"| {group} | {len(rows)} | {len(rows):.1f} |", overview)
                if total <= 200:
                    pages = [report]
                    self.assertNotIn("## File pages", overview)
                    self.assertEqual(list(root.glob("*_part*.md")), [])
                else:
                    pages = sorted(root.glob("*_part*.md"))
                    self.assertEqual(len(pages), (total + 199) // 200)
                    self.assertEqual(overview.count("![frame"), 0)
                    self.assertNotRegex(overview, r"(?m)^\| video\d")
                    for i, page in enumerate(pages):
                        self.assertIn(
                            f"[Part {i + 1}]({vqc.urllib.parse.quote(page.name)})", overview
                        )
                found_files = []
                for i, page in enumerate(pages):
                    text = page.read_text()
                    files = [line.split("|")[1].strip() for line in text.splitlines()
                             if line.startswith("| video")]
                    self.assertLessEqual(len(files), 200)
                    self.assertEqual(text.count("![frame"), len(files) if deep else 0)
                    found_files.extend(files)
                    if total > 200:
                        self.assertNotIn("test error", text)
                        self.assertIn("[Overview](quality%20report.md)", text)
                        if i:
                            self.assertIn(
                                f"[Previous]({vqc.urllib.parse.quote(pages[i - 1].name)})", text
                            )
                        if i + 1 < len(pages):
                            self.assertIn(
                                f"[Next]({vqc.urllib.parse.quote(pages[i + 1].name)})", text
                            )
                self.assertEqual(found_files, expected_files)

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
                        patch.object(vqc, "ffprobe", return_value={}), \
                        patch.object(vqc.noise_diagnostics, "analyze_noise", return_value={
                            "signature": vqc.noise_diagnostics.cache_signature(8),
                            "status": "unsupported", "reason": "test unsupported source"}), \
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
                        self.assertTrue((library / "22_low-good_F-V_Movie.mp4").is_file())
                        self.assertTrue((library / "22_low-good_F-V_Movie.srt").is_file())
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

    def test_temporal_noise_thresholds_and_nonstacking(self):
        def row(score, coverage=0.3, status="ok", noise=0, version=None):
            return {"noise": noise, "_noise_diag": {
                "status": status, "method": "aligned-temporal",
                "signature": {"version": vqc.noise_diagnostics.VERSION if version is None else version},
                "review_score": score, "coverage": coverage}}

        for score, coverage, level in (
                (1.725, 0.4, 0), (1.726, 0.2, 0), (1.726, 0.2001, 1),
                (3, 0.4, 1), (3.001, 0.4, 2), (9, 0.1999, 0),
                (9, 1, 2), (9, 1.01, 0), (None, 0.3, 0),
                (float("nan"), 0.3, 0), (float("inf"), 0.3, 0)):
            with self.subTest(score=score, coverage=coverage):
                r = row(score, coverage)
                self.assertEqual(vqc.temporal_noise_level(r), level)
                self.assertEqual(vqc.noise_penalty(r), level)
                self.assertEqual(vqc.picture_flags(r), {0: [], 1: ["noisy"], 2: ["very noisy"]}[level])
        for status in ("unsupported", "error", "insufficient evidence"):
            self.assertEqual(vqc.noise_penalty(row(9, status=status)), 0)
        self.assertEqual(vqc.noise_penalty(row(9, version=1)), 0)
        self.assertEqual(vqc.noise_penalty(row(2, noise=9)), 1)
        self.assertEqual(vqc.noise_penalty(row(4, noise=9)), 2)
        self.assertEqual(vqc.picture_flags(row(2, noise=9)), ["very noisy"])

    def test_temporal_deep_upgrade_rename_force_and_metadata(self):
        for raw_grade in ("excellent", "good", "fair", "poor"):
            for score, drop, letter in ((1.726, 1, "N"), (3.001, 2, "V")):
                with self.subTest(grade=raw_grade, score=score), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    video = root / "Movie.mp4"
                    video.write_bytes(b"fixture")
                    video.with_suffix(".srt").write_text("subtitle")
                    cache, report = root / "cache.jsonl", root / "report.md"
                    snap_dir = root / "snapshots"
                    snap_dir.mkdir()
                    st = video.stat()
                    key = f"{video}|{st.st_size}|{int(st.st_mtime)}"
                    snap = vqc.snap_name(key)
                    (snap_dir / snap).write_bytes(b"snapshot")
                    raw = {"path": str(video), "tier": "1080p", "grade": raw_grade,
                           "group": f"1080p/{raw_grade}", "width": 1920, "height": 1080,
                           "rotation": 0, "norm_bpp": 0.2, "codec": "h264", "video_kbps": 10000,
                           "duration_min": 1, "size_mb": 10}
                    cache.write_text(json.dumps({**raw, "_key": key, "noise": 0, "snapshot": snap}) + "\n")
                    diag = {"signature": vqc.noise_diagnostics.cache_signature(8),
                            "method": "aligned-temporal", "status": "ok",
                            "review_score": score, "coverage": 0.3}
                    grades = [name for name, _ in vqc.GRADES]
                    expected = grades[min(grades.index(raw_grade) + drop, len(grades) - 1)]
                    base_args = ["vqc", str(root), "--cache", str(cache), "-o", str(report), "--rename"]

                    def probe(path):
                        return {**raw, "path": str(path)}

                    def deep_result(row, frames, resdet, snapshot):
                        snapshot.write_bytes(b"snapshot")
                        return {"noise": 0, "snapshot": snapshot.name}

                    with patch.object(vqc, "in_uv_venv", return_value=True), \
                            patch.object(vqc, "ensure_resdet", return_value=None), \
                            patch.object(vqc, "ffprobe", return_value={}), \
                            patch.object(vqc, "analyze", side_effect=probe), \
                            patch.object(vqc, "deep_analyze", side_effect=deep_result) as deep, \
                            patch.object(vqc.noise_diagnostics, "analyze_noise", return_value=diag) as temporal, \
                            contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        for extra in (["--deep"], ["--deep"], ["--deep", "--force"]):
                            with patch("sys.argv", base_args + extra):
                                vqc.main()
                            name = f"{vqc.quality_code('1080p', expected)}_1080p-{expected}_F-{letter}_Movie"
                            self.assertTrue((root / f"{name}.mp4").is_file())
                            self.assertTrue((root / f"{name}.srt").is_file())
                            self.assertEqual(len(list(root.glob("*.mp4"))), 1)
                            self.assertIn(f"## 1080p/{expected}", report.read_text())
                            self.assertIn(f"| {letter} |", (root / "report_noise.md").read_text())
                        self.assertEqual(deep.call_count, 1)  # Only force repeats an already valid deep analysis.
                        self.assertEqual(temporal.call_count, 2)
                        with patch("sys.argv", base_args):
                            vqc.main()
                        name = f"{vqc.quality_code('1080p', raw_grade)}_1080p-{raw_grade}_Movie"
                        self.assertTrue((root / f"{name}.mp4").is_file())
                        self.assertTrue((root / f"{name}.srt").is_file())
                        self.assertNotIn("Local temporal review", report.read_text())

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
                        patch.object(vqc, "ffprobe", return_value={}), \
                        patch.object(vqc.noise_diagnostics, "analyze_noise", return_value={
                            "signature": vqc.noise_diagnostics.cache_signature(8),
                            "status": "unsupported", "reason": "test unsupported source"}), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    for _ in range(2):
                        with patch("sys.argv", args + ["--deep"]):
                            vqc.main()
                        name = f"{vqc.quality_code('low', deep_grade)}_low-{deep_grade}_F-VDBHS_Movie"
                        self.assertTrue((root / f"{name}.mp4").is_file())
                        self.assertTrue((root / f"{name}.srt").is_file())
                        text = report.read_text()
                        self.assertIn(f"## low/{deep_grade}", text)
                        self.assertIn("very noisy, dark, crushed blacks, clipped highlights, saturated", text)
                        self.assertIn("| effective | detail | noise | temporal residual | temporal coverage | "
                                      "noise grade drop | flags | snapshot |", text)
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
