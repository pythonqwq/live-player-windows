"""Exercise real Qt window transitions on Windows without a media server."""

import ctypes
import sys
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from main import PlayerWindow


def is_topmost(window: PlayerWindow) -> bool:
    return bool(ctypes.windll.user32.GetWindowLongW(int(window.winId()), -20) & 0x8)


def main() -> None:
    app = QApplication(sys.argv)
    window = PlayerWindow()
    window.show()
    QTest.qWait(150)
    assert window.title_bar.isVisible()
    assert window.windowTitle() == "直播播放器"
    assert window.monitor_panel.isVisible(), "宽窗口应显示实时监测"
    assert "等待输入" in window.log_view.toPlainText()
    assert not window.is_window_maximized

    window.title_bar.max_button.click()
    QTest.qWait(150)
    assert window.is_window_maximized, "最大化按钮没有生效"
    assert window.geometry() == window.screen().availableGeometry()
    assert window.title_bar.max_button.text() == "❐"
    window.title_bar.max_button.click()
    QTest.qWait(150)
    assert not window.is_window_maximized, "还原按钮没有生效"
    window.title_bar.min_button.click()
    QTest.qWait(150)
    assert window.isMinimized(), "最小化按钮没有生效"
    window.showNormal()
    QTest.qWait(150)

    previous = window.geometry()
    window.title_bar.mini_button.click()
    QTest.qWait(200)
    assert window.is_mini and is_topmost(window), "小窗没有置顶"
    assert window.width() < previous.width()
    assert window.mini_panel.isVisible()
    assert window.source_panel.isHidden() and window.control_panel.isHidden()
    window.resize(390, 250)
    QTest.qWait(120)
    assert window.width() == 390 and window.height() == 250
    shot = Path(__file__).parent / "vendor-cache" / "window-mini.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(shot))

    window.title_bar.mini_button.click()
    QTest.qWait(150)
    assert not window.is_mini and not is_topmost(window), "退出小窗未取消置顶"
    assert window.source_panel.isVisible() and window.control_panel.isVisible()

    window.toggle_fullscreen()
    QTest.qWait(150)
    assert window.isFullScreen() and window.title_bar.isHidden(), "视频全屏状态错误"
    window.leave_fullscreen()
    QTest.qWait(150)
    assert not window.isFullScreen() and window.title_bar.isVisible()

    window.resize(760, 540)
    QTest.qWait(150)
    assert window.address.isVisible() and not window.version_label.isVisible()
    assert not window.protocol_chips[-1].isVisible(), "窄窗口下协议行未收起"
    assert window.header.isHidden() and window.title_bar.theme_button.isVisible()
    assert window.address.height() >= 40 and window.play_button.height() >= 40, "窄窗口输入行被压缩"
    assert not window.monitor_panel.isVisible()
    window.title_bar.monitor_button.click()
    QTest.qWait(100)
    assert window.monitor_panel.isVisible() and not window.playback_area.isVisible(), "窄窗口监测切换失败"
    window.title_bar.monitor_button.click()
    QTest.qWait(100)
    assert window.playback_area.isVisible() and not window.monitor_panel.isVisible()
    window.title_bar.theme_button.click()
    assert window.is_dark, "窄窗口下无法切换主题"
    window.title_bar.theme_button.click()

    video = Path(__file__).parent / "vendor-cache" / "test-video.mp4"
    if video.is_file():
        window.address.setText(str(video))
        window.play_source()
        QTest.qWait(2500)
        assert window.player.is_playing(), "本地视频没有开始播放"
        assert window.chart.samples, "稳定性图表没有实时采样"
        assert "读取" in window.log_view.toPlainText(), "连接与读取日志没有更新"
        window.enter_mini()
        QTest.qWait(800)
        assert window.player.is_playing() and is_topmost(window), "切换小窗后播放中断"
        frame = video.parent / "window-mini-frame.png"
        assert window.player.video_take_snapshot(0, str(frame), 0, 0) == 0
        window.toggle_fullscreen()
        QTest.qWait(500)
        assert window.isFullScreen() and window.player.is_playing(), "小窗切全屏中断播放"
        window.leave_fullscreen()
        QTest.qWait(200)
        window.stop()
    window.title_bar.close_button.click()
    assert not window.isVisible(), "关闭按钮没有生效"
    print("标题栏按钮、最大化、小窗置顶与缩放、全屏、窄窗口适配、播放切换：通过")


if __name__ == "__main__":
    main()
