import unittest
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recovery_support import (
    MidnightRecoveryController,
    recovery_shutdown_ready,
    resume_mode_after_recovery,
    screen_pair_matches,
)


BOT_ROOT = Path(__file__).resolve().parents[1]
RECOVERY_ROOT = BOT_ROOT / "templates" / "midnight_recovery"
REFERENCE_ROOT = RECOVERY_ROOT / "references"


class MidnightRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.controller = MidnightRecoveryController()
        self.taps = []

    def tap(self, action):
        self.taps.append(action)
        return True

    @staticmethod
    def matches(**values):
        result = {
            "data_update": False,
            "warning": False,
            "title_screen": False,
            "login_bonus": False,
            "news": False,
            "main_menu": False,
        }
        result.update(values)
        return result

    @staticmethod
    def best_score(reference_path, template_path):
        with Image.open(reference_path) as image:
            reference = np.array(image.convert("L"), copy=True)
        with Image.open(template_path) as image:
            template = np.array(image.convert("L"), copy=True)
        score_map = cv2.matchTemplate(
            reference, template, cv2.TM_CCOEFF_NORMED
        )
        return cv2.minMaxLoc(score_map)[1]

    @staticmethod
    def roi_score(reference_path, template_path, roi):
        x1, y1, x2, y2 = roi
        with Image.open(reference_path) as image:
            reference = np.array(image.convert("L"), copy=True)
        with Image.open(template_path) as image:
            template = np.array(image.convert("L"), copy=True)
        result = cv2.matchTemplate(
            reference[y1:y2, x1:x2], template, cv2.TM_CCOEFF_NORMED
        )
        return cv2.minMaxLoc(result)[1]

    def test_context_templates_match_only_their_reference_screens(self):
        refs = {
            "data_update": "01_data_update.png",
            "warning": "02_warning.png",
            "title_screen": "03_title_screen.png",
            "login_bonus": "04_login_bonus.png",
            "news": "05_news.png",
        }
        contexts = {
            "data_update": "data_update_context.png",
            "login_bonus": "login_bonus_context.png",
            "news": "news_context.png",
        }
        for screen, template_name in contexts.items():
            template = RECOVERY_ROOT / template_name
            for other_screen, reference_name in refs.items():
                score = self.best_score(
                    REFERENCE_ROOT / reference_name, template
                )
                with self.subTest(template=screen, reference=other_screen):
                    if screen == other_screen:
                        self.assertGreaterEqual(score, 0.90)
                    else:
                        self.assertLess(score, 0.90)

    def test_existing_templates_match_their_full_screen_references(self):
        template_to_reference = {
            "data_update.png": "01_data_update.png",
            "warning.png": "02_warning.png",
            "title_screen.png": "03_title_screen.png",
            "login_bonus.png": "04_login_bonus.png",
            "news.png": "05_news.png",
        }
        for template_name, reference_name in template_to_reference.items():
            score = self.best_score(
                REFERENCE_ROOT / reference_name,
                RECOVERY_ROOT / template_name,
            )
            with self.subTest(template=template_name):
                self.assertGreaterEqual(score, 0.90)

    def test_button_marker_without_context_does_not_satisfy_screen_pair(self):
        self.assertFalse(screen_pair_matches(None, 0.999))
        self.assertFalse(screen_pair_matches(0.999, None))
        self.assertFalse(screen_pair_matches(0.899, 0.999))
        self.assertFalse(screen_pair_matches(0.999, 0.899))
        self.assertTrue(screen_pair_matches(0.90, 0.90))

    def test_screen_contexts_and_buttons_match_inside_configured_rois(self):
        fixtures = {
            "data_update": (
                "01_data_update.png",
                "data_update_context.png",
                (60, 480, 660, 665),
                "data_update.png",
                (250, 645, 465, 750),
            ),
            "login_bonus": (
                "04_login_bonus.png",
                "login_bonus_context.png",
                (40, 75, 680, 195),
                "login_bonus.png",
                (240, 1000, 480, 1110),
            ),
            "news": (
                "05_news.png",
                "news_context.png",
                (0, 75, 720, 340),
                "news.png",
                (230, 990, 500, 1110),
            ),
        }
        for screen, (ref, context, context_roi, button, button_roi) in fixtures.items():
            context_score = self.roi_score(
                REFERENCE_ROOT / ref, RECOVERY_ROOT / context, context_roi
            )
            button_score = self.roi_score(
                REFERENCE_ROOT / ref, RECOVERY_ROOT / button, button_roi
            )
            with self.subTest(screen=screen):
                self.assertTrue(screen_pair_matches(context_score, button_score))

    def test_data_update_entry_requires_two_context_and_button_frames(self):
        pair_is_visible = screen_pair_matches(0.99, 0.99)
        self.assertIsNone(
            self.controller.update(
                self.matches(data_update=pair_is_visible), self.tap, now=1.0
            )
        )
        self.assertTrue(self.controller.blocks_normal_actions)
        event = self.controller.update(
            self.matches(data_update=pair_is_visible), self.tap, now=2.0
        )
        self.assertEqual(event, "tap:data_update")
        self.assertEqual(self.taps, ["data_update"])
        self.assertEqual(self.controller.state, "WAIT_WARNING")

    def test_screens_are_tapped_in_order_and_main_menu_is_required(self):
        self.controller.update(self.matches(data_update=True), self.tap, now=0.0)
        self.controller.update(self.matches(data_update=True), self.tap, now=1.0)
        sequence = ["warning", "title_screen", "login_bonus", "news"]
        expected_actions = [
            "warning_center", "title_center", "login_bonus", "news"
        ]
        now = 2.0
        for screen, action in zip(sequence, expected_actions):
            self.assertIsNone(
                self.controller.update(self.matches(**{screen: True}), self.tap, now=now)
            )
            now += 1
            self.assertEqual(
                self.controller.update(
                    self.matches(**{screen: True}), self.tap, now=now
                ),
                f"tap:{action}",
            )
            now += 1

        self.assertEqual(
            self.controller.update(self.matches(main_menu=True), self.tap, now=now),
            None,
        )
        now += 1
        self.assertEqual(
            self.controller.update(self.matches(main_menu=True), self.tap, now=now),
            "complete",
        )
        self.assertEqual(
            self.taps,
            [
                "data_update",
                "warning_center",
                "title_center",
                "login_bonus",
                "news",
            ],
        )

    def test_out_of_order_screen_enters_safe_wait_without_tapping(self):
        self.controller.update(self.matches(data_update=True), self.tap, now=0.0)
        self.controller.update(self.matches(data_update=True), self.tap, now=1.0)
        self.assertEqual(
            self.controller.update(
                self.matches(login_bonus=True), self.tap, now=2.0
            ),
            "safe_wait",
        )
        self.assertEqual(self.controller.state, "SAFE_WAIT")
        self.assertEqual(self.taps, ["data_update"])

    def test_missing_expected_screen_times_out_to_safe_wait(self):
        self.controller.update(self.matches(data_update=True), self.tap, now=0.0)
        self.controller.update(self.matches(data_update=True), self.tap, now=1.0)
        event = self.controller.update(self.matches(), self.tap, now=62.0)
        self.assertEqual(event, "safe_wait")
        self.assertEqual(self.taps, ["data_update"])

    def test_retry_is_bounded_and_requires_the_previous_screen(self):
        self.controller.update(self.matches(data_update=True), self.tap, now=0.0)
        self.controller.update(self.matches(data_update=True), self.tap, now=1.0)
        self.controller.update(self.matches(data_update=True), self.tap, now=13.0)
        self.assertEqual(
            self.controller.update(
                self.matches(data_update=True), self.tap, now=14.0
            ),
            "tap:data_update",
        )
        self.controller.update(self.matches(data_update=True), self.tap, now=27.0)
        self.assertIsNone(
            self.controller.update(
                self.matches(data_update=True), self.tap, now=28.0
            )
        )
        self.assertEqual(self.taps, ["data_update", "data_update"])

    def test_pause_freezes_recovery_deadlines_and_preserves_step(self):
        self.controller.update(self.matches(data_update=True), self.tap, now=0.0)
        self.controller.update(self.matches(data_update=True), self.tap, now=1.0)
        self.controller.pause(now=2.0)
        self.assertIsNone(
            self.controller.update(self.matches(), self.tap, now=200.0)
        )
        self.controller.resume(now=202.0)
        event = self.controller.update(self.matches(), self.tap, now=203.0)
        self.assertIsNone(event)
        self.assertEqual(self.controller.state, "WAIT_WARNING")
        self.assertEqual(self.taps, ["data_update"])

    def test_safe_wait_can_resume_only_when_current_expected_screen_returns(self):
        self.controller.update(self.matches(data_update=True), self.tap, now=0.0)
        self.controller.update(self.matches(data_update=True), self.tap, now=1.0)
        self.controller.update(self.matches(news=True), self.tap, now=2.0)
        self.assertEqual(self.controller.state, "SAFE_WAIT")
        self.controller.update(self.matches(), self.tap, now=3.0)
        self.controller.update(self.matches(warning=True), self.tap, now=4.0)
        self.assertIsNone(
            self.controller.update(self.matches(warning=True), self.tap, now=5.0)
        )
        self.assertEqual(self.taps, ["data_update"])
        self.assertEqual(self.controller.state, "WAIT_WARNING")
        self.assertEqual(
            self.controller.update(self.matches(warning=True), self.tap, now=6.0),
            "tap:warning_center",
        )

    def test_f9_can_stop_at_completion_or_safe_wait_without_tapping(self):
        self.assertFalse(recovery_shutdown_ready(True, "WAIT_WARNING"))
        self.assertTrue(recovery_shutdown_ready(True, "SAFE_WAIT"))
        self.assertTrue(recovery_shutdown_ready(True, "COMPLETE", "complete"))
        self.assertFalse(recovery_shutdown_ready(False, "SAFE_WAIT"))

    def test_resume_route_preserves_selected_mode_and_pc_wait(self):
        cases = {
            ("NINJA_ROAD_ONLY", "NINJA_ROAD"): "SWITCH_TO_NR",
            ("PHANTOM_CASTLE_ONLY", "PC_WAIT"): "PC_WAIT",
            ("PHANTOM_CASTLE_ONLY", "PHANTOM_CASTLE"): "SWITCH_TO_PC",
            ("DUAL_MODE", "PHANTOM_CASTLE"): "SWITCH_TO_PC",
            ("DUAL_MODE", "NINJA_ROAD"): "SWITCH_TO_NR",
            ("DUAL_MODE", "PC_WAIT"): "PC_WAIT",
            ("RAMEN_MISSION_ONLY", "RAMEN_IN_MISSION"): "RAMEN_START",
        }
        for (selected, previous), expected in cases.items():
            with self.subTest(selected=selected, previous=previous):
                self.assertEqual(
                    resume_mode_after_recovery(selected, previous), expected
                )


if __name__ == "__main__":
    unittest.main()
