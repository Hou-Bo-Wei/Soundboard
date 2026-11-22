"""資料模型。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from soundboard.i18n import tr


@dataclass(frozen=True)
class AudioDevice:
    """音訊裝置資訊。

    Attributes:
        index (int): sounddevice 的裝置編號（每次開機可能不同，所以只在執行期使用）。
        name (str): 裝置名稱，設定檔以名稱記錄裝置。
        host_api (str): 所屬的音訊 API 名稱，例如 ``Windows WASAPI``。
        max_input_channels (int): 最大輸入聲道數。
        max_output_channels (int): 最大輸出聲道數。
    """

    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int

    @property
    def is_input(self) -> bool:
        """是否為錄音（輸入）裝置。"""
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        """是否為播放（輸出）裝置。"""
        return self.max_output_channels > 0


@dataclass(frozen=True)
class Hotkey:
    """全域快捷鍵（Win32 RegisterHotKey 的參數）。

    Attributes:
        modifiers (int): 修飾鍵位元遮罩（MOD_ALT=1、MOD_CONTROL=2、MOD_SHIFT=4、MOD_WIN=8）。
        vk (int): Windows 虛擬鍵碼（Virtual-Key Code）。
    """

    modifiers: int
    vk: int

    def to_json(self) -> list[int]:
        """序列化成 JSON 陣列。

        Returns:
            list[int]: ``[modifiers, vk]``。
        """
        return [self.modifiers, self.vk]

    @classmethod
    def from_json(cls, raw: Any) -> Hotkey | None:
        """從 JSON 陣列還原，格式不對時回傳 None（容忍手動改壞的設定檔）。

        Args:
            raw (Any): 預期為 ``[modifiers, vk]``。

        Returns:
            Hotkey | None: 快捷鍵或 None。
        """
        if (isinstance(raw, list) and len(raw) == 2 and all(isinstance(v, int) for v in raw)
                and 0 < raw[1] < 256 and 0 <= raw[0] < 16):
            return cls(modifiers=raw[0], vk=raw[1])
        return None


@dataclass
class SoundItem:
    """音效版上的一個音效。

    Attributes:
        key (str): 唯一識別碼（相對於音效資料夾的路徑）。
        path (Path): 音效檔完整路徑。
        display_name (str): 顯示在按鈕上的名稱。
        volume (float): 此音效的個別音量倍率（1.0 = 原音量）。
    """

    key: str
    path: Path
    display_name: str
    volume: float = 1.0


@dataclass
class AppSettings:
    """使用者設定，會序列化成 JSON。

    Attributes:
        sound_dir (str): 音效資料夾。
        host_api (str): 使用的音訊 API。
        monitor_device (str | None): 自己聽的播放裝置名稱，``None`` 表示系統預設。
        virtual_mic_device (str | None): 送進虛擬麥克風的播放裝置名稱（例如 CABLE Input）。
        mic_input_device (str | None): 真實麥克風名稱，``None`` 表示系統預設。
        passthrough_enabled (bool): 是否把真實麥克風混進虛擬麥克風。
        monitor_volume (float): 自己聽到的音效音量。
        mic_volume (float): 朋友聽到的音效音量。
        passthrough_volume (float): 真實麥克風音量。
        allow_overlap (bool): 是否允許多個音效同時播放。
        display_names (dict[str, str]): 自訂按鈕名稱，key 為 SoundItem.key。
        sound_volumes (dict[str, float]): 個別音效音量。
        sound_hotkeys (dict[str, list[int]]): 音效的全域快捷鍵（SoundItem.key → Hotkey JSON）。
        action_hotkeys (dict[str, list[int]]): 功能快捷鍵（"stop" / "overlay" / "preview" → Hotkey JSON）。
        overlay_keys (list[str]): 放在小視窗上的音效（依加入順序）。
        overlay_visible (bool): 小視窗是否顯示。
        overlay_x (int | None): 小視窗左上角 X 座標，None 表示預設右下角。
        overlay_y (int | None): 小視窗左上角 Y 座標。
        overlay_alpha (float): 小視窗不透明度（0.3–1.0）。
        overlay_columns (int): 小視窗每列按鈕數。
        sound_groups (dict[str, list[str]]): 音效群組（群組名稱 → 音效 key，依顯示順序）；
            一個音效最多屬於一個群組。
        collapsed_groups (list[str]): 目前收合的群組名稱。
        overlay_group (str | None): 小視窗目前顯示的群組；None 表示顯示「📌 釘選」的音效。
        join_sound_key (str | None): 進入 Discord 語音時自動播放的音效 key。
        join_sound_enabled (bool): 是否啟用進入語音音效。
        join_sound_delay (float): 偵測到進入語音後延遲幾秒才播放（等 Discord 連線完成）。
        denoise_level (str): 真實麥克風降噪強度："off" / "standard" / "strong"。
        replay_enabled (bool): 是否持續錄下指定程式的聲音（即時回放）。
        replay_apps (list[str]): 要錄的程式執行檔名稱（例如 ``Discord.exe``）。
        replay_seconds (int): 保留最近幾秒的聲音。
        close_to_tray (bool): 按視窗的 ✕ 時縮到系統匣繼續在背景執行（而不是結束程式）。
        autostart_hidden (bool): 開機自動啟動時不顯示主視窗，只放在系統匣。
        tray_hint_shown (bool): 是否已提示過「程式還在背景執行」（只提示一次）。
        mixer_apps (list[str]): 混音器裡要把聲音送給朋友的程式（執行檔名稱，依顯示順序）。
        mixer_volumes (dict[str, float]): 混音器音量倍率；key 為執行檔名稱（小寫）或 ``@master``（總音量）。
        mixer_muted (list[str]): 混音器中靜音的通道：執行檔名稱（小寫）、``@master``、``@sounds``。
        language (str): 介面語言代碼（``zh_TW``／``en``／``ja``／``ko``），空字串表示跟隨系統。
    """

    sound_dir: str = ""
    host_api: str = "Windows WASAPI"
    monitor_device: str | None = None
    virtual_mic_device: str | None = None
    mic_input_device: str | None = None
    passthrough_enabled: bool = True
    monitor_volume: float = 0.6
    mic_volume: float = 0.8
    passthrough_volume: float = 1.0
    allow_overlap: bool = True
    display_names: dict[str, str] = field(default_factory=dict)
    sound_volumes: dict[str, float] = field(default_factory=dict)
    sound_hotkeys: dict[str, list[int]] = field(default_factory=dict)
    action_hotkeys: dict[str, list[int]] = field(default_factory=dict)
    overlay_keys: list[str] = field(default_factory=list)
    overlay_visible: bool = False
    overlay_x: int | None = None
    overlay_y: int | None = None
    overlay_alpha: float = 0.85
    overlay_columns: int = 2
    sound_groups: dict[str, list[str]] = field(default_factory=dict)
    collapsed_groups: list[str] = field(default_factory=list)
    overlay_group: str | None = None
    join_sound_key: str | None = None
    join_sound_enabled: bool = True
    join_sound_delay: float = 1.5
    denoise_level: str = "standard"
    # 預設關閉：持續錄音涉及隱私（例如朋友的說話聲），要使用者自己決定開啟
    replay_enabled: bool = False
    replay_apps: list[str] = field(default_factory=list)
    replay_seconds: int = 180
    # 背景常駐：快捷鍵、小視窗、進場音效在關掉主視窗後仍要能用，所以預設按 ✕ 只是縮到系統匣
    close_to_tray: bool = True
    autostart_hidden: bool = True
    tray_hint_shown: bool = False
    mixer_apps: list[str] = field(default_factory=list)
    mixer_volumes: dict[str, float] = field(default_factory=dict)
    mixer_muted: list[str] = field(default_factory=list)
    language: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppSettings:
        """從字典建立設定，忽略不認得的欄位（相容舊版設定檔）。

        Args:
            raw (dict[str, Any]): JSON 解析後的字典。

        Returns:
            AppSettings: 設定物件。

        Raises:
            TypeError: raw 不是字典時拋出。
        """
        if not isinstance(raw, dict):
            raise TypeError("settings must be a JSON object")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def rename_sound_key(self, old: str, new: str) -> None:
        """音效檔改名後，把以舊 key 記錄的設定（音量、快捷鍵、小視窗）搬到新 key。

        自訂顯示名稱直接移除：改名就是為了讓檔名本身成為顯示名稱。

        Args:
            old (str): 原本的 key（相對路徑）。
            new (str): 改名後的 key。
        """
        self.display_names.pop(old, None)
        self.display_names.pop(new, None)
        for store in (self.sound_volumes, self.sound_hotkeys):
            if old in store:
                store[new] = store.pop(old)
        # 保留在小視窗與群組中的排列順序
        self.overlay_keys = [new if k == old else k for k in self.overlay_keys]
        for name, keys in self.sound_groups.items():
            self.sound_groups[name] = [new if k == old else k for k in keys]
        if self.join_sound_key == old:
            self.join_sound_key = new

    def forget_sound_key(self, key: str) -> None:
        """音效檔被刪除後，移除所有以它的 key 記錄的設定（名稱、音量、快捷鍵、小視窗、群組、進場音效）。

        Args:
            key (str): 音效 key。
        """
        self.display_names.pop(key, None)
        self.sound_volumes.pop(key, None)
        self.sound_hotkeys.pop(key, None)
        self.overlay_keys = [k for k in self.overlay_keys if k != key]
        for name, keys in self.sound_groups.items():
            self.sound_groups[name] = [k for k in keys if k != key]
        if self.join_sound_key == key:
            self.join_sound_key = None

    # ------------------------------------------------------------------ 群組
    def group_of(self, key: str) -> str | None:
        """查詢音效所屬的群組。

        Args:
            key (str): 音效 key。

        Returns:
            str | None: 群組名稱；未分組時為 None。
        """
        return next((name for name, keys in self.sound_groups.items() if key in keys), None)

    def add_group(self, name: str) -> None:
        """新增空群組（已存在則不動）。

        Args:
            name (str): 群組名稱。
        """
        self.sound_groups.setdefault(name, [])

    def move_to_group(self, keys: list[str], group: str | None, before: str | None = None) -> None:
        """把多個音效移到某個群組（會先從原本的群組移除）。

        Args:
            keys (list[str]): 音效 key。
            group (str | None): 目標群組；None 表示移出群組（未分組）。
            before (str | None): 插在這個音效前面（拖曳排序用）；None 或找不到時加在最後。
        """
        moving = list(dict.fromkeys(keys))
        moving_set = set(moving)
        for name in self.sound_groups:
            self.sound_groups[name] = [k for k in self.sound_groups[name] if k not in moving_set]
        if group is None:
            return
        self.add_group(group)
        members = self.sound_groups[group]
        index = members.index(before) if before in members else len(members)
        members[index:index] = moving

    def rename_group(self, old: str, new: str) -> None:
        """重新命名群組，保留群組的排列位置與收合狀態。

        Args:
            old (str): 原名稱。
            new (str): 新名稱。

        Raises:
            ValueError: 新名稱已被其他群組使用時拋出。
        """
        if old == new or old not in self.sound_groups:
            return
        if new in self.sound_groups:
            raise ValueError(tr("已有名稱為「{name}」的群組", name=new))
        self.sound_groups = {(new if k == old else k): v for k, v in self.sound_groups.items()}
        self.collapsed_groups = [new if g == old else g for g in self.collapsed_groups]
        if self.overlay_group == old:
            self.overlay_group = new

    def delete_group(self, name: str) -> None:
        """刪除群組；裡面的音效回到未分組，音效檔本身不受影響。

        Args:
            name (str): 群組名稱。
        """
        self.sound_groups.pop(name, None)
        self.collapsed_groups = [g for g in self.collapsed_groups if g != name]
        if self.overlay_group == name:
            self.overlay_group = None

    def cycle_overlay_group(self, step: int) -> str | None:
        """切換小視窗顯示的群組：📌 釘選 → 群組 1 → 群組 2 → … → 📌 釘選。

        Args:
            step (int): +1 下一個、-1 上一個。

        Returns:
            str | None: 切換後的群組；None 表示「📌 釘選」。
        """
        sources: list[str | None] = [None, *self.sound_groups]
        current = self.overlay_group if self.overlay_group in self.sound_groups else None
        self.overlay_group = sources[(sources.index(current) + step) % len(sources)]
        return self.overlay_group

    def move_group(self, name: str, offset: int) -> None:
        """調整群組顯示順序。

        Args:
            name (str): 群組名稱。
            offset (int): -1 往前、+1 往後。
        """
        names = list(self.sound_groups)
        if name not in names:
            return
        i = names.index(name)
        j = max(0, min(len(names) - 1, i + offset))
        names.insert(j, names.pop(i))
        self.sound_groups = {n: self.sound_groups[n] for n in names}

    def move_group_before(self, name: str, before: str | None) -> bool:
        """把群組移到另一個群組前面（首頁拖曳群組標題時使用）。

        Args:
            name (str): 要移動的群組。
            before (str | None): 放在這個群組前面；None 表示移到最後。

        Returns:
            bool: 順序有改變時為 True。
        """
        names = list(self.sound_groups)
        if name not in names or name == before or (before is not None and before not in names):
            return False
        names.remove(name)
        names.insert(names.index(before) if before is not None else len(names), name)
        if names == list(self.sound_groups):
            return False
        self.sound_groups = {n: self.sound_groups[n] for n in names}
        return True
