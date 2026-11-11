"""確信度の較正（v0.42.6 / issue #5 の続き）.

固定する契約:
  * 確信度の帯（90-100 / 70-89 / 50-69 / 0-49 / unknown）と、「討論で立場を変えた」割合を
    `confidence_calibration.json` に貯める。
  * 真の正誤ラベルではないので、返すのは**割合**だけ（note に代理ラベルであることを書く）。
  * `consult(debate_depth=deep)` の参加者について、初回の確信度と `changed` を 1 件ずつ記録する。
  * 高確信度の帯で覆る割合が 3 割以上（n>=5）なら content 警告に使える文字列を返す。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402


class Buckets(unittest.TestCase):
    def test_bucket_edges(self):
        self.assertEqual(S.confidence_bucket(100), "90-100")
        self.assertEqual(S.confidence_bucket(90), "90-100")
        self.assertEqual(S.confidence_bucket(89), "70-89")
        self.assertEqual(S.confidence_bucket(70), "70-89")
        self.assertEqual(S.confidence_bucket(50), "50-69")
        self.assertEqual(S.confidence_bucket(10), "0-49")
        self.assertEqual(S.confidence_bucket(None), "unknown")


class _State(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-calib-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.addCleanup(self._restore)

    def _restore(self):
        if self._orig is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig

    def read(self):
        with open(S.calib_path(), encoding="utf-8") as fh:
            return json.load(fh)


class Ledger(_State):
    def test_notes_accumulate_per_bucket(self):
        S.note_confidence("m/a", 95, flipped=False)
        S.note_confidence("m/a", 97, flipped=True)
        S.note_confidence("m/a", 60, flipped=False)
        data = self.read()
        self.assertEqual(data["models"]["m/a"]["buckets"]["90-100"], {"n": 2, "flipped": 1})
        self.assertEqual(data["models"]["m/a"]["buckets"]["50-69"], {"n": 1, "flipped": 0})

    def test_bad_input_is_ignored(self):
        S.note_confidence("", 90, flipped=True)
        S.note_confidence("m/b", None, flipped=True)
        self.assertFalse(os.path.exists(S.calib_path()))

    def test_aggregate_rate(self):
        for conf, flipped in [(95, True), (95, True), (95, False), (75, False)]:
            S.note_confidence("m/a", conf, flipped=flipped)
        got = S.confidence_calibration(["m/a"])["buckets"]
        self.assertEqual(got["90-100"]["n"], 3)
        self.assertAlmostEqual(got["90-100"]["flip_rate"], 0.667, places=3)
        self.assertEqual(got["70-89"]["flip_rate"], 0.0)

    def test_refs_filter(self):
        S.note_confidence("m/a", 95, flipped=True)
        S.note_confidence("m/b", 95, flipped=False)
        only_a = S.confidence_calibration(["m/a"])["buckets"]
        self.assertEqual(only_a["90-100"]["n"], 1)
        self.assertEqual(only_a["90-100"]["flipped"], 1)

    def test_note_says_it_is_a_proxy(self):
        self.assertIn("代理", S.confidence_calibration()["note"])


class Warning(unittest.TestCase):
    def test_warns_when_high_confidence_flips_often(self):
        calib = {"buckets": {"90-100": {"n": 6, "flipped": 2, "flip_rate": 0.333}}}
        self.assertIn("90-100", S._calibration_warning(calib))

    def test_quiet_when_sample_is_small_or_rate_is_low(self):
        self.assertEqual(S._calibration_warning({"buckets": {"90-100": {"n": 3, "flipped": 3,
                                                                      "flip_rate": 1.0}}}), "")
        self.assertEqual(S._calibration_warning({"buckets": {"90-100": {"n": 10, "flipped": 1,
                                                                      "flip_rate": 0.1}}}), "")
        self.assertEqual(S._calibration_warning({}), "")


if __name__ == "__main__":
    unittest.main()
