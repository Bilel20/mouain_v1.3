# -*- coding: utf-8 -*-
"""واجهة إدارة المخططات التعليمية."""
import json
import html
import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path
import sys
from PySide6.QtCore import (
    QDate, QEvent, QMarginsF, QMimeData, QStandardPaths, Signal,
    QStringListModel, QTimer, Qt,
)
from PySide6.QtGui import (
    QColor, QDrag, QFont, QPageLayout, QPageSize,
    QStandardItem, QStandardItemModel, QTextCharFormat, QTextCursor,
    QTextDocument, QTextFormat, QTextOption,
)
from PySide6.QtPrintSupport import QPrinter
from app_config import DETAIL_KEYS_BY_NAMAT, PLANS_NAMES, load_custom_font
from database import PLAN_ACTIVITY_ALIASES
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QBoxLayout,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QFileDialog,
    QPlainTextEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)


WEEKDAYS = (
    ("الإثنين", 0), ("الثلاثاء", 1), ("الأربعاء", 2),
    ("الخميس", 3), ("الجمعة", 4), ("السبت", 5), ("الأحد", 6),
)

PDF_ACTIVITY_CARD_COLORS = (
    ("#2563EB", "#EFF6FF"),
    ("#059669", "#ECFDF5"),
    ("#D97706", "#FFFBEB"),
    ("#7C3AED", "#F5F3FF"),
    ("#DB2777", "#FDF2F8"),
    ("#0891B2", "#ECFEFF"),
    ("#4F46E5", "#EEF2FF"),
    ("#65A30D", "#F7FEE7"),
)
PDF_LESSON_NUMBER_COLORS = {
    "منهجي": ("#1D4ED8", "#DBEAFE"),
    "إدماج": ("#059669", "#ECFDF5"),
    "تقييم": ("#D97706", "#FFFBEB"),
    "دعم وعلاج": ("#7C3AED", "#F5F3FF"),
}
PDF_WIDGET_SPACING = "5"


def _pdf_activity_card_colors(activity_name):
    normalized_name = str(activity_name or "").strip().casefold()
    digest = hashlib.sha256(normalized_name.encode("utf-8")).digest()
    color_index = int.from_bytes(digest[:4], "big") % len(
        PDF_ACTIVITY_CARD_COLORS
    )
    return PDF_ACTIVITY_CARD_COLORS[color_index]


def _keep_activity_widgets_on_same_page(document, page_size):
    page_width = page_size.width()
    page_height = page_size.height()
    if page_width <= 0 or page_height <= 0:
        return

    layout = document.documentLayout()
    always_before = QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore

    for _ in range(document.blockCount() + 1):
        layout.documentSize()
        activity_tables = []

        def collect_tables(frame):
            for child in frame.childFrames():
                if child.__class__.__name__ == "QTextTable":
                    rect = layout.frameBoundingRect(child)
                    is_widget_lesson_row = (
                        child.rows() == 1
                        and child.columns() >= 3
                        and child.columns() % 2 == 1
                        and rect.width() >= page_width * 0.8
                    )
                    is_activity_cards = (
                        child.rows() == 1
                        and bool(child.childFrames())
                        and rect.width() >= page_width * 0.7
                    )
                    if is_widget_lesson_row or is_activity_cards:
                        activity_tables.append((child, rect))
                collect_tables(child)

        collect_tables(document.rootFrame())
        moved_table = False
        for table, rect in activity_tables:
            if rect.height() >= page_height:
                continue
            offset_in_page = rect.y() % page_height
            if (
                offset_in_page + rect.height() > page_height + 0.1
                and not table.format().pageBreakPolicy() & always_before
            ):
                table_format = table.format()
                table_format.setPageBreakPolicy(always_before)
                table.setFormat(table_format)
                moved_table = True
                break
        if not moved_table:
            break


def _pdf_text(value):
    return html.escape(str(value or "").strip()).replace("\n", "<br/>")


def _pdf_inline_text(value):
    return html.escape(" ".join(str(value or "").split()))


def _pdf_rounded_box_html(
    content, border_color, background_color, text_align, width="100%"
):
    return (
        f"<table border='1' style='border-color:{border_color}; "
        "border-collapse:collapse;' align='right' "
        f"width='{width}' cellspacing='0' cellpadding='0' "
        f"bgcolor='{background_color}'>"
        "<tr>"
        f"<td bgcolor='{background_color}' dir='rtl' "
        f"align='{text_align}' style='padding:4px; "
        f"text-align:{text_align};'>{content}</td>"
        "</tr></table>"
    )


def _pdf_detail_text(detail, text_key, name_key):
    return str(detail.get(text_key) or detail.get(name_key) or "").strip()


def _teaching_plan_name(snapshot):
    return str(snapshot["plan"].get("title") or "المخطط").strip()


def _teaching_plan_pdf_html(snapshot, page_width_px):
    plan = snapshot["plan"]
    plan_number = plan.get("plan_number")
    class_name = plan.get("class_name")
    title = (
        f"مخطط {_teaching_plan_name(snapshot)} - عدد "
        f"{plan_number if plan_number is not None else 'غير محدد'} "
        f"القسم - {class_name or ''}"
    )
    content = [
        "<html><head><meta charset='utf-8'/><style>",
        "html, body { direction: rtl; text-align: right; color: #172033; "
        "font-size: 7pt; }",
        "h1 { color: #123B67; font-size: 10pt; text-align: center; "
        "margin: 0 0 8pt 0; }",
        "h2 { color: #123B67; font-size: 8pt; text-align: center; "
        "margin: 10pt 0 5pt 0; }",
        "p { margin: 3pt 0; }",
        "table { width: 100%; border-collapse: collapse; direction: rtl; "
        "text-align: right; }",
        "th, td { border: 0.5pt solid #8795A5; padding: 4pt; "
        "vertical-align: top; direction: rtl; }",
        "th { background-color: #E8F1FA; text-align: right; }",
        ".plan-data th { width: 20%; }",
        ".plan-data td { text-align: center; }",
        ".lesson-table { border: none; border-collapse: collapse; "
        "direction: rtl; }",
        ".activity-widget-lesson-table { border-collapse: collapse; }",
        ".activity-widget-lesson-layout { width: 100%; "
        "border-collapse: collapse; direction: rtl; }",
        ".lesson-number { width: 10%; white-space: nowrap; "
        "text-align: right; vertical-align: middle; font-weight: bold; "
        "color: #1E3A8A; padding: 0; border: none; }",
        ".lesson-content { text-align: right; vertical-align: top; "
        "padding: 0; border: none; }",
        ".activity-widget-lesson-number { width: 60px; white-space: nowrap; "
        "text-align: center; "
        "vertical-align: middle; color: #1D4ED8; }",
        ".activity-cards { border: none; border-collapse: separate; "
        "direction: rtl; }",
        ".symbol { font-weight: bold; color: #123B67; }",
        "</style></head><body dir='rtl' lang='ar'>",
        f"<h2 style='text-align: center; font-size: 8pt;'>{_pdf_text(title)}</h2>",
    ]

    weekday_names = dict(WEEKDAYS)
    metadata = (
        ("المجال", plan.get("domain_name")),
        ("كفاية المجال", plan.get("domain_competence")),
        ("تاريخ الانطلاق", plan.get("start_date")),
        ("عدد الحصص", plan.get("lesson_count")),
    )
    items = snapshot.get("items", [])
    component_texts = []
    for item in items:
        for detail in item.get("details", []):
            component = _pdf_detail_text(
                detail, "component_text", "component_name"
            )
            if component and component not in component_texts:
                component_texts.append(component)
    component_symbols = {
        component: f"[م{index}]"
        for index, component in enumerate(component_texts, 1)
    }
    content.append("<table width='100%'><tr><td style='width: 33%; align: center;'>")
    for prop in snapshot.get("properties", []):
        key = str(prop.get("key") or "").strip()
        value = str(prop.get("value") or "").strip()
        if key and value and key not in {"تقديم الوحدة", "وضعية الاستكشاف"}:
            metadata += ((key, value),)
    metadata_rows = [
        f"<tr><td>{_pdf_text(value)}</td><th>{_pdf_text(label)}</th></tr>"
        for label, value in metadata if value not in (None, "")
    ]
    if metadata_rows:
        content.extend((
            "<table class='plan-data' dir='rtl'>",
            *metadata_rows,
            "</table>",
        ))
    content.append("</td><td style='width: 34%; align: center;'>")
    if component_symbols:
        content.append(
            "<table class='plan-data' dir='rtl'>"
            "<tr><th colspan='2' style='text-align:center;'>"
            "مفتاح مكونات الكفاية</th></tr>"
        )
        for component, symbol in component_symbols.items():
            content.append(
                f"<tr><td class='symbol' align='center'>"
                f"{_pdf_text(component)}</td>"
                f"<td>{_pdf_text(symbol)}</td></tr>"
            )
        content.append("</table>")
    content.append("</td><td style='width: 33%; align: center;'>")
    content.append(
        "<table width='100%' class='plan-data' dir='rtl'>"
        "<tr><th style='text-align:center;'>الكفاية</th>"
        "<th style='text-align:center;'>النشاط</th></tr>"
    )
    for item in items:
        content.append(
            f"<tr><td align='center'>"
            f"{_pdf_text(item.get('competence_text'))}</td>"
            f"<td align='center'>{_pdf_text(item.get('activity_name'))}</td></tr>"
        )
    content.append("</table>")
    content.append("</td></tr></table>")

    plan_properties = {
        str(prop.get("key") or "").strip():
        str(prop.get("value") or "").strip()
        for prop in snapshot.get("properties", [])
    }
    unit_introduction = plan_properties.get("تقديم الوحدة", "")
    if unit_introduction:
        content.extend((
            "<table class='plan-data' dir='rtl'><tr>"
            "<td dir='rtl' style='text-align:right;'>"
            "<h3 style='text-align:center; font-size:8pt;'>تقديم الوحدة</h3>"
            f"<p dir='rtl' style='text-align:right;'>{_pdf_text(unit_introduction)}</p>"
            "</td></tr></table>",
        ))

    items_by_id = {
        item.get("source_id"): item for item in items
        if item.get("source_id") is not None
    }
    activities_by_mode = defaultdict(list)
    for activity in snapshot.get("activities", []):
        activities_by_mode[activity.get("learning_mode")].append(activity)
    mode_settings = {
        setting.get("learning_mode"): setting
        for setting in snapshot.get("mode_settings", [])
    }
    configured_activities = {
        "إدماج": {
            activity.get("activity_name"): activity
            for activity in snapshot.get("integration_activities", [])
        },
        "تقييم": {
            activity.get("activity_name"): activity
            for activity in snapshot.get("evaluation_activities", [])
        },
        "دعم وعلاج": {
            activity.get("activity_name"): activity
            for activity in snapshot.get("support_activities", [])
        },
    }
    mode_sections = (
        ("منهجي", "حصص التعلم المنهجي"),
        ("إدماج", "حصص التعلم الإدماجي"),
        ("تقييم", "حصص التعلم التقييمي"),
        ("دعم وعلاج", "حصص الدعم والعلاج"),
    )
    mode_counts = plan.get("mode_lesson_counts", {})
    lesson_number_offset = 0
    for mode, heading in mode_sections:
        uses_activity_widget_layout = mode in {
            "منهجي", "إدماج", "تقييم", "دعم وعلاج"
        }
        try:
            lesson_count = max(0, int(mode_counts.get(mode, 0) or 0))
        except (TypeError, ValueError):
            lesson_count = 0
        lesson_number_offset += lesson_count
        content.append(
            f"<h3 style='text-align: center; font-size: 8pt;'>{_pdf_text(heading)} "
            f"<b>(عدد الحصص: {lesson_count})</b></h3>"
        )
        if not lesson_count:
            continue
        table_class = (
            "lesson-table activity-widget-lesson-table"
            if uses_activity_widget_layout else "lesson-table"
        )
        content.append(
            f"<table class='{table_class}' dir='rtl' border='0' "
            "width='100%' "
            f"cellspacing='{0 if uses_activity_widget_layout else PDF_WIDGET_SPACING}' "
            "cellpadding='0'>"
        )
        for mode_lesson_number in range(1, lesson_count + 1):
            lesson_number = (
                lesson_number_offset - lesson_count + mode_lesson_number
            )
            lesson_activities = [
                activity for activity in activities_by_mode.get(mode, [])
                if activity.get("lesson_number") == mode_lesson_number
            ]
            activity_cards = []
            if uses_activity_widget_layout and lesson_activities:
                page_width_points = page_width_px * 72 / 96
                available_width = max(
                    0,
                    page_width_points - 60
                    - int(PDF_WIDGET_SPACING)
                    * (len(lesson_activities) + 2),
                )
                card_width = (
                    available_width / len(lesson_activities)
                )
            else:
                card_width = (
                    100 / len(lesson_activities)
                    if lesson_activities else 100
                )
            for activity in lesson_activities:
                border_color, background_color = (
                    _pdf_activity_card_colors(activity.get("activity_name"))
                )
                activity_name = _pdf_text(activity.get("activity_name"))
                line_parts = []
                if mode == "منهجي":
                    item = items_by_id.get(activity.get("item_source_id"))
                    details = item.get("details", []) if item else []
                    components = list(dict.fromkeys(
                        component_symbols.get(
                            _pdf_detail_text(
                                detail, "component_text", "component_name"
                            )
                        )
                        for detail in details
                        if _pdf_detail_text(
                            detail, "component_text", "component_name"
                        )
                    ))
                    if components:
                        line_parts.append(
                            f"مكوّن الكفاية: {', '.join(components)}"
                        )
                    objectives = list(dict.fromkeys(
                        _pdf_detail_text(
                            detail, "objective_text", "objective_name"
                        )
                        for detail in details
                        if _pdf_detail_text(
                            detail, "objective_text", "objective_name"
                        )
                    ))
                    if not objectives and activity.get("specific_objective"):
                        objectives.append(activity["specific_objective"])
                    if objectives:
                        line_parts.append(
                            f"الهدف المميز: {', '.join(objectives)}"
                        )
                    detail_contents = list(dict.fromkeys(
                        _pdf_detail_text(
                            detail, "content_text", "content_name"
                        )
                        for detail in details
                        if _pdf_detail_text(
                            detail, "content_text", "content_name"
                        )
                    ))
                    activity_content = str(
                        activity.get("content") or ""
                    ).strip()
                    if activity_content and activity_content not in detail_contents:
                        detail_contents.append(activity_content)
                    if detail_contents:
                        line_parts.append(
                            f"المحتوى: {', '.join(detail_contents)}"
                        )
                    for key, label in (
                        ("lesson_goal", "هدف الحصة"),
                        ("target_competencies", "الكفايات المستهدفة"),
                        ("evaluation", "التقييم"),
                        ("expected_performance", "الأداء المنتظر"),
                        ("target_criteria", "المعايير المستهدفة"),
                        ("demographic_concept", "المفهوم السكاني"),
                        ("health_concept", "المفهوم الصحي"),
                        ("life_skills", "المهارات الحياتية"),
                    ):
                        value = activity.get(key)
                        if value not in (None, ""):
                            line_parts.append(f"{label}: {value}")
                    for prop in activity.get("properties", []):
                        key = str(prop.get("key") or "").strip()
                        value = str(prop.get("value") or "").strip()
                        if key and value:
                            line_parts.append(f"{key}: {value}")
                else:
                    configured = configured_activities.get(mode, {}).get(
                        activity.get("activity_name"), {}
                    )
                    if mode == "إدماج":
                        label = "معايير الإدماج"
                        keys = (
                            "target_criteria", "target_competencies",
                            "target_text",
                        )
                    elif mode == "تقييم":
                        label = "الأداء المنتظر"
                        keys = ("expected_performance", "target_text")
                    else:
                        label = "المعايير المستهدفة"
                        keys = ("target_criteria",)
                    value = next(
                        (
                            activity.get(key) or configured.get(key)
                            for key in keys
                            if activity.get(key) or configured.get(key)
                        ),
                        "",
                    )
                    if mode == "تقييم" and not value:
                        value = mode_settings.get(mode, {}).get(
                            "expected_performance", ""
                        )
                    if value:
                        line_parts.append(f"{label}: {value}")
                if uses_activity_widget_layout:
                    line = (
                        "<p align='center' "
                        "style='margin-top:0; margin-bottom:3pt;'>"
                        f"<b>{activity_name}: {activity.get("duration_minutes")} دقيقة</b></p>"
                    )
                    if line_parts:
                        line += (
                            "<p align='center' dir='rtl' "
                            "style='margin-top:0; margin-bottom:3pt; "
                            "color:#334155;'>"
                            f"{_pdf_inline_text(' _ '.join(line_parts))}</p>"
                        )
                    card_alignment = "center"
                else:
                    line = (
                        f"<font color='{border_color}'>"
                        f"<b>{activity_name}</b></font>"
                    )
                    if line_parts:
                        line += "".join(
                            f"<p style='margin:4pt 0 0 0; color:#334155;'>"
                            f"{_pdf_text(part)}</p>"
                            for part in line_parts
                        )
                    card_alignment = "right"
                if uses_activity_widget_layout:
                    activity_cards.append(
                        f"<td width='{card_width:.2f}' "
                        f"bgcolor='{background_color}' dir='rtl' "
                        f"align='{card_alignment}' "
                        f"style='border:1px solid {border_color}; "
                        "padding:4px 4px 0 4px; direction:rtl;'>"
                        f"{line}</td>"
                    )
                else:
                    card_html = _pdf_rounded_box_html(
                        line, border_color, background_color,
                        card_alignment
                    )
                    activity_cards.append(
                        f"<td width='{card_width:.2f}%' valign='top' "
                        f"style='direction:rtl;'>{card_html}</td>"
                    )
            activity_table = ""
            if activity_cards:
                if uses_activity_widget_layout:
                    spacer = (
                        f"<td width='{PDF_WIDGET_SPACING}' "
                        "style='border:0; padding:0;'></td>"
                    )
                    activity_table = (
                        spacer
                        + spacer.join(reversed(activity_cards))
                        + spacer
                    )
                else:
                    activity_table = (
                        "<table class='activity-cards' dir='rtl' "
                        "width='100%' border='0' "
                        f"cellspacing='{PDF_WIDGET_SPACING}' "
                        "cellpadding='0'><tr>"
                        f"{''.join(reversed(activity_cards))}</tr></table>"
                    )
            elif uses_activity_widget_layout:
                page_width_points = page_width_px * 72 / 96
                activity_table = (
                    f"<td width='{max(0, page_width_points - 65):.2f}' "
                    "style='border:0; padding:0;'>"
                    "<span style='font-size:1pt;'>&nbsp;</span></td>"
                )
            number_border, number_background = PDF_LESSON_NUMBER_COLORS[
                mode
            ]
            if uses_activity_widget_layout:
                lesson_number_cell = (
                    f"<td class='lesson-number activity-widget-lesson-number' "
                    "width='60' bgcolor='"
                    f"{number_background}' style='width:60px; "
                    f"border:1px solid {number_border}; "
                    "vertical-align:middle; text-align:center;'>"
                    f"<b><font color='{number_border}'>"
                    f"{_pdf_text(lesson_number)}</font></b></td>"
                )
            else:
                lesson_number_cell = (
                    f"<td class='lesson-number' width='40px' "
                    "style='vertical-align:middle;'>"
                    "<table border='1' cellspacing='0' cellpadding='0' "
                    f"width='100%' bgcolor='{number_background}' "
                    f"style='border-color:{number_border}; "
                    "border-collapse:collapse;'>"
                    f"<tr><td align='center' dir='rtl' "
                    f"bgcolor='{number_background}' "
                    f"style='border-color:{number_border}; "
                    "padding:4px;'>"
                    f"<b><font color='{number_border}'>"
                    f"{_pdf_text(lesson_number)}</font></b></td></tr>"
                    "</table></td>"
                )
            content.append(
                "<tr>"
                + (
                    "<td class='activity-widget-lesson-row' "
                    "style='padding:0; border:0;'>"
                    "<table class='activity-widget-lesson-layout' dir='rtl' "
                    "width='100%' border='0' "
                    "cellspacing='0' cellpadding='0'>"
                    "<tr>"
                    f"{activity_table}{lesson_number_cell}"
                    "<td width='"
                    f"{PDF_WIDGET_SPACING}' "
                    "style='border:0; padding:0;'></td>"
                    "</tr></table></td>"
                    if uses_activity_widget_layout
                    else (
                        f"<td class='lesson-content'>{activity_table}</td>"
                        f"{lesson_number_cell}"
                    )
                )
                + "</tr>"
            )
            if uses_activity_widget_layout:
                content.append(
                    "<tr><td style='border:0; padding:0;'>"
                    "<p style='margin:0; font-size:1pt;'>&nbsp;</p>"
                    "<p style='margin:0; font-size:1pt;'>&nbsp;</p>"
                    "</td></tr>"
                )
        content.append("</table>")

    exploration = plan_properties.get("وضعية الاستكشاف", "")
    if exploration:
        content.extend((
            "<table class='plan-data' dir='rtl'><tr>"
            "<td dir='rtl' style='text-align:right;'>"
            "<h3 style='text-align:center; font-size:8pt;'>وضعية الاستكشاف</h3>"
            f"<p dir='rtl' style='text-align:right;'>{_pdf_text(exploration)}</p>"
            "</td></tr></table>",
        ))
    content.append("</body></html>")
    return "".join(content)


def export_teaching_plan_pdf(snapshot, output_path):
    if not isinstance(snapshot, dict) or not isinstance(
        snapshot.get("plan"), dict
    ):
        raise ValueError("بيانات المخطط غير صالحة لتصدير PDF.")
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setPageSize(QPageSize(QPageSize.A4))
    printer.setPageOrientation(QPageLayout.Orientation.Landscape)
    printer.setPageMargins(
        QMarginsF(5.0, 5.0, 5.0, 5.0),
        QPageLayout.Unit.Millimeter,
    )
    if printer.pageLayout().orientation() != QPageLayout.Orientation.Landscape:
        raise OSError("تعذر ضبط اتجاه صفحات ملف PDF إلى أفقي.")
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(output_path))
    document = QTextDocument()
    document.setDefaultFont(QFont(load_custom_font(), 9))
    document.setDocumentMargin(0)
    text_option = QTextOption()
    text_option.setTextDirection(Qt.RightToLeft)
    document.setDefaultTextOption(text_option)
    page_size = printer.pageLayout().paintRect(
        QPageLayout.Unit.Point
    ).size()
    document.setPageSize(page_size)
    page_width_px = page_size.width() * 96 / 72
    document.setHtml(
        _teaching_plan_pdf_html(snapshot, page_width_px)
    )
    _keep_activity_widgets_on_same_page(document, page_size)
    document.print_(printer)
    path = Path(output_path)
    if (
        printer.printerState() == QPrinter.PrinterState.Error
        or not path.is_file()
        or path.stat().st_size < 1000
    ):
        raise OSError("لم يتم إنشاء ملف PDF صالح.")
    return path


