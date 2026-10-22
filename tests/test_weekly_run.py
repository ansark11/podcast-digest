import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scheduler"))
import weekly_run  # noqa: E402


def pmset_output(text):
    return mock.patch.object(weekly_run.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, stdout=text))


class LastScheduledTimeTest(unittest.TestCase):
    def test_wednesday_7pm(self):
        wed_7pm = datetime(2026, 10, 21, 19, 0)
        self.assertEqual(weekly_run.last_scheduled_time(datetime(2026, 10, 21, 19, 0)), wed_7pm)
        self.assertEqual(weekly_run.last_scheduled_time(datetime(2026, 10, 27, 23, 59)), wed_7pm)
        # Wednesday before 7 PM still belongs to the previous week.
        self.assertEqual(weekly_run.last_scheduled_time(datetime(2026, 10, 21, 18, 59)), datetime(2026, 10, 14, 19, 0))


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
