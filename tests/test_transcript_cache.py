import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import podcast_digest as pd  # noqa: E402

EPISODE = {"guid": "ep-1", "title": "An Episode", "audio_url": "https://example.com/a.mp3", "published": ""}


class TranscriptCacheTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(pd, "TRANSCRIPT_CACHE_DIR", tmp / "cache"),
            mock.patch.object(pd, "fetch_feed_transcript", return_value=None),
            mock.patch.object(pd, "download_audio"),
            mock.patch.object(pd, "build_summary_prompt", return_value="prompt"),
            mock.patch.object(pd, "save_episode_outputs"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_failed_summary_keeps_transcript_and_retry_reuses_it(self):
        with mock.patch.object(pd, "transcribe_audio", return_value="the transcript") as transcribe, \
                mock.patch.object(pd, "summarize_transcript", side_effect=ConnectionError):
            with self.assertRaises(ConnectionError):
                pd.process_episode("Show", EPISODE)
        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(pd.cached_transcript_path("ep-1").read_text(), "the transcript")

        with mock.patch.object(pd, "transcribe_audio") as transcribe, \
                mock.patch.object(pd, "summarize_transcript", return_value={"overview": "x"}):
            pd.process_episode("Show", EPISODE)
        transcribe.assert_not_called()
        pd.save_episode_outputs.assert_called_once_with("Show", EPISODE, "the transcript", {"overview": "x"},
                                                        pd.TRANSCRIPTION_MODEL)
        self.assertFalse(pd.cached_transcript_path("ep-1").exists())


if __name__ == "__main__":
    unittest.main()
