import os
import sys
import time
import random
import traceback
from collections import defaultdict
from stability_support import (
    ProcessMemoryMonitor,
    StageStatistics,
    is_opencv_allocation_error,
    screenshot_as_grayscale,
    start_keyboard_listener,
    stop_keyboard_listener,
)
from recovery_support import (
    MidnightRecoveryController,
    recovery_shutdown_ready,
    resume_mode_after_recovery,
    screen_pair_matches,
)

try:
    import cv2
    import numpy as np
    import adbutils
    from PIL import Image

    # ---------------------------- CONFIG ----------------------------
    # In a PyInstaller build, keep templates and persistent files beside the EXE.
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    TIMER_FILE = os.path.join(BASE_DIR, "pc_timer.txt")

    EXPECTED_WIDTH, EXPECTED_HEIGHT = 720, 1280
    ADB_HOST, ADB_PORT = "127.0.0.1", 5037
    LDPLAYER_PORTS = (5554, 5555, 5556, 5557)

    PC_REGEN_SECONDS = 20 * 60
    PC_ONLY_RETRY_SECONDS = 25 * 60
    RAMEN_STAMINA_RETRY_SECONDS = 50 * 60
    RAMEN_REWARD_REVEAL_X, RAMEN_REWARD_REVEAL_Y = 350, 975
    CAROUSEL_X, CAROUSEL_Y = 75, 1000
    MAX_ADB_RETRIES = 5
    MAX_NAVIGATION_ATTEMPTS = 12
    ADB_RETRY_DELAY = 3

    ACTION_COOLDOWN = 1.8
    TRANSITION_TIMEOUT = 12
    STUCK_WARNING = 60

    # Optional (x1, y1, x2, y2) search regions confirmed from the supplied
    # 720x1280 restart references. Other templates use full-screen matching.
    TEMPLATE_ROI = {
        "templates/midnight_recovery/data_update_context.png": (60, 480, 660, 665),
        "templates/midnight_recovery/data_update.png": (250, 645, 465, 750),
        "templates/midnight_recovery/warning.png": (150, 300, 570, 800),
        "templates/midnight_recovery/title_screen.png": (160, 700, 600, 920),
        "templates/midnight_recovery/login_bonus_context.png": (40, 75, 680, 195),
        "templates/midnight_recovery/login_bonus.png": (240, 1000, 480, 1110),
        "templates/midnight_recovery/news_context.png": (0, 75, 720, 340),
        "templates/midnight_recovery/news.png": (230, 990, 500, 1110),
    }

    RECOVERY_CONTEXT_TEMPLATES = {
        "data_update": "templates/midnight_recovery/data_update_context.png",
        "login_bonus": "templates/midnight_recovery/login_bonus_context.png",
        "news": "templates/midnight_recovery/news_context.png",
    }
    RECOVERY_BUTTON_TEMPLATES = {
        "data_update": "templates/midnight_recovery/data_update.png",
        "login_bonus": "templates/midnight_recovery/login_bonus.png",
        "news": "templates/midnight_recovery/news.png",
    }
    RECOVERY_SINGLE_TEMPLATES = {
        "warning": "templates/midnight_recovery/warning.png",
        "title_screen": "templates/midnight_recovery/title_screen.png",
    }
    RECOVERY_TEMPLATE_PATHS = (
        tuple(RECOVERY_CONTEXT_TEMPLATES.values())
        + tuple(RECOVERY_BUTTON_TEMPLATES.values())
        + tuple(RECOVERY_SINGLE_TEMPLATES.values())
    )

    def log(message):
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    # -------------------------- PC TIMER ----------------------------
    def load_last_pc_time():
        try:
            with open(TIMER_FILE, "r") as f:
                return float(f.read().strip())
        except (OSError, ValueError):
            return 0.0

    def save_last_pc_time(timestamp):
        try:
            with open(TIMER_FILE, "w") as f:
                f.write(str(timestamp))
        except OSError as e:
            log(f"[Warning] Could not save PC timer: {e}")

    # -------------------------- MODE SELECTOR -----------------------
    def select_game_mode():
        while True:
            print("\n" + "=" * 54)
            print("             NARUTO BLAZING REVIVAL BOT")
            print("=" * 54)
            print("\n  SELECT GAME MODE\n")
            print("  [1] Ninja Road")
            print("      Choose whether to include Phantom Castle.\n")
            print("  [2] Phantom Castle")
            print("      Farm Phantom Castle using available stamina.\n")
            print("  [3] Normal Mission")
            print("      Start after manually opening the mission.\n")
            print("  [0] Exit")
            print("\n" + "-" * 54)
            choice = input("  Enter selection: ").strip()

            if choice == "1":
                include_pc = input(
                    "\n  Include Phantom Castle as well? (y/n): "
                ).strip().lower()
                return "DUAL_MODE" if include_pc in ("y", "yes") else "NINJA_ROAD_ONLY"
            if choice == "2":
                return "PHANTOM_CASTLE_ONLY"
            if choice == "3":
                return "RAMEN_MISSION_ONLY"
            if choice == "0":
                raise SystemExit
            print("  Invalid selection. Choose 0-3.")

    selected_mode = select_game_mode()

    # ------------------------- ADB HANDLING -------------------------
    adb = adbutils.AdbClient(host=ADB_HOST, port=ADB_PORT)
    device = None
    connection_retries = 0

    def connect_ldplayer():
        global device, connection_retries
        for attempt in range(1, MAX_ADB_RETRIES + 1):
            try:
                if device is not None:
                    try:
                        device.shell("echo connected")
                        return True
                    except Exception:
                        pass

                for port in LDPLAYER_PORTS:
                    try:
                        adb.connect(f"{ADB_HOST}:{port}")
                    except Exception:
                        pass

                devices = adb.device_list()
                if devices:
                    old_serial = device.serial if device is not None else None
                    device = next(
                        (d for d in devices if d.serial == old_serial),
                        devices[0]
                    )
                    device.shell("echo connected")
                    log(f"[ADB] Connected to {device.serial}")
                    return True
            except Exception as e:
                log(f"[ADB] Attempt {attempt}/{MAX_ADB_RETRIES}: {e}")

            connection_retries += 1
            time.sleep(ADB_RETRY_DELAY)
        return False

    if not connect_ldplayer():
        raise RuntimeError(
            "No LDPlayer ADB device found. Start LDPlayer and enable ADB."
        )

    # ---------------------------- HOTKEYS ---------------------------
    paused = False
    stop_after_current = False
    listener = None

    def toggle_pause():
        global paused
        paused = not paused
        log("*** BOT PAUSED ***" if paused else "*** BOT RESUMED ***")

    def request_graceful_stop():
        global stop_after_current, paused
        if not stop_after_current:
            stop_after_current = True
            paused = False
            log("*** F9: FINISH CURRENT CONTENT, THEN REPORT ***")

    try:
        from pynput import keyboard as pynput_kb
        listener = start_keyboard_listener(
            pynput_kb, toggle_pause, request_graceful_stop
        )
        hotkey_msg = "F8 = Pause/Resume | F9 = Finish current content and report"
    except ImportError:
        hotkey_msg = "Install pynput to enable F8/F9 hotkeys"
    except Exception as e:
        listener = None
        hotkey_msg = f"F8/F9 unavailable: keyboard listener failed ({e})"
        log(f"[Hotkeys] {hotkey_msg}")

    # ------------------------ ACTION INVENTORY ----------------------
    SHARED_ACTIONS = [
        ("templates/auto_off.png", "Enable Auto-Battle", 0.90),
        ("templates/next_btn.png", "Post-Battle Next", 0.85),
    ]

    NINJA_ROAD_ACTIONS = SHARED_ACTIONS + [
        ("templates/stamina_no.png", "NR Out of Stamina - Decline Refill", 0.88),
        ("templates/no_teams_ok.png", "Team Wiped - Dismiss Pop-up", 0.85),
        ("templates/mission_complete.png", "Mission Complete Banner", 0.85),
        ("templates/ready_btn.png", "Map Ready!!", 0.85),
        ("templates/depart_btn.png", "Confirm Depart", 0.85),
        ("templates/challenge_btn.png", "Start Ninja Road Run", 0.85),
    ]

    PHANTOM_CASTLE_ACTIONS = SHARED_ACTIONS + [
        ("templates/stamina_no.png", "PC Out of Stamina - Decline Refill", 0.88),
        ("templates/pc_runs_limit_ok.png", "PC Runs Limit Popup OK", 0.88),
        ("templates/pc_victory.png", "PC Victory Banner", 0.85),
        ("templates/pc_reward_ok.png", "PC Post-Fight Reward OK", 0.85),
        ("templates/pc_start_btn.png", "PC Start Battle", 0.85),
        ("templates/pc_opponent.png", "PC Select Opponent", 0.85),
    ]

    RAMEN_TEMPLATES = [
        ("templates/ramen_start_mission.png", "Normal Mission Start Mission", 0.82),
        ("templates/mission_complete.png", "Normal Mission Mission Complete (shared)", 0.85),
        ("templates/ramen_level_up_ok.png", "Normal Mission Level Up Popup OK", 0.85),
        ("templates/next_btn.png", "Normal Mission Post-Mission Next (shared)", 0.85),
        ("templates/ramen_retry.png", "Normal Mission Retry", 0.82),
        ("templates/ramen_stamina_no.png", "Normal Mission Not Enough Stamina", 0.85),
        ("templates/stamina_no.png", "Normal Mission Decline Stamina Refill", 0.88),
    ]

    NAVIGATION_TEMPLATES = [
        "templates/nav_back_btn.png",
        "templates/menu_pc_btn.png",
        "templates/menu_nr_btn.png",
    ]

    # ------------------------- TEMPLATE CACHE -----------------------
    # Cache grayscale image, width, height and optional ROI once.
    TEMPLATE_CACHE = {}
    MISSING_TEMPLATES = []

    def get_template(rel_path):
        if rel_path in TEMPLATE_CACHE:
            return TEMPLATE_CACHE[rel_path]

        full_path = os.path.join(BASE_DIR, rel_path)
        if not os.path.isfile(full_path):
            TEMPLATE_CACHE[rel_path] = None
            MISSING_TEMPLATES.append(rel_path)
            return None

        try:
            with Image.open(full_path) as image:
                with image.convert("L") as grayscale:
                    array = np.array(grayscale, copy=True)
            if array.ndim != 2 or array.size == 0:
                raise ValueError("Empty or invalid grayscale image")

            h, w = array.shape
            item = {
                "image": array,
                "width": w,
                "height": h,
                "roi": TEMPLATE_ROI.get(rel_path),
            }
            TEMPLATE_CACHE[rel_path] = item
            return item
        except Exception as e:
            if isinstance(e, MemoryError):
                raise
            log(f"[Template Error] {rel_path}: {e}")
            TEMPLATE_CACHE[rel_path] = None
            MISSING_TEMPLATES.append(rel_path)
            return None

    def preload_templates():
        paths = set()
        # Recovery recognition must be available regardless of selected mode.
        paths.update(RECOVERY_TEMPLATE_PATHS)
        paths.update(NAVIGATION_TEMPLATES)

        if selected_mode in ("NINJA_ROAD_ONLY", "DUAL_MODE"):
            paths.update(path for path, _, _ in NINJA_ROAD_ACTIONS)
            paths.add("templates/nav_back_btn.png")
            paths.add("templates/menu_nr_btn.png")

        if selected_mode in ("PHANTOM_CASTLE_ONLY", "DUAL_MODE"):
            paths.update(path for path, _, _ in PHANTOM_CASTLE_ACTIONS)
            paths.add("templates/nav_back_btn.png")
            paths.add("templates/menu_pc_btn.png")

        if selected_mode == "RAMEN_MISSION_ONLY":
            paths.update(path for path, _, _ in RAMEN_TEMPLATES)

        log("[Templates] Preloading selected-mode templates...")
        for path in sorted(paths):
            if get_template(path) is None:
                log(f"[Template Warning] Missing or invalid: {path}")

        loaded = sum(v is not None for v in TEMPLATE_CACHE.values())
        log(f"[Templates] Loaded {loaded}; missing/invalid {len(MISSING_TEMPLATES)}.")

    # ------------------------ SCREEN CAPTURE ------------------------
    def get_gray_screenshot():
        try:
            gray = screenshot_as_grayscale(device, np)

            h, w = gray.shape
            if (w, h) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
                log(
                    f"[Warning] Actual screenshot {w}x{h}; expected "
                    f"{EXPECTED_WIDTH}x{EXPECTED_HEIGHT}."
                )
            return gray
        except Exception as e:
            if isinstance(e, MemoryError):
                raise
            log(f"[ADB] Screenshot failed: {e}; reconnecting.")
            if not connect_ldplayer():
                raise RuntimeError("ADB screenshot failed and reconnect failed")
            return screenshot_as_grayscale(device, np)

    # ----------------------- SCREEN WATCHDOG ------------------------
    def fingerprint(gray):
        return cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)

    previous_fingerprint = None
    last_screen_change = time.monotonic()
    last_watchdog_log = 0.0

    def monitor_screen(gray):
        global previous_fingerprint, last_screen_change, last_watchdog_log
        current = fingerprint(gray)

        if previous_fingerprint is None:
            previous_fingerprint = current
            last_screen_change = time.monotonic()
            return

        delta = float(np.mean(cv2.absdiff(current, previous_fingerprint)))
        now = time.monotonic()
        if delta >= 1.5:
            previous_fingerprint = current
            last_screen_change = now
        elif (
            now - last_screen_change >= STUCK_WARNING
            and now - last_watchdog_log >= STUCK_WARNING
        ):
            log(
                f"[Watchdog] Screen barely changed for {STUCK_WARNING}s. "
                "No automatic blind tap will be sent."
            )
            last_watchdog_log = now

    # ---------------------- MATCHING AND TAPPING --------------------
    last_action_at = defaultdict(float)
    last_action_label = "None"
    pending_transition = None

    def match_template(gray, rel_path):
        cached = get_template(rel_path)
        if cached is None:
            return None

        search = gray
        ox = oy = 0
        roi = cached["roi"]
        if roi is not None:
            x1, y1, x2, y2 = map(int, roi)
            sh, sw = gray.shape
            x1, x2 = max(0, min(x1, sw)), max(0, min(x2, sw))
            y1, y2 = max(0, min(y1, sh)), max(0, min(y2, sh))
            if x2 <= x1 or y2 <= y1:
                return None
            search = gray[y1:y2, x1:x2]
            ox, oy = x1, y1

        if (
            cached["height"] > search.shape[0]
            or cached["width"] > search.shape[1]
        ):
            return None

        try:
            result = cv2.matchTemplate(
                search, cached["image"], cv2.TM_CCOEFF_NORMED
            )
            _, score, _, loc = cv2.minMaxLoc(result)
            return (
                score,
                (loc[0] + ox, loc[1] + oy),
                cached["width"],
                cached["height"],
            )
        except cv2.error as e:
            if is_opencv_allocation_error(e):
                raise MemoryError(f"OpenCV could not allocate memory matching {rel_path}") from e
            log(f"[OpenCV] {rel_path}: {e}")
            return None

    def is_visible(gray, rel_path, threshold=0.85):
        result = match_template(gray, rel_path)
        return result is not None and result[0] >= threshold

    def note_transition(action, before):
        global pending_transition
        pending_transition = {
            "action": action,
            "before": fingerprint(before),
            "started": time.monotonic(),
            "warned": False,
        }

    def verify_transition(gray):
        global pending_transition
        if pending_transition is None:
            return

        now = time.monotonic()
        delta = float(
            np.mean(cv2.absdiff(
                fingerprint(gray), pending_transition["before"]
            ))
        )
        if delta >= 1.5:
            log(f"[Transition] Screen changed after {pending_transition['action']}.")
            pending_transition = None
        elif (
            now - pending_transition["started"] >= TRANSITION_TIMEOUT
            and not pending_transition["warned"]
        ):
            log(
                f"[Transition Warning] No visible screen change after "
                f"{pending_transition['action']}. Rechecking current screen."
            )
            pending_transition["warned"] = True

    def find_and_tap(gray, rel_path, threshold=0.85, label="", click_anchor="center", double_tap=False):
        global last_action_label

        now = time.monotonic()
        if now - last_action_at[rel_path] < ACTION_COOLDOWN:
            return False

        result = match_template(gray, rel_path)
        if result is None:
            return False

        score, (left, top), width, height = result
        if score < threshold:
            return False

        sh, sw = gray.shape
        if click_anchor == "top_left":
            # Use the exact origin returned by cv2.matchTemplate.
            x, y = left, top
        else:
            x = left + width // 2
            y = top + height // 2
        x = max(0, min(int(x), sw - 1))
        y = max(0, min(int(y), sh - 1))

        try:
            device.click(x, y)
            if double_tap:
                time.sleep(0.18)
                device.click(x, y)
            last_action_at[rel_path] = now
            last_action_label = label or rel_path
            log(f"{label} (Match: {score:.2f}) -> {"Double-tap" if double_tap else "Tap"} ({x}, {y})")
            return True
        except Exception as e:
            log(f"[ADB] Tap failed: {e}")
            return False

    def safe_click(x, y, label):
        global last_action_label
        try:
            device.click(int(x), int(y))
            last_action_label = label
            log(f"{label} -> Tap ({x}, {y})")
            return True
        except Exception as e:
            log(f"[ADB] Click failed: {e}")
            return False

    # -------------------- MIDNIGHT RESTART RECOVERY -----------------
    recovery_controller = MidnightRecoveryController(
        screen_timeout=60.0,
        retry_delay=12.0,
        max_attempts=2,
        confirm_frames=2,
    )
    recovery_resume_mode = None

    def recovery_screen_matches(gray):
        matches = {}
        for name in RECOVERY_CONTEXT_TEMPLATES:
            context_result = match_template(
                gray, RECOVERY_CONTEXT_TEMPLATES[name]
            )
            button_result = match_template(
                gray, RECOVERY_BUTTON_TEMPLATES[name]
            )
            context_score = context_result[0] if context_result is not None else None
            button_score = button_result[0] if button_result is not None else None
            matches[name] = screen_pair_matches(
                context_score,
                button_score,
                context_threshold=0.90,
                button_threshold=0.90,
            )

        for name, path in RECOVERY_SINGLE_TEMPLATES.items():
            matches[name] = is_visible(gray, path, 0.90)

        if recovery_controller.expects_main_menu:
            matches["main_menu"] = (
                is_visible(gray, "templates/menu_nr_btn.png", 0.90)
                or is_visible(gray, "templates/menu_pc_btn.png", 0.90)
            )
        return matches

    def tap_recovery_action(action):
        button_actions = {
            "data_update": (
                "templates/midnight_recovery/data_update.png",
                "Dismiss Data Update Popup (OK)",
            ),
            "login_bonus": (
                "templates/midnight_recovery/login_bonus.png",
                "Confirm Login Bonus",
            ),
            "news": (
                "templates/midnight_recovery/news.png",
                "Close News",
            ),
        }
        if action in button_actions:
            path, label = button_actions[action]
            return find_and_tap(gray, path, 0.90, label)
        if action == "warning_center":
            return safe_click(360, 640, "Continue past Warning screen")
        if action == "title_center":
            return safe_click(360, 640, "Tap To Start")
        log(f"[Recovery] Refusing unknown recovery action: {action}")
        return False

    # ---------------------------- STATS -----------------------------
    stats = {
        "start_time": time.time(),
        "nr_completed": 0,
        "nr_wipes": 0,
        "total_nr_maps": 0,
        "pc_battles": 0,
        "pc_victories": 0,
        "pc_cycles": 0,
        "normal_mission_runs": 0,
    }
    stage_stats = StageStatistics(history_limit=100)
    memory_monitor = ProcessMemoryMonitor(interval_seconds=60.0)

    def format_duration(seconds):
        return (
            f"{int(seconds // 3600)}h "
            f"{int((seconds % 3600) // 60)}m "
            f"{int(seconds % 60)}s"
        )

    def print_session_report():
        elapsed = time.time() - stats["start_time"]
        ended = stats["nr_completed"] + stats["nr_wipes"]
        clear_rate = f"{100 * stats['nr_completed'] / ended:.1f}%" if ended else "N/A"
        avg_wipe = (
            f"Map {stage_stats.wipe_total / stage_stats.wipe_count:.1f}/20"
            if stage_stats.wipe_count else "N/A (0 wipes)"
        )
        recent_wipes = ", ".join(map(str, stage_stats.recent_wipes)) or "None"
        wipe_history = (
            f"{recent_wipes} (latest {len(stage_stats.recent_wipes)}"
            f" of {stage_stats.wipe_count})"
            if stage_stats.wipe_count > len(stage_stats.recent_wipes)
            else recent_wipes
        )
        avg_all = (
            f"Map {stage_stats.all_total / stage_stats.all_count:.1f}/20"
            if stage_stats.all_count else "N/A"
        )
        pc_rate = (
            f"{100 * stats['pc_victories'] / stats['pc_battles']:.1f}%"
            if stats["pc_battles"] else "N/A"
        )

        print("\n" + "=" * 54)
        print("           SESSION AUTOMATION REPORT")
        print("=" * 54)
        print(f"  Total Runtime             : {format_duration(elapsed)}")
        if selected_mode in ("NINJA_ROAD_ONLY", "DUAL_MODE"):
            print("-" * 54)
            print("  [NINJA ROAD]")
            print(f"  Full Runs Completed       : {stats['nr_completed']}")
            print(f"  Team Wipes                : {stats['nr_wipes']}")
            print(f"  Clear Rate                : {clear_rate}")
            print(f"  Avg Stage Until Wipe      : {avg_wipe}")
            print(f"  Wipe Map History          : {wipe_history}")
            print(f"  Avg Stage (All Runs)      : {avg_all}")
            print(f"  Total Maps Cleared/Entered: {stats['total_nr_maps']}")

        if pc_event_active:
            print("-" * 54)
            print("  [PHANTOM CASTLE]")
            print(f"  PC Battles Fought         : {stats['pc_battles']}")
            print(f"  PC Victories Logged       : {stats['pc_victories']} ({pc_rate})")
            print(f"  PC Stamina Cycles Done    : {stats['pc_cycles']}")

        if selected_mode == "RAMEN_MISSION_ONLY":
            print("-" * 54)
            print("  [NORMAL MISSION]")
            print(f"  Completed Runs            : {stats['normal_mission_runs']}")

        print("-" * 54)
        print("  [STABILITY]")
        print(f"  ADB Reconnection Attempts : {connection_retries}")
        print(f"  Last Action               : {last_action_label}")
        memory = memory_monitor.snapshot()
        def format_memory(value):
            return f"{value / (1024 * 1024):.1f} MB" if value is not None else "N/A"
        print(f"  Current RAM               : {format_memory(memory['current'])}")
        print(f"  Average RAM               : {format_memory(memory['average'])}")
        print(f"  Peak RAM                  : {format_memory(memory['peak'])}")
        print(f"  RAM Samples               : {memory['sample_count']}")
        if memory["error"]:
            print(f"  RAM Monitor Status        : {memory['error']}")
        print("=" * 54 + "\n")

    # ------------------------ NAVIGATION ----------------------------
    navigation_attempts = 0
    pc_main_menu_reached = False

    def navigate_to_pc(gray):
        global navigation_attempts, pc_main_menu_reached

        # Check the destination first. Once the main menu has been reached,
        # never treat a matching back-button image on the carousel as a
        # reason to leave the menu again.
        if find_and_tap(
            gray, "templates/menu_pc_btn.png", 0.80, "Open Phantom Castle"
        ):
            note_transition("Open Phantom Castle", gray)
            navigation_attempts = 0
            pc_main_menu_reached = False
            return "PHANTOM_CASTLE"

        if not pc_main_menu_reached:
            if find_and_tap(
                gray, "templates/nav_back_btn.png", 0.85, "Back to Main Menu"
            ):
                note_transition("Back to Main Menu", gray)
                pc_main_menu_reached = True
                navigation_attempts = 0
                log("[Navigation] Main menu reached; scanning carousel for Phantom Castle.")
                time.sleep(2.2)
                return "SWITCH_TO_PC"

        navigation_attempts += 1
        if navigation_attempts > MAX_NAVIGATION_ATTEMPTS:
            log("[Navigation] PC navigation attempts capped; restarting search.")
            navigation_attempts = 0
            time.sleep(3)
            return "SWITCH_TO_PC"

        safe_click(
            CAROUSEL_X + random.randint(-3, 3),
            CAROUSEL_Y + random.randint(-3, 3),
            "Rotate menu carousel"
        )
        time.sleep(1.5)
        return "SWITCH_TO_PC"

    # Startup navigation is deliberately forced for Ninja Road selections:
    # leave the current screen, return to the main menu, then locate Ninja Road
    # through the carousel before beginning a run.
    force_nr_startup_navigation = selected_mode in (
        "NINJA_ROAD_ONLY", "DUAL_MODE"
    )

    def navigate_to_nr(gray):
        global navigation_attempts, force_nr_startup_navigation

        # During startup, do not accept an already-visible Ninja Road landing
        # screen: first back out to the main menu and reselect it via carousel.
        if (
            not force_nr_startup_navigation
            and is_visible(gray, "templates/challenge_btn.png", 0.85)
        ):
            navigation_attempts = 0
            log("=== Returned to Ninja Road Mode ===")
            return "NINJA_ROAD"

        if find_and_tap(
            gray, "templates/menu_nr_btn.png", 0.80, "Open Ninja Road"
        ):
            note_transition("Open Ninja Road", gray)
            navigation_attempts = 0
            force_nr_startup_navigation = False
            time.sleep(2.5)
            return "SWITCH_TO_NR"

        if find_and_tap(
            gray, "templates/nav_back_btn.png", 0.85, "Back to Main Menu"
        ):
            note_transition("Back to Main Menu", gray)
            navigation_attempts += 1
            time.sleep(2.2)
            return "SWITCH_TO_NR"

        navigation_attempts += 1
        if navigation_attempts > MAX_NAVIGATION_ATTEMPTS:
            log("[Navigation] NR navigation attempts capped; restarting search.")
            navigation_attempts = 0
            time.sleep(3)
            return "SWITCH_TO_NR"

        safe_click(
            CAROUSEL_X + random.randint(-3, 3),
            CAROUSEL_Y + random.randint(-3, 3),
            "Rotate menu carousel"
        )
        time.sleep(1.5)
        return "SWITCH_TO_NR"

    # --------------------------- STARTUP ----------------------------
    # Selecting Phantom Castle (or including it with Ninja Road) means
    # the user has chosen to run that mode; no extra event-availability prompt.
    pc_event_active = selected_mode in ("PHANTOM_CASTLE_ONLY", "DUAL_MODE")

    preload_templates()
    last_pc_run_time = load_last_pc_time()

    if selected_mode == "PHANTOM_CASTLE_ONLY":
        mode = "SWITCH_TO_PC"
    elif selected_mode == "RAMEN_MISSION_ONLY":
        mode = "RAMEN_START"
    else:
        # Route through the main menu/carousel before starting Ninja Road.
        mode = "SWITCH_TO_NR"
    runs_started = 0
    current_map = 0
    nr_stamina_cooldown_until = 0.0
    pc_retry_until = 0.0
    report_printed = False

    mode_names = {
        "NINJA_ROAD_ONLY": "Ninja Road Only Mode Active",
        "PHANTOM_CASTLE_ONLY": "Phantom Castle Only Mode Active",
        "RAMEN_MISSION_ONLY": "Normal Mission Mode Active",
        "DUAL_MODE": "Ninja Road + Phantom Castle Mode Active",
    }
    log(mode_names[selected_mode])
    if selected_mode == "RAMEN_MISSION_ONLY":
        log("[Normal Mission] Manually open the mission and leave the bot at Start Mission.")
        log("[Normal Mission] Waiting for the Start Mission button; automation begins there.")
    log(hotkey_msg)

    # ------------------------- MAIN LOOP ----------------------------
    try:
        memory_monitor.start()
        while True:
            if paused:
                recovery_controller.pause()
                time.sleep(0.5)
                continue
            recovery_controller.resume()

            try:
                # Exactly one fresh screenshot per normal loop iteration.
                gray = get_gray_screenshot()
                monitor_screen(gray)
                verify_transition(gray)

                # Recovery recognition preempts every mode-specific action.
                recovery_matches = recovery_screen_matches(gray)
                recovery_event = recovery_controller.update(
                    recovery_matches, tap_recovery_action
                )
                if recovery_controller.blocks_normal_actions or recovery_event == "complete":
                    if recovery_resume_mode is None:
                        recovery_resume_mode = mode
                        pending_transition = None

                    if recovery_event == "safe_wait":
                        log(
                            f"[Recovery] Safe wait: {recovery_controller.safe_reason}. "
                            "No taps or normal mode actions will be sent."
                        )
                    elif recovery_event == "complete":
                        previous_mode = recovery_resume_mode or mode
                        recovery_resume_mode = None
                        resumed_mode = resume_mode_after_recovery(
                            selected_mode, previous_mode
                        )
                        if recovery_shutdown_ready(
                            stop_after_current,
                            recovery_controller.state,
                            recovery_event,
                        ):
                            mode = "DONE"
                            log("[Recovery] Main menu verified; honoring F9 shutdown.")
                        else:
                            mode = resumed_mode
                            pc_main_menu_reached = True
                            force_nr_startup_navigation = False
                            log(
                                f"[Recovery] Main menu verified; resuming "
                                f"{mode_names[selected_mode]} via {mode}."
                            )
                            if selected_mode == "RAMEN_MISSION_ONLY":
                                log(
                                    "[Normal Mission] Recovery returned to the menu; "
                                    "open the mission manually to resume automation."
                                )
                        recovery_controller.reset()
                        if mode == "DONE":
                            break
                        continue

                    if recovery_shutdown_ready(
                        stop_after_current, recovery_controller.state, recovery_event
                    ):
                        mode = "DONE"
                        log("[Recovery] F9 requested during safe wait; stopping without further taps.")
                        break
                    continue
                elif recovery_resume_mode is not None:
                    # A one-frame false candidate disappeared; no recovery began.
                    recovery_resume_mode = None

                acted = False
                pc_is_ready = (
                    pc_event_active
                    and time.time() - last_pc_run_time >= PC_REGEN_SECONDS
                )

                # ================ NINJA ROAD =================
                if mode == "NINJA_ROAD":
                    if (
                        stop_after_current
                        and is_visible(gray, "templates/challenge_btn.png", 0.85)
                    ):
                        log("[F9 Stop] Ninja Road run concluded.")
                        break

                    if time.time() < nr_stamina_cooldown_until and not pc_is_ready:
                        if stop_after_current:
                            log("[F9 Stop] Ninja Road stamina unavailable.")
                            break
                        time.sleep(5)
                        continue

                    for path, label, threshold in NINJA_ROAD_ACTIONS:
                        if (
                            "Start Ninja Road Run" in label
                            and pc_is_ready
                            and not stop_after_current
                            and is_visible(gray, path, threshold)
                        ):
                            log("[Mode Switch] Phantom Castle stamina ready.")
                            mode = "SWITCH_TO_PC"
                            acted = True
                            break

                        if find_and_tap(gray, path, threshold, label):
                            note_transition(label, gray)

                            if "Start Ninja Road Run" in label:
                                runs_started += 1
                                log(f"=== Starting Ninja Road Run #{runs_started} ===")
                            elif "Confirm Depart" in label:
                                current_map = 0
                            elif "Map Ready!!" in label:
                                current_map += 1
                                stats["total_nr_maps"] += 1
                                log(f"--- [Map {current_map}/20] ---")
                            elif "Mission Complete Banner" in label:
                                stats["nr_completed"] += 1
                                stage_stats.record(20)
                            elif "Team Wiped" in label:
                                stage = current_map if current_map > 0 else 1
                                stats["nr_wipes"] += 1
                                stage_stats.record(stage, wiped=True)
                                log(f"[Stats] Team wipe on Map {stage}/20.")
                            elif "NR Out of Stamina" in label:
                                nr_stamina_cooldown_until = time.time() + PC_REGEN_SECONDS
                                resume = time.strftime(
                                    "%H:%M:%S",
                                    time.localtime(nr_stamina_cooldown_until)
                                )
                                log(f"[NR Stamina] Cooldown until {resume}.")
                                if stop_after_current:
                                    break
                                if pc_is_ready:
                                    mode = "SWITCH_TO_PC"

                            acted = True
                            time.sleep(random.uniform(2.0, 2.6))
                            break

                    if stop_after_current and time.time() < nr_stamina_cooldown_until:
                        break

                # =========== NINJA ROAD -> PHANTOM CASTLE ===========
                elif mode == "SWITCH_TO_PC":
                    mode = navigate_to_pc(gray)
                    acted = True

                # ===== PHANTOM CASTLE-ONLY STAMINA WAIT =====
                elif mode == "PC_WAIT":
                    remaining = pc_retry_until - time.time()
                    if remaining <= 0:
                        log("[Phantom Castle] 25-minute wait complete; retrying.")
                        mode = "SWITCH_TO_PC"
                    else:
                        # Keep the wait lightweight; no game actions are sent.
                        if int(remaining) % 60 == 0:
                            log(
                                f"[Phantom Castle] Retry in "
                                f"{int(remaining // 60)} minute(s)."
                            )
                        time.sleep(min(5, remaining))
                    acted = True

                # ================ PHANTOM CASTLE ====================
                elif mode == "PHANTOM_CASTLE":
                    for path, label, threshold in PHANTOM_CASTLE_ACTIONS:
                        if find_and_tap(gray, path, threshold, label):
                            note_transition(label, gray)

                            if "PC Select Opponent" in label:
                                stats["pc_battles"] += 1
                            elif "PC Victory Banner" in label:
                                stats["pc_victories"] += 1
                            elif "PC Runs Limit Popup" in label:
                                # This rare limit popup also blocks the battle
                                # after opponent selection; treat it as a 25m
                                # PC cooldown without spending pearls.
                                if stats["pc_battles"] > 0:
                                    stats["pc_battles"] -= 1
                                stats["pc_cycles"] += 1
                                cooldown_started = time.time()

                                if stop_after_current:
                                    log("[Phantom Castle] F9 stop requested; session complete.")
                                    mode = "DONE"
                                elif selected_mode == "PHANTOM_CASTLE_ONLY":
                                    last_pc_run_time = cooldown_started
                                    save_last_pc_time(last_pc_run_time)
                                    pc_main_menu_reached = False
                                    pc_retry_until = cooldown_started + PC_ONLY_RETRY_SECONDS
                                    retry_at = time.strftime(
                                        "%H:%M:%S", time.localtime(pc_retry_until)
                                    )
                                    log(
                                        "[Phantom Castle] Rare battle-limit popup dismissed. "
                                        f"Waiting 25 minutes; retry at {retry_at}."
                                    )
                                    mode = "PC_WAIT"
                                else:
                                    # Dual mode normally uses a 20m readiness
                                    # interval. Offset the saved timestamp by 5m
                                    # so its existing readiness check enforces 25m.
                                    last_pc_run_time = cooldown_started + (
                                        PC_ONLY_RETRY_SECONDS - PC_REGEN_SECONDS
                                    )
                                    save_last_pc_time(last_pc_run_time)
                                    next_pc = time.strftime(
                                        "%H:%M:%S",
                                        time.localtime(last_pc_run_time + PC_REGEN_SECONDS)
                                    )
                                    log(
                                        "[Phantom Castle] Rare battle-limit popup dismissed. "
                                        f"Returning to Ninja Road; PC retry available at {next_pc}."
                                    )
                                    mode = "SWITCH_TO_NR"

                            elif "PC Out of Stamina" in label:
                                # Undo the opponent-selection count for the
                                # unstarted battle when stamina is exhausted.
                                if stats["pc_battles"] > 0:
                                    stats["pc_battles"] -= 1
                                stats["pc_cycles"] += 1
                                last_pc_run_time = time.time()
                                save_last_pc_time(last_pc_run_time)

                                if stop_after_current:
                                    log("[Phantom Castle] F9 stop requested; session complete.")
                                    mode = "DONE"
                                elif selected_mode == "PHANTOM_CASTLE_ONLY":
                                    # The retry begins from the current PC screen,
                                    # so the next navigation pass must back out once.
                                    pc_main_menu_reached = False
                                    pc_retry_until = time.time() + PC_ONLY_RETRY_SECONDS
                                    retry_at = time.strftime(
                                        "%H:%M:%S",
                                        time.localtime(pc_retry_until)
                                    )
                                    log(
                                        "[Phantom Castle] No stamina remaining. "
                                        f"Waiting 25 minutes; retry at {retry_at}."
                                    )
                                    mode = "PC_WAIT"
                                else:
                                    next_pc = time.strftime(
                                        "%H:%M:%S",
                                        time.localtime(last_pc_run_time + PC_REGEN_SECONDS)
                                    )
                                    log(
                                        f"[Mode Switch] PC stamina spent; "
                                        f"returning to Ninja Road until {next_pc}."
                                    )
                                    mode = "SWITCH_TO_NR"

                            acted = True
                            time.sleep(random.uniform(2.0, 2.6))
                            break

                    if mode == "DONE":
                        break

                # ================= NORMAL MISSION ==================
                elif mode == "RAMEN_START":
                    if stop_after_current:
                        log("[F9 Stop] Normal Mission session stopped before starting another run.")
                        mode = "DONE"
                    elif find_and_tap(gray, "templates/ramen_start_mission.png", 0.82, "Start Normal Mission"):
                        note_transition("Start Normal Mission", gray)
                        ramen_auto_enabled = False
                        mode = "RAMEN_IN_MISSION"
                        acted = True

                elif mode == "RAMEN_IN_MISSION":
                    if not ramen_auto_enabled and find_and_tap(
                        gray, "templates/auto_off.png", 0.90, "Enable Auto-Battle"
                    ):
                        note_transition("Enable Auto-Battle", gray)
                        ramen_auto_enabled = True
                        time.sleep(1.0)
                        acted = True
                    elif find_and_tap(gray, "templates/mission_complete.png", 0.85,
                                      "Dismiss Mission Complete"):
                        note_transition("Dismiss Mission Complete", gray)
                        stats["normal_mission_runs"] += 1
                        if stop_after_current:
                            log("[F9 Stop] Current Normal Mission completed.")
                            mode = "DONE"
                        else:
                            mode = "RAMEN_POST_NEXT"
                        acted = True

                elif mode == "RAMEN_POST_NEXT":
                    # Level Up can appear over the first post-mission screen.
                    # Dismiss it before attempting to press Next.
                    if find_and_tap(gray, "templates/ramen_level_up_ok.png", 0.85,
                                    "Dismiss Normal Mission Level Up Popup (OK)"):
                        note_transition("Dismiss Normal Mission Level Up Popup", gray)
                        time.sleep(1.0)
                        acted = True
                    elif find_and_tap(gray, "templates/next_btn.png", 0.85,
                                      "Normal Mission Post-Mission Next"):
                        note_transition("Normal Mission Post-Mission Next", gray)
                        mode = "RAMEN_REWARD_REVEAL"
                        acted = True

                elif mode == "RAMEN_REWARD_REVEAL":
                    if safe_click(RAMEN_REWARD_REVEAL_X, RAMEN_REWARD_REVEAL_Y,
                                  "Reveal Normal Mission Retry Button (1/2)"):
                        time.sleep(1.0)
                        safe_click(RAMEN_REWARD_REVEAL_X, RAMEN_REWARD_REVEAL_Y,
                                   "Reveal Normal Mission Retry Button (2/2)")
                        time.sleep(1.0)
                        ramen_retry_screen_wait_started = time.time()
                        mode = "RAMEN_RETRY"
                        acted = True

                elif mode == "RAMEN_RETRY":
                    if find_and_tap(gray, "templates/ramen_retry.png", 0.82,
                                    "Retry Normal Mission"):
                        note_transition("Retry Normal Mission", gray)
                        ramen_retry_last_tap = time.time()
                        mode = "RAMEN_RETRY_CHECK"
                    elif time.time() - ramen_retry_screen_wait_started >= 3:
                        log("[Normal Mission] Retry button not detected after reveal; repeating both reveal taps.")
                        mode = "RAMEN_REWARD_REVEAL"
                        acted = True

                elif mode == "RAMEN_RETRY_CHECK":
                    if is_visible(gray, "templates/ramen_stamina_no.png", 0.85):
                        if find_and_tap(gray, "templates/stamina_no.png", 0.88,
                                        "Decline Normal Mission Stamina Refill"):
                            note_transition("Decline Normal Mission Stamina Refill", gray)
                            ramen_retry_until = time.time() + RAMEN_STAMINA_RETRY_SECONDS
                            retry_at = time.strftime("%H:%M:%S", time.localtime(ramen_retry_until))
                            log(f"[Normal Mission] Not enough stamina. Declined refill; waiting 50 minutes until {retry_at}.")
                            mode = "RAMEN_STAMINA_WAIT"
                    elif is_visible(gray, "templates/ramen_start_mission.png", 0.82):
                        log("[Normal Mission] Retry accepted; returned to Start Mission.")
                        mode = "RAMEN_START"
                    elif time.time() - ramen_retry_last_tap >= 5 and is_visible(
                        gray, "templates/ramen_retry.png", 0.82
                    ):
                        log("[Normal Mission] Retry screen still visible; tapping Retry again.")
                        mode = "RAMEN_RETRY"
                    else:
                        time.sleep(1.0)
                    acted = True

                elif mode == "RAMEN_STAMINA_WAIT":
                    if stop_after_current:
                        log("[F9 Stop] Stopping during Normal Mission stamina wait.")
                        mode = "DONE"
                        acted = True
                    else:
                        remaining = ramen_retry_until - time.time()
                        if remaining <= 0:
                            log("[Normal Mission] 50-minute stamina wait complete; retrying.")
                            mode = "RAMEN_RETRY"
                        else:
                            if int(remaining) % 60 < 5:
                                log(f"[Normal Mission] Retry available in {int(remaining // 60)} minute(s).")
                            time.sleep(min(5, remaining))
                        acted = True

                # =========== PHANTOM CASTLE -> NINJA ROAD ===========
                elif mode == "SWITCH_TO_NR":
                    if selected_mode == "RAMEN_MISSION_ONLY":
                        log("[Safety] Normal Mission mode blocked menu navigation; returning to Start Mission loop.")
                        mode = "RAMEN_START"
                    else:
                        mode = navigate_to_nr(gray)
                    acted = True

                else:
                    if selected_mode == "RAMEN_MISSION_ONLY":
                        log(f"[Normal Mission] Unexpected state {mode}; returning to Start Mission loop.")
                        mode = "RAMEN_START"
                    else:
                        log(f"[Warning] Unknown mode {mode}; resetting to Ninja Road.")
                        mode = "NINJA_ROAD"

                if mode == "DONE":
                    break

                if not acted:
                    time.sleep(1.0)

            except MemoryError as e:
                log(f"[Fatal Memory Error] {e}. Stopping automation safely.")
                raise
            except cv2.error as e:
                if is_opencv_allocation_error(e):
                    log(f"[Fatal OpenCV Memory Error] {e}. Stopping automation safely.")
                    raise MemoryError("OpenCV memory allocation failed") from e
                log(f"[Warning] Main-loop OpenCV error: {e}")
                log("[ADB] Attempting reconnection...")
                if not connect_ldplayer():
                    raise RuntimeError("LDPlayer reconnection failed")
                time.sleep(ADB_RETRY_DELAY)
            except Exception as e:
                log(f"[Warning] Main-loop error: {e}")
                log("[ADB] Attempting reconnection...")
                if not connect_ldplayer():
                    raise RuntimeError("LDPlayer reconnection failed")
                time.sleep(ADB_RETRY_DELAY)

    finally:
        memory_monitor.stop()
        if listener is not None:
            try:
                stop_keyboard_listener(listener)
            except Exception as e:
                log(f"[Hotkeys] Listener shutdown error: {e}")

        if not report_printed:
            print_session_report()
            report_printed = True

    input("Session finished. Press Enter to close this window...")

except SystemExit:
    pass
except Exception:
    print("\n[CRASH] Unexpected error:")
    traceback.print_exc()
    input("\nPress Enter to close this window...")
