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
import shutil

# Entries in a panel's output file
TIMESTAMP_LINE = r"(.*) : \(([\d:]+) , ([\d:]+)\)$"  # <panel>.txt: "task : (start , end)"
RATING_LINE = r"(.*?) : (.*?) \| Time: ([\d:]+) to ([\d:]+)$"  # <panel>_scores.txt: "task : score | Time: start to end"

# VLC settings that make decoding lighter for videos that lag or skip, at some cost to the picture
PERFORMANCE_OPTIONS = (
    "--avcodec-skiploopfilter=4",  # skip the H.264 loop filter (slightly blockier picture)
    "--avcodec-skip-frame=1",  # don't decode frames nothing else depends on (choppier motion)
    "--avcodec-fast",  # allow the decoder's speed tricks
    "--file-caching=3000",  # read further ahead, for slow disks (ms)
)

# FFmpeg settings for the lighter copy of a video: at most 720p and 30 fps, easy to decode, a keyframe
# every second for smoother seeking, no audio (the player has none). -copyts keeps the original's
# timing, so times marked on the copy match the original even if its first frame doesn't start at 0.
LIGHTER_COPY_ARGS = [
    "-copyts", "-map", "0:v:0", "-an", "-sn", "-dn",
    "-vf", "scale=-2:'min(720,ih)',fps=30",
    "-c:v", "libx264", "-preset", "veryfast", "-tune", "fastdecode", "-crf", "26", "-g", "30",
    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
]  # fmt: skip
LIGHTER_COPY_BYTES_PER_SEC = 400000  # rough size of the copy, for the disk space check
LIGHTER_COPY_MAX_LENGTH_DIFF_MS = 500  # a copy further off than this from the original is rejected


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


