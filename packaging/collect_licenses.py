"""Copy the license files of the bundled runtime packages into the build.

Run with the build interpreter: ``python packaging/collect_licenses.py <target>``.
Starts from requirements.txt and follows each package's runtime dependencies.
"""
from __future__ import annotations

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSE_HINTS = ("license", "licence", "copying", "notice", "authors")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(line: str) -> str | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    if ";" in line and "extra ==" in line:
        return None  # Optional extras are not bundled.
    match = re.match(r"[A-Za-z0-9_.-]+", line)
    return canonical(match.group(0)) if match else None


def runtime_distributions() -> list[metadata.Distribution]:
    installed = {canonical(dist.metadata["Name"]): dist for dist in metadata.distributions()}
    pending = [name for name in map(requirement_name, (ROOT / "requirements.txt").read_text().splitlines()) if name]
    seen: dict[str, metadata.Distribution] = {}
    while pending:
        name = pending.pop()
        if name in seen or name not in installed:
            continue
        seen[name] = installed[name]
        for requirement in installed[name].requires or ():
            dependency = requirement_name(requirement)
            if dependency:
                pending.append(dependency)
    return sorted(seen.values(), key=lambda dist: canonical(dist.metadata["Name"]))


def main(target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for distribution in runtime_distributions():
        name = distribution.metadata["Name"]
        files = [
            entry for entry in distribution.files or ()
            if any(hint in entry.name.lower() for hint in LICENSE_HINTS) and ".dist-info" in str(entry)
        ]
        folder = target / f"{name}-{distribution.version}"
        for entry in files:
            source = Path(entry.locate())
            if source.is_file():
                folder.mkdir(exist_ok=True)
                shutil.copyfile(source, folder / source.name)
                copied += 1
        if not files:
            folder.mkdir(exist_ok=True)
            (folder / "LICENSE-INFO.txt").write_text(
                f"{name} {distribution.version}\nLicense: {distribution.metadata.get('License-Expression') or distribution.metadata.get('License', 'see project page')}\n",
                encoding="utf-8",
            )
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copyfile(python_license, target / "Python-LICENSE.txt")
        copied += 1
    print(f"{copied} license files copied to {target}")
    return 0 if copied else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
