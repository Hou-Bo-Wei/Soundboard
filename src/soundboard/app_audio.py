"""混音器：把指定程式（Chrome、Spotify…）的聲音即時送進虛擬麥克風給朋友聽。

用 :class:`ProcessCapture`（Windows 程式音訊迴送）只擷取該程式的聲音：程式照常從你的喇叭播放，
同一份聲音另外複製一份混進虛擬麥克風，所以你聽到的不受影響。

* 每個程式的每個「最上層程式」（PID）各有一個 :class:`LiveSource`，音量與靜音以程式為單位一起套用。
* 背景監看執行緒每 2 秒檢查程式是否啟動／關閉，程式重開後自動接上（例如 Chrome 關掉再開）。
* 不能加入 Discord：朋友的聲音會被送回給朋友，變成回音。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from soundboard.i18n import tr
from soundboard.mixer import LiveSource
from soundboard.process_capture import CaptureError, ProcessCapture, list_processes, root_pids, supported

logger = logging.getLogger(__name__)

WATCH_INTERVAL = 2.0
# 送回語音軟體會造成回音的程式（小寫）
BLOCKED_APPS: frozenset[str] = frozenset({"discord.exe", "discordptb.exe", "discordcanary.exe"})


def app_key(exe: str) -> str:
    """程式在設定檔裡的 key（不分大小寫）。"""
    return exe.lower()


def is_blocked(exe: str) -> bool:
    """這個程式是否不能加入混音器（會造成回音）。"""
    return app_key(exe) in BLOCKED_APPS


@dataclass
class _Route:
    """一個正在擷取的程式實例。"""

    app: str
    pid: int
    source: LiveSource
    capture: ProcessCapture | None = None
    error: str | None = None


@dataclass(frozen=True)
class RouteStatus:
    """介面顯示用的程式狀態。

    Attributes:
        state (str): ``sending``（已接上）／``waiting``（程式沒開）／``error``／``unsupported``。
        detail (str): 補充說明（錯誤訊息）。
    """

    state: str
    detail: str = ""


class AppAudioRouter:
    """管理要送給朋友的程式聲音。

    Args:
        publish (Callable[[dict[str, LiveSource]], None]): 把目前所有來源交給音訊引擎（整個換掉）。
        capture_factory (Callable | None): 建立擷取物件的函式（測試時可替換）。
        process_lister (Callable | None): 列出程式的函式（測試時可替換）。
        is_supported (Callable[[], bool] | None): 系統是否支援（測試時可替換）。
    """

    def __init__(self, publish: Callable[[dict[str, LiveSource]], None],
                 capture_factory: Callable[..., ProcessCapture] | None = None,
                 process_lister: Callable[[], dict[int, tuple[str, int]]] | None = None,
                 is_supported: Callable[[], bool] | None = None) -> None:
        self._publish = publish
        self._factory = capture_factory or ProcessCapture
        self._lister = process_lister or list_processes
        self._supported = is_supported or supported
        self.apps: list[str] = []
        self._gains: dict[str, float] = {}
        self._muted: set[str] = set()
        self._routes: list[_Route] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        # 加入程式時叫醒監看執行緒立刻接上；接上要等系統回應（最多數秒），不能在介面執行緒做
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 設定
    @property
    def supported(self) -> bool:
        """系統是否支援只擷取特定程式的聲音（Windows 10 2004 以上）。"""
        return self._supported()

    def configure(self, apps: list[str], gains: dict[str, float], muted: set[str]) -> None:
        """設定要送出的程式與各自的音量；沒有任何程式時停止監看。

        Args:
            apps (list[str]): 執行檔名稱。
            gains (dict[str, float]): :func:`app_key` → 音量倍率（沒有的視為 1.0）。
            muted (set[str]): 靜音的 :func:`app_key`。
        """
        self.apps = [a for a in apps if not is_blocked(a)]
        self._gains = dict(gains)
        self._muted = set(muted)
        wanted = {app_key(a) for a in self.apps}
        with self._lock:
            removed = [r for r in self._routes if app_key(r.app) not in wanted]
            self._routes = [r for r in self._routes if app_key(r.app) in wanted]
            for route in self._routes:
                self._apply(route)
        for route in removed:
            if route.capture is not None:
                route.capture.stop()
        self._publish_routes()
        if self.apps and self.supported:
            self.start()
            self._kick.set()
        else:
            self.stop()

    def set_gain(self, app: str, gain: float) -> None:
        """即時調整某個程式的音量（拖曳推桿時呼叫，不重新連線）。"""
        self._gains[app_key(app)] = gain
        self._apply_all()

    def set_muted(self, app: str, muted: bool) -> None:
        """即時切換某個程式的靜音。"""
        (self._muted.add if muted else self._muted.discard)(app_key(app))
        self._apply_all()

    def _apply_all(self) -> None:
        with self._lock:
            for route in self._routes:
                self._apply(route)

    def _apply(self, route: _Route) -> None:
        key = app_key(route.app)
        route.source.gain = self._gains.get(key, 1.0)
        route.source.muted = key in self._muted

    # ------------------------------------------------------------------ 生命週期
    @property
    def running(self) -> bool:
        """監看執行緒是否在執行。"""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """開始監看（已在執行時不做事）。"""
        if self.running:
            return
        self._stop.clear()
        self._kick.clear()
        self._thread = threading.Thread(target=self._watch_loop, name="AppAudioWatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止所有擷取。"""
        self._stop.set()
        self._kick.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._thread = None
        with self._lock:
            routes, self._routes = self._routes, []
        for route in routes:
            if route.capture is not None:
                route.capture.stop()
        self._publish_routes()

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - 監看失敗不能讓整個混音器停掉，下次再試
                logger.exception("混音器：檢查程式時發生錯誤")
            self._kick.wait(WATCH_INTERVAL)
            self._kick.clear()

    def refresh(self) -> None:
        """接上新啟動的程式、移除已關閉的程式。"""
        if not self.apps or not self.supported:
            return
        procs = self._lister()
        changed = False
        with self._lock:
            keep, ended = [], []
            for route in self._routes:
                # 比對執行檔名稱：程式關掉後 PID 可能被別的程式拿去用
                alive = app_key(procs.get(route.pid, ("", 0))[0]) == app_key(route.app)
                if alive and route.capture is not None and route.capture.running:
                    keep.append(route)
                elif alive and route.error:
                    keep.append(route)      # 失敗的程式在它還開著時不重試，避免每 2 秒寫一次錯誤
                else:
                    # 程式關掉後 Windows 的擷取不會自己結束（只是不再有聲音），要主動停掉
                    ended.append(route)
                    changed = True
            self._routes = keep
        for route in ended:
            if route.capture is not None:
                route.capture.stop()
        with self._lock:
            known = {r.pid for r in self._routes}
        for app in list(self.apps):
            for pid in root_pids(app, procs):
                if pid in known or self._stop.is_set():
                    continue
                self._attach(app, pid)
                changed = True
        if changed:
            self._publish_routes()

    def _attach(self, app: str, pid: int) -> None:
        route = _Route(app, pid, LiveSource())
        self._apply(route)
        try:
            capture = self._factory(pid, route.source.write_pcm)
            capture.start()
        except CaptureError as exc:
            route.error = str(exc)
            logger.warning("混音器：無法擷取 %s（PID %d）：%s", app, pid, exc)
        else:
            route.capture = capture
            logger.info("混音器：開始把 %s（PID %d）的聲音送給朋友", app, pid)
        with self._lock:
            if app_key(app) in {app_key(a) for a in self.apps}:
                self._routes.append(route)
            elif route.capture is not None:
                route.capture.stop()

    def _publish_routes(self) -> None:
        with self._lock:
            sources = {f"{app_key(r.app)}:{r.pid}": r.source for r in self._routes if r.capture is not None}
        self._publish(sources)

    # ------------------------------------------------------------------ 狀態
    def status(self, app: str) -> RouteStatus:
        """某個程式目前的狀態。"""
        if not self.supported:
            return RouteStatus("unsupported", tr("需要 Windows 10 2004 或以上版本"))
        key = app_key(app)
        with self._lock:
            routes = [r for r in self._routes if app_key(r.app) == key]
        if any(r.capture is not None and r.capture.running for r in routes):
            return RouteStatus("sending")
        errors = [r.error for r in routes if r.error]
        if errors:
            return RouteStatus("error", errors[0])
        return RouteStatus("waiting")

    def level(self, app: str, now: float | None = None) -> float:
        """某個程式目前的音量表數值（已套用音量）。"""
        key = app_key(app)
        now = time.monotonic() if now is None else now
        with self._lock:
            routes = [r for r in self._routes if app_key(r.app) == key]
        return max((r.source.level(now) for r in routes), default=0.0)
