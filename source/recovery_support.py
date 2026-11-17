"""Deterministic state handling for the game's midnight restart sequence."""
from __future__ import annotations

import time


def screen_pair_matches(context_score, button_score, context_threshold=0.90, button_threshold=0.90):
    """Require both screen context and its corresponding button marker."""
    return (
        context_score is not None
        and button_score is not None
        and context_score >= context_threshold
        and button_score >= button_threshold
    )


def resume_mode_after_recovery(selected_mode: str, previous_mode: str) -> str:
    """Route the selected mode back in from a positively verified menu."""
    if selected_mode == "RAMEN_MISSION_ONLY":
        # Normal Mission requires the user to open the mission manually.
        return "RAMEN_START"
    if selected_mode == "PHANTOM_CASTLE_ONLY":
        return "PC_WAIT" if previous_mode == "PC_WAIT" else "SWITCH_TO_PC"
    if selected_mode == "NINJA_ROAD_ONLY":
        return "SWITCH_TO_NR"
    if selected_mode == "DUAL_MODE":
        if previous_mode == "PC_WAIT":
            return "PC_WAIT"
        if previous_mode in ("SWITCH_TO_PC", "PHANTOM_CASTLE"):
            return "SWITCH_TO_PC"
        return "SWITCH_TO_NR"
    raise ValueError(f"Unknown selected mode: {selected_mode}")


def recovery_shutdown_ready(stop_requested: bool, state: str, event=None) -> bool:
    """F9 may stop at completed recovery or immediately from its safe wait."""
    return bool(stop_requested and (event == "complete" or state == "SAFE_WAIT"))


