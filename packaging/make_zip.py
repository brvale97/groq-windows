"""Zip the folder build with standard forward-slash entry names.

Windows PowerShell 5.1's Compress-Archive writes backslashes, which other zip
tools and non-Windows checks treat as part of the file name.
Usage: python packaging/make_zip.py <folder> <zip>
"""
import sys
import zipfile
from pathlib import Path


def main(folder: Path, target: Path) -> int:
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(folder).as_posix())
    print(f"{len(files)} files written to {target}")
    return 0 if files else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
