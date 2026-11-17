import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stability_support import (
    ProcessMemoryMonitor,
    StageStatistics,
    is_opencv_allocation_error,
    screenshot_as_grayscale,
    start_keyboard_listener,
    stop_keyboard_listener,
)


class StabilitySupportTests(unittest.TestCase):
    def test_hotkey_callbacks_register_f8_and_f9_and_listener_stops_and_joins(self):
        calls = []

        class FakeListener:
            def __init__(self, on_press):
                self.on_press = on_press
                self._ready = False
                self.alive = False
                self.stop_called = False
                self.join_timeout = None

            def start(self):
                self.alive = True
                self._ready = True

            def stop(self):
                self.stop_called = True
                self.alive = False

            def join(self, timeout=None):
                self.join_timeout = timeout

            def is_alive(self):
                return self.alive

        class FakeKeyboard:
            class Key:
                f8 = object()
                f9 = object()
                other = object()

            Listener = FakeListener

        listener = start_keyboard_listener(
            FakeKeyboard,
            lambda: calls.append("pause"),
            lambda: calls.append("stop"),
        )
        listener.on_press(FakeKeyboard.Key.f8)
        listener.on_press(FakeKeyboard.Key.f9)
        listener.on_press(FakeKeyboard.Key.other)
        stop_keyboard_listener(listener)

        self.assertEqual(calls, ["pause", "stop"])
        self.assertTrue(listener.stop_called)
        self.assertEqual(listener.join_timeout, 5.0)
        self.assertFalse(listener.is_alive())

    def test_hotkey_listener_reports_callback_errors_when_joined(self):
        class FakeListener:
            def __init__(self):
                self.stop_called = False
                self.join_called = False

            def stop(self):
                self.stop_called = True

            def join(self, timeout=None):
                self.join_called = True
                raise ValueError("callback failed")

            def is_alive(self):
                return False

        listener = FakeListener()
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            stop_keyboard_listener(listener)
        self.assertTrue(listener.stop_called)
        self.assertTrue(listener.join_called)

    def test_memory_monitor_tracks_current_average_and_peak_in_constant_state(self):
        values = iter((100, 300, 200))
        monitor = ProcessMemoryMonitor(sampler=lambda: next(values))

        monitor.sample_now()
        monitor.sample_now()
        monitor.sample_now()
        snapshot = monitor.snapshot()

        self.assertEqual(snapshot["current"], 200)
        self.assertEqual(snapshot["average"], 200)
        self.assertEqual(snapshot["peak"], 300)
        self.assertEqual(snapshot["sample_count"], 3)

    @unittest.skipUnless(os.name == "nt", "Windows working-set API only")
    def test_windows_working_set_sampler_returns_process_rss(self):
        self.assertGreater(ProcessMemoryMonitor._windows_working_set_bytes(), 0)

    def test_memory_monitor_stops_and_joins_its_worker(self):
        monitor = ProcessMemoryMonitor(
            interval_seconds=0.01, sampler=lambda: 42
        )
        monitor.start()
        time.sleep(0.04)
        worker = monitor._thread
        monitor.stop()

        self.assertIsNotNone(worker)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(monitor._thread)
        self.assertGreaterEqual(monitor.snapshot()["sample_count"], 2)

    def test_stage_statistics_keep_exact_aggregates_and_only_latest_100_wipes(self):
        stats = StageStatistics()
        for stage in range(1, 121):
            stats.record(stage, wiped=True)
        stats.record(20, wiped=False)

        self.assertEqual(stats.all_count, 121)
        self.assertEqual(stats.all_total, sum(range(1, 121)) + 20)
        self.assertEqual(stats.wipe_count, 120)
        self.assertEqual(stats.wipe_total, sum(range(1, 121)))
        self.assertEqual(len(stats.recent_wipes), 100)
        self.assertEqual(stats.recent_wipes[0], 21)
        self.assertEqual(stats.recent_wipes[-1], 120)

    def test_screenshot_returns_owned_array_and_closes_both_images(self):
        class FakeImage:
            def __init__(self):
                self.closed = False
                self.converted = None

            def convert(self, mode):
                self.assert_mode = mode
                self.converted = FakeConverted()
                return self.converted

            def close(self):
                self.closed = True

        class FakeConverted:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeNumpy:
            def __init__(self):
                self.copy_argument = None

            def array(self, image, copy):
                self.copy_argument = copy
                return FakeArray()

        class FakeArray:
            ndim = 2
            size = 4

        image = FakeImage()

        class FakeDevice:
            @staticmethod
            def screenshot():
                return image

        numpy_module = FakeNumpy()
        result = screenshot_as_grayscale(FakeDevice(), numpy_module)

        self.assertEqual(result.ndim, 2)
        self.assertTrue(numpy_module.copy_argument)
        self.assertTrue(image.closed)
        self.assertTrue(image.converted.closed)

    def test_screenshot_closes_images_when_conversion_fails(self):
        class FakeImage:
            closed = False

            def convert(self, mode):
                raise MemoryError("allocation failed")

            def close(self):
                self.closed = True

        image = FakeImage()

        class FakeDevice:
            @staticmethod
            def screenshot():
                return image

        with self.assertRaises(MemoryError):
            screenshot_as_grayscale(FakeDevice(), object())
        self.assertTrue(image.closed)

    def test_opencv_allocation_errors_are_distinguished(self):
        class OpenCVError(Exception):
            code = -4

        self.assertTrue(is_opencv_allocation_error(OpenCVError("failure")))
        self.assertTrue(is_opencv_allocation_error(Exception("Out of memory")))
        self.assertFalse(is_opencv_allocation_error(Exception("bad argument")))


if __name__ == "__main__":
    unittest.main()