class TeachingPlanViewPage(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setLayoutDirection(Qt.RightToLeft)

        layout = QVBoxLayout(self)
        title = QLabel("عرض المخطط")
        title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #1E40AF;"
        )
        layout.addWidget(title)

        selectors = QGridLayout()
        selectors.setHorizontalSpacing(12)
        selectors.setVerticalSpacing(8)
        self.class_combo = QComboBox()
        self.domain_combo = QComboBox()
        self.name_combo = QComboBox()
        self.unit_number_combo = QComboBox()
        selector_fields = (
            ("القسم", self.class_combo),
            ("المجال", self.domain_combo),
            ("اسم المخطط", self.name_combo),
            ("عدد الوحدة", self.unit_number_combo),
        )
        for index, (label, combo) in enumerate(selector_fields):
            combo.setMinimumWidth(180)
            combo.setLayoutDirection(Qt.RightToLeft)
            combo.currentIndexChanged.connect(self.update_plan_filters)
            field = QWidget()
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            caption = QLabel(label)
            caption.setStyleSheet(
                "font-weight: bold; color: #334155;"
            )
            field_layout.addWidget(caption)
            field_layout.addWidget(combo)
            selectors.addWidget(field, index // 4, index % 4)
        layout.addLayout(selectors)

        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content_container = QWidget()
        self.content_container.setLayoutDirection(Qt.RightToLeft)
        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(12, 12, 12, 20)
        self.content_layout.setSpacing(14)
        self.content_scroll.setWidget(self.content_container)
        layout.addWidget(self.content_scroll, 1)
        self.refresh_plans()

    def refresh_plans(self):
        selected_ids = (
            self.class_combo.currentData(),
            self.domain_combo.currentData(),
            self.name_combo.currentData(),
            self.unit_number_combo.currentData(),
        )
        self._plan_rows = self.db.teaching_plans()
        self._class_labels = {
            class_id: f"{class_name} — {level_name}"
            for class_id, class_name, _level_id, level_name
            in self.db.planning_classes()
        }
        self._set_combo_options(
            self.class_combo,
            "اختر القسم",
            [
                (class_id, self._class_labels.get(class_id, class_name))
                for class_id, class_name in dict.fromkeys(
                    (row[2], row[3]) for row in self._plan_rows
                )
            ],
            selected_ids[0],
        )
        self.update_plan_filters(
            selected_ids=selected_ids
        )

    @staticmethod
    def _set_combo_options(combo, placeholder, options, selected=None):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, None)
        for value, label in options:
            combo.addItem(str(label), value)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def update_plan_filters(self, *_args, selected_ids=None):
        if not hasattr(self, "_plan_rows"):
            return
        if selected_ids is None:
            selected_ids = (
                self.class_combo.currentData(),
                self.domain_combo.currentData(),
                self.name_combo.currentData(),
                self.unit_number_combo.currentData(),
            )
        class_id, domain_id, plan_name, unit_number = selected_ids
        class_rows = [
            row for row in self._plan_rows
            if class_id is None or row[2] == class_id
        ]
        domains = list(dict.fromkeys(
            (row[4], row[5]) for row in class_rows
        ))
        self._set_combo_options(
            self.domain_combo, "اختر المجال", domains,
            domain_id,
        )
        domain_id = self.domain_combo.currentData()

        domain_rows = [
            row for row in class_rows
            if domain_id is None or row[4] == domain_id
        ]
        names = list(dict.fromkeys((row[1], row[1]) for row in domain_rows))
        self._set_combo_options(
            self.name_combo, "اختر اسم المخطط", names,
            plan_name,
        )
        plan_name = self.name_combo.currentData()

        name_rows = [
            row for row in domain_rows
            if plan_name is None or row[1] == plan_name
        ]
        numbers = list(dict.fromkeys(
            (
                row[11] if row[11] is not None else -1,
                f"عدد {row[11]}" if row[11] is not None else "غير محدد",
            )
            for row in name_rows
        ))
        self._set_combo_options(
            self.unit_number_combo, "اختر عدد الوحدة", numbers,
            unit_number,
        )
        self.display_selected_plan()

    def display_selected_plan(self, *_args):
        class_id = self.class_combo.currentData()
        domain_id = self.domain_combo.currentData()
        plan_name = self.name_combo.currentData()
        unit_number = self.unit_number_combo.currentData()
        if (
            class_id is None or domain_id is None or plan_name is None
            or unit_number is None
        ):
            self._clear_content()
            prompt = QLabel(
                "اختر القسم والمجال واسم المخطط وعدد الوحدة لعرضه."
            )
            prompt.setAlignment(Qt.AlignCenter)
            prompt.setStyleSheet("color: #64748B; padding: 36px;")
            self.content_layout.addWidget(prompt)
            return
        else:
            matching_plan = next(
                (
                    row for row in self._plan_rows
                    if row[2] == class_id and row[4] == domain_id
                    and row[1] == plan_name
                    and (row[11] if row[11] is not None else -1)
                    == unit_number
                ),
                None,
            )
            plan_id = matching_plan[0] if matching_plan else None
        self._clear_content()
        if plan_id is None:
            empty = QLabel("لا يوجد مخطط يطابق الاختيارات المحددة.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color: #64748B; padding: 36px;")
            self.content_layout.addWidget(empty)
            return
        try:
            snapshot = self.db.export_teaching_plan(int(plan_id))
            self._render_snapshot(snapshot)
        except (ValueError, sqlite3.Error) as error:
            QMessageBox.warning(
                self, "تعذر عرض المخطط", str(error)
            )

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _table(headers, rows, column_widths=None):
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setLayoutDirection(Qt.RightToLeft)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setFocusPolicy(Qt.NoFocus)
        table.setWordWrap(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setAlternatingRowColors(True)
        table.setStyleSheet("""
            QTableWidget {
                background: #FFFFFF;
                alternate-background-color: #F8FAFC;
                border: 1px solid #D9E2EF;
                border-radius: 10px;
                gridline-color: #E2E8F0;
                color: #1E293B;
            }
            QHeaderView::section {
                background: #EAF2FC;
                color: #123B67;
                font-weight: bold;
                text-align: center;
                border: none;
                border-bottom: 1px solid #CBD5E1;
                padding: 8px;
            }
        """)
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                cell = QTableWidgetItem(
                    "" if value is None else str(value)
                )
                cell.setTextAlignment(Qt.AlignCenter)
                table.setItem(row_index, column, cell)
        table.resizeRowsToContents()
        for row_index in range(table.rowCount()):
            table.setRowHeight(
                row_index, max(38, table.rowHeight(row_index))
            )
        if column_widths:
            for column, width in enumerate(column_widths):
                table.setColumnWidth(column, width)
        table.setMinimumHeight(max(64, table.horizontalHeader().sizeHint().height()
                                   + sum(table.rowHeight(i)
                                         for i in range(table.rowCount())) + 4))
        return table

    @staticmethod
    def _panel(title, accent="#2563EB"):
        panel = QFrame()
        panel.setObjectName("planViewPanel")
        panel.setStyleSheet(
            "QFrame#planViewPanel { background: #FFFFFF; "
            f"border: 1px solid #D9E2EF; border-top: 3px solid {accent}; "
            "border-radius: 12px; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 10, 12, 12)
        panel_layout.setSpacing(8)
        heading = QLabel(title)
        heading.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #123B67; "
            "border: none; background: transparent;"
        )
        heading.setAlignment(Qt.AlignCenter)
        panel_layout.addWidget(heading)
        return panel, panel_layout

    def _render_snapshot(self, snapshot):
        plan = snapshot["plan"]
        title_text = (
            f"مخطط {_teaching_plan_name(snapshot)} — عدد "
            f"{plan.get('plan_number') if plan.get('plan_number') is not None else 'غير محدد'} "
            f"— القسم: {plan.get('class_name') or ''}"
        )
        banner = QFrame()
        banner.setStyleSheet(
            "QFrame { background: #123B67; border-radius: 14px; }"
        )
        banner_layout = QVBoxLayout(banner)
        banner_layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel(title_text)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: white; "
            "border: none; background: transparent;"
        )
        banner_layout.addWidget(title)
        self.content_layout.addWidget(banner)

        metadata = [
            ("المجال", plan.get("domain_name")),
            ("كفاية المجال", plan.get("domain_competence")),
            ("تاريخ الانطلاق", plan.get("start_date")),
            ("عدد الحصص", plan.get("lesson_count")),
        ]
        properties = {
            str(prop.get("key") or "").strip():
            str(prop.get("value") or "").strip()
            for prop in snapshot.get("properties", [])
        }
        metadata.extend(
            (key, value)
            for key, value in properties.items()
            if key and value and key not in {"تقديم الوحدة", "وضعية الاستكشاف"}
        )
        metadata_rows = [
            (label, value) for label, value in metadata
            if value not in (None, "")
        ]

        items = snapshot.get("items", [])
        component_texts = []
        for item in items:
            for detail in item.get("details", []):
                component = _pdf_detail_text(
                    detail, "component_text", "component_name"
                )
                if component and component not in component_texts:
                    component_texts.append(component)
        component_symbols = {
            component: f"[م{index}]"
            for index, component in enumerate(component_texts, 1)
        }
        info_grid = QGridLayout()
        info_grid.setHorizontalSpacing(12)
        info_grid.setVerticalSpacing(12)
        info_panel, info_layout = self._panel("بيانات المخطط")
        info_layout.addWidget(self._table(("البيان", "التفاصيل"), metadata_rows))
        info_grid.addWidget(info_panel, 0, 0)

        if component_symbols:
            symbols_panel, symbols_layout = self._panel(
                "مفتاح مكونات الكفاية", "#059669"
            )
            symbols_layout.addWidget(self._table(
                ("الرمز", "مكوّن الكفاية"),
                [(symbol, component)
                 for component, symbol in component_symbols.items()],
            ))
            info_grid.addWidget(symbols_panel, 0, 1)

        items_panel, items_layout = self._panel("الأنشطة وكفاياتها", "#D97706")
        items_layout.addWidget(self._table(
            ("النشاط", "الكفاية"),
            [(item.get("activity_name"), item.get("competence_text"))
             for item in items],
        ))
        info_grid.addWidget(items_panel, 0, 2)
        self.content_layout.addLayout(info_grid)

        unit_introduction = properties.get("تقديم الوحدة", "")
        if unit_introduction:
            self.content_layout.addWidget(
                self._text_panel("تقديم الوحدة", unit_introduction, "#0891B2")
            )

        items_by_id = {
            item.get("source_id"): item for item in items
            if item.get("source_id") is not None
        }
        activities_by_mode = defaultdict(list)
        for activity in snapshot.get("activities", []):
            activities_by_mode[activity.get("learning_mode")].append(activity)
        mode_settings = {
            setting.get("learning_mode"): setting
            for setting in snapshot.get("mode_settings", [])
        }
        configured_activities = {
            "إدماج": {
                activity.get("activity_name"): activity
                for activity in snapshot.get("integration_activities", [])
            },
            "تقييم": {
                activity.get("activity_name"): activity
                for activity in snapshot.get("evaluation_activities", [])
            },
            "دعم وعلاج": {
                activity.get("activity_name"): activity
                for activity in snapshot.get("support_activities", [])
            },
        }
        mode_sections = (
            ("منهجي", "حصص التعلم المنهجي"),
            ("إدماج", "حصص التعلم الإدماجي"),
            ("تقييم", "حصص التعلم التقييمي"),
            ("دعم وعلاج", "حصص الدعم والعلاج"),
        )
        mode_counts = plan.get("mode_lesson_counts", {})
        number_offset = 0
        for mode, heading in mode_sections:
            try:
                lesson_count = max(0, int(mode_counts.get(mode, 0) or 0))
            except (TypeError, ValueError):
                lesson_count = 0
            number_offset += lesson_count
            color, background = PDF_LESSON_NUMBER_COLORS[mode]
            section, section_layout = self._panel(
                f"{heading} — عدد الحصص: {lesson_count}", color
            )
            if lesson_count:
                for mode_lesson_number in range(1, lesson_count + 1):
                    lesson_number = (
                        number_offset - lesson_count + mode_lesson_number
                    )
                    lesson_activities = [
                        activity
                        for activity in activities_by_mode.get(mode, [])
                        if activity.get("lesson_number") == mode_lesson_number
                    ]
                    section_layout.addWidget(self._lesson_card(
                        mode, mode_lesson_number, lesson_number,
                        lesson_activities, items_by_id,
                        component_symbols, configured_activities,
                        mode_settings,
                    ))
            else:
                empty = QLabel("لا توجد حصص محددة لهذا النمط.")
                empty.setAlignment(Qt.AlignCenter)
                empty.setStyleSheet("color: #64748B; padding: 8px;")
                section_layout.addWidget(empty)
            self.content_layout.addWidget(section)

        exploration = properties.get("وضعية الاستكشاف", "")
        if exploration:
            self.content_layout.addWidget(
                self._text_panel("وضعية الاستكشاف", exploration, "#7C3AED")
            )
        self.content_layout.addStretch(1)

    def _text_panel(self, title, text, accent):
        panel, panel_layout = self._panel(title, accent)
        body = QLabel(str(text))
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setStyleSheet(
            "color: #334155; line-height: 1.5; border: none; "
            "background: transparent; padding: 6px;"
        )
        panel_layout.addWidget(body)
        return panel

    def _lesson_card(
        self, mode, mode_lesson_number, lesson_number, activities,
        items_by_id, component_symbols, configured_activities, mode_settings,
    ):
        lesson = QFrame()
        lesson.setStyleSheet(
            "QFrame { background: #F8FAFC; border: 1px solid #E2E8F0; "
            "border-radius: 12px; }"
        )
        lesson_layout = QHBoxLayout(lesson)
        lesson_layout.setContentsMargins(10, 10, 10, 10)
        lesson_layout.setSpacing(10)
        number = QLabel(f"الحصة\n{lesson_number}")
        number.setAlignment(Qt.AlignCenter)
        number.setMinimumSize(64, 54)
        color, background = PDF_LESSON_NUMBER_COLORS[mode]
        number.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {color}; "
            f"background: {background}; border: 1px solid {color}; "
            "border-radius: 10px; padding: 6px;"
        )
        lesson_layout.addWidget(number, 0, Qt.AlignTop)
        cards = QGridLayout()
        cards.setHorizontalSpacing(8)
        cards.setVerticalSpacing(8)
        if activities:
            for index, activity in enumerate(activities):
                card = self._activity_card(
                    mode, activity, items_by_id, component_symbols,
                    configured_activities, mode_settings,
                )
                cards.addWidget(card, index // 2, index % 2)
            lesson_layout.addLayout(cards, 1)
        else:
            empty = QLabel("لم تُوزّع أنشطة على هذه الحصة بعد.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "color: #64748B; border: none; background: transparent;"
            )
            lesson_layout.addWidget(empty, 1)
        return lesson

    def _activity_card(
        self, mode, activity, items_by_id, component_symbols,
        configured_activities, mode_settings,
    ):
        border, background = _pdf_activity_card_colors(
            activity.get("activity_name")
        )
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {background}; border: 1px solid {border}; "
            "border-radius: 11px; }"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(5)
        name = QLabel(
            f"{activity.get('activity_name') or 'نشاط'}"
            f" — {activity.get('duration_minutes') or ''} دقيقة"
        )
        name.setWordWrap(True)
        name.setAlignment(Qt.AlignCenter)
        name.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {border}; "
            "border: none; background: transparent;"
        )
        card_layout.addWidget(name)

        details = []
        if mode == "منهجي":
            item = items_by_id.get(activity.get("item_source_id"))
            item_details = item.get("details", []) if item else []
            components = list(dict.fromkeys(
                component_symbols.get(
                    _pdf_detail_text(
                        detail, "component_text", "component_name"
                    )
                )
                for detail in item_details
                if _pdf_detail_text(
                    detail, "component_text", "component_name"
                )
            ))
            if components:
                details.append(("مكوّن الكفاية", "، ".join(components)))
            objectives = list(dict.fromkeys(
                _pdf_detail_text(detail, "objective_text", "objective_name")
                for detail in item_details
                if _pdf_detail_text(detail, "objective_text", "objective_name")
            ))
            if not objectives and activity.get("specific_objective"):
                objectives.append(activity["specific_objective"])
            if objectives:
                details.append(("الهدف المميز", "، ".join(objectives)))
            contents = list(dict.fromkeys(
                _pdf_detail_text(detail, "content_text", "content_name")
                for detail in item_details
                if _pdf_detail_text(detail, "content_text", "content_name")
            ))
            activity_content = str(activity.get("content") or "").strip()
            if activity_content and activity_content not in contents:
                contents.append(activity_content)
            if contents:
                details.append(("المحتوى", "، ".join(contents)))
            for key, label in (
                ("lesson_goal", "هدف الحصة"),
                ("target_competencies", "الكفايات المستهدفة"),
                ("evaluation", "التقييم"),
                ("expected_performance", "الأداء المنتظر"),
                ("target_criteria", "المعايير المستهدفة"),
                ("demographic_concept", "المفهوم السكاني"),
                ("health_concept", "المفهوم الصحي"),
                ("life_skills", "المهارات الحياتية"),
            ):
                value = activity.get(key)
                if value not in (None, ""):
                    details.append((label, value))
            details.extend(
                (str(prop.get("key") or "").strip(),
                 str(prop.get("value") or "").strip())
                for prop in activity.get("properties", [])
                if str(prop.get("key") or "").strip()
                and str(prop.get("value") or "").strip()
            )
        else:
            configured = configured_activities.get(mode, {}).get(
                activity.get("activity_name"), {}
            )
            if mode == "إدماج":
                label = "معايير الإدماج"
                keys = ("target_criteria", "target_competencies", "target_text")
            elif mode == "تقييم":
                label = "الأداء المنتظر"
                keys = ("expected_performance", "target_text")
            else:
                label = "المعايير المستهدفة"
                keys = ("target_criteria",)
            value = next(
                (
                    activity.get(key) or configured.get(key)
                    for key in keys
                    if activity.get(key) or configured.get(key)
                ),
                "",
            )
            if mode == "تقييم" and not value:
                value = mode_settings.get(mode, {}).get(
                    "expected_performance", ""
                )
            if value:
                details.append((label, value))
        for label, value in details:
            field = QLabel(f"{label}: {value}")
            field.setWordWrap(True)
            field.setAlignment(Qt.AlignCenter)
            field.setTextInteractionFlags(Qt.TextSelectableByMouse)
            field.setStyleSheet(
                "color: #334155; border: none; background: transparent;"
            )
            card_layout.addWidget(field)
        return card

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_plans()


def _style_primary_button(button):
    button.setProperty("primary", "true")
    button.style().unpolish(button)
    button.style().polish(button)


def _set_action_type(button, action_type):
    button.setProperty("actionType", action_type)
    button.style().unpolish(button)
    button.style().polish(button)


