"""版本資訊。"""

from __future__ import annotations

from soundboard import __version__

APP_NAME = "Soundboard"


def display_version(version: str = __version__) -> str:
    """畫面上顯示的版本號：去掉結尾的「.0」（``0.1.0`` → ``0.1``、``1.2.3`` 不變）。

    Args:
        version (str): 完整版本號。

    Returns:
        str: 顯示用的版本號。
    """
    parts = version.split(".")
    while len(parts) > 2 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)