class TimeEdit(QLineEdit):
    """Start or end time in a panel row: "H:MM:SS", or "0" while it isn't set.

    Click it to change the time and press Enter (Escape cancels). While it's
    being changed it shows HH:MM:SS with fixed colons, so only the digits can be
    typed over or erased. Leaving it applies the change through editingFinished.
    Double-click it to jump the video to that time.
    """

    jump = pyqtSignal(str)

    def __init__(self, text):
        super().__init__(text)
        self.committed = text  # the time in effect, while a new one is being typed
        self.setToolTip("Click to change the time, then press Enter (Esc cancels).\nDouble-click to jump the video here.")
        # A white input box, so it's clear the time can be edited
        self.setStyleSheet(
            "QLineEdit { color: black; background: white; border: 1px solid gray; padding: 1px 3px; }"
            "QLineEdit:focus { border: 2px solid #3399ff; }"
        )
        self.setFixedWidth(self.fontMetrics().boundingRect("00:00:00").width() + 24)

    def setText(self, text):
        self.committed = text
        self._show_committed()

    def _show_committed(self):
        if self.hasFocus():
            h, m, s = (int(p) for p in self.committed.split(":")) if ":" in self.committed else (0, 0, 0)
            self.setInputMask("00:00:00")
            super().setText("{:02d}:{:02d}:{:02d}".format(h, m, s))
        else:
            self.setInputMask("")
            super().setText(self.committed)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._show_committed()  # switch to the fixed HH:MM:SS form for typing

    def focusOutEvent(self, event):
        if event.reason() == Qt.PopupFocusReason:  # right-click menu: still editing
            super().focusOutEvent(event)
            return
        super().focusOutEvent(event)  # emits editingFinished, which applies or rejects the typed time
        self._show_committed()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.setModified(False)  # nothing typed is applied
            self.clearFocus()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.clearFocus()  # applies the new time and hands the keyboard back to the player
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.setModified(False)
        self.clearFocus()
        self.jump.emit(self.committed)


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
        self._last_time_ms = 0
        self.original_path = None  # the video that was opened
        self.video_path = None  # what's playing: the original or its lighter copy
        self._restarting = False  # the player was restarted and hasn't reported its real time yet
        self._pause_after_restart = False
        self.copyProcess = None  # FFmpeg, while it's making a lighter copy

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

        self.performanceCheck = QCheckBox("Performance mode")
        self.performanceCheck.setStyleSheet("color: white")
        self.performanceCheck.setToolTip(
            "For videos that lag or skip: lowers the picture quality while playing to reduce the load.\n"
            "The video file and snapshots aren't affected."
        )
        self.performanceCheck.setFocusPolicy(Qt.NoFocus)  # so Space keeps controlling play/pause
        self.performanceCheck.toggled.connect(self.toggle_performance_mode)
        bottomLayout.addWidget(self.performanceCheck)

        self.lighterCopyBtn = QPushButton("Make lighter copy")
        self.lighterCopyBtn.setToolTip(
            "For videos that still lag or skip: makes a smaller copy (720p, 30 fps) to play instead.\n"
            "The original isn't changed, annotations stay in the same folder, and snapshots still come from the original."
        )
        self.lighterCopyBtn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.lighterCopyBtn.setFocusPolicy(Qt.NoFocus)  # so Space keeps controlling play/pause
        self.lighterCopyBtn.setEnabled(False)
        self.lighterCopyBtn.clicked.connect(self.on_lighter_copy_clicked)
        bottomLayout.addWidget(self.lighterCopyBtn)

        self.copyProgress = QProgressBar()
        self.copyProgress.setFixedWidth(120)
        self.copyProgress.setStyleSheet("color: white")
        self.copyProgress.hide()
        bottomLayout.addWidget(self.copyProgress)

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
        self.overlapCheckbox = [None] * 10
        # Panels opened from a saved annotation file: that file, and the line each row came from
        self.editFile = [None] * 10
        self.editLines = [None] * 10

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

        openAnnotationsAction = QAction("&Open Saved Annotations", self)
        openAnnotationsAction.setStatusTip("Open a saved timestamp or rating file as a panel to edit its entries")
        openAnnotationsAction.setShortcut("Ctrl+E")
        openAnnotationsAction.triggered.connect(self.open_annotations)

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
        fileMenu.addAction(openAnnotationsAction)
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
    # Event time validation + per-panel "Allow overlapping events" option
    # =========================================================================
    def _add_overlap_checkbox(self, panel_index):
        checkbox = QCheckBox("Allow overlapping events")
        checkbox.setChecked(True)
        checkbox.setStyleSheet("color: white")
        checkbox.setToolTip(
            "When unchecked, events in this panel can't overlap: a start or end "
            "time that falls inside another event of this panel is rejected."
        )
        checkbox.toggled.connect(partial(self.on_overlap_toggled, panel_index))
        self.overlapCheckbox[panel_index] = checkbox
        self.formLayout[panel_index].addRow(checkbox)

    def on_overlap_toggled(self, panel_index, allowed):
        """Refuse to turn overlap off while the panel already has overlapping events."""
        if allowed:
            return
        for i in range(len(self.tasklist[panel_index])):
            start = self.startingTimelist[panel_index][i].text()
            end = self.endingTimelist[panel_index][i].text()
            if start == "0" and end == "0":
                continue
            error = self._check_event_times(panel_index, i, start, end)
            if error:
                QMessageBox.warning(
                    self,
                    "Overlapping events",
                    error + "\n\nClear one of them before turning off overlapping events.",
                )
                self.overlapCheckbox[panel_index].setChecked(True)
                return

    def _check_event_times(self, panel_index, i, start_text, end_text):
        """Return why row i can't have these start/end times, or None if they're fine."""
        start = None if start_text == "0" else self._time_to_seconds(start_text)
        end = None if end_text == "0" else self._time_to_seconds(end_text)
        if start is not None and end is not None and end < start:
            return "End time ({}) is before start time ({}).".format(end_text, start_text)

        if self.overlapCheckbox[panel_index].isChecked():
            return None

        # A row with only one time set is checked as a single point in time
        start = end if start is None else start
        end = start if end is None else end
        for task, other_start, other_end in self._other_events(panel_index, i):
            # Events that only touch (one ends when the next starts) don't overlap
            if start < self._time_to_seconds(other_end) and self._time_to_seconds(other_start) < end:
                return "'{}' would overlap with '{}' ({} to {}).".format(
                    self.tasklist[panel_index][i].text(), task, other_start, other_end
                )
        return None

    def _other_events(self, panel_index, i):
        """(task, start, end) of every event in the panel except row i.

        Rows marked on screen are used as shown. Tasks that aren't marked fall
        back to the times saved in the panel's output file, so events saved in
        an earlier session are checked as well.
        """
        tasks = [label.text() for label in self.tasklist[panel_index]]
        events = []
        for j, task in enumerate(tasks):
            start = self.startingTimelist[panel_index][j].text()
            end = self.endingTimelist[panel_index][j].text()
            if j != i and start != "0" and end != "0":
                events.append((task, start, end))

        on_screen = {task for task, _, _ in events} | {tasks[i]}
        return events + [event for event in self._saved_events(panel_index) if event[0] not in on_screen]

    def _saved_events(self, panel_index):
        """(task, start, end) of the entries already in the panel's output file."""
        if not (self.save_directory or self.editFile[panel_index]):
            return []
        path = self._output_path(panel_index)
        if not os.path.exists(path):
            return []
        pattern = RATING_LINE if self.ratingButtonslist[panel_index] is not None else TIMESTAMP_LINE
        events = []
        with open(path) as f:
            for line in f:
                m = re.match(pattern, line.strip())
                if m:
                    events.append((m.groups()[0], m.groups()[-2], m.groups()[-1]))
        return events

    def _output_path(self, panel_index):
        """File the panel saves to: the annotation file it was opened from, or one named after the panel."""
        if self.editFile[panel_index]:
            return self.editFile[panel_index]
        suffix = "_scores.txt" if self.ratingButtonslist[panel_index] is not None else ".txt"
        return "{}/{}{}".format(self.save_directory, self.form_title[panel_index].text(), suffix)

    def _replace_saved_entry(self, panel_index, i, new_line):
        """Swap row i's entry in its opened annotation file for new_line, or remove it if new_line is None."""
        path = self.editFile[panel_index]
        lines = []
        if os.path.exists(path):
            with open(path) as f:
                lines = [line.strip() for line in f if line.strip()]
        if self.editLines[panel_index][i] in lines:
            lines.remove(self.editLines[panel_index][i])
        if new_line is not None:
            lines.append(new_line)
        with open(path, "w") as f:
            f.writelines(line + "\n" for line in lines)
        self._dedupe_and_sort_file(path)
        self.editLines[panel_index][i] = new_line

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
        elif event.key() in (Qt.Key_Right, Qt.Key_Left):
            duration = self._duration_ms()
            if duration > 0:
                step = int(5000 * 100000 / duration)  # 5 seconds in slider units
                if event.key() == Qt.Key_Left:
                    step = -step
                self.set_position(min(100000, max(0, self.slider.value() + step)))
        elif event.key() == Qt.Key_F5:
            self.close()
        else:
            super().keyPressEvent(event)

    def open_video(self):
        if self.copyProcess is not None and not self._cancel_lighter_copy():
            return

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

            # A lighter copy made earlier is played instead, and it can be switched off with the button
            self.original_path = filename
            self.video_path = self._lighter_copy_path() if os.path.exists(self._lighter_copy_path()) else filename
            self._set_media()
            self._last_time_ms = 0
            self._update_video_title()

            self.playBtn.setEnabled(True)

            self._attach_video_output()

            self.cap = cv2.VideoCapture(filename)  # snapshots always come from the original
            self.play_video()

            self.d.setText(str(self.getDurationValue()))
            self._update_lighter_copy_button()

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

    def _set_media(self, *options):
        self.media = self.instance.media_new(self.video_path, *options)
        self.mediaPlayer.set_media(self.media)
        self.media.parse()

    def _attach_video_output(self):
        if platform.system() == "Windows":
            self.mediaPlayer.set_hwnd(int(self.videowidget.winId()))
        elif platform.system() == "Darwin":
            self.mediaPlayer.set_nsobject(int(self.videowidget.winId()))
        else:
            self.mediaPlayer.set_xwindow(int(self.videowidget.winId()))

    def toggle_performance_mode(self, enabled):
        """Restart the player with or without PERFORMANCE_OPTIONS, keeping the place in the video.

        VLC only reads these options when it starts.
        """
        instance = vlc.Instance("--no-audio", *(PERFORMANCE_OPTIONS if enabled else ()))
        if instance is None:
            QMessageBox.warning(self, "Performance mode", "This version of VLC doesn't support performance mode.")
            self.performanceCheck.blockSignals(True)
            self.performanceCheck.setChecked(False)
            self.performanceCheck.blockSignals(False)
            return

        state = self.mediaPlayer.get_state()
        time_ms = self._current_time_ms()
        self._stop_for_restart(state)
        self.mediaPlayer.release()
        self.instance.release()

        self.instance = instance
        self.mediaPlayer = self.instance.media_player_new()
        self._attach_video_output()
        self.set_speed(self.playbackspeedBtn.currentIndex())
        if self.media is not None:
            self._resume_media(state, time_ms)

    def _stop_for_restart(self, state):
        self.timer.stop()
        if state == vlc.State.Paused:
            self.mediaPlayer.set_pause(0)  # VLC 3.0.12 can crash when stopped while paused
        self.mediaPlayer.stop()
        if self.media is not None:
            self.media.release()

    def _resume_media(self, state, time_ms):
        """Load video_path and carry on from time_ms in the state the old player was in."""
        if state not in (vlc.State.Paused, vlc.State.Opening, vlc.State.Buffering, vlc.State.Playing):
            self._set_media()  # stopped: stay ready to play from the start
            return
        self._set_media(":start-time={}".format(time_ms / 1000))
        # VLC reports time 0 when it starts paused, so it's paused in update_ui once it has really started
        self._restarting = True
        self._pause_after_restart = state == vlc.State.Paused
        self.mediaPlayer.play()
        self.timer.start()

    # =========================================================================
    # Lighter copy of a video, made with FFmpeg, for computers that can't play the original smoothly
    # =========================================================================
    def _lighter_copy_path(self):
        return "{}/lighter_copy.mp4".format(self.save_directory)

    def _update_video_title(self):
        if self.video_path == self.original_path:
            self.setWindowTitle(self.media.get_meta(0))
        else:
            self.setWindowTitle("{} (lighter copy)".format(self.vidname))

    def _update_lighter_copy_button(self):
        if self.copyProcess is not None:
            text = "Cancel lighter copy"
        elif self.video_path != self.original_path:
            text = "Play original"
        elif self.save_directory and os.path.exists(self._lighter_copy_path()):
            text = "Play lighter copy"
        else:
            text = "Make lighter copy"
        self.lighterCopyBtn.setText(text)
        self.lighterCopyBtn.setEnabled(self.original_path is not None)

    def on_lighter_copy_clicked(self):
        if self.copyProcess is not None:
            self._cancel_lighter_copy()
        elif self.video_path != self.original_path:
            self._switch_video(self.original_path)
        elif os.path.exists(self._lighter_copy_path()):
            self._switch_video(self._lighter_copy_path())
        else:
            self._start_lighter_copy()

    def _switch_video(self, path):
        """Play the original or the lighter copy instead, from the same time."""
        state = self.mediaPlayer.get_state()
        time_ms = self._current_time_ms()
        self._stop_for_restart(state)
        self.video_path = path
        self._resume_media(state, time_ms)
        self._update_video_title()
        self._update_lighter_copy_button()

    def _start_lighter_copy(self):
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            QMessageBox.warning(
                self,
                "FFmpeg not found",
                "To make lighter copies, run this in Anaconda Prompt, then restart SurgUI:\n\npip install imageio-ffmpeg",
            )
            return

        self.copyDurationMs = self._duration_ms()
        needed = self.copyDurationMs // 1000 * LIGHTER_COPY_BYTES_PER_SEC
        if shutil.disk_usage(self.save_directory).free < needed:
            QMessageBox.warning(
                self,
                "Not enough disk space",
                "The copy needs about {} MB of free space.".format(needed // 1000000),
            )
            return

        self.copyTarget = self._lighter_copy_path()
        self.copyPart = "{}/lighter_copy.part.mp4".format(self.save_directory)
        self.copyCancelled = False
        self.copyProgress.setRange(0, 100 if self.copyDurationMs else 0)  # 0, 0: busy without a percentage
        self.copyProgress.setValue(0)
        self.copyProgress.show()

        self.copyProcess = QProcess(self)
        self.copyProcess.readyReadStandardOutput.connect(self._on_lighter_copy_output)
        self.copyProcess.finished.connect(self._on_lighter_copy_finished)
        self.copyProcess.errorOccurred.connect(self._on_lighter_copy_error)
        self.copyProcess.start(
            ffmpeg,
            ["-y", "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
            + ["-i", self.original_path]
            + LIGHTER_COPY_ARGS
            + [self.copyPart],
        )
        self._update_lighter_copy_button()

    def _on_lighter_copy_output(self):
        if self.copyProcess is None:
            return
        text = bytes(self.copyProcess.readAllStandardOutput()).decode("ascii", "ignore")
        done = re.findall(r"out_time_ms=(\d+)", text)  # despite its name, in microseconds
        if done and self.copyDurationMs:
            self.copyProgress.setValue(min(99, int(done[-1]) // 10 // self.copyDurationMs))

    def _on_lighter_copy_error(self, error):
        if error == QProcess.FailedToStart:
            self._on_lighter_copy_finished(-1, QProcess.CrashExit)

    def _on_lighter_copy_finished(self, exit_code, exit_status):
        process, self.copyProcess = self.copyProcess, None
        if process is None:
            return
        self.copyProgress.hide()
        error = None
        if self.copyCancelled:
            pass
        elif exit_code != 0 or exit_status != QProcess.NormalExit:
            error = "FFmpeg couldn't convert this video.\n\n" + bytes(process.readAllStandardError()).decode(errors="replace")[-400:]
        else:
            error = self._check_lighter_copy()
        process.deleteLater()

        if self.copyCancelled or error:
            try:
                if os.path.exists(self.copyPart):
                    os.remove(self.copyPart)
            except OSError as e:
                print("Could not delete {}: {}".format(self.copyPart, e))
        self._update_lighter_copy_button()
        if error:
            QMessageBox.warning(self, "Lighter copy not made", error)
        elif not self.copyCancelled:
            reply = QMessageBox.question(self, "Lighter copy ready", "Switch to the lighter copy now?")
            if reply == QMessageBox.Yes and self.video_path == self.original_path:
                self._switch_video(self.copyTarget)

    def _check_lighter_copy(self):
        """Put the finished copy in place if its length matches the original, or return what's wrong."""
        media = self.instance.media_new(self.copyPart)
        media.parse()
        length = media.get_duration()
        media.release()
        if self.copyDurationMs and abs(length - self.copyDurationMs) > LIGHTER_COPY_MAX_LENGTH_DIFF_MS:
            return (
                "The copy's length ({}) doesn't match the original ({}), so times marked on it could be wrong. "
                "Please use the original video, with Performance mode if it helps.".format(
                    self._format_ms(max(0, length)), self._format_ms(self.copyDurationMs)
                )
            )
        try:
            os.replace(self.copyPart, self.copyTarget)
        except OSError as e:
            return "Could not save the copy: {}".format(e)
        return None

    def _cancel_lighter_copy(self):
        """Ask, then stop making the copy. Returns False if the user wants it to carry on."""
        reply = QMessageBox.question(self, "Lighter copy", "A lighter copy is still being made. Cancel it?")
        if reply != QMessageBox.Yes:
            return False
        if self.copyProcess is not None:  # it may have finished while the question was open
            self.copyCancelled = True
            self.copyProcess.kill()
            self.copyProcess.waitForFinished(5000)
        return True

    def closeEvent(self, event):
        if self.copyProcess is not None and not self._cancel_lighter_copy():
            event.ignore()
            return
        super().closeEvent(event)

    def play_video(self):
        self._pause_after_restart = False  # the user decides from here
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
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Folder")
        if not directory:
            return  # dialog cancelled, keep the current folder
        self.parrentDirectory = directory

        if self.vidname:
            if not os.path.exists(self.parrentDirectory + "/" + self.vidname):
                os.mkdir(self.parrentDirectory + "/" + self.vidname)
            self.save_directory = self.parrentDirectory + "/" + self.vidname

            if not os.path.exists(self.parrentDirectory + "/" + self.vidname + "/images"):
                os.mkdir(self.parrentDirectory + "/" + self.vidname + "/images")
            self.image_save_directory = self.parrentDirectory + "/" + self.vidname + "/images"

            print("Saving directory: ", self.parrentDirectory + "/" + self.vidname)
            self._update_lighter_copy_button()

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

            self._add_time_panel(title, lines)

    def _add_time_panel(self, title, lines):
        """Add a timestamping panel with one row per line ("task#start tip#end tip")."""
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
        self._add_overlap_checkbox(self.panel_index)

        for i, line in enumerate(lines):
            line = line.split("#")

            self.tasklist[self.panel_index].append(QLabel(line[0]))
            self.tasklist[self.panel_index][i].setStyleSheet("background-color: black ; color: white")
            self.tasklist[self.panel_index][i].setFont(QFont("Times", 10, weight=QFont.Bold))
            self.tasklist[self.panel_index][i].setWordWrap(True)

            self.startingButtonlist[self.panel_index].append(QPushButton("starts"))
            self.startingButtonlist[self.panel_index][i].setFixedWidth(50)
            self.startingTimelist[self.panel_index].append(TimeEdit("0"))
            self.startingTimelist[self.panel_index][i].jump.connect(self.go_to_time)
            self.startingTimelist[self.panel_index][i].editingFinished.connect(
                partial(self.on_time_edited, self.panel_index, i, "start")
            )
            self.startingButtonlist[self.panel_index][i].clicked.connect(
                partial(self.onstartbuttonClicked, self.panel_index, i)
            )

            self.endingButtonlist[self.panel_index].append(QPushButton("ends"))
            self.endingButtonlist[self.panel_index][i].setFixedWidth(50)
            self.endingTimelist[self.panel_index].append(TimeEdit("0"))
            self.endingTimelist[self.panel_index][i].jump.connect(self.go_to_time)
            self.endingTimelist[self.panel_index][i].editingFinished.connect(
                partial(self.on_time_edited, self.panel_index, i, "end")
            )
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

            self._add_rating_panel(title, lines)

            if self.save_directory:
                scores_path = self.save_directory + "/" + self.form_title[self.panel_index].text() + "_scores.txt"
                if os.path.exists(scores_path):
                    # Restore the saved scores. Lines are "task : score | Time: start to end",
                    # or just "task : score" in older files. The file itself is left as is.
                    with open(scores_path, "r") as f:
                        lines = f.readlines()
                    for line in lines:
                        parts = line.strip().split(" : ", 1)
                        if len(parts) != 2:
                            continue
                        rating_item = parts[0]
                        score = parts[1].split(" | ")[0].strip()
                        if not score.isdigit():
                            print(f"Skipping invalid score: {score}")
                            continue
                        for i in range(len(self.groupButtonlist[self.panel_index])):
                            if self.tasklist[self.panel_index][i].text() == rating_item:
                                for btn in self.ratingButtonslist[self.panel_index][i]:
                                    if int(btn.text()) == int(score):
                                        btn.setChecked(True)

                    # NEW: clean up the existing entries
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

    def _add_rating_panel(self, title, lines):
        """Add a rating panel with one row per line ("task : number of scores")."""
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
        self._add_overlap_checkbox(self.panel_index)

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
            self.startingTimelist[self.panel_index].append(TimeEdit("0"))
            self.startingTimelist[self.panel_index][i].jump.connect(self.go_to_time)
            self.startingTimelist[self.panel_index][i].editingFinished.connect(
                partial(self.on_time_edited, self.panel_index, i, "start")
            )
            self.startingButtonlist[self.panel_index][i].clicked.connect(
                partial(self.onstartbuttonClicked, self.panel_index, i)
            )

            self.endingButtonlist[self.panel_index].append(QPushButton("ends"))
            self.endingButtonlist[self.panel_index][i].setFixedWidth(50)
            self.endingTimelist[self.panel_index].append(TimeEdit("0"))
            self.endingTimelist[self.panel_index][i].jump.connect(self.go_to_time)
            self.endingTimelist[self.panel_index][i].editingFinished.connect(
                partial(self.on_time_edited, self.panel_index, i, "end")
            )
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

    def set_time_ms(self, ms):
        self.timer.stop()
        self.mediaPlayer.set_time(int(ms))
        self.timer.start()

    def go_to_time(self, time_text):
        """Jump the video to a time shown in a panel row (it was double-clicked)."""
        if self.media is not None and time_text != "0":
            self.set_time_ms(self._time_to_seconds(time_text) * 1000)

    def open_annotations(self):
        """Open a saved timestamp or rating file as a panel with one row per entry.

        Saving a row (or picking a score, in a rating file) replaces that entry
        in the file, and clearing a row removes it.
        """
        start_dir = self.save_directory or self.parrentDirectory or "./outputs"
        filename, _ = QFileDialog.getOpenFileName(self, "Open Saved Annotations", start_dir, "Text files (*.txt)")
        if filename == "":
            return

        with open(filename) as f:
            lines = [line.strip() for line in f if line.strip()]
        is_rating = filename.endswith("_scores.txt") or any("| Time:" in line for line in lines)
        entries = []  # (line, its fields)
        for line in lines:
            m = re.match(RATING_LINE if is_rating else TIMESTAMP_LINE, line)
            if m:
                entries.append((line, m.groups()))
        if not entries:
            QMessageBox.warning(
                self,
                "Not an annotation file",
                "No saved entries were found in this file. Pick a file from the outputs folder, "
                "like <panel>.txt or <panel>_scores.txt.",
            )
            return

        title = "Editing " + os.path.basename(filename)
        if is_rating:
            # The rating scale isn't saved with the scores: show 1-5, or up to the highest score used
            top = max([5] + [int(fields[1]) for _, fields in entries if fields[1].isdigit()])
            self._add_rating_panel(title, ["{} : {}".format(fields[0], top - 1) for _, fields in entries])
        else:
            self._add_time_panel(title, [fields[0] for _, fields in entries])

        p = self.panel_index
        self.editFile[p] = filename
        self.editLines[p] = [line for line, _ in entries]
        for i, (_, fields) in enumerate(entries):
            self.startingTimelist[p][i].setText(fields[-2])
            self.endingTimelist[p][i].setText(fields[-1])
            self.endingButtonlist[p][i].setEnabled(True)
            self.clearEntryBtn[p][i].setEnabled(True)
            if is_rating:
                for btn in self.ratingButtonslist[p][i]:
                    btn.setEnabled(True)
                    if btn.text() == fields[1]:
                        btn.setChecked(True)

        if len(entries) < len(lines):
            QMessageBox.information(
                self,
                "Some lines not loaded",
                "{} line(s) in this file aren't in the expected format. "
                "They're left in the file as they are.".format(len(lines) - len(entries)),
            )

    def handle_errors(self):
        self.playBtn.setEnabled(False)
        self.label.setText("Error: " + self.mediaPlayer.errorString())

    def _duration_ms(self):
        """Video length in ms, or 0 while it isn't known (VLC reports -1)."""
        if self.media is None:
            return 0
        duration = self.media.get_duration()
        if duration <= 0:
            duration = self.mediaPlayer.get_length()
        return max(0, duration)

    def _current_time_ms(self):
        """Playback time in ms, never negative.

        VLC reports -1 once the player is stopped, which update_ui does when
        the video reaches its end, so marking an end time there used to give a
        negative time. Fall back to the last valid time instead.
        """
        if self.media is None:
            return 0
        if self.mediaPlayer.get_state() == vlc.State.Ended:
            t = self._duration_ms() or self._last_time_ms
        else:
            t = self.mediaPlayer.get_time()
            if t < 0 or (self._restarting and t == 0):
                t = self._last_time_ms
        duration = self._duration_ms()
        if duration:
            t = min(t, duration)
        self._last_time_ms = t
        return t

    def _format_ms(self, ms):
        mins, sec = divmod(ms // 1000, 60)
        hour, mins = divmod(mins, 60)
        return "%d:%02d:%02d" % (hour, mins, sec)

    def getSliderValue(self):
        return self._format_ms(self._current_time_ms())

    def getDurationValue(self):
        return self._format_ms(self._duration_ms())

    def onstartbuttonClicked(self, panel_index, i):
        self._set_start_time(panel_index, i, self.getSliderValue())

    def onendbuttonClicked(self, panel_index, i):
        self._set_end_time(panel_index, i, self.getSliderValue())

    def _set_start_time(self, panel_index, i, value):
        error = self._check_event_times(panel_index, i, value, self.endingTimelist[panel_index][i].text())
        if error:
            QMessageBox.warning(self, "Start time not set", error)
            return
        self.startingTimelist[panel_index][i].setText(str(value))
        self.endingButtonlist[panel_index][i].setEnabled(True)
        self.check_save_conditions(panel_index, i)
        self._save_rating_times_if_editing(panel_index, i)

    def _set_end_time(self, panel_index, i, value):
        error = self._check_event_times(panel_index, i, self.startingTimelist[panel_index][i].text(), value)
        if error:
            QMessageBox.warning(self, "End time not set", error)
            return
        self.endingTimelist[panel_index][i].setText(str(value))
        if self.ratingButtonslist[panel_index] and len(self.ratingButtonslist[panel_index]) > i:
            for btn in self.ratingButtonslist[panel_index][i]:
                btn.setEnabled(True)
        self.check_save_conditions(panel_index, i)
        self._save_rating_times_if_editing(panel_index, i)

    def on_time_edited(self, panel_index, i, which):
        """A new start or end time was typed into a panel row."""
        field = (self.startingTimelist if which == "start" else self.endingTimelist)[panel_index][i]
        if not field.isModified():
            return  # clicked in and out, or Escape
        field.setModified(False)
        typed = field.text()  # HH:MM:SS, with any erased digits missing
        if not re.fullmatch(r"\d\d:[0-5]\d:[0-5]\d", typed):
            QMessageBox.warning(
                self,
                "Time not changed",
                "{} isn't a complete time. Fill in every digit of HH:MM:SS; "
                "minutes and seconds go up to 59.".format(field.displayText()),
            )
            return
        value = self._format_ms(self._time_to_seconds(typed) * 1000)
        duration = self._duration_ms()
        if duration and self._time_to_seconds(value) * 1000 > duration:
            QMessageBox.warning(
                self, "Time not changed", "{} is after the end of the video ({}).".format(value, self.getDurationValue())
            )
            return
        if value == field.committed:
            return
        if which == "start":
            self._set_start_time(panel_index, i, value)
        elif not self.endingButtonlist[panel_index][i].isEnabled():
            QMessageBox.warning(self, "Time not changed", "Set the start time first.")
        else:
            self._set_end_time(panel_index, i, value)

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

    def _has_save_directory(self, panel_index):
        """Entries are saved in a folder named after the video, so one must be open.
        Panels opened from a saved annotation file save back to that file instead."""
        if self.save_directory or self.editFile[panel_index]:
            return True
        QMessageBox.warning(self, "No video open", "Open a video before saving entries.")
        return False

    def onsaveEntryBtnClicked(self, panel_index, i):
        if not self._has_save_directory(panel_index):
            return
        self.saveEntryBtn[panel_index][i].setEnabled(False)

        task_name = self.tasklist[panel_index][i].text()
        starting_time = self.startingTimelist[panel_index][i].text()
        ending_time = self.endingTimelist[panel_index][i].text()
        line = "{} : ({} , {})".format(task_name, starting_time, ending_time)

        if self.editFile[panel_index]:
            self._replace_saved_entry(panel_index, i, line)
        else:
            filepath = self._output_path(panel_index)
            with open(filepath, "a") as f:
                f.write(line + "\n")

            # NEW: dedupe + chronological sort after each save
            self._dedupe_and_sort_file(filepath)

        self.clearEntryBtn[panel_index][i].setEnabled(True)

    def onclearEntryBtnClicked(self, panel_index, i):
        if self.editFile[panel_index] and self.editLines[panel_index][i]:
            reply = QMessageBox.question(
                self,
                "Remove entry",
                "Remove this entry from {}?\n\n{}\n\nYou can mark and save it again afterwards.".format(
                    os.path.basename(self.editFile[panel_index]), self.editLines[panel_index][i]
                ),
            )
            if reply != QMessageBox.Yes:
                return
            self._replace_saved_entry(panel_index, i, None)

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
            frame_num = self._current_time_ms()
            self.cap.set(cv2.CAP_PROP_POS_MSEC, frame_num)
            (ret, self.frame) = self.cap.read()
            if not ret:
                # e.g. at the very end of the video, where there's no frame left to read
                QMessageBox.warning(self, "Snapshot not saved", "Could not read the video frame at this time.")
                return
            if self.image_save_directory:
                self.image = self.image_save_directory + "/{}Frame{}.png".format(
                    str(self.vidname).split(".")[-2], str(frame_num)
                )
            cv2.imwrite(self.image, self.frame)

    def buffer_frame(self, image):
        self.ImagesBuffer = image

    def update_ui(self):
        state = self.mediaPlayer.get_state()
        if state in (vlc.State.NothingSpecial, vlc.State.Opening, vlc.State.Buffering):
            return  # still starting up, don't mistake it for having stopped
        if self._restarting and self.mediaPlayer.get_time() > 0:
            self._restarting = False
            if self._pause_after_restart:
                self._pause_after_restart = False
                self.mediaPlayer.set_pause(1)
        # Read the time now: once the player is stopped VLC only reports -1
        self._current_time_ms()
        self.d.setText(self.getDurationValue())
        media_pos = self.mediaPlayer.get_position()
        # Leave the slider alone while the user holds it: setValue on a pressed
        # slider emits sliderMoved, which would seek back to VLC's position
        if media_pos >= 0 and not self.slider.isSliderDown() and not self._restarting:
            self.slider.setValue(int(media_pos * 100000))
        if not self.mediaPlayer.is_playing():
            self.timer.stop()
            if not self.is_paused:
                self.stop()

    def stop(self):
        self._restarting = False
        self._pause_after_restart = False
        self.mediaPlayer.stop()
        self._set_media()  # drop any start-time from performance mode, so the next play starts from the beginning
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
        # Finish a time that's still being typed, so it's saved too
        focused = QApplication.focusWidget()
        if isinstance(focused, TimeEdit):
            focused.clearFocus()
        for panel_index in range(1, self.num_panels + 1):
            # Rating panels have no save buttons: their entries are saved when a score is picked
            for i, btn in enumerate(self.saveEntryBtn[panel_index] or []):
                if btn.isEnabled():
                    if not self._has_save_directory(panel_index):
                        return
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
        if not self._has_save_directory(panel_index):
            return
        task_name = self.tasklist[panel_index][i].text()
        starting_time = self.startingTimelist[panel_index][i].text()
        ending_time = self.endingTimelist[panel_index][i].text()

        score = None
        for j in range(len(self.ratingButtonslist[panel_index][i])):
            if self.ratingButtonslist[panel_index][i][j].isChecked():
                score = self.ratingButtonslist[panel_index][i][j].text()
        line = "{} : {} | Time: {} to {}".format(task_name, score, starting_time, ending_time)

        if self.editFile[panel_index]:
            self._replace_saved_entry(panel_index, i, line)
        else:
            filepath = self._output_path(panel_index)
            with open(filepath, "a") as f:
                f.write(line + "\n")

            # NEW: dedupe + chronological sort after each rating save
            self._dedupe_and_sort_file(filepath)

        if self.saveEntryBtn[panel_index] and len(self.saveEntryBtn[panel_index]) > i:
            self.saveEntryBtn[panel_index][i].setEnabled(False)

    def _save_rating_times_if_editing(self, panel_index, i):
        """Rating rows have no save button, so in an opened annotation file a re-marked time is saved right away."""
        if self.editFile[panel_index] and self.ratingButtonslist[panel_index] is not None:
            start = self.startingTimelist[panel_index][i].text()
            end = self.endingTimelist[panel_index][i].text()
            if start != "0" and end != "0" and any(btn.isChecked() for btn in self.ratingButtonslist[panel_index][i]):
                self.save_rating_entry(panel_index, i)

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
                self.overlapCheckbox[panel_index] = None
                self.editFile[panel_index] = None
                self.editLines[panel_index] = None

            self.num_panels = 0


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Window()
    sys.exit(app.exec_())