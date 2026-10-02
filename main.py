"""Standalone Windows stream player using the bundled libVLC runtime."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


ROOT = bundle_dir()
VLC_ROOT = ROOT / "vlc"
if not VLC_ROOT.is_dir():  # source checkout; frozen builds always use ROOT/vlc
    VLC_ROOT = ROOT / "vendor-cache" / "unpacked" / "vlc-3.0.24"
if not (VLC_ROOT / "libvlc.dll").is_file():
    raise RuntimeError("缺少 VLC 播放内核，请先运行 build.ps1。")

# Give the bundled DLL and plugins priority over any VLC installation on the host.
os.environ["PYTHON_VLC_LIB_PATH"] = str(VLC_ROOT / "libvlc.dll")
os.environ["PYTHON_VLC_MODULE_PATH"] = str(VLC_ROOT / "plugins")
os.environ["VLC_PLUGIN_PATH"] = str(VLC_ROOT / "plugins")
_dll_directory = os.add_dll_directory(str(VLC_ROOT))
vendor_dir = ROOT / "vendor"
if vendor_dir.is_dir():
    sys.path.insert(0, str(vendor_dir))

import vlc  # noqa: E402
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient, QShortcut  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSizeGrip,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


ALLOWED_SCHEMES = {"rtmp", "rtmps", "rtsp", "srt", "http", "https", "udp", "rtp"}
ASSETS = ROOT / "assets"


def parse_source(value: str) -> tuple[str, str, str]:
    """Return (kind, VLC input, safe UI label); reject unexpected URI schemes."""
    value = value.strip().strip('"')
    if not value:
        raise ValueError("请先输入流地址，或选择本地视频文件。")
    # A drive letter is not a URI scheme. Check existing local files first.
    path = Path(value).expanduser()
    if path.is_file():
        return "file", str(path.resolve()), path.name
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("支持 RTMP、RTMPS、RTSP、SRT、HTTP(S)、UDP、RTP 地址或本地文件。")
    if scheme not in {"udp", "rtp"} and not parsed.netloc:
        raise ValueError("流地址缺少服务器名称。")
    if any(ord(char) < 32 for char in value):
        raise ValueError("流地址包含无效字符。")
    # Never show a token, URL user info, or query string in status text.
    host = parsed.hostname or "网络流"
    return "network", value, f"{scheme.upper()} · {host}"


def format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 3600:02}:{(seconds // 60) % 60:02}:{seconds % 60:02}"


class Background(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.dark = False

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        base = QLinearGradient(0, 0, self.width(), self.height())
        if self.dark:
            base.setColorAt(0, QColor("#1b2e47"))
            base.setColorAt(1, QColor("#091827"))
        else:
            base.setColorAt(0, QColor("#f7faff"))
            base.setColorAt(1, QColor("#e7f0fa"))
        painter.fillRect(self.rect(), base)
        glow = QRadialGradient(QPointF(self.width() * 0.12, self.height() * 0.05),
                               max(self.width(), self.height()) * 0.7)
        glow.setColorAt(0, QColor(27, 177, 214, 22 if self.dark else 28))
        glow.setColorAt(1, QColor(27, 177, 214, 0))
        painter.fillRect(self.rect(), glow)
        painter.end()


class VLCSignals(QObject):
    playing = Signal()
    paused = Signal()
    ended = Signal()
    error = Signal()


class StabilityChart(QWidget):
    """Compact live chart of measured playback health, never synthetic data."""

    def __init__(self) -> None:
        super().__init__()
        self.samples: deque[int] = deque(maxlen=60)
        self.rates: deque[float] = deque(maxlen=60)
        self.dark = False
        self.setMinimumHeight(145)
        self.setMaximumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("绿色为播放稳定度；青色为读取速度趋势，按近 60 秒峰值缩放")

    def add_sample(self, value: int, rate: float = 0.0) -> None:
        self.samples.append(max(0, min(100, value)))
        self.rates.append(max(0.0, rate))
        self.update()

    def clear(self) -> None:
        self.samples.clear()
        self.rates.clear()
        self.update()

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = QColor("#85a4c5" if self.dark else "#8498ad")
        grid = QColor("#29425f" if self.dark else "#e0eaf3")
        rect = self.rect().adjusted(9, 12, -12, -23)
        top, bottom = rect.top(), rect.bottom()
        painter.setPen(QPen(grid, 1))
        for fraction in (0, 0.5, 1):
            y = top + (bottom - top) * fraction
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(ink)
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(9, self.height() - 6,
                         "开始" if self.samples and len(self.samples) < 60 else "60 秒前")
        painter.drawText(self.width() - 43, self.height() - 6, "现在")
        if not self.samples:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "连接后显示实时采样")
            painter.end()
            return
        values = list(self.samples)
        points = []
        slots = max(1, len(values) - 1)
        for index, value in enumerate(values):
            x = rect.right() if len(values) == 1 else rect.left() + index * rect.width() / slots
            y = bottom - (bottom - top) * value / 100
            points.append(QPointF(x, y))
        if len(self.rates) > 1 and max(self.rates) > 0:
            peak = max(self.rates)
            speed_line = QPainterPath()
            for index, rate in enumerate(self.rates):
                x = rect.left() + index * rect.width() / slots
                y = bottom - (bottom - top) * min(1.0, rate / peak) * 0.72
                point = QPointF(x, y)
                if index == 0:
                    speed_line.moveTo(point)
                else:
                    speed_line.lineTo(point)
            painter.setPen(QPen(QColor("#3fb8d3" if self.dark else "#278dac"), 1.5))
            painter.drawPath(speed_line)
        if len(points) > 1:
            line_color = QColor(
                "#56d4a8" if self.dark else "#168b70"
            ) if values[-1] >= 90 else QColor("#e4a74e" if values[-1] >= 65 else "#e16961")
            fill = QPainterPath(points[0])
            for point in points[1:]:
                fill.lineTo(point)
            fill.lineTo(QPointF(points[-1].x(), bottom))
            fill.lineTo(QPointF(points[0].x(), bottom))
            fill.closeSubpath()
            fill_color = QColor(line_color)
            fill_color.setAlpha(30 if self.dark else 35)
            painter.fillPath(fill, fill_color)
            line = QPainterPath(points[0])
            for point in points[1:]:
                line.lineTo(point)
            painter.setPen(QPen(line_color, 2.5))
            painter.drawPath(line)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#56d4a8" if self.dark else "#168b70")
                         if values[-1] >= 90 else QColor("#e4a74e" if values[-1] >= 65 else "#e16961"))
        painter.drawEllipse(points[-1], 4, 4)
        painter.end()


class TitleBar(QWidget):
    """A title bar that follows the player's visual language."""

    def __init__(self, owner: "PlayerWindow") -> None:
        super().__init__(owner)
        self.owner = owner
        self.setObjectName("titleBar")
        self.setFixedHeight(46)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 6, 0)
        layout.setSpacing(9)
        logo = QLabel()
        logo.setObjectName("appLogo")
        logo.setFixedSize(32, 32)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(QPixmap(str(ASSETS / "logo.png")).scaled(
            25, 25, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        ))
        layout.addWidget(logo)
        self.title_label = QLabel("直播播放器")
        self.title_label.setObjectName("windowTitle")
        layout.addWidget(self.title_label)
        self.caption = QLabel("/  LIVE PLAYER")
        self.caption.setObjectName("windowCaption")
        layout.addWidget(self.caption)
        layout.addStretch(1)

        self.monitor_button = QPushButton("监测")
        self.monitor_button.setObjectName("titleAction")
        self.monitor_button.setFixedSize(76, 32)
        self.monitor_button.setToolTip("显示或隐藏运行监测")
        self.monitor_button.clicked.connect(owner.toggle_monitor)
        layout.addWidget(self.monitor_button)

        self.theme_button = QPushButton("换肤")
        self.theme_button.setObjectName("titleAction")
        self.theme_button.setFixedSize(54, 32)
        self.theme_button.setToolTip("切换深色或浅色模式")
        self.theme_button.clicked.connect(owner.toggle_theme)
        layout.addWidget(self.theme_button)

        self.mini_button = QPushButton("小窗")
        self.mini_button.setObjectName("titleAction")
        self.mini_button.setFixedSize(54, 32)
        self.mini_button.setToolTip("小窗置顶播放")
        self.mini_button.clicked.connect(owner.toggle_mini)
        layout.addWidget(self.mini_button)

        self.min_button = QPushButton("─")
        self.min_button.setObjectName("titleAction")
        self.min_button.setFixedSize(42, 32)
        self.min_button.setToolTip("最小化")
        self.min_button.clicked.connect(owner.showMinimized)
        layout.addWidget(self.min_button)

        self.max_button = QPushButton("□")
        self.max_button.setObjectName("titleAction")
        self.max_button.setFixedSize(42, 32)
        self.max_button.setToolTip("最大化")
        self.max_button.clicked.connect(owner.toggle_maximize)
        layout.addWidget(self.max_button)

        self.close_button = QPushButton("×")
        self.close_button.setObjectName("titleClose")
        self.close_button.setFixedSize(42, 32)
        self.close_button.setToolTip("关闭播放器")
        self.close_button.clicked.connect(owner.close)
        layout.addWidget(self.close_button)

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        if self.owner.is_dark:
            gradient.setColorAt(0, QColor("#071a31"))
            gradient.setColorAt(1, QColor("#113457"))
        else:
            gradient.setColorAt(0, QColor("#003a8c"))
            gradient.setColorAt(1, QColor("#0050b3"))
        painter.fillRect(self.rect(), gradient)
        painter.end()

    def sync_state(self) -> None:
        self.title_label.setText("直播播放器")
        self.mini_button.setText("还原" if self.owner.is_mini else "小窗")
        self.mini_button.setToolTip("返回主窗口" if self.owner.is_mini else "小窗置顶播放")
        self.caption.setVisible(not self.owner.is_mini and self.owner.width() >= 900)
        self.max_button.setVisible(not self.owner.is_mini)
        self.monitor_button.setVisible(not self.owner.is_mini and not self.owner.is_fullscreen_video)
        self.max_button.setText("❐" if self.owner.is_window_maximized else "□")
        self.max_button.setToolTip("还原窗口" if self.owner.is_window_maximized else "最大化")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self.owner.isFullScreen():
            handle = self.owner.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self.owner.is_mini:
                self.owner.leave_mini()
            else:
                self.owner.toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class PlayerWindow(QMainWindow):
    def __init__(self, *, test_report: Path | None = None) -> None:
        super().__init__()
        self.test_report = test_report
        self.ever_playing = False
        self.last_error = ""
        self.current_kind = ""
        self.current_media = None
        self.current_source = ""
        self.drag_active = False
        self.is_dark = False
        self.is_fullscreen_video = False
        self.is_mini = False
        self._restore_geometry = None
        self._restore_maximized = False
        self._restore_max_after_fullscreen = False
        self._pre_fullscreen_geometry = None
        self._is_custom_maximized = False
        self._max_restore_geometry = None
        self._mini_geometry = None
        self.is_scrubbing = False
        self.monitor_visible = True
        self.monitor_focus = False
        self._last_stats = None
        self._last_sample_time = 0.0
        self._last_log_time = 0.0
        self._last_monitor_state = "idle"
        self._buffer_count = 0
        self._stats_unavailable_logged = False
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("直播播放器")
        self.setWindowIcon(QIcon(str(ASSETS / "player.ico")))
        self.setMinimumSize(760, 540)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1220, max(760, available.width() - 64)),
                    min(810, max(540, available.height() - 64)))
        self.setAcceptDrops(True)

        self.instance = vlc.Instance("--no-video-title-show", "--quiet", "--no-osd")
        if self.instance is None:
            raise RuntimeError("VLC 播放内核启动失败。")
        self.player = self.instance.media_player_new()
        self.signals = VLCSignals()
        events = self.player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerPlaying, self._vlc_playing)
        events.event_attach(vlc.EventType.MediaPlayerPaused, self._vlc_paused)
        events.event_attach(vlc.EventType.MediaPlayerEndReached, self._vlc_ended)
        events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._vlc_error)
        self.signals.playing.connect(self._on_playing)
        self.signals.paused.connect(self._on_paused)
        self.signals.ended.connect(self._on_ended)
        self.signals.error.connect(self._on_error)

        self._build_ui()
        self._apply_theme()
        self.player.audio_set_volume(80)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_playback)
        self.timer.start(300)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._poll_monitor)
        self.monitor_timer.start(1000)
        QShortcut(QKeySequence("Space"), self, activated=self.toggle_pause)
        QShortcut(QKeySequence("F"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self, activated=self.leave_fullscreen)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_file)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.title_bar = TitleBar(self)
        root_layout.addWidget(self.title_bar)
        self.background = Background()
        root_layout.addWidget(self.background, 1)
        outer = QVBoxLayout(self.background)
        outer.setContentsMargins(28, 18, 28, 16)
        outer.setSpacing(14)

        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(4, 0, 4, 0)
        header.setSpacing(15)
        logo = QLabel()
        logo.setPixmap(QPixmap(str(ASSETS / "logo.png")).scaled(
            62, 62, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        ))
        header.addWidget(logo)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel("直播播放器")
        title.setObjectName("brandTitle")
        subtitle = QLabel("LIVE PLAYER  /  多协议播放")
        subtitle.setObjectName("brandSubtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        header.addLayout(brand)
        header.addStretch(1)
        self.version_label = QLabel("WINDOWS 10 / 11  ·  64-BIT")
        self.version_label.setObjectName("versionLabel")
        header.addWidget(self.version_label)
        self.theme_button = QPushButton("深色模式")
        self.theme_button.setObjectName("ghostButton")
        self.theme_button.clicked.connect(self.toggle_theme)
        header.addWidget(self.theme_button)
        outer.addWidget(self.header)

        self.card = QFrame()
        self.card.setObjectName("mainCard")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(20, 18, 20, 16)
        card_layout.setSpacing(0)
        self.content_row = QWidget()
        content_layout = QHBoxLayout(self.content_row)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(18)
        self.playback_area = QWidget()
        playback_layout = QVBoxLayout(self.playback_area)
        playback_layout.setContentsMargins(0, 0, 0, 0)
        playback_layout.setSpacing(13)
        content_layout.addWidget(self.playback_area, 1)

        self.source_panel = QWidget()
        source_layout = QVBoxLayout(self.source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(12)

        entry_header = QHBoxLayout()
        section = QLabel("视频源")
        section.setObjectName("sectionTitle")
        entry_header.addWidget(section)
        entry_header.addStretch(1)
        self.privacy_label = QLabel("地址只在本次运行中使用 · 不保存密钥")
        self.privacy_label.setObjectName("hint")
        entry_header.addWidget(self.privacy_label)
        source_layout.addLayout(entry_header)

        input_row = QHBoxLayout()
        input_row.setSpacing(9)
        self.address = QLineEdit()
        self.address.setObjectName("sourceInput")
        self.address.setPlaceholderText("粘贴 rtmp://、rtsp://、srt://、https://… 或本地文件路径")
        self.address.returnPressed.connect(self.play_source)
        input_row.addWidget(self.address, 1)
        browse = QPushButton("打开文件")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self.open_file)
        input_row.addWidget(browse)
        self.play_button = QPushButton("▶  开始播放")
        self.play_button.setObjectName("primaryButton")
        self.play_button.clicked.connect(self.play_source)
        input_row.addWidget(self.play_button)
        source_layout.addLayout(input_row)

        chips = QHBoxLayout()
        chips.setSpacing(7)
        self.protocol_chips = []
        for text in ("RTMP / RTMPS", "RTSP", "SRT", "HLS / HTTP", "MP4 / FLV / MKV"):
            chip = QLabel(text)
            chip.setObjectName("protocolChip")
            chips.addWidget(chip)
            self.protocol_chips.append(chip)
        chips.addStretch(1)
        self.cache_label = QLabel("网络缓冲")
        self.cache_label.setObjectName("hint")
        chips.addWidget(self.cache_label)
        self.cache = QComboBox()
        self.cache.addItems(["低延迟 300 ms", "均衡 1000 ms", "稳定 2000 ms"])
        self.cache.setCurrentIndex(1)
        self.cache.setObjectName("cacheCombo")
        chips.addWidget(self.cache)
        source_layout.addLayout(chips)
        playback_layout.addWidget(self.source_panel)

        self.video_card = QFrame()
        self.video_card.setObjectName("videoCard")
        video_layout = QVBoxLayout(self.video_card)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)
        self.video_stack = QStackedWidget()
        self.placeholder = QWidget()
        self.placeholder.setObjectName("placeholder")
        empty_layout = QVBoxLayout(self.placeholder)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(6)
        play_mark = QLabel("▶")
        play_mark.setObjectName("playMark")
        play_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("等待视频源")
        empty_title.setObjectName("emptyTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_sub = QLabel("输入直播地址，或将视频文件拖入窗口")
        empty_sub.setObjectName("emptySubtitle")
        empty_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(play_mark)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_sub)
        self.video_stack.addWidget(self.placeholder)
        self.video_surface = QWidget()
        self.video_surface.setObjectName("videoSurface")
        self.video_surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_surface.setStyleSheet("background:#071522;")
        self.video_stack.addWidget(self.video_surface)
        video_layout.addWidget(self.video_stack)
        playback_layout.addWidget(self.video_card, 1)

        self.mini_panel = QWidget()
        self.mini_panel.setObjectName("miniPanel")
        mini_layout = QHBoxLayout(self.mini_panel)
        mini_layout.setContentsMargins(11, 5, 7, 5)
        mini_layout.setSpacing(7)
        self.mini_state = QLabel("小窗播放")
        self.mini_state.setObjectName("miniState")
        mini_layout.addWidget(self.mini_state)
        mini_layout.addStretch(1)
        self.mini_pause = QPushButton("暂停")
        self.mini_pause.setObjectName("miniButton")
        self.mini_pause.setEnabled(False)
        self.mini_pause.clicked.connect(self.toggle_pause)
        mini_layout.addWidget(self.mini_pause)
        self.mini_mute = QPushButton("静音")
        self.mini_mute.setObjectName("miniButton")
        self.mini_mute.clicked.connect(self.toggle_mute)
        mini_layout.addWidget(self.mini_mute)
        mini_restore = QPushButton("返回主窗口")
        mini_restore.setObjectName("miniButton")
        mini_restore.clicked.connect(self.leave_mini)
        mini_layout.addWidget(mini_restore)
        mini_grip = QSizeGrip(self.mini_panel)
        mini_grip.setToolTip("拖动调整小窗大小")
        mini_layout.addWidget(mini_grip)
        playback_layout.addWidget(self.mini_panel)
        self.mini_panel.hide()

        self.control_panel = QWidget()
        control_layout = QVBoxLayout(self.control_panel)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(10)
        meta = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status = QLabel("就绪，等待播放")
        self.status.setObjectName("statusText")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        meta.addWidget(self.status_dot)
        meta.addWidget(self.status)
        meta.addStretch(1)
        self.live_badge = QLabel("LIVE")
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.hide()
        meta.addWidget(self.live_badge)
        control_layout.addLayout(meta)

        progress = QHBoxLayout()
        self.elapsed = QLabel("00:00:00")
        self.elapsed.setObjectName("timeLabel")
        progress.addWidget(self.elapsed)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setEnabled(False)
        self.seek.sliderPressed.connect(self._seek_start)
        self.seek.sliderReleased.connect(self._seek_end)
        progress.addWidget(self.seek, 1)
        self.duration = QLabel("00:00:00")
        self.duration.setObjectName("timeLabel")
        progress.addWidget(self.duration)
        control_layout.addLayout(progress)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.pause_button = QPushButton("暂停")
        self.pause_button.setObjectName("secondaryButton")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        controls.addWidget(self.pause_button)
        stop = QPushButton("停止")
        stop.setObjectName("secondaryButton")
        stop.clicked.connect(self.stop)
        controls.addWidget(stop)
        self.mute_button = QPushButton("静音")
        self.mute_button.setObjectName("secondaryButton")
        self.mute_button.clicked.connect(self.toggle_mute)
        controls.addWidget(self.mute_button)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setObjectName("volumeSlider")
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(130)
        self.volume.valueChanged.connect(self.set_volume)
        controls.addWidget(self.volume)
        self.volume_value = QLabel("80%")
        self.volume_value.setObjectName("timeLabel")
        controls.addWidget(self.volume_value)
        controls.addStretch(1)
        self.fullscreen_button = QPushButton("全屏  F")
        self.fullscreen_button.setObjectName("secondaryButton")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        controls.addWidget(self.fullscreen_button)
        control_layout.addLayout(controls)
        playback_layout.addWidget(self.control_panel)
        self._build_monitor_panel()
        content_layout.addWidget(self.monitor_panel)
        card_layout.addWidget(self.content_row, 1)
        outer.addWidget(self.card, 1)

        self.footer = QWidget()
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_text = QLabel("本地播放  ·  VLC 3.0.24 内核  ·  Ctrl+O 打开文件 / 空格暂停 / F 全屏 / Esc 退出全屏")
        footer_text.setObjectName("footerText")
        footer_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_layout.addStretch(1)
        footer_layout.addWidget(footer_text)
        footer_layout.addStretch(1)
        normal_grip = QSizeGrip(self.footer)
        normal_grip.setToolTip("拖动调整窗口大小")
        footer_layout.addWidget(normal_grip)
        outer.addWidget(self.footer)
        self.title_bar.sync_state()

    def _build_monitor_panel(self) -> None:
        self.monitor_panel = QFrame()
        self.monitor_panel.setObjectName("monitorPanel")
        self.monitor_panel.setMinimumWidth(260)
        self.monitor_panel.setMaximumWidth(330)
        monitor = QVBoxLayout(self.monitor_panel)
        monitor.setContentsMargins(16, 16, 16, 14)
        monitor.setSpacing(9)

        heading = QHBoxLayout()
        monitor_title = QLabel("运行监测")
        monitor_title.setObjectName("monitorTitle")
        heading.addWidget(monitor_title)
        heading.addStretch(1)
        self.health_caption = QLabel("等待连接")
        self.health_caption.setObjectName("healthCaption")
        heading.addWidget(self.health_caption)
        monitor.addLayout(heading)
        self.monitor_hint = QLabel("连接、画面和读取数据实时更新")
        self.monitor_hint.setObjectName("monitorHint")
        monitor.addWidget(self.monitor_hint)

        score_row = QHBoxLayout()
        score_label = QLabel("播放稳定度")
        score_label.setObjectName("monitorSection")
        score_row.addWidget(score_label)
        score_row.addStretch(1)
        self.health_value = QLabel("--")
        self.health_value.setObjectName("healthValue")
        score_row.addWidget(self.health_value)
        monitor.addLayout(score_row)
        self.chart = StabilityChart()
        monitor.addWidget(self.chart, 1)
        self.monitor_explanation = QLabel("近 60 秒 · 绿色稳定度 / 青色读取趋势")
        self.monitor_explanation.setObjectName("monitorHint")
        self.monitor_explanation.setWordWrap(True)
        monitor.addWidget(self.monitor_explanation)

        self.metrics_panel = QWidget()
        metrics = QGridLayout(self.metrics_panel)
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setHorizontalSpacing(12)
        metrics.setVerticalSpacing(5)
        metric_labels = ("读取速度", "画面帧", "累计丢帧", "缓冲次数")
        metric_defaults = ("-- KiB/s", "--", "--", "0")
        metric_values = []
        for index, (label, default) in enumerate(zip(metric_labels, metric_defaults)):
            column = index % 2
            row = index // 2 * 2
            caption = QLabel(label)
            caption.setObjectName("metricCaption")
            value = QLabel(default)
            value.setObjectName("metricValue")
            metrics.addWidget(caption, row, column)
            metrics.addWidget(value, row + 1, column)
            metric_values.append(value)
        self.rate_value, self.frame_value, self.drop_value, self.buffer_value = metric_values
        monitor.addWidget(self.metrics_panel)
        self.compact_stats = QLabel("读取 -- · 丢帧 --")
        self.compact_stats.setObjectName("monitorHint")
        self.compact_stats.hide()
        monitor.addWidget(self.compact_stats)

        log_heading = QHBoxLayout()
        log_title = QLabel("连接与采样日志")
        log_title.setObjectName("monitorSection")
        log_heading.addWidget(log_title)
        log_heading.addStretch(1)
        clear_button = QPushButton("清空")
        clear_button.setObjectName("textAction")
        clear_button.clicked.connect(self._clear_log)
        log_heading.addWidget(clear_button)
        copy_button = QPushButton("复制")
        copy_button.setObjectName("textAction")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))
        log_heading.addWidget(copy_button)
        monitor.addLayout(log_heading)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(240)
        self.log_view.setMinimumHeight(122)
        monitor.addWidget(self.log_view, 2)
        self._append_log("系统", "等待输入直播地址或选择本地文件")

    def _append_log(self, category: str, message: str) -> None:
        # Only sanitized labels and numerical media statistics reach this in-memory log.
        scroll = self.log_view.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum() - 4
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  [{category}] {message}")
        if at_bottom:
            scroll.setValue(scroll.maximum())

    def _clear_log(self) -> None:
        self.log_view.clear()
        self._append_log("系统", "日志已清空")

    def _style(self) -> str:
        if self.is_dark:
            fg, text, muted = "#ecf4ff", "#cdddec", "#91abc6"
            card, panel, input_bg, border = "rgba(16,29,48,235)", "#0b1728", "#152b47", "#375372"
            secondary = "#1d3656"
            chips_text = "#9bb8d3"
            monitor_bg, log_bg = "#10243b", "#0b1a2d"
            title_bg = "#092540"
        else:
            fg, text, muted = "#073a7b", "#1e3c5c", "#6b7c90"
            card, panel, input_bg, border = "rgba(255,255,255,236)", "#091a2c", "#ffffff", "#d4e2f2"
            secondary = "#f0f5fb"
            chips_text = "#5c7692"
            monitor_bg, log_bg = "#f3f7fb", "#eaf1f8"
            title_bg = "qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #003a8c,stop:1 #0050b3)"
        return f"""
            QWidget {{ font-family: 'Segoe UI','Microsoft YaHei'; color:{text}; font-size:13px; }}
            #titleBar {{ background:{title_bg}; border-bottom:1px solid #174e8d; }}
            #windowTitle {{ color:white; font-size:13px; font-weight:700; }}
            #appLogo {{ background:#eff8ff; border-radius:8px; }}
            #windowCaption {{ color:#b7daf5; font-size:10px; font-weight:700; letter-spacing:1px; }}
            #titleAction {{ color:#f2f8ff; background:transparent; border:0; border-radius:7px;
                            font-size:14px; font-weight:600; }}
            #titleAction:hover {{ background:rgba(255,255,255,40); }}
            #titleClose {{ color:#f2f8ff; background:transparent; border:0; border-radius:7px;
                           font-size:22px; font-weight:400; }}
            #titleClose:hover {{ background:#c44536; }}
            #brandTitle {{ color:{fg}; font-size:20px; font-weight:700; }}
            #brandSubtitle {{ color:#2873c0; font-size:11px; font-weight:700; letter-spacing:1px; }}
            #versionLabel,#hint,#footerText {{ color:{muted}; font-size:11px; }}
            #mainCard {{ background:{card}; border:1px solid {border}; border-radius:20px; }}
            #sectionTitle {{ color:{fg}; font-size:18px; font-weight:700; }}
            #sourceInput {{ background:{input_bg}; border:1px solid {border}; border-radius:10px;
                            padding:10px 13px; color:{text}; font-size:13px; selection-background-color:#0050b3; }}
            #sourceInput:focus {{ border:2px solid #00b8d9; }}
            #primaryButton {{ background:#0050b3; color:white; border:0; border-radius:10px;
                              padding:11px 19px; font-weight:700; }}
            #primaryButton:hover {{ background:#003a8c; }}
            #secondaryButton,#ghostButton {{ background:{secondary}; color:{fg}; border:1px solid {border};
                                            border-radius:10px; padding:9px 12px; font-weight:600; }}
            #secondaryButton:hover,#ghostButton:hover {{ border:1px solid #4d8fd6; background:#dceafa; color:#003a8c; }}
            #secondaryButton:disabled {{ color:#98a9bb; }}
            #protocolChip {{ color:{chips_text}; background:transparent; border:0;
                            padding:3px 2px; font-size:11px; font-weight:600; }}
            #cacheCombo {{ background:{input_bg}; color:{text}; border:1px solid {border};
                          border-radius:8px; padding:5px 8px; min-width:125px; }}
            #cacheCombo QAbstractItemView {{ background:{input_bg}; color:{text}; selection-background-color:#dceafa; }}
            #videoCard {{ background:{panel}; border:1px solid #173558; border-radius:13px; }}
            #monitorPanel {{ background:{monitor_bg}; border:1px solid {border}; border-radius:13px; }}
            #monitorTitle {{ color:{fg}; font-size:17px; font-weight:700; }}
            #monitorSection {{ color:{fg}; font-size:12px; font-weight:700; }}
            #monitorHint,#metricCaption {{ color:{muted}; font-size:10px; }}
            #metricValue {{ color:{text}; font-size:15px; font-weight:700; }}
            #healthValue {{ color:#168b70; font-size:21px; font-weight:700; }}
            #healthCaption {{ color:#168b70; font-size:11px; font-weight:700; }}
            #textAction {{ color:{fg}; background:transparent; border:0; padding:3px 4px; font-size:11px; }}
            #textAction:hover {{ color:#0089ac; }}
            #logView {{ background:{log_bg}; color:{text}; border:1px solid {border}; border-radius:8px;
                        padding:8px; font-family:'Microsoft YaHei UI','Segoe UI'; font-size:11px; }}
            #miniPanel {{ background:{card}; border-top:1px solid {border}; }}
            #miniState {{ color:{muted}; font-size:11px; }}
            #miniButton {{ background:{secondary}; color:{fg}; border:1px solid {border};
                           border-radius:7px; padding:5px 8px; font-size:11px; font-weight:600; }}
            #miniButton:hover {{ border:1px solid #4d8fd6; }}
            #placeholder {{ background:#0a1c31; border-radius:16px; }}
            #playMark {{ color:#63cce4; font-size:57px; }}
            #emptyTitle {{ color:#e8f4ff; font-size:21px; font-weight:700; }}
            #emptySubtitle {{ color:#8faac6; font-size:12px; }}
            #statusDot {{ color:#86a0b8; font-size:13px; }}
            #statusText {{ color:{text}; font-size:12px; }}
            #liveBadge {{ color:white; background:#c44536; border-radius:7px; padding:3px 8px;
                          font-weight:700; font-size:11px; }}
            #timeLabel {{ color:{muted}; font-size:11px; min-width:40px; }}
            QSlider::groove:horizontal {{ height:5px; border-radius:2px; background:#cddfec; }}
            QSlider::sub-page:horizontal {{ background:#00b8d9; border-radius:2px; }}
            QSlider::handle:horizontal {{ background:#0050b3; border:2px solid #ffffff;
                                         width:15px; margin:-6px 0; border-radius:8px; }}
        """

    def _apply_theme(self) -> None:
        self.background.dark = self.is_dark
        self.background.update()
        self.title_bar.update()
        self.chart.dark = self.is_dark
        self.chart.update()
        self.setStyleSheet(self._style())
        self.theme_button.setText("浅色模式" if self.is_dark else "深色模式")
        self.title_bar.theme_button.setText("浅色" if self.is_dark else "深色")

    def toggle_theme(self) -> None:
        self.is_dark = not self.is_dark
        self._apply_theme()

    def toggle_monitor(self) -> None:
        compact = self.width() < 1060 or self.height() < 640
        if compact:
            self.monitor_focus = not self.monitor_focus
        else:
            self.monitor_visible = not self.monitor_visible
        self._adapt_ui()

    def _adapt_ui(self) -> None:
        if not hasattr(self, "monitor_panel"):
            return
        width = self.width()
        monitor_compact = width < 1060 or self.height() < 640
        self.monitor_panel.setMinimumWidth(0 if monitor_compact else 260)
        self.monitor_panel.setMaximumWidth(16777215 if monitor_compact else 330)
        self.monitor_panel.layout().setSpacing(6 if monitor_compact else 9)
        self.monitor_panel.layout().setContentsMargins(*( (12, 11, 12, 10) if monitor_compact else (16, 16, 16, 14) ))
        self.monitor_hint.setVisible(not monitor_compact)
        self.monitor_explanation.setVisible(not monitor_compact)
        self.metrics_panel.setVisible(not monitor_compact)
        self.compact_stats.setVisible(monitor_compact)
        self.chart.setMinimumHeight(95 if monitor_compact else 145)
        self.chart.setMaximumHeight(115 if monitor_compact else 190)
        self.log_view.setMinimumHeight(95 if monitor_compact else 122)
        if self.is_mini or self.is_fullscreen_video:
            self.monitor_panel.hide()
            self.playback_area.show()
        elif monitor_compact:
            self.monitor_panel.setVisible(self.monitor_focus)
            self.playback_area.setVisible(not self.monitor_focus)
        else:
            self.monitor_panel.setVisible(self.monitor_visible)
            self.playback_area.show()
        self.title_bar.monitor_button.setText(
            ("播放器" if self.monitor_focus else "监测") if monitor_compact
            else ("收起监测" if self.monitor_visible else "监测")
        )
        self.title_bar.monitor_button.setVisible(not self.is_mini and not self.is_fullscreen_video)
        self.header.hide()
        self.title_bar.theme_button.setVisible(not self.is_mini and not self.is_fullscreen_video)
        self.title_bar.caption.setVisible(width >= 900 and not self.is_mini)
        self.version_label.setVisible(width >= 1080 and not self.is_mini and not self.is_fullscreen_video)
        self.privacy_label.setVisible(width >= 940 and not self.is_mini and not self.is_fullscreen_video)
        self.cache_label.setVisible(width >= 900 and not self.is_mini)
        for index, chip in enumerate(self.protocol_chips):
            chip.setVisible(width >= (800 if index < 3 else 960) and not self.is_mini)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._adapt_ui()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "title_bar"):
            self.title_bar.sync_state()

    def toggle_maximize(self) -> None:
        if self.is_mini:
            self.leave_mini()
        if self._is_custom_maximized:
            self._is_custom_maximized = False
            if self._max_restore_geometry is not None:
                self.setGeometry(self._max_restore_geometry)
        else:
            self._max_restore_geometry = self.geometry()
            self.setGeometry((self.screen() or QApplication.primaryScreen()).availableGeometry())
            self._is_custom_maximized = True
        self.title_bar.sync_state()

    @property
    def is_window_maximized(self) -> bool:
        return self._is_custom_maximized

    def _set_always_on_top(self, enabled: bool) -> None:
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.SetWindowPos(ctypes.c_void_p(int(self.winId())),
                            ctypes.c_void_p(-1 if enabled else -2),
                            0, 0, 0, 0, 0x0013)

    def toggle_mini(self) -> None:
        if self.is_mini:
            self.leave_mini()
        else:
            self.enter_mini()

    def enter_mini(self) -> None:
        if self.is_fullscreen_video:
            self.leave_fullscreen()
        self._restore_maximized = self._is_custom_maximized
        self._restore_geometry = self.geometry()
        self._is_custom_maximized = False
        self.is_mini = True
        self.setMinimumSize(350, 230)
        self.title_bar.setFixedHeight(40)
        self.header.hide()
        self.source_panel.hide()
        self.control_panel.hide()
        self.monitor_panel.hide()
        self.playback_area.show()
        self.footer.hide()
        self.mini_panel.show()
        self.background.layout().setContentsMargins(0, 0, 0, 0)
        self.card.layout().setContentsMargins(0, 0, 0, 0)
        available = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        if self._mini_geometry is not None and available.intersects(self._mini_geometry):
            self.setGeometry(self._mini_geometry)
        else:
            self.resize(480, 315)
            self.move(available.right() - self.width() - 22,
                      available.bottom() - self.height() - 22)
        self._set_always_on_top(True)
        self.title_bar.sync_state()
        self._adapt_ui()

    def leave_mini(self) -> None:
        if not self.is_mini:
            return
        self._mini_geometry = self.geometry()
        self._set_always_on_top(False)
        self.is_mini = False
        self.setMinimumSize(760, 540)
        self.title_bar.setFixedHeight(46)
        self.header.show()
        self.source_panel.show()
        self.control_panel.show()
        self.footer.show()
        self.mini_panel.hide()
        self.background.layout().setContentsMargins(28, 18, 28, 16)
        self.card.layout().setContentsMargins(22, 20, 22, 17)
        if self._restore_geometry is not None:
            self.setGeometry(self._restore_geometry)
        if self._restore_maximized:
            self._is_custom_maximized = True
        self.title_bar.sync_state()
        self._adapt_ui()

    def _set_status(self, message: str, tone: str = "idle") -> None:
        self.status.setText(message)
        self.status_dot.setStyleSheet({
            "idle": "color:#86a0b8;", "loading": "color:#d48806;",
            "playing": "color:#2eaf83;", "error": "color:#d64c43;",
        }.get(tone, "color:#86a0b8;"))

    def _reset_monitor(self) -> None:
        self.chart.clear()
        self.health_value.setText("--")
        self.health_caption.setText("正在连接")
        self.health_value.setStyleSheet("")
        self.health_caption.setStyleSheet("")
        self.rate_value.setText("-- KiB/s")
        self.frame_value.setText("--")
        self.drop_value.setText("--")
        self.buffer_value.setText("0")
        self._last_stats = None
        self._last_sample_time = 0.0
        self._last_log_time = 0.0
        self._last_monitor_state = "opening"
        self._buffer_count = 0
        self._buffer_active = False
        self._connected_current = False
        self._stats_unavailable_logged = False

    def _show_health(self, score: int, caption: str, rate: float = 0.0) -> None:
        self.chart.add_sample(score, rate)
        self.health_value.setText(f"{score}%")
        self.health_caption.setText(caption)
        color = "#168b70" if score >= 90 else "#b96b24" if score >= 65 else "#c84e47"
        self.health_value.setStyleSheet(f"color:{color};")
        self.health_caption.setStyleSheet(f"color:{color};")

    def _poll_monitor(self) -> None:
        if not self.current_source or self.current_media is None:
            return
        state = self.player.get_state()
        if state == vlc.State.Buffering and not self._buffer_active:
            self._buffer_active = True
            self._buffer_count += 1
            self.buffer_value.setText(str(self._buffer_count))
            self._append_log("连接", "正在缓冲，等待数据恢复")
        elif state != vlc.State.Buffering:
            self._buffer_active = False

        now = time.monotonic()
        stats = vlc.MediaStats()
        try:
            available = bool(self.current_media.get_stats(ctypes.byref(stats)))
        except (AttributeError, OSError):
            available = False
        previous = self._last_stats
        interval = max(0.1, now - self._last_sample_time) if self._last_sample_time else 1.0
        rate = None
        lost_delta = 0
        corrupt_delta = 0
        discontinuity_delta = 0
        displayed_delta = 0
        if available:
            self.frame_value.setText(f"{stats.displayed_pictures:,}")
            self.drop_value.setText(f"{stats.lost_pictures:,}")
            if previous is not None:
                read_delta = max(0, stats.read_bytes - previous.read_bytes)
                rate = read_delta / interval / 1024
                self.rate_value.setText(f"{rate:,.1f} KiB/s")
                self.compact_stats.setText(f"读取 {rate:,.1f} KiB/s  ·  累计丢帧 {stats.lost_pictures}")
                displayed_delta = max(0, stats.displayed_pictures - previous.displayed_pictures)
                lost_delta = max(0, stats.lost_pictures - previous.lost_pictures)
                corrupt_delta = max(0, stats.demux_corrupted - previous.demux_corrupted)
                discontinuity_delta = max(0, stats.demux_discontinuity - previous.demux_discontinuity)
            self._last_stats = stats
            self._last_sample_time = now
        elif not self._stats_unavailable_logged:
            self._append_log("采样", "VLC 暂未提供媒体统计，继续监测播放状态")
            self._stats_unavailable_logged = True

        if state == vlc.State.Buffering:
            self._show_health(35, "缓冲中")
        elif state == vlc.State.Playing:
            if available and previous is not None:
                total_frames = max(1, displayed_delta + lost_delta)
                penalty = round(lost_delta / total_frames * 500) + corrupt_delta * 12 + discontinuity_delta * 15
                score = max(0, 100 - min(100, penalty))
                caption = "稳定" if score >= 90 else "波动" if score >= 65 else "需关注"
                self._show_health(score, caption, rate or 0.0)
            else:
                self.health_value.setText("--")
                self.health_caption.setText("播放中 · 待采样")
        elif state == vlc.State.Error:
            self._show_health(0, "连接异常")
        elif state == vlc.State.Paused:
            self.health_caption.setText("已暂停")

        if available and previous is not None and now - self._last_log_time >= 2:
            rate_text = f"{rate:,.1f} KiB/s" if rate is not None else "--"
            self._append_log("采样", f"读取 {rate_text} · 新画面 {displayed_delta} 帧 · 累计丢帧 {stats.lost_pictures}")
            self._last_log_time = now

    def play_source(self) -> None:
        try:
            kind, source, label = parse_source(self.address.text())
        except ValueError as exc:
            self._set_status(str(exc), "error")
            self._append_log("错误", "视频源格式无效，请检查输入")
            return
        self.stop(reset_status=False)
        self._reset_monitor()
        self._append_log("连接", f"开始连接 {label}")
        self.current_kind = kind
        self.current_source = source
        self.last_error = ""
        self.video_stack.setCurrentWidget(self.video_surface)
        self.video_surface.winId()  # create a native HWND before binding VLC
        self.player.set_hwnd(int(self.video_surface.winId()))
        if kind == "file":
            self.current_media = self.instance.media_new_path(source)
        else:
            self.current_media = self.instance.media_new_location(source)
            caching = (300, 1000, 2000)[self.cache.currentIndex()]
            self.current_media.add_option(f":network-caching={caching}")
            self.current_media.add_option(f":live-caching={caching}")
        self.player.set_media(self.current_media)
        if self.player.play() == -1:
            self._on_error()
            return
        self._set_status(f"正在连接 · {label}", "loading")
        self.pause_button.setEnabled(True)
        self.mini_pause.setEnabled(True)
        self.mini_state.setText("正在连接")

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开视频或音频", "", "媒体文件 (*.mp4 *.flv *.mkv *.mov *.avi *.ts *.m3u8 *.mp3 *.aac *.wav *.webm);;所有文件 (*)"
        )
        if path:
            self.address.setText(path)
            self.play_source()

    def toggle_pause(self) -> None:
        if not self.current_source:
            return
        if self.player.is_playing():
            self.player.set_pause(1)
        else:
            self.player.set_pause(0)

    def stop(self, *, reset_status: bool = True) -> None:
        had_source = bool(self.current_source)
        if self.current_media is not None:
            self.player.stop()
            self.player.set_media(None)
            self.current_media = None
        self.current_source = ""
        self.current_kind = ""
        self.video_stack.setCurrentWidget(self.placeholder)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("暂停")
        self.mini_pause.setEnabled(False)
        self.mini_pause.setText("暂停")
        self.mini_state.setText("小窗播放")
        self.seek.setEnabled(False)
        self.seek.setValue(0)
        self.elapsed.setText("00:00:00")
        self.duration.setText("00:00:00")
        self.live_badge.hide()
        if had_source and reset_status:
            self._append_log("播放", "用户停止播放")
            self.health_caption.setText("已停止")
        if reset_status:
            self._set_status("已停止播放")

    def set_volume(self, value: int) -> None:
        self.player.audio_set_volume(value)
        self.volume_value.setText(f"{value}%")
        if value and self.player.audio_get_mute():
            self.player.audio_set_mute(False)
            self.mute_button.setText("静音")
            self.mini_mute.setText("静音")

    def toggle_mute(self) -> None:
        muted = not bool(self.player.audio_get_mute())
        self.player.audio_set_mute(muted)
        self.mute_button.setText("取消静音" if muted else "静音")
        self.mini_mute.setText("取消静音" if muted else "静音")

    def _seek_start(self) -> None:
        self.is_scrubbing = True

    def _seek_end(self) -> None:
        if self.player.is_seekable():
            self.player.set_position(self.seek.value() / 1000)
        self.is_scrubbing = False

    def _refresh_playback(self) -> None:
        if not self.current_source:
            return
        current = self.player.get_time()
        total = self.player.get_length()
        is_vod = total > 1000 and bool(self.player.is_seekable())
        self.seek.setEnabled(is_vod)
        self.live_badge.setVisible(not is_vod and self.player.is_playing())
        self.elapsed.setText(format_time(current))
        self.duration.setText(format_time(total) if is_vod else "直播")
        if is_vod and not self.is_scrubbing and total > 0:
            self.seek.setValue(max(0, min(1000, round(current / total * 1000))))

    def _vlc_playing(self, _event) -> None:
        self.signals.playing.emit()

    def _vlc_paused(self, _event) -> None:
        self.signals.paused.emit()

    def _vlc_ended(self, _event) -> None:
        self.signals.ended.emit()

    def _vlc_error(self, _event) -> None:
        self.signals.error.emit()

    def _on_playing(self) -> None:
        self.ever_playing = True
        if not self._connected_current:
            self._append_log("连接", "播放器进入播放状态，开始读取媒体")
            self._connected_current = True
        elif self._buffer_active or self._last_monitor_state == "paused":
            self._append_log("播放", "数据恢复，继续播放")
        self._last_monitor_state = "playing"
        self.pause_button.setText("暂停")
        self.mini_pause.setText("暂停")
        self.mini_state.setText("正在播放")
        self._set_status("正在播放", "playing")

    def _on_paused(self) -> None:
        self._last_monitor_state = "paused"
        self._append_log("播放", "已暂停")
        self.pause_button.setText("继续")
        self.mini_pause.setText("继续")
        self.mini_state.setText("已暂停")
        self._set_status("已暂停")

    def _on_ended(self) -> None:
        self._append_log("播放", "媒体播放结束")
        self._set_status("播放结束")
        self.stop(reset_status=False)
        self.mini_state.setText("播放结束")
        self.health_caption.setText("播放结束")

    def _on_error(self) -> None:
        self._append_log("错误", "连接或解码失败，请检查网络、权限及编码")
        self.last_error = "无法播放：请检查地址、网络、权限或编码格式。"
        self.stop(reset_status=False)
        self._set_status(self.last_error, "error")
        self.mini_state.setText("播放失败")
        self.health_caption.setText("连接异常")

    def toggle_fullscreen(self) -> None:
        if self.is_fullscreen_video:
            self.leave_fullscreen()
            return
        if self.is_mini:
            self.leave_mini()
        self._restore_max_after_fullscreen = self._is_custom_maximized
        self._pre_fullscreen_geometry = self.geometry()
        self.is_fullscreen_video = True
        self.title_bar.hide()
        self.header.hide()
        self.footer.hide()
        self.source_panel.hide()
        self.control_panel.hide()
        self.monitor_panel.hide()
        self.playback_area.show()
        self.background.layout().setContentsMargins(0, 0, 0, 0)
        self.card.layout().setContentsMargins(0, 0, 0, 0)
        self.showFullScreen()
        self.fullscreen_button.setText("退出全屏  Esc")

    def leave_fullscreen(self) -> None:
        if not self.is_fullscreen_video:
            return
        self.is_fullscreen_video = False
        self.showNormal()
        if self._pre_fullscreen_geometry is not None:
            self.setGeometry(self._pre_fullscreen_geometry)
        self._is_custom_maximized = self._restore_max_after_fullscreen
        self.title_bar.show()
        self.header.show()
        self.footer.show()
        self.source_panel.show()
        self.control_panel.show()
        self.background.layout().setContentsMargins(28, 18, 28, 16)
        self.card.layout().setContentsMargins(22, 20, 22, 17)
        self.fullscreen_button.setText("全屏  F")
        self.title_bar.sync_state()
        self._adapt_ui()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        data = event.mimeData()
        if data.hasUrls() and data.urls():
            url = data.urls()[0]
            value = url.toLocalFile() if url.isLocalFile() else url.toString()
        else:
            lines = data.text().strip().splitlines() if data.hasText() else []
            value = lines[0] if lines else ""
        self.address.setText(value)
        self.play_source()
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        if self.is_mini:
            self._set_always_on_top(False)
        if self.test_report:
            self.test_report.write_text(json.dumps({
                "ever_playing": self.ever_playing,
                "last_error": self.last_error,
                "vlc_version": vlc.libvlc_get_version().decode("utf-8", "replace"),
                "monitor_samples": len(self.chart.samples),
                "monitor_log_lines": self.log_view.blockCount(),
                "monitor_read_rate": self.rate_value.text(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stop(reset_status=False)
        self.player.release()
        self.instance.release()
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--play", help="打开本地媒体文件或网络地址")
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--exit-after", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--test-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("直播播放器")
    app.setWindowIcon(QIcon(str(ASSETS / "player.ico")))
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)
    window = PlayerWindow(test_report=args.test_report)
    window.show()
    if args.play:
        window.address.setText(args.play)
        QTimer.singleShot(100, window.play_source)
    if args.screenshot:
        def save_screenshot() -> None:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.screenshot))
        QTimer.singleShot(1200, save_screenshot)
    if args.exit_after:
        QTimer.singleShot(args.exit_after * 1000, window.close)
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # A windowed PyInstaller executable has no console; show startup failures.
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "播放器启动失败", str(exc))
        raise SystemExit(1)
