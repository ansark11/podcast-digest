import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scheduler"))
import weekly_run  # noqa: E402


def pmset_output(text):
    return mock.patch.object(weekly_run.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, stdout=text))


class AwakeTest(unittest.TestCase):
    def test_full_wake(self):
        with pmset_output("Current System Capabilities are: CPU Graphics Audio Network \nCurrent Power State: 4\n"):
            self.assertTrue(weekly_run.awake())

    def test_dark_wake(self):
        with pmset_output("Current System Capabilities are: CPU Network \nCurrent Power State: 4\n"):
            self.assertFalse(weekly_run.awake())

    def test_unreadable_state_does_not_block(self):
        with mock.patch.object(weekly_run.subprocess, "run", side_effect=OSError):
            self.assertTrue(weekly_run.awake())
        with pmset_output(""):
            self.assertTrue(weekly_run.awake())


class SecondsAsleepTest(unittest.TestCase):
    def test_counts_wall_time_the_uptime_clock_missed(self):
        uptime = time.clock_gettime(time.CLOCK_UPTIME_RAW)
        # As if the run started 600s ago by the wall clock but only 30s of it was awake.
        asleep = weekly_run.seconds_asleep(time.time() - 600, uptime - 30)
        self.assertAlmostEqual(asleep, 570, delta=1)

    def test_no_sleep(self):
        asleep = weekly_run.seconds_asleep(time.time(), time.clock_gettime(time.CLOCK_UPTIME_RAW))
        self.assertLess(abs(asleep), 1)


if __name__ == "__main__":
    unittest.main()
