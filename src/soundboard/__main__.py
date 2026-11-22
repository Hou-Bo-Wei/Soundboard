"""程式進入點：``python -m soundboard``。"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import sys
import time
from pathlib import Path

from soundboard import devices
from soundboard.config import is_frozen, load_settings, log_path, settings_path
from soundboard.autostart import AUTOSTART_FLAG, RESTART_FLAG
from soundboard.decoder import find_ffmpeg, supported_extensions
from soundboard.i18n import set_language
from soundboard.tray import SingleInstance, instance_id, show_running_instance

# 已有一個音效版在執行、但它剛啟動還沒建好系統匣圖示時，最多等這麼久再放棄
SHOW_EXISTING_TIMEOUT = 10.0
# 重新啟動（切換語言）時，最多等舊的程式結束這麼久
RESTART_TIMEOUT = 15.0


def print_devices(host_api: str) -> None:
    """在主控台列出音訊裝置，方便除錯。

    Args:
        host_api (str): 音訊 API 名稱。
    """
    print(f"音訊 API：{', '.join(devices.list_host_apis())}")
    print(f"\n[{host_api}] 裝置：")
    devs = devices.list_devices(host_api)
    for dev in devs:
        kind = "錄音" if dev.is_input else "播放"
        print(f"  #{dev.index:<3} {kind}  {dev.name}")
    cable = devices.find_virtual_cable(devs)
    print(f"\n自動偵測到的虛擬麥克風：{cable.name if cable else '（無，請安裝 VB-Audio Virtual Cable）'}")
    print(f"ffmpeg：{find_ffmpeg() or '（未找到）'}")
    print(f"支援格式：{' '.join(sorted(supported_extensions()))}")
    print(f"設定檔：{settings_path()}")


def setup_logging(verbose: bool) -> None:
    """設定記錄輸出。

    以 pythonw（sys.stderr 為 None）或打包後的 exe（沒有主控台可看）執行時，
    改寫入檔案，朋友回報問題時才有紀錄可看。

    Args:
        verbose (bool): 是否輸出除錯等級訊息。
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if sys.stderr is not None and not is_frozen():
        logging.basicConfig(level=level, format=fmt)
        return
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 每次啟動覆寫，避免紀錄檔無限長大；但先把上一次的留成 .prev，閃退後重開才看得到閃退前發生什麼事
        if path.exists():
            path.replace(path.with_suffix(".prev.log"))
        logging.basicConfig(level=level, format=fmt, filename=path, filemode="w", encoding="utf-8")
    except OSError:
        logging.basicConfig(level=level, handlers=[logging.NullHandler()])
        return
    # 原生程式庫（音訊、編碼器）崩潰時 Python 來不及記錄例外；faulthandler 會把當下的堆疊寫進紀錄檔
    for handler in logging.getLogger().handlers:
        stream = getattr(handler, "stream", None)
        if stream is not None:
            faulthandler.enable(stream)
            break


def parse_args(argv: list[str]) -> argparse.Namespace:
    """解析命令列參數。

    Args:
        argv (list[str]): 參數（不含程式名稱）。

    Returns:
        argparse.Namespace: 解析結果。
    """
    parser = argparse.ArgumentParser(prog="soundboard", description="Discord 風格音效版")
    parser.add_argument("folder", nargs="?", help="音效資料夾（會記住，下次不用再指定）")
    parser.add_argument("--list-devices", action="store_true", help="列出音訊裝置後結束")
    parser.add_argument("--mic-test", action="store_true", help="啟動後直接開啟麥克風測試視窗")
    parser.add_argument("-v", "--verbose", action="store_true", help="顯示除錯訊息")
    parser.add_argument(AUTOSTART_FLAG, action="store_true",
                        help="開機自動啟動時使用：依設定只在系統匣背景執行、不顯示主視窗")
    parser.add_argument(RESTART_FLAG, action="store_true",
                        help="程式內部使用：等目前執行中的音效版結束後再啟動（切換介面語言時）")
    return parser.parse_args(argv)


def show_existing(ident: str) -> bool:
    """請已在執行的音效版顯示主視窗（它可能剛啟動，等一下再試）。

    Args:
        ident (str): 執行個體代號。

    Returns:
        bool: 通知成功時為 True。
    """
    deadline = time.monotonic() + SHOW_EXISTING_TIMEOUT
    while time.monotonic() < deadline:
        if show_running_instance(ident):
            return True
        time.sleep(0.2)
    return False


def acquire_instance(instance: SingleInstance, wait: bool) -> bool:
    """取得單一執行個體的鎖；重新啟動時等舊的程式結束。

    Args:
        instance (SingleInstance): 執行個體鎖。
        wait (bool): 拿不到時是否持續重試（``--restart``）。

    Returns:
        bool: 取得時為 True。
    """
    deadline = time.monotonic() + (RESTART_TIMEOUT if wait else 0.0)
    while True:
        if instance.acquire():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def main(argv: list[str] | None = None) -> int:
    """程式主函式。

    Args:
        argv (list[str] | None): 命令列參數，預設讀 ``sys.argv``。

    Returns:
        int: 結束代碼。
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    instance: SingleInstance | None = None
    if not args.list_devices:
        # 在設定紀錄檔之前檢查：第二個執行個體不能把正在執行那個的紀錄檔換成新的
        instance = SingleInstance(instance_id(settings_path()))
        if not acquire_instance(instance, wait=args.restart):
            if not args.autostart:
                show_existing(instance.ident)
            return 0
    setup_logging(args.verbose)
    logging.getLogger(__name__).info("音效版啟動，設定檔：%s", settings_path())
    settings = load_settings()
    if args.folder:
        settings.sound_dir = str(Path(args.folder).resolve())

    if args.list_devices:
        print_devices(settings.host_api)
        return 0

    # 介面字型與文字都在匯入／建立元件時決定，所以語言要在匯入介面之前設定
    set_language(settings.language)
    from soundboard.gui import run  # 延後匯入：--list-devices 不需要載入 Tk

    run(settings, open_mic_test=args.mic_test, start_hidden=args.autostart and settings.autostart_hidden)
    del instance    # mutex 要持有到視窗關閉為止
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