class MidnightRecoveryController:
    """Gate gameplay actions while a visually identified restart is recovered.

    ``matches`` contains booleans for each screen marker. Data Update, Login
    Bonus, and News booleans must already represent a context AND button match.
    The injected tap callback receives one of the action names returned by
    ``action_for_screen`` and returns whether the tap was sent successfully.
    """

    SCREENS = (
        "data_update",
        "warning",
        "title_screen",
        "login_bonus",
        "news",
    )
    ACTIONS = {
        "data_update": "data_update",
        "warning": "warning_center",
        "title_screen": "title_center",
        "login_bonus": "login_bonus",
        "news": "news",
    }

    def __init__(
        self,
        screen_timeout: float = 60.0,
        retry_delay: float = 12.0,
        max_attempts: int = 2,
        confirm_frames: int = 2,
        clock=None,
    ):
        if screen_timeout <= 0 or retry_delay <= 0:
            raise ValueError("Recovery timeouts must be positive")
        if max_attempts < 1 or confirm_frames < 1:
            raise ValueError("Recovery attempts and confirmation frames must be positive")
        self.screen_timeout = float(screen_timeout)
        self.retry_delay = float(retry_delay)
        self.max_attempts = int(max_attempts)
        self.confirm_frames = int(confirm_frames)
        self._clock = clock or time.monotonic
        self.reset()

    @property
    def active(self) -> bool:
        return self.state not in ("IDLE", "COMPLETE")

    @property
    def blocks_normal_actions(self) -> bool:
        return self.active or self._candidate_count > 0

    @property
    def expects_main_menu(self) -> bool:
        return self.active and self.step_index >= len(self.SCREENS)

    def reset(self) -> None:
        self.state = "IDLE"
        self.step_index = 0
        self.attempts = [0] * len(self.SCREENS)
        self.last_tap_index = None
        self.last_tap_at = None
        self.stage_started_at = None
        self.safe_reason = None
        self._candidate_name = None
        self._candidate_count = 0
        self._safe_expected_index = None
        self._safe_resume_state = None
        self._paused_at = None

    def pause(self, now: float | None = None) -> None:
        if self.active and self._paused_at is None:
            self._paused_at = self._now(now)

    def resume(self, now: float | None = None) -> None:
        if self._paused_at is None:
            return
        resumed_at = self._now(now)
        paused_for = max(0.0, resumed_at - self._paused_at)
        if self.stage_started_at is not None:
            self.stage_started_at += paused_for
        if self.last_tap_at is not None:
            self.last_tap_at += paused_for
        self._paused_at = None

    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    def _candidate(self, name: str, matched: bool) -> bool:
        if not matched:
            self._candidate_name = None
            self._candidate_count = 0
            return False
        if self._candidate_name == name:
            self._candidate_count += 1
        else:
            self._candidate_name = name
            self._candidate_count = 1
        return self._candidate_count >= self.confirm_frames

    def _enter_safe_wait(self, reason: str, expected_index: int, resume_state: str):
        changed = self.state != "SAFE_WAIT"
        self.state = "SAFE_WAIT"
        self.safe_reason = reason
        self._safe_expected_index = expected_index
        self._safe_resume_state = resume_state
        self._candidate_name = None
        self._candidate_count = 0
        return "safe_wait" if changed else None

    def _issue_tap(self, index: int, tap, now: float):
        screen = self.SCREENS[index]
        action = self.ACTIONS[screen]
        self.attempts[index] += 1
        self.last_tap_index = index
        self.last_tap_at = now
        succeeded = bool(tap(action))
        if succeeded:
            self.step_index = index + 1
            self.stage_started_at = now
            self.state = (
                f"WAIT_{self.SCREENS[self.step_index].upper()}"
                if self.step_index < len(self.SCREENS)
                else "VERIFY_MAIN_MENU"
            )
        else:
            # Keep this screen expected; a failed ADB tap can be retried only
            # after retry_delay and while the same verified screen remains.
            self.step_index = index
            if self.stage_started_at is None:
                self.stage_started_at = now
            self.state = f"WAIT_{screen.upper()}"
        self._candidate_name = None
        self._candidate_count = 0
        return f"tap:{action}" if succeeded else f"tap_failed:{action}"

    def _retry_previous(self, matches, tap, now: float):
        index = self.last_tap_index
        if index is None or self.last_tap_at is None:
            return None
        if not matches.get(self.SCREENS[index], False):
            self._candidate_name = None
            self._candidate_count = 0
            return None
        if now - self.last_tap_at < self.retry_delay:
            return None
        if self.attempts[index] >= self.max_attempts:
            return None
        if not self._candidate(self.SCREENS[index], True):
            return None
        return self._issue_tap(index, tap, now)

    def update(self, matches: dict, tap, now: float | None = None):
        """Consume one screenshot's recognition results and optionally tap.

        Returns a small event string (for logging) or ``None``. Any detected
        out-of-order recovery screen enters SAFE_WAIT; it never skips a step.
        """
        now = self._now(now)
        if self._paused_at is not None:
            return None

        if self.state == "COMPLETE":
            return "complete"

        if self.state == "IDLE":
            if matches.get("data_update", False):
                if not self._candidate("data_update", True):
                    return None
                self.stage_started_at = now
                self.state = "WAIT_DATA_UPDATE"
                return self._issue_tap(0, tap, now)
            self._candidate_name = None
            self._candidate_count = 0
            unexpected = next(
                (name for name in self.SCREENS[1:] if matches.get(name, False)),
                None,
            )
            if unexpected is not None:
                return self._enter_safe_wait(
                    f"Detected {unexpected} without the Data Update entry screen",
                    0,
                    "WAIT_DATA_UPDATE",
                )
            return None

        if self.state == "SAFE_WAIT":
            expected_index = self._safe_expected_index
            if expected_index is None:
                return None
            expected = (
                "main_menu"
                if expected_index >= len(self.SCREENS)
                else self.SCREENS[expected_index]
            )
            if not matches.get(expected, False):
                self._candidate_name = None
                self._candidate_count = 0
                return None
            if not self._candidate(expected, True):
                return None
            self.state = self._safe_resume_state or "VERIFY_MAIN_MENU"
            self.safe_reason = None
            self._candidate_name = None
            self._candidate_count = 0

        if self.step_index >= len(self.SCREENS):
            if matches.get("main_menu", False):
                if self._candidate("main_menu", True):
                    self.state = "COMPLETE"
                    self._candidate_name = None
                    self._candidate_count = 0
                    return "complete"
                return None

            if any(matches.get(name, False) for name in self.SCREENS):
                return self._enter_safe_wait(
                    "A recovery screen appeared while verifying the main menu",
                    len(self.SCREENS),
                    "VERIFY_MAIN_MENU",
                )
            event = self._retry_previous(matches, tap, now)
            if event:
                return event
            if (
                self.stage_started_at is not None
                and now - self.stage_started_at >= self.screen_timeout
            ):
                return self._enter_safe_wait(
                    "Main menu was not positively recognized after News closed",
                    len(self.SCREENS),
                    "VERIFY_MAIN_MENU",
                )
            return None

        expected_index = self.step_index
        expected = self.SCREENS[expected_index]
        if matches.get(expected, False):
            if not self._candidate(expected, True):
                return None
            if self.attempts[expected_index] < self.max_attempts and (
                self.attempts[expected_index] == 0
                or (
                    self.last_tap_index == expected_index
                    and self.last_tap_at is not None
                    and now - self.last_tap_at >= self.retry_delay
                )
            ):
                return self._issue_tap(expected_index, tap, now)
            self._candidate_name = None
            self._candidate_count = 0
        else:
            event = self._retry_previous(matches, tap, now)
            if event:
                return event
            unexpected = next(
                (
                    name
                    for name in self.SCREENS
                    if name != expected
                    and name != (
                        self.SCREENS[self.last_tap_index]
                        if self.last_tap_index is not None
                        else None
                    )
                    and matches.get(name, False)
                ),
                None,
            )
            if unexpected is not None:
                return self._enter_safe_wait(
                    f"Expected {expected}; detected {unexpected} out of order",
                    expected_index,
                    f"WAIT_{expected.upper()}",
                )

        if (
            self.stage_started_at is not None
            and now - self.stage_started_at >= self.screen_timeout
        ):
            return self._enter_safe_wait(
                f"Timed out waiting for {expected}",
                expected_index,
                f"WAIT_{expected.upper()}",
            )
        return None
