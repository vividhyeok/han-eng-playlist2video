import os
import tempfile
import unittest

from app.lyrics.exception_policy import classify_lyrics, extract_protected_english
from app.lyrics.translator_v2 import _repeat_groups, parse_lrc_and_translate
from app.media.video_maker import _group_simultaneous_lyrics, _wrap_subtitle, _write_ass


class TranslationPolicyTests(unittest.TestCase):
    def test_mixed_korean_english_requires_translation(self):
        policy = classify_lyrics(["난 Seoul에서 flex with my crew"])
        self.assertEqual(policy.language_mode, "mixed")
        self.assertTrue(policy.translation_required)
        self.assertIn("Seoul", extract_protected_english("난 Seoul에서 flex"))

    def test_repeated_hooks_are_grouped(self):
        self.assertEqual(_repeat_groups(["가자", "verse", "가자"]), [[0, 2]])

    def test_plain_lyrics_cannot_bypass_auto_sync(self):
        import asyncio

        handle, path = tempfile.mkstemp(suffix=".lrc")
        os.close(handle)
        output = path + ".json"
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write("첫 번째 줄\n두 번째 줄\n")
            with self.assertRaisesRegex(ValueError, "AI 자동 싱크"):
                asyncio.run(parse_lrc_and_translate(path, output, duration=120))
        finally:
            os.remove(path)


class SubtitleLayoutTests(unittest.TestCase):
    def test_long_subtitle_wraps_to_two_lines(self):
        wrapped = _wrap_subtitle("긴 가사가 화면 밖으로 나가지 않도록 안전하게 두 줄로 나뉘어야 합니다", korean=True)
        self.assertLessEqual(len(wrapped.splitlines()), 2)

    def test_equal_timestamps_are_merged(self):
        grouped = _group_simultaneous_lyrics([
            {"start_time": 1, "original": "첫 줄", "english": "First"},
            {"start_time": 1, "original": "둘째 줄", "english": "Second"},
        ])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["original"], "첫 줄 둘째 줄")

    def test_english_only_line_is_not_duplicated(self):
        handle, path = tempfile.mkstemp(suffix=".ass")
        os.close(handle)
        try:
            _write_ass([{"start_time": 0, "original": "Just do it", "english": "Just do it"}], 3, path)
            with open(path, encoding="utf-8-sig") as file:
                events = [line for line in file if line.startswith("Dialogue:")]
            self.assertEqual(len(events), 1)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
