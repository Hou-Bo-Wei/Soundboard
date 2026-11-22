"""PyInstaller 的進入點腳本（PyInstaller 需要一個實體 .py 檔當入口）。"""

from soundboard.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
