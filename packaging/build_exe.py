"""把音效版打包成單一 Windows 執行檔，並整理成可以直接分享給朋友的資料夾。

用法（在專案根目錄）::

    pip install -e ".[build]"
    python packaging/build_exe.py

輸出：
    release/音效版/Soundboard.exe     單一執行檔（朋友不需要安裝 Python）
    release/音效版/使用說明.txt        給朋友看的安裝與設定步驟
    release/音效版.zip                 上面資料夾的壓縮檔，直接傳給朋友
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
RELEASE_DIR = ROOT / "release" / "音效版"
ICON_PATH = BUILD_DIR / "soundboard.ico"
EXE_NAME = "Soundboard"


def make_icon(path: Path) -> None:
    """用 Pillow 畫一個簡單的喇叭圖示（避免把二進位圖檔放進原始碼）。

    Args:
        path (Path): 要輸出的 .ico 路徑。

    Raises:
        ImportError: 沒有安裝 Pillow 時拋出。
    """
    from PIL import Image, ImageDraw

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 8, size - 8, size - 8), radius=56, fill=(88, 101, 242, 255))
    white = (255, 255, 255, 255)
    # 喇叭本體：方塊＋梯形
    draw.rectangle((52, 100, 92, 156), fill=white)
    draw.polygon([(92, 100), (140, 60), (140, 196), (92, 156)], fill=white)
    # 聲波：兩道圓弧
    for r in (40, 72):
        box = (140 - r + 12, 128 - r, 140 + r + 12, 128 + r)
        draw.arc(box, start=-45, end=45, fill=white, width=14)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def run_pyinstaller() -> Path:
    """執行 PyInstaller 產生單一執行檔。

    Returns:
        Path: 產生的 .exe 路徑。

    Raises:
        subprocess.CalledProcessError: PyInstaller 失敗時拋出。
    """
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",            # 單一檔案，朋友只要拿到一個 exe
        "--windowed",           # 不顯示黑色主控台視窗
        "--name", EXE_NAME,
        "--icon", str(ICON_PATH),
        # 圖示也一併打包，讓視窗左上角與工作列顯示同一個圖示
        "--add-data", f"{ICON_PATH};.",
        "--paths", str(ROOT / "src"),
        # 麥克風降噪用的 RNNoise DLL 與授權檔，放到 soundboard/_vendor（denoise.py 由 sys._MEIPASS 找）
        "--add-data", f"{ROOT / 'src' / 'soundboard' / '_vendor'};soundboard/_vendor",
        # 介面翻譯檔（英、日、韓），放到 soundboard/locales（i18n.py 由 sys._MEIPASS 找）
        "--add-data", f"{ROOT / 'src' / 'soundboard' / 'locales'};soundboard/locales",
        # sounddevice / soundfile 的 DLL（PortAudio、libsndfile）由 pyinstaller-hooks-contrib 自動收集
        "--collect-data", "_sounddevice_data",
        "--collect-data", "_soundfile_data",
        # 貼圖縮圖用的 PIL.ImageTk 是在函式內才匯入，明確列出以免漏打包
        "--hidden-import", "PIL.ImageTk",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR / "pyinstaller"),
        "--specpath", str(BUILD_DIR),
        str(PACKAGING / "launcher.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    return DIST_DIR / f"{EXE_NAME}.exe"


def _stage_release(exe: Path, stage: Path) -> None:
    """在 stage 資料夾組出要分享的內容（exe、說明、授權、空的音效資料夾）。"""
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "音效").mkdir(parents=True)
    shutil.copy2(exe, stage / exe.name)
    shutil.copy2(PACKAGING / "使用說明.txt", stage / "使用說明.txt")
    # exe 內含 RNNoise（降噪），依授權需附上授權說明
    licenses = stage / "第三方授權"
    licenses.mkdir()
    vendor = ROOT / "src" / "soundboard" / "_vendor"
    for name in ("README.txt", "LICENSE-pyrnnoise.txt"):
        shutil.copy2(vendor / name, licenses / name)


def _update_release_dir(stage: Path) -> bool:
    """把暫存區的內容更新到 release 資料夾。

    不整個刪除 release 資料夾：使用者可能把自己的音效放在裡面的「音效」資料夾，
    也可能正在執行裡面的 Soundboard.exe（檔案被鎖住）。

    Returns:
        bool: 全部更新成功為 True；exe 正在執行而無法覆蓋時為 False。
    """
    (RELEASE_DIR / "音效").mkdir(parents=True, exist_ok=True)
    ok = True
    for item in stage.iterdir():
        if item.name == "音效":
            continue
        target = RELEASE_DIR / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        except PermissionError:
            print(f"⚠ 無法覆蓋 {target}（程式可能正在執行），請關閉後重新打包，或直接使用 zip 檔")
            ok = False
    return ok


def make_release(exe: Path) -> tuple[Path, bool]:
    """整理成分享用的資料夾並壓縮成 zip。

    先在 build 資料夾組好並壓縮，所以就算 release 資料夾裡的 exe 正在執行，zip 也一定會更新。

    Args:
        exe (Path): PyInstaller 產生的執行檔。

    Returns:
        tuple[Path, bool]: (zip 檔路徑, release 資料夾是否完整更新)。
    """
    stage = BUILD_DIR / "release" / RELEASE_DIR.name
    _stage_release(exe, stage)
    archive = shutil.make_archive(str(BUILD_DIR / "release" / RELEASE_DIR.name), "zip",
                                  root_dir=stage.parent, base_dir=stage.name)
    final_zip = RELEASE_DIR.with_suffix(".zip")
    final_zip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, final_zip)
    return final_zip, _update_release_dir(stage)


def main() -> int:
    """建置流程主函式。

    Returns:
        int: 結束代碼。
    """
    # 輸出被導向檔案時，Windows 主控台編碼（cp950）印不出 ⚠ 等符號，會讓打包在最後一步當掉
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    make_icon(ICON_PATH)
    exe = run_pyinstaller()
    archive, folder_ok = make_release(exe)
    size_mb = exe.stat().st_size / 1024 / 1024
    folder_note = "" if folder_ok else "（exe 正在執行，未覆蓋）"
    print(f"\n完成！\n  執行檔：{exe}（{size_mb:.1f} MB）\n  分享資料夾：{RELEASE_DIR}{folder_note}\n  壓縮檔：{archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
