"""音訊裝置列舉與虛擬麥克風偵測。"""

from __future__ import annotations

import sys

import sounddevice as sd

from soundboard.config import VIRTUAL_CABLE_KEYWORDS
from soundboard.models import AudioDevice


def list_host_apis() -> list[str]:
    """列出系統可用的音訊 API。

    Returns:
        list[str]: API 名稱，例如 ``["MME", "Windows WASAPI", ...]``。
    """
    return [api["name"] for api in sd.query_hostapis()]


def list_devices(host_api: str) -> list[AudioDevice]:
    """列出某個音訊 API 底下的所有裝置。

    同一個實體裝置會在 MME / DirectSound / WASAPI 各出現一次，
    只取一種 API 才不會讓使用者看到一堆重複名稱。

    Args:
        host_api (str): 音訊 API 名稱。

    Returns:
        list[AudioDevice]: 裝置清單；找不到該 API 時回傳空清單。
    """
    apis = sd.query_hostapis()
    api_index = next((i for i, api in enumerate(apis) if api["name"] == host_api), None)
    if api_index is None:
        return []
    result: list[AudioDevice] = []
    for index, info in enumerate(sd.query_devices()):
        if info["hostapi"] != api_index:
            continue
        result.append(
            AudioDevice(
                index=index,
                name=info["name"],
                host_api=host_api,
                max_input_channels=int(info["max_input_channels"]),
                max_output_channels=int(info["max_output_channels"]),
            )
        )
    return result


def find_by_name(devices: list[AudioDevice], name: str | None, want_output: bool) -> AudioDevice | None:
    """以名稱尋找裝置。

    Args:
        devices (list[AudioDevice]): 候選裝置。
        name (str | None): 裝置名稱，None 表示不指定。
        want_output (bool): True 找播放裝置，False 找錄音裝置。

    Returns:
        AudioDevice | None: 找到的裝置。
    """
    if not name:
        return None
    for dev in devices:
        if dev.name == name and (dev.is_output if want_output else dev.is_input):
            return dev
    return None


def is_virtual_cable(name: str) -> bool:
    """判斷裝置名稱是否為虛擬音源線。

    Args:
        name (str): 裝置名稱。

    Returns:
        bool: 是虛擬音源線時為 True。
    """
    lowered = name.lower()
    return any(keyword.lower() in lowered for keyword in VIRTUAL_CABLE_KEYWORDS)


def pick_monitor_device(outputs: list[AudioDevice], mic_name: str | None = None) -> AudioDevice | None:
    """挑一個適合「自己聽」的實體播放裝置（排除虛擬音源線）。

    裝了 VB-CABLE 後 Windows 常把 CABLE Input 設成預設播放裝置，
    此時「系統預設」會讓音效流進虛擬線而自己聽不到，所以需要另外挑一個實體裝置。
    優先選與麥克風同一個產品的裝置（例如耳麥的喇叭端），最符合使用者戴著耳機的情境。

    Args:
        outputs (list[AudioDevice]): 播放裝置清單。
        mic_name (str | None): 使用者的麥克風名稱，用來比對同一組耳麥。

    Returns:
        AudioDevice | None: 建議的裝置；沒有任何實體播放裝置時為 None。
    """
    real = [d for d in outputs if d.is_output and not is_virtual_cable(d.name)]
    if mic_name and "(" in mic_name:
        # 「麥克風 (Logitech G733 Gaming Headset)」→ 取括號內的產品名稱比對
        product = mic_name[mic_name.find("(") + 1 :].rstrip(")").strip().lower()
        for dev in real:
            if product and product in dev.name.lower():
                return dev
    return real[0] if real else None


def find_virtual_cable(devices: list[AudioDevice]) -> AudioDevice | None:
    """自動尋找虛擬音源線的播放端（例如 VB-Audio 的 CABLE Input）。

    Args:
        devices (list[AudioDevice]): 候選裝置。

    Returns:
        AudioDevice | None: 第一個符合的播放裝置，找不到則為 None。
    """
    # 依關鍵字順序找，讓「CABLE Input」優先於「CABLE In 16ch」等變體
    for keyword in VIRTUAL_CABLE_KEYWORDS:
        for dev in devices:
            if dev.is_output and keyword.lower() in dev.name.lower():
                return dev
    return None


def default_device_index(want_output: bool, host_api: str) -> int | None:
    """取得某 API 的系統預設裝置編號。

    Args:
        want_output (bool): True 取預設播放裝置，False 取預設錄音裝置。
        host_api (str): 音訊 API 名稱。

    Returns:
        int | None: 裝置編號，沒有預設裝置時為 None。
    """
    for api in sd.query_hostapis():
        if api["name"] == host_api:
            idx = api["default_output_device" if want_output else "default_input_device"]
            return idx if idx >= 0 else None
    return None


_MMDEVICES_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
_DEVICE_STATE_ACTIVE = 1


def active_endpoint_signature() -> frozenset[str] | None:
    """取得目前「已啟用」的 Windows 音訊端點集合，用來偵測裝置插拔。

    PortAudio 只在初始化時列舉一次裝置，之後插上喇叭、開啟藍牙耳機都看不到。
    Windows 會把每個端點的狀態寫在登錄檔 MMDevices 底下，讀它很便宜，
    集合一變就代表有裝置接上或拔掉，可以觸發重新列舉。

    Returns:
        frozenset[str] | None: 端點 id 集合；非 Windows 或讀取失敗時為 None。
    """
    if sys.platform != "win32":
        return None
    import winreg

    active: set[str] = set()
    try:
        for flow in ("Render", "Capture"):
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{_MMDEVICES_KEY}\{flow}") as parent:
                count = winreg.QueryInfoKey(parent)[0]
                for i in range(count):
                    guid = winreg.EnumKey(parent, i)
                    try:
                        with winreg.OpenKey(parent, guid) as sub:
                            state, _ = winreg.QueryValueEx(sub, "DeviceState")
                    except OSError:
                        continue
                    if state == _DEVICE_STATE_ACTIVE:
                        active.add(f"{flow}/{guid}")
    except OSError:
        return None
    return frozenset(active)
