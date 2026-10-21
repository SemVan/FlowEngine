"""окно Qt для обычного интерфейса FlowEngine"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    data = root / "personal-data"
    data.mkdir(exist_ok=True)
    log_stream = (data / "desktop.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = log_stream
    sys.stderr = log_stream
    os.environ["FLOWENGINE_CONFIG_DIR"] = str(data)
    from PySide6.QtCore import QTimer, QUrl, QLockFile
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMessageBox, QVBoxLayout, QWidget, QPushButton, QFileDialog
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from flowengine.desktop_session import DesktopSession

    app = QApplication(sys.argv)
    app.setApplicationName("FlowEngine Personal")
    lock = QLockFile(str(data / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "FlowEngine", "FlowEngine Personal уже запущен. Откройте существующее окно.")
        return
    session = DesktopSession(data, mock=not args.real)

    class LocalPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            return url.toString() == "about:blank" or url.toString().startswith("blob:" + session.url + "/") or (
                url.scheme() == "http" and url.host() == "127.0.0.1" and url.port() == session.port
            )

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("FlowEngine Personal — " + ("оборудование" if args.real else "симулятор"))
            available = app.primaryScreen().availableGeometry()
            self.resize(min(1440, available.width()), min(900, available.height()))
            self.setMinimumSize(1000, 680)
            self.setStyleSheet("background:#181b20;color:#eeeeef;font:14px 'Segoe UI';")
            box = QWidget()
            layout = QVBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            self.notice = QLabel("   Запускаем FlowEngine…")
            self.notice.setMinimumHeight(42)
            layout.addWidget(self.notice)
            self.recover = QPushButton("Открыть заводской профиль")
            self.recover.hide()
            self.recover.clicked.connect(self.recover_default)
            layout.addWidget(self.recover)
            self.view = QWebEngineView()
            self.view.setPage(LocalPage(self.view))
            self.view.page().profile().downloadRequested.connect(self.save_download)
            self.view.page().setBackgroundColor(QColor("#181b20"))
            layout.addWidget(self.view)
            self.setCentralWidget(box)
            self.generation = 0
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.update_session)
            self.timer.start(200)

        def save_download(self, download):
            filename, _ = QFileDialog.getSaveFileName(self, "Сохранить файл", str(root / download.downloadFileName()))
            if filename:
                path = Path(filename)
                download.setDownloadDirectory(str(path.parent))
                download.setDownloadFileName(path.name)
                download.accept()
            else:
                download.cancel()

        def update_session(self):
            if session.error:
                self.notice.setText("   " + session.error)
                self.notice.show()
                self.recover.setVisible(not session.thread.is_alive())
            elif session.ready and session.generation != self.generation:
                path = self.view.url().path() or "/config"
                self.generation = session.generation
                self.view.setUrl(QUrl(session.url + path))
                self.notice.hide()
                self.recover.hide()
            elif not session.ready:
                self.notice.setText("   Применяем профиль…" if self.generation else "   Запускаем FlowEngine…")
                self.notice.show()

        def recover_default(self):
            import threading
            if session.thread.is_alive():
                return
            session.profile = "default"
            session.error = ""
            session.pending = None
            session.thread = threading.Thread(target=session.run, daemon=True)
            self.recover.hide()
            session.thread.start()

        def closeEvent(self, event):
            try:
                session.close()
            except Exception:
                QMessageBox.information(self, "Сеанс ещё активен", "Дождитесь завершения операции или остановите её кнопкой СТОП, затем закройте окно.")
                event.ignore()
                return
            self.timer.stop()
            session.thread.join(timeout=8)
            event.accept()

    window = Window()
    window.show()
    session.thread.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