class PlanPropertiesTable(QTableWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setColumnWidth(0, int(self.viewport().width() * 0.25))


class CheckableComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setModel(QStandardItemModel(self))
        self.view().pressed.connect(self.toggle_item)
        self.lineEdit().returnPressed.connect(self.add_typed_item)
        self.setPlaceholderText("اختر هدفاً أو أضف هدفاً جديداً")

    def add_checkable_item(self, text, checked=False):
        item = QStandardItem(str(text))
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        self.model().appendRow(item)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.refresh_text()

    def toggle_item(self, index):
        item = self.model().itemFromIndex(index)
        if item is None:
            return
        item.setCheckState(
            Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
        )
        self.refresh_text()

    def add_typed_item(self):
        text = self.lineEdit().text().strip()
        if not text:
            self.refresh_text()
            return
        matching_items = self.model().findItems(text, Qt.MatchFixedString)
        item = matching_items[0] if matching_items else None
        if item is None:
            self.add_checkable_item(text, checked=True)
        else:
            item.setCheckState(Qt.Checked)
            self.refresh_text()

    def set_checked_texts(self, texts):
        selected = {str(text).strip() for text in texts if str(text).strip()}
        for index in range(self.count()):
            item = self.model().item(index)
            item.setCheckState(
                Qt.Checked if item.text() in selected else Qt.Unchecked
            )
        self.refresh_text()

    def checked_texts(self):
        return [
            self.model().item(index).text()
            for index in range(self.count())
            if self.model().item(index).checkState() == Qt.Checked
        ]

    def refresh_text(self):
        self.lineEdit().setText("، ".join(self.checked_texts()))


class AutocompletePlainTextEdit(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.completion_model = QStringListModel(self)
        self.completer = QCompleter(self.completion_model, self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.activated.connect(self.insert_completion)

    def set_suggestions(self, suggestions):
        self.completion_model.setStringList(list(dict.fromkeys(suggestions)))

    def insert_completion(self, completion):
        self.setPlainText(completion)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        prefix = self.textCursor().block().text().strip()
        if len(prefix) < 2:
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(prefix)
        self.completer.complete(self.cursorRect())


class TeachingPlanDialog(QDialog):
    PROPERTY_PRESETS = (
        "مدار الاهتمام",
        "مشروع الوحدة",
        "تقديم الوحدة",
        "وضعية الاستكشاف",
    )
    INTEREST_AREAS = (
        "الصحّة والرفاه",
        "المبادرة وبناء المشروع",
        "المحيط والاستهلاك",
        "وسائل الإعلام والاتصال",
        "العيش معاً والمواطنة",
    )

    def __init__(self, db, parent=None, plan=None):
        super().__init__(parent)
        self.db = db
        self.plan = plan
        self._initializing = True
        self.setWindowTitle("بيانات المخطط")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(1120, 720)

        layout = QVBoxLayout(self)
        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        body = QWidget()
        body.setLayoutDirection(Qt.LeftToRight)
        columns = QHBoxLayout(body)
        columns.setDirection(QBoxLayout.LeftToRight)
        columns.setContentsMargins(0, 0, 0, 0)
        left_column = QWidget()
        left_column.setLayoutDirection(Qt.RightToLeft)
        left_layout = QVBoxLayout(left_column)
        right_column = QWidget()
        right_column.setLayoutDirection(Qt.RightToLeft)
        right_layout = QVBoxLayout(right_column)
        columns.addWidget(left_column, 1)
        columns.addWidget(right_column, 1)
        content_scroll.setWidget(body)
        layout.addWidget(content_scroll, 1)

        form_group = QGroupBox("بيانات المخطط")
        form_group.setLayoutDirection(Qt.RightToLeft)
        form = QFormLayout(form_group)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.majal_combo = QComboBox()
        for majal_id, name in db.planning_majals():
            self.majal_combo.addItem(name, majal_id)
        form.addRow("المجال:", self.majal_combo)

        self.plan_name_combo = QComboBox()
        self.plan_name_combo.addItem("اختر اسم المخطط", None)
        form.addRow("اسم المخطط:", self.plan_name_combo)

        self.classe_combo = QComboBox()
        for class_id, class_name, _level_id, level_name in db.planning_classes():
            self.classe_combo.addItem(f"{class_name} — {level_name}", class_id)
        self.competence_combo = QComboBox()
        self.classe_combo.currentIndexChanged.connect(self.load_competences)
        self.majal_combo.currentIndexChanged.connect(
            self.update_plan_name_options
        )
        self.majal_combo.currentIndexChanged.connect(self.load_competences)
        self.update_plan_name_options()
        form.addRow("القسم:", self.classe_combo)
        form.addRow("كفاية المجال:", self.competence_combo)

        self.start_date = QDateEdit(QDate.currentDate())
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        form.addRow("تاريخ الانطلاق:", self.start_date)
        self.lesson_count = QSpinBox()
        self.lesson_count.setRange(0, 4000)
        self.lesson_count.setValue(0)
        self.lesson_count.setReadOnly(True)
        self.lesson_count.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        form.addRow("عدد الحصص:", self.lesson_count)
        counts_group = QGroupBox("عدد الحصص حسب نمط التعلم")
        counts_layout = QFormLayout(counts_group)
        counts_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mode_lesson_counts = {}
        for mode_name in PlanActivityDialog.MODES:
            count_spin = QSpinBox()
            count_spin.setRange(0, 1000)
            self.mode_lesson_counts[mode_name] = count_spin
            counts_layout.addRow(f"حصص {mode_name}:", count_spin)
        form.addRow(counts_group)
        self.mode_ratio_warning = QLabel()
        self.mode_ratio_warning.setWordWrap(True)
        form.addRow("", self.mode_ratio_warning)
        self.lesson_duration_combo = QComboBox()
        for minutes in (30, 60, 120):
            self.lesson_duration_combo.addItem(
                f"{minutes} دقيقة", minutes
            )
        form.addRow("الزمن المخصص لكل حصة:", self.lesson_duration_combo)
        self.lesson_duration_suggestion = QLabel()
        self.lesson_duration_suggestion.setStyleSheet("color: #475569;")
        form.addRow("", self.lesson_duration_suggestion)
        right_layout.addWidget(form_group)

        properties_group = QGroupBox("خاصيات يحددها المستخدم لهذا المخطط")
        properties_group.setToolTip(
            "أضف اسماً وقيمة لكل خاصية تريد حفظها مع هذا المخطط، "
            "مثل مدار الاهتمام أو تقديم الوحدة. مرّر المؤشر هنا لعرض هذا التوضيح."
        )
        properties_group.setFont(QApplication.font())
        properties_layout = QVBoxLayout(properties_group)
        self.plan_properties = []
        self.editing_property_row = None

        property_form = QGroupBox("إدخال الخاصية")
        property_form.setObjectName("planPropertyForm")
        property_form.setLayoutDirection(Qt.RightToLeft)
        property_form_layout = QFormLayout(property_form)
        property_form_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.property_name_combo = QComboBox()
        self.property_name_combo.setEditable(True)
        self.property_name_combo.addItems(self.PROPERTY_PRESETS)
        self.property_name_combo.setFont(QApplication.font())
        self.property_name_combo.setToolTip(
            "اختر اسماً مقترحاً أو اكتب اسم الخاصية بنفسك."
        )
        property_form_layout.addRow("اسم الخاصية:", self.property_name_combo)
        self.property_value_container = QWidget()
        self.property_value_layout = QVBoxLayout(self.property_value_container)
        self.property_value_layout.setContentsMargins(0, 0, 0, 0)
        property_form_layout.addRow("القيمة:", self.property_value_container)
        self.property_value_editor = None

        form_actions = QHBoxLayout()
        self.save_property_button = QPushButton("إضافة الخاصية")
        _set_action_type(self.save_property_button, "add")
        self.save_property_button.setFont(QApplication.font())
        self.save_property_button.clicked.connect(self.save_property_form)
        self.cancel_property_edit_button = QPushButton("إلغاء التعديل")
        _set_action_type(self.cancel_property_edit_button, "cancel")
        self.cancel_property_edit_button.setFont(QApplication.font())
        self.cancel_property_edit_button.clicked.connect(
            self.clear_property_form
        )
        self.cancel_property_edit_button.hide()
        form_actions.addWidget(self.save_property_button)
        form_actions.addWidget(self.cancel_property_edit_button)
        property_form_layout.addRow(form_actions)
        properties_layout.addWidget(property_form, 1)

        property_table_panel = QWidget()
        property_table_panel.setObjectName("planPropertyTablePanel")
        property_table_layout = QVBoxLayout(property_table_panel)
        property_actions = QHBoxLayout()
        property_actions.addStretch()
        self.edit_property_button = QPushButton("تعديل الخاصية المحددة")
        _set_action_type(self.edit_property_button, "edit")
        self.edit_property_button.setFont(QApplication.font())
        self.edit_property_button.clicked.connect(self.edit_selected_property)
        self.edit_property_button.setEnabled(False)
        self.delete_property_button = QPushButton("حذف الخاصية المحددة")
        _set_action_type(self.delete_property_button, "delete")
        self.delete_property_button.setFont(QApplication.font())
        self.delete_property_button.clicked.connect(
            self.remove_property_row
        )
        self.delete_property_button.setEnabled(False)
        property_actions.addWidget(self.edit_property_button)
        property_actions.addWidget(self.delete_property_button)
        property_table_layout.addLayout(property_actions)

        self.properties_table = PlanPropertiesTable(0, 2)
        self.properties_table.setHorizontalHeaderLabels(["اسم الخاصية", "القيمة"])
        self.properties_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Interactive
        )
        self.properties_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self.properties_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.properties_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.properties_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.properties_table.setWordWrap(True)
        self.properties_table.setMinimumHeight(310)
        self.properties_table.setFont(QApplication.font())
        self.properties_table.setToolTip(
            "هذا الجدول للعرض فقط. استخدم زري التعديل والحذف أعلاه، "
            "ويظهر نموذج الإدخال فوقه."
        )
        self.properties_table.itemSelectionChanged.connect(
            self.update_property_actions
        )
        property_table_layout.addWidget(self.properties_table, 1)
        properties_layout.addWidget(property_table_panel, 1)
        left_layout.addWidget(properties_group, 1)
        self.property_name_combo.currentTextChanged.connect(
            self.change_property_value_editor
        )
        self.change_property_value_editor(
            self.property_name_combo.currentText()
        )

        days_box = QGroupBox("أيام التدريس الأسبوعية وعدد الحصص")
        days_layout = QGridLayout(days_box)
        self.day_checks = {}
        self.day_lesson_counts = {}
        days_layout.addWidget(QLabel("اليوم"), 0, 0)
        days_layout.addWidget(QLabel("عدد الحصص"), 0, 1)
        for row, (name, value) in enumerate(WEEKDAYS, start=1):
            checkbox = QCheckBox(name)
            checkbox.setChecked(value < 6)
            daily_count = QSpinBox()
            daily_count.setRange(1, 20)
            daily_count.setValue(1)
            daily_count.setEnabled(checkbox.isChecked())
            checkbox.toggled.connect(
                lambda checked, spin=daily_count: spin.setEnabled(checked)
            )
            self.day_checks[value] = checkbox
            self.day_lesson_counts[value] = daily_count
            days_layout.addWidget(checkbox, row, 0)
            days_layout.addWidget(daily_count, row, 1)
        left_layout.addWidget(days_box)

        self.date_preview = QLabel()
        self.date_preview.setStyleSheet("color: #475569;")
        left_layout.addWidget(self.date_preview)
        self.autosave_status_label = QLabel("تُحفظ التغييرات آلياً.")
        self.autosave_status_label.setStyleSheet("color: #475569;")
        right_layout.addWidget(self.autosave_status_label)
        right_layout.addStretch()
        self.start_date.dateChanged.connect(self.update_date_preview)
        self.lesson_count.valueChanged.connect(self.update_date_preview)
        for day, checkbox in self.day_checks.items():
            checkbox.toggled.connect(self.update_date_preview)
            checkbox.toggled.connect(self.schedule_autosave)
            self.day_lesson_counts[day].valueChanged.connect(
                self.update_date_preview
            )
            self.day_lesson_counts[day].valueChanged.connect(
                self.schedule_autosave
            )
        self.competence_combo.currentIndexChanged.connect(self.update_date_preview)
        self.classe_combo.currentIndexChanged.connect(
            self.update_duration_suggestion
        )
        self.majal_combo.currentIndexChanged.connect(
            self.update_duration_suggestion
        )
        if not plan:
            self.update_duration_suggestion()

        buttons = QHBoxLayout()
        close_button = QPushButton("إغلاق")
        _set_action_type(close_button, "close")
        close_button.clicked.connect(self.close_dialog)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        if plan:
            self._set_combo_data(self.majal_combo, plan[3])
            self.update_plan_name_options(selected_name=plan[1])
            self._set_combo_data(self.classe_combo, plan[2])
            self.load_competences()
            self._set_combo_data(self.competence_combo, plan[4])
            self.start_date.setDate(QDate.fromString(plan[5], "yyyy-MM-dd"))
            duration_index = self.lesson_duration_combo.findData(plan[9])
            if duration_index >= 0:
                self.lesson_duration_combo.setCurrentIndex(duration_index)
            saved_counts = plan[10:14]
            for mode_name, count in zip(
                PlanActivityDialog.MODES, saved_counts
            ):
                self.mode_lesson_counts[mode_name].setValue(count)
            self.lesson_duration_suggestion.setText(
                "مدة الحصة محفوظة مع هذا المخطط، ويمكن تعديلها."
            )
            for _property_id, key, value in db.teaching_plan_properties(plan[0]):
                self.plan_properties.append((key, value))
            self.refresh_properties_table()
            selected_days = {int(day) for day in plan[7].split(",") if day}
            saved_daily_counts = db.teaching_plan_weekday_counts(plan[0])
            for day, checkbox in self.day_checks.items():
                checkbox.setChecked(day in selected_days)
                if day in saved_daily_counts:
                    self.day_lesson_counts[day].setValue(
                        saved_daily_counts[day]
                    )
                self.day_lesson_counts[day].setEnabled(
                    checkbox.isChecked()
                )
        else:
            self.load_competences()
        for widget in (
            self.plan_name_combo,
            self.classe_combo,
            self.majal_combo,
            self.competence_combo,
            self.start_date,
            self.lesson_count,
            self.lesson_duration_combo,
            self.property_name_combo,
        ):
            widget.setLayoutDirection(Qt.RightToLeft)
        self.start_date.setAlignment(Qt.AlignRight)
        self.lesson_count.setAlignment(Qt.AlignRight)
        for count_spin in self.mode_lesson_counts.values():
            count_spin.setLayoutDirection(Qt.RightToLeft)
            count_spin.setAlignment(Qt.AlignRight)
        for combo in (
            self.classe_combo,
            self.majal_combo,
            self.competence_combo,
            self.lesson_duration_combo,
            self.property_name_combo,
        ):
            line_edit = combo.lineEdit()
            if line_edit is not None:
                line_edit.setLayoutDirection(Qt.RightToLeft)
                line_edit.setAlignment(Qt.AlignRight)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(500)
        self.autosave_timer.timeout.connect(self.save)
        self.plan_name_combo.currentIndexChanged.connect(
            self.schedule_autosave
        )
        self.classe_combo.currentIndexChanged.connect(
            self.schedule_autosave
        )
        self.majal_combo.currentIndexChanged.connect(
            self.schedule_autosave
        )
        self.competence_combo.currentIndexChanged.connect(
            self.schedule_autosave
        )
        self.start_date.dateChanged.connect(self.schedule_autosave)
        self.lesson_duration_combo.currentIndexChanged.connect(
            self.schedule_autosave
        )
        for count_spin in self.mode_lesson_counts.values():
            count_spin.valueChanged.connect(self.update_lesson_counts)
        for checkbox in self.day_checks.values():
            checkbox.toggled.connect(self.schedule_autosave)
        self.update_lesson_counts()
        self.update_date_preview()
        self._initializing = False

    @staticmethod
    def _set_combo_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def update_plan_name_options(self, *_args, selected_name=None):
        if selected_name is None:
            selected_name = self.plan_name_combo.currentData()
        majal_name = self.majal_combo.currentText().strip()
        self.plan_name_combo.blockSignals(True)
        self.plan_name_combo.clear()
        self.plan_name_combo.addItem("اختر اسم المخطط", None)
        for plan_name, plan_data in PLANS_NAMES.items():
            if plan_data.get("المجال") == majal_name:
                self.plan_name_combo.addItem(plan_name, plan_name)
        self._set_combo_data(self.plan_name_combo, selected_name)
        self.plan_name_combo.blockSignals(False)

    def refresh_properties_table(self):
        self.properties_table.setRowCount(len(self.plan_properties))
        for row, (key, value) in enumerate(self.plan_properties):
            for column, text in enumerate((key, value)):
                item = QTableWidgetItem(text)
                if column == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.properties_table.setItem(row, column, item)
        self.update_property_actions()

    def update_property_actions(self):
        has_selection = self.properties_table.currentRow() >= 0
        self.edit_property_button.setEnabled(has_selection)
        self.delete_property_button.setEnabled(has_selection)

    def save_property_form(self):
        key = self.property_name_combo.currentText().strip()
        if not key:
            QMessageBox.warning(self, "اسم مطلوب", "أدخل اسم الخاصية.")
            return
        if isinstance(self.property_value_editor, QComboBox):
            value = self.property_value_editor.currentText().strip()
        else:
            value = self.property_value_editor.toPlainText().strip()

        if self.editing_property_row is None:
            self.plan_properties.append((key, value))
        else:
            self.plan_properties[self.editing_property_row] = (key, value)
        self.refresh_properties_table()
        self.clear_property_form()
        self.schedule_autosave()

    def edit_selected_property(self):
        row = self.properties_table.currentRow()
        if not 0 <= row < len(self.plan_properties):
            return
        self.editing_property_row = row
        key, value = self.plan_properties[row]
        self.property_name_combo.setCurrentText(key)
        self.set_property_value(value)
        self.save_property_button.setText("حفظ التعديل")
        _set_action_type(self.save_property_button, "edit")
        self.cancel_property_edit_button.show()

    def remove_property_row(self):
        row = self.properties_table.currentRow()
        if not 0 <= row < len(self.plan_properties):
            return
        self.plan_properties.pop(row)
        if self.editing_property_row == row:
            self.clear_property_form()
        elif (
            self.editing_property_row is not None
            and self.editing_property_row > row
        ):
            self.editing_property_row -= 1
        self.refresh_properties_table()
        self.schedule_autosave()

    def clear_property_form(self):
        self.editing_property_row = None
        self.property_name_combo.setCurrentText(self.PROPERTY_PRESETS[0])
        self.set_property_value("")
        self.save_property_button.setText("إضافة الخاصية")
        _set_action_type(self.save_property_button, "add")
        self.cancel_property_edit_button.hide()

    def create_property_value_editor(self, key, value=""):
        if key.strip() == "مدار الاهتمام":
            editor = QComboBox()
            editor.setEditable(True)
            editor.addItems(self.INTEREST_AREAS)
            editor.setCurrentText(value)
            editor.setFont(QApplication.font())
            editor.setLayoutDirection(Qt.RightToLeft)
            editor.lineEdit().setAlignment(Qt.AlignRight)
            editor.setToolTip("اختر مجال اهتمام أو اكتب قيمة أخرى.")
            return editor

        editor = QPlainTextEdit()
        editor.setPlainText(value)
        editor.setFont(QApplication.font())
        editor.setLayoutDirection(Qt.RightToLeft)
        text_option = editor.document().defaultTextOption()
        text_option.setAlignment(Qt.AlignRight)
        editor.document().setDefaultTextOption(text_option)
        editor.setMinimumHeight(editor.fontMetrics().lineSpacing() * 5 + 16)
        editor.setPlaceholderText("اكتب المحتوى هنا...")
        editor.setToolTip("أدخل محتوى الخاصية في مساحة متعددة الأسطر.")
        return editor

    def current_property_value(self):
        if isinstance(self.property_value_editor, QComboBox):
            return self.property_value_editor.currentText()
        if isinstance(self.property_value_editor, QPlainTextEdit):
            return self.property_value_editor.toPlainText()
        return ""

    def set_property_value(self, value):
        if isinstance(self.property_value_editor, QComboBox):
            self.property_value_editor.setCurrentText(value)
        elif isinstance(self.property_value_editor, QPlainTextEdit):
            self.property_value_editor.setPlainText(value)

    def change_property_value_editor(self, key):
        wants_interest_combo = key.strip() == "مدار الاهتمام"
        if (
            self.property_value_editor is not None
            and isinstance(self.property_value_editor, QComboBox)
            == wants_interest_combo
        ):
            return
        previous_value = self.current_property_value()
        if self.property_value_editor is not None:
            self.property_value_layout.removeWidget(self.property_value_editor)
            self.property_value_editor.deleteLater()
        self.property_value_editor = self.create_property_value_editor(
            key, previous_value
        )
        self.property_value_layout.addWidget(self.property_value_editor)

    def load_competences(self, *_args):
        current_id = self.competence_combo.currentData()
        self.competence_combo.clear()
        classe_id = self.classe_combo.currentData()
        majal_id = self.majal_combo.currentData()
        if classe_id is not None and majal_id is not None:
            for competence_id, text in self.db.planning_competences_majal(
                classe_id, majal_id
            ):
                self.competence_combo.addItem(text, competence_id)
        self._set_combo_data(self.competence_combo, current_id)
        self.update_date_preview()

    def update_date_preview(self, *_args):
        if self.competence_combo.currentData() is None:
            self.date_preview.setText("لا توجد كفاية مجال مسجلة لهذا القسم والمجال.")
            return
        if self.lesson_count.value() < 1:
            self.date_preview.setText("حدد حصصاً واحدة على الأقل ضمن أنماط التعلم.")
            return
        weekly_schedule = {
            day: spin.value()
            for day, spin in self.day_lesson_counts.items()
            if self.day_checks[day].isChecked()
        }
        if not weekly_schedule:
            self.date_preview.setText(
                "اختر أيام التدريس وحدد عدد الحصص في كل يوم."
            )
            return
        start = self.start_date.date().toString("yyyy-MM-dd")
        try:
            end = self.db.calculate_teaching_plan_end_date(
                start, self.lesson_count.value(), weekly_schedule
            )
        except (ValueError, OverflowError) as exc:
            self.date_preview.setText(f"تعذر حساب تاريخ النهاية: {exc}")
            return
        self.date_preview.setText(f"تاريخ النهاية المتوقع بعد استبعاد العطل: {end}")

    def update_lesson_counts(self, *_args):
        counts = {
            mode: spin.value()
            for mode, spin in self.mode_lesson_counts.items()
        }
        total = sum(counts.values())
        self.lesson_count.setValue(total)
        methodical_ratio = (
            counts["منهجي"] * 100 / total if total else 0
        )
        if not total:
            self.mode_ratio_warning.setText(
                "حدد عدد الحصص لكل نمط؛ مجموعها يحدد عدد حصص المخطط."
            )
            self.mode_ratio_warning.setStyleSheet("color: #475569;")
        elif not 65 <= methodical_ratio <= 85:
            self.mode_ratio_warning.setText(
                f"تحذير: نسبة حصص المنهجي {methodical_ratio:.1f}%، "
                "ويُستحسن أن تكون بين 65% و85%."
            )
            self.mode_ratio_warning.setStyleSheet(
                "color: #B45309; font-weight: bold;"
            )
        else:
            self.mode_ratio_warning.setText(
                f"نسبة حصص المنهجي {methodical_ratio:.1f}% — ضمن النطاق المقترح."
            )
            self.mode_ratio_warning.setStyleSheet("color: #15803D;")
        self.update_date_preview()
        if hasattr(self, "autosave_timer"):
            self.schedule_autosave()

    def schedule_autosave(self, *_args):
        if self._initializing:
            return
        self.autosave_status_label.setText("تُحفظ التغييرات آلياً...")
        self.autosave_timer.start()

    def update_duration_suggestion(self, *_args):
        classe_id = self.classe_combo.currentData()
        majal_id = self.majal_combo.currentData()
        if classe_id is None or majal_id is None:
            self.lesson_duration_suggestion.setText(
                "لا توجد ملاحظات سابقة؛ اختر المدة المناسبة."
            )
            return
        duration, source = self.db.suggest_teaching_plan_duration(
            classe_id, majal_id
        )
        if duration is not None:
            index = self.lesson_duration_combo.findData(duration)
            if index >= 0:
                self.lesson_duration_combo.setCurrentIndex(index)
        else:
            self.lesson_duration_combo.setCurrentIndex(
                self.lesson_duration_combo.findData(60)
            )
        self.lesson_duration_suggestion.setText(source)

    def save(self):
        title = self.plan_name_combo.currentData()
        weekly_schedule = {
            day: spin.value()
            for day, spin in self.day_lesson_counts.items()
            if self.day_checks[day].isChecked()
        }
        lesson_count = sum(
            spin.value() for spin in self.mode_lesson_counts.values()
        )
        if (
            title not in PLANS_NAMES
            or self.competence_combo.currentData() is None
            or not weekly_schedule or lesson_count < 1
        ):
            self.autosave_status_label.setText(
                "اختر اسم المخطط من القائمة وأكمل الكفاية وأيام التدريس "
                "وعدد الحصص لكل يوم، وحدد حصة واحدة على الأقل لكل نمط مطلوب."
            )
            self.autosave_status_label.setStyleSheet("color: #B45309;")
            return False
        mode_counts = {
            mode: spin.value()
            for mode, spin in self.mode_lesson_counts.items()
        }
        try:
            saved_plan_id = self.db.save_teaching_plan(
                title,
                self.classe_combo.currentData(),
                self.majal_combo.currentData(),
                self.competence_combo.currentData(),
                self.start_date.date().toString("yyyy-MM-dd"),
                lesson_count,
                weekly_schedule,
                self.plan[0] if self.plan else None,
                self.plan_properties,
                self.lesson_duration_combo.currentData(),
                mode_counts,
            )
        except (ValueError, TypeError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر حفظ المخطط", str(exc))
            self.autosave_status_label.setText("تعذر الحفظ الآلي.")
            self.autosave_status_label.setStyleSheet("color: #B91C1C;")
            return False
        if self.plan is None:
            self.plan = self.db.teaching_plan(saved_plan_id)
        self.autosave_status_label.setText("تم الحفظ آلياً.")
        self.autosave_status_label.setStyleSheet("color: #15803D;")
        return True

    def close_dialog(self):
        self.autosave_timer.stop()
        if self.plan_name_combo.currentData() is not None or self.plan is not None:
            if not self.save():
                if self.plan is not None:
                    return
                answer = QMessageBox.question(
                    self,
                    "مخطط غير مكتمل",
                    "لم يُحفظ المخطط لأن بياناته الأساسية غير مكتملة. هل تريد الإغلاق؟",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
        self.accept()

    def closeEvent(self, event):
        self.autosave_timer.stop()
        if self.plan_name_combo.currentData() is not None or self.plan is not None:
            if not self.save():
                if self.plan is not None:
                    event.ignore()
                    return
                answer = QMessageBox.question(
                    self,
                    "مخطط غير مكتمل",
                    "لم يُحفظ المخطط لأن بياناته الأساسية غير مكتملة. هل تريد الإغلاق؟",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
        event.accept()


class PlanItemsDialog(QDialog):
    def __init__(self, db, plan, parent=None):
        super().__init__(parent)
        self.db = db
        self.plan = plan
        self.details = []
        self.item_id = None
        self.editing_detail_index = None
        self.loading_form = False
        self.loading_plan_number = True
        self.detail_autosave_timer = QTimer(self)
        self.detail_autosave_timer.setSingleShot(True)
        self.detail_autosave_timer.setInterval(450)
        self.detail_autosave_timer.timeout.connect(self.autosave_detail)
        self.setWindowTitle("تحديد أنشطة المخطط وكفاياتها")
        self.setLayoutDirection(Qt.RightToLeft)
        available_geometry = self.screen().availableGeometry()
        self.setFixedSize(
            min(1250, available_geometry.width()),
            min(760, available_geometry.height()),
        )

        layout = QVBoxLayout(self)
        mode_layout = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(PlanActivityDialog.MODES)
        self.mode_combo.hide()
        self.mode_group = QGroupBox("نمط التعلم")
        mode_choices_layout = QHBoxLayout(self.mode_group)
        self.mode_button_group = QButtonGroup(self)
        self.mode_button_group.setExclusive(True)
        self.mode_checkboxes = []
        for mode_name in PlanActivityDialog.MODES:
            checkbox = QCheckBox(mode_name)
            self.mode_button_group.addButton(checkbox)
            self.mode_checkboxes.append(checkbox)
            mode_choices_layout.addWidget(checkbox)
        self.mode_button_group.buttonClicked.connect(
            self.select_learning_mode
        )
        layout.addWidget(self.mode_group)
        mode_layout.addWidget(QLabel("عدد المخطط:"))
        self.plan_number_spin = QSpinBox()
        self.plan_number_spin.setRange(1, 2_147_483_647)
        self.plan_number_spin.setValue(1)
        self.plan_number_spin.setEnabled(True)
        self.plan_number_spin.valueChanged.connect(
            self.autosave_plan_number
        )
        mode_layout.addWidget(self.plan_number_spin)
        mode_layout.addStretch()
        layout.addLayout(mode_layout)
        self.pending_mode_selections = {}
        self.pending_mode_performance = {}
        self.pending_mode_targets = {}
        self.mode_objective_drafts = {}
        self.mode_activity_id = None
        self.mode_activity_mode = self.mode_combo.currentText()
        self.loading_mode_activity = False
        panels = QHBoxLayout()
        form_box = QGroupBox("بيانات النشاط ومكوّناته")
        self.form_box = form_box
        form_layout = QVBoxLayout(form_box)
        form = QFormLayout()

        self.activity_combo = QComboBox()
        self.activity_combo.setEditable(True)
        self.activity_names = db.teaching_plan_activity_names(plan[0])
        self.activity_combo.addItems(self.activity_names)
        form.addRow("المادة / النشاط:", self.activity_combo)

        self.competence_combo = QComboBox()
        self.competence_combo.setEditable(True)
        form.addRow("كفاية المادة:", self.competence_combo)

        self.component_combo = QComboBox()
        self.component_combo.setEditable(True)
        form.addRow("مكوّن الكفاية:", self.component_combo)
        self.objective_combo = QComboBox()
        self.objective_combo.setEditable(True)
        form.addRow("الهدف المميز (اختياري):", self.objective_combo)
        self.content_combo = QComboBox()
        self.content_combo.setEditable(True)
        form.addRow("المحتوى:", self.content_combo)
        for combo in (
            self.activity_combo, self.competence_combo,
            self.component_combo, self.objective_combo, self.content_combo,
        ):
            completer = combo.completer()
            if completer is not None:
                completer.setCaseSensitivity(Qt.CaseInsensitive)
                completer.setFilterMode(Qt.MatchContains)
        form_layout.addLayout(form)

        detail_buttons = QHBoxLayout()
        self.save_detail_button = QPushButton("إضافة المكوّن والمحتوى")
        _set_action_type(self.save_detail_button, "add")
        _style_primary_button(self.save_detail_button)
        self.save_detail_button.clicked.connect(self.save_detail)
        edit_detail = QPushButton("تعديل السطر المحدد")
        _set_action_type(edit_detail, "edit")
        edit_detail.clicked.connect(self.edit_detail)
        remove_detail = QPushButton("حذف السطر المحدد")
        _set_action_type(remove_detail, "delete")
        remove_detail.clicked.connect(self.remove_detail)
        detail_buttons.addWidget(self.save_detail_button)
        detail_buttons.addWidget(edit_detail)
        detail_buttons.addWidget(remove_detail)
        detail_buttons.addStretch()
        form_layout.addLayout(detail_buttons)

        self.details_table = QTableWidget(0, 3)
        self.details_table.setHorizontalHeaderLabels([
            "مكوّن الكفاية", "الهدف المميز", "المحتوى",
        ])
        self.details_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.details_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.details_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.details_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        form_layout.addWidget(self.details_table, 1)

        form_actions = QHBoxLayout()
        clear_form = QPushButton("نشاط جديد")
        _set_action_type(clear_form, "add")
        clear_form.clicked.connect(self.clear_form)
        form_actions.addWidget(clear_form)
        form_actions.addStretch()
        form_layout.addLayout(form_actions)
        panels.addWidget(form_box, 1)

        saved_box = QGroupBox("الأنشطة المختارة للمخطط")
        saved_layout = QVBoxLayout(saved_box)
        self.items_table = QTableWidget(0, 4)
        self.items_table.setHorizontalHeaderLabels([
            "النشاط", "كفاية المادة", "المكونات", "المحتويات",
        ])
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.items_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.items_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.items_table.itemSelectionChanged.connect(self.load_selected_item)
        saved_layout.addWidget(self.items_table, 1)
        self.mode_objectives_tree = QTreeWidget()
        self.mode_objectives_tree.setHeaderHidden(True)
        self.mode_objectives_tree.setMaximumHeight(360)
        self.mode_objectives_tree.itemChanged.connect(
            self.update_mode_tree_check_state
        )
        saved_layout.addWidget(self.mode_objectives_tree, 1)
        self.mode_activity_combo = QComboBox()
        saved_layout.addWidget(QLabel("النشاط المحدد للنمط:"))
        saved_layout.addWidget(self.mode_activity_combo)
        self.mode_objective_combo = CheckableComboBox()
        self.mode_objective_label = QLabel("الأهداف المميزة:")
        saved_layout.addWidget(self.mode_objective_label)
        saved_layout.addWidget(self.mode_objective_combo)
        self.target_competencies_edit = QPlainTextEdit()
        self.target_competencies_edit.setFixedHeight(
            self.target_competencies_edit.fontMetrics().lineSpacing() * 4 + 20
        )
        self.target_competencies_edit.setLayoutDirection(Qt.RightToLeft)
        self.target_competencies_label = QLabel("الكفايات المستهدفة:")
        saved_layout.addWidget(self.target_competencies_label)
        saved_layout.addWidget(self.target_competencies_edit)
        self.toggle_mode_activity_button = QPushButton("إضافة النشاط المحدد")
        _set_action_type(self.toggle_mode_activity_button, "add")
        self.toggle_mode_activity_button.clicked.connect(
            self.toggle_mode_activity
        )
        saved_layout.addWidget(self.toggle_mode_activity_button)
        self.expected_performance_edit = QPlainTextEdit()
        self.expected_performance_edit.setFixedHeight(
            self.expected_performance_edit.fontMetrics().lineSpacing() * 4 + 20
        )
        self.expected_performance_edit.setLayoutDirection(Qt.RightToLeft)
        self.expected_performance_label = QLabel("الأداء المنتظر:")
        saved_layout.addWidget(self.expected_performance_label)
        saved_layout.addWidget(self.expected_performance_edit)

        self.integration_panel = QGroupBox("أنشطة الإدماج ومحتوياتها")
        integration_layout = QHBoxLayout(self.integration_panel)
        integration_form_layout = QVBoxLayout()
        self.integration_activity_combo = QComboBox()
        self.integration_activity_combo.addItems(self.activity_names)
        integration_form_layout.addWidget(QLabel("النشاط:"))
        integration_form_layout.addWidget(self.integration_activity_combo)
        integration_form_layout.addWidget(
            QLabel("وصف النشاط (مع الإكمال التلقائي):")
        )
        self.integration_prompt_edit = AutocompletePlainTextEdit()
        self.integration_prompt_edit.setFixedHeight(
            self.integration_prompt_edit.fontMetrics().lineSpacing() * 2 + 16
        )
        self.integration_prompt_edit.setLayoutDirection(Qt.RightToLeft)
        integration_form_layout.addWidget(self.integration_prompt_edit)
        integration_form_layout.addWidget(QLabel("صيغة وضعية الإدماج:"))
        self.integration_template_combo = QComboBox()
        self.integration_template_combo.setEditable(True)
        self.integration_template_combo.addItems((
            "حل وضعيات مشكل دالة يتطلب حلها توظيف الكفايات التالية:",
            "قراءة نص سردي والإجابة شفويا وكتابيا عن أسئلة متصلة تتطلب الكفايات والمهارات التالية:",
            "إنتاج نص سردي يتطلب توظيف الكفايات والمهارات التالية:",
        ))
        integration_form_layout.addWidget(self.integration_template_combo)
        integration_form_layout.addWidget(QLabel("المحتويات المتاحة:"))
        self.integration_contents_table = QTableWidget(0, 1)
        self.integration_contents_table.setHorizontalHeaderLabels(
            ["المحتوى — اختر واحداً أو أكثر"]
        )
        self.integration_contents_table.setSelectionMode(
            QAbstractItemView.NoSelection
        )
        self.integration_contents_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.integration_contents_table.setMaximumHeight(150)
        integration_form_layout.addWidget(self.integration_contents_table)
        self.integration_target_edit = QPlainTextEdit()
        self.integration_target_edit.setFixedHeight(
            self.integration_target_edit.fontMetrics().lineSpacing() * 6 + 20
        )
        self.integration_target_edit.setLayoutDirection(Qt.RightToLeft)
        integration_buttons = QHBoxLayout()
        self.add_integration_activity_button = QPushButton("إضافة النشاط")
        _set_action_type(self.add_integration_activity_button, "add")
        _style_primary_button(self.add_integration_activity_button)
        self.add_integration_activity_button.clicked.connect(
            self.add_integration_activity
        )
        self.new_integration_activity_button = QPushButton("إضافة نشاط جديد")
        _set_action_type(self.new_integration_activity_button, "add")
        self.new_integration_activity_button.clicked.connect(
            self.new_integration_activity
        )
        integration_buttons.addWidget(self.add_integration_activity_button)
        integration_buttons.addWidget(self.new_integration_activity_button)
        integration_panel_layout = QVBoxLayout()
        integration_panel_layout.addWidget(
            QLabel("نص النشاط والكفايات المستهدفة:")
        )
        integration_panel_layout.addWidget(self.integration_target_edit)
        integration_panel_layout.addLayout(integration_buttons)
        integration_panel_layout.addWidget(
            QLabel("الأنشطة المضافة للإدماج:")
        )
        self.integration_table = QTableWidget(0, 3)
        self.integration_table.setHorizontalHeaderLabels([
            "النشاط", "صيغة الوضعية", "المحتويات المختارة",
        ])
        self.integration_table.setSelectionBehavior(
            QAbstractItemView.SelectRows
        )
        self.integration_table.setSelectionMode(
            QAbstractItemView.SingleSelection
        )
        self.integration_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self.integration_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.integration_table.itemSelectionChanged.connect(
            self.update_integration_table_actions
        )
        integration_panel_layout.addWidget(self.integration_table, 1)
        integration_table_buttons = QHBoxLayout()
        self.edit_integration_activity_button = QPushButton(
            "تعديل النشاط المحدد"
        )
        _set_action_type(self.edit_integration_activity_button, "edit")
        self.edit_integration_activity_button.clicked.connect(
            self.edit_integration_activity
        )
        self.delete_integration_activity_button = QPushButton(
            "حذف النشاط المحدد"
        )
        _set_action_type(self.delete_integration_activity_button, "delete")
        self.delete_integration_activity_button.clicked.connect(
            self.delete_integration_activity
        )
        integration_table_buttons.addWidget(
            self.edit_integration_activity_button
        )
        integration_table_buttons.addWidget(
            self.delete_integration_activity_button
        )
        integration_panel_layout.addLayout(integration_table_buttons)
        integration_layout.addLayout(integration_form_layout, 1)
        integration_layout.addLayout(integration_panel_layout, 1)
        saved_layout.addWidget(self.integration_panel, 1)
        self.integration_data_loading = False
        self.editing_integration_activity = None
        self.integration_selected_contents = set()
        self.last_integration_template = ""
        self.integration_autosave_timer = QTimer(self)
        self.integration_autosave_timer.setSingleShot(True)
        self.integration_autosave_timer.setInterval(350)
        self.integration_autosave_timer.timeout.connect(
            self.autosave_integration_draft
        )
        self.integration_activity_combo.currentIndexChanged.connect(
            self.load_integration_contents
        )
        self.integration_prompt_edit.textChanged.connect(
            self.on_integration_prompt_changed
        )
        self.integration_template_combo.currentTextChanged.connect(
            self.update_integration_template
        )
        self.integration_contents_table.itemChanged.connect(
            self.update_integration_content_selection
        )
        self.integration_target_edit.textChanged.connect(
            self.schedule_integration_autosave
        )
        self.integration_activity_combo.setCurrentIndex(
            0 if self.integration_activity_combo.count() else -1
        )

        self.evaluation_panel = QGroupBox("أنشطة التقييم ومحتوياتها")
        evaluation_layout = QHBoxLayout(self.evaluation_panel)
        evaluation_form_layout = QVBoxLayout()
        self.evaluation_activity_combo = QComboBox()
        self.evaluation_activity_combo.addItems(self.activity_names)
        evaluation_form_layout.addWidget(QLabel("النشاط:"))
        evaluation_form_layout.addWidget(self.evaluation_activity_combo)
        evaluation_form_layout.addWidget(
            QLabel("وصف النشاط (مع الإكمال التلقائي):")
        )
        self.evaluation_prompt_edit = AutocompletePlainTextEdit()
        self.evaluation_prompt_edit.setFixedHeight(
            self.evaluation_prompt_edit.fontMetrics().lineSpacing() * 2 + 16
        )
        self.evaluation_prompt_edit.setLayoutDirection(Qt.RightToLeft)
        evaluation_form_layout.addWidget(self.evaluation_prompt_edit)
        evaluation_form_layout.addWidget(QLabel("النصوص المقترحة:"))
        self.evaluation_template_combo = QComboBox()
        self.evaluation_template_combo.setEditable(True)
        self.evaluation_template_combo.addItems((
            "أن يكون المتعلم قادرا على قراءة نص سردي قراءة صامتة وسريعة "
            "ويجيب شفويا وكتابيا أن أسئلة متصلة بالمهارات والكفايات التالية:",
            "أن يكون المتعلم قادرا على إنتاج نص سردي يوظف فيه المهارات "
            "والكفايات التالية:",
            "أن يكون المتعلم قادرا على حل وضعيات مشكل دالة تتطلب توظيف "
            "المهارات والكفايات التالية:",
        ))
        evaluation_form_layout.addWidget(self.evaluation_template_combo)
        evaluation_form_layout.addWidget(QLabel("المحتويات المتاحة:"))
        self.evaluation_contents_table = QTableWidget(0, 1)
        self.evaluation_contents_table.setHorizontalHeaderLabels(
            ["المحتوى — اختر واحداً أو أكثر"]
        )
        self.evaluation_contents_table.setSelectionMode(
            QAbstractItemView.NoSelection
        )
        self.evaluation_contents_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.evaluation_contents_table.setMaximumHeight(150)
        evaluation_form_layout.addWidget(self.evaluation_contents_table)
        self.evaluation_target_edit = QPlainTextEdit()
        self.evaluation_target_edit.setFixedHeight(
            self.evaluation_target_edit.fontMetrics().lineSpacing() * 6 + 20
        )
        self.evaluation_target_edit.setLayoutDirection(Qt.RightToLeft)
        evaluation_buttons = QHBoxLayout()
        self.add_evaluation_activity_button = QPushButton("إضافة النشاط")
        _set_action_type(self.add_evaluation_activity_button, "add")
        _style_primary_button(self.add_evaluation_activity_button)
        self.add_evaluation_activity_button.clicked.connect(
            self.add_evaluation_activity
        )
        self.new_evaluation_activity_button = QPushButton("إضافة نشاط جديد")
        _set_action_type(self.new_evaluation_activity_button, "add")
        self.new_evaluation_activity_button.clicked.connect(
            self.new_evaluation_activity
        )
        evaluation_buttons.addWidget(self.add_evaluation_activity_button)
        evaluation_buttons.addWidget(self.new_evaluation_activity_button)
        evaluation_panel_layout = QVBoxLayout()
        evaluation_panel_layout.addWidget(
            QLabel("نص النشاط والأداء المنتظر:")
        )
        evaluation_panel_layout.addWidget(self.evaluation_target_edit)
        evaluation_panel_layout.addLayout(evaluation_buttons)
        evaluation_panel_layout.addWidget(
            QLabel("الأنشطة المضافة للتقييم:")
        )
        self.evaluation_table = QTableWidget(0, 3)
        self.evaluation_table.setHorizontalHeaderLabels([
            "النشاط", "النص المقترح", "المحتويات المختارة",
        ])
        self.evaluation_table.setSelectionBehavior(
            QAbstractItemView.SelectRows
        )
        self.evaluation_table.setSelectionMode(
            QAbstractItemView.SingleSelection
        )
        self.evaluation_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self.evaluation_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.evaluation_table.itemSelectionChanged.connect(
            self.update_evaluation_table_actions
        )
        evaluation_panel_layout.addWidget(self.evaluation_table, 1)
        evaluation_table_buttons = QHBoxLayout()
        self.edit_evaluation_activity_button = QPushButton(
            "تعديل النشاط المحدد"
        )
        _set_action_type(self.edit_evaluation_activity_button, "edit")
        self.edit_evaluation_activity_button.clicked.connect(
            self.edit_evaluation_activity
        )
        self.delete_evaluation_activity_button = QPushButton(
            "حذف النشاط المحدد"
        )
        _set_action_type(self.delete_evaluation_activity_button, "delete")
        self.delete_evaluation_activity_button.clicked.connect(
            self.delete_evaluation_activity
        )
        evaluation_table_buttons.addWidget(
            self.edit_evaluation_activity_button
        )
        evaluation_table_buttons.addWidget(
            self.delete_evaluation_activity_button
        )
        evaluation_panel_layout.addLayout(evaluation_table_buttons)
        evaluation_layout.addLayout(evaluation_form_layout, 1)
        evaluation_layout.addLayout(evaluation_panel_layout, 1)
        saved_layout.addWidget(self.evaluation_panel, 1)
        self.evaluation_data_loading = False
        self.editing_evaluation_activity = None
        self.evaluation_selected_contents = set()
        self.last_evaluation_template = ""
        self.evaluation_autosave_timer = QTimer(self)
        self.evaluation_autosave_timer.setSingleShot(True)
        self.evaluation_autosave_timer.setInterval(350)
        self.evaluation_autosave_timer.timeout.connect(
            self.autosave_evaluation_draft
        )
        self.evaluation_activity_combo.currentIndexChanged.connect(
            self.load_evaluation_contents
        )
        self.evaluation_prompt_edit.textChanged.connect(
            self.on_evaluation_prompt_changed
        )
        self.evaluation_template_combo.currentTextChanged.connect(
            self.update_evaluation_template
        )
        self.evaluation_contents_table.itemChanged.connect(
            self.update_evaluation_content_selection
        )
        self.evaluation_target_edit.textChanged.connect(
            self.schedule_evaluation_autosave
        )
        self.evaluation_activity_combo.setCurrentIndex(
            0 if self.evaluation_activity_combo.count() else -1
        )

        self.support_panel = QGroupBox("أنشطة الدعم والعلاج")
        support_panel_layout = QVBoxLayout(self.support_panel)
        self.support_activity_combo = QComboBox()
        self.support_activity_combo.addItems(self.activity_names)
        support_panel_layout.addWidget(QLabel("النشاط:"))
        support_panel_layout.addWidget(self.support_activity_combo)
        support_panel_layout.addWidget(QLabel("المعايير المستهدفة:"))
        self.support_target_criteria_text = (
            "أن يكون المتعلم قادرا على تجاوز الصعوبات التي ستحدد على "
            "ضوء نتائج تقييم نهاية الوحدة / الفترة"
        )
        self.support_target_criteria_edit = QPlainTextEdit()
        self.support_target_criteria_edit.setFixedHeight(
            self.support_target_criteria_edit.fontMetrics().lineSpacing() * 2
            + 20
        )
        self.support_target_criteria_edit.setLayoutDirection(Qt.RightToLeft)
        self.support_target_criteria_edit.setReadOnly(True)
        self.support_target_criteria_edit.setPlainText(
            self.support_target_criteria_text
        )
        support_panel_layout.addWidget(self.support_target_criteria_edit)
        support_buttons = QHBoxLayout()
        self.add_support_activity_button = QPushButton("إضافة النشاط")
        _set_action_type(self.add_support_activity_button, "add")
        _style_primary_button(self.add_support_activity_button)
        self.add_support_activity_button.clicked.connect(
            self.add_support_activity
        )
        self.new_support_activity_button = QPushButton("إضافة نشاط جديد")
        _set_action_type(self.new_support_activity_button, "add")
        self.new_support_activity_button.clicked.connect(
            self.new_support_activity
        )
        support_buttons.addWidget(self.add_support_activity_button)
        support_buttons.addWidget(self.new_support_activity_button)
        support_panel_layout.addLayout(support_buttons)
        support_panel_layout.addWidget(
            QLabel("الأنشطة المضافة للدعم والعلاج:")
        )
        self.support_table = QTableWidget(0, 2)
        self.support_table.setHorizontalHeaderLabels([
            "النشاط", "المعايير المستهدفة",
        ])
        self.support_table.setSelectionBehavior(
            QAbstractItemView.SelectRows
        )
        self.support_table.setSelectionMode(
            QAbstractItemView.SingleSelection
        )
        self.support_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self.support_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.support_table.itemSelectionChanged.connect(
            self.update_support_table_actions
        )
        support_panel_layout.addWidget(self.support_table, 1)
        support_table_buttons = QHBoxLayout()
        self.edit_support_activity_button = QPushButton(
            "تعديل النشاط المحدد"
        )
        _set_action_type(self.edit_support_activity_button, "edit")
        self.edit_support_activity_button.clicked.connect(
            self.edit_support_activity
        )
        self.delete_support_activity_button = QPushButton(
            "حذف النشاط المحدد"
        )
        _set_action_type(self.delete_support_activity_button, "delete")
        self.delete_support_activity_button.clicked.connect(
            self.delete_support_activity
        )
        support_table_buttons.addWidget(self.edit_support_activity_button)
        support_table_buttons.addWidget(self.delete_support_activity_button)
        support_panel_layout.addLayout(support_table_buttons)
        saved_layout.addWidget(self.support_panel, 1)
        self.editing_support_activity = None
        self.support_autosave_timer = QTimer(self)
        self.support_autosave_timer.setSingleShot(True)
        self.support_autosave_timer.setInterval(350)
        self.support_autosave_timer.timeout.connect(
            self.autosave_support_activity
        )
        self.support_activity_combo.currentIndexChanged.connect(
            self.load_support_activity
        )
        self.refresh_support_table()
        self.support_activity_combo.setCurrentIndex(
            0 if self.support_activity_combo.count() else -1
        )

        item_buttons = QHBoxLayout()
        delete_item = QPushButton("حذف النشاط المحدد")
        _set_action_type(delete_item, "delete")
        delete_item.clicked.connect(self.delete_selected_item)
        self.delete_item_button = delete_item
        finish = QPushButton("إنهاء")
        _set_action_type(finish, "close")
        _style_primary_button(finish)
        finish.clicked.connect(self.accept)
        item_buttons.addWidget(delete_item)
        item_buttons.addStretch()
        item_buttons.addWidget(finish)
        saved_layout.addLayout(item_buttons)
        panels.addWidget(saved_box, 1)
        panels_widget = QWidget()
        panels_widget.setLayout(panels)
        panels_scroll = QScrollArea()
        panels_scroll.setWidgetResizable(True)
        panels_scroll.setWidget(panels_widget)
        layout.addWidget(panels_scroll, 1)

        for combo in (
            self.activity_combo, self.competence_combo,
            self.component_combo, self.objective_combo, self.content_combo,
            self.integration_template_combo, self.evaluation_template_combo,
        ):
            line_edit = combo.lineEdit()
            if line_edit is not None:
                line_edit.setLayoutDirection(Qt.RightToLeft)
                line_edit.setAlignment(Qt.AlignRight)
        for editor in (
            self.target_competencies_edit,
            self.expected_performance_edit,
            self.integration_prompt_edit,
            self.integration_target_edit,
            self.evaluation_prompt_edit,
            self.evaluation_target_edit,
            self.support_target_criteria_edit,
        ):
            editor.setLayoutDirection(Qt.RightToLeft)
            text_option = editor.document().defaultTextOption()
            text_option.setAlignment(Qt.AlignRight)
            editor.document().setDefaultTextOption(text_option)

        self.activity_combo.currentTextChanged.connect(
            self.load_competence_suggestions
        )
        self.content_combo.activated.connect(self.schedule_detail_autosave)
        content_line_edit = self.content_combo.lineEdit()
        if content_line_edit is not None:
            content_line_edit.editingFinished.connect(
                self.schedule_detail_autosave
            )
        self.competence_combo.currentTextChanged.connect(
            self.load_component_suggestions
        )
        self.component_combo.currentTextChanged.connect(
            self.load_objective_suggestions
        )
        self.objective_combo.currentTextChanged.connect(
            self.load_content_suggestions
        )
        self.activity_combo.setInsertPolicy(QComboBox.NoInsert)
        self.competence_combo.setInsertPolicy(QComboBox.NoInsert)
        self.saved_mode_settings = db.teaching_plan_mode_settings(plan[0])
        self.pending_mode_selections = {
            mode: {
                **{
                    item_id: set(objectives)
                    for item_id, objectives in setting[
                        "selected_objectives"
                    ].items()
                },
                **{
                    item_id: set()
                    for item_id in setting["selected_items"]
                    if item_id not in setting["selected_objectives"]
                },
                **{
                    activity_name: set(objectives)
                    for activity_name, objectives in setting.get(
                        "selected_activities", {}
                    ).items()
                },
            }
            for mode, setting in self.saved_mode_settings["modes"].items()
        }
        self.mode_combo.setCurrentText(
            self.saved_mode_settings["default_mode"]
        )
        self.sync_learning_mode_checkboxes(
            self.saved_mode_settings["default_mode"]
        )
        self.last_mode = self.mode_combo.currentText()
        self.mode_combo.currentTextChanged.connect(
            self.sync_learning_mode_checkboxes
        )
        self.mode_combo.currentTextChanged.connect(self.update_mode_setup)
        self.mode_activity_combo.currentIndexChanged.connect(
            self.load_mode_activity_options
        )
        self.mode_settings_autosave_timer = QTimer(self)
        self.mode_settings_autosave_timer.setSingleShot(True)
        self.mode_settings_autosave_timer.setInterval(350)
        self.mode_settings_autosave_timer.timeout.connect(
            self.autosave_mode_settings
        )
        self.mode_objective_combo.model().itemChanged.connect(
            self.on_mode_objective_changed
        )
        self.target_competencies_edit.textChanged.connect(
            self.cache_mode_target_text
        )
        self.expected_performance_edit.textChanged.connect(
            self.schedule_mode_settings_autosave
        )
        self.load_items()
        self.update_mode_setup()
        self.load_competence_suggestions(self.activity_combo.currentText())
        self.loading_plan_number = False

    def select_learning_mode(self, button):
        self.mode_combo.setCurrentText(button.text())

    def sync_learning_mode_checkboxes(self, mode):
        for checkbox in self.mode_checkboxes:
            checkbox.setChecked(checkbox.text() == mode)

    def update_mode_setup(self, mode=None):
        previous_mode = self.last_mode
        if previous_mode and previous_mode != mode:
            self.pending_mode_selections[previous_mode] = (
                self.collect_mode_selections(previous_mode)
            )
            self.cache_mode_activity_state(previous_mode)
            self.pending_mode_performance[previous_mode] = (
                self.expected_performance_edit.toPlainText()
            )
            self.mode_settings_autosave_timer.stop()
            if previous_mode == "إدماج":
                self.autosave_integration_draft()
            elif previous_mode == "تقييم":
                self.autosave_evaluation_draft()
            elif previous_mode == "دعم وعلاج":
                self.autosave_support_activity()
            else:
                self.autosave_mode_settings(previous_mode)
        mode = mode or self.mode_combo.currentText()
        self.last_mode = mode
        methodical = mode == "منهجي"
        integration = mode == "إدماج"
        evaluation = mode == "تقييم"
        support = mode == "دعم وعلاج"
        self.form_box.setVisible(methodical)
        self.items_table.setVisible(methodical)
        self.mode_objectives_tree.hide()
        self.mode_activity_combo.setVisible(
            not methodical and not integration and not evaluation and not support
        )
        self.mode_objective_combo.hide()
        self.mode_objective_label.hide()
        self.target_competencies_edit.hide()
        self.target_competencies_label.hide()
        self.toggle_mode_activity_button.setVisible(
            not methodical and not integration and not evaluation and not support
        )
        self.expected_performance_label.hide()
        self.expected_performance_edit.hide()
        self.integration_panel.setVisible(integration)
        self.evaluation_panel.setVisible(evaluation)
        self.support_panel.setVisible(support)
        self.delete_item_button.setVisible(methodical)
        if integration:
            self.load_integration_contents()
        elif evaluation:
            self.load_evaluation_contents()
        elif support:
            self.refresh_support_table()
            self.load_support_activity()
        else:
            self.populate_mode_activity_combo()
            self.load_mode_activity_options()
        self.expected_performance_edit.setPlainText(
            self.pending_mode_performance.get(
                mode,
                self.saved_mode_settings["modes"].get(
                    mode, {}
                ).get("expected_performance", ""),
            )
        )

    def populate_mode_activity_combo(self):
        current_id = self.mode_activity_combo.currentData()
        self.mode_activity_combo.blockSignals(True)
        self.mode_activity_combo.clear()
        plan_items = self.db.teaching_plan_items(self.plan[0])
        activity_names = list(self.activity_names)
        known_names = {name.strip().casefold() for name in activity_names}
        for plan_item, _details in plan_items:
            name = str(plan_item[1] or "").strip()
            if name.casefold() not in known_names:
                activity_names.append(name)
                known_names.add(name.casefold())
        for activity_name in activity_names:
            plan_item = next(
                (
                    item for item, _details in plan_items
                    if self._same_activity_name(activity_name, item[1])
                ),
                None,
            )
            activity_key = plan_item[0] if plan_item else activity_name
            label = (
                f"{activity_name} — {plan_item[3]}"
                if plan_item else activity_name
            )
            self.mode_activity_combo.addItem(label, activity_key)
            self.mode_activity_combo.setItemData(
                self.mode_activity_combo.count() - 1,
                activity_name, Qt.UserRole + 1,
            )
        index = self.mode_activity_combo.findData(current_id)
        self.mode_activity_combo.setCurrentIndex(
            index if index >= 0 else (0 if self.mode_activity_combo.count() else -1)
        )
        self.mode_activity_combo.blockSignals(False)

    def refresh_integration_table(self):
        records = self.db.teaching_plan_integration_activities(self.plan[0])
        self.integration_table.setRowCount(len(records))
        for row, record in enumerate(records):
            activity_name, _prompt, template, _target, contents = record
            values = (
                activity_name,
                template,
                "، ".join(contents),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value or ""))
                if column == 0:
                    cell.setData(Qt.UserRole, activity_name)
                self.integration_table.setItem(row, column, cell)
        self.update_integration_table_actions()

    def update_integration_table_actions(self):
        enabled = self.integration_table.currentRow() >= 0
        self.edit_integration_activity_button.setEnabled(enabled)
        self.delete_integration_activity_button.setEnabled(enabled)

    def load_integration_contents(self, *_args):
        activity_name = self.integration_activity_combo.currentText().strip()
        if (
            not self.integration_data_loading
            and activity_name != getattr(self, "last_integration_activity", "")
        ):
            if self.editing_integration_activity:
                self.integration_autosave_timer.stop()
                self.editing_integration_activity = None
            self.integration_selected_contents.clear()
            self.integration_data_loading = True
            self.integration_prompt_edit.clear()
            self.integration_target_edit.clear()
            self.integration_data_loading = False
        self.last_integration_activity = activity_name
        contents = self.db.teaching_plan_integration_contents(
            self.plan[0], activity_name
        )
        self.integration_prompt_edit.set_suggestions(contents)
        self.integration_contents_table.blockSignals(True)
        self.integration_contents_table.setRowCount(len(contents))
        for row, content in enumerate(contents):
            item = QTableWidgetItem(content)
            item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
            )
            item.setCheckState(
                Qt.Checked
                if content in self.integration_selected_contents
                else Qt.Unchecked
            )
            self.integration_contents_table.setItem(row, 0, item)
        self.integration_contents_table.blockSignals(False)
        self.update_integration_target()

    def update_integration_content_selection(self, item):
        if item is None or self.integration_data_loading:
            return
        content = item.text().strip()
        if item.checkState() == Qt.Checked:
            self.integration_selected_contents.add(content)
        else:
            self.integration_selected_contents.discard(content)
        self.update_integration_target()
        self.schedule_integration_autosave()

    def update_integration_template(self, template):
        if self.integration_data_loading:
            return
        previous = self.last_integration_template
        prompt = self.integration_prompt_edit.toPlainText().strip()
        if not prompt or prompt == previous:
            self.integration_data_loading = True
            self.integration_prompt_edit.setPlainText(template)
            self.integration_data_loading = False
        self.last_integration_template = template
        self.update_integration_target()
        self.schedule_integration_autosave()

    def on_integration_prompt_changed(self):
        if self.integration_data_loading:
            return
        self.update_integration_target()
        self.schedule_integration_autosave()

    def update_integration_target(self, *_args):
        prompt = self.integration_prompt_edit.toPlainText().strip()
        if not prompt:
            prompt = self.integration_template_combo.currentText().strip()
        selected_contents = [
            self.integration_contents_table.item(row, 0).text()
            for row in range(self.integration_contents_table.rowCount())
            if self.integration_contents_table.item(
                row, 0
            ).checkState() == Qt.Checked
        ]
        target_text = "\n".join(
            part for part in (prompt, *selected_contents) if part
        )
        if self.integration_target_edit.toPlainText() != target_text:
            self.integration_data_loading = True
            self.integration_target_edit.setPlainText(target_text)
            self.integration_data_loading = False

    def schedule_integration_autosave(self, *_args):
        if (
            not self.integration_data_loading
            and self.editing_integration_activity
        ):
            self.integration_autosave_timer.start()

    def autosave_integration_draft(self):
        if not self.editing_integration_activity:
            return True
        activity_name = self.integration_activity_combo.currentText().strip()
        if activity_name != self.editing_integration_activity:
            return True
        selected_contents = [
            self.integration_contents_table.item(row, 0).text()
            for row in range(self.integration_contents_table.rowCount())
            if self.integration_contents_table.item(
                row, 0
            ).checkState() == Qt.Checked
        ]
        try:
            self.db.save_teaching_plan_integration_activity(
                self.plan[0],
                activity_name,
                self.integration_prompt_edit.toPlainText(),
                self.integration_template_combo.currentText(),
                self.integration_target_edit.toPlainText(),
                selected_contents,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self, "تعذر الحفظ التلقائي لنشاط الإدماج", str(exc)
            )
            return False
        self.refresh_integration_table()
        return True

    def add_integration_activity(self):
        activity_name = self.integration_activity_combo.currentText().strip()
        if not activity_name:
            QMessageBox.warning(self, "اختيار مطلوب", "اختر نشاطاً أولاً.")
            return
        selected_contents = [
            self.integration_contents_table.item(row, 0).text()
            for row in range(self.integration_contents_table.rowCount())
            if self.integration_contents_table.item(
                row, 0
            ).checkState() == Qt.Checked
        ]
        try:
            self.db.save_teaching_plan_integration_activity(
                self.plan[0],
                activity_name,
                self.integration_prompt_edit.toPlainText(),
                self.integration_template_combo.currentText(),
                self.integration_target_edit.toPlainText(),
                selected_contents,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر إضافة نشاط الإدماج", str(exc))
            return
        self.editing_integration_activity = activity_name
        self.integration_activity_combo.setEnabled(False)
        self.refresh_integration_table()

    def new_integration_activity(self):
        self.integration_autosave_timer.stop()
        self.editing_integration_activity = None
        self.integration_activity_combo.setEnabled(True)
        existing = {
            record[0]
            for record in self.db.teaching_plan_integration_activities(
                self.plan[0]
            )
        }
        next_index = next(
            (
                index for index in range(self.integration_activity_combo.count())
                if self.integration_activity_combo.itemText(index) not in existing
            ),
            0,
        )
        self.integration_activity_combo.setCurrentIndex(next_index)
        self.integration_data_loading = True
        self.integration_selected_contents.clear()
        self.integration_prompt_edit.clear()
        self.integration_template_combo.setCurrentIndex(0)
        self.last_integration_template = (
            self.integration_template_combo.currentText()
        )
        self.integration_data_loading = False
        self.load_integration_contents()
        self.integration_prompt_edit.setFocus()

    def edit_integration_activity(self):
        row = self.integration_table.currentRow()
        if row < 0:
            return
        activity_name = self.integration_table.item(
            row, 0
        ).data(Qt.UserRole)
        record = next(
            (
                value for value
                in self.db.teaching_plan_integration_activities(self.plan[0])
                if value[0] == activity_name
            ),
            None,
        )
        if record is None:
            return
        _, prompt, template, target, selected_contents = record
        self.integration_autosave_timer.stop()
        self.integration_data_loading = True
        self.editing_integration_activity = activity_name
        self.integration_activity_combo.setEnabled(False)
        self.integration_selected_contents = set(selected_contents)
        index = self.integration_activity_combo.findText(
            activity_name, Qt.MatchFixedString
        )
        self.integration_activity_combo.setCurrentIndex(index)
        self.integration_template_combo.setCurrentText(template)
        self.last_integration_template = template
        self.integration_prompt_edit.setPlainText(prompt)
        self.integration_target_edit.setPlainText(target)
        self.integration_data_loading = False
        self.load_integration_contents()
        self.integration_data_loading = True
        self.integration_target_edit.setPlainText(target)
        self.integration_data_loading = False

    def delete_integration_activity(self):
        row = self.integration_table.currentRow()
        if row < 0:
            return
        activity_name = self.integration_table.item(
            row, 0
        ).data(Qt.UserRole)
        try:
            self.db.delete_teaching_plan_integration_activity(
                self.plan[0], activity_name
            )
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "تعذر حذف نشاط الإدماج", str(exc))
            return
        if self.editing_integration_activity == activity_name:
            self.integration_autosave_timer.stop()
            self.editing_integration_activity = None
            self.integration_activity_combo.setEnabled(True)
        self.refresh_integration_table()

    def refresh_evaluation_table(self):
        records = self.db.teaching_plan_evaluation_activities(self.plan[0])
        self.evaluation_table.setRowCount(len(records))
        for row, record in enumerate(records):
            activity_name, _prompt, template, _target, contents = record
            values = (
                activity_name,
                template,
                "، ".join(contents),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value or ""))
                if column == 0:
                    cell.setData(Qt.UserRole, activity_name)
                self.evaluation_table.setItem(row, column, cell)
        self.update_evaluation_table_actions()

    def update_evaluation_table_actions(self):
        enabled = self.evaluation_table.currentRow() >= 0
        self.edit_evaluation_activity_button.setEnabled(enabled)
        self.delete_evaluation_activity_button.setEnabled(enabled)

    def load_evaluation_contents(self, *_args):
        activity_name = self.evaluation_activity_combo.currentText().strip()
        if (
            not self.evaluation_data_loading
            and activity_name != getattr(self, "last_evaluation_activity", "")
        ):
            if self.editing_evaluation_activity:
                self.evaluation_autosave_timer.stop()
                self.editing_evaluation_activity = None
            self.evaluation_selected_contents.clear()
            self.evaluation_data_loading = True
            self.evaluation_prompt_edit.clear()
            self.evaluation_target_edit.clear()
            self.evaluation_data_loading = False
        self.last_evaluation_activity = activity_name
        contents = self.db.teaching_plan_integration_contents(
            self.plan[0], activity_name
        )
        self.evaluation_prompt_edit.set_suggestions(contents)
        self.evaluation_contents_table.blockSignals(True)
        self.evaluation_contents_table.setRowCount(len(contents))
        for row, content in enumerate(contents):
            item = QTableWidgetItem(content)
            item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
            )
            item.setCheckState(
                Qt.Checked
                if content in self.evaluation_selected_contents
                else Qt.Unchecked
            )
            self.evaluation_contents_table.setItem(row, 0, item)
        self.evaluation_contents_table.blockSignals(False)
        self.update_evaluation_target()

    def update_evaluation_content_selection(self, item):
        if item is None or self.evaluation_data_loading:
            return
        content = item.text().strip()
        if item.checkState() == Qt.Checked:
            self.evaluation_selected_contents.add(content)
        else:
            self.evaluation_selected_contents.discard(content)
        self.update_evaluation_target()
        self.schedule_evaluation_autosave()

    def update_evaluation_template(self, template):
        if self.evaluation_data_loading:
            return
        previous = self.last_evaluation_template
        prompt = self.evaluation_prompt_edit.toPlainText().strip()
        if not prompt or prompt == previous:
            self.evaluation_data_loading = True
            self.evaluation_prompt_edit.setPlainText(template)
            self.evaluation_data_loading = False
        self.last_evaluation_template = template
        self.update_evaluation_target()
        self.schedule_evaluation_autosave()

    def on_evaluation_prompt_changed(self):
        if self.evaluation_data_loading:
            return
        self.update_evaluation_target()
        self.schedule_evaluation_autosave()

    def update_evaluation_target(self, *_args):
        prompt = self.evaluation_prompt_edit.toPlainText().strip()
        if not prompt:
            prompt = self.evaluation_template_combo.currentText().strip()
        selected_contents = [
            self.evaluation_contents_table.item(row, 0).text()
            for row in range(self.evaluation_contents_table.rowCount())
            if self.evaluation_contents_table.item(
                row, 0
            ).checkState() == Qt.Checked
        ]
        target_text = "\n".join(
            part for part in (prompt, *selected_contents) if part
        )
        if self.evaluation_target_edit.toPlainText() != target_text:
            self.evaluation_data_loading = True
            self.evaluation_target_edit.setPlainText(target_text)
            self.evaluation_data_loading = False

    def schedule_evaluation_autosave(self, *_args):
        if (
            not self.evaluation_data_loading
            and self.editing_evaluation_activity
        ):
            self.evaluation_autosave_timer.start()

    def autosave_evaluation_draft(self):
        if not self.editing_evaluation_activity:
            return True
        activity_name = self.evaluation_activity_combo.currentText().strip()
        if activity_name != self.editing_evaluation_activity:
            return True
        selected_contents = [
            self.evaluation_contents_table.item(row, 0).text()
            for row in range(self.evaluation_contents_table.rowCount())
            if self.evaluation_contents_table.item(
                row, 0
            ).checkState() == Qt.Checked
        ]
        try:
            self.db.save_teaching_plan_evaluation_activity(
                self.plan[0],
                activity_name,
                self.evaluation_prompt_edit.toPlainText(),
                self.evaluation_template_combo.currentText(),
                self.evaluation_target_edit.toPlainText(),
                selected_contents,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self, "تعذر الحفظ التلقائي لنشاط التقييم", str(exc)
            )
            return False
        self.refresh_evaluation_table()
        return True

    def add_evaluation_activity(self):
        activity_name = self.evaluation_activity_combo.currentText().strip()
        if not activity_name:
            QMessageBox.warning(self, "اختيار مطلوب", "اختر نشاطاً أولاً.")
            return
        selected_contents = [
            self.evaluation_contents_table.item(row, 0).text()
            for row in range(self.evaluation_contents_table.rowCount())
            if self.evaluation_contents_table.item(
                row, 0
            ).checkState() == Qt.Checked
        ]
        try:
            self.db.save_teaching_plan_evaluation_activity(
                self.plan[0],
                activity_name,
                self.evaluation_prompt_edit.toPlainText(),
                self.evaluation_template_combo.currentText(),
                self.evaluation_target_edit.toPlainText(),
                selected_contents,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر إضافة نشاط التقييم", str(exc))
            return
        self.editing_evaluation_activity = activity_name
        self.evaluation_activity_combo.setEnabled(False)
        self.refresh_evaluation_table()

    def new_evaluation_activity(self):
        self.evaluation_autosave_timer.stop()
        self.editing_evaluation_activity = None
        self.evaluation_activity_combo.setEnabled(True)
        existing = {
            record[0]
            for record in self.db.teaching_plan_evaluation_activities(
                self.plan[0]
            )
        }
        next_index = next(
            (
                index
                for index in range(self.evaluation_activity_combo.count())
                if self.evaluation_activity_combo.itemText(index) not in existing
            ),
            0,
        )
        self.evaluation_activity_combo.setCurrentIndex(next_index)
        self.evaluation_data_loading = True
        self.evaluation_selected_contents.clear()
        self.evaluation_prompt_edit.clear()
        self.evaluation_template_combo.setCurrentIndex(0)
        self.last_evaluation_template = (
            self.evaluation_template_combo.currentText()
        )
        self.evaluation_data_loading = False
        self.load_evaluation_contents()
        self.evaluation_prompt_edit.setFocus()

    def edit_evaluation_activity(self):
        row = self.evaluation_table.currentRow()
        if row < 0:
            return
        activity_name = self.evaluation_table.item(
            row, 0
        ).data(Qt.UserRole)
        record = next(
            (
                value
                for value in self.db.teaching_plan_evaluation_activities(
                    self.plan[0]
                )
                if value[0] == activity_name
            ),
            None,
        )
        if record is None:
            return
        _, prompt, template, target, selected_contents = record
        self.evaluation_autosave_timer.stop()
        self.evaluation_data_loading = True
        self.editing_evaluation_activity = activity_name
        self.evaluation_activity_combo.setEnabled(False)
        self.evaluation_selected_contents = set(selected_contents)
        index = self.evaluation_activity_combo.findText(
            activity_name, Qt.MatchFixedString
        )
        self.evaluation_activity_combo.setCurrentIndex(index)
        self.evaluation_template_combo.setCurrentText(template)
        self.last_evaluation_template = template
        self.evaluation_prompt_edit.setPlainText(prompt)
        self.evaluation_target_edit.setPlainText(target)
        self.evaluation_data_loading = False
        self.load_evaluation_contents()
        self.evaluation_data_loading = True
        self.evaluation_target_edit.setPlainText(target)
        self.evaluation_data_loading = False

    def delete_evaluation_activity(self):
        row = self.evaluation_table.currentRow()
        if row < 0:
            return
        activity_name = self.evaluation_table.item(
            row, 0
        ).data(Qt.UserRole)
        try:
            self.db.delete_teaching_plan_evaluation_activity(
                self.plan[0], activity_name
            )
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "تعذر حذف نشاط التقييم", str(exc))
            return
        if self.editing_evaluation_activity == activity_name:
            self.evaluation_autosave_timer.stop()
            self.editing_evaluation_activity = None
            self.evaluation_activity_combo.setEnabled(True)
        self.refresh_evaluation_table()

    def refresh_support_table(self):
        records = self.db.teaching_plan_support_activities(self.plan[0])
        self.support_table.setRowCount(len(records))
        for row, (activity_name, _target_criteria) in enumerate(records):
            target_criteria = self.support_target_criteria_text
            for column, value in enumerate((activity_name, target_criteria)):
                cell = QTableWidgetItem(str(value or ""))
                if column == 0:
                    cell.setData(Qt.UserRole, activity_name)
                self.support_table.setItem(row, column, cell)
        self.update_support_table_actions()

    def update_support_table_actions(self):
        enabled = self.support_table.currentRow() >= 0
        self.edit_support_activity_button.setEnabled(enabled)
        self.delete_support_activity_button.setEnabled(enabled)

    def load_support_activity(self, *_args):
        activity_name = self.support_activity_combo.currentText().strip()
        if self.editing_support_activity:
            if activity_name != self.editing_support_activity:
                self.support_autosave_timer.stop()
                self.editing_support_activity = None
                self.support_activity_combo.setEnabled(True)
        self.support_target_criteria_edit.blockSignals(True)
        self.support_target_criteria_edit.setPlainText(
            self.support_target_criteria_text
        )
        self.support_target_criteria_edit.blockSignals(False)

    def autosave_support_activity(self):
        activity_name = self.editing_support_activity
        if not activity_name:
            return True
        if self.support_activity_combo.currentText().strip() != activity_name:
            return True
        try:
            self.db.save_teaching_plan_support_activity(
                self.plan[0],
                activity_name,
                self.support_target_criteria_text,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self, "تعذر الحفظ التلقائي لمعايير الدعم والعلاج", str(exc)
            )
            return False
        self.refresh_support_table()
        return True

    def add_support_activity(self):
        activity_name = self.support_activity_combo.currentText().strip()
        if not activity_name:
            QMessageBox.warning(self, "اختيار مطلوب", "اختر نشاطاً أولاً.")
            return
        try:
            self.db.save_teaching_plan_support_activity(
                self.plan[0],
                activity_name,
                self.support_target_criteria_text,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self, "تعذر إضافة نشاط الدعم والعلاج", str(exc)
            )
            return
        self.editing_support_activity = activity_name
        self.support_activity_combo.setEnabled(False)
        self.refresh_support_table()

    def new_support_activity(self):
        self.support_autosave_timer.stop()
        self.editing_support_activity = None
        self.support_activity_combo.setEnabled(True)
        existing = {
            record[0]
            for record in self.db.teaching_plan_support_activities(
                self.plan[0]
            )
        }
        next_index = next(
            (
                index
                for index in range(self.support_activity_combo.count())
                if self.support_activity_combo.itemText(index) not in existing
            ),
            0,
        )
        self.support_activity_combo.setCurrentIndex(next_index)
        self.support_target_criteria_edit.setPlainText(
            self.support_target_criteria_text
        )
        self.support_target_criteria_edit.setFocus()

    def edit_support_activity(self):
        row = self.support_table.currentRow()
        if row < 0:
            return
        activity_name = self.support_table.item(
            row, 0
        ).data(Qt.UserRole)
        index = self.support_activity_combo.findText(
            activity_name, Qt.MatchFixedString
        )
        if index < 0:
            return
        self.support_activity_combo.setCurrentIndex(index)
        self.editing_support_activity = activity_name
        self.support_activity_combo.setEnabled(False)
        self.load_support_activity()
        self.support_target_criteria_edit.setFocus()

    def delete_support_activity(self):
        row = self.support_table.currentRow()
        if row < 0:
            return
        activity_name = self.support_table.item(
            row, 0
        ).data(Qt.UserRole)
        try:
            self.db.delete_teaching_plan_support_activity(
                self.plan[0], activity_name
            )
        except sqlite3.Error as exc:
            QMessageBox.warning(
                self, "تعذر حذف نشاط الدعم والعلاج", str(exc)
            )
            return
        if self.editing_support_activity == activity_name:
            self.support_autosave_timer.stop()
            self.editing_support_activity = None
            self.support_activity_combo.setEnabled(True)
        self.refresh_support_table()

    @staticmethod
    def _same_activity_name(first, second):
        first = str(first or "").strip().casefold()
        second = str(second or "").strip().casefold()
        if first == second:
            return True
        first_aliases = {
            alias.casefold()
            for alias in PLAN_ACTIVITY_ALIASES.get(first, ())
        }
        second_aliases = {
            alias.casefold()
            for alias in PLAN_ACTIVITY_ALIASES.get(second, ())
        }
        return second in first_aliases or first in second_aliases

    def cache_mode_activity_state(self, mode=None):
        mode = mode or self.mode_activity_mode
        item_id = self.mode_activity_id
        if item_id is None:
            return
        selections = self.pending_mode_selections.setdefault(mode, {})
        if mode in ("إدماج", "تقييم"):
            draft_key = (mode, item_id)
            objectives = self.current_target_objectives()
            self.mode_objective_drafts[draft_key] = objectives
            if item_id in selections:
                selections[item_id] = objectives
            self.pending_mode_targets.setdefault(mode, {})[item_id] = (
                self.target_competencies_edit.toPlainText()
            )
        elif mode == "دعم وعلاج" and item_id in selections:
            selections[item_id] = set()

    def load_mode_activity_options(self, *_args):
        self.cache_mode_activity_state()
        item_id = self.mode_activity_combo.currentData()
        mode = self.mode_combo.currentText()
        self.loading_mode_activity = True
        self.mode_activity_id = item_id
        self.mode_objective_combo.blockSignals(True)
        self.mode_objective_combo.model().blockSignals(True)
        self.mode_objective_combo.clear()
        if item_id is not None and mode in ("إدماج", "تقييم"):
            saved_mode = self.saved_mode_settings["modes"].get(mode, {})
            selected = self.mode_objective_drafts.get(
                (mode, item_id),
                self.pending_mode_selections.get(mode, {}).get(
                    item_id,
                    saved_mode.get("selected_objectives", {}).get(
                        item_id, set()
                    ),
                ),
            )
            for objective in self.mode_objectives_for_activity(item_id):
                self.mode_objective_combo.add_checkable_item(
                    objective, objective in selected
                )
            for objective in sorted(set(selected)):
                if self.mode_objective_combo.findText(
                    objective, Qt.MatchFixedString
                ) < 0:
                    self.mode_objective_combo.add_checkable_item(
                        objective, checked=True
                    )
            target_text = self.pending_mode_targets.get(mode, {}).get(
                item_id, "\n" + "\n".join(sorted(selected))
            )
            self.target_competencies_edit.setPlainText(target_text)
        else:
            self.target_competencies_edit.clear()
        self.mode_objective_combo.model().blockSignals(False)
        self.mode_objective_combo.blockSignals(False)
        self.mode_objective_combo.refresh_text()
        self.mode_activity_mode = mode
        self.loading_mode_activity = False
        selected_ids = set(self.collect_mode_selections(mode))
        self.toggle_mode_activity_button.setText(
            "إزالة النشاط المحدد"
            if item_id in selected_ids else "إضافة النشاط المحدد"
        )
        _set_action_type(
            self.toggle_mode_activity_button,
            "delete" if item_id in selected_ids else "add",
        )

    def mode_objectives_for_activity(self, item_id):
        objectives = []
        activity_name = str(
            self.mode_activity_combo.currentData(Qt.UserRole + 1) or ""
        ).strip()
        for item, details in self.db.teaching_plan_items(self.plan[0]):
            if (
                item[0] == item_id
                or self._same_activity_name(item[1], activity_name)
            ):
                for detail in details:
                    objective = str(detail[3] or "").strip()
                    if objective and objective not in objectives:
                        objectives.append(objective)
        if not objectives:
            for competence_id, _activity_name, competence_text in (
                self.db.teaching_plan_item_competences(
                    self.plan[0], activity_name
                )
            ):
                for suggestion in self.db.teaching_plan_item_suggestions(
                    self.plan[0], competence_id, competence_text
                ):
                    objective = str(suggestion[6] or "").strip()
                    if objective and objective not in objectives:
                        objectives.append(objective)
        for _item, details in self.db.teaching_plan_items(self.plan[0]):
            for detail in details:
                objective = str(detail[3] or "").strip()
                if objective and objective not in objectives:
                    objectives.append(objective)
        return objectives

    def update_mode_objective_text(self, _item):
        item_id = self.mode_activity_combo.currentData()
        if item_id is None:
            return
        mode = self.mode_activity_mode
        selected = set(self.mode_objective_combo.checked_texts())
        self.mode_objective_drafts[(mode, item_id)] = selected
        if item_id in self.pending_mode_selections.setdefault(mode, {}):
            self.pending_mode_selections[mode][item_id] = selected
        text = "\n" + "\n".join(self.mode_objective_combo.checked_texts())
        self.pending_mode_targets.setdefault(mode, {})[item_id] = text
        self.target_competencies_edit.setPlainText(text)
        self.schedule_mode_settings_autosave()

    def on_mode_objective_changed(self, item):
        self.update_mode_objective_text(item)

    def schedule_mode_settings_autosave(self, *_args):
        if not self.loading_mode_activity:
            self.mode_settings_autosave_timer.start()

    def current_target_objectives(self):
        return {
            line.strip()
            for line in self.target_competencies_edit.toPlainText().splitlines()
            if line.strip()
        }

    def cache_mode_target_text(self):
        item_id = self.mode_activity_combo.currentData()
        mode = self.mode_activity_mode
        if (
            self.loading_mode_activity or item_id is None
            or mode not in ("إدماج", "تقييم")
        ):
            return
        self.pending_mode_targets.setdefault(mode, {})[item_id] = (
            self.target_competencies_edit.toPlainText()
        )
        self.schedule_mode_settings_autosave()

    def toggle_mode_activity(self):
        item_id = self.mode_activity_combo.currentData()
        if item_id is None:
            return
        mode = self.mode_activity_mode
        selections = self.pending_mode_selections.setdefault(mode, {})
        if item_id in selections:
            del selections[item_id]
        elif mode in ("إدماج", "تقييم"):
            objectives = self.current_target_objectives()
            if not objectives:
                QMessageBox.warning(
                    self, "اختيار مطلوب",
                    "اختر هدفاً مميزاً واحداً على الأقل لهذا النشاط.",
                )
                return
            selections[item_id] = objectives
        else:
            selections[item_id] = set()
        self.load_mode_activity_options()
        self.schedule_mode_settings_autosave()

    def populate_mode_objectives_tree(self):
        mode = self.mode_combo.currentText()
        previous_selection = self.pending_mode_selections.get(mode)
        if previous_selection is None:
            previous_selection = self.saved_mode_settings["modes"].get(
                mode, {}
            ).get("selected_objectives", {})
        saved_items = self.saved_mode_settings["modes"].get(
            mode, {}
        ).get("selected_items", set())
        all_objectives = []
        for _plan_item, details in self.db.teaching_plan_items(self.plan[0]):
            for detail in details:
                objective = str(detail[3] or "").strip()
                if objective and objective not in all_objectives:
                    all_objectives.append(objective)

        self.mode_objectives_tree.blockSignals(True)
        self.mode_objectives_tree.clear()
        for plan_item, details in self.db.teaching_plan_items(self.plan[0]):
            item_id = plan_item[0]
            activity_node = QTreeWidgetItem([
                f"{plan_item[1]} — {plan_item[3]}"
            ])
            activity_node.setData(0, Qt.UserRole, item_id)
            activity_node.setFlags(
                activity_node.flags()
                | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
            )
            activity_node.setCheckState(
                0, Qt.Checked if item_id in saved_items else Qt.Unchecked
            )
            if mode in ("إدماج", "تقييم"):
                objectives = []
                for detail in details:
                    objective = str(detail[3] or "").strip()
                    if objective and objective not in objectives:
                        objectives.append(objective)
                if not objectives:
                    objectives = all_objectives
                selected_objectives = previous_selection.get(
                    item_id, set()
                )
                for objective in objectives:
                    objective_node = QTreeWidgetItem([objective])
                    objective_node.setData(0, Qt.UserRole, objective)
                    objective_node.setFlags(
                        objective_node.flags()
                        | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                    )
                    objective_node.setCheckState(
                        0,
                        Qt.Checked
                        if objective in selected_objectives
                        else Qt.Unchecked,
                    )
                    activity_node.addChild(objective_node)
            self.mode_objectives_tree.addTopLevelItem(activity_node)
        self.mode_objectives_tree.blockSignals(False)

    def update_mode_tree_check_state(self, item, _column):
        if item.parent() is not None:
            parent = item.parent()
            selected_count = sum(
                parent.child(index).checkState(0) == Qt.Checked
                for index in range(parent.childCount())
            )
            if selected_count == 0:
                state = Qt.Unchecked
            elif selected_count == parent.childCount():
                state = Qt.Checked
            else:
                state = Qt.PartiallyChecked
            parent.setCheckState(0, state)
        else:
            state = item.checkState(0)
            for index in range(item.childCount()):
                item.child(index).setCheckState(0, state)
        self.schedule_mode_settings_autosave()

    def collect_mode_selections(self, mode=None):
        mode = mode or self.mode_combo.currentText()
        self.cache_mode_activity_state(mode)
        return {
            item_id: set(objectives)
            for item_id, objectives in self.pending_mode_selections.get(
                mode, {}
            ).items()
        }

    def autosave_mode_settings(self, mode=None):
        mode = mode or self.mode_combo.currentText()
        if mode == "إدماج":
            return True
        selections = (
            self.collect_mode_selections(mode)
            if mode != "منهجي" else {}
        )
        try:
            self.db.save_teaching_plan_mode_settings(
                self.plan[0],
                mode,
                selections,
                self.expected_performance_edit.toPlainText()
                if mode == "تقييم" else "",
            )
        except ValueError as exc:
            QMessageBox.warning(self, "تعذر الحفظ التلقائي", str(exc))
            return False
        self.saved_mode_settings = self.db.teaching_plan_mode_settings(
            self.plan[0]
        )
        setting = self.saved_mode_settings["modes"].get(mode, {})
        self.pending_mode_selections[mode] = {
            **{
                item_id: set(objectives)
                for item_id, objectives in setting.get(
                    "selected_objectives", {}
                ).items()
            },
            **{
                item_id: set()
                for item_id in setting.get("selected_items", set())
                if item_id not in setting.get("selected_objectives", {})
            },
            **{
                activity_name: set(objectives)
                for activity_name, objectives in setting.get(
                    "selected_activities", {}
                ).items()
            },
        }
        self.pending_mode_performance.pop(mode, None)
        return True

    @staticmethod
    def _prepare_suggestion_combo(combo, suggestions, selected_text=""):
        combo.blockSignals(True)
        combo.clear()
        seen = set()
        for text, record_id in suggestions:
            text = str(text or "").strip()
            if not text or (text, record_id) in seen:
                continue
            seen.add((text, record_id))
            combo.addItem(text, record_id)
        combo.setCurrentText(selected_text)
        combo.blockSignals(False)

    @staticmethod
    def _selected_record_id(combo):
        index = combo.findText(combo.currentText(), Qt.MatchExactly)
        return combo.itemData(index) if index >= 0 else None

    def load_competence_suggestions(self, activity_name):
        rows = self.db.teaching_plan_item_competences(
            self.plan[0], activity_name.strip()
        ) if activity_name.strip() else []
        selected_competence = rows[0][2] if rows else ""
        self._prepare_suggestion_combo(
            self.competence_combo,
            [(row[2], row[0]) for row in rows],
            selected_competence,
        )
        self.component_combo.setCurrentText("")
        self.objective_combo.setCurrentText("")
        self.content_combo.setCurrentText("")
        self.load_component_suggestions()

    def suggestion_rows(self):
        competence_text = self.competence_combo.currentText().strip()
        if not competence_text:
            return []
        competence_id = self._selected_record_id(self.competence_combo)
        return self.db.teaching_plan_item_suggestions(
            self.plan[0], competence_id,
            competence_text,
        )

    def load_component_suggestions(self, *_args):
        rows = self.suggestion_rows()
        self._prepare_suggestion_combo(
            self.component_combo,
            [(row[4], row[3]) for row in rows if row[4]],
            "",
        )
        self.objective_combo.setCurrentText("")
        self.content_combo.setCurrentText("")
        self.load_objective_suggestions()

    def load_objective_suggestions(self, *_args):
        rows = self.suggestion_rows()
        component_id = self._selected_record_id(self.component_combo)
        if component_id is not None:
            rows = [row for row in rows if row[3] == component_id]
        else:
            component_text = self.component_combo.currentText().strip()
            rows = [
                row for row in rows
                if str(row[4] or "").strip() == component_text
            ] if component_text else []
        suggestions = [(row[6], row[5]) for row in rows if row[6]]
        unique_suggestions = []
        seen_texts = set()
        for text, record_id in suggestions:
            normalized = str(text or "").strip()
            if normalized and normalized not in seen_texts:
                seen_texts.add(normalized)
                unique_suggestions.append((normalized, record_id))
        self._prepare_suggestion_combo(
            self.objective_combo,
            unique_suggestions,
            "",
        )
        self.content_combo.setCurrentText("")
        self.load_content_suggestions()

    def load_content_suggestions(self, *_args):
        rows = self.suggestion_rows()
        component_id = self._selected_record_id(self.component_combo)
        objective_id = self._selected_record_id(self.objective_combo)
        if component_id is not None:
            rows = [row for row in rows if row[3] == component_id]
        else:
            component_text = self.component_combo.currentText().strip()
            rows = [
                row for row in rows
                if str(row[4] or "").strip() == component_text
            ] if component_text else []
        if objective_id is not None:
            rows = [row for row in rows if row[5] == objective_id]
        else:
            objective_text = self.objective_combo.currentText().strip()
            rows = [
                row for row in rows
                if str(row[6] or "").strip() == objective_text
            ] if objective_text else []
        if not self.competence_combo.currentText().strip():
            rows = []
        self._prepare_suggestion_combo(
            self.content_combo,
            [(row[8], row[7]) for row in rows if row[8]],
            "",
        )

    def save_detail(self):
        if (
            not self.activity_combo.currentText().strip()
            or not self.competence_combo.currentText().strip()
        ):
            QMessageBox.warning(
                self, "بيانات ناقصة",
                "حدد النشاط وكفايته قبل إضافة مكوّن أو محتوى.",
            )
            return
        component_text = self.component_combo.currentText().strip()
        objective_text = self.objective_combo.currentText().strip()
        content_text = self.content_combo.currentText().strip()
        detail = (
            self._selected_record_id(self.component_combo),
            component_text,
            self._selected_record_id(self.objective_combo),
            objective_text,
            self._selected_record_id(self.content_combo),
            content_text,
        )
        if self.editing_detail_index is None:
            self.details.append(detail)
            row = self.details_table.rowCount()
            self.details_table.insertRow(row)
        else:
            row = self.editing_detail_index
            self.details[row] = detail
        for column, text in enumerate((
            component_text, objective_text, content_text,
        )):
            self.details_table.setItem(row, column, QTableWidgetItem(text))
        self.editing_detail_index = None
        self.save_detail_button.setText("إضافة المكوّن والمحتوى")
        _set_action_type(self.save_detail_button, "add")
        self.component_combo.setCurrentText("")
        self.objective_combo.setCurrentText("")
        self.content_combo.setCurrentText("")
        self.update_locked_fields()
        self.load_objective_suggestions()
        self.persist_item()

    def schedule_detail_autosave(self, *_args):
        if self.loading_form:
            return
        if (
            self.activity_combo.currentText().strip()
            and self.competence_combo.currentText().strip()
            and self.component_combo.currentText().strip()
            and self.content_combo.currentText().strip()
        ):
            self.detail_autosave_timer.start()

    def autosave_detail(self):
        if (
            self.activity_combo.currentText().strip()
            and self.competence_combo.currentText().strip()
            and self.component_combo.currentText().strip()
            and self.content_combo.currentText().strip()
        ):
            self.save_detail()

    def edit_detail(self):
        row = self.details_table.currentRow()
        if row < 0:
            return
        detail = self.details[row]
        self.loading_form = True
        self._set_combo_value(
            self.component_combo, detail[0], detail[1]
        )
        self._set_combo_value(self.objective_combo, detail[2], detail[3])
        self._set_combo_value(self.content_combo, detail[4], detail[5])
        self.editing_detail_index = row
        self.loading_form = False
        self.save_detail_button.setText("تعديل السطر المحدد")
        _set_action_type(self.save_detail_button, "edit")

    @staticmethod
    def _set_combo_value(combo, record_id, text):
        if record_id is not None:
            index = combo.findData(record_id)
            if index >= 0:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentText(text)

    def update_locked_fields(self):
        locked = bool(self.details)
        self.activity_combo.setEnabled(not locked)
        self.competence_combo.setEnabled(not locked)

    def remove_detail(self):
        row = self.details_table.currentRow()
        if row >= 0:
            self.detail_autosave_timer.stop()
            removed_detail = self.details.pop(row)
            self.details_table.removeRow(row)
            if self.editing_detail_index == row:
                self.editing_detail_index = None
                self.save_detail_button.setText("إضافة المكوّن والمحتوى")
                _set_action_type(self.save_detail_button, "add")
            elif (
                self.editing_detail_index is not None
                and self.editing_detail_index > row
            ):
                self.editing_detail_index -= 1
            self.update_locked_fields()
            self.load_objective_suggestions()
            if self.details:
                self.persist_item()
            elif self.item_id is not None:
                try:
                    self.db.delete_teaching_plan_item(self.item_id)
                except sqlite3.IntegrityError:
                    QMessageBox.warning(
                        self, "تعذر حذف النشاط",
                        "هذا النشاط مستخدم في حصة مخططة؛ احذف ارتباطه بالحصة أولاً.",
                    )
                    self.details.insert(row, removed_detail)
                    self.refresh_details_table()
                    return
                self.item_id = None
                self.load_items()

    def load_items(self):
        rows = self.db.teaching_plan_items(self.plan[0])
        self._refresh_activity_suggestions()
        self.refresh_plan_number_controls()
        self.populate_mode_activity_combo()
        self.refresh_integration_table()
        self.refresh_evaluation_table()
        self.refresh_support_table()
        if (
            self.mode_combo.currentText() == "إدماج"
            and not self.editing_integration_activity
        ):
            self.load_integration_contents()
        if (
            self.mode_combo.currentText() == "تقييم"
            and not self.editing_evaluation_activity
        ):
            self.load_evaluation_contents()
        self.items_table.setRowCount(len(rows))
        for row_index, (item, details) in enumerate(rows):
            values = (
                item[1], item[3], len(details),
                "، ".join(detail[4] for detail in details),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.UserRole, item[0])
                self.items_table.setItem(row_index, column, cell)
        self.items_table.clearSelection()
        self.populate_mode_objectives_tree()

    def load_selected_item(self):
        row = self.items_table.currentRow()
        if row < 0:
            return
        item_id = self.items_table.item(row, 0).data(Qt.UserRole)
        record = self.db.teaching_plan_item(item_id)
        if record is None:
            return
        item, details = record
        self.item_id = item_id
        self.loading_form = True
        self._refresh_activity_suggestions(item[2])
        self.activity_combo.setCurrentText(item[2])
        self.load_competence_suggestions(item[2])
        self.competence_combo.setCurrentText(item[4])
        self.load_component_suggestions()
        self.details = [
            (
                detail[1], detail[2], detail[3], detail[4],
                detail[5], detail[6],
            )
            for detail in details
        ]
        self.refresh_details_table()
        self.load_objective_suggestions()
        self.update_locked_fields()
        self.loading_form = False

    def refresh_details_table(self):
        self.details_table.setRowCount(len(self.details))
        for row, detail in enumerate(self.details):
            for column, text in enumerate((detail[1], detail[3], detail[5])):
                self.details_table.setItem(
                    row, column, QTableWidgetItem(text)
                )

    def persist_item(self):
        activity_name = self.activity_combo.currentText().strip()
        try:
            self.item_id = self.db.save_teaching_plan_item(
                self.plan[0],
                activity_name,
                self._selected_record_id(self.competence_combo),
                self.competence_combo.currentText(),
                self.details,
                self.item_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر الحفظ التلقائي للنشاط", str(exc))
            return False
        self.load_items()
        if self.db.teaching_plan_number(self.plan[0]) is None:
            try:
                number = self.db.next_teaching_plan_number(
                    self.plan[1], self.plan[2], self.plan[0]
                )
                self.plan_number_spin.setValue(number)
                self.db.save_teaching_plan_number(
                    self.plan[0], number
                )
            except (ValueError, sqlite3.Error) as exc:
                QMessageBox.warning(
                    self, "تعذر تعيين عدد المخطط تلقائياً", str(exc)
                )
                return False
        return True

    def refresh_plan_number_controls(self):
        self.loading_plan_number = True
        try:
            number = self.db.teaching_plan_number(self.plan[0])
            if number is None:
                number = self.db.next_teaching_plan_number(
                    self.plan[1], self.plan[2], self.plan[0]
                )
                self.db.save_teaching_plan_number(self.plan[0], number)
            self.plan_number_spin.setValue(number)
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self, "تعذر تحديد عدد المخطط تلقائياً", str(exc)
            )
        finally:
            self.loading_plan_number = False

    def autosave_plan_number(self, *_args):
        if self.loading_plan_number:
            return
        try:
            self.db.save_teaching_plan_number(
                self.plan[0], self.plan_number_spin.value()
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر الحفظ التلقائي للعدد", str(exc))
            return

    def done(self, result):
        self.detail_autosave_timer.stop()
        self.mode_settings_autosave_timer.stop()
        self.integration_autosave_timer.stop()
        self.evaluation_autosave_timer.stop()
        self.support_autosave_timer.stop()
        self.autosave_detail()
        self.autosave_integration_draft()
        self.autosave_evaluation_draft()
        self.autosave_support_activity()
        if self.mode_combo.currentText() not in ("إدماج", "تقييم"):
            self.autosave_mode_settings()
        super().done(result)

    def clear_form(self):
        self.detail_autosave_timer.stop()
        self.item_id = None
        self.loading_form = True
        self._refresh_activity_suggestions("")
        self.activity_combo.setCurrentText("")
        self.competence_combo.setCurrentText("")
        self.component_combo.setCurrentText("")
        self.objective_combo.setCurrentText("")
        self.content_combo.setCurrentText("")
        self.details.clear()
        self.editing_detail_index = None
        self.refresh_details_table()
        self.update_locked_fields()
        self.loading_form = False
        self.save_detail_button.setText("إضافة المكوّن والمحتوى")
        _set_action_type(self.save_detail_button, "add")
        self.load_competence_suggestions("")

    def _refresh_activity_suggestions(self, selected_text=None):
        if selected_text is None:
            selected_text = self.activity_combo.currentText()
        selected_item_id = self.item_id
        existing_names = {
            str(row[0][1] or "").strip().casefold()
            for row in self.db.teaching_plan_items(self.plan[0])
            if row[0][0] != selected_item_id
        }
        self.activity_combo.blockSignals(True)
        self.activity_combo.clear()
        self.activity_combo.addItems([
            name for name in self.activity_names
            if name.strip().casefold() not in existing_names
        ])
        self.activity_combo.setCurrentText(selected_text)
        self.activity_combo.blockSignals(False)

    def delete_selected_item(self):
        row = self.items_table.currentRow()
        if row < 0:
            return
        item_id = self.items_table.item(row, 0).data(Qt.UserRole)
        try:
            self.db.delete_teaching_plan_item(item_id)
        except sqlite3.IntegrityError:
            QMessageBox.warning(
                self, "تعذر حذف النشاط",
                "هذا النشاط مستخدم في حصة مخططة؛ احذف ارتباطه بالحصة أولاً.",
            )
            return
        if self.item_id == item_id:
            self.clear_form()
        self.load_items()


class PlanActivityDialog(QDialog):
    FIELDS = (
        ("content", "المحتوى"),
        ("lesson_goal", "هدف الحصة"),
        ("specific_objective", "الأهداف المميزة"),
        ("target_competencies", "الكفايات المستهدفة"),
        ("evaluation", "التقييم"),
        ("expected_performance", "الأداء المنتظر"),
        ("target_criteria", "المعايير المستهدفة"),
        ("demographic_concept", "المفهوم السكاني"),
        ("health_concept", "المفهوم الصحي"),
        ("life_skills", "المهارات الحياتية"),
    )
    FIELD_INDEX = {
        key: index + 6 for index, (key, _label) in enumerate(FIELDS)
    }
    MODES = ("منهجي", "إدماج", "تقييم", "دعم وعلاج")

    def __init__(self, db, plan, parent=None, activity=None):
        super().__init__(parent)
        self.db = db
        self.plan = plan
        self.activity = activity
        self.mode_configuration = db.teaching_plan_mode_settings(plan[0])
        self.setWindowTitle("حصة مخططة")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(850, 760)

        layout = QVBoxLayout(self)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(self.MODES)
        self.mode_combo.currentTextChanged.connect(self.update_mode_form)
        form.addRow("نمط التعلم:", self.mode_combo)
        self.lesson_number = QSpinBox()
        self.lesson_number.setRange(1, max(1, plan[6]))
        form.addRow("رقم الحصة:", self.lesson_number)
        self.competence_list = QListWidget()
        self.competence_items = {}
        self.plan_items = db.teaching_plan_items(plan[0])
        self.plan_item_details = {
            plan_item[0]: details for plan_item, details in self.plan_items
        }
        for plan_item, _details in self.plan_items:
            item = QListWidgetItem(
                f"{plan_item[1]} — {plan_item[3]}"
            )
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, plan_item[0])
            self.competence_list.addItem(item)
            self.competence_items[plan_item[0]] = item
        self.competence_list.setMaximumHeight(125)
        form.addRow("النشاط / كفاية المادة:", self.competence_list)
        self.objective_tree = QTreeWidget()
        self.objective_tree.setHeaderHidden(True)
        self.objective_tree.setMaximumHeight(230)
        self.objective_tree.itemChanged.connect(self.update_objective_activity_state)
        self.objective_tree_mode = None
        form.addRow("الأنشطة والكفايات المستهدفة:", self.objective_tree)
        body_layout.addLayout(form)
        activity_selection = QHBoxLayout()
        select_all = QPushButton("تحديد كل الأنشطة")
        _set_action_type(select_all, "add")
        select_all.clicked.connect(self.select_all_competences)
        clear_selection = QPushButton("إلغاء التحديد")
        _set_action_type(clear_selection, "clear")
        clear_selection.clicked.connect(self.clear_competences)
        self.select_all_button = select_all
        self.clear_selection_button = clear_selection
        activity_selection.addWidget(select_all)
        activity_selection.addWidget(clear_selection)
        activity_selection.addStretch()
        body_layout.addLayout(activity_selection)

        self.text_fields = {}
        self.text_field_labels = {}
        for key, label in self.FIELDS:
            editor = QPlainTextEdit()
            editor.setMaximumHeight(58)
            if key == "expected_performance":
                editor.setFixedHeight(editor.fontMetrics().lineSpacing() * 4 + 20)
                editor.setLayoutDirection(Qt.RightToLeft)
            self.text_fields[key] = editor
            field_label = QLabel(label + ":")
            self.text_field_labels[key] = field_label
            body_layout.addWidget(field_label)
            body_layout.addWidget(editor)
        self.objective_tree_label = form.labelForField(self.objective_tree)
        self.competence_list_label = form.labelForField(self.competence_list)

        self.properties_group = QGroupBox(
            "خاصيات إضافية يحددها المستخدم"
        )
        properties_group = self.properties_group
        properties_layout = QVBoxLayout(properties_group)
        self.properties_table = QTableWidget(0, 2)
        self.properties_table.setHorizontalHeaderLabels(["اسم الخاصية", "القيمة"])
        self.properties_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.properties_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        properties_layout.addWidget(self.properties_table)
        property_buttons = QHBoxLayout()
        add_property = QPushButton("إضافة خاصية")
        _set_action_type(add_property, "add")
        add_property.clicked.connect(self.add_property_row)
        remove_property = QPushButton("حذف الخاصية المحددة")
        _set_action_type(remove_property, "delete")
        remove_property.clicked.connect(self.remove_property_row)
        property_buttons.addWidget(add_property)
        property_buttons.addWidget(remove_property)
        property_buttons.addStretch()
        properties_layout.addLayout(property_buttons)
        body_layout.addWidget(properties_group)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        save_button = QPushButton("حفظ")
        _set_action_type(save_button, "save")
        _style_primary_button(save_button)
        save_button.clicked.connect(self.save)
        cancel_button = QPushButton("إلغاء")
        _set_action_type(cancel_button, "cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(save_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

        self.update_mode_form()
        if activity:
            row, properties = activity
            self.lesson_number.setValue(row[1])
            self.mode_combo.setCurrentText(row[2])
            item = self.competence_items.get(row[3])
            if item:
                item.setCheckState(Qt.Checked)
            for competence_id, other_item in self.competence_items.items():
                if competence_id != row[3]:
                    other_item.setFlags(
                        other_item.flags() & ~Qt.ItemIsEnabled
                    )
            self.select_all_button.setEnabled(False)
            self.clear_selection_button.setEnabled(False)
            if row[2] in ("إدماج", "تقييم"):
                self.restore_objective_selection(row[3], row[9])
            for index, (key, _label) in enumerate(self.FIELDS, start=6):
                self.text_fields[key].setPlainText(row[index] or "")
            for _property_id, key, value in properties:
                self.add_property_row(key, value)
        else:
            self.mode_combo.setCurrentText(
                self.mode_configuration["default_mode"]
            )
            mode_settings = self.mode_configuration["modes"].get(
                self.mode_combo.currentText(), {}
            )
            self.text_fields["expected_performance"].setPlainText(
                mode_settings.get("expected_performance", "")
            )
            self.apply_mode_activity_defaults()
            if not self.competence_items:
                QMessageBox.information(
                    self, "لم يتم إعداد أنشطة المخطط",
                    "أعدّ أنشطة المخطط وكفاياتها ومكوناتها ومحتوياتها أولاً.",
                )

    def update_mode_form(self, *_args):
        mode = self.mode_combo.currentText()
        uses_objective_tree = mode in ("إدماج", "تقييم")
        self.competence_list.setVisible(not uses_objective_tree)
        self.objective_tree.setVisible(uses_objective_tree)
        self.objective_tree_label.setVisible(uses_objective_tree)
        self.competence_list_label.setText(
            "الأنشطة:" if mode == "دعم وعلاج"
            else "النشاط / كفاية المادة:"
        )
        if mode == "إدماج":
            self.objective_tree_label.setText("الأنشطة / الكفايات المستهدفة:")
        elif mode == "تقييم":
            self.objective_tree_label.setText("الأنشطة / الكفايات:")
        self.select_all_button.setVisible(True)
        self.clear_selection_button.setVisible(True)
        self.select_all_button.setText(
            "تحديد كل الأهداف" if uses_objective_tree else "تحديد كل الأنشطة"
        )
        if uses_objective_tree:
            self.populate_objective_tree()
        else:
            self.apply_mode_activity_defaults()

        visible_fields = {
            "منهجي": set(self.text_fields),
            "إدماج": set(),
            "تقييم": {"expected_performance"},
            "دعم وعلاج": set(),
        }.get(mode, set())
        for key, editor in self.text_fields.items():
            self.text_field_labels[key].setVisible(key in visible_fields)
            editor.setVisible(key in visible_fields)
        self.properties_group.setVisible(mode == "منهجي")

    def apply_mode_activity_defaults(self):
        if self.activity:
            return
        mode_settings = self.mode_configuration["modes"].get(
            self.mode_combo.currentText(), {}
        )
        selected_items = mode_settings.get("selected_items", set())
        for plan_item_id, item in self.competence_items.items():
            item.setCheckState(
                Qt.Checked if plan_item_id in selected_items else Qt.Unchecked
            )
    def populate_objective_tree(self):
        mode = self.mode_combo.currentText()
        selected_by_activity = {}
        for index in range(self.objective_tree.topLevelItemCount()):
            activity_node = self.objective_tree.topLevelItem(index)
            selected_by_activity[activity_node.data(0, Qt.UserRole)] = {
                activity_node.child(child_index).text(0)
                for child_index in range(activity_node.childCount())
                if activity_node.child(child_index).checkState(0)
                == Qt.Checked
            }
        self.objective_tree.blockSignals(True)
        self.objective_tree.clear()
        all_objectives = []
        for plan_item, details in self.plan_items:
            for detail in details:
                objective = str(detail[3] or "").strip()
                if objective and objective not in all_objectives:
                    all_objectives.append(objective)
        for plan_item, details in self.plan_items:
            objectives = []
            for detail in details:
                objective = str(detail[3] or "").strip()
                if objective and objective not in objectives:
                    objectives.append(objective)
            if not objectives:
                objectives = list(all_objectives)
            activity_node = QTreeWidgetItem([
                f"{plan_item[1]} — {plan_item[3]}"
            ])
            activity_node.setData(0, Qt.UserRole, plan_item[0])
            activity_node.setFlags(
                activity_node.flags()
                | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
            )
            if (
                self.activity
                and plan_item[0] != self.activity[0][3]
            ):
                activity_node.setFlags(
                    activity_node.flags() & ~Qt.ItemIsEnabled
                )
            activity_node.setCheckState(0, Qt.Unchecked)
            for objective in objectives:
                objective_node = QTreeWidgetItem([objective])
                objective_node.setData(0, Qt.UserRole, objective)
                objective_node.setFlags(
                    objective_node.flags()
                    | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                )
                if (
                    self.activity
                    and plan_item[0] != self.activity[0][3]
                ):
                    objective_node.setFlags(
                        objective_node.flags() & ~Qt.ItemIsEnabled
                    )
                objective_node.setCheckState(0, Qt.Unchecked)
                activity_node.addChild(objective_node)
            self.objective_tree.addTopLevelItem(activity_node)
        self.objective_tree.blockSignals(False)
        if self.objective_tree_mode == mode and selected_by_activity:
            for index in range(self.objective_tree.topLevelItemCount()):
                activity_node = self.objective_tree.topLevelItem(index)
                selected = selected_by_activity.get(
                    activity_node.data(0, Qt.UserRole), set()
                )
                for child_index in range(activity_node.childCount()):
                    objective_node = activity_node.child(child_index)
                    if objective_node.text(0) in selected:
                        objective_node.setCheckState(0, Qt.Checked)
        elif self.activity:
            row = self.activity[0]
            self.restore_objective_selection(row[3], row[9])
        else:
            selected_by_activity = (
                self.mode_configuration["modes"].get(
                    self.mode_combo.currentText(), {}
                ).get("selected_objectives", {})
            )
            for index in range(self.objective_tree.topLevelItemCount()):
                activity_node = self.objective_tree.topLevelItem(index)
                selected = selected_by_activity.get(
                    activity_node.data(0, Qt.UserRole), set()
                )
                for child_index in range(activity_node.childCount()):
                    objective_node = activity_node.child(child_index)
                    if objective_node.text(0) in selected:
                        objective_node.setCheckState(0, Qt.Checked)
        self.objective_tree_mode = mode

    def update_objective_activity_state(self, item, column):
        if item.parent() is not None:
            parent = item.parent()
            selected_count = sum(
                parent.child(index).checkState(0) == Qt.Checked
                for index in range(parent.childCount())
            )
            if selected_count == 0:
                state = Qt.Unchecked
            elif selected_count == parent.childCount():
                state = Qt.Checked
            else:
                state = Qt.PartiallyChecked
            parent.setCheckState(
                0, state
            )
        elif item.checkState(0) != Qt.PartiallyChecked:
            state = item.checkState(0)
            for index in range(item.childCount()):
                item.child(index).setCheckState(0, state)

    def restore_objective_selection(self, plan_item_id, target_competencies):
        selected = {
            text.strip()
            for text in str(target_competencies or "").splitlines()
            if text.strip()
        }
        for index in range(self.objective_tree.topLevelItemCount()):
            activity_node = self.objective_tree.topLevelItem(index)
            matches_activity = activity_node.data(0, Qt.UserRole) == plan_item_id
            for child_index in range(activity_node.childCount()):
                objective_node = activity_node.child(child_index)
                checked = matches_activity and (
                    objective_node.text(0) in selected
                )
                objective_node.setCheckState(
                    0, Qt.Checked if checked else Qt.Unchecked
                )
            if matches_activity and activity_node.childCount() == 0:
                activity_node.setCheckState(0, Qt.Checked)

    @staticmethod
    def _set_combo_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def select_all_competences(self):
        if self.mode_combo.currentText() in ("إدماج", "تقييم"):
            for index in range(self.objective_tree.topLevelItemCount()):
                self.objective_tree.topLevelItem(index).setCheckState(
                    0, Qt.Checked
                )
        else:
            for item in self.competence_items.values():
                item.setCheckState(Qt.Checked)

    def clear_competences(self):
        for index in range(self.objective_tree.topLevelItemCount()):
            self.objective_tree.topLevelItem(index).setCheckState(
                0, Qt.Unchecked
            )
        for item in self.competence_items.values():
            item.setCheckState(Qt.Unchecked)

    def add_property_row(self, key="", value=""):
        row = self.properties_table.rowCount()
        self.properties_table.insertRow(row)
        self.properties_table.setItem(row, 0, QTableWidgetItem(key))
        value_item = QTableWidgetItem(value)
        value_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.properties_table.setItem(row, 1, value_item)

    def remove_property_row(self):
        row = self.properties_table.currentRow()
        if row >= 0:
            self.properties_table.removeRow(row)

    def save(self):
        mode = self.mode_combo.currentText()
        if mode in ("إدماج", "تقييم"):
            selected_activities = []
            for index in range(self.objective_tree.topLevelItemCount()):
                activity_node = self.objective_tree.topLevelItem(index)
                selected_objectives = [
                    activity_node.child(child_index).text(0)
                    for child_index in range(activity_node.childCount())
                    if activity_node.child(child_index).checkState(0)
                    == Qt.Checked
                ]
                if selected_objectives:
                    selected_activities.append((
                        int(activity_node.data(0, Qt.UserRole)),
                        selected_objectives,
                    ))
            activity_values = []
            for plan_item_id, objectives in selected_activities:
                values = dict.fromkeys(self.text_fields, "")
                if mode == "تقييم":
                    values["expected_performance"] = (
                        self.text_fields["expected_performance"].toPlainText()
                    )
                values["target_competencies"] = "\n".join(objectives)
                activity_values.append((plan_item_id, values))
        elif mode == "دعم وعلاج":
            competence_ids = [
                int(self.competence_list.item(row).data(Qt.UserRole))
                for row in range(self.competence_list.count())
                if self.competence_list.item(row).checkState() == Qt.Checked
            ]
            activity_values = [
                (plan_item_id, dict.fromkeys(self.text_fields, ""))
                for plan_item_id in competence_ids
            ]
        else:
            competence_ids = [
                int(self.competence_list.item(row).data(Qt.UserRole))
                for row in range(self.competence_list.count())
                if self.competence_list.item(row).checkState() == Qt.Checked
            ]
            activity_values = [
                (plan_item_id, {
                    key: editor.toPlainText()
                    for key, editor in self.text_fields.items()
                })
                for plan_item_id in competence_ids
            ]
        if not activity_values:
            QMessageBox.warning(self, "تعذر الحفظ", "لا توجد كفاية مادة مرتبطة بالمخطط.")
            return
        if mode == "تقييم" and any(
            not values["expected_performance"].strip()
            for _plan_item_id, values in activity_values
        ):
            QMessageBox.warning(
                self, "بيانات ناقصة", "أدخل الأداء المنتظر للتقييم."
            )
            return
        if mode == "إدماج" and any(
            not values["target_competencies"].strip()
            for _plan_item_id, values in activity_values
        ):
            QMessageBox.warning(
                self, "بيانات ناقصة",
                "اختر هدفاً مميزاً واحداً على الأقل لكل نشاط محدد.",
            )
            return
        properties = []
        for row in range(self.properties_table.rowCount()):
            key_item = self.properties_table.item(row, 0)
            value_item = self.properties_table.item(row, 1)
            properties.append((
                key_item.text() if key_item else "",
                value_item.text() if value_item else "",
            ))
        try:
            for plan_item_id, values in activity_values:
                self.db.save_teaching_plan_activity(
                    self.plan[0],
                    self.lesson_number.value(),
                    mode,
                    plan_item_id,
                    values,
                    properties,
                    self.activity[0][0] if self.activity else None,
                    self.activity[0][16] if self.activity else self.plan[9],
                )
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "تعذر حفظ الحصة", str(exc))
            return
        self.accept()


class OfficialHolidayCalendar(QCalendarWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._blocked_dates = set()
        self._last_allowed_date = self.selectedDate()
        self.selectionChanged.connect(self.restore_allowed_selection)
        self.currentPageChanged.connect(self.refresh_holiday_formats)
        self.refresh_holiday_formats()

    def set_blocked_dates(self, blocked_dates):
        self._blocked_dates = set(blocked_dates)
        selected = self.selectedDate()
        if selected in self._blocked_dates:
            available = next(
                (
                    selected.addDays(offset)
                    for offset in range(1, 367)
                    if selected.addDays(offset) not in self._blocked_dates
                ),
                QDate.currentDate(),
            )
            self.blockSignals(True)
            self.setSelectedDate(available)
            self.blockSignals(False)
            self._last_allowed_date = available
        else:
            self._last_allowed_date = selected
        self.refresh_holiday_formats()

    def restore_allowed_selection(self):
        selected = self.selectedDate()
        if selected in self._blocked_dates:
            self.blockSignals(True)
            self.setSelectedDate(self._last_allowed_date)
            self.blockSignals(False)
            return
        self._last_allowed_date = selected

    def refresh_holiday_formats(self, *_args):
        self.setDateTextFormat(QDate(), QTextCharFormat())
        month_start = QDate(self.yearShown(), self.monthShown(), 1)
        month_end = month_start.addMonths(1).addDays(-1)
        holiday_format = QTextCharFormat()
        holiday_format.setBackground(QColor("#FDE68A"))
        holiday_format.setForeground(QColor("#92400E"))
        blocked_format = QTextCharFormat()
        blocked_format.setBackground(QColor("#E2E8F0"))
        blocked_format.setForeground(QColor("#94A3B8"))
        for _holiday_id, start_text, end_text, name in self.db.official_holidays():
            start = max(
                QDate.fromString(start_text, "yyyy-MM-dd"), month_start
            )
            end = min(
                QDate.fromString(end_text, "yyyy-MM-dd"), month_end
            )
            if start > end:
                continue
            holiday_format.setToolTip(name or "عطلة رسمية")
            date_value = start
            while date_value <= end:
                self.setDateTextFormat(
                    date_value,
                    blocked_format
                    if date_value in self._blocked_dates
                    else holiday_format,
                )
                date_value = date_value.addDays(1)


class OfficialHolidaysPage(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setLayoutDirection(Qt.RightToLeft)
        self.editing_holiday_id = None
        layout = QVBoxLayout(self)
        title = QLabel("إدارة العطل الرسمية")
        title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #1E40AF;"
        )
        layout.addWidget(title)

        body = QHBoxLayout()
        self.calendar = OfficialHolidayCalendar(self.db)
        self.calendar.setMinimumWidth(360)
        self.calendar.setMinimumHeight(300)
        self.calendar.clicked.connect(self.select_agenda_date)
        body.addWidget(self.calendar, 1)

        management_layout = QVBoxLayout()
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([
            "المعرّف", "من تاريخ", "إلى تاريخ", "اسم العطلة",
        ])
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        management_layout.addWidget(self.table, 1)

        form = QFormLayout()
        self.start_date_edit = QDateEdit(QDate.currentDate())
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.start_date_edit.setReadOnly(True)
        self.start_calendar = OfficialHolidayCalendar(
            self.db, self.start_date_edit
        )
        self.start_date_edit.setCalendarWidget(self.start_calendar)
        self.end_date_edit = QDateEdit(QDate.currentDate())
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_date_edit.setReadOnly(True)
        self.end_calendar = OfficialHolidayCalendar(
            self.db, self.end_date_edit
        )
        self.end_date_edit.setCalendarWidget(self.end_calendar)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("اسم العطلة")
        form.addRow("من:", self.start_date_edit)
        form.addRow("إلى:", self.end_date_edit)
        form.addRow("اسم العطلة:", self.name_edit)
        management_layout.addLayout(form)
        buttons = QHBoxLayout()
        self.add_button = None
        for text, callback, action_type in (
            ("إضافة", self.add_holiday, "add"),
            ("تعديل المحدد", self.edit_holiday, "edit"),
            ("حذف المحدد", self.delete_holiday, "delete"),
        ):
            button = QPushButton(text)
            _set_action_type(button, action_type)
            if text == "إضافة":
                _style_primary_button(button)
            button.clicked.connect(callback)
            buttons.addWidget(button)
            if text == "إضافة":
                self.add_button = button
            elif text == "تعديل المحدد":
                self.edit_button = button
            elif text == "حذف المحدد":
                self.delete_button = button
        management_layout.addLayout(buttons)
        body.addLayout(management_layout, 2)
        layout.addLayout(body, 1)
        self.table.itemSelectionChanged.connect(self.fill_form)
        self.start_date_edit.dateChanged.connect(self.update_actions)
        self.end_date_edit.dateChanged.connect(self.update_actions)
        self.load()

    def blocked_dates(self):
        excluded_id = self.editing_holiday_id
        dates = set()
        for holiday_id, start_text, end_text, _name in (
            self.db.official_holidays()
        ):
            if holiday_id == excluded_id:
                continue
            start = QDate.fromString(start_text, "yyyy-MM-dd")
            end = QDate.fromString(end_text, "yyyy-MM-dd")
            date_value = start
            while date_value.isValid() and date_value <= end:
                dates.add(date_value)
                date_value = date_value.addDays(1)
        return dates

    def refresh_date_calendars(self):
        blocked_dates = self.blocked_dates()
        self.start_calendar.set_blocked_dates(blocked_dates)
        self.end_calendar.set_blocked_dates(blocked_dates)

    def select_agenda_date(self, date_value):
        for row in range(self.table.rowCount()):
            start = QDate.fromString(
                self.table.item(row, 1).text(), "yyyy-MM-dd"
            )
            end = QDate.fromString(
                self.table.item(row, 2).text(), "yyyy-MM-dd"
            )
            if start <= date_value <= end:
                self.table.setCurrentCell(row, 1)
                return
        self.editing_holiday_id = None
        self.table.clearSelection()
        self.start_date_edit.setDate(date_value)
        self.end_date_edit.setDate(date_value)
        self.name_edit.clear()
        self.refresh_date_calendars()

    def load(self):
        rows = self.db.official_holidays()
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.calendar.refresh_holiday_formats()
        self.refresh_date_calendars()
        self.update_actions()

    def fill_form(self):
        row = self.table.currentRow()
        if row < 0:
            self.editing_holiday_id = None
            self.refresh_date_calendars()
            self.update_actions()
            return
        self.editing_holiday_id = int(self.table.item(row, 0).text())
        date_item = self.table.item(row, 1)
        end_date_item = self.table.item(row, 2)
        name_item = self.table.item(row, 3)
        if date_item:
            self.start_date_edit.setDate(
                QDate.fromString(date_item.text(), "yyyy-MM-dd")
            )
        if end_date_item:
            self.end_date_edit.setDate(
                QDate.fromString(end_date_item.text(), "yyyy-MM-dd")
            )
        self.name_edit.setText(name_item.text() if name_item else "")
        self.refresh_date_calendars()
        self.update_actions()

    def update_actions(self):
        has_selection = self.table.currentRow() >= 0
        start = self.start_date_edit.date()
        end = self.end_date_edit.date()
        add_available = start <= end and not self.holiday_range_overlaps(
            start, end
        )
        self.add_button.setEnabled(add_available)
        self.edit_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

    def holiday_range_overlaps(self, start, end, exclude_id=None):
        start_text = start.toString("yyyy-MM-dd")
        end_text = end.toString("yyyy-MM-dd")
        return any(
            holiday_id != exclude_id
            and existing_start <= end_text
            and existing_end >= start_text
            for holiday_id, existing_start, existing_end, _name
            in self.db.official_holidays()
        )

    def add_holiday(self):
        self.editing_holiday_id = None
        self._save_holiday()

    def edit_holiday(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "اختيار مطلوب", "حدد عطلة لتعديلها.")
            return
        self._save_holiday(int(self.table.item(row, 0).text()))

    def _save_holiday(self, holiday_id=None):
        try:
            self.db.save_official_holiday(
                self.start_date_edit.date().toString("yyyy-MM-dd"),
                self.end_date_edit.date().toString("yyyy-MM-dd"),
                self.name_edit.text(),
                holiday_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر حفظ العطلة", str(exc))
            return
        self.load()
        self.table.clearSelection()
        self.name_edit.clear()
        self.editing_holiday_id = None
        self.refresh_date_calendars()

    def delete_holiday(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "اختيار مطلوب", "حدد عطلة لحذفها.")
            return
        try:
            self.db.delete_official_holiday(
                int(self.table.item(row, 0).text())
            )
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "تعذر حذف العطلة", str(exc))
            return
        self.load()


class PlanActivityCard(QFrame):
    def __init__(self, activity, color, duration_minutes, parent=None):
        super().__init__(parent)
        self.activity = activity
        self.drag_start_position = None
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedSize(100, 100)
        self.setCursor(Qt.OpenHandCursor)
        tooltip_lines = [f"النشاط: {activity['activity_name']}"]
        if activity.get("learning_mode") == "إدماج":
            tooltip_lines.append(
                "الكفايات المستهدفة: "
                f"{activity['target_competencies']}"
            )
        elif activity.get("learning_mode") != "تقييم":
            tooltip_lines.append(f"المحتوى: {activity['content']}")
        tooltip_lines.extend((
            f"التوقيت: {duration_minutes} دقيقة",
            "اسحب البطاقة إلى الحصة المطلوبة لنسخها.",
        ))
        self.setToolTip("\n".join(tooltip_lines))
        self.setStyleSheet(
            "QFrame {"
            f"background-color: {color};"
            "border: 1px solid #94A3B8; border-radius: 10px;"
            "}"
        )
        card_layout = QVBoxLayout(self)
        card_layout.setContentsMargins(4, 4, 4, 4)
        card_layout.setSpacing(2)
        name = QLabel(activity["activity_name"])
        name.setWordWrap(True)
        name.setAlignment(Qt.AlignCenter)
        name.setStyleSheet(
            "font-weight: bold; font-size: 8pt; color: #0F172A; border: none; "
            "background: transparent;"
        )
        card_layout.addWidget(name)
        if activity.get("learning_mode") not in ("تقييم", "دعم وعلاج"):
            detail_text = (
                activity["target_competencies"]
                if activity.get("learning_mode") == "إدماج"
                else activity["content"]
            )
            detail = QLabel(detail_text)
            detail.setWordWrap(True)
            detail.setAlignment(Qt.AlignCenter)
            detail.setStyleSheet(
                "font-size: 7pt; color: #334155; border: none; "
                "background: transparent;"
            )
            card_layout.addWidget(detail, 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_start_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self.drag_start_position is None
            or not event.buttons() & Qt.LeftButton
            or (event.position().toPoint() - self.drag_start_position).manhattanLength()
            < QApplication.startDragDistance()
        ):
            return
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setText(json.dumps(self.activity, ensure_ascii=False))
        drag.setMimeData(mime_data)
        drag.exec(Qt.CopyAction)


class PlanScheduledActivityButton(QPushButton):
    def __init__(self, activity_id, learning_mode, text, parent=None):
        super().__init__(text, parent)
        self.activity_id = activity_id
        self.learning_mode = learning_mode
        self.drag_start_position = None
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("اسحب النشاط إلى حصة لنسخها إليها.")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_start_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self.drag_start_position is None
            or not event.buttons() & Qt.LeftButton
            or (
                event.position().toPoint() - self.drag_start_position
            ).manhattanLength() < QApplication.startDragDistance()
        ):
            return
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setText(json.dumps({
            "activity_id": self.activity_id,
            "learning_mode": self.learning_mode,
        }))
        drag.setMimeData(mime_data)
        drag.exec(Qt.CopyAction)


class PlanLessonDropRow(QFrame):
    def __init__(
        self, mode, lesson_number, on_drop, parent=None,
        display_number=None,
    ):
        super().__init__(parent)
        self.mode = mode
        self.lesson_number = lesson_number
        self.display_number = display_number or lesson_number
        self.on_drop = on_drop
        self.setAcceptDrops(True)
        self.setMinimumHeight(124)
        self.setFrameShape(QFrame.StyledPanel)
        if mode == "منهجي":
            row_background = "#EFF6FF"
            row_border = "2px solid #2563EB"
        else:
            row_background = "#F8FAFC"
            row_border = "1px dashed #94A3B8"
        self.setStyleSheet(
            "PlanLessonDropRow {"
            f"background: {row_background}; "
            f"border: {row_border}; border-radius: 8px;"
            "}"
        )
        self.row_layout = QHBoxLayout(self)
        self.row_layout.setContentsMargins(10, 10, 10, 10)
        label = QLabel(str(self.display_number))
        label.setMinimumWidth(36)
        label.setStyleSheet(
            "font-weight: bold; color: #1E293B; border: none;"
        )
        self.row_layout.addWidget(label)
        self.activities_layout = QHBoxLayout()
        self.activities_layout.setAlignment(Qt.AlignCenter)
        self.row_layout.addLayout(self.activities_layout, 1)
        self.drop_hint = QLabel("أفلت النشاط هنا لإضافته إلى الحصة")
        self.drop_hint.setAlignment(Qt.AlignCenter)
        self.drop_hint.setStyleSheet("color: #64748B; border: none;")
        self.row_layout.addWidget(self.drop_hint)
        self.row_layout.addStretch()

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        try:
            activity = json.loads(event.mimeData().text())
        except (TypeError, ValueError):
            event.ignore()
            return
        if (
            not isinstance(activity, dict)
            or not {"item_id", "activity_id"}.intersection(activity)
        ):
            event.ignore()
            return
        self.on_drop(self, activity)
        event.acceptProposedAction()


class PlanDistributionEntryDialog(QDialog):
    MODE_FIELDS = {
        "منهجي": (
            ("content", "المحتوى"),
            ("lesson_goal", "هدف الحصة"),
            ("specific_objective", "الهدف المميز"),
        ),
        "إدماج": (("target_criteria", "المعايير المستهدفة"),),
        "تقييم": (("expected_performance", "الأداء المنتظر"),),
        "دعم وعلاج": (("target_criteria", "المعايير المستهدفة"),),
    }

    def __init__(
        self, mode, activity, lesson_number, default_duration,
        existing=None, parent=None, property_suggestions=(),
    ):
        super().__init__(parent)
        self.mode = mode
        self.activity = activity
        self.existing = existing
        self.setWindowTitle(f"إضافة نشاط إلى الحصة {mode} {lesson_number}")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(520, 600)

        layout = QVBoxLayout(self)
        title_text = activity["activity_name"]
        if mode == "إدماج":
            title_text += f" — {activity['target_competencies']}"
        elif activity["content"]:
            title_text += f" — {activity['content']}"
        title = QLabel(title_text)
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        form = QFormLayout()
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 300)
        self.duration_spin.setSuffix(" دقيقة")
        self.duration_spin.setValue(
            existing[0][16] if existing else default_duration
        )
        form.addRow("الزمن المخصص للنشاط:", self.duration_spin)
        body_layout.addLayout(form)

        self.fields = {}
        existing_row = existing[0] if existing else None
        self.property_suggestions = {
            key: values for key, values in property_suggestions
        }
        field_property_keys = {
            "content": ("المحتوى",),
            "lesson_goal": ("هدف الحصة",),
            "specific_objective": ("الهدف المميز", "الأهداف المميزة"),
            "target_competencies": ("الكفايات المستهدفة",),
            "evaluation": ("التقييم",),
            "expected_performance": ("الأداء المنتظر",),
            "target_criteria": ("المعايير المستهدفة",),
            "demographic_concept": ("المفهوم السكاني",),
            "health_concept": ("مفهوم صحي أو سكاني",),
            "life_skills": ("المهارة الحياتية", "المهارات الحياتية"),
        }
        for key, label in self.MODE_FIELDS[mode]:
            editor = QPlainTextEdit()
            editor.setLayoutDirection(Qt.RightToLeft)
            text_option = editor.document().defaultTextOption()
            text_option.setAlignment(Qt.AlignRight)
            editor.document().setDefaultTextOption(text_option)
            editor.setMinimumHeight(82)
            if key == "content":
                editor.setPlainText(
                    existing_row[6] if existing_row else activity["content"]
                )
            elif existing_row:
                value = str(
                    existing_row[PlanActivityDialog.FIELD_INDEX[key]] or ""
                )
                if (
                    not value and mode == "إدماج"
                    and key == "target_criteria"
                ):
                    value = str(activity.get("target_criteria", "") or "")
                if (
                    not value and mode == "تقييم"
                    and key == "expected_performance"
                ):
                    value = str(
                        activity.get("expected_performance", "") or ""
                    )
                if (
                    not value and mode == "دعم وعلاج"
                    and key == "target_criteria"
                ):
                    value = str(activity.get("target_criteria", "") or "")
                editor.setPlainText(value)
            else:
                editor.setPlainText(activity.get(key, ""))
                historical_values = next(
                    (
                        self.property_suggestions[property_key]
                        for property_key in field_property_keys.get(key, ())
                        if self.property_suggestions.get(property_key)
                    ),
                    [],
                )
                if historical_values:
                    editor.setPlaceholderText(
                        "قيم سابقة لهذا النشاط: "
                        + "، ".join(historical_values)
                    )
            self.fields[key] = editor
            form.addRow(f"{label}:", editor)

        self.custom_properties_group = QGroupBox("خاصيات إضافية")
        custom_layout = QVBoxLayout(self.custom_properties_group)
        self.custom_rows_layout = QVBoxLayout()
        custom_layout.addLayout(self.custom_rows_layout)
        self.custom_rows = []
        field_keys = {
            label for _key, label in self.MODE_FIELDS[mode]
        }
        self.property_key_suggestions = list(dict.fromkeys(
            key for key in (
                *DETAIL_KEYS_BY_NAMAT.get(mode, ()),
                *self.property_suggestions.keys(),
            )
            if key not in field_keys
        ))
        add_property = QPushButton("إضافة خاصية")
        _set_action_type(add_property, "add")
        add_property.clicked.connect(
            lambda _checked=False: self.add_custom_property_row()
        )
        custom_layout.addWidget(add_property)
        body_layout.addWidget(self.custom_properties_group)
        loaded_keys = set()
        for _property_id, key, value in existing[1] if existing else ():
            self.add_custom_property_row(key, value)
            loaded_keys.add(key)
        for key in self.property_key_suggestions:
            if key not in loaded_keys:
                self.add_custom_property_row(key)
                loaded_keys.add(key)
        self.add_custom_property_row()
        body_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        save_button = QPushButton("إضافة إلى الحصة")
        _set_action_type(save_button, "save")
        _style_primary_button(save_button)
        save_button.clicked.connect(self.accept)
        cancel_button = QPushButton("إلغاء")
        _set_action_type(cancel_button, "cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(save_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)
    def add_custom_property_row(self, key="", value=""):
        row = QHBoxLayout()
        key_edit = QLineEdit(key)
        key_edit.setPlaceholderText("اسم الخاصية (من دفتر إعداد الدروس)")
        key_edit.setCompleter(QCompleter(self.property_key_suggestions, key_edit))
        value_edit = QLineEdit(value)
        value_edit.setPlaceholderText("القيمة")
        value_edit.setCompleter(QCompleter(
            self.property_suggestions.get(key, []), value_edit
        ))
        for editor in (key_edit, value_edit):
            editor.setLayoutDirection(Qt.RightToLeft)
            editor.setAlignment(Qt.AlignRight)
        row.addWidget(key_edit)
        row.addWidget(value_edit)
        self.custom_rows_layout.addLayout(row)
        self.custom_rows.append((key_edit, value_edit))
        key_edit.textChanged.connect(
            lambda text, editor=value_edit: editor.setCompleter(
                QCompleter(self.property_suggestions.get(text, []), editor)
            )
        )
        key_edit.textChanged.connect(self.ensure_trailing_property_row)
        value_edit.textChanged.connect(self.ensure_trailing_property_row)

    def ensure_trailing_property_row(self, *_args):
        if not self.custom_rows:
            self.add_custom_property_row()
            return
        key_edit, value_edit = self.custom_rows[-1]
        if key_edit.text().strip() or value_edit.text().strip():
            self.add_custom_property_row()

    def values(self):
        fields = {
            key: editor.toPlainText().strip()
            for key, editor in self.fields.items()
        }
        fields.setdefault("content", self.activity["content"])
        fields.setdefault("lesson_goal", "")
        fields.setdefault("specific_objective", "")
        fields.setdefault("target_competencies", "")
        fields.setdefault("evaluation", "")
        fields.setdefault("expected_performance", "")
        fields.setdefault("target_criteria", "")
        fields.setdefault("demographic_concept", "")
        fields.setdefault("health_concept", "")
        fields.setdefault("life_skills", "")
        properties = [
            (key.text().strip(), value.text().strip())
            for key, value in self.custom_rows
            if key.text().strip()
        ]
        return fields, properties, self.duration_spin.value()


class PlanDistributionPage(QWidget):
    def __init__(self, db, plan, on_back, parent=None):
        super().__init__(parent)
        self.db = db
        self.plan = plan
        self.on_back = on_back
        self.setWindowTitle(f"توزيع أنشطة المخطط: {plan[1]}")
        self.setLayoutDirection(Qt.RightToLeft)
        self.mode_rows = {}
        self.activity_card_widgets = []
        self.activity_card_columns = None

        layout = QVBoxLayout(self)
        self.title_label = QLabel(f"توزيع أنشطة المخطط على الحصص: {plan[1]}")
        self.title_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #1E40AF;"
        )
        layout.addWidget(self.title_label)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        mode_layout = QHBoxLayout()
        mode_layout.setAlignment(Qt.AlignHCenter)
        self.mode_checks = {}
        for mode in PlanActivityDialog.MODES:
            checkbox = QCheckBox(mode)
            checkbox.setChecked(mode == "منهجي")
            self.mode_group.addButton(checkbox)
            self.mode_checks[mode] = checkbox
            mode_layout.addWidget(checkbox)
        layout.addLayout(mode_layout)

        self.activities_group = QGroupBox("أنشطة النمط المحدد")
        self.activities_group.installEventFilter(self)
        activities_layout = QVBoxLayout(self.activities_group)
        self.activities_grid = QGridLayout()
        self.activities_grid.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        activities_layout.addLayout(self.activities_grid)
        activities_scroll = QScrollArea()
        activities_scroll.setWidgetResizable(True)
        activities_scroll.setMinimumHeight(180)
        activities_scroll.setMaximumHeight(260)
        activities_scroll.setWidget(self.activities_group)
        layout.addWidget(activities_scroll)

        self.lessons_scroll = QScrollArea()
        self.lessons_scroll.setWidgetResizable(True)
        self.lesson_rows_container = QWidget()
        self.lesson_rows_layout = QGridLayout(self.lesson_rows_container)
        self.lesson_rows_layout.setHorizontalSpacing(8)
        self.lesson_rows_layout.setVerticalSpacing(8)
        for column in range(3):
            self.lesson_rows_layout.setColumnStretch(column, 1)
        self.lessons_scroll.setWidget(self.lesson_rows_container)
        layout.addWidget(self.lessons_scroll, 1)

        actions = QHBoxLayout()
        close_button = QPushButton("العودة إلى المخططات")
        _set_action_type(close_button, "back")
        close_button.clicked.connect(self.on_back)
        actions.addStretch()
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self.mode_group.buttonClicked.connect(self.load_mode)
        self.load_mode("منهجي")

    def eventFilter(self, watched, event):
        if (
            watched is self.activities_group
            and event.type() == QEvent.Resize
        ):
            QTimer.singleShot(0, self.layout_activity_cards)
        return super().eventFilter(watched, event)

    def layout_activity_cards(self):
        if not self.activity_card_widgets:
            return
        grid_width = (
            self.activities_group.contentsRect().width()
            - self.activities_grid.contentsMargins().left()
            - self.activities_grid.contentsMargins().right()
        )
        spacing = self.activities_grid.horizontalSpacing()
        if spacing < 0:
            spacing = 6
        card_width = self.activity_card_widgets[0].width()
        columns = max(1, (grid_width + spacing) // (card_width + spacing))
        if columns == self.activity_card_columns:
            return
        while self.activities_grid.count():
            self.activities_grid.takeAt(0)
        self.activity_card_columns = columns
        for index, card in enumerate(self.activity_card_widgets):
            self.activities_grid.addWidget(
                card, index // columns, index % columns
            )

    @staticmethod
    def activity_colors(item_id):
        seed = (
            int(item_id)
            if isinstance(item_id, int)
            else sum(
                (index + 1) * ord(char)
                for index, char in enumerate(str(item_id))
            )
        )
        hue = (seed * 137) % 360
        return (
            QColor.fromHsv(hue, 48, 248).name(),
            QColor.fromHsv(hue, 180, 190).name(),
        )

    def display_lesson_number(self, mode, mode_lesson_number):
        mode_index = PlanActivityDialog.MODES.index(mode)
        previous_lesson_count = sum(
            int(self.plan[10 + index] or 0)
            for index in range(mode_index)
        )
        return previous_lesson_count + mode_lesson_number

    def activity_cards_for_mode(self, mode):
        plan_items = self.db.teaching_plan_items(self.plan[0])
        items_by_name = {
            self.db._normalized_plan_activity(item[1]): (item, details)
            for item, details in plan_items
        }
        cards = []
        if mode == "منهجي":
            configured = [
                (
                    item[1],
                    [str(detail[4] or "").strip() for detail in details],
                    {},
                )
                for item, details in plan_items
            ]
        elif mode in ("إدماج", "تقييم"):
            configured = []
        else:
            configured = [
                (row[0], [], {"target_criteria": row[1]})
                for row in self.db.teaching_plan_support_activities(
                    self.plan[0]
                )
            ]

        mode_settings = self.db.teaching_plan_mode_settings(
            self.plan[0]
        )["modes"].get(mode, {})
        if mode == "إدماج":
            for record in self.db.teaching_plan_integration_activities(
                self.plan[0]
            ):
                activity_name, _prompt, _template, target_text, _contents = record
                item_data = items_by_name.get(
                    self.db._normalized_plan_activity(activity_name)
                )
                item, _details = item_data if item_data else (None, ())
                cards.append({
                    "item_id": item[0] if item else None,
                    "activity_name": activity_name,
                    "learning_mode": mode,
                    "content": "",
                    "competence_text": item[3] if item else "",
                    "target_competencies": target_text,
                    "target_criteria": target_text,
                    "expected_performance": mode_settings.get(
                        "expected_performance", ""
                    ),
                })
            return cards
        if mode == "تقييم":
            for record in self.db.teaching_plan_evaluation_activities(
                self.plan[0]
            ):
                activity_name, _prompt, _template, target_text, _contents = record
                item_data = items_by_name.get(
                    self.db._normalized_plan_activity(activity_name)
                )
                item, _details = item_data if item_data else (None, ())
                cards.append({
                    "item_id": item[0] if item else None,
                    "activity_name": activity_name,
                    "learning_mode": mode,
                    "content": "",
                    "competence_text": item[3] if item else "",
                    "target_competencies": "",
                    "expected_performance": (
                        target_text
                        or mode_settings.get("expected_performance", "")
                    ),
                })
            return cards
        if mode == "دعم وعلاج":
            for activity_name, target_criteria in (
                self.db.teaching_plan_support_activities(self.plan[0])
            ):
                item_data = items_by_name.get(
                    self.db._normalized_plan_activity(activity_name)
                )
                item, _details = item_data if item_data else (None, ())
                cards.append({
                    "item_id": item[0] if item else None,
                    "activity_name": activity_name,
                    "learning_mode": mode,
                    "content": "",
                    "competence_text": item[3] if item else "",
                    "target_criteria": target_criteria,
                })
            return cards

        for activity_name, selected_contents, default_fields in configured:
            item_data = items_by_name.get(
                self.db._normalized_plan_activity(activity_name)
            )
            if not item_data:
                continue
            item, details = item_data
            objectives_by_content = {
                str(detail[4] or "").strip().casefold():
                str(detail[3] or "").strip()
                for detail in details
            }
            detail_contents = [
                str(detail[4] or "").strip()
                for detail in details if str(detail[4] or "").strip()
            ]
            contents = [
                str(content or "").strip()
                for content in selected_contents if str(content or "").strip()
            ] or detail_contents or [""]
            for content in dict.fromkeys(contents):
                cards.append({
                    "item_id": item[0],
                    "activity_name": item[1],
                    "learning_mode": mode,
                    "content": content,
                    "competence_text": item[3],
                    **default_fields,
                    "specific_objective": objectives_by_content.get(
                        content.casefold(),
                        default_fields.get("specific_objective", ""),
                    ),
                    "expected_performance": mode_settings.get(
                        "expected_performance", ""
                    ),
                })
        return cards

    def load_mode(self, mode=None, *_args):
        if not isinstance(mode, str):
            checked_button = self.mode_group.checkedButton()
            mode = checked_button.text() if checked_button else "منهجي"
        self.clear_layout(self.activities_grid)
        self.activity_card_widgets = []
        self.activity_card_columns = None
        self.clear_layout(self.lesson_rows_layout)
        self.mode_rows.clear()
        cards = self.activity_cards_for_mode(mode)
        self.mode_checks[mode].setChecked(True)
        self.saved_activities = self.db.teaching_plan_activities(
            self.plan[0]
        )
        for activity in cards:
            color, border = self.activity_colors(
                activity["item_id"] or activity["activity_name"]
            )
            card = PlanActivityCard(
                activity, color, self.plan[9] or 60, self
            )
            card.setStyleSheet(
                f"QFrame {{background-color: {color}; "
                f"border: 2px solid {border}; border-radius: 10px;}}"
            )
            self.activity_card_widgets.append(card)
        if not cards:
            empty = QLabel("لا توجد أنشطة أو محتويات مسجلة لهذا النمط.")
            empty.setStyleSheet("color: #64748B;")
            self.activities_grid.addWidget(empty, 0, 0)
        else:
            self.layout_activity_cards()

        mode_index = PlanActivityDialog.MODES.index(mode)
        lesson_count = int(self.plan[10 + mode_index] or 0)
        if lesson_count == 0:
            self.lesson_rows_layout.addWidget(
                QLabel("لا توجد حصص محددة لهذا النمط في إعدادات المخطط."),
                0, 0, 1, 3,
            )
        for lesson_number in range(1, lesson_count + 1):
            row = PlanLessonDropRow(
                mode,
                lesson_number,
                self.drop_activity,
                self,
                display_number=self.display_lesson_number(
                    mode, lesson_number
                ),
            )
            self.mode_rows[(mode, lesson_number)] = row
            row_index, column_index = divmod(lesson_number - 1, 3)
            self.lesson_rows_layout.addWidget(
                row, row_index, column_index
            )
            self.populate_lesson_row(row)

    def load(self):
        self.title_label.setText(
            f"توزيع أنشطة المخطط على الحصص: {self.plan[1]}"
        )
        checked = self.mode_group.checkedButton()
        self.load_mode(checked.text() if checked else "منهجي")

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            nested = item.layout()
            if nested is not None:
                PlanDistributionPage.clear_layout(nested)

    def populate_lesson_row(self, row):
        for activity, _properties in self.saved_activities:
            if activity[1] != row.lesson_number or activity[2] != row.mode:
                continue
            card_data = {
                "item_id": activity[3],
                "activity_name": activity[4],
                "learning_mode": activity[2],
                "content": activity[6],
                "competence_text": activity[5],
                "specific_objective": activity[8],
                "target_competencies": activity[9],
            }
            color, border = self.activity_colors(
                activity[3] or activity[4]
            )
            button = PlanScheduledActivityButton(
                activity[0],
                activity[2],
                f"{activity[4]}\n{activity[6]}\n{activity[16]} دقيقة"
            )
            button.setFixedSize(125, 100)
            button.setToolTip(
                f"النشاط: {activity[4]}\n"
                f"المحتوى: {activity[6]}\n"
                f"التوقيت: {activity[16]} دقيقة\n"
                "اسحب النشاط إلى حصة أخرى لنسخه إليها."
            )
            button.setStyleSheet(
                f"QPushButton {{background-color: {color}; "
                f"border: 1px solid {border}; border-radius: 8px; "
                "padding: 3px; font-size: 7pt; text-align: center;}"
            )
            button.clicked.connect(
                lambda _checked=False, row=row, card=card_data, activity=activity:
                self.open_entry_dialog(row, card, activity)
            )
            entry_widget = QWidget()
            entry_widget.setFixedSize(125, 100)
            entry_layout = QGridLayout(entry_widget)
            entry_layout.setContentsMargins(0, 0, 0, 0)
            entry_layout.setSpacing(0)
            entry_layout.addWidget(button, 0, 0)
            delete_button = QToolButton()
            delete_button.setText("×")
            delete_button.setToolTip("حذف النشاط من الحصة")
            delete_button.setFixedSize(20, 20)
            _set_action_type(delete_button, "delete")
            delete_button.setStyleSheet(
                "QToolButton {font-size: 14pt; font-weight: bold; "
                "padding: 0; border-radius: 7px;}"
            )
            delete_button.clicked.connect(
                lambda _checked=False, activity_id=activity[0]:
                self.delete_activity(activity_id)
            )
            entry_layout.addWidget(
                delete_button, 0, 0, Qt.AlignLeft | Qt.AlignTop
            )
            row.activities_layout.addWidget(entry_widget)
        row.drop_hint.setVisible(row.activities_layout.count() == 0)

    def open_entry_dialog(self, row, activity_card, activity_row=None):
        default_duration = self.plan[9] or 60
        existing = None
        if activity_row is not None:
            existing = next(
                item for item in self.db.teaching_plan_activities(self.plan[0])
                if item[0][0] == activity_row[0]
            )
        property_suggestions = self.db.teaching_diary_activity_properties(
            activity_card["activity_name"], row.mode
        )
        dialog = PlanDistributionEntryDialog(
            row.mode,
            activity_card,
            row.display_number,
            default_duration,
            existing,
            self,
            property_suggestions=property_suggestions,
        )
        if not dialog.exec():
            return
        fields, properties, duration = dialog.values()
        try:
            self.db.save_teaching_plan_activity(
                self.plan[0],
                row.lesson_number,
                row.mode,
                activity_card["item_id"],
                fields,
                properties,
                activity_row[0] if activity_row else None,
                duration,
                activity_name=activity_card["activity_name"],
            )
        except (ValueError, TypeError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر حفظ توزيع النشاط", str(exc))
            return
        self.load_mode(row.mode)

    def drop_activity(self, row, activity):
        if "activity_id" in activity:
            self.copy_scheduled_activity(row, activity)
            return
        if activity not in self.activity_cards_for_mode(row.mode):
            QMessageBox.warning(
                self, "نشاط غير متاح",
                "لا يمكن توزيع هذا النشاط ضمن نمط التعلم المحدد.",
            )
            return
        self.open_entry_dialog(row, activity)

    def copy_scheduled_activity(self, target_row, payload):
        if payload.get("learning_mode") != target_row.mode:
            QMessageBox.warning(
                self, "نمط تعلم مختلف",
                "يمكن نسخ النشاط إلى حصص النمط المحدد فقط.",
            )
            return
        activity_id = payload.get("activity_id")
        activity = next(
            (
                saved for saved in self.saved_activities
                if saved[0][0] == activity_id
            ),
            None,
        )
        if activity is None:
            QMessageBox.warning(
                self, "تعذر نسخ النشاط",
                "لم يعد هذا التوزيع موجوداً في المخطط.",
            )
            return
        row, properties = activity
        fields = {
            key: str(row[index] or "")
            for key, index in PlanActivityDialog.FIELD_INDEX.items()
        }
        try:
            self.db.save_teaching_plan_activity(
                self.plan[0],
                target_row.lesson_number,
                target_row.mode,
                row[3],
                fields,
                [(key, value) for _property_id, key, value in properties],
                duration_minutes=row[16],
                activity_name=row[4],
            )
        except (ValueError, TypeError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "تعذر نسخ النشاط", str(exc))
            return
        self.load_mode(target_row.mode)

    def delete_activity(self, activity_id):
        answer = QMessageBox.question(
            self, "تأكيد الحذف", "هل تريد حذف هذا النشاط من الحصة؟"
        )
        if answer == QMessageBox.Yes:
            self.db.delete_teaching_plan_activity(activity_id)
            checked = self.mode_group.checkedButton()
            self.load_mode(checked.text() if checked else "منهجي")


class TeachingPlanCard(QFrame):
    selected = Signal(int)
    double_clicked = Signal(int)

    def __init__(self, plan, weekday_counts, items=(), parent=None):
        super().__init__(parent)
        self.plan_id = int(plan[0])
        self._selected = False
        self.setObjectName("teachingPlanCard")
        self.setMinimumWidth(360)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("""
            QFrame#teachingPlanCard {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 14px;
            }
            QFrame#teachingPlanCard[selected="true"] {
                background: #F8FBFF;
                border: 2px solid #2563EB;
            }
        """)
        self.setProperty("selected", "false")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)

        header = QHBoxLayout()
        title = QLabel(str(plan[1] or "").strip() or "مخطط دون اسم")
        title.setWordWrap(True)
        title.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #123B67; "
            "border: none; background: transparent;"
        )
        header.addWidget(title, 1)
        number_text = (
            f"عدد {plan[11]}" if plan[11] is not None else "دون رقم"
        )
        number = QLabel(number_text)
        number.setAlignment(Qt.AlignCenter)
        number.setStyleSheet(
            "font-weight: bold; color: #1D4ED8; background: #DBEAFE; "
            "border: none; border-radius: 10px; padding: 5px 9px;"
        )
        header.addWidget(number)
        layout.addLayout(header)

        class_label = QLabel(f"القسم: {plan[3]}")
        class_label.setStyleSheet(
            "font-weight: bold; color: #334155; border: none; "
            "background: transparent;"
        )
        layout.addWidget(class_label)

        info = QGridLayout()
        info.setHorizontalSpacing(10)
        info.setVerticalSpacing(5)
        self._add_info(info, 0, 0, "المجال", plan[5])
        self._add_info(info, 0, 1, "تاريخ الانطلاق", plan[8])
        self._add_info(info, 1, 0, "مدة الحصة", f"{plan[12]} دقيقة")
        self._add_info(info, 1, 1, "النهاية المتوقعة", plan[17])
        layout.addLayout(info)

        competence = QLabel(f"كفاية المجال: {plan[7] or ''}")
        competence.setWordWrap(True)
        competence.setStyleSheet(
            "color: #334155; border: none; border-radius: 8px; "
            "background: #F1F5F9; padding: 8px;"
        )
        layout.addWidget(competence)

        if items:
            items_heading = QLabel("الأنشطة وكفاياتها")
            items_heading.setStyleSheet(
                "font-weight: bold; color: #123B67; border: none; "
                "background: transparent;"
            )
            layout.addWidget(items_heading)
            for item, _details in items[:4]:
                item_name = str(item[1] or "").strip()
                item_competence = str(item[3] or "").strip()
                item_label = QLabel(
                    f"{item_name} — {item_competence}"
                    if item_competence else item_name
                )
                item_label.setWordWrap(True)
                item_label.setStyleSheet(
                    "color: #334155; border: none; border-radius: 7px; "
                    "background: #F8FAFC; padding: 5px;"
                )
                layout.addWidget(item_label)
            if len(items) > 4:
                more_items = QLabel(f"و{len(items) - 4} أنشطة أخرى")
                more_items.setStyleSheet(
                    "font-size: 10px; color: #64748B; border: none; "
                    "background: transparent;"
                )
                layout.addWidget(more_items)

        days = {day: name for name, day in WEEKDAYS}
        schedule_text = "، ".join(
            f"{days.get(day, str(day))}: {count}"
            for day, count in sorted(weekday_counts.items())
        ) or "غير محددة"
        schedule = QLabel(f"الحصص الأسبوعية: {schedule_text}")
        schedule.setWordWrap(True)
        schedule.setStyleSheet(
            "color: #475569; border: none; background: transparent;"
        )
        layout.addWidget(schedule)

        mode_names = ("منهجي", "إدماج", "تقييم", "دعم وعلاج")
        mode_colors = {
            "منهجي": ("#1D4ED8", "#DBEAFE"),
            "إدماج": ("#059669", "#ECFDF5"),
            "تقييم": ("#D97706", "#FFFBEB"),
            "دعم وعلاج": ("#7C3AED", "#F5F3FF"),
        }
        modes = QHBoxLayout()
        modes.setSpacing(6)
        for index, mode in enumerate(mode_names, start=13):
            color, background = mode_colors[mode]
            label = QLabel(f"{mode}: {plan[index]}")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(
                f"font-size: 11px; color: {color}; background: {background}; "
                "border: none; border-radius: 8px; padding: 5px;"
            )
            modes.addWidget(label, 1)
        layout.addLayout(modes)

        lesson_total = QLabel(f"إجمالي الحصص: {plan[9]}")
        lesson_total.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lesson_total.setStyleSheet(
            "font-weight: bold; color: #123B67; border: none; "
            "background: transparent;"
        )
        layout.addWidget(lesson_total)

        for label in self.findChildren(QLabel):
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    @staticmethod
    def _add_info(layout, row, column, title, value):
        block = QWidget()
        block.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        block_layout = QVBoxLayout(block)
        block_layout.setContentsMargins(0, 0, 0, 0)
        block_layout.setSpacing(2)
        caption = QLabel(title)
        caption.setStyleSheet(
            "font-size: 10px; color: #64748B; border: none; "
            "background: transparent;"
        )
        text = QLabel(str(value or ""))
        text.setWordWrap(True)
        text.setStyleSheet(
            "font-weight: bold; color: #334155; border: none; "
            "background: transparent;"
        )
        block_layout.addWidget(caption)
        block_layout.addWidget(text)
        layout.addWidget(block, row, column)

    def set_selected(self, selected):
        self._selected = bool(selected)
        self.setProperty("selected", "true" if self._selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.selected.emit(self.plan_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self.plan_id)
        super().mouseDoubleClickEvent(event)


class TeachingPlansPage(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._distribution_pages = {}
        self._plan_cards = {}
        self._selected_plan_id = None
        self.setAcceptDrops(True)
        self.setLayoutDirection(Qt.RightToLeft)
        layout = QVBoxLayout(self)
        title = QLabel("المخططات التعليمية")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #1E40AF;")
        layout.addWidget(title)
        guidance = QLabel(
            "النمط المنهجي توجيهي بنسبة تقريبية 75% من التعلمات، ولا يفرض التطبيق توزيعاً آلياً."
        )
        guidance.setStyleSheet("color: #475569;")
        layout.addWidget(guidance)

        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("البحث في المخططات:"))
        self.plan_search = QLineEdit()
        self.plan_search.setPlaceholderText(
            "ابحث عن مخطط سابق بالاسم أو القسم أو المجال أو الكفاية..."
        )
        self.plan_search.setLayoutDirection(Qt.RightToLeft)
        self.plan_search.setAlignment(Qt.AlignRight)
        self.plan_search.setAcceptDrops(False)
        self.plan_search.textChanged.connect(self.load_plans)
        search_layout.addWidget(self.plan_search, 1)
        layout.addLayout(search_layout)

        plan_buttons = QHBoxLayout()
        for text, callback, action_type in (
            ("مخطط جديد", self.add_plan, "add"),
            ("تعديل المخطط", self.edit_plan, "edit"),
            ("حذف المخطط", self.delete_plan, "delete"),
        ):
            button = QPushButton(text)
            _set_action_type(button, action_type)
            if text == "مخطط جديد":
                _style_primary_button(button)
            button.clicked.connect(callback)
            plan_buttons.addWidget(button)
        self.configure_items_button = QPushButton("إعداد أنشطة المخطط")
        _set_action_type(self.configure_items_button, "edit")
        _style_primary_button(self.configure_items_button)
        self.configure_items_button.clicked.connect(self.configure_items)
        self.configure_items_button.setEnabled(False)
        plan_buttons.addWidget(self.configure_items_button)
        self.distribute_activities_button = QPushButton(
            "توزيع أنشطة المخطط على الحصص"
        )
        _set_action_type(self.distribute_activities_button, "browse")
        self.distribute_activities_button.clicked.connect(
            self.distribute_activities
        )
        self.distribute_activities_button.setEnabled(False)
        plan_buttons.addWidget(self.distribute_activities_button)
        self.export_json_button = QPushButton("تصدير JSON")
        _set_action_type(self.export_json_button, "browse")
        self.export_json_button.clicked.connect(self.export_selected_plan)
        self.export_json_button.setEnabled(False)
        plan_buttons.addWidget(self.export_json_button)
        self.import_json_button = QPushButton("استيراد JSON")
        _set_action_type(self.import_json_button, "browse")
        self.import_json_button.clicked.connect(self.import_plan_file)
        plan_buttons.addWidget(self.import_json_button)
        self.export_pdf_button = QPushButton("تصدير PDF")
        _set_action_type(self.export_pdf_button, "browse")
        self.export_pdf_button.clicked.connect(
            self.export_selected_plan_pdf
        )
        self.export_pdf_button.setEnabled(False)
        plan_buttons.addWidget(self.export_pdf_button)
        plan_buttons.addStretch()
        layout.addLayout(plan_buttons)

        self.plans_scroll = QScrollArea()
        self.plans_scroll.setWidgetResizable(True)
        self.plans_scroll.setAcceptDrops(False)
        self.plans_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.plans_container = QWidget()
        self.plans_container.setLayoutDirection(Qt.RightToLeft)
        self.plans_grid = QGridLayout(self.plans_container)
        self.plans_grid.setContentsMargins(8, 8, 8, 8)
        self.plans_grid.setHorizontalSpacing(12)
        self.plans_grid.setVerticalSpacing(12)
        self.plans_scroll.setWidget(self.plans_container)
        layout.addWidget(self.plans_scroll, 1)
        self.load_plans()

    @staticmethod
    def _configure_table(table):
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

    @staticmethod
    def _is_json_file_drop(event):
        mime_data = event.mimeData()
        if not mime_data.hasUrls():
            return False
        urls = mime_data.urls()
        return (
            len(urls) == 1
            and urls[0].isLocalFile()
            and urls[0].toLocalFile().lower().endswith(".json")
        )

    def dragEnterEvent(self, event):
        if self._is_json_file_drop(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if self._is_json_file_drop(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        if not self._is_json_file_drop(event):
            event.ignore()
            return
        event.acceptProposedAction()
        self.import_plan_file(
            event.mimeData().urls()[0].toLocalFile()
        )

    def export_selected_plan(self):
        plan_id = self.selected_plan()
        if plan_id is None:
            QMessageBox.information(
                self, "اختيار مطلوب", "حدد مخططاً لتصديره."
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "تصدير المخطط", "", "ملفات JSON (*.json)"
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            snapshot = self.db.export_teaching_plan(plan_id)
            with open(path, "w", encoding="utf-8") as file:
                json.dump(snapshot, file, ensure_ascii=False, indent=2)
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(
                self, "تعذر تصدير المخطط", str(error)
            )
            return
        QMessageBox.information(self, "اكتمل التصدير", "تم تصدير المخطط بنجاح.")

    def export_selected_plan_pdf(self):
        plan_id = self.selected_plan()
        if plan_id is None:
            QMessageBox.information(
                self, "اختيار مطلوب", "حدد مخططاً لتصديره."
            )
            return
        try:
            snapshot = self.db.export_teaching_plan(plan_id)
            documents_path = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DocumentsLocation
            )
            if not documents_path:
                raise OSError("تعذر تحديد مجلد Documents الخاص بالنظام.")
            documents = Path(documents_path)
            documents.mkdir(parents=True, exist_ok=True)

            plan = snapshot["plan"]
            plan_name = _teaching_plan_name(snapshot)
            plan_number = (
                plan.get("plan_number")
                if plan.get("plan_number") is not None else "غير محدد"
            )
            filename = (
                f"مخطط {plan_name} - عدد {plan_number} "
                f"القسم - {plan.get('class_name') or ''}"
            )
            filename = "".join(
                "_" if character in '<>:"/\\|?*' or ord(character) < 32
                else character
                for character in filename
            ).strip(" .")
            output_path = documents / f"{filename[:180] or 'مخطط'}.pdf"
            export_teaching_plan_pdf(snapshot, output_path)
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
            QMessageBox.warning(
                self, "تعذر تصدير المخطط", str(error)
            )
            return
        QMessageBox.information(
            self,
            "اكتمل التصدير",
            f"تم تصدير المخطط إلى:\n{output_path}",
        )

    def import_plan_file(self, path=None):
        if not isinstance(path, str):
            path, _ = QFileDialog.getOpenFileName(
                self, "استيراد مخطط", "", "ملفات JSON (*.json)"
            )
        if not path:
            return
        if not path.lower().endswith(".json"):
            QMessageBox.warning(
                self, "ملف غير صالح", "اختر ملفاً بامتداد JSON."
            )
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as file:
                snapshot = json.load(file)
            if not isinstance(snapshot, dict):
                raise ValueError("يجب أن يحتوي ملف JSON على كائن مخطط.")
            if (
                snapshot.get("format") != "jrnl_teaching_plan"
                or snapshot.get("version") != 1
            ):
                raise ValueError(
                    "صيغة ملف JSON غير مدعومة للمخططات التعليمية."
                )
            plan_data = snapshot.get("plan")
            if not isinstance(plan_data, dict):
                raise ValueError("بيانات المخطط غير صالحة في ملف JSON.")
            duplicate_rows = [
                row for row in self.db.teaching_plans()
                if (
                    str(row[1]).strip().casefold()
                    == str(plan_data.get("title") or "").strip().casefold()
                    and str(row[3]).strip().casefold()
                    == str(plan_data.get("class_name") or "").strip().casefold()
                    and str(row[5]).strip().casefold()
                    == str(plan_data.get("domain_name") or "").strip().casefold()
                    and str(row[8]).strip()
                    == str(plan_data.get("start_date") or "").strip()
                )
            ]
            replace_plan_id = None
            if duplicate_rows:
                target = duplicate_rows[0]
                if len(duplicate_rows) > 1:
                    choices = [
                        f"{row[1]} — {row[3]} — {row[8]} (#{row[0]})"
                        for row in duplicate_rows
                    ]
                    selected, accepted = QInputDialog.getItem(
                        self, "اختيار المخطط المطابق",
                        "توجد عدة مخططات مطابقة؛ اختر المخطط المراد استبداله:",
                        choices, 0, False,
                    )
                    if not accepted:
                        return
                    target = duplicate_rows[choices.index(selected)]

                prompt = QMessageBox(self)
                prompt.setWindowTitle("مخطط مكرر")
                prompt.setText(
                    "يوجد مخطط بالقسم والمجال والاسم وتاريخ الانطلاق نفسها."
                )
                copy_button = prompt.addButton(
                    "استيراد كنسخة جديدة", QMessageBox.AcceptRole
                )
                replace_button = prompt.addButton(
                    "استبدال المخطط المطابق", QMessageBox.DestructiveRole
                )
                prompt.addButton("إلغاء", QMessageBox.RejectRole)
                prompt.exec()
                if prompt.clickedButton() is replace_button:
                    replace_plan_id = target[0]
                elif prompt.clickedButton() is not copy_button:
                    return

            imported_id = self.db.import_teaching_plan(
                snapshot, replace_plan_id=replace_plan_id
            )
            if replace_plan_id is not None:
                self._distribution_pages.pop(replace_plan_id, None)
            self.load_plans(include_plan_id=imported_id)
        except (OSError, json.JSONDecodeError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(
                self, "تعذر استيراد المخطط", str(error)
            )
            return
        message = "تم استيراد المخطط بنجاح."
        if (
            plan_data.get("plan_number") is not None
            and self.db.teaching_plan_number(imported_id) is None
        ):
            message += (
                "\nلم يُحفظ رقم المخطط لأنه مستخدم بالفعل في القسم والمجال؛ "
                "يمكن تعيين رقم آخر من إعدادات المخطط."
            )
        QMessageBox.information(self, "اكتمل الاستيراد", message)

    def selected_plan(self):
        if self._selected_plan_id in self._plan_cards:
            return self._selected_plan_id
        return None

    def load_plans(self, include_plan_id=None):
        previous_plan_id = self._selected_plan_id
        all_rows = self.db.teaching_plans()
        query = self.plan_search.text().strip().casefold()
        if query:
            rows = [
                row for row in all_rows
                if any(
                    query in str(value or "").casefold()
                    for value in (row[1], row[3], row[5], row[7], row[8])
                )
            ]
        else:
            today = QDate.currentDate().toString("yyyy-MM-dd")
            rows = [
                row for row in all_rows
                if row[8] <= today <= row[17]
            ]
        if include_plan_id is not None:
            included = next(
                (row for row in all_rows if row[0] == include_plan_id),
                None,
            )
            if included is not None and all(
                row[0] != include_plan_id for row in rows
            ):
                rows.append(included)
            rows.sort(key=lambda row: (row[8], row[0]), reverse=True)
        self._clear_plan_cards()
        selected_plan_id = None
        if rows:
            visible_ids = {row[0] for row in rows}
            if include_plan_id in visible_ids:
                selected_plan_id = include_plan_id
            elif previous_plan_id in visible_ids:
                selected_plan_id = previous_plan_id
            else:
                selected_plan_id = rows[0][0]
            for index, row in enumerate(rows):
                plan_id = row[0]
                try:
                    weekday_counts = self.db.teaching_plan_weekday_counts(
                        plan_id
                    )
                except sqlite3.Error as error:
                    QMessageBox.warning(
                        self, "تعذر تحميل أيام المخطط", str(error)
                    )
                    weekday_counts = {}
                try:
                    items = self.db.teaching_plan_items(plan_id)
                except sqlite3.Error as error:
                    QMessageBox.warning(
                        self, "تعذر تحميل أنشطة المخطط", str(error)
                    )
                    items = []
                card = TeachingPlanCard(row, weekday_counts, items)
                card.selected.connect(self.select_plan)
                card.double_clicked.connect(self.edit_plan_by_id)
                self._plan_cards[plan_id] = card
                self.plans_grid.addWidget(card, index // 2, index % 2)
                card.set_selected(plan_id == selected_plan_id)
            self.plans_grid.setRowStretch((len(rows) + 1) // 2, 1)
        else:
            empty = QLabel("لا توجد مخططات مطابقة للبحث.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color: #64748B; padding: 24px;")
            self.plans_grid.addWidget(empty, 0, 0, 1, 2)
        self._selected_plan_id = selected_plan_id
        self.update_plan_actions()

    def _clear_plan_cards(self):
        self._plan_cards.clear()
        while self.plans_grid.count():
            item = self.plans_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def select_plan(self, plan_id):
        if plan_id not in self._plan_cards:
            return
        self._selected_plan_id = plan_id
        for card_id, card in self._plan_cards.items():
            card.set_selected(card_id == plan_id)
        self.update_plan_actions()

    def update_plan_actions(self):
        has_selected_plan = self.selected_plan() is not None
        self.configure_items_button.setEnabled(has_selected_plan)
        self.distribute_activities_button.setEnabled(has_selected_plan)
        self.export_json_button.setEnabled(has_selected_plan)
        self.export_pdf_button.setEnabled(has_selected_plan)

    def add_plan(self):
        dialog = TeachingPlanDialog(self.db, self)
        dialog.exec()
        self.load_plans()

    def edit_plan(self):
        plan_id = self.selected_plan()
        if plan_id is None:
            QMessageBox.information(self, "اختيار مطلوب", "حدد مخططاً لتعديله.")
            return
        dialog = TeachingPlanDialog(self.db, self, self.db.teaching_plan(plan_id))
        dialog.exec()
        self.load_plans()

    def delete_plan(self):
        plan_id = self.selected_plan()
        if plan_id is None:
            QMessageBox.information(self, "اختيار مطلوب", "حدد مخططاً لحذفه.")
            return
        answer = QMessageBox.question(
            self, "تأكيد الحذف",
            "سيُحذف المخطط وحصصه وخصائصه الإضافية. هل تريد المتابعة؟",
        )
        if answer == QMessageBox.Yes:
            self.db.delete_teaching_plan(plan_id)
            self.load_plans()

    def configure_items(self):
        plan_id = self.selected_plan()
        if plan_id is None:
            QMessageBox.information(self, "اختيار مطلوب", "حدد مخططاً أولاً.")
            return
        dialog = PlanItemsDialog(
            self.db, self.db.teaching_plan(plan_id), self
        )
        dialog.exec()
        self.load_plans(include_plan_id=plan_id)

    def edit_plan_by_id(self, plan_id):
        if plan_id not in self._plan_cards:
            return
        self.select_plan(plan_id)
        self.edit_plan()

    def distribute_activities(self):
        plan_id = self.selected_plan()
        if plan_id is None:
            QMessageBox.information(self, "اختيار مطلوب", "حدد مخططاً أولاً.")
            return
        plan = self.db.teaching_plan(plan_id)
        error = self.distribution_validation_error(plan)
        if error:
            QMessageBox.warning(self, "المخطط غير جاهز للتوزيع", error)
            return

        window = self.window()
        pages_stack = getattr(window, "pages_stack", None)
        if pages_stack is None:
            QMessageBox.warning(
                self,
                "تعذر فتح صفحة التوزيع",
                "تعذر الوصول إلى مكدس صفحات التطبيق.",
            )
            return

        page = self._distribution_pages.get(plan_id)
        if page is None:
            page = PlanDistributionPage(
                self.db,
                plan,
                lambda: self.return_from_distribution(pages_stack),
            )
            self._distribution_pages[plan_id] = page
            pages_stack.addWidget(page)
        else:
            page.plan = plan
            page.load()
        pages_stack.setCurrentWidget(page)

    def distribution_validation_error(self, plan):
        if plan is None:
            return "تعذر تحميل بيانات المخطط."
        if not str(plan[1] or "").strip():
            return "أدخل اسم المخطط واحفظه أولاً."
        if any(value is None for value in plan[2:5]):
            return "تحقق من القسم والمجال وكفاية المجال في إعدادات المخطط."
        start_date = QDate.fromString(str(plan[5]), "yyyy-MM-dd")
        if not start_date.isValid():
            return "تاريخ انطلاق المخطط غير صالح."
        try:
            weekdays = [int(day) for day in str(plan[7]).split(",") if day]
        except ValueError:
            return "أيام التدريس المسجلة في المخطط غير صالحة."
        if not weekdays or any(day < 0 or day > 6 for day in weekdays):
            return "حدد أيام تدريس صالحة في إعدادات المخطط."
        if plan[9] not in (30, 60, 120):
            return "حدد زمناً صالحاً للحصة: 30 أو 60 أو 120 دقيقة."

        modes = PlanActivityDialog.MODES
        try:
            mode_counts = tuple(int(count) for count in plan[10:14])
            lesson_count = int(plan[6])
        except (TypeError, ValueError):
            return "أعداد الحصص حسب نمط التعلم غير صالحة."
        if (
            len(mode_counts) != len(modes)
            or any(count < 1 for count in mode_counts)
            or sum(mode_counts) != lesson_count
        ):
            return (
                "حدد عدداً صحيحاً لا يقل عن حصة واحدة لكل نمط تعلم، "
                "وتأكد من أن مجموع الأعداد يساوي إجمالي حصص المخطط."
            )
        return None

    def return_from_distribution(self, pages_stack):
        pages_stack.setCurrentWidget(self)
        self.update_plan_actions()
