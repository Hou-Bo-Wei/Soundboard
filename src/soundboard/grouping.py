"""音效分組：把音效清單依使用者設定的群組切成要顯示的區塊。"""

from __future__ import annotations

from dataclasses import dataclass

from soundboard.i18n import N_, tr
from soundboard.models import AppSettings, SoundItem

# 只標記不翻譯：模組載入時還沒決定介面語言，顯示時（Section.title）才 tr
UNGROUPED_LABEL = N_("未分組")


@dataclass
class Section:
    """首頁中的一個區塊（一個群組或「未分組」）。

    Attributes:
        name (str | None): 群組名稱；None 表示未分組。
        items (list[SoundItem]): 這個區塊要顯示的音效（已套用搜尋）。
        total (int): 群組內實際存在的音效總數（未套用搜尋）。
        collapsed (bool): 是否收合（收合時不顯示按鈕）。
    """

    name: str | None
    items: list[SoundItem]
    total: int
    collapsed: bool

    @property
    def title(self) -> str:
        """區塊標題文字（群組名稱是使用者資料不翻譯，只有「未分組」依介面語言顯示）。"""
        return self.name if self.name is not None else tr(UNGROUPED_LABEL)


def matches(item: SoundItem, query: str) -> bool:
    """音效是否符合搜尋字串（不分大小寫，比對名稱與檔案路徑）。

    Args:
        item (SoundItem): 音效。
        query (str): 已 casefold 的搜尋字串，空字串表示全部符合。

    Returns:
        bool: 符合時為 True。
    """
    return not query or query in item.display_name.casefold() or query in item.key.casefold()


def build_sections(items: list[SoundItem], settings: AppSettings, query: str = "") -> list[Section]:
    """依群組設定把音效切成區塊。

    沒有任何群組時只回傳一個「未分組」區塊，畫面就跟沒有分組功能時一樣。
    搜尋時會忽略收合狀態並隱藏沒有符合項目的區塊，避免搜得到卻看不到。

    Args:
        items (list[SoundItem]): 所有音效（已依名稱排序）。
        settings (AppSettings): 使用者設定（群組、收合狀態）。
        query (str): 搜尋字串。

    Returns:
        list[Section]: 依群組順序排列的區塊，「未分組」永遠在最後。
    """
    query = query.strip().casefold()
    by_key = {item.key: item for item in items}
    grouped: set[str] = set()
    sections: list[Section] = []
    for name, keys in settings.sound_groups.items():
        # 群組內保留使用者加入的順序；找不到檔案的 key 先保留在設定裡（檔案可能只是暫時移走）
        members = [by_key[k] for k in keys if k in by_key and k not in grouped]
        grouped.update(item.key for item in members)
        shown = [item for item in members if matches(item, query)]
        if query and not shown:
            continue
        collapsed = name in settings.collapsed_groups and not query
        sections.append(Section(name, shown, len(members), collapsed))
    rest = [item for item in items if item.key not in grouped]
    shown_rest = [item for item in rest if matches(item, query)]
    if shown_rest or (not query and (rest or not sections)):
        collapsed = "" in settings.collapsed_groups and not query and bool(sections)
        sections.append(Section(None, shown_rest, len(rest), collapsed))
    return sections
