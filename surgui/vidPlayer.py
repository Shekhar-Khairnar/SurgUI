from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QAction,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QSlider,
    QStyle,
    QFrame,
    QSizePolicy,
    QFileDialog,
    QLineEdit,
    QFormLayout,
    QGroupBox,
    QScrollArea,
    QMainWindow,
    QComboBox,
    QSplitter,
)
import sys
import os
import platform
import cv2
import vlc
from PyQt5.QtMultimedia import (
    QMediaContent,
    QMediaPlayer,
    QVideoFrame,
    QAbstractVideoSurface,
    QAbstractVideoBuffer,
    QVideoSurfaceFormat,
)
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtGui import QIcon, QPalette, QImage, QPainter, QFont
from PyQt5.QtCore import Qt, QUrl, pyqtSignal, QPoint, QRect, QObject
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
from PyQt5 import QtWidgets
from functools import partial
import subprocess
import json
import time
import re


class Slider(QSlider):
    def mousePressEvent(self, event, window):
        if event.button() == Qt.LeftButton:
            event.accept()
            x = event.pos().x()
            value = (self.maximum() - self.minimum()) * x / self.width() + self.minimum()
            window.timer.stop()
            window.mediaPlayer.set_position(value / 100000)
            window.timer.start()
        else:
            return super().mousePressEvent(window, event)

    def mouseMoveEvent(self, event, window):
        event.accept()
        x = event.pos().x()
        value = (self.maximum() - self.minimum()) * x / self.width() + self.minimum()
        window.mediaPlayer.set_position(value / 100000)
        self.setValue(value)


