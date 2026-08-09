import html
import re
from functools import lru_cache
from pathlib import Path

CHANGELOG_PATH = Path(__file__).resolve().parent.parent.parent / "CHANGELOG.md"

_INLINE_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    escaped = html.escape(text)
    return _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)


def _parse_release_header(line: str):
    m = re.match(r"^##\s+\[([^\]]+)\]\s*(.*)$", line)
    if not m:
        return None
    version, rest = m.group(1), m.group(2).strip()
    date = ""
    api_version = ""
    app_version = ""
    api_m = re.search(r"API\s+v([\w.]+)", rest)
    if api_m:
        api_version = api_m.group(1)
        rest = rest[: api_m.start()].strip(" —-")
    app_m = re.search(r"App\s+([\w.]+)", rest)
    if app_m:
        app_version = app_m.group(1)
        rest = rest[: app_m.start()].strip(" —-")
    if rest:
        date = rest
    return {"version": version, "date": date, "api_version": api_version, "app_version": app_version}


@lru_cache(maxsize=1)
def parse_changelog():
    if not CHANGELOG_PATH.exists():
        return []

    lines = CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()
    releases = []
    current_release = None
    current_track = None
    current_group = None

    for raw in lines:
        line = raw.rstrip()

        header = _parse_release_header(line)
        if header:
            current_release = {**header, "tracks": {"API": [], "App": []}}
            releases.append(current_release)
            current_track = None
            current_group = None
            continue

        if current_release is None:
            continue

        track_m = re.match(r"^###\s+(API|App)\s*$", line)
        if track_m:
            current_track = track_m.group(1)
            current_group = None
            continue

        group_m = re.match(r"^####\s+(.+)$", line)
        if group_m and current_track:
            current_group = {"label": group_m.group(1).strip(), "items": []}
            current_release["tracks"][current_track].append(current_group)
            continue

        item_m = re.match(r"^-\s+(.+)$", line)
        if item_m and current_group is not None:
            current_group["items"].append(_inline(item_m.group(1).strip()))
            continue

        no_change_m = re.match(r"^_(.+)_$", line)
        if no_change_m and current_track and not current_release["tracks"][current_track]:
            current_release["tracks"][current_track].append(
                {"label": "", "items": [], "note": no_change_m.group(1).strip()}
            )

    return releases
