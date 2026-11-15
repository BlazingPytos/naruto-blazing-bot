"""
Create the GitHub release ZIP and SHA256SUMS.txt.
Usage: py make_release.py 2.5.0
Expected input: dist/NarutoBlazingBot/ from the PyInstaller build.
"""
from pathlib import Path
import hashlib
import shutil
import sys
import zipfile

if len(sys.argv) != 2:
    raise SystemExit("Usage: py make_release.py VERSION   (example: 2.5.0)")

version = sys.argv[1].lstrip("vV")
if not version or any(not part.isdigit() for part in version.split(".")):
    raise SystemExit("Version must be numeric, e.g. 2.5.0")

root = Path(__file__).resolve().parent
app_dir = root.parent / "bot_source" / "dist" / "NarutoBlazingBot"
if not (app_dir / "NarutoBlazingBot.exe").exists():
    raise SystemExit(f"Missing {app_dir / 'NarutoBlazingBot.exe'}")
if not (app_dir / "templates").is_dir():
    raise SystemExit(f"Missing templates folder in {app_dir}")
if not (app_dir / "_internal").is_dir():
    raise SystemExit(f"Missing PyInstaller _internal folder in {app_dir}")

(app_dir / "version.txt").write_text(version, encoding="utf-8")
release_dir = root / "release"
release_dir.mkdir(exist_ok=True)
zip_name = "NarutoBlazingBot-win64.zip"
zip_path = release_dir / zip_name

if zip_path.exists():
    zip_path.unlink()

with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in app_dir.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(app_dir))

digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
(release_dir / "SHA256SUMS.txt").write_text(
    f"{digest}  {zip_name}\n", encoding="utf-8"
)
print(f"Created: {zip_path}")
print(f"Created: {release_dir / 'SHA256SUMS.txt'}")
print(f"SHA-256: {digest}")