class Window(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Surgical Video Player")
        self.setGeometry(350, 100, 700, 500)
        self.setWindowIcon(QIcon("player.png"))

        p = self.palette()
        p.setColor(QPalette.Window, Qt.black)
        self.setPalette(p)

        self.setFocusPolicy(Qt.StrongFocus)

        self.num_panels = 0
        self.is_paused = False

        # NEW: track current panel orientation ("side" or "bottom")
        self.panel_orientation = "side"

        self.init_ui()
        self.showMaximized()

    def init_ui(self):
        self.instance = vlc.Instance("--no-audio")
        self.media = None
        self.mediaPlayer = self.instance.media_player_new()

        # In this widget, the video will be drawn
        if platform.system() == "Darwin":  # for MacOS
            self.videowidget = QtWidgets.QMacCocoaViewContainer(0)
        else:  # Linux and Windows
            self.videowidget = QVideoWidget()
            self.mediaPlayer.set_xwindow(int(self.videowidget.winId()))

        self.videowidget_g = QVideoWidget()

        # create open button
        openBtn = QPushButton("Open Video")
        openBtn.clicked.connect(self.open_video)

        # create button for taking a snapshot
        snapBtn = QPushButton("snapshot (save the image)")
        snapBtn.clicked.connect(self.screenshotCall)
        self.ImagesBuffer = None

        labelmeBtn = QPushButton("Annotate (labelme)")
        labelmeBtn.clicked.connect(self.annotate)

        # font size input box
        self.panelFontEdit = QLineEdit()
        self.panelFontEdit.setFixedWidth(60)
        self.panelFontEdit.setText("10")
        self.panelFontEdit.setPlaceholderText("pt")
        self.panelFontEdit.setStyleSheet("background-color: white; color: black;")
        self.panelFontEdit.setToolTip("Panel task font size (pt). Type a number and press Enter.")
        self.panelFontEdit.editingFinished.connect(self.apply_panel_font_size_from_text)

        snapBtn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        labelmeBtn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        snapBtn.setMaximumWidth(260)
        labelmeBtn.setMaximumWidth(260)

        # create button for playing
        self.playBtn = QPushButton()
        self.playBtn.setEnabled(False)
        self.playBtn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.playBtn.clicked.connect(self.play_video)

        self.playbackspeedBtn = QComboBox()
        self.playbackspeedBtn.addItems(["1X", "1.5X", "2X", "3X"])
        self.playbackspeedBtn.currentIndexChanged.connect(self.set_speed)

        # create slider
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMaximum(100000)
        self.slider.sliderMoved.connect(self.position_changed)
        self.slider.sliderPressed.connect(self.position_changed)

        self.l = QLabel("0:00:00")
        self.l.setStyleSheet("color: white")
        self.slider.valueChanged.connect(self.display_time)
        self.d = QLabel("0:00:00")
        self.d.setStyleSheet("color: white")

        self.label = QLabel()
        self.label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        # controls row
        hboxLayout = QHBoxLayout()
        hboxLayout.setContentsMargins(0, 0, 0, 0)
        hboxLayout.addWidget(self.playBtn)
        hboxLayout.addWidget(self.playbackspeedBtn)
        hboxLayout.addWidget(self.l)
        hboxLayout.addWidget(self.slider)
        hboxLayout.addWidget(self.d)

        # snapshot/annotate/font row
        bottomLayout = QHBoxLayout()
        bottomLayout.setContentsMargins(0, 0, 0, 0)
        bottomLayout.setSpacing(10)
        bottomLayout.addWidget(snapBtn)
        bottomLayout.addWidget(labelmeBtn)

        fontLbl = QLabel("Font:")
        fontLbl.setStyleSheet("color: white")
        bottomLayout.addWidget(fontLbl)
        bottomLayout.addWidget(self.panelFontEdit)

        # NEW: orientation toggle button on the controls row
        self.orientationBtn = QPushButton("Panels: Bottom")
        self.orientationBtn.setToolTip("Move panels to the bottom (Ctrl+T to toggle)")
        self.orientationBtn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.orientationBtn.clicked.connect(self.toggle_panel_orientation)
        bottomLayout.addWidget(self.orientationBtn)

        bottomLayout.addStretch(1)

        # left side (video + controls)
        vboxLayout = QVBoxLayout()
        vboxLayout.addWidget(self.videowidget)
        vboxLayout.addLayout(hboxLayout)
        vboxLayout.addLayout(bottomLayout)
        vboxLayout.addWidget(self.label)

        vboxLayout.setStretch(0, 1)
        vboxLayout.setStretch(1, 0)
        vboxLayout.setStretch(2, 0)
        vboxLayout.setStretch(3, 0)

        leftWidget = QWidget()
        leftWidget.setLayout(vboxLayout)

        # panels are side-by-side + individually resizable
        self.panelsSplitter = QSplitter(Qt.Horizontal)

        # Root splitter: video + panels. Orientation flips between Horizontal (side)
        # and Vertical (bottom).
        self.rootSplitter = QSplitter(Qt.Horizontal)
        self.rootSplitter.addWidget(leftWidget)
        self.rootSplitter.addWidget(self.panelsSplitter)
        self.rootSplitter.setStretchFactor(0, 4)
        self.rootSplitter.setStretchFactor(1, 1)
        self.rootSplitter.setSizes([900, 350])

        self.mainLayout = QHBoxLayout()
        self.mainLayout.setContentsMargins(0, 0, 0, 0)
        self.mainLayout.addWidget(self.rootSplitter)

        # create empty lists for the panels to be added
        self.groupbox = [None] * 10
        self.formLayout = [None] * 10
        self.form_title = [None] * 10
        self.panelRemoveBtn = [None] * 10
        self.scroll = [None] * 10
        self.tasklist = [None] * 10
        self.startingButtonlist = [None] * 10
        self.startingTimelist = [None] * 10
        self.endingButtonlist = [None] * 10
        self.endingTimelist = [None] * 10
        self.saveEntryBtn = [None] * 10
        self.clearEntryBtn = [None] * 10
        self.ratingButtonslist = [None] * 10
        self.yesButtonlist = [None] * 10
        self.noButtonlist = [None] * 10
        self.ratingItemlist = [None] * 10
        self.groupButtonlist = [None] * 10

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_ui)

        self.setLayout(self.mainLayout)

        self.parrentDirectory = None
        self.save_directory = None
        self.vidname = None
        self.image = None
        self.image_save_directory = None
        self.segmentation_labels = set()

        # create menu bar
        menuBar = QMenuBar(self)
        fileMenu = menuBar.addMenu("&File")
        viewMenu = menuBar.addMenu("&View")
        HelpMenu = menuBar.addMenu("&Help")

        openVideoAction = QAction("&Open Video", self)
        openVideoAction.setShortcut("Ctrl+O")
        openVideoAction.setStatusTip("Open Video")
        openVideoAction.triggered.connect(self.open_video)

        addPanelAction = QAction("&Add Panel", self)
        addPanelAction.setStatusTip("Add Panel")
        addPanelAction.triggered.connect(self.add_panel)

        addTimePanelFileAction = QAction("&Add Timestamping Panel", self)
        addTimePanelFileAction.setStatusTip("Add Timestamping Panel From File")
        addTimePanelFileAction.triggered.connect(self.add_time_panel_from_file)

        addRatingPanelFileAction = QAction("&Add Rating Panel", self)
        addRatingPanelFileAction.setStatusTip("Add Rating Panel From File")
        addRatingPanelFileAction.triggered.connect(self.add_rating_panel_from_file)

        saveEntriesAction = QAction("&Save", self)
        saveEntriesAction.setStatusTip("Save all entries")
        saveEntriesAction.setShortcut("Ctrl+S")
        saveEntriesAction.triggered.connect(self.save)

        clearPanelsAction = QAction("&Clear Panels", self)
        clearPanelsAction.setStatusTip("Clear all panels")
        clearPanelsAction.setShortcut("Ctrl+C")
        clearPanelsAction.triggered.connect(self.clearPanels)

        changeSaveDirectoryAction = QAction("&Change Save Directory", self)
        changeSaveDirectoryAction.setStatusTip("Change Save Directory")
        changeSaveDirectoryAction.triggered.connect(self.changeDirectory)

        # NEW: toggle panel orientation menu action
        togglePanelOrientationAction = QAction("&Toggle Panel Orientation", self)
        togglePanelOrientationAction.setStatusTip("Switch panels between side and bottom layout")
        togglePanelOrientationAction.setShortcut("Ctrl+T")
        togglePanelOrientationAction.triggered.connect(self.toggle_panel_orientation)

        exitAction = QAction("&Exit", self)
        exitAction.setStatusTip("Exit")
        exitAction.setShortcut("Ctrl+Q")
        exitAction.triggered.connect(self.close)

        fileMenu.addAction(openVideoAction)
        fileMenu.addAction(addTimePanelFileAction)
        fileMenu.addAction(addRatingPanelFileAction)
        fileMenu.addAction(changeSaveDirectoryAction)
        fileMenu.addAction(saveEntriesAction)
        fileMenu.addAction(clearPanelsAction)
        fileMenu.addAction(exitAction)

        viewMenu.addAction(togglePanelOrientationAction)

    # =========================================================================
    # NEW: Panel orientation toggle
    # =========================================================================
    def toggle_panel_orientation(self):
        """Switch panels between right-of-video (side) and below-video (bottom)."""
        if self.panel_orientation == "side":
            self.panel_orientation = "bottom"
            self.rootSplitter.setOrientation(Qt.Vertical)
            # Give video more vertical space than the panels by default
            total = max(self.rootSplitter.height(), 600)
            self.rootSplitter.setSizes([int(total * 0.65), int(total * 0.35)])
            self.orientationBtn.setText("Panels: Side")
            self.orientationBtn.setToolTip("Move panels to the side (Ctrl+T to toggle)")
        else:
            self.panel_orientation = "side"
            self.rootSplitter.setOrientation(Qt.Horizontal)
            total = max(self.rootSplitter.width(), 1200)
            self.rootSplitter.setSizes([int(total * 0.72), int(total * 0.28)])
            self.orientationBtn.setText("Panels: Bottom")
            self.orientationBtn.setToolTip("Move panels to the bottom (Ctrl+T to toggle)")

    # =========================================================================
    # NEW: helpers for deduplicating + chronological sort of output files
    # =========================================================================
    def _time_to_seconds(self, time_str):
        """Convert 'H:MM:SS' (or 'MM:SS' / 'SS') to total seconds for sorting."""
        if time_str is None:
            return 0
        try:
            parts = str(time_str).strip().split(":")
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                return parts[0] * 60 + parts[1]
            elif len(parts) == 1:
                return parts[0]
        except (ValueError, IndexError):
            pass
        return 0

    def _extract_start_time(self, line):
        """Pull the starting timestamp out of either output format."""
        # Rating format: "task : score | Time: H:MM:SS to H:MM:SS"
        m = re.search(r"Time:\s*([\d:]+)\s*to", line)
        if m:
            return self._time_to_seconds(m.group(1))
        # Timestamp format: "task : (H:MM:SS , H:MM:SS)"
        m = re.search(r":\s*\(\s*([\d:]+)\s*,", line)
        if m:
            return self._time_to_seconds(m.group(1))
        return 0

    def _dedupe_and_sort_file(self, filepath):
        """Remove duplicate lines and sort chronologically by starting time."""
        if not filepath or not os.path.exists(filepath):
            return
        try:
            with open(filepath, "r") as f:
                lines = f.readlines()

            seen = set()
            unique = []
            for raw in lines:
                key = raw.strip()
                if not key:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                if not raw.endswith("\n"):
                    raw = raw + "\n"
                unique.append(raw)

            unique.sort(key=self._extract_start_time)

            with open(filepath, "w") as f:
                f.writelines(unique)
        except OSError as e:
            print("Could not dedupe/sort {}: {}".format(filepath, e))

    # =========================================================================
    # Existing methods
    # =========================================================================
    def apply_panel_font_size_from_text(self):
        if not hasattr(self, "panelFontEdit"):
            return
        txt = self.panelFontEdit.text().strip()
        if not txt:
            return
        try:
            pt = int(txt)
        except ValueError:
            return

        pt = max(6, min(40, pt))

        for panel_index in range(1, self.num_panels + 1):
            if not self.tasklist[panel_index]:
                continue
            for lbl in self.tasklist[panel_index]:
                if isinstance(lbl, QLabel):
                    f = lbl.font()
                    f.setPointSize(pt)
                    lbl.setFont(f)
                    lbl.setWordWrap(True)
                    lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.play_video()
        elif event.key() == Qt.Key_Right:
            p = self.slider.value()
            p = p + int(500000000 / self.media.get_duration())
            self.set_position(p)
        elif event.key() == Qt.Key_Left:
            p = self.slider.value()
            p = p - int(500000000 / self.media.get_duration())
            self.set_position(p)
        elif event.key() == Qt.Key_F5:
            self.close()
        else:
            super().keyPressEvent(event)

    def open_video(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open Video",
            filter=(
                "Video files (*.MP4;*.AVI;*.MPG;*.MPEG;"
                "*.MOV;*.mp4;*.avi;*.mpg;*.mpeg;);; "
                "All files(*.*)"
            ),
        )

        if filename != "":
            self.media = self.instance.media_new(filename)
            self.mediaPlayer.set_media(self.media)
            self.media.parse()
            self.setWindowTitle(self.media.get_meta(0))

            self.playBtn.setEnabled(True)

            if platform.system() == "Windows":
                self.mediaPlayer.set_hwnd(int(self.videowidget.winId()))
            elif platform.system() == "Darwin":
                self.mediaPlayer.set_nsobject(int(self.videowidget.winId()))
            else:
                print("Unsupported platform")

            self.cap = cv2.VideoCapture(filename)
            self.play_video()

            self.d.setText(str(self.getDurationValue()))

            if not self.parrentDirectory:
                self.parrentDirectory = "./outputs"
                if not os.path.exists(self.parrentDirectory):
                    os.mkdir(self.parrentDirectory)

            self.vidname = os.path.basename(filename)
            print("Saving directory: ", self.parrentDirectory + "/" + self.vidname)
            if not os.path.exists(self.parrentDirectory + "/" + self.vidname):
                os.mkdir(self.parrentDirectory + "/" + self.vidname)
            self.save_directory = self.parrentDirectory + "/" + self.vidname

            if not os.path.exists(self.parrentDirectory + "/" + self.vidname + "/images"):
                os.mkdir(self.parrentDirectory + "/" + self.vidname + "/images")
            self.image_save_directory = self.parrentDirectory + "/" + self.vidname + "/images"

            if self.num_panels > 0:
                self.clearPanels()
                for panel_index in range(1, self.num_panels + 1):
                    if self.groupButtonlist[panel_index]:
                        if not os.path.exists(
                            self.save_directory + "/" + self.form_title[panel_index].text() + "_scores.txt"
                        ):
                            with open(
                                "{}/{}_scores.txt".format(
                                    self.save_directory, self.form_title[panel_index].text()
                                ),
                                "a",
                            ) as out:
                                for i in range(len(self.groupButtonlist[panel_index])):
                                    out.write("{} \n".format(self.tasklist[self.panel_index][i].text()))
                        else:
                            with open(
                                "{}/{}_scores.txt".format(
                                    self.save_directory, self.form_title[panel_index].text()
                                ),
                                "r",
                            ) as f:
                                lines = f.readlines()
                                for line in lines:
                                    rating_item = line.split(" : ")[0]
                                    if len(line.split(" : ")) == 2:
                                        score = line.split(" : ")[1]
                                        for i in range(len(self.groupButtonlist[panel_index])):
                                            if self.tasklist[panel_index][i].text() == rating_item:
                                                for j in range(len(self.ratingButtonslist[panel_index][i])):
                                                    if int(self.ratingButtonslist[panel_index][i][j].text()) == int(score):
                                                        self.ratingButtonslist[panel_index][i][j].setChecked(True)

    def play_video(self):
        if self.mediaPlayer.is_playing():
            self.mediaPlayer.pause()
            self.is_paused = True
            self.playBtn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            self.timer.stop()
        else:
            self.mediaPlayer.play()
            self.timer.start()
            self.is_paused = False
            self.playBtn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def changeDirectory(self):
        self.parrentDirectory = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Folder")

        if self.vidname:
            if not os.path.exists(self.parrentDirectory + "/" + self.vidname):
                os.mkdir(self.parrentDirectory + "/" + self.vidname)
            self.save_directory = self.parrentDirectory + "/" + self.vidname

            if not os.path.exists(self.parrentDirectory + "/" + self.vidname + "/images"):
                os.mkdir(self.parrentDirectory + "/" + self.vidname + "/images")
            self.image_save_directory = self.parrentDirectory + "/" + self.vidname + "/images"

            print("Saving directory: ", self.parrentDirectory + "/" + self.vidname)

    def display_time(self):
        time_ = self.getSliderValue()
        self.l.setText("{}".format(str(time_)))

    def add_panel(self):
        pass

    def onpanelRemoveBtnClicked(self, panel_index):
        pass

    def add_time_panel_from_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Open Text", filter="Text files (*.txt)")

        if filename != "":
            title = str(os.path.basename(filename)).split(".")[0]
            with open(filename) as f:
                lines = f.read().splitlines()
            print("Opened time panel:", title)

            self.num_panels += 1
            self.panel_index = self.num_panels

            self.tasklist[self.panel_index] = []
            self.startingButtonlist[self.panel_index] = []
            self.startingTimelist[self.panel_index] = []
            self.endingButtonlist[self.panel_index] = []
            self.endingTimelist[self.panel_index] = []
            self.saveEntryBtn[self.panel_index] = []
            self.clearEntryBtn[self.panel_index] = []

            self.groupbox[self.panel_index] = QGroupBox()
            self.formLayout[self.panel_index] = QFormLayout()

            self.form_title[self.panel_index] = QLabel(title)
            self.form_title[self.panel_index].setStyleSheet("color: white")
            self.form_title[self.panel_index].setAlignment(Qt.AlignCenter)
            self.form_title[self.panel_index].setFont(QFont("Times", 12, weight=QFont.Bold))

            self.panelRemoveBtn[self.panel_index] = QPushButton("Exit")
            self.panelRemoveBtn[self.panel_index].clicked.connect(
                partial(self.onpanelRemoveBtnClicked, self.panel_index)
            )

            self.formLayout[self.panel_index].addRow(self.form_title[self.panel_index])

            for i, line in enumerate(lines):
                line = line.split("#")

                self.tasklist[self.panel_index].append(QLabel(line[0]))
                self.tasklist[self.panel_index][i].setStyleSheet("background-color: black ; color: white")
                self.tasklist[self.panel_index][i].setFont(QFont("Times", 10, weight=QFont.Bold))
                self.tasklist[self.panel_index][i].setWordWrap(True)

                self.startingButtonlist[self.panel_index].append(QPushButton("starts"))
                self.startingButtonlist[self.panel_index][i].setFixedWidth(50)
                self.startingTimelist[self.panel_index].append(QLabel("0"))
                self.startingTimelist[self.panel_index][i].setStyleSheet("color: white")
                self.startingButtonlist[self.panel_index][i].clicked.connect(
                    partial(self.onstartbuttonClicked, self.panel_index, i)
                )

                self.endingButtonlist[self.panel_index].append(QPushButton("ends"))
                self.endingButtonlist[self.panel_index][i].setFixedWidth(50)
                self.endingTimelist[self.panel_index].append(QLabel("0"))
                self.endingTimelist[self.panel_index][i].setStyleSheet("color: white")
                self.endingButtonlist[self.panel_index][i].clicked.connect(
                    partial(self.onendbuttonClicked, self.panel_index, i)
                )

                if len(line) == 3:
                    self.startingButtonlist[self.panel_index][i].setToolTip(line[1])
                    self.endingButtonlist[self.panel_index][i].setToolTip(line[2])

                self.saveEntryBtn[self.panel_index].append(QPushButton("save"))
                self.clearEntryBtn[self.panel_index].append(QPushButton("clear"))
                self.saveEntryBtn[self.panel_index][i].setFixedWidth(50)
                self.clearEntryBtn[self.panel_index][i].setFixedWidth(50)
                self.saveEntryBtn[self.panel_index][i].setEnabled(False)
                self.saveEntryBtn[self.panel_index][i].clicked.connect(
                    partial(self.onsaveEntryBtnClicked, self.panel_index, i)
                )
                self.clearEntryBtn[self.panel_index][i].setEnabled(False)
                self.clearEntryBtn[self.panel_index][i].clicked.connect(
                    partial(self.onclearEntryBtnClicked, self.panel_index, i)
                )

                self.formLayout[self.panel_index].addRow(self.tasklist[self.panel_index][i])
                self.formLayout[self.panel_index].addRow(
                    self.startingButtonlist[self.panel_index][i],
                    self.startingTimelist[self.panel_index][i],
                )
                self.formLayout[self.panel_index].addRow(
                    self.endingButtonlist[self.panel_index][i],
                    self.endingTimelist[self.panel_index][i],
                )
                self.formLayout[self.panel_index].addRow(
                    self.saveEntryBtn[self.panel_index][i],
                    self.clearEntryBtn[self.panel_index][i],
                )

            self.groupbox[self.panel_index].setLayout(self.formLayout[self.panel_index])
            self.scroll[self.panel_index] = QScrollArea()
            self.scroll[self.panel_index].setWidget(self.groupbox[self.panel_index])
            self.scroll[self.panel_index].setWidgetResizable(True)
            self.scroll[self.panel_index].setFocusPolicy(Qt.StrongFocus)

            self.panelsSplitter.addWidget(self.scroll[self.panel_index])

            self.apply_panel_font_size_from_text()

    def add_rating_panel_from_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Open Text", filter="Text files (*.txt)")

        if filename != "" and str(filename).endswith(".txt"):
            title = str(os.path.basename(filename)).split(".")[0]

            with open(filename) as f:
                lines = f.read().splitlines()
            print("Opened rating panel:", title)

            self.num_panels += 1
            self.panel_index = self.num_panels

            self.tasklist[self.panel_index] = []
            self.yesButtonlist[self.panel_index] = []
            self.noButtonlist[self.panel_index] = []
            self.ratingButtonslist[self.panel_index] = []
            self.startingButtonlist[self.panel_index] = []
            self.startingTimelist[self.panel_index] = []
            self.endingButtonlist[self.panel_index] = []
            self.endingTimelist[self.panel_index] = []
            self.groupButtonlist[self.panel_index] = []
            self.saveEntryBtn[self.panel_index] = []
            self.clearEntryBtn[self.panel_index] = []

            self.yes_label = QLabel("Yes")
            self.yes_label.setStyleSheet("color: white")
            self.no_label = QLabel("No")
            self.no_label.setStyleSheet("color: white")

            self.groupbox[self.panel_index] = QGroupBox()
            self.formLayout[self.panel_index] = QFormLayout()
            self.formLayout[self.panel_index].setVerticalSpacing(10)

            self.form_title[self.panel_index] = QLabel(title)
            self.form_title[self.panel_index].setStyleSheet("color: white")
            self.form_title[self.panel_index].setAlignment(Qt.AlignCenter)
            self.form_title[self.panel_index].setFont(QFont("Times", 12, weight=QFont.Bold))

            self.panelRemoveBtn[self.panel_index] = QPushButton("Exit")
            self.panelRemoveBtn[self.panel_index].clicked.connect(
                partial(self.onpanelRemoveBtnClicked, self.panel_index)
            )

            self.formLayout[self.panel_index].addRow(self.form_title[self.panel_index])

            for i, line in enumerate(lines):
                parts = line.strip().split(" : ")
                task = parts[0]

                num_scores = 5
                if len(parts) > 1:
                    try:
                        num_scores = int(parts[1]) + 1
                    except ValueError:
                        print(
                            "Warning: Invalid number of scores provided in line",
                            i + 1,
                            "using default value of 5.",
                        )

                self.tasklist[self.panel_index].append(QLabel(task))
                self.tasklist[self.panel_index][i].setStyleSheet("background-color: black ; color: white")
                self.tasklist[self.panel_index][i].setFont(QFont("Times", 10, weight=QFont.Bold))
                self.tasklist[self.panel_index][i].setWordWrap(True)
                self.tasklist[self.panel_index][i].setFixedWidth(100)

                self.startingButtonlist[self.panel_index].append(QPushButton("starts"))
                self.startingButtonlist[self.panel_index][i].setFixedWidth(50)
                self.startingTimelist[self.panel_index].append(QLabel("0"))
                self.startingTimelist[self.panel_index][i].setStyleSheet("color: white")
                self.startingButtonlist[self.panel_index][i].clicked.connect(
                    partial(self.onstartbuttonClicked, self.panel_index, i)
                )

                self.endingButtonlist[self.panel_index].append(QPushButton("ends"))
                self.endingButtonlist[self.panel_index][i].setFixedWidth(50)
                self.endingTimelist[self.panel_index].append(QLabel("0"))
                self.endingTimelist[self.panel_index][i].setStyleSheet("color: white")
                self.endingButtonlist[self.panel_index][i].clicked.connect(
                    partial(self.onendbuttonClicked, self.panel_index, i)
                )
                self.endingButtonlist[self.panel_index][i].setEnabled(False)

                self.ratingButtonslist[self.panel_index].append([])

                for j in range(num_scores):
                    self.ratingButtonslist[self.panel_index][i].append(QRadioButton(str(j + 1)))
                    self.ratingButtonslist[self.panel_index][i][j].setStyleSheet(
                        "background-color: black ; color: white"
                    )
                for btn in self.ratingButtonslist[self.panel_index][i]:
                    btn.setEnabled(False)

                self.groupButtonlist[self.panel_index].append(QButtonGroup())
                for j in range(num_scores):
                    self.groupButtonlist[self.panel_index][i].addButton(
                        self.ratingButtonslist[self.panel_index][i][j]
                    )
                self.groupButtonlist[self.panel_index][i].buttonClicked.connect(
                    partial(self.save_rating_entry, self.panel_index, i)
                )

                hbLayout = QHBoxLayout()
                for j in range(num_scores):
                    hbLayout.addWidget(self.ratingButtonslist[self.panel_index][i][j])
                hbLayout.setContentsMargins(0, 0, 0, 0)
                container = QWidget()
                container.setLayout(hbLayout)

                self.clearEntryBtn[self.panel_index].append(QPushButton("clear"))
                self.clearEntryBtn[self.panel_index][i].setFixedWidth(50)
                self.clearEntryBtn[self.panel_index][i].setEnabled(True)
                self.clearEntryBtn[self.panel_index][i].clicked.connect(
                    partial(self.onclearEntryBtnClicked, self.panel_index, i)
                )

                self.formLayout[self.panel_index].addRow(self.tasklist[self.panel_index][i], container)
                self.formLayout[self.panel_index].addRow(
                    self.startingButtonlist[self.panel_index][i],
                    self.startingTimelist[self.panel_index][i],
                )
                self.formLayout[self.panel_index].addRow(
                    self.endingButtonlist[self.panel_index][i],
                    self.endingTimelist[self.panel_index][i],
                )
                self.formLayout[self.panel_index].addRow(self.clearEntryBtn[self.panel_index][i])

            self.groupbox[self.panel_index].setLayout(self.formLayout[self.panel_index])
            self.scroll[self.panel_index] = QScrollArea()
            self.scroll[self.panel_index].setWidget(self.groupbox[self.panel_index])
            self.scroll[self.panel_index].setWidgetResizable(True)
            self.scroll[self.panel_index].setFocusPolicy(Qt.StrongFocus)

            self.panelsSplitter.addWidget(self.scroll[self.panel_index])

            self.apply_panel_font_size_from_text()

        if self.save_directory:
            scores_path = self.save_directory + "/" + self.form_title[self.panel_index].text() + "_scores.txt"
            if os.path.exists(scores_path):
                with open(scores_path, "r") as f:
                    lines = f.readlines()
                    new_lines = []
                    for line in lines:
                        rating_item = line.split(" : ")[0]
                        details = line.strip().split(" : ")[1]
                        existing_score, existing_times = details.split(" | ") if " | " in details else (details, "Not set")

                        if len(line.split(" : ")) == 2:
                            score = line.split(" : ")[1]
                            for i in range(len(self.groupButtonlist[self.panel_index])):
                                if self.tasklist[self.panel_index][i].text() == rating_item:
                                    for j in range(len(self.ratingButtonslist[self.panel_index][i])):
                                        if score.strip().isdigit():
                                            if int(self.ratingButtonslist[self.panel_index][i][j].text()) == int(score):
                                                self.ratingButtonslist[self.panel_index][i][j].setChecked(True)
                                        else:
                                            print(f"Skipping invalid score: {score}")
                                    new_lines.append(
                                        f"{rating_item} : {score} | {self.startingTimelist[self.panel_index][i].text()} to {self.endingTimelist[self.panel_index][i].text()}"
                                    )

                with open(scores_path, "w") as f:
                    f.writelines(new_lines)

                # NEW: clean up after rewrite
                self._dedupe_and_sort_file(scores_path)
            else:
                with open(scores_path, "a") as out:
                    for i in range(len(self.groupButtonlist[self.panel_index])):
                        starting_time = self.startingTimelist[self.panel_index][i].text()
                        ending_time = self.endingTimelist[self.panel_index][i].text()
                        score = None
                        for j in range(len(self.ratingButtonslist[self.panel_index][i])):
                            if self.ratingButtonslist[self.panel_index][i][j].isChecked():
                                score = self.ratingButtonslist[self.panel_index][i][j].text()
                        if starting_time != "0" and ending_time != "0" and score is not None:
                            out.write(
                                "{} : {} | Time: {} to {}\n".format(
                                    self.tasklist[self.panel_index][i].text(),
                                    score,
                                    starting_time,
                                    ending_time,
                                )
                            )
                # NEW: clean up after initial write
                self._dedupe_and_sort_file(scores_path)

    def position_changed(self):
        self.timer.stop()
        pos = self.slider.value()
        self.mediaPlayer.set_position(pos / 100000)
        self.timer.start()

    def get_position(self):
        _ = self.mediaPlayer.position()

    def duration_changed(self, duration):
        self.slider.setRange(0, duration)

    def set_position(self, position):
        self.timer.stop()
        self.mediaPlayer.set_position(position / 100000)
        self.timer.start()

    def handle_errors(self):
        self.playBtn.setEnabled(False)
        self.label.setText("Error: " + self.mediaPlayer.errorString())

    def getSliderValue(self):
        value = int(self.mediaPlayer.get_position() * (self.media.get_duration()))
        value = value // 1000
        mins, sec = divmod(value, 60)
        hour, mins = divmod(mins, 60)
        return "%d:%02d:%02d" % (hour, mins, sec)

    def getDurationValue(self):
        value = int(self.media.get_duration())
        value = value // 1000
        mins, sec = divmod(value, 60)
        hour, mins = divmod(mins, 60)
        return "%d:%02d:%02d" % (hour, mins, sec)

    def onstartbuttonClicked(self, panel_index, i):
        value = self.getSliderValue()
        self.startingTimelist[panel_index][i].setText(str(value))
        self.startingTimelist[panel_index][i].setStyleSheet("color: white")
        self.endingButtonlist[panel_index][i].setEnabled(True)

    def onendbuttonClicked(self, panel_index, i):
        value = self.getSliderValue()
        self.endingTimelist[panel_index][i].setText(str(value))
        self.endingTimelist[panel_index][i].setStyleSheet("color: white")
        if self.ratingButtonslist[panel_index] and len(self.ratingButtonslist[panel_index]) > i:
            for btn in self.ratingButtonslist[panel_index][i]:
                btn.setEnabled(True)
        self.check_save_conditions(panel_index, i)

    def onRatingSelected(self, panel_index, i):
        self.check_save_conditions(panel_index, i)

    def check_save_conditions(self, panel_index, i):
        start_time = self.startingTimelist[panel_index][i].text()
        end_time = self.endingTimelist[panel_index][i].text()

        if self.ratingButtonslist[panel_index] and self.ratingButtonslist[panel_index][i]:
            rating_selected = any(btn.isChecked() for btn in self.ratingButtonslist[panel_index][i])
            if start_time != "0" and end_time != "0" and rating_selected:
                if self.saveEntryBtn[panel_index] and len(self.saveEntryBtn[panel_index]) > i:
                    self.saveEntryBtn[panel_index][i].setEnabled(True)
            else:
                if self.saveEntryBtn[panel_index] and len(self.saveEntryBtn[panel_index]) > i:
                    self.saveEntryBtn[panel_index][i].setEnabled(False)

            if self.clearEntryBtn[panel_index] and len(self.clearEntryBtn[panel_index]) > i:
                self.clearEntryBtn[panel_index][i].setEnabled(True)
        else:
            if start_time != "0" and end_time != "0":
                if self.saveEntryBtn[panel_index] and len(self.saveEntryBtn[panel_index]) > i:
                    self.saveEntryBtn[panel_index][i].setEnabled(True)
            else:
                if self.saveEntryBtn[panel_index] and len(self.saveEntryBtn[panel_index]) > i:
                    self.saveEntryBtn[panel_index][i].setEnabled(False)

            if self.clearEntryBtn[panel_index] and len(self.clearEntryBtn[panel_index]) > i:
                self.clearEntryBtn[panel_index][i].setEnabled(True)

    def check_timestamp_save_conditions(self, panel_index, i):
        start_time = self.startingTimelist[panel_index][i].text()
        end_time = self.endingTimelist[panel_index][i].text()
        if start_time != "0" and end_time != "0":
            self.saveEntryBtn[panel_index][i].setEnabled(True)
            self.clearEntryBtn[panel_index][i].setEnabled(True)
        else:
            self.saveEntryBtn[panel_index][i].setEnabled(False)
            self.clearEntryBtn[panel_index][i].setEnabled(False)

    def check_rating_save_conditions(self, panel_index, i):
        start_time = self.startingTimelist[panel_index][i].text()
        end_time = self.endingTimelist[panel_index][i].text()
        rating_selected = any(btn.isChecked() for btn in self.ratingButtonslist[panel_index][i])
        if start_time != "0" and end_time != "0" and rating_selected:
            self.saveEntryBtn[panel_index][i].setEnabled(True)
        else:
            self.saveEntryBtn[panel_index][i].setEnabled(False)
        self.clearEntryBtn[panel_index][i].setEnabled(True)

    def onsaveEntryBtnClicked(self, panel_index, i):
        self.saveEntryBtn[panel_index][i].setEnabled(False)

        form_title = self.form_title[panel_index].text()
        task_name = self.tasklist[panel_index][i].text()
        starting_time = self.startingTimelist[panel_index][i].text()
        ending_time = self.endingTimelist[panel_index][i].text()

        filepath = "{}/{}.txt".format(self.save_directory, form_title)
        with open(filepath, "a") as f:
            f.write("{} : ({} , {})\n".format(task_name, starting_time, ending_time))

        # NEW: dedupe + chronological sort after each save
        self._dedupe_and_sort_file(filepath)

        self.clearEntryBtn[panel_index][i].setEnabled(True)

    def onclearEntryBtnClicked(self, panel_index, i):
        self.startingTimelist[panel_index][i].setText("0")
        self.endingTimelist[panel_index][i].setText("0")

        if self.ratingButtonslist[panel_index]:
            self.groupButtonlist[panel_index][i].setExclusive(False)
            for btn in self.ratingButtonslist[panel_index][i]:
                btn.setChecked(False)
            for btn in self.ratingButtonslist[panel_index][i]:
                btn.setEnabled(False)
            self.groupButtonlist[panel_index][i].setExclusive(True)

        if self.clearEntryBtn[panel_index] and len(self.clearEntryBtn[panel_index]) > i:
            self.clearEntryBtn[panel_index][i].setEnabled(True)

        if self.saveEntryBtn[panel_index] and len(self.saveEntryBtn[panel_index]) > i:
            self.saveEntryBtn[panel_index][i].setEnabled(False)

        self.startingButtonlist[panel_index][i].setEnabled(True)
        self.endingButtonlist[panel_index][i].setEnabled(False)

    def screenshotCall(self):
        if self.mediaPlayer.is_playing():
            self.mediaPlayer.pause()
            self.is_paused = True
            self.playBtn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            self.timer.stop()

        if self.vidname:
            frame_num = int(self.mediaPlayer.get_position() * (self.media.get_duration()))
            self.cap.set(cv2.CAP_PROP_POS_MSEC, frame_num)
            (ret, self.frame) = self.cap.read()
            if self.image_save_directory:
                self.image = self.image_save_directory + "/{}Frame{}.png".format(
                    str(self.vidname).split(".")[-2], str(frame_num)
                )
            cv2.imwrite(self.image, self.frame)

    def buffer_frame(self, image):
        self.ImagesBuffer = image

    def update_ui(self):
        media_pos = int(self.mediaPlayer.get_position() * 100000)
        self.slider.setValue(media_pos)
        if not self.mediaPlayer.is_playing():
            self.timer.stop()
            if not self.is_paused:
                self.stop()

    def stop(self):
        self.mediaPlayer.stop()
        self.playBtn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def set_speed(self, i):
        if i == 0:
            self.mediaPlayer.set_rate(1)
        elif i == 1:
            self.mediaPlayer.set_rate(1.5)
        elif i == 2:
            self.mediaPlayer.set_rate(2)
        elif i == 3:
            self.mediaPlayer.set_rate(3)

    def save(self):
        if self.num_panels != 0:
            for panel_index in range(1, self.num_panels + 1):
                if self.startingButtonlist[panel_index]:
                    for i in range(len(self.startingButtonlist[panel_index])):
                        if self.saveEntryBtn[panel_index][i] and self.saveEntryBtn[panel_index][i].isEnabled():
                            self.onsaveEntryBtnClicked(panel_index, i)

    def annotate(self):
        if self.image_save_directory:
            for json_file in os.listdir(self.image_save_directory):
                if json_file.endswith(".json"):
                    with open(os.path.join(self.image_save_directory, json_file)) as annotation_file:
                        data = json.load(annotation_file)
                        for shape in data["shapes"]:
                            if shape["label"]:
                                if not shape["label"] in self.segmentation_labels:
                                    self.segmentation_labels.add(shape["label"])

        if self.image:
            if len(self.segmentation_labels) == 0:
                subprocess.Popen(
                    [
                        "labelme",
                        "{}".format(self.image),
                        "--output",
                        "{}".format(self.image.replace(self.image.split(".")[-1], "json")),
                    ]
                )
            else:
                labels = ",".join(l for l in self.segmentation_labels)
                print(labels)
                subprocess.Popen(
                    [
                        "labelme",
                        "{}".format(self.image),
                        "--output",
                        "{}".format(self.image.replace(self.image.split(".")[-1], "json")),
                        "--labels",
                        labels,
                    ]
                )

    def save_rating_entry(self, panel_index, i):
        form_title = self.form_title[panel_index].text()
        task_name = self.tasklist[panel_index][i].text()
        starting_time = self.startingTimelist[panel_index][i].text()
        ending_time = self.endingTimelist[panel_index][i].text()

        score = None
        for j in range(len(self.ratingButtonslist[panel_index][i])):
            if self.ratingButtonslist[panel_index][i][j].isChecked():
                score = self.ratingButtonslist[panel_index][i][j].text()

        filepath = "{}/{}_scores.txt".format(self.save_directory, form_title)
        with open(filepath, "a") as f:
            f.write("{} : {} | Time: {} to {}\n".format(task_name, score, starting_time, ending_time))

        # NEW: dedupe + chronological sort after each rating save
        self._dedupe_and_sort_file(filepath)

        if self.saveEntryBtn[panel_index] and len(self.saveEntryBtn[panel_index]) > i:
            self.saveEntryBtn[panel_index][i].setEnabled(False)

    def clearPanels(self):
        if self.num_panels != 0:
            for panel_index in range(1, self.num_panels + 1):
                if self.scroll[panel_index]:
                    self.scroll[panel_index].setParent(None)
                    self.scroll[panel_index].deleteLater()

                self.groupbox[panel_index] = None
                self.formLayout[panel_index] = None
                self.form_title[panel_index] = None
                self.panelRemoveBtn[panel_index] = None
                self.scroll[panel_index] = None
                self.tasklist[panel_index] = None
                self.startingButtonlist[panel_index] = None
                self.startingTimelist[panel_index] = None
                self.endingButtonlist[panel_index] = None
                self.endingTimelist[panel_index] = None
                self.saveEntryBtn[panel_index] = None
                self.clearEntryBtn[panel_index] = None
                self.ratingButtonslist[panel_index] = None
                self.groupButtonlist[panel_index] = None

            self.num_panels = 0

    def close(self):
        sys.exit(app.exec_())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Window()
    sys.exit(app.exec_())