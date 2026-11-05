"""Checks that a lighter copy of a video has the same length as the original.

Makes real copies with FFmpeg, using the same settings as the app. Run it from
the SurgUI folder with:

    python -m unittest discover tests
"""
import os
import re
import subprocess
import tempfile
import unittest

import imageio_ffmpeg

from surgui.vidPlayer import LIGHTER_COPY_ARGS, LIGHTER_COPY_MAX_LENGTH_DIFF_MS

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SOURCE = ["-f", "lavfi", "-i", "testsrc=s=1920x1080:r=60:d=5"]
ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


def run_ffmpeg(*args):
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def length_ms(path):
    """Time of a video's end, which is the length the player shows.

    FFmpeg's Duration leaves out any time before the first frame, but VLC counts it.
    """
    output = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+), start: (\d+\.\d+)", output)
    if not m:
        raise AssertionError("No length found for {}:\n{}".format(path, output))
    return round((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) + float(m.group(4))) * 1000)


class LighterCopyLengthTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def path(self, name):
        return os.path.join(self.folder, name)

    def assert_copy_has_same_length(self, original):
        copy = self.path("copy.mp4")
        run_ffmpeg("-i", original, *LIGHTER_COPY_ARGS, copy)
        self.assertGreater(length_ms(original), 0)
        self.assertLessEqual(abs(length_ms(copy) - length_ms(original)), LIGHTER_COPY_MAX_LENGTH_DIFF_MS)

    def test_constant_frame_rate(self):
        original = self.path("original.mp4")
        run_ffmpeg(*SOURCE, *ENCODE, original)
        self.assert_copy_has_same_length(original)

    def test_variable_frame_rate(self):
        original = self.path("original.mp4")
        # Keeps 40 frames out of every 120, so the picture holds still between them
        run_ffmpeg(*SOURCE, "-vf", "select='lt(mod(n\\,120)\\,40)'", "-vsync", "vfr", *ENCODE, original)
        self.assert_copy_has_same_length(original)

    def test_first_frame_not_at_zero(self):
        video = self.path("video.mp4")
        original = self.path("original.mp4")
        run_ffmpeg(*SOURCE, *ENCODE, video)
        run_ffmpeg("-itsoffset", "2", "-i", video, "-c", "copy", original)
        self.assert_copy_has_same_length(original)


if __name__ == "__main__":
    unittest.main()
