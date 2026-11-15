"""
Blazing Bot Launcher
- Checks the latest stable GitHub Release.
- Downloads and SHA-256 verifies the release ZIP.
- Installs only application files, preserving timer/log/config data.
- Launches the bot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# REQUIRED: replace these with your GitHub account and repository.
GITHUB_OWNER = "BlazingPytos"
GITHUB_REPO = "naruto-blazing-bot"

RELEASE_ZIP_NAME = "NarutoBlazingBot-win64.zip"
CHECKSUMS_NAME = "SHA256SUMS.txt"
APP_FOLDER_NAME = "NarutoBlazingBot"
BOT_EXE_NAME = "NarutoBlazingBot.exe"
GITHUB_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)

ROOT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
APP_DIR = ROOT_DIR / APP_FOLDER_NAME
BOT_EXE = APP_DIR / BOT_EXE_NAME
PERSISTENT_NAMES = {"pc_timer.txt", "logs", "user_config.json", "config.json"}

def say(message: str) -> None:
    print(message, flush=True)

def version_tuple(value: str):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(map(int, match.groups())) if match else (0, 0, 0)

def read_installed_version() -> str:
    path = APP_DIR / "version.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"

def github_json(url: str):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "BlazingBotLauncher",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))

def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "BlazingBotLauncher"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)

def find_asset(release: dict, name: str) -> str:
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return asset["browser_download_url"]
    raise RuntimeError(f"Release is missing required asset: {name}")

def get_expected_hash(checksum_text: str, filename: str) -> str:
    for line in checksum_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            candidate = parts[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", candidate):
                return candidate
    raise RuntimeError(f"No valid SHA-256 entry found for {filename}")

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def safe_extract(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for item in archive.infolist():
            target = (destination / item.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("Release ZIP contains an unsafe path.")
        archive.extractall(destination)

def install_release(release: dict, release_version: str) -> None:
    zip_url = find_asset(release, RELEASE_ZIP_NAME)
    checksum_url = find_asset(release, CHECKSUMS_NAME)

    with tempfile.TemporaryDirectory(prefix="blazing_bot_update_") as temp_name:
        temp = Path(temp_name)
        archive_path = temp / RELEASE_ZIP_NAME
        checksum_path = temp / CHECKSUMS_NAME
        extracted = temp / "extracted"

        say("Downloading update...")
        download(zip_url, archive_path)
        download(checksum_url, checksum_path)

        expected = get_expected_hash(
            checksum_path.read_text(encoding="utf-8"),
            RELEASE_ZIP_NAME,
        )
        actual = sha256_file(archive_path)
        if actual != expected:
            raise RuntimeError("SHA-256 verification failed. The update was not installed.")

        say("Verifying package contents...")
        safe_extract(archive_path, extracted)

        required = [
            extracted / BOT_EXE_NAME,
            extracted / "templates",
            extracted / "_internal",
        ]
        missing = [str(path.name) for path in required if not path.exists()]
        if missing:
            raise RuntimeError(
                "Release package is incomplete. Missing: " + ", ".join(missing)
            )

        (extracted / "version.txt").write_text(release_version, encoding="utf-8")

        APP_DIR.mkdir(parents=True, exist_ok=True)
        backup = ROOT_DIR / ".blazing_bot_update_backup"
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir()

        managed = [BOT_EXE_NAME, "_internal", "templates", "version.txt"]
        backed_up = []
        try:
            for name in managed:
                current = APP_DIR / name
                if current.exists():
                    target = backup / name
                    if current.is_dir():
                        shutil.copytree(current, target)
                    else:
                        shutil.copy2(current, target)
                    backed_up.append(name)

            for name in managed:
                current = APP_DIR / name
                if current.is_dir():
                    shutil.rmtree(current)
                elif current.exists():
                    current.unlink()

            for name in managed:
                incoming = extracted / name
                target = APP_DIR / name
                if incoming.is_dir():
                    shutil.copytree(incoming, target)
                elif incoming.exists():
                    shutil.copy2(incoming, target)

        except Exception:
            # Roll back managed files; user data is never part of this operation.
            for name in managed:
                current = APP_DIR / name
                if current.is_dir():
                    shutil.rmtree(current)
                elif current.exists():
                    current.unlink()
            for name in backed_up:
                old = backup / name
                target = APP_DIR / name
                if old.is_dir():
                    shutil.copytree(old, target)
                elif old.exists():
                    shutil.copy2(old, target)
            raise
        finally:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)

    say(f"Successfully installed version {release_version}.")

def launch_bot() -> None:
    if not BOT_EXE.exists():
        raise RuntimeError(
            "The bot is not installed and no update could be downloaded. "
            "Check your GitHub repository settings and internet connection."
        )
    say("Starting Naruto Blazing Bot...")
    subprocess.Popen([str(BOT_EXE)], cwd=str(APP_DIR))

def main() -> int:
    if GITHUB_OWNER == "YOUR_GITHUB_USERNAME" or GITHUB_REPO == "YOUR_REPOSITORY":
        say("GitHub repository is not configured in launcher.py.")
        if BOT_EXE.exists():
            say("Starting the locally installed bot without checking for updates.")
            launch_bot()
            return 0
        say("Configure GITHUB_OWNER and GITHUB_REPO before first installation.")
        input("Press Enter to close...")
        return 1

    try:
        say("Checking for updates...")
        release = github_json(GITHUB_API)
        latest_version = release.get("tag_name", "").lstrip("vV")
        if not latest_version or version_tuple(latest_version) == (0, 0, 0):
            raise RuntimeError("Latest GitHub release has an invalid version tag.")

        installed_version = read_installed_version()
        say(f"Installed version: {installed_version}")
        say(f"Latest version:    {latest_version}")

        if not BOT_EXE.exists():
            say("No existing installation found. Installing the latest version...")
            install_release(release, latest_version)
        elif version_tuple(latest_version) > version_tuple(installed_version):
            answer = input("An update is available. Install it now? (Y/n): ").strip().lower()
            if answer in ("", "y", "yes"):
                install_release(release, latest_version)
            else:
                say("Skipping update.")
        else:
            say("You already have the latest version.")

    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        say(f"Could not check GitHub for updates: {exc}")
        if not BOT_EXE.exists():
            input("No installed bot is available. Press Enter to close...")
            return 1
        say("Continuing with the currently installed version.")
    except Exception as exc:
        say(f"Update check/install failed: {exc}")
        if not BOT_EXE.exists():
            input("The bot is not installed. Press Enter to close...")
            return 1
        answer = input("Start the existing installed version anyway? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            return 1

    try:
        launch_bot()
        return 0
    except Exception as exc:
        say(f"Could not start the bot: {exc}")
        input("Press Enter to close...")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
