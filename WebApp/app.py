import importlib
import sys
from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import QEvent, QFile, QObject, QIODevice, Qt, QUrl
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedLayout,
    QSlider,
    QWidget,
)

STREAM_URL = "http://192.168.254.123:81/stream"
BAUD_RATE = 9600
NEUTRAL = 49
DEADZONE = 2
WidgetType = TypeVar("WidgetType", bound=QWidget)


class KeyboardController(QObject):
    key_positions = {
        Qt.Key.Key_W: ("forwardSlider", 98),
        Qt.Key.Key_S: ("forwardSlider", 0),
        Qt.Key.Key_A: ("turnSlider", 0),
        Qt.Key.Key_D: ("turnSlider", 98),
    }

    def __init__(self, controller):
        super().__init__(controller.window)
        self.controller = controller
        self.pressed_keys = []

    def eventFilter(self, watched, event):
        window = self.controller.window
        if not isinstance(watched, QWidget) or (
            watched is not window and not window.isAncestorOf(watched)
        ):
            return False

        if watched is window and event.type() == QEvent.Type.Resize:
            self.controller.resize_layout()
            return False

        if event.type() == QEvent.Type.WindowDeactivate:
            self.reset()
            return False

        if isinstance(window.focusWidget(), QLineEdit):
            return False

        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key not in self.key_positions:
                return False
            if not event.isAutoRepeat():
                if key in self.pressed_keys:
                    self.pressed_keys.remove(key)
                self.pressed_keys.append(key)
                self.apply_keys()
            return True

        if event.type() == QEvent.Type.KeyRelease:
            key = event.key()
            if key not in self.key_positions:
                return False
            if not event.isAutoRepeat() and key in self.pressed_keys:
                self.pressed_keys.remove(key)
                self.apply_keys()
            return True

        return False

    def apply_keys(self):
        positions = {}
        for key in self.pressed_keys:
            slider_name, value = self.key_positions[key]
            positions[slider_name] = value

        self.controller.forward_slider.setValue(
            positions.get("forwardSlider", NEUTRAL)
        )
        self.controller.turn_slider.setValue(
            positions.get("turnSlider", NEUTRAL)
        )
        self.controller.update_key_indicator()

    def reset(self):
        if not self.pressed_keys:
            return
        self.pressed_keys.clear()
        self.apply_keys()


