"""Image viewer: a zoom/pan QGraphicsView that shows one photograph and draws
the laser's own rows on top of it.

Display only. It reports nothing and measures nothing - the overlay lines are
computed by the main window from the laser data and handed here already in
pixel rows, so this widget never needs a scale, a calibration or a click.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsLineItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QWidget, QVBoxLayout,
)

from . import styles

OVERLAY_PEN_WIDTH = 3
OVERLAY_FONT_SCALE = 4.0


class ImageView(QWidget):
    """Displays one photograph; fit, zoom, pan, and the laser overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self):
        self.scene = QGraphicsScene(self)
        self.view = _ZoomableView(self.scene, self)

    # --- Layout ---
    def _create_layouts(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.view)

    # --- Signals & Slots ---
    def _connect_signals(self):
        pass

    # --- Styling ---
    def _apply_styles(self):
        self.view.setStyleSheet(f"background-color: {styles.BORDER_COLOR}; border: none;")

    # --- Initial State ---
    def _load_initial_state(self):
        self._pixmap_item = None
        self._overlay_lines: list[tuple[float, str, str]] = []  # (row_px, label, color)
        self._overlay_items: list = []

    # --- Business Logic ---








    # bar overlay -------------------------------------------------------------



    def show_array(self, bgr: np.ndarray):
        """Display a BGR numpy image (undistorted path)."""
        h, w = bgr.shape[:2]
        rgb = np.ascontiguousarray(bgr[:, :, ::-1])
        img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        self._set_pixmap(QPixmap.fromImage(img.copy()))

    def show_file(self, path: Path):
        """Display a JPEG straight from disk (raw path)."""
        self._set_pixmap(QPixmap(str(path)))

    def clear(self):
        """Show nothing. Used when a camera has no frame on this trigger -
        an empty view is honest, a stale one from the previous trigger is not.
        """
        self._set_pixmap(QPixmap())

    def _set_pixmap(self, pixmap: QPixmap):
        self._overlay_items.clear()  # scene.clear() below deletes the items
        self.scene.clear()
        self._pixmap_item = self.scene.addPixmap(pixmap)
        self.scene.setSceneRect(self._pixmap_item.boundingRect())
        self._draw_overlay_lines()
        self.view.fit_to_scene()

    def set_overlay_lines(self, lines: list[tuple[float, str, str]]):
        """Horizontal marker lines: (row_px, label, color). Rows outside the
        image are skipped. Redraws immediately if an image is shown."""
        self._overlay_lines = list(lines)
        self._redraw_overlay_lines()

    def _redraw_overlay_lines(self):
        for item in self._overlay_items:
            if item.scene() is not None:
                self.scene.removeItem(item)
        self._overlay_items.clear()
        self._draw_overlay_lines()

    def _draw_overlay_lines(self):
        if self._pixmap_item is None:
            return
        rect = self._pixmap_item.boundingRect()
        for entry in self._overlay_lines:
            row, label, color = entry[0], entry[1], entry[2]
            weight = entry[3] if len(entry) > 3 else OVERLAY_PEN_WIDTH
            if not (0 <= row <= rect.height()):
                continue
            self._overlay_items.extend(
                self._create_overlay_line_items(row, label, color, rect.width(), weight))

    def _create_overlay_line_items(self, row: float, label: str, color: str,
                                   width: float, weight: float = OVERLAY_PEN_WIDTH) -> list:
        """Factory method — creates the line (+ label, if any) for ONE overlay line."""
        pen = QPen(QColor(color), weight)
        pen.setStyle(Qt.DashLine)
        line = QGraphicsLineItem(0, row, width, row)
        line.setPen(pen)
        self.scene.addItem(line)
        if not label:
            return [line]          # unlabelled: the thin per-scan marks
        text = QGraphicsSimpleTextItem(label)
        text.setBrush(QColor(color))
        text.setScale(OVERLAY_FONT_SCALE)
        text_h = text.boundingRect().height() * OVERLAY_FONT_SCALE
        text.setPos(10, row - text_h - 4 if row > text_h + 8 else row + 4)
        self.scene.addItem(text)
        return [line, text]

    def fit(self):
        self.view.fit_to_scene()


class _ZoomableView(QGraphicsView):
    """QGraphicsView with wheel zoom and drag pan."""

    ZOOM_STEP = 1.25

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._pan_from = None


    def fit_to_scene(self):
        if not self.scene().sceneRect().isEmpty():
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        factor = self.ZOOM_STEP if event.angleDelta().y() > 0 else 1 / self.ZOOM_STEP
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._pan_from = event.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_from is not None:
            delta = event.position() - self._pan_from
            self._pan_from = event.position()
            h, v = self.horizontalScrollBar(), self.verticalScrollBar()
            h.setValue(h.value() - int(delta.x()))
            v.setValue(v.value() - int(delta.y()))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._pan_from is not None:
            self._pan_from = None
            self.viewport().setCursor(Qt.ArrowCursor)
            return
        super().mouseReleaseEvent(event)