class RobotController:
    reference_width = 1200
    reference_height = 720
    reference_geometries = {
        "Feed": (512, 76, 291, 252),
        "turnTitle": (850, 58, 315, 30),
        "turnStatus": (850, 85, 315, 20),
        "turnSlider": (889, 100, 275, 46),
        "turnFastLeft": (889, 148, 50, 18),
        "turnSlow": (980, 148, 96, 18),
        "turnFastRight": (1114, 148, 50, 18),
        "forwardTitle": (897, 204, 310, 38),
        "forwardStatus": (897, 240, 310, 20),
        "forwardSlider": (1009, 255, 46, 275),
        "forwardFastTop": (1058, 255, 76, 18),
        "forwardSlow": (1058, 380, 100, 22),
        "forwardFastBottom": (1058, 512, 76, 18),
        "keyboardCard": (73, 466, 262, 227),
        "portNameEdit": (512, 544, 294, 35),
        "reconnBtn": (583, 591, 150, 41),
        "connectionStatus": (512, 638, 294, 20),
        "cameraStatus": (0, 0, 1, 1),
        "gripStatus": (0, 0, 1, 1),
        "gripBtn": (921, 617, 108, 51),
        "stopBtn": (1039, 617, 107, 51),
    }
    keyboard_geometries = {
        "keyboardTitle": (20, 67, 222, 54),
        "keyHint": (16, 120, 230, 35),
    }

    def __init__(self, application):
        self.application = application
        self.serial_connection = None
        self.gripper_open = None

        script_dir = Path(__file__).resolve().parent
        ui_file = QFile(str(script_dir / "app.ui"))
        if not ui_file.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError(f"Unable to open the UI file: {ui_file.fileName()}")
        try:
            self.window = QUiLoader().load(ui_file)
        finally:
            ui_file.close()
        if self.window is None:
            raise RuntimeError("Qt could not load app.ui.")

        self.forward_slider = self.require_widget(QSlider, "forwardSlider")
        self.turn_slider = self.require_widget(QSlider, "turnSlider")
        self.port_edit = self.require_widget(QLineEdit, "portNameEdit")
        self.connection_status = self.require_widget(QLabel, "connectionStatus")
        self.forward_status = self.require_widget(QLabel, "forwardStatus")
        self.turn_status = self.require_widget(QLabel, "turnStatus")
        self.grip_status = self.require_widget(QLabel, "gripStatus")

        feed = self.require_widget(QWidget, "Feed")
        self.feed_placeholder = self.require_widget(QLabel, "feedPlaceholder")
        feed_layout = QStackedLayout(feed)
        feed_layout.setContentsMargins(0, 0, 0, 0)
        feed_layout.addWidget(self.feed_placeholder)
        self.web_view = QWebEngineView(feed)
        self.web_view.setUrl(QUrl(STREAM_URL))
        feed_layout.addWidget(self.web_view)
        feed_layout.setCurrentWidget(self.feed_placeholder)
        self.feed_layout = feed_layout
        self.web_view.loadFinished.connect(self.on_camera_load_finished)

        self.forward_slider.valueChanged.connect(self.update_drive)
        self.turn_slider.valueChanged.connect(self.update_drive)
        self.forward_slider.sliderReleased.connect(
            lambda: self.forward_slider.setValue(NEUTRAL)
        )
        self.turn_slider.sliderReleased.connect(
            lambda: self.turn_slider.setValue(NEUTRAL)
        )
        self.require_widget(QPushButton, "reconnBtn").clicked.connect(
            self.toggle_connection
        )
        self.require_widget(QPushButton, "gripBtn").clicked.connect(
            self.toggle_gripper
        )
        self.require_widget(QPushButton, "stopBtn").clicked.connect(self.stop_robot)

        self.keyboard = KeyboardController(self)
        application.installEventFilter(self.keyboard)
        self.window.installEventFilter(self.keyboard)
        self.window.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize_layout()
        self.update_drive()
        self.update_key_indicator()
        self.window.setStyleSheet(self.stylesheet())

    def require_widget(self, widget_type: type[WidgetType], name: str) -> WidgetType:
        widget = self.window.findChild(widget_type, name)
        if widget is None:
            raise RuntimeError(f"Required widget {name!r} is missing from app.ui.")
        return widget

    @staticmethod
    def stylesheet():
        return """
            QWidget {
                background-color: #eff5ef;
                color: #203b29;
                font-family: Arial;
                font-size: 12px;
            }
            QWidget#Feed {
                background-color: #dcebe0;
                border: 2px solid #3e7950;
                border-radius: 12px;
            }
            QLabel#feedPlaceholder {
                background-color: #dcebe0;
                color: #36734a;
                font-size: 15px;
                font-weight: 700;
            }
            QFrame#keyboardCard {
                background-color: #dfede1;
                border: 1px solid #75a982;
                border-radius: 12px;
            }
            QLabel#keyboardTitle {
                background: transparent;
                color: #245c35;
                font-size: 13px;
                font-weight: 700;
            }
            QLabel#keyHint {
                background-color: #c9e5cf;
                border: 1px solid #79b487;
                border-radius: 7px;
                color: #1d6235;
                font-size: 12px;
                font-weight: 600;
                padding: 4px;
            }
            QLabel#turnTitle, QLabel#forwardTitle {
                background-color: #e0eee2;
                border: 1px solid #bed8c2;
                border-radius: 6px;
                color: #245f37;
                font-size: 12px;
                font-weight: 700;
                padding: 3px;
            }
            QLabel#turnFastLeft, QLabel#turnSlow, QLabel#turnFastRight,
            QLabel#forwardFastTop, QLabel#forwardSlow, QLabel#forwardFastBottom {
                background: transparent;
                color: #477b53;
                font-size: 9px;
                font-weight: 700;
            }
            QLabel#turnStatus, QLabel#forwardStatus {
                background-color: #e5f1e7;
                border: 1px solid #c6ddca;
                border-radius: 8px;
                color: #237b40;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#connectionStatus {
                background-color: #fff3d8;
                border: 1px solid #e6ca82;
                border-radius: 8px;
                color: #8a6413;
                font-size: 10px;
                font-weight: 700;
            }
            QLabel#connectionStatus[connected="true"] {
                background-color: #d6efdc;
                border-color: #75b985;
                color: #1f7138;
            }
            QLabel#cameraStatus, QLabel#gripStatus {
                background: transparent;
            }
            QLineEdit {
                background-color: #fbfdfb;
                border: 1px solid #7eaa86;
                border-radius: 8px;
                color: #203b29;
                padding: 5px 9px;
                selection-background-color: #a7d6b1;
            }
            QLineEdit:focus {
                border: 2px solid #329456;
            }
            QPushButton {
                background-color: #d3e9d7;
                border: 1px solid #438052;
                border-radius: 8px;
                color: #205a32;
                font-weight: 700;
                padding: 4px 8px;
            }
            QPushButton:hover {
                background-color: #c0e0c8;
                border-color: #267744;
            }
            QPushButton:pressed {
                background-color: #a9d2b3;
            }
            QPushButton#reconnBtn {
                background-color: #318b4d;
                border: 1px solid #236f3b;
                color: #ffffff;
            }
            QPushButton#reconnBtn:hover {
                background-color: #267a41;
            }
            QPushButton#gripBtn {
                background-color: #cbe7d1;
                border-color: #398050;
            }
            QPushButton#stopBtn {
                background-color: #f4e0df;
                border-color: #b45b55;
                color: #8a302b;
            }
            QPushButton#stopBtn:hover {
                background-color: #edcecb;
                border-color: #99453f;
            }
            QSlider::groove:vertical {
                background-color: #d5e8d8;
                border: 1px solid #5b9669;
                border-radius: 8px;
                width: 18px;
            }
            QSlider::handle:vertical {
                background-color: #319453;
                border: 2px solid #f8fff9;
                border-radius: 8px;
                height: 20px;
                margin: 0 -9px;
            }
            QSlider::groove:horizontal {
                background-color: #d5e8d8;
                border: 1px solid #5b9669;
                border-radius: 8px;
                height: 16px;
            }
            QSlider::handle:horizontal {
                background-color: #319453;
                border: 2px solid #f8fff9;
                border-radius: 8px;
                width: 20px;
                margin: -10px 0;
            }
        """

    def toggle_connection(self):
        if self.serial_connection is not None:
            self.disconnect_robot()
            return

        port = self.port_edit.text().strip()
        if not port:
            self.set_connection_status("ENTER A COM PORT", connected=False)
            return

        try:
            serial_module = importlib.import_module("serial")
            connection = serial_module.Serial(
                port=port,
                baudrate=BAUD_RATE,
                timeout=0.1,
                write_timeout=0.25,
            )
        except (ImportError, OSError, ValueError) as error:
            self.set_connection_status(f"CONNECTION FAILED: {error}", connected=False)
            QMessageBox.warning(
                self.window,
                "Robot connection failed",
                f"Could not connect to {port}.\n\n{error}",
            )
            return

        self.serial_connection = connection
        self.require_widget(QPushButton, "reconnBtn").setText("DISCONNECT")
        self.set_connection_status(f"CONNECTED · {port}", connected=True)

    def disconnect_robot(self):
        connection = self.serial_connection
        self.serial_connection = None
        self.require_widget(QPushButton, "reconnBtn").setText("CONNECT ROBOT")
        if connection is not None:
            try:
                connection.close()
            except OSError as error:
                self.set_connection_status(f"DISCONNECT ERROR: {error}", connected=False)
                QMessageBox.warning(
                    self.window,
                    "Robot disconnect failed",
                    str(error),
                )
                return
        self.set_connection_status("DISCONNECTED", connected=False)

    def send_command(self, command: str):
        if self.serial_connection is None:
            return
        try:
            self.serial_connection.write(command.encode("ascii"))
        except OSError as error:
            self.disconnect_robot()
            self.set_connection_status(f"COMMAND FAILED: {error}", connected=False)
            QMessageBox.warning(
                self.window,
                "Robot command failed",
                f"The connection was closed after a write error.\n\n{error}",
            )

    def update_drive(self):
        forward = self.forward_slider.value() - NEUTRAL
        turn = self.turn_slider.value() - NEUTRAL

        if abs(forward) > DEADZONE and abs(forward) >= abs(turn):
            command = "F" if forward > 0 else "B"
            self.forward_status.setText("Speed: 100")
            self.turn_status.setText("Speed: 0")
        elif abs(turn) > DEADZONE:
            command = "R" if turn > 0 else "L"
            self.forward_status.setText("Speed: 0")
            self.turn_status.setText("Speed: 100")
        else:
            command = "S"
            self.forward_status.setText("Speed: 0")
            self.turn_status.setText("Speed: 0")
        self.send_command(command)

    def resize_layout(self):
        scale_x = self.window.width() / self.reference_width
        scale_y = self.window.height() / self.reference_height
        for name, (x, y, width, height) in self.reference_geometries.items():
            widget = self.require_widget(QWidget, name)
            widget.setGeometry(
                round(x * scale_x),
                round(y * scale_y),
                max(1, round(width * scale_x)),
                max(1, round(height * scale_y)),
            )

        keyboard = self.require_widget(QWidget, "keyboardCard")
        scale_x = keyboard.width() / 262
        scale_y = keyboard.height() / 227
        for name, (x, y, width, height) in self.keyboard_geometries.items():
            self.require_widget(QLabel, name).setGeometry(
                round(x * scale_x),
                round(y * scale_y),
                round(width * scale_x),
                round(height * scale_y),
            )

    def toggle_gripper(self):
        self.gripper_open = not self.gripper_open
        self.send_command("O" if self.gripper_open else "P")
        state = "OPEN" if self.gripper_open else "CLOSED"
        self.grip_status.setText(f"Gripper: {state}")

    def stop_robot(self):
        self.keyboard.reset()
        self.forward_slider.setValue(NEUTRAL)
        self.turn_slider.setValue(NEUTRAL)
        self.send_command("S")
        self.forward_status.setText("NEUTRAL")
        self.turn_status.setText("NEUTRAL")

    def update_key_indicator(self):
        names = {
            Qt.Key.Key_W: "W",
            Qt.Key.Key_A: "A",
            Qt.Key.Key_S: "S",
            Qt.Key.Key_D: "D",
        }
        pressed = [names[key] for key in self.keyboard.pressed_keys]
        text = "(W, A, S, D)"
        if pressed:
            text += f"\nPressed: {', '.join(pressed)}"
        self.require_widget(QLabel, "keyHint").setText(text)

    def on_camera_load_finished(self, succeeded: bool):
        status = "CAMERA CONNECTED" if succeeded else "CAMERA UNAVAILABLE"
        self.require_widget(QLabel, "cameraStatus").setText(status)
        current = self.web_view if succeeded else self.feed_placeholder
        self.feed_layout.setCurrentWidget(current)

    def set_connection_status(self, message: str, connected: bool):
        self.connection_status.setText(message)
        self.connection_status.setProperty("connected", connected)
        self.connection_status.style().unpolish(self.connection_status)
        self.connection_status.style().polish(self.connection_status)

    def show(self):
        self.window.show()

    def shutdown(self):
        self.send_command("S")
        self.application.removeEventFilter(self.keyboard)
        self.window.removeEventFilter(self.keyboard)
        self.disconnect_robot()


def main():
    application = QApplication(sys.argv)
    controller = RobotController(application)
    application.aboutToQuit.connect(controller.shutdown)
    controller.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
