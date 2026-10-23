# -*- coding: utf-8 -*-
"""الواجهة الرئيسية (SchoolPlanner) — بقية الكود مقسم إلى:
app_config, database, completion, dialogs, widgets.
"""
from app_config import (APP_DIR, DB_PATH, ICON_PATH, SCHOOL_INFO_COLUMNS,
                        ArabicDialogTranslator, load_custom_font)
from database import (
    Database, SchoolDataFormatter, activity_completion_issues,
    duration_minutes, lesson_completion_issues,
    required_activity_properties,
)
from dialogs import (ActivityEntryDialog, BulkLessonEntryDialog, CompetenceManagerDialog,
                     BulkActivityDetailsDialog, ConceptManagerDialog,
                     CustomCalendarWidget, DaySelectionDialog,
                     DetailEntryDialog, EntryDialog, LessonEntryDialog,
                     PreviousSchoolYearsDialog, SchoolYearSelectionDialog,
                     TimetableDialog, YearImportDialog)
from widgets import (CompetencesCbaBrowsePage, DayLessonsBrowsePage,
                     OfficialProgramsBrowsePage, OfficialProgramsWidget)
from plans import (
    OfficialHolidaysPage, TeachingPlanViewPage, TeachingPlansPage,
)

import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
import numpy as np

import json
from datetime import datetime
from pathlib import Path
import pandas as pd
from database_connection import connect_database
import sqlite3
from PySide6.QtCore import Qt, QDate, QLocale, QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QGridLayout, QSplitter, QGroupBox, QDialog,
    QLabel, QLineEdit, QDateEdit,
    QTableWidget, QTableWidgetItem, QPushButton, QMessageBox,
    QHeaderView, QAbstractItemView, QMenu, QToolButton, QScrollArea,
)
import sys


SCHOOL_INFO_DIALOG_FIELDS = [
    ("السنة الدراسية", "text", ""),
    ("المؤسسة", "text", ""),
    ("المعلم", "text", ""),
    ("المتفقد", "text", ""),
    ("اسم المساعد البيداغوجي", "text", ""),
    ("الرتبة", "text", ""),
    ("الشهادة العلمية", "text", ""),
    ("تاريخ الانتداب", "date", ""),
    ("تاريخ الترسيم", "date", ""),
    ("تاريخ الزيارة الأخيرة", "date", ""),
    ("العدد البيداغوجي المسند", "text", ""),
    ("خلاصة تقرير الزيارة", "plaintext", ""),
    ("اللغة", "text", ""),
    ("الهاتف", "text", ""),
    ("البريد الإلكتروني", "text", ""),
    ("الحالة العائلية", "text", ""),
    ("عدد الأطفال", "nonnegative_int", 0),
]


def _school_info_to_dialog_values(values):
    return list(values[:4]) + [values[5], values[4]] + list(values[6:])


def _school_info_from_dialog_values(values):
    return list(values[:4]) + [values[5], values[4]] + list(values[6:])


class StatisticsLoadWorker(QObject):
    result_ready = Signal(object, int, bool)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, database_path, force_refresh=False):
        super().__init__()
        self.database_path = database_path
        self.cache_path = f"{database_path}.analytics.sqlite3"
        self.force_refresh = force_refresh

    @Slot()
    def run(self):
        try:
            with connect_database(self.database_path, timeout=10) as con:
                con.execute("BEGIN")
                revision_row = con.execute(
                    "SELECT revision FROM app_data_revision WHERE id=1"
                ).fetchone()
                revision = int(revision_row[0]) if revision_row else 0

                with connect_database(self.cache_path, timeout=10) as cache:
                    cache.execute("""
                        CREATE TABLE IF NOT EXISTS analytics_cache (
                            source_path TEXT PRIMARY KEY,
                            source_revision INTEGER NOT NULL,
                            payload TEXT NOT NULL,
                            refreshed_at TEXT NOT NULL
                        )
                    """)
                    cached = cache.execute(
                        """SELECT source_revision, payload
                           FROM analytics_cache WHERE source_path=?""",
                        (str(Path(self.database_path).resolve()),),
                    ).fetchone()
                if (
                    not self.force_refresh and cached
                    and int(cached[0]) == revision
                ):
                    con.rollback()
                    self.result_ready.emit(
                        json.loads(cached[1]), revision, True
                    )
                    return

                source = pd.read_sql_query("""
                    SELECT h.id AS lesson_id, d.date, c.name AS class_name,
                        m.name AS subject, lm.name AS learning_mode,
                        h.time AS lesson_time,
                        n.id AS activity_id, n.title AS activity_title,
                        n.time AS activity_time,
                        nd.id AS detail_id, nd.key_name,
                        nd.value_text
                    FROM hessa h
                    JOIN day_data d ON d.id = h.day_data_id
                    JOIN classe c ON c.id = h.classe_id
                    LEFT JOIN majal m ON m.id = h.majal_id
                    LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                    LEFT JOIN nachat n ON n.hessa_id = h.id
                    LEFT JOIN nachat_detail nd ON nd.nachat_id = n.id
                    ORDER BY d.date, h.id, n.id, nd.id
                """, con)
                con.commit()

            payload = self._build_analytics(source)
            with connect_database(self.cache_path, timeout=10) as cache:
                cache.execute("""
                    INSERT INTO analytics_cache(
                        source_path, source_revision, payload, refreshed_at
                    ) VALUES(?, ?, ?, datetime('now', 'localtime'))
                    ON CONFLICT(source_path) DO UPDATE SET
                        source_revision=excluded.source_revision,
                        payload=excluded.payload,
                        refreshed_at=excluded.refreshed_at
                """, (
                    str(Path(self.database_path).resolve()), revision,
                    json.dumps(payload, ensure_ascii=False),
                ))
            self.result_ready.emit(payload, revision, False)
        except (sqlite3.Error, pd.errors.DatabaseError, ValueError, TypeError) as exc:
            self.failed.emit(f"تعذر تحميل الإحصائيات من قاعدة البيانات: {exc}")
        finally:
            self.finished.emit()

    @staticmethod
    def _build_analytics(source):
        mode_properties = {
            "منهجي": ("المحتوى", "هدف الحصة"),
            "إدماج": ("الكفايات المستهدفة",),
            "تقييم": ("الأداء المنتظر",),
            "دعم وعلاج": ("المعايير المستهدفة",),
        }
        if source.empty:
            lessons = pd.DataFrame(columns=[
                "lesson_id", "date", "class_name", "subject",
                "learning_mode", "lesson_time",
            ])
            activities = pd.DataFrame(columns=[
                "activity_id", "lesson_id", "activity_title",
                "activity_time", "learning_mode",
            ])
            details = pd.DataFrame(columns=[
                "detail_id", "activity_id", "activity_title",
                "key_name", "value_text", "learning_mode",
            ])
        else:
            lessons = source.drop_duplicates("lesson_id")[
                ["lesson_id", "date", "class_name", "subject",
                 "learning_mode", "lesson_time"]
            ].copy()
            activities = source.dropna(subset=["activity_id"]).drop_duplicates(
                "activity_id"
            )[
                ["activity_id", "lesson_id", "activity_title",
                 "activity_time", "learning_mode"]
            ].copy()
            details = source.dropna(subset=["detail_id"])[
                ["detail_id", "activity_id", "activity_title", "key_name",
                 "value_text", "learning_mode"]
            ].copy()

        lesson_count = int(len(lessons))
        activity_count = int(len(activities))
        detail_count = int(len(details))
        classes_count = int(lessons["class_name"].nunique())
        dates = pd.to_datetime(lessons["date"], errors="coerce")
        recorded_days = int(dates.nunique())

        activity_counts = (
            activities.groupby("lesson_id").size()
            if not activities.empty else pd.Series(dtype="int64")
        )
        metrics = {
            "lessons": lesson_count,
            "days": recorded_days,
            "classes": classes_count,
            "activities": activity_count,
            "properties": detail_count,
            "average_activities": round(
                activity_count / lesson_count, 1
            ) if lesson_count else 0,
            "average_properties": round(
                detail_count / activity_count, 1
            ) if activity_count else 0,
        }

        lesson_minutes = pd.to_numeric(
            lessons["lesson_time"].astype(str).str.extract(r"(\d+)")[0],
            errors="coerce",
        )
        lesson_minutes.index = lessons["lesson_id"]
        if not activities.empty:
            activities["duration_minutes"] = pd.to_numeric(
                activities["activity_time"].astype(str).str.extract(
                    r"(\d+)"
                )[0],
                errors="coerce",
            )
            activity_minutes = activities.groupby("lesson_id")[
                "duration_minutes"
            ].sum(min_count=1)
            has_valid_activity_times = activities.groupby(
                "lesson_id"
            )["duration_minutes"].count()
            actual_activity_count = activities.groupby("lesson_id").size()
        else:
            activity_minutes = pd.Series(dtype="float64")
            has_valid_activity_times = pd.Series(dtype="int64")
            actual_activity_count = pd.Series(dtype="int64")

        filled_keys = {}
        if not details.empty:
            valid_details = details[
                details["value_text"].fillna("").astype(str).str.strip() != ""
            ]
            filled_keys = (
                valid_details.groupby("activity_id")["key_name"]
                .agg(lambda values: set(str(value).strip() for value in values))
                .to_dict()
            )
        activity_complete = {}
        for activity in activities.itertuples(index=False):
            required = mode_properties.get(
                str(activity.learning_mode or "").strip(), ()
            )
            present = filled_keys.get(activity.activity_id, set())
            activity_complete[activity.activity_id] = all(
                key in present for key in required
            )
        complete_activity_counts = (
            activities.assign(
                is_complete=activities["activity_id"].map(activity_complete)
            ).groupby("lesson_id")["is_complete"].all()
            if not activities.empty else pd.Series(dtype=bool)
        )
        has_activities = activity_counts.reindex(
            lessons["lesson_id"], fill_value=0
        ) > 0
        minutes_match = (
            activity_minutes.reindex(lessons["lesson_id"]).to_numpy()
            == lesson_minutes.reindex(lessons["lesson_id"]).to_numpy()
        )
        valid_times = (
            has_valid_activity_times.reindex(
                lessons["lesson_id"], fill_value=0
            ).to_numpy()
            == actual_activity_count.reindex(
                lessons["lesson_id"], fill_value=0
            ).to_numpy()
        )
        all_activities_complete = complete_activity_counts.reindex(
            lessons["lesson_id"], fill_value=False
        ).to_numpy()
        complete_lessons = int(
            (has_activities.to_numpy() & minutes_match & valid_times
             & all_activities_complete).sum()
        )
        metrics["complete_lessons"] = complete_lessons
        metrics["incomplete_lessons"] = lesson_count - complete_lessons
        metrics["completion_rate"] = round(
            complete_lessons / lesson_count * 100, 1
        ) if lesson_count else 0

        subject_counts = (
            lessons.assign(subject=lessons["subject"].fillna("غير محدد"))
            .groupby("subject").size().sort_values(ascending=False)
        )
        mode_counts = (
            lessons.assign(
                learning_mode=lessons["learning_mode"].fillna("غير محدد")
            ).groupby("learning_mode").size().sort_values(ascending=False)
        )
        activity_title_counts = (
            activities.assign(
                activity_title=activities["activity_title"].fillna("غير محدد")
            ).groupby("activity_title").size().sort_values(ascending=False)
        )
        if details.empty:
            property_usage = []
        else:
            used_details = details[
                details["value_text"].fillna("").astype(str).str.strip() != ""
            ].copy()
            used_details["activity_title"] = (
                used_details["activity_title"].fillna("غير محدد")
            )
            used_details["key_name"] = used_details["key_name"].fillna(
                "غير محدد"
            )
            grouped = used_details.groupby(
                ["activity_title", "key_name"]
            ).agg(
                uses=("detail_id", "count"),
                activities=("activity_id", "nunique"),
            ).sort_values(
                ["uses", "activities"], ascending=False
            ).head(40)
            property_usage = [
                {
                    "activity": str(activity),
                    "property": str(key),
                    "uses": int(row["uses"]),
                    "activities": int(row["activities"]),
                }
                for (activity, key), row in grouped.iterrows()
            ]

        weekly = []
        forecast = []
        valid_dates = dates.dropna()
        if not valid_dates.empty:
            lesson_dates = lessons.loc[valid_dates.index, ["lesson_id"]].copy()
            lesson_dates["week"] = valid_dates.dt.to_period("W").astype(str)
            weekly_series = lesson_dates.groupby("week")["lesson_id"].nunique()
            last_period = valid_dates.max().to_period("W")
            periods = pd.period_range(
                end=last_period, periods=12, freq="W"
            )
            weekly_series = weekly_series.reindex(
                periods.astype(str), fill_value=0
            )
            weekly = [
                {"week": str(period), "lessons": int(weekly_series.loc[str(period)])}
                for period in periods
            ]
            baseline = weekly_series.tail(8).astype(float)
            if len(baseline) >= 2 and baseline.sum() > 0:
                x_values = list(range(len(baseline)))
                slope, intercept = np.polyfit(
                    x_values, baseline.to_numpy(), 1
                )
                predicted = [
                    max(0, int(round(slope * index + intercept)))
                    for index in range(len(baseline), len(baseline) + 4)
                ]
            else:
                average = float(baseline.mean()) if len(baseline) else 0
                predicted = [max(0, int(round(average)))] * 4
            future_periods = pd.period_range(
                start=last_period + 1, periods=4, freq="W"
            )
            forecast = [
                {"week": str(period), "lessons": predicted[index]}
                for index, period in enumerate(future_periods)
            ]

        return {
            "metrics": metrics,
            "subjects": [
                {"name": str(name), "count": int(count)}
                for name, count in subject_counts.items()
            ],
            "learning_modes": [
                {"name": str(name), "count": int(count)}
                for name, count in mode_counts.items()
            ],
            "activities": [
                {"name": str(name), "count": int(count)}
                for name, count in activity_title_counts.head(12).items()
            ],
            "property_usage": property_usage,
            "weekly": weekly,
            "forecast": forecast,
        }

class LessonHistoryLoadWorker(QObject):
    result_ready = Signal(object, object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self, database_path, weekday_num, existing_count,
        generate_defaults=True,
    ):
        super().__init__()
        self.database_path = database_path
        self.weekday_num = weekday_num
        self.existing_count = existing_count
        self.generate_defaults = generate_defaults

    @Slot()
    def run(self):
        try:
            with connect_database(self.database_path, timeout=10) as con:
                history = pd.read_sql_query("""
                    SELECT c.name AS classe, m.name AS majal, h.heure, h.time,
                        h.wehda_number, h.hessa_number,
                        lm.name AS namat_taallom, d.date
                    FROM hessa h
                    JOIN day_data d ON h.day_data_id = d.id
                    JOIN classe c ON c.id = h.classe_id
                    LEFT JOIN majal m ON m.id=h.majal_id
                    LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                """, con)

            defaults = None
            if self.generate_defaults and not history.empty:
                history["date"] = pd.to_datetime(
                    history["date"], errors="coerce"
                )
                same_weekday = history[
                    history["date"].dt.dayofweek == self.weekday_num
                ]
                if self.existing_count < len(same_weekday) and not same_weekday.empty:
                    def most_common(column, fallback):
                        values = same_weekday[column].mode()
                        return values.iloc[0] if not values.empty else fallback

                    defaults = {
                        "classe": most_common("classe", ""),
                        "majal": most_common("majal", ""),
                        "heure": most_common("heure", "08:00-09:00"),
                        "time": most_common("time", "60 دقيقة"),
                        "namat": most_common("namat_taallom", "منهجي"),
                    }
            self.result_ready.emit(history, defaults)
        except (sqlite3.Error, pd.errors.DatabaseError) as exc:
            self.failed.emit(f"تعذر تحميل سجل الحصص السابقة: {exc}")
        finally:
            self.finished.emit()


class TimetableLoadWorker(QObject):
    result_ready = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, database_path, year_id):
        super().__init__()
        self.database_path = database_path
        self.year_id = year_id

    @Slot()
    def run(self):
        try:
            with connect_database(self.database_path, timeout=10) as con:
                history = pd.read_sql_query("""
                    SELECT c.name AS classe, m.name AS majal, h.heure, h.time,
                        h.wehda_number, h.hessa_number,
                        lm.name AS namat_taallom, d.date
                    FROM hessa h
                    JOIN day_data d ON h.day_data_id = d.id
                    JOIN classe c ON c.id = h.classe_id
                    LEFT JOIN majal m ON m.id=h.majal_id
                    LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                    WHERE d.ann_scolaire_id = ?
                """, con, params=(self.year_id,))
            schedule = TimetableDialog.build_schedule(history)
            self.result_ready.emit(schedule)
        except (sqlite3.Error, pd.errors.DatabaseError, ValueError, TypeError) as exc:
            self.failed.emit(f"تعذر توليد جدول الأوقات: {exc}")
        finally:
            self.finished.emit()


class SchoolPlanner(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database(DB_PATH)
        self.day_id = None
        self.selected_lesson_id = None
        self.selected_activity_id = None
        self.copied_details = []
        self.copied_lessons = []
        self.is_school_year_read_only = False
        
        self.setWindowTitle("المعين للتخطيط اليومي للحصص")
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
            
        self.resize(1250, 800)
        self.setLayoutDirection(Qt.RightToLeft)
        self.build_ui()
        self.load_school_info()

        self.db.cleanup_empty_days()
        workdays = self.db.recorded_workdays()
        if workdays:
            day_id, latest_date_str = workdays[0]
            qd = QDate.fromString(latest_date_str, "yyyy-MM-dd")
            if qd.isValid():
                self.date_edit.setDate(qd)
        
        self.load_day()
        if workdays:
            self.day_lessons_page.set_day(day_id, latest_date_str)
            self.pages_stack.setCurrentWidget(self.day_lessons_page)
        else:
            self.pages_stack.setCurrentWidget(self.lesson_page)
        self.load_statistics()
        self.setStyleSheet("QLineEdit { qproperty-alignment: AlignCenter; }")
        self.showMaximized()
        if not workdays:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.information(
                    self,
                    "لا يوجد يوم عمل مسجل",
                    "لا يوجد يوم عمل مسجل بعد. تم فتح صفحة تصفح دفتر إعداد الدروس.",
                ),
            )
        self.setAcceptDrops(True)

    def build_ui(self):
        self._year_scoped_mutation_widgets = []
        self._year_scoped_mutation_actions = []
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        from PySide6.QtGui import QKeySequence, QShortcut
        self.competence_shortcut = QShortcut(QKeySequence("Ctrl+Shift+E"), self)
        self.competence_shortcut.activated.connect(self.open_competence_manager)
        self.concept_shortcut = QShortcut(QKeySequence("Ctrl+Shift+R"), self)
        self.concept_shortcut.activated.connect(self.open_concept_manager)

        self.school_box = QGroupBox("بيانات المعلم")
        school_grid = QGridLayout(self.school_box)
        self.year_edit = QLineEdit("2026-2027")
        self.school_edit = QLineEdit()
        self.teacher_edit = QLineEdit()
        self.inspector_edit = QLineEdit()
        self.grade_edit = QLineEdit()
        for i, (label, widget) in enumerate([
            ("السنة الدراسية", self.year_edit), ("المؤسسة", self.school_edit),
            ("المعلم", self.teacher_edit), ("المتفقد", self.inspector_edit),
            ("الرتبة", self.grade_edit)
        ]):
            school_grid.addWidget(QLabel(label), 0, i)
            school_grid.addWidget(widget, 1, i)
        self.save_info_btn = QPushButton("حفظ البيانات")
        self.save_info_btn.setProperty("actionType", "save")
        self.save_info_btn.clicked.connect(self.save_school_info)
        school_grid.addWidget(self.save_info_btn, 1, 5)
        self._year_scoped_mutation_widgets.extend([
            self.year_edit, self.school_edit, self.teacher_edit,
            self.inspector_edit, self.grade_edit, self.save_info_btn,
        ])
        self.school_box.setVisible(False)

        agenda = QGroupBox("الأجندة — اختر اليوم الدراسي")
        agenda_row = QHBoxLayout(agenda)
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dddd، dd/MM/yyyy")
        self.date_edit.setLocale(QLocale(QLocale.Arabic, QLocale.Tunisia))
        self.date_edit.setMinimumWidth(190)

        self.custom_calendar = CustomCalendarWidget(self.db, self.date_edit)
        self.date_edit.setCalendarWidget(self.custom_calendar)
        self.date_edit.dateChanged.connect(self.load_day)
        agenda_row.addWidget(QLabel("اليوم:"))
        agenda_row.addWidget(self.date_edit)
        self.day_status = QLabel()
        agenda_row.addWidget(self.day_status, 1)
        self.prev_btn = QPushButton("◀ اليوم السابق")
        self.prev_btn.setProperty("actionType", "browse")
        self.view_day_btn = QPushButton("عرض اليوم")
        self.view_day_btn.setObjectName("viewDayButton")
        self.view_day_btn.setProperty("actionType", "browse")
        self.view_day_btn.setEnabled(False)
        self.view_day_btn.clicked.connect(self.open_day_lessons_view)
        self.next_btn = QPushButton("اليوم التالي ▶")
        self.next_btn.setProperty("actionType", "browse")
        self.prev_btn.clicked.connect(self.show_previous_recorded_workday)
        self.next_btn.clicked.connect(self.show_next_recorded_workday)
        agenda_row.addWidget(self.prev_btn)
        agenda_row.addWidget(self.view_day_btn)
        agenda_row.addWidget(self.next_btn)

        splitter = QSplitter(Qt.Vertical)

        lesson_box = QGroupBox("الحصص — اختر حصة أو أكثر لإدارة أنشطتها")
        lesson_layout = QVBoxLayout(lesson_box)
        lesson_buttons = QHBoxLayout()

        for label, callback, act_type in [
            ("＋ إضافة حصة", self.add_lesson, "add"),
            ("＋ إضافة كل الحصص", self.add_all_lessons, "add"),
            ("✎ تعديل الحصة", self.edit_lesson, "edit"),
            ("－ حذف الحصة", self.delete_lesson, "delete"),
            ("📋 نسخ الحصة", self.copy_selected_lessons, "copy_paste"),
            ("📌 لصق الحصة", self.paste_lessons, "copy_paste"),
            ("🗑 مسح حصص اليوم", self.clear_current_day_lessons, "clear"),
            ("⚠️ مسح الكل (كل الأيام)", self.clear_all_lessons_in_db, "clear")
        ]:
            b = QPushButton(label)
            b.clicked.connect(callback)
            b.setProperty("actionType", act_type)
            lesson_buttons.addWidget(b)
            if act_type in {"add", "edit", "delete", "copy_paste", "clear"}:
                self._year_scoped_mutation_widgets.append(b)
            if label == "＋ إضافة حصة":
                self.add_lesson_button = b
            elif label == "✎ تعديل الحصة":
                self.edit_lesson_button = b

        lesson_layout.addLayout(lesson_buttons)
        self.lesson_loading_label = QLabel()
        self.lesson_loading_label.setAlignment(Qt.AlignCenter)
        self.lesson_loading_label.setStyleSheet(
            "color: #1E3A8A; background-color: #EFF6FF; "
            "border: 1px solid #BFDBFE; border-radius: 8px; "
            "padding: 6px; font-weight: bold;"
        )
        self.lesson_loading_label.hide()
        lesson_layout.addWidget(self.lesson_loading_label)
        self.lesson_table = self.make_table(
            ["المعرّف", " الحصة", "القسم", "المجال ", "التوقيت",
             "المدة", " الوحدة", "نمط التعلم"]
        )
        self.lesson_table.setColumnHidden(0, True)
        self.lesson_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.lesson_table.itemSelectionChanged.connect(self.on_lesson_selected)
        lesson_layout.addWidget(self.lesson_table)
        splitter.addWidget(lesson_box)

        lower = QSplitter(Qt.Horizontal)
        splitter.addWidget(lower)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        act_box = QGroupBox("أنشطة الحصة المحددة")
        act_layout = QVBoxLayout(act_box)
        act_buttons = QHBoxLayout()

        for label, callback, act_type in [
            ("＋ إضافة نشاط", self.add_activity, "add"),
            ("✎ تعديل النشاط", self.edit_activity, "edit"),
            ("－ حذف النشاط", self.delete_activity, "delete"),
            ("🗑 مسح كل الأنشطة", self.clear_all_activities_for_lesson, "clear")
        ]:
            b = QPushButton(label)
            b.clicked.connect(callback)
            b.setProperty("actionType", act_type)
            act_buttons.addWidget(b)
            if act_type in {"add", "edit", "delete", "copy_paste", "clear"}:
                self._year_scoped_mutation_widgets.append(b)
            if label == "＋ إضافة نشاط":
                self.add_activity_button = b
                b.setEnabled(False)

        act_layout.addLayout(act_buttons)
        self.bulk_activity_details_button = QPushButton(
            "إضافة جميع الخصائص لجميع الأنشطة"
        )
        self.bulk_activity_details_button.setProperty("actionType", "add")
        self.bulk_activity_details_button.clicked.connect(
            self.add_all_activity_details
        )
        self.bulk_activity_details_button.setEnabled(False)
        act_layout.addWidget(self.bulk_activity_details_button)
        self._year_scoped_mutation_widgets.append(
            self.bulk_activity_details_button
        )
        self.activity_table = self.make_table(["المعرّف", "عنوان النشاط", "المدة"])
        self.activity_table.setColumnHidden(0, True)
        self.activity_table.itemSelectionChanged.connect(self.on_activity_selected)
        act_layout.addWidget(self.activity_table)

        self.activity_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.activity_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.activity_table.setColumnWidth(2, 90)

        lower.addWidget(act_box)

        detail_box = QGroupBox("خصائص النشاط المحدد (اسم الخاصية / قيمتها)")
        detail_layout = QVBoxLayout(detail_box)
        detail_buttons = QHBoxLayout()

        for label, callback, act_type in [
            ("＋ إضافة خاصية", self.add_detail, "add"),
            ("✎ تعديل الخاصية", self.edit_detail, "edit"),
            ("－ حذف الخاصية", self.delete_detail, "delete"),
            ("📋 نسخ الخصائص", self.copy_activity_details, "copy_paste"),
            ("📌 لصق الخصائص", self.paste_activity_details, "copy_paste"),
            ("🗑 مسح كل الخصائص", self.clear_all_details_for_activity, "clear")
        ]:
            b = QPushButton(label)
            b.clicked.connect(callback)
            b.setProperty("actionType", act_type)
            detail_buttons.addWidget(b)
            if act_type in {"add", "edit", "delete", "copy_paste", "clear"}:
                self._year_scoped_mutation_widgets.append(b)

        detail_layout.addLayout(detail_buttons)
        self.detail_table = self.make_table(["المعرّف", "اسم الخاصية", "القيمة"])
        self.detail_table.setColumnHidden(0, True)
        detail_layout.addWidget(self.detail_table)

        self.detail_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.detail_table.setColumnWidth(1, 150)
        self.detail_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        lower.addWidget(detail_box)
        lower.setSizes([600, 600])
        splitter.setSizes([370, 500])

        self.stats_page = QWidget()
        stats_page_layout = QVBoxLayout(self.stats_page)
        stats_page_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_scroll_area = QScrollArea()
        self.stats_scroll_area.setWidgetResizable(True)
        self.stats_content = QWidget()
        stats_layout = QVBoxLayout(self.stats_content)
        stats_layout.setContentsMargins(24, 20, 24, 24)
        stats_layout.setSpacing(18)
        self.stats_scroll_area.setWidget(self.stats_content)
        stats_page_layout.addWidget(self.stats_scroll_area)

        stats_header_layout = QHBoxLayout()
        stats_title = QLabel("لوحة الإحصائيات والتوقعات")
        stats_title.setStyleSheet(
            "font-size: 22px; font-weight: 800; color: #0F172A;"
        )
        self.refresh_stats_btn = QPushButton("تحديث التحليلات")
        self.refresh_stats_btn.setProperty("primary", "true")
        self.refresh_stats_btn.clicked.connect(
            lambda: self.load_statistics(force_refresh=True)
        )
        stats_header_layout.addWidget(stats_title)
        stats_header_layout.addStretch()
        stats_header_layout.addWidget(self.refresh_stats_btn)
        stats_layout.addLayout(stats_header_layout)

        self.stats_loading_label = QLabel()
        self.stats_loading_label.setObjectName("statsLoadingLabel")
        self.stats_loading_label.setAlignment(Qt.AlignCenter)
        self.stats_loading_label.hide()
        stats_layout.addWidget(self.stats_loading_label)

        cards_grid = QGridLayout()
        cards_grid.setHorizontalSpacing(12)
        cards_grid.setVerticalSpacing(12)
        self.stats_cards = {}
        card_definitions = [
            ("lessons", "إجمالي الحصص", "0", "#2563EB"),
            ("days", "الأيام المسجلة", "0", "#0F766E"),
            ("classes", "الأقسام", "0", "#7C3AED"),
            ("activities", "الأنشطة", "0", "#D97706"),
            ("properties", "الخصائص المسجلة", "0", "#DB2777"),
            ("completion_rate", "اكتمال الحصص", "0%", "#059669"),
            ("average_activities", "متوسط الأنشطة للحصة", "0", "#0891B2"),
            ("average_properties", "متوسط الخصائص للنشاط", "0", "#9333EA"),
            ("forecast_total", "توقع الحصص للأربعة أسابيع", "0", "#EA580C"),
        ]
        for index, (key, title, value, accent) in enumerate(card_definitions):
            card = QFrame()
            card.setObjectName("statisticsCard")
            card.setStyleSheet(
                "QFrame#statisticsCard { background: #FFFFFF; "
                "border: 1px solid #E2E8F0; border-top: 4px solid "
                f"{accent}; border-radius: 12px; }}"
            )
            card_layout = QVBoxLayout(card)
            card_title = QLabel(title)
            card_title.setStyleSheet(
                "color: #64748B; font-size: 11pt; background: transparent;"
            )
            value_label = QLabel(value)
            value_label.setStyleSheet(
                f"color: {accent}; font-size: 23pt; font-weight: 800; "
                "background: transparent;"
            )
            card_layout.addWidget(card_title)
            card_layout.addWidget(value_label)
            self.stats_cards[key] = value_label
            cards_grid.addWidget(card, index // 3, index % 3)
        stats_layout.addLayout(cards_grid)

        charts_grid = QGridLayout()
        charts_grid.setHorizontalSpacing(14)
        charts_grid.setVerticalSpacing(14)
        self.stats_charts = {}
        chart_definitions = [
            ("subjects", "الحصص حسب المجال"),
            ("modes", "أنماط التعلّم"),
            ("properties", "استخدام الخصائص حسب النشاط"),
            ("weekly", "نشاط الحصص والتوقع للأربعة أسابيع القادمة"),
        ]
        for index, (key, title) in enumerate(chart_definitions):
            chart_box = QGroupBox(title)
            chart_box.setObjectName("statisticsChartCard")
            chart_layout = QVBoxLayout(chart_box)
            figure, axis = plt.subplots(figsize=(6.2, 3.6))
            figure.patch.set_facecolor("#FFFFFF")
            canvas = FigureCanvas(figure)
            canvas.setMinimumHeight(310)
            chart_layout.addWidget(canvas)
            self.stats_charts[key] = (figure, axis, canvas)
            charts_grid.addWidget(chart_box, index // 2, index % 2)
        stats_layout.addLayout(charts_grid)

        tables_grid = QGridLayout()
        self.stats_table = self.make_table(
            ["المجال", "الحصص", "النسبة"]
        )
        self.stats_activity_table = self.make_table(
            ["النشاط", "عدد مرات التسجيل"]
        )
        self.stats_property_table = self.make_table(
            ["النشاط", "اسم الخاصية", "الاستخدام", "أنشطة مختلفة"]
        )
        for table in (
            self.stats_table, self.stats_activity_table,
            self.stats_property_table,
        ):
            table.setMinimumHeight(240)
        tables_grid.addWidget(self._statistics_table_group(
            "توزيع المجالات", self.stats_table
        ), 0, 0)
        tables_grid.addWidget(self._statistics_table_group(
            "الأنشطة الأكثر استخداماً", self.stats_activity_table
        ), 0, 1)
        tables_grid.addWidget(self._statistics_table_group(
            "استعمال أسماء الخصائص حسب الأنشطة", self.stats_property_table
        ), 1, 0, 1, 2)
        stats_layout.addLayout(tables_grid)
        self.stats_cache_label = QLabel()
        self.stats_cache_label.setStyleSheet("color: #64748B;")
        stats_layout.addWidget(self.stats_cache_label)
        self.stats_revision_timer = QTimer(self)
        self.stats_revision_timer.setInterval(3000)
        self.stats_revision_timer.timeout.connect(
            self._check_statistics_revision
        )
        self.stats_revision_timer.start()

        self.official_programs_page = OfficialProgramsWidget(self.db, self)
        self.official_programs_browse_page = OfficialProgramsBrowsePage(self.db, self)

        self.competences_cba_page = CompetencesCbaBrowsePage(self.db, self)
        self.day_lessons_page = DayLessonsBrowsePage(
            self.db, parent=self, on_back=lambda: self.pages_stack.setCurrentWidget(self.lesson_page)
        )

        self.pages_stack = QStackedWidget()

        self.lesson_page = QWidget()
        lesson_page_layout = QVBoxLayout(self.lesson_page)
        lesson_page_layout.addWidget(self.school_box)
        lesson_page_layout.addWidget(agenda)
        lesson_page_layout.addWidget(splitter, 1)

        self.pages_stack.addWidget(self.lesson_page)
        self.pages_stack.addWidget(self.stats_page)
        self.pages_stack.addWidget(self.official_programs_page)
        self.pages_stack.addWidget(self.official_programs_browse_page)
        self.pages_stack.addWidget(self.competences_cba_page)
        self.pages_stack.addWidget(self.day_lessons_page)

        self.plans_page = TeachingPlansPage(self.db, self)
        self.pages_stack.addWidget(self.plans_page)

        self.plan_view_page = TeachingPlanViewPage(self.db, self)
        self.pages_stack.addWidget(self.plan_view_page)

        self.official_holidays_page = OfficialHolidaysPage(self.db, self)
        self.pages_stack.addWidget(self.official_holidays_page)

        self.assessment_page = QWidget()
        assessment_layout = QVBoxLayout(self.assessment_page)
        assessment_layout.addWidget(QLabel("صفحة دفتر التقييم والمتابعة — أضف مكوناتها هنا."))
        self.pages_stack.addWidget(self.assessment_page)

        self._page_descriptions = {
            self.lesson_page: (
                "دفتر إعداد الدروس",
                "اختر يوماً لإضافة الحصص ومتابعة الأنشطة المسجلة.",
            ),
            self.stats_page: (
                "الإحصائيات",
                "ملخص استعمال الحصص والأنشطة والخصائص المسجلة.",
            ),
            self.official_programs_page: (
                "إدارة البرامج الرسمية",
                "إدارة البرامج والكفايات المعتمدة في المؤسسة.",
            ),
            self.official_programs_browse_page: (
                "تصفح البرامج الرسمية",
                "ابحث في البرامج الرسمية حسب المستوى والمجال.",
            ),
            self.competences_cba_page: (
                "الكفايات والمفاهيم",
                "تصفح المفاهيم المرتبطة بالمقاربة بالكفايات.",
            ),
            self.day_lessons_page: (
                "الأيام المسجلة",
                "استعرض دفتر إعداد الدروس حسب اليوم الدراسي.",
            ),
            self.plans_page: (
                "المخططات",
                "أنشئ المخططات التعليمية وتابع تقدم إنجازها.",
            ),
            self.plan_view_page: (
                "عرض المخطط",
                "تصفح محتوى المخطط بالتنسيق نفسه المستخدم في ملف PDF.",
            ),
            self.official_holidays_page: (
                "العطل الرسمية",
                "أدر فترات العطل المعتمدة في السنة الدراسية.",
            ),
            self.assessment_page: (
                "التقييم والمتابعة",
                "دفتر التقييم والمتابعة.",
            ),
        }
        page_header = QFrame()
        page_header.setObjectName("pageHeader")
        page_header_layout = QVBoxLayout(page_header)
        page_header_layout.setContentsMargins(16, 10, 16, 10)
        page_header_layout.setSpacing(2)
        self.page_title_label = QLabel()
        self.page_title_label.setObjectName("pageTitle")
        self.page_description_label = QLabel()
        self.page_description_label.setObjectName("pageDescription")
        page_header_layout.addWidget(self.page_title_label)
        page_header_layout.addWidget(self.page_description_label)
        root.addWidget(page_header)
        root.addWidget(self.pages_stack, 1)

        footer = QHBoxLayout()
        self.export_hint = QLabel("تُحفظ التغييرات مباشرة في قاعدة بيانات SQLite.")
        self.export_hint.setObjectName("saveHint")
        footer.addWidget(self.export_hint, 1)
        root.addLayout(footer)

        self.statusBar().showMessage(f"قاعدة البيانات: {DB_PATH}")
        toolbar = self.addToolBar("التنقل")
        toolbar.setObjectName("mainNavigationBar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        toolbar.setLayoutDirection(Qt.RightToLeft)
        toolbar.setToolTip("التنقل بين صفحات التطبيق")
        self._page_navigation_actions = []
        self.pages_stack.currentChanged.connect(self.update_page_navigation_actions)
        self.pages_stack.currentChanged.connect(self.update_page_header)

        lesson_book_menu = QMenu("دفتر إعداد الدروس", self)
        open_lesson_action = QAction("التصرف في الدفتر", self)
        open_lesson_action.setProperty("actionType", "browse")
        open_lesson_action.triggered.connect(lambda: self.pages_stack.setCurrentWidget(self.lesson_page))
        self.register_page_navigation_action(open_lesson_action, self.lesson_page)
        lesson_book_menu.addAction(open_lesson_action)
        view_day_action = QAction("عرض الأيام المسجلة بالدفتر", self)
        view_day_action.setProperty("actionType", "browse")
        view_day_action.triggered.connect(self.open_day_lessons_from_menu)
        self.register_page_navigation_action(view_day_action, self.day_lessons_page)
        lesson_book_menu.addAction(view_day_action)
        lesson_book_menu.addSeparator()

        toggle_action = QAction("بيانات المعلم", self)
        toggle_action.triggered.connect(self.toggle_enseig_info)
        self._year_scoped_mutation_actions.append(toggle_action)

        export_pdf_action = QAction("تصدير إلى ملف PDF", self)
        export_pdf_action.triggered.connect(self.export_pdf)

        export_json_action = QAction("تصدير يوم بصيغة JSON", self)
        export_json_action.triggered.connect(self.export_day_to_json)

        import_json_action = QAction("استيراد يوم من JSON", self)
        import_json_action.triggered.connect(self.import_day_from_json)
        self._year_scoped_mutation_actions.append(import_json_action)

        export_year_action = QAction("تصدير سنة دراسية كاملة (JSON)", self)
        export_year_action.triggered.connect(self.export_all_days_for_year)

        import_year_action = QAction("استيراد سنة دراسية كاملة (JSON)", self)
        import_year_action.triggered.connect(self.import_all_years_from_json)
        self._year_scoped_mutation_actions.append(import_year_action)

        self.timetable_action = QAction("جدول الأوقات", self)
        self.timetable_action.triggered.connect(self.open_timetable_dialog)

        lesson_book_menu.addAction(export_pdf_action)
        lesson_book_menu.addSeparator()
        lesson_book_menu.addAction(export_json_action)
        lesson_book_menu.addAction(import_json_action)
        lesson_book_menu.addSeparator()
        lesson_book_menu.addAction(export_year_action)
        lesson_book_menu.addAction(import_year_action)

        lesson_book_button = QToolButton(self)
        lesson_book_button.setText("دفتر إعداد الدروس")
        lesson_book_button.setMenu(lesson_book_menu)
        lesson_book_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(lesson_book_button)

        school_year_menu = QMenu("السنة الدراسية", self)
        school_year_menu.addAction(toggle_action)
        school_year_menu.addAction(self.timetable_action)
        official_holidays_action = QAction("إدارة العطل الرسمية", self)
        official_holidays_action.setProperty("actionType", "browse")
        official_holidays_action.triggered.connect(
            lambda: self.pages_stack.setCurrentWidget(
                self.official_holidays_page
            )
        )
        self.register_page_navigation_action(
            official_holidays_action, self.official_holidays_page
        )
        school_year_menu.addAction(official_holidays_action)
        self.add_school_year_action = QAction("إضافة سنة دراسية جديدة", self)
        self.add_school_year_action.triggered.connect(self.add_school_year)
        self._year_scoped_mutation_actions.append(self.add_school_year_action)
        self.previous_school_years_action = QAction(
            "عرض السنوات الدراسية السابقة", self
        )
        self.previous_school_years_action.triggered.connect(
            self.show_previous_school_years
        )
        school_year_menu.addSeparator()
        school_year_menu.addAction(self.previous_school_years_action)
        school_year_menu.addAction(self.add_school_year_action)
        school_year_menu.aboutToShow.connect(
            self.update_add_school_year_action_state
        )
        school_year_button = QToolButton(self)
        school_year_button.setText("السنة الدراسية")
        school_year_button.setMenu(school_year_menu)
        school_year_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(school_year_button)
        self.update_add_school_year_action_state()

        prog_menu = QMenu("البرامج الرسمية", self)
        browse_prog_action = QAction("تصفح البرامج الرسمية", self)
        browse_prog_action.setProperty("actionType", "browse")
        browse_prog_action.triggered.connect(self.open_official_programs_browser)
        self.register_page_navigation_action(
            browse_prog_action, self.official_programs_browse_page
        )
        prog_menu.addAction(browse_prog_action)
        open_prog_action = QAction("فتح إدارة البرامج الرسمية", self)
        open_prog_action.triggered.connect(lambda: self.pages_stack.setCurrentWidget(self.official_programs_page))
        self.register_page_navigation_action(open_prog_action, self.official_programs_page)
        prog_menu.addAction(open_prog_action)
        prog_menu.addSeparator()

        browse_cba_action = QAction("تصفح مفاهيم متصلة بالمقاربة بالكفايات", self)
        browse_cba_action.setProperty("actionType", "browse")
        browse_cba_action.triggered.connect(self.open_competences_cba_browser)
        self.register_page_navigation_action(browse_cba_action, self.competences_cba_page)
        prog_menu.addAction(browse_cba_action)

        prog_menu.addSeparator()

        self.export_prog_action = QAction("تصدير البرامج الرسمية كاملة (JSON)", self)
        self.export_prog_action.triggered.connect(self.export_programs_json)
        prog_menu.addAction(self.export_prog_action)

        self.import_prog_action = QAction("استيراد البرامج الرسمية من JSON", self)
        self.import_prog_action.triggered.connect(self.import_programs_json)
        prog_menu.addAction(self.import_prog_action)

        prog_menu.aboutToShow.connect(self.update_prog_actions_state)

        prog_button = QToolButton(self)
        prog_button.setText("البرامج الرسمية")
        prog_button.setMenu(prog_menu)
        prog_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(prog_button)

        toolbar.addSeparator()
        stats_action = QAction("فتح لوحة الإحصائيات", self)
        stats_action.setText("الإحصائيات")
        stats_action.setStatusTip("عرض الإحصائيات والتوقعات")
        stats_action.triggered.connect(self.open_statistics_page)
        self.register_page_navigation_action(stats_action, self.stats_page)
        stats_button = QToolButton(self)
        stats_button.setObjectName("statisticsNavigationButton")
        stats_button.setDefaultAction(stats_action)
        toolbar.addWidget(stats_button)

        plans_action = QAction("فتح المخططات", self)
        plans_action.setText("إدارة المخططات")
        plans_action.setStatusTip("عرض المخططات التعليمية")
        plans_action.triggered.connect(lambda: self.pages_stack.setCurrentWidget(self.plans_page))
        self.register_page_navigation_action(plans_action, self.plans_page)
        plans_button = QToolButton(self)
        plans_button.setObjectName("plansNavigationButton")
        plans_button.setText("المخططات")
        plans_menu = QMenu("المخططات", self)
        plans_menu.addAction(plans_action)
        view_plan_action = QAction("عرض المخطط", self)
        view_plan_action.setProperty("actionType", "browse")
        view_plan_action.setStatusTip("عرض المخطط بتنسيق PDF داخل التطبيق")
        view_plan_action.triggered.connect(
            lambda: self.pages_stack.setCurrentWidget(self.plan_view_page)
        )
        self.register_page_navigation_action(
            view_plan_action, self.plan_view_page
        )
        plans_menu.addAction(view_plan_action)
        plans_button.setMenu(plans_menu)
        plans_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        toolbar.addWidget(plans_button)

        assessment_action = QAction("فتح دفتر التقييم والمتابعة", self)
        assessment_action.setText("التقييم والمتابعة")
        assessment_action.setStatusTip("فتح دفتر التقييم والمتابعة")
        assessment_action.triggered.connect(lambda: self.pages_stack.setCurrentWidget(self.assessment_page))
        self.register_page_navigation_action(assessment_action, self.assessment_page)
        assessment_button = QToolButton(self)
        assessment_button.setObjectName("assessmentNavigationButton")
        assessment_button.setDefaultAction(assessment_action)
        toolbar.addWidget(assessment_button)

        toolbar.addSeparator()
        about_menu = QMenu("حول التطبيق", self)
        about_action = QAction("معلومات التطبيق", self)
        about_action.setProperty("actionType", "browse")
        about_action.triggered.connect(self.show_about_dialog)
        about_menu.addAction(about_action)
        about_button = QToolButton(self)
        about_button.setText("حول التطبيق")
        about_button.setMenu(about_menu)
        about_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(about_button)
        self.update_page_header(self.pages_stack.currentIndex())

    @staticmethod
    def _statistics_table_group(title, table):
        group = QGroupBox(title)
        layout = QVBoxLayout(group)
        layout.addWidget(table)
        return group

    def show_about_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("حول التطبيق")
        dialog.setObjectName("aboutApplicationDialog")
        dialog.setLayoutDirection(Qt.RightToLeft)
        dialog.setMinimumWidth(460)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setStyleSheet(
            "QFrame { background-color: #EFF6FF; border: 1px solid #BFDBFE; "
            "border-radius: 14px; }"
        )
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 18, 18, 18)
        title = QLabel("المعين للتخطيط اليومي للحصص")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "font-size: 19pt; font-weight: 800; color: #1E3A8A; "
            "background: transparent; border: none;"
        )
        description = QLabel("البرنامج المساعد التربوي للمعلم")
        description.setAlignment(Qt.AlignCenter)
        description.setStyleSheet(
            "font-size: 11pt; color: #475569; background: transparent; "
            "border: none;"
        )
        version = QLabel("الإصدار v 1.2")
        version.setAlignment(Qt.AlignCenter)
        version.setStyleSheet(
            "font-size: 10pt; font-weight: 700; color: #2563EB; "
            "background: transparent; border: none;"
        )
        header_layout.addWidget(title)
        header_layout.addWidget(description)
        header_layout.addWidget(version)
        layout.addWidget(header)

        developer = QLabel(
            "<b>المطور</b><br>بلال المزوغي"
        )
        phone = QLabel(
            '<b>الهاتف</b><br><a href="tel:20971271">20971271</a>'
        )
        email = QLabel(
            '<b>البريد الإلكتروني</b><br>'
            '<a href="mailto:mzbi9620@gmail.com">mzbi9620@gmail.com</a>'
        )
        for label in (developer, phone, email):
            label.setTextFormat(Qt.RichText)
            label.setTextInteractionFlags(Qt.TextBrowserInteraction)
            label.setOpenExternalLinks(True)
            label.setStyleSheet(
                "padding: 8px 12px; color: #334155; background: #FFFFFF; "
                "border: 1px solid #E2E8F0; border-radius: 8px;"
            )
            layout.addWidget(label)

        close_button = QPushButton("إغلاق")
        close_button.setProperty("actionType", "close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)

        if self.styleSheet():
            dialog.setStyleSheet(self.styleSheet())
        dialog.exec()

    def open_timetable_dialog(self):
        if getattr(self, "_timetable_thread", None) is not None:
            return
        self.timetable_action.setEnabled(False)
        self.statusBar().showMessage("في انتظار توليد البيانات ...")
        self._timetable_thread = QThread(self)
        self._timetable_worker = TimetableLoadWorker(
            self.db.path, self.db.year_id
        )
        self._timetable_worker.moveToThread(self._timetable_thread)
        self._timetable_thread.started.connect(
            self._timetable_worker.run, Qt.QueuedConnection
        )
        self._timetable_worker.result_ready.connect(
            self._open_timetable_with_schedule
        )
        self._timetable_worker.failed.connect(self._timetable_load_failed)
        self._timetable_worker.finished.connect(self._timetable_thread.quit)
        self._timetable_worker.finished.connect(
            self._timetable_worker.deleteLater
        )
        self._timetable_thread.finished.connect(
            self._timetable_thread_finished
        )
        self._timetable_thread.start()

    @Slot(object)
    def _open_timetable_with_schedule(self, schedule):
        self.statusBar().clearMessage()
        dlg = TimetableDialog(None, parent=self, schedule=schedule)
        dlg.setLayoutDirection(self.layoutDirection())
        if self.styleSheet():
            dlg.setStyleSheet(self.styleSheet())
        dlg.exec()

    @Slot(str)
    def _timetable_load_failed(self, message):
        self.statusBar().showMessage(message)
        QMessageBox.critical(self, "تعذر توليد جدول الأوقات", message)

    @Slot()
    def _timetable_thread_finished(self):
        thread = self._timetable_thread
        self._timetable_worker = None
        self._timetable_thread = None
        if thread is not None:
            thread.deleteLater()
        self.timetable_action.setEnabled(True)

    def open_official_programs_browser(self):
        self.official_programs_browse_page.load_levels()
        self.pages_stack.setCurrentWidget(self.official_programs_browse_page)

    def open_competences_cba_browser(self):
        self.pages_stack.setCurrentWidget(self.competences_cba_page)

    def register_page_navigation_action(self, action, page):
        self._page_navigation_actions.append((action, page))
        action.setEnabled(self.pages_stack.currentWidget() is not page)

    def open_statistics_page(self):
        self.pages_stack.setCurrentWidget(self.stats_page)
        self.load_statistics()

    def update_page_navigation_actions(self, current_index):
        current_page = self.pages_stack.widget(current_index)
        for action, page in self._page_navigation_actions:
            action.setEnabled(current_page is not page)

    def update_page_header(self, current_index):
        page = self.pages_stack.widget(current_index)
        title, description = self._page_descriptions.get(
            page, ("المعين للتخطيط اليومي للحصص", "")
        )
        self.page_title_label.setText(title)
        self.page_description_label.setText(description)

    def open_competence_manager(self):
        dlg = CompetenceManagerDialog(db=self.db, parent=self)
        dlg.setLayoutDirection(self.layoutDirection())
        if self.styleSheet():
            dlg.setStyleSheet(self.styleSheet())
        dlg.exec()

    def open_concept_manager(self):
        dlg = ConceptManagerDialog(db=self.db, parent=self)
        dlg.setLayoutDirection(self.layoutDirection())
        if self.styleSheet():
            dlg.setStyleSheet(self.styleSheet())
        dlg.exec()

    def load_statistics(self, force_refresh=False):
        if getattr(self, "_statistics_thread", None) is not None:
            return

        waiting_text = (
            "جارٍ تحديث التحليلات..."
            if force_refresh else "جارٍ تحميل التحليلات..."
        )
        self.stats_loading_label.setText(waiting_text)
        self.stats_loading_label.show()
        self.refresh_stats_btn.setEnabled(False)
        self.stats_cache_label.setText(waiting_text)

        self._statistics_thread = QThread(self)
        self._statistics_worker = StatisticsLoadWorker(
            self.db.path, force_refresh=force_refresh
        )
        self._statistics_worker.moveToThread(self._statistics_thread)
        self._statistics_thread.started.connect(
            self._statistics_worker.run, Qt.QueuedConnection
        )
        self._statistics_worker.result_ready.connect(self._display_statistics)
        self._statistics_worker.failed.connect(self._statistics_load_failed)
        self._statistics_worker.finished.connect(self._statistics_thread.quit)
        self._statistics_worker.finished.connect(
            self._statistics_worker.deleteLater
        )
        self._statistics_thread.finished.connect(
            self._statistics_thread_finished
        )
        self._statistics_thread.start()

    @Slot(object, int, bool)
    def _display_statistics(self, payload, revision, from_cache):
        self.stats_loading_label.hide()
        self._statistics_source_revision = revision
        metrics = payload["metrics"]
        self.stats_cards["lessons"].setText(
            f"{metrics['lessons']:,}"
        )
        self.stats_cards["days"].setText(f"{metrics['days']:,}")
        self.stats_cards["classes"].setText(f"{metrics['classes']:,}")
        self.stats_cards["activities"].setText(
            f"{metrics['activities']:,}"
        )
        self.stats_cards["properties"].setText(
            f"{metrics['properties']:,}"
        )
        self.stats_cards["completion_rate"].setText(
            f"{metrics['completion_rate']:.1f}%"
        )
        self.stats_cards["average_activities"].setText(
            str(metrics["average_activities"])
        )
        self.stats_cards["average_properties"].setText(
            str(metrics["average_properties"])
        )
        forecast_total = sum(
            item["lessons"] for item in payload["forecast"]
        )
        self.stats_cards["forecast_total"].setText(str(forecast_total))
        cache_state = "من قاعدة التحليلات المحلية" if from_cache else "تم تحديثها الآن"
        self.stats_cache_label.setText(
            f"{cache_state} — نسخة البيانات {revision} — "
            f"نسبة اكتمال الحصص {metrics['completion_rate']:.1f}%"
        )

        self._fill_statistics_table(
            self.stats_table,
            ["المجال", "الحصص", "النسبة"],
            [
                (
                    item["name"], item["count"],
                    f"{item['count'] / metrics['lessons'] * 100:.1f}%"
                    if metrics["lessons"] else "0%",
                )
                for item in payload["subjects"]
            ],
        )
        self._fill_statistics_table(
            self.stats_activity_table,
            ["النشاط", "عدد مرات التسجيل"],
            [(item["name"], item["count"]) for item in payload["activities"]],
        )
        self._fill_statistics_table(
            self.stats_property_table,
            ["النشاط", "اسم الخاصية", "الاستخدام", "أنشطة مختلفة"],
            [
                (
                    item["activity"], item["property"], item["uses"],
                    item["activities"],
                )
                for item in payload["property_usage"]
            ],
        )
        self._draw_statistics_charts(payload)

    @staticmethod
    def _fill_statistics_table(table, headers, rows):
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                table.setItem(row_index, column, item)
        table.resizeRowsToContents()

    def _draw_statistics_charts(self, payload):
        font = load_custom_font()
        plt.rcParams["font.family"] = "sans-serif"
        plt.rcParams["font.sans-serif"] = [
            font, "Noto Naskh Arabic", "Arial", "DejaVu Sans"
        ]
        palette = [
            "#2563EB", "#0F766E", "#7C3AED", "#D97706",
            "#DB2777", "#0891B2", "#65A30D", "#EA580C",
        ]

        figure, axis, canvas = self.stats_charts["subjects"]
        axis.clear()
        subjects = payload["subjects"][:10]
        if subjects:
            names = [item["name"] for item in reversed(subjects)]
            counts = [item["count"] for item in reversed(subjects)]
            axis.barh(names, counts, color=palette[:len(names)])
            axis.set_xlabel("عدد الحصص")
            axis.grid(axis="x", alpha=0.2)
        else:
            axis.text(0.5, 0.5, "لا توجد بيانات بعد", ha="center", va="center")
        axis.spines[["top", "right", "left"]].set_visible(False)
        figure.tight_layout()
        canvas.draw_idle()

        figure, axis, canvas = self.stats_charts["modes"]
        axis.clear()
        modes = payload["learning_modes"]
        if modes and sum(item["count"] for item in modes):
            axis.pie(
                [item["count"] for item in modes],
                labels=[item["name"] for item in modes],
                autopct="%1.0f%%", startangle=90,
                colors=palette[:len(modes)],
                wedgeprops={"width": 0.42, "edgecolor": "#FFFFFF"},
            )
            axis.axis("equal")
        else:
            axis.text(0.5, 0.5, "لا توجد بيانات بعد", ha="center", va="center")
        figure.tight_layout()
        canvas.draw_idle()

        figure, axis, canvas = self.stats_charts["properties"]
        axis.clear()
        property_usage = payload["property_usage"][:10]
        if property_usage:
            labels = [
                f"{item['activity']} — {item['property']}"
                for item in reversed(property_usage)
            ]
            counts = [
                item["uses"] for item in reversed(property_usage)
            ]
            axis.barh(labels, counts, color="#DB2777")
            axis.set_xlabel("عدد القيم المسجلة")
            axis.grid(axis="x", alpha=0.2)
            axis.tick_params(axis="y", labelsize=8)
        else:
            axis.text(0.5, 0.5, "لا توجد خصائص مسجلة", ha="center", va="center")
        axis.spines[["top", "right", "left"]].set_visible(False)
        figure.tight_layout()
        canvas.draw_idle()

        figure, axis, canvas = self.stats_charts["weekly"]
        axis.clear()
        weekly = payload["weekly"]
        forecast = payload["forecast"]
        if weekly:
            historical_labels = [item["week"] for item in weekly]
            historical_counts = [item["lessons"] for item in weekly]
            axis.plot(
                historical_labels, historical_counts,
                color="#2563EB", marker="o", linewidth=2,
                label="الحصص المسجلة",
            )
            if forecast:
                forecast_labels = [item["week"] for item in forecast]
                forecast_counts = [item["lessons"] for item in forecast]
                axis.plot(
                    [historical_labels[-1], *forecast_labels],
                    [historical_counts[-1], *forecast_counts],
                    color="#EA580C", marker="o", linestyle="--",
                    linewidth=2, label="التوقع",
                )
            axis.tick_params(axis="x", labelrotation=35, labelsize=8)
            axis.grid(axis="y", alpha=0.2)
            axis.legend(frameon=False)
        else:
            axis.text(0.5, 0.5, "لا توجد بيانات زمنية", ha="center", va="center")
        axis.spines[["top", "right"]].set_visible(False)
        figure.tight_layout()
        canvas.draw_idle()

    @Slot(str)
    def _statistics_load_failed(self, message):
        self.stats_loading_label.setText(message)
        self.stats_loading_label.show()
        self.stats_cache_label.setText("فشل تحديث التحليلات")
        QMessageBox.critical(self, "تعذر تحميل الإحصائيات", message)

    @Slot()
    def _statistics_thread_finished(self):
        thread = self._statistics_thread
        self._statistics_worker = None
        self._statistics_thread = None
        if thread is not None:
            thread.deleteLater()
        self.refresh_stats_btn.setEnabled(True)

    def _check_statistics_revision(self):
        if self.pages_stack.currentWidget() is not self.stats_page:
            return
        if getattr(self, "_statistics_thread", None) is not None:
            return
        try:
            revision = self.db.analytics_revision()
        except sqlite3.Error as exc:
            self.statusBar().showMessage(
                f"تعذر فحص تحديثات الإحصائيات: {exc}", 5000
            )
            return
        if revision != getattr(self, "_statistics_source_revision", None):
            self.load_statistics()

    def toggle_enseig_info(self):
        if not self.ensure_school_year_writable():
            return
        row = self.db.school_info()
        current_values = list(row) if row else [
            self.db.school_year_name_for_date(),
            "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", 0
        ]
        dialog_values = _school_info_to_dialog_values(current_values)
        dlg = EntryDialog(
            "بيانات المعلم والمؤسسة", SCHOOL_INFO_DIALOG_FIELDS,
            values=dialog_values, parent=self
        )
        if dlg.exec():
            values = _school_info_from_dialog_values(dlg.values())
            if not values[0]:
                QMessageBox.warning(self, "تنبيه", "يرجى إدخال السنة الدراسية.")
                return
            self.db.update_school_info(values)
            for w, val in zip([self.year_edit, self.school_edit, self.teacher_edit, self.inspector_edit, self.grade_edit], values):
                w.setText(val or "")
            self.statusBar().showMessage("تم حفظ بيانات المؤسسة والمعلم بنجاح.", 3000)

    def update_add_school_year_action_state(self):
        if not hasattr(self, "add_school_year_action"):
            return
        self.add_school_year_action.setEnabled(
            not self.is_school_year_read_only
            and self.db.year_id != self.db.current_school_year_id()
        )

    def ensure_school_year_writable(self):
        if not self.is_school_year_read_only:
            return True
        QMessageBox.information(
            self, "السنة الدراسية للعرض فقط",
            "السنة الدراسية المحددة سابقة؛ يمكنك تصفحها وتصدير بياناتها فقط."
        )
        return False

    def set_school_year_read_only(self, read_only):
        self.is_school_year_read_only = bool(read_only)
        for widget in self._year_scoped_mutation_widgets:
            widget.setEnabled(not self.is_school_year_read_only)
        for action in self._year_scoped_mutation_actions:
            action.setEnabled(not self.is_school_year_read_only)
        self.update_add_school_year_action_state()
        self._update_add_activity_button()
        self._update_bulk_activity_details_button()

    def show_previous_school_years(self):
        if getattr(self, "_lesson_history_thread", None) is not None:
            return
        dlg = PreviousSchoolYearsDialog(
            self.db, selected_year_id=self.db.year_id, parent=self
        )
        if not dlg.exec():
            return
        year_id, year_name = dlg.selected_year()
        if not year_id:
            QMessageBox.warning(self, "السنة الدراسية", "لم يتم اختيار سنة دراسية.")
            return

        workdays = self.db.recorded_workdays(year_id)
        if workdays:
            day_id, day_text = workdays[0]
        else:
            try:
                start_year = int(str(year_name).split("-", 1)[0])
            except (TypeError, ValueError):
                QMessageBox.warning(
                    self, "السنة الدراسية غير صالحة",
                    f"تعذر تحديد تاريخ بداية السنة الدراسية: {year_name}"
                )
                return
            day_id = None
            day_text = f"{start_year:04d}-09-01"

        selected_date = QDate.fromString(day_text, "yyyy-MM-dd")
        if not selected_date.isValid():
            QMessageBox.warning(
                self, "تاريخ السنة الدراسية غير صالح",
                f"تعذر عرض التاريخ: {day_text}"
            )
            return

        self.db.year_id = year_id
        self.set_school_year_read_only(
            year_id != self.db.current_school_year_id()
        )
        self.load_school_info()
        self.day_lessons_page.calendar.year_id = year_id
        self.day_lessons_page.calendar.refresh_recorded_dates()
        self.date_edit.setDate(selected_date)
        self.load_day()
        self.day_lessons_page.set_day(day_id, day_text)
        self.pages_stack.setCurrentWidget(self.day_lessons_page)
        self.statusBar().showMessage(
            f"السنة الدراسية {year_name}"
            + (" — للعرض والتصدير فقط" if self.is_school_year_read_only else ""),
            5000,
        )

    def add_school_year(self):
        if not self.ensure_school_year_writable():
            return
        new_year_name = self.db.school_year_name_for_date()
        school_info = self.db.school_info()
        if not school_info:
            QMessageBox.warning(
                self, "تعذر إضافة السنة الدراسية",
                "لم يتم العثور على بيانات السنة الدراسية الحالية."
            )
            return
        if str(school_info[0] or "").strip() == new_year_name:
            self.update_add_school_year_action_state()
            return

        dialog_values = _school_info_to_dialog_values(list(school_info))
        dialog_values[0] = new_year_name
        dlg = EntryDialog(
            "إضافة سنة دراسية جديدة", SCHOOL_INFO_DIALOG_FIELDS,
            values=dialog_values, parent=self
        )
        dlg.widgets[0].setReadOnly(True)
        if not dlg.exec():
            return

        values = _school_info_from_dialog_values(dlg.values())
        values[0] = new_year_name
        try:
            new_year_id = self.db.create_school_year(values)
        except ValueError as exc:
            QMessageBox.warning(self, "تعذر إضافة السنة الدراسية", str(exc))
            self.update_add_school_year_action_state()
            return
        except sqlite3.Error as exc:
            QMessageBox.critical(
                self, "تعذر إضافة السنة الدراسية",
                f"حدث خطأ أثناء حفظ بيانات السنة الجديدة:\n{exc}"
            )
            return

        self.db.year_id = new_year_id
        for widget, value in zip(
            [self.year_edit, self.school_edit, self.teacher_edit,
             self.inspector_edit, self.grade_edit],
            [values[0], values[1], values[2], values[3], values[4]],
        ):
            widget.setText(str(value or ""))

        start_year = int(new_year_name.split("-", 1)[0])
        first_school_day = QDate(start_year, 9, 1)
        self.date_edit.setDate(first_school_day)
        self.load_day()
        self.day_lessons_page.set_day(
            None, first_school_day.toString("yyyy-MM-dd")
        )
        self.pages_stack.setCurrentWidget(self.lesson_page)
        self.update_add_school_year_action_state()
        self.statusBar().showMessage(
            f"تمت إضافة السنة الدراسية {new_year_name} مع الإبقاء على بيانات السنة السابقة.",
            5000,
        )
        QMessageBox.information(
            self, "تمت إضافة السنة الدراسية",
            f"أُضيفت السنة الدراسية {new_year_name}، "
            "ونُسخت إليها بيانات المعلم دون تغيير بيانات السنة السابقة."
        )

    def make_table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setMinimumHeight(120)
        return table

    def load_school_info(self):
        row = self.db.school_info()
        if row:
            for w, value in zip(
                [self.year_edit, self.school_edit, self.teacher_edit, self.inspector_edit, self.grade_edit], row
            ):
                w.setText(value or "")

    def save_school_info(self):
        if not self.ensure_school_year_writable():
            return
        values = list(self.db.school_info() or [
            "2026-2027", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", 0
        ])
        values[:5] = [
            self.year_edit.text().strip(),
            self.school_edit.text().strip(),
            self.teacher_edit.text().strip(),
            self.inspector_edit.text().strip(),
            self.grade_edit.text().strip()
        ]
        if not values[0]:
            QMessageBox.warning(self, "تنبيه", "يرجى إدخال السنة الدراسية.")
            return
        self.db.update_school_info(values)
        self.statusBar().showMessage("تم حفظ بيانات المؤسسة.", 3000)
        self.school_box.setVisible(False)

    def load_day(self, *_):
        qd = self.date_edit.date()
        day = qd.toString("yyyy-MM-dd")
        if self.is_school_year_read_only:
            self.day_id = self.db.recorded_day_id(day)
        else:
            self.day_id = self.db.get_day_id(day)
        self.selected_lesson_id = None
        self.selected_activity_id = None

        if hasattr(self, 'custom_calendar'):
            self.custom_calendar.refresh_recorded_dates()
            self.custom_calendar.update()
        
        self.activity_table.setRowCount(0)
        self.detail_table.setRowCount(0)
        self.refresh_lessons()
        self.day_status.setText(f"التاريخ: {day} — عدد الحصص: {self.lesson_table.rowCount()}")

    def fill_table(self, table, rows):
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                for column, value in enumerate(row):
                    item = QTableWidgetItem(
                        "" if value is None else str(value)
                    )
                    if column == 0:
                        item.setData(Qt.UserRole, value)
                    table.setItem(row_index, column, item)
            table.resizeRowsToContents()
        finally:
            table.setUpdatesEnabled(True)
            table.viewport().update()

    def current_id(self, table):
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        return int(item.text()) if item else None

    def refresh_lessons(self, select_id=None):
        rows = self.db.lessons(self.day_id)
        self.fill_table(self.lesson_table, rows)
        self._refresh_lesson_completion_colors()
        self._update_workday_navigation_buttons()
        self.view_day_btn.setEnabled(bool(rows))
        self.day_status.setText(f"التاريخ: {self.date_edit.date().toString('yyyy-MM-dd')} — عدد الحصص: {len(rows)}")
        
        if select_id:
            for r in range(self.lesson_table.rowCount()):
                if int(self.lesson_table.item(r, 0).text()) == select_id:
                    self.lesson_table.setCurrentCell(r, 0)
                    break
        elif self.lesson_table.rowCount() > 0:
            self.lesson_table.setCurrentCell(0, 0)
        self._update_add_activity_button()

    def _update_workday_navigation_buttons(self):
        selected_day = self.date_edit.date().toString("yyyy-MM-dd")
        recorded_days = [
            day for _, day in self.db.recorded_workdays()
            if day
        ]
        self.prev_btn.setEnabled(any(day < selected_day for day in recorded_days))
        self.next_btn.setEnabled(any(day > selected_day for day in recorded_days))

    def _navigate_recorded_workday(self, direction):
        selected_day = self.date_edit.date().toString("yyyy-MM-dd")
        recorded_days = sorted(
            day for _, day in self.db.recorded_workdays() if day
        )
        if direction < 0:
            candidates = [day for day in recorded_days if day < selected_day]
            target_day = candidates[-1] if candidates else None
        else:
            candidates = [day for day in recorded_days if day > selected_day]
            target_day = candidates[0] if candidates else None

        if target_day is not None:
            self.date_edit.setDate(QDate.fromString(target_day, "yyyy-MM-dd"))
        self._update_workday_navigation_buttons()

    def show_previous_recorded_workday(self):
        self._navigate_recorded_workday(-1)

    def show_next_recorded_workday(self):
        self._navigate_recorded_workday(1)

    def open_day_lessons_view(self):
        try:
            if not self.day_id or not self.db.lessons(self.day_id):
                self.view_day_btn.setEnabled(False)
                return

            day_text = self.date_edit.date().toString("yyyy-MM-dd")
            self.day_lessons_page.set_day(self.day_id, day_text)
            self.pages_stack.setCurrentWidget(self.day_lessons_page)
        except (sqlite3.Error, RuntimeError) as exc:
            QMessageBox.critical(
                self, "تعذر عرض حصص اليوم",
                f"حدث خطأ أثناء تحميل بيانات اليوم:\n{exc}"
            )

    def open_day_lessons_from_menu(self):
        day_text = self.date_edit.date().toString("yyyy-MM-dd")
        try:
            day_id = self.db.recorded_day_id(day_text)
            self.day_lessons_page.set_day(day_id, day_text)
            self.pages_stack.setCurrentWidget(self.day_lessons_page)
        except (sqlite3.Error, RuntimeError) as exc:
            QMessageBox.critical(
                self, "تعذر عرض حصص اليوم",
                f"حدث خطأ أثناء تحميل بيانات اليوم:\n{exc}"
            )

    def refresh_activities(self, select_id=None):
        if not self.selected_lesson_id:
            self.activity_table.setRowCount(0)
            self._update_add_activity_button()
            self._refresh_lesson_completion_colors()
            return
        activities = self.db.activities(self.selected_lesson_id)
        self.fill_table(self.activity_table, activities)
        self._refresh_activity_completion_colors(activities)
        self._refresh_lesson_completion_colors()
        self._update_add_activity_button()
        
        if select_id:
            for r in range(self.activity_table.rowCount()):
                if int(self.activity_table.item(r, 0).text()) == select_id:
                    self.activity_table.setCurrentCell(r, 0)
                    break
        elif self.activity_table.rowCount() > 0:
            self.activity_table.setCurrentCell(0, 0)

    @staticmethod
    def _duration_minutes(value):
        return duration_minutes(value)

    @staticmethod
    def _required_activity_properties(learning_mode):
        return required_activity_properties(learning_mode)

    def _activity_completion_issues(self, details, learning_mode):
        return activity_completion_issues(details, learning_mode)

    @staticmethod
    def _set_completion_row_style(table, row, issues, item_type):
        if issues:
            background = QColor("#FEE2E2")
            foreground = QColor("#991B1B")
            state_text = "غير مكتملة"
            tooltip = f"{state_text}: " + "؛ ".join(issues)
        else:
            background = QColor("#DCFCE7")
            foreground = QColor("#166534")
            state_text = "مكتملة"
            tooltip = f"{item_type} {state_text} وفق الشروط المطلوبة."

        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                item.setBackground(background)
                item.setForeground(foreground)
                item.setToolTip(tooltip)

    def _refresh_activity_completion_colors(self, activities=None):
        if not self.selected_lesson_id:
            return
        if activities is None:
            activities = self.db.activities(self.selected_lesson_id)
        completion_data = self.db.activity_completion_data(
            (self.selected_lesson_id,)
        ).get(self.selected_lesson_id, {})
        learning_mode = completion_data.get("learning_mode", "")
        details_by_id = completion_data.get("activities", {})
        issues_by_id = {
            activity[0]: self._activity_completion_issues(
                details_by_id.get(activity[0], {}).get("details", []),
                learning_mode,
            )
            for activity in activities
        }
        for row in range(self.activity_table.rowCount()):
            item = self.activity_table.item(row, 0)
            if item is None:
                continue
            activity_id = int(item.text())
            self._set_completion_row_style(
                self.activity_table, row, issues_by_id.get(activity_id, []),
                "النشاط",
            )

    def _refresh_lesson_completion_colors(self):
        lesson_ids = [
            int(self.lesson_table.item(row, 0).text())
            for row in range(self.lesson_table.rowCount())
            if self.lesson_table.item(row, 0) is not None
        ]
        completion_data = self.db.activity_completion_data(lesson_ids)
        for row in range(self.lesson_table.rowCount()):
            id_item = self.lesson_table.item(row, 0)
            if id_item is None:
                continue
            lesson_id = int(id_item.text())
            lesson = completion_data.get(lesson_id, {})
            self._set_completion_row_style(
                self.lesson_table, row, lesson_completion_issues(lesson),
                "الحصة"
            )

    def _activity_time_budget(self, exclude_activity_id=None):
        if not self.selected_lesson_id:
            return None, None

        lesson = self.db.lesson(self.selected_lesson_id)
        lesson_minutes = self._duration_minutes(lesson[4]) if lesson else None
        if lesson_minutes is None:
            return None, None

        activity_minutes = 0
        for activity in self.db.activities(self.selected_lesson_id):
            if activity[0] == exclude_activity_id:
                continue
            minutes = self._duration_minutes(activity[2])
            if minutes is None:
                return lesson_minutes, None
            activity_minutes += minutes
        return lesson_minutes, activity_minutes

    def _update_add_activity_button(self):
        if self.is_school_year_read_only:
            self.add_activity_button.setEnabled(False)
            self.add_activity_button.setToolTip(
                "السنة الدراسية السابقة متاحة للتصفح والتصدير فقط."
            )
            return
        if not self.selected_lesson_id:
            self.add_activity_button.setEnabled(False)
            self.add_activity_button.setToolTip("اختر حصة لإضافة نشاط.")
            return

        lesson_minutes, activity_minutes = self._activity_time_budget()
        if lesson_minutes is None:
            self.add_activity_button.setEnabled(False)
            self.add_activity_button.setToolTip(
                "حدد مدة الحصة بالدقائق قبل إضافة الأنشطة."
            )
        elif activity_minutes is None:
            self.add_activity_button.setEnabled(False)
            self.add_activity_button.setToolTip(
                "تعذر حساب المدد؛ صحح مدد الأنشطة لتكون بالدقائق."
            )
        elif activity_minutes >= lesson_minutes:
            self.add_activity_button.setEnabled(False)
            self.add_activity_button.setToolTip(
                "اكتمل الوقت المخصص للحصة؛ عدّل مدة الحصة أو مدد أنشطتها."
            )
        else:
            self.add_activity_button.setEnabled(True)
            self.add_activity_button.setToolTip("")

    def on_lesson_selected(self):
        self.selected_lesson_id = self.current_id(self.lesson_table)
        self.selected_activity_id = None
        self.detail_table.setRowCount(0)
        self.refresh_activities()
        self._update_bulk_activity_details_button()

    def _update_bulk_activity_details_button(self):
        if not hasattr(self, "bulk_activity_details_button"):
            return
        self.bulk_activity_details_button.setEnabled(
            bool(self.selected_lesson_id) and not self.is_school_year_read_only
        )

    def on_activity_selected(self):
        self.selected_activity_id = self.current_id(self.activity_table)
        self.refresh_details()

    def refresh_details(self, select_id=None):
        if not self.selected_activity_id:
            self.detail_table.setRowCount(0)
            self._refresh_completion_colors()
            return
        self.fill_table(self.detail_table, self.db.details(self.selected_activity_id))
        if select_id:
            for r in range(self.detail_table.rowCount()):
                if int(self.detail_table.item(r, 0).text()) == select_id:
                    self.detail_table.selectRow(r)
                    break
        self._refresh_completion_colors()

    def _refresh_completion_colors(self):
        self._refresh_activity_completion_colors()
        self._refresh_lesson_completion_colors()

    def add_lesson(self):
        if not self.ensure_school_year_writable():
            return
        if getattr(self, "_lesson_history_thread", None) is not None:
            return

        self._start_lesson_history_load(
            self._open_lesson_entry_dialog,
            self.lesson_table.rowCount(),
        )

    def _start_lesson_history_load(
        self, result_handler, existing_count, generate_defaults=True
    ):
        if getattr(self, "_lesson_history_thread", None) is not None:
            return
        waiting_text = "في انتظار توليد البيانات ..."
        self.lesson_loading_label.setText(waiting_text)
        self.lesson_loading_label.show()
        self.add_lesson_button.setEnabled(False)
        self.edit_lesson_button.setEnabled(False)
        self.date_edit.setEnabled(False)

        self._lesson_history_thread = QThread(self)
        self._lesson_history_worker = LessonHistoryLoadWorker(
            self.db.path,
            self.date_edit.date().dayOfWeek() - 1,
            existing_count,
            generate_defaults,
        )
        self._lesson_history_worker.moveToThread(self._lesson_history_thread)
        self._lesson_history_thread.started.connect(
            self._lesson_history_worker.run, Qt.QueuedConnection
        )
        self._lesson_history_worker.result_ready.connect(result_handler)
        self._lesson_history_worker.failed.connect(self._lesson_history_failed)
        self._lesson_history_worker.finished.connect(
            self._lesson_history_thread.quit
        )
        self._lesson_history_worker.finished.connect(
            self._lesson_history_worker.deleteLater
        )
        self._lesson_history_thread.finished.connect(
            self._lesson_history_thread_finished
        )
        self._lesson_history_thread.start()

    @Slot(object, object)
    def _open_lesson_entry_dialog(self, df_history, predicted):
        if not self.ensure_school_year_writable():
            return
        def_hessa_num = max(1, self.lesson_table.rowCount() + 1)
        default_vals = ["", "", "08:00-09:00", "60 دقيقة", 1, def_hessa_num, "منهجي"]
        if predicted:
            default_vals[0] = predicted.get("classe", "")
            default_vals[1] = predicted.get("majal", "")
            default_vals[2] = predicted.get("heure", "08:00-09:00")
            default_vals[3] = predicted.get("time", "60 دقيقة")
            default_vals[4] = predicted.get("wehda", 1)
            default_vals[5] = def_hessa_num
            default_vals[6] = predicted.get("namat", "منهجي")

        dlg = LessonEntryDialog(
            "إضافة حصة", df_history, default_values=default_vals,
            parent=self, db=self.db,
        )
        if dlg.exec():
            v = dlg.values()
            if not v[0] or not v[1]:
                QMessageBox.warning(self, "بيانات ناقصة", "يرجى إدخال القسم والمجال / المادة.")
                return
            try:
                new_id = self.db.save_lesson(v, self.day_id)
            except (ValueError, sqlite3.Error) as exc:
                QMessageBox.critical(
                    self, "تعذر حفظ الحصة", f"حدث خطأ أثناء حفظ الحصة:\n{exc}"
                )
                return
            self.refresh_lessons(new_id)

    @Slot(object, object)
    def _open_lesson_edit_dialog(self, df_history, _predicted):
        if not self.ensure_school_year_writable():
            return
        lesson_id, current_values = self._pending_lesson_edit
        dlg = LessonEntryDialog(
            "تعديل حصة", df_history, default_values=current_values,
            parent=self, db=self.db,
        )
        if dlg.exec():
            values = dlg.values()
            if not values[0] or not values[1]:
                QMessageBox.warning(
                    self, "بيانات ناقصة", "القسم والمادة مطلوبان."
                )
                return
            try:
                self.db.save_lesson(values, self.day_id, lesson_id)
            except (ValueError, sqlite3.Error) as exc:
                QMessageBox.critical(
                    self, "تعذر حفظ الحصة", f"حدث خطأ أثناء حفظ الحصة:\n{exc}"
                )
                return
            self.refresh_lessons(lesson_id)

    @Slot(str)
    def _lesson_history_failed(self, message):
        self.lesson_loading_label.setText(message)
        self.lesson_loading_label.show()
        QMessageBox.critical(self, "تعذر تحميل الحصص السابقة", message)

    @Slot()
    def _lesson_history_thread_finished(self):
        thread = self._lesson_history_thread
        self._lesson_history_worker = None
        self._lesson_history_thread = None
        if thread is not None:
            thread.deleteLater()
        self.add_lesson_button.setEnabled(not self.is_school_year_read_only)
        self.edit_lesson_button.setEnabled(not self.is_school_year_read_only)
        self.date_edit.setEnabled(True)
        if not self.lesson_loading_label.text().startswith("تعذر"):
            self.lesson_loading_label.hide()

    def add_all_lessons(self):
        if not self.ensure_school_year_writable():
            return
        target_day = self.date_edit.date().toString("yyyy-MM-dd")
        weekday_num = self.date_edit.date().dayOfWeek() - 1
        try:
            lessons = self.db.lessons_for_weekday(weekday_num, target_day)
        except sqlite3.Error as e:
            QMessageBox.critical(self, "خطأ", f"تعذر تحميل حصص هذا اليوم من قاعدة البيانات: {e}")
            return

        if not lessons:
            QMessageBox.information(
                self, "لا توجد حصص مسجلة",
                "لم يتم العثور على حصص مسجلة في قاعدة البيانات ليوم مماثل من أيام الأسبوع."
            )
            return

        existing_keys = {
            (str(row[2] or "").strip(), str(row[3] or "").strip(),
             str(row[4] or "").strip(), str(row[5] or "").strip())
            for row in self.db.lessons(self.day_id)
        }
        seen_keys = set()
        new_lessons = []
        for lesson in lessons:
            key = tuple(str(value or "").strip() for value in lesson)
            if key not in existing_keys and key not in seen_keys:
                new_lessons.append(lesson)
                seen_keys.add(key)

        if not new_lessons:
            QMessageBox.information(
                self, "الحصص موجودة",
                "جميع الحصص المسجلة لهذا اليوم مضافة بالفعل."
            )
            return

        number_defaults = {}
        try:
            for classe, majal, _, _ in new_lessons:
                key = (str(classe or "").strip(), str(majal or "").strip())
                if key not in number_defaults:
                    number_defaults[key] = self.db.lesson_number_defaults(*key)
        except sqlite3.Error as e:
            QMessageBox.critical(
                self, "خطأ",
                f"تعذر تحميل أرقام الوحدات والحصص المسجلة: {e}"
            )
            return

        dlg = BulkLessonEntryDialog(new_lessons, number_defaults, self)
        if not dlg.exec():
            return

        values = dlg.values()
        if any(not lesson[0] or not lesson[1] for lesson in values):
            QMessageBox.warning(self, "بيانات ناقصة", "تعذر إضافة حصة لا تحتوي على القسم والمجال.")
            return

        try:
            self.db.save_lessons(values, self.day_id)
        except sqlite3.Error as e:
            QMessageBox.critical(self, "خطأ", f"حدث خطأ أثناء إضافة الحصص: {e}")
            return

        self.refresh_lessons()
        self.statusBar().showMessage(f"تمت إضافة {len(values)} حصة بنجاح.", 3000)

    def edit_lesson(self):
        if not self.ensure_school_year_writable():
            return
        if getattr(self, "_lesson_history_thread", None) is not None:
            return
        lid = self.current_id(self.lesson_table)
        if not lid:
            return self.warn_select("حصة")
        try:
            row = self.db.lesson(lid)
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "تعذر تحميل الحصة", str(exc))
            return
        if row is None:
            QMessageBox.warning(self, "تعذر تحميل الحصة", "لم تعد الحصة موجودة.")
            return
        current_values = [
            row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[9]
        ]
        self._pending_lesson_edit = (lid, current_values)
        self._start_lesson_history_load(
            self._open_lesson_edit_dialog,
            self.lesson_table.rowCount(),
            generate_defaults=False,
        )

    def delete_lesson(self):
        if not self.ensure_school_year_writable():
            return
        lid = self.current_id(self.lesson_table)
        if not lid:
            return self.warn_select("حصة")
        if self.confirm("حذف الحصة المحددة وكل أنشطتها وخصائصها؟"):
            self.db.delete_lesson(lid)
            self.selected_lesson_id = None
            self.refresh_lessons()
            self.activity_table.setRowCount(0); self.detail_table.setRowCount(0)

    def copy_selected_lessons(self):
        selected_rows = self.lesson_table.selectionModel().selectedRows()
        if not selected_rows:
            return self.warn_select("حصة واحدة على الأقل لنسخها")
        
        self.copied_lessons = []
        for index in selected_rows:
            row_idx = index.row()
            item = self.lesson_table.item(row_idx, 0)
            if not item:
                continue
            lesson_id = int(item.text())
            lesson_row = self.db.lesson(lesson_id)
            if not lesson_row:
                continue
            
            lesson_data = {
                "classe": lesson_row[1],
                "classe_level_id": lesson_row[9],
                "majal": lesson_row[2],
                "heure": lesson_row[3],
                "time": lesson_row[4],
                "wehda_number": lesson_row[5],
                "hessa_number": lesson_row[6],
                "namat_taallom": lesson_row[7],
                "activities": []
            }
            
            for act in self.db.activities(lesson_id):
                act_id, act_title, act_time = act[0], act[1], act[2]
                details = self.db.details(act_id)
                act_data = {
                    "title": act_title,
                    "time": act_time,
                    "details": [(d[1], d[2]) for d in details]
                }
                lesson_data["activities"].append(act_data)
                
            self.copied_lessons.append(lesson_data)
        self.statusBar().showMessage(f"تم نسخ {len(self.copied_lessons)} حصة بنجاح.", 3000)

    def paste_lessons(self):
        if not self.ensure_school_year_writable():
            return
        if not self.copied_lessons:
            QMessageBox.warning(self, "تنبيه", "لا توجد حصص منسوخة حالياً.")
            return
        if not self.day_id:
            QMessageBox.warning(self, "تنبيه", "يرجى تحديد اليوم المراد لصق الحصص فيه.")
            return
            
        try:
            with self.db.connect() as con:
                cursor = con.cursor()
                for les in self.copied_lessons:
                    classe_id = self.db.resolve_class_id(
                        con, les["classe"], les["classe_level_id"]
                    )
                    majal_id, learning_mode_id = (
                        self.db.resolve_lesson_lookup_ids(
                            con, les["majal"], les["namat_taallom"]
                        )
                    )
                    cursor.execute("""
                        INSERT INTO hessa(
                            classe_id, majal_id, heure, time, wehda_number,
                            hessa_number, learning_mode_id, day_data_id
                        )
                        VALUES(?,?,?,?,?,?,?,?)
                    """, (
                        classe_id, majal_id, les["heure"], les["time"],
                        les["wehda_number"], les["hessa_number"],
                        learning_mode_id, self.day_id
                    ))
                    new_hessa_id = cursor.lastrowid
                    
                    for act in les["activities"]:
                        cursor.execute("""
                            INSERT INTO nachat(title, time, hessa_id) VALUES(?,?,?)
                        """, (act["title"], act["time"], new_hessa_id))
                        new_nachat_id = cursor.lastrowid
                        
                        for detail in act["details"]:
                            cursor.execute("""
                                INSERT INTO nachat_detail(key_name, value_text, nachat_id) VALUES(?,?,?)
                            """, (detail[0], detail[1], new_nachat_id))
                con.commit()
                
            self.refresh_lessons()
            self.statusBar().showMessage("تم لصق الحصص المنسوخة بنجاح.", 3000)
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"حدث خطأ أثناء لصق الحصص: {e}")

    def _current_lesson_context(self):
        if not self.selected_lesson_id:
            return "", "", None
        row = self.db.lesson(self.selected_lesson_id)
        if not row:
            return "", "", None
        return str(row[2] or ""), str(row[7] or ""), row[8]

    def add_activity(self):
        if not self.ensure_school_year_writable():
            return
        if not self.selected_lesson_id:
            return self.warn_select("حصة أولاً")
        majal, _namat, _day_data_id = self._current_lesson_context()
        weekday = self.date_edit.date().dayOfWeek() - 1
        dlg = ActivityEntryDialog("إضافة نشاط", majal, self.db, weekday, parent=self)
        while dlg.exec():
            title, duration = dlg.values()
            if not title:
                return QMessageBox.warning(self, "بيانات ناقصة", "عنوان النشاط مطلوب.")
            duration_minutes = self._duration_minutes(duration)
            if duration_minutes is None:
                return QMessageBox.warning(
                    self, "مدة غير صالحة", "يرجى تحديد مدة النشاط بالدقائق."
                )
            lesson_minutes, activity_minutes = self._activity_time_budget()
            if lesson_minutes is None:
                return QMessageBox.warning(
                    self, "مدة الحصة غير صالحة",
                    "حدد مدة الحصة بالدقائق قبل إضافة نشاط."
                )
            if activity_minutes is None:
                return QMessageBox.warning(
                    self, "مدد أنشطة غير صالحة",
                    "تعذر حساب مجموع مدد الأنشطة؛ صحح مددها لتكون بالدقائق."
                )
            if activity_minutes + duration_minutes > lesson_minutes:
                remaining_minutes = max(lesson_minutes - activity_minutes, 0)
                QMessageBox.warning(
                    self, "تجاوز مدة الحصة",
                    f"مجموع مدد الأنشطة سيتجاوز مدة الحصة "
                    f"({lesson_minutes} دقيقة). غيّر مدة هذا النشاط إلى "
                    f"{remaining_minutes} دقيقة أو أقل."
                )
                dlg.time_combo.setFocus()
                continue
            aid = self.db.save_activity(title, duration, self.selected_lesson_id)
            self.refresh_activities(aid)
            break

    def edit_activity(self):
        if not self.ensure_school_year_writable():
            return
        aid = self.current_id(self.activity_table)
        if not aid:
            return self.warn_select("نشاط")
        majal, _namat, _day_data_id = self._current_lesson_context()
        row = next((x for x in self.db.activities(self.selected_lesson_id) if x[0] == aid), None)
        if not row:
            return
        weekday = self.date_edit.date().dayOfWeek() - 1
        dlg = ActivityEntryDialog("تعديل نشاط", majal, self.db, weekday,
                                  values=[row[1], row[2]], parent=self)
        while dlg.exec():
            title, duration = dlg.values()
            if not title:
                return QMessageBox.warning(self, "بيانات ناقصة", "عنوان النشاط مطلوب.")
            duration_minutes = self._duration_minutes(duration)
            if duration_minutes is None:
                return QMessageBox.warning(
                    self, "مدة غير صالحة", "يرجى تحديد مدة النشاط بالدقائق."
                )
            lesson_minutes, activity_minutes = self._activity_time_budget(
                exclude_activity_id=aid
            )
            if lesson_minutes is None:
                return QMessageBox.warning(
                    self, "مدة الحصة غير صالحة",
                    "حدد مدة الحصة بالدقائق قبل تعديل النشاط."
                )
            if activity_minutes is None:
                return QMessageBox.warning(
                    self, "مدد أنشطة غير صالحة",
                    "تعذر حساب مجموع مدد الأنشطة؛ صحح مددها لتكون بالدقائق."
                )
            if activity_minutes + duration_minutes > lesson_minutes:
                remaining_minutes = max(lesson_minutes - activity_minutes, 0)
                QMessageBox.warning(
                    self, "تجاوز مدة الحصة",
                    f"مجموع مدد الأنشطة سيتجاوز مدة الحصة "
                    f"({lesson_minutes} دقيقة). غيّر مدة هذا النشاط إلى "
                    f"{remaining_minutes} دقيقة أو أقل."
                )
                dlg.time_combo.setFocus()
                continue
            self.db.save_activity(title, duration, self.selected_lesson_id, aid)
            self.refresh_activities(aid)
            break

    def delete_activity(self):
        if not self.ensure_school_year_writable():
            return
        aid = self.current_id(self.activity_table)
        if not aid:
            return self.warn_select("نشاط")
        if self.confirm("حذف النشاط المحدد وكل خصائصه؟"):
            self.db.delete_activity(aid)
            self.selected_activity_id = None
            self.refresh_activities()
            self.detail_table.setRowCount(0)

    def _detail_dialog_context(self):
        majal, namat, _day_data_id = self._current_lesson_context()
        activity_row = next((x for x in self.db.activities(self.selected_lesson_id or -1)
                             if x[0] == self.selected_activity_id), None)
        activity_title = activity_row[1] if activity_row else ""
        return namat, activity_title

    def add_detail(self):
        if not self.ensure_school_year_writable():
            return
        if not self.selected_activity_id:
            return self.warn_select("نشاط أولاً")
        namat, activity_title = self._detail_dialog_context()
        current_details = self.db.details(self.selected_activity_id)
        current_count = len(current_details)
        contextual_content = next(
            (
                str(detail[2] or "")
                for detail in current_details
                if str(detail[1] or "").strip() == "المحتوى"
            ),
            None,
        )
        dlg = DetailEntryDialog("إضافة خاصية للنشاط", namat, activity_title,
                                self.db, current_count=current_count,
                                parent=self,
                                contextual_content=contextual_content)
        if dlg.exec():
            key, value = dlg.values()
            if not key:
                return QMessageBox.warning(self, "بيانات ناقصة", "يرجى اختيار اسم الخاصية.")
            if any(d[1] == key for d in current_details):
                reply = QMessageBox.question(
                    self,
                    "تنبيه: خاصية موجودة مسبقاً",
                    f"اسم الخاصية '{key}' موجود مسبقاً لهذا النشاط.\nهل تريد متابعة الإضافة رغم ذلك؟",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                if reply == QMessageBox.No:
                    return
            did = self.db.save_detail(key, value, self.selected_activity_id)
            self.refresh_details(did)

    def add_all_activity_details(self):
        if not self.ensure_school_year_writable():
            return
        if not self.selected_lesson_id:
            return self.warn_select("حصة أولاً")
        lesson = self.db.lesson(self.selected_lesson_id)
        if not lesson:
            return QMessageBox.warning(
                self, "الحصة غير موجودة",
                "تعذر العثور على الحصة المحددة."
            )
        activities = self.db.activities(self.selected_lesson_id)
        if not activities:
            return QMessageBox.information(
                self, "لا توجد أنشطة",
                "أضف نشاطاً واحداً على الأقل للحصة قبل إدخال خصائصها."
            )
        lesson_label = (
            f"الحصة {lesson[6]} — {lesson[1]} — {lesson[2]} "
            f"— نمط التعلم: {lesson[7]}"
        )
        dialog = BulkActivityDetailsDialog(
            self.db, lesson_label, activities, lesson[7], parent=self
        )
        if dialog.exec():
            self.refresh_activities(self.selected_activity_id)
            if self.selected_activity_id:
                self.refresh_details()

    def edit_detail(self):
        if not self.ensure_school_year_writable():
            return
        did = self.current_id(self.detail_table)
        if not did:
            return self.warn_select("خاصية")
        current_details = self.db.details(self.selected_activity_id)
        row = next((x for x in current_details if x[0] == did), None)
        if not row:
            return
        namat, activity_title = self._detail_dialog_context()
        contextual_content = next(
            (
                str(detail[2] or "")
                for detail in current_details
                if str(detail[1] or "").strip() == "المحتوى"
            ),
            None,
        )
        dlg = DetailEntryDialog("تعديل خاصية", namat, activity_title, self.db,
                                values=[row[1], row[2]], parent=self,
                                contextual_content=contextual_content)
        if dlg.exec():
            key, value = dlg.values()
            if not key:
                return QMessageBox.warning(self, "بيانات ناقصة", "يرجى اختيار اسم الخاصية.")
            self.db.save_detail(key, value, self.selected_activity_id, did)
            self.refresh_details(did)

    def delete_detail(self):
        if not self.ensure_school_year_writable():
            return
        did = self.current_id(self.detail_table)
        if not did:
            return self.warn_select("خاصية")
        if self.confirm("هل تريد حذف الخاصية المحددة؟"):
            self.db.delete_detail(did)
            self.refresh_details()

    def warn_select(self, what):
        QMessageBox.information(self, "تحديد مطلوب", f"يرجى تحديد {what} من الجدول.")

    def confirm(self, message):
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("تأكيد العملية")
        msg_box.setText(message)
        msg_box.setIcon(QMessageBox.Question)
        btn_yes = msg_box.addButton("نعم", QMessageBox.AcceptRole)
        btn_yes.setProperty("actionType", "save")
        btn_no = msg_box.addButton("إلغاء", QMessageBox.RejectRole)
        btn_no.setProperty("actionType", "cancel")
        msg_box.exec()
        return msg_box.clickedButton() == btn_yes

    def export_pdf(self):
        selection_dialog = DaySelectionDialog(self.db, parent=self)
        if not selection_dialog.exec():
            return

        selected_date_text = selection_dialog.get_selected_date_str()
        try:
            day_id = self.db.recorded_day_id(selected_date_text)
            if not day_id:
                QMessageBox.information(
                    self,
                    "تنبيه",
                    f"لا توجد حصص مسجلة للتاريخ: {selected_date_text}",
                )
                return

            self.day_lessons_page.set_day(day_id, selected_date_text)
            self.day_lessons_page.export_pdf()
        except (sqlite3.Error, RuntimeError) as exc:
            QMessageBox.critical(
                self,
                "تعذر تصدير PDF",
                f"حدث خطأ أثناء تحميل بيانات اليوم:\n{exc}",
            )

    def export_day_to_json(self):
        import json
        dlg = DaySelectionDialog(self.db, parent=self)
        if not dlg.exec():
            return
        
        selected_date_text = dlg.get_selected_date_str()
        formatter = SchoolDataFormatter(str(DB_PATH))
        formatted_db_date = formatter.normalize_date(selected_date_text)
        
        with formatter.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT d.id AS day_id, d.date, a.nom_annee, a.nom_ecole,
                          a.nom_enseignant, a.nom_inspecteur, a.grade
                   FROM day_data d
                   JOIN ann_scolaire a ON d.ann_scolaire_id = a.id
                   WHERE d.date = ? AND d.ann_scolaire_id = ?""",
                (formatted_db_date, self.db.year_id),
            )
            day_row = cursor.fetchone()
            if not day_row:
                QMessageBox.information(self, "تنبيه", f"لا توجد بيانات مسجلة للتاريخ: {selected_date_text}")
                return False
                
            day_data = {
                "date": day_row["date"],
                "school_info": {
                    "nom_annee": day_row["nom_annee"], 
                    "nom_ecole": day_row["nom_ecole"], 
                    "nom_enseignant": day_row["nom_enseignant"], 
                    "nom_inspecteur": day_row["nom_inspecteur"], 
                    "grade": day_row["grade"]
                },
                "lessons": []
            }
            
            cursor.execute("""
                SELECT h.id, c.name AS classe, l.name AS classe_level,
                    m.name AS majal, h.heure, h.time, h.wehda_number,
                    h.hessa_number, lm.name AS namat_taallom
                FROM hessa h
                JOIN classe c ON c.id=h.classe_id
                JOIN level l ON l.id=c.level_id
                LEFT JOIN majal m ON m.id=h.majal_id
                LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                WHERE h.day_data_id=? ORDER BY h.id ASC
            """, (day_row["day_id"],))
            hessa_rows = cursor.fetchall()
            
            for h in hessa_rows:
                lesson_dict = {
                    "id": h["id"], "classe": h["classe"],
                    "classe_level": h["classe_level"], "majal": h["majal"],
                    "heure": h["heure"], "time": h["time"], "wehda_number": h["wehda_number"], 
                    "hessa_number": h["hessa_number"], "namat_taallom": h["namat_taallom"], "activities": []
                }
                
                cursor.execute("SELECT id, title, time FROM nachat WHERE hessa_id = ? ORDER BY id ASC", (h["id"],))
                nachat_rows = cursor.fetchall()
                
                for n in nachat_rows:
                    activity_dict = {"id": n["id"], "title": n["title"], "time": n["time"], "details": []}
                    cursor.execute("SELECT key_name, value_text FROM nachat_detail WHERE nachat_id = ? ORDER BY id ASC", (n["id"],))
                    detail_rows = cursor.fetchall()
                    
                    for d in detail_rows:
                        activity_dict["details"].append({"key_name": d["key_name"], "value_text": d["value_text"]})
                        
                    lesson_dict["activities"].append(activity_dict)
                    
                day_data["lessons"].append(lesson_dict)
            
            docs_dir = Path.home() / "Documents"
            docs_dir.mkdir(parents=True, exist_ok=True)
            filename = docs_dir / f"school_day_{day_row['date']}.json"
            
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(day_data, f, ensure_ascii=False, indent=4)
                
            QMessageBox.information(self, "نجاح التصدير", f"تم تصدير بيانات يوم {day_row['date']} بنجاح إلى مجلد المستندات:\n{filename}")
            return True

    def import_day_from_json(self):
        if not self.ensure_school_year_writable():
            return
        import json
        from PySide6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(self, "اختيار ملف JSON للاستيراد", str(APP_DIR), "ملفات JSON (*.json);;كل الملفات (*)")
        if not file_path:
            return
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                day_data = json.load(f)
            target_date = day_data.get("date")
            if not target_date:
                QMessageBox.warning(self, "خطأ في الملف", "ملف JSON لا يحتوي على حقل التاريخ (date).")
                return
            with self.db.connect() as con:
                cursor = con.cursor()
                cursor.execute("SELECT id FROM day_data WHERE date = ? AND ann_scolaire_id = ?", (target_date, self.db.year_id))
                existing_day = cursor.fetchone()
                if existing_day:
                    old_day_id = existing_day[0]
                    cursor.execute("DELETE FROM day_data WHERE id = ?", (old_day_id,))
                cursor.execute("INSERT INTO day_data (date, ann_scolaire_id) VALUES (?, ?)", (target_date, self.db.year_id))
                new_day_id = cursor.lastrowid
                for lesson in day_data.get("lessons", []):
                    classe_id = self.db.resolve_class_id(
                        con, lesson.get("classe", ""),
                        lesson.get("classe_level"),
                    )
                    majal_id, learning_mode_id = (
                        self.db.resolve_lesson_lookup_ids(
                            con, lesson.get("majal", ""),
                            lesson.get("namat_taallom", ""),
                        )
                    )
                    cursor.execute("""
                        INSERT INTO hessa (
                            classe_id, majal_id, heure, time, wehda_number,
                            hessa_number, learning_mode_id, day_data_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        classe_id, majal_id,
                        lesson.get("heure", ""), lesson.get("time", ""),
                        lesson.get("wehda_number", 1),
                        lesson.get("hessa_number", 1),
                        learning_mode_id, new_day_id,
                    ))
                    new_hessa_id = cursor.lastrowid
                    for activity in lesson.get("activities", []):
                        cursor.execute("INSERT INTO nachat (title, time, hessa_id) VALUES (?, ?, ?)", (activity.get("title", ""), activity.get("time", ""), new_hessa_id))
                        new_nachat_id = cursor.lastrowid
                        for detail in activity.get("details", []):
                            cursor.execute("INSERT INTO nachat_detail (key_name, value_text, nachat_id) VALUES (?, ?, ?)", (detail.get("key_name", ""), detail.get("value_text", ""), new_nachat_id))
                con.commit()
            qd = QDate.fromString(target_date, "yyyy-MM-dd")
            if qd.isValid():
                self.date_edit.setDate(qd)
            self.load_day()
            QMessageBox.information(self, "نجاح الاستيراد", f"تم استيراد بيانات يوم {target_date} بنجاح وتسجيلها في التطبيق.")
        except Exception as e:
            QMessageBox.critical(self, "خطأ في الاستيراد", f"حدث خطأ أثناء قراءة أو استيراد ملف JSON:\n{e}")

    @staticmethod
    def _single_json_drop_path(event):
        """أرجع مسار ملف JSON واحد فقط، وتجاهل السحب الداخلي والعناصر الأخرى."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        urls = mime.urls()
        if len(urls) != 1:
            return None
        path = urls[0].toLocalFile()
        if not path or Path(path).suffix.lower() != ".json":
            return None
        return path

    @staticmethod
    def _classify_json_payload(data):
        """تصنيف ملفات التطبيق حتى لا تختلط نسخة البرامج الرسمية بملفات الأيام."""
        if not isinstance(data, dict):
            return None
        if data.get("format") == "almo3in_official_programs":
            return "official_full"
        if isinstance(data.get("programs"), list) and isinstance(data.get("levels"), list) and isinstance(data.get("majals"), list):
            return "official_full"
        if "moukawan_kifaya" in data and "matiere" in data:
            return "official_tree"
        if isinstance(data.get("days"), list) or ("school_info" in data and "date" not in data):
            return "school_year"
        if "date" in data and isinstance(data.get("lessons"), list):
            return "school_day"
        return None

    def dragEnterEvent(self, event):
        # نقبل الملفات الخارجية بصيغة JSON فقط؛ السحب الداخلي بين عناصر الواجهة لا يُلتقط هنا.
        path = self._single_json_drop_path(event)
        if path:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        file_path = self._single_json_drop_path(event)
        if not file_path:
            event.ignore()
            return

        try:
            with open(file_path, "r", encoding="utf-8-sig") as source:
                data = json.load(source)
            payload_type = self._classify_json_payload(data)
            if payload_type in ("official_full", "official_tree"):
                self.pages_stack.setCurrentWidget(self.official_programs_page)
                self.import_programs_json(file_path)
            elif payload_type in ("school_year", "school_day"):
                self.process_json_import(file_path)
            else:
                QMessageBox.warning(
                    self,
                    "نوع ملف غير معروف",
                    "لم يتعرّف التطبيق على محتوى الملف. اسحب ملف JSON صادرًا عن التطبيق أو عن صفحة البرامج الرسمية.",
                )
                event.ignore()
                return
            event.acceptProposedAction()
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            QMessageBox.critical(self, "تعذّر قراءة الملف", f"لم نتمكّن من قراءة ملف JSON:\n{exc}")
            event.ignore()
        except Exception as exc:
            QMessageBox.critical(self, "خطأ أثناء الاستيراد", f"تعذّر استيراد الملف:\n{exc}")
            event.ignore()

    def import_all_years_from_json(self):
        if not self.ensure_school_year_writable():
            return
        from PySide6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(self, "اختيار ملف JSON السنوي للاستيراد", str(Path.home() / "Documents"), "ملفات JSON (*.json);;كل الملفات (*)")
        if not file_path:
            return
        self.process_json_import(file_path)

    def process_json_import(self, file_path):
        if not self.ensure_school_year_writable():
            return
        import json
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            if "days" in data:
                school_info = data.get("school_info", {})
                default_year_name = school_info.get("nom_annee", "2026-2027")
                
                dlg = YearImportDialog(self.db, default_year_name=default_year_name, parent=self)
                if not dlg.exec():
                    return
                target_year_name = dlg.get_selected_year_name()
                if not target_year_name:
                    QMessageBox.warning(self, "تنبيه", "اسم السنة الدراسية مطلوب.")
                    return

                with self.db.connect() as con:
                    cursor = con.cursor()
                    cursor.execute("SELECT id FROM ann_scolaire WHERE nom_annee = ?", (target_year_name,))
                    row = cursor.fetchone()
                    legacy_profile_columns = SCHOOL_INFO_COLUMNS[1:5]
                    extended_profile_columns = SCHOOL_INFO_COLUMNS[5:]
                    if row:
                        target_year_id = row[0]
                        assignments = [
                            f"{column}=?" for column in legacy_profile_columns
                        ] + [
                            f"{column}=COALESCE(?, {column})"
                            for column in extended_profile_columns
                        ]
                        cursor.execute(
                            f"UPDATE ann_scolaire SET {', '.join(assignments)} "
                            "WHERE id=?",
                            tuple(
                                school_info.get(column, "")
                                for column in legacy_profile_columns
                            )
                            + tuple(
                                school_info.get(column)
                                for column in extended_profile_columns
                            )
                            + (target_year_id,)
                        )
                    else:
                        insert_values = [target_year_name]
                        for column in SCHOOL_INFO_COLUMNS[1:]:
                            default_value = 0 if column == "nombre_enfants" else ""
                            insert_values.append(
                                school_info.get(column, default_value)
                            )
                        placeholders = ", ".join("?" for _ in SCHOOL_INFO_COLUMNS)
                        cursor.execute(
                            f"INSERT INTO ann_scolaire "
                            f"({', '.join(SCHOOL_INFO_COLUMNS)}) "
                            f"VALUES ({placeholders})",
                            insert_values
                        )
                        target_year_id = cursor.lastrowid

                    imported_days_count = 0
                    for day_item in data.get("days", []):
                        target_date = day_item.get("date")
                        if not target_date:
                            continue
                        
                        cursor.execute("SELECT id FROM day_data WHERE date = ? AND ann_scolaire_id = ?", (target_date, target_year_id))
                        existing_day = cursor.fetchone()
                        if existing_day:
                            cursor.execute("DELETE FROM day_data WHERE id = ?", (existing_day[0],))
                        
                        cursor.execute("INSERT INTO day_data (date, ann_scolaire_id) VALUES (?, ?)", (target_date, target_year_id))
                        new_day_id = cursor.lastrowid
                        
                        for lesson in day_item.get("lessons", []):
                            classe_id = self.db.resolve_class_id(
                                con, lesson.get("classe", ""),
                                lesson.get("classe_level"),
                            )
                            majal_id, learning_mode_id = (
                                self.db.resolve_lesson_lookup_ids(
                                    con, lesson.get("majal", ""),
                                    lesson.get("namat_taallom", ""),
                                )
                            )
                            cursor.execute("""
                                INSERT INTO hessa (
                                    classe_id, majal_id, heure, time,
                                    wehda_number, hessa_number,
                                    learning_mode_id, day_data_id
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                classe_id, majal_id,
                                lesson.get("heure", ""), lesson.get("time", ""),
                                lesson.get("wehda_number", 1),
                                lesson.get("hessa_number", 1),
                                learning_mode_id, new_day_id,
                            ))
                            new_hessa_id = cursor.lastrowid
                            
                            for activity in lesson.get("activities", []):
                                cursor.execute("INSERT INTO nachat (title, time, hessa_id) VALUES (?, ?, ?)", (activity.get("title", ""), activity.get("time", ""), new_hessa_id))
                                new_nachat_id = cursor.lastrowid
                                
                                for detail in activity.get("details", []):
                                    cursor.execute("INSERT INTO nachat_detail (key_name, value_text, nachat_id) VALUES (?, ?, ?)", (detail.get("key_name", ""), detail.get("value_text", ""), new_nachat_id))
                        
                        imported_days_count += 1
                    con.commit()

                self.load_school_info()
                self.load_day()
                QMessageBox.information(self, "نجاح الاستيراد الشامل", f"تم استيراد السنة الدراسية بكافة أيامها ({imported_days_count} يوم) بنجاح!")
            else:
                target_date = data.get("date")
                if not target_date:
                    QMessageBox.warning(self, "خطأ في الملف", "ملف JSON لا يحتوي على حقل التاريخ (date) أو بيانات صحيحة.")
                    return
                with self.db.connect() as con:
                    cursor = con.cursor()
                    cursor.execute("SELECT id FROM day_data WHERE date = ? AND ann_scolaire_id = ?", (target_date, self.db.year_id))
                    existing_day = cursor.fetchone()
                    if existing_day:
                        cursor.execute("DELETE FROM day_data WHERE id = ?", (existing_day[0],))
                    cursor.execute("INSERT INTO day_data (date, ann_scolaire_id) VALUES (?, ?)", (target_date, self.db.year_id))
                    new_day_id = cursor.lastrowid
                    for lesson in data.get("lessons", []):
                        classe_id = self.db.resolve_class_id(
                            con, lesson.get("classe", ""),
                            lesson.get("classe_level"),
                        )
                        majal_id, learning_mode_id = (
                            self.db.resolve_lesson_lookup_ids(
                                con, lesson.get("majal", ""),
                                lesson.get("namat_taallom", ""),
                            )
                        )
                        cursor.execute("""
                            INSERT INTO hessa (
                                classe_id, majal_id, heure, time, wehda_number,
                                hessa_number, learning_mode_id, day_data_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            classe_id, majal_id,
                            lesson.get("heure", ""), lesson.get("time", ""),
                            lesson.get("wehda_number", 1),
                            lesson.get("hessa_number", 1),
                            learning_mode_id, new_day_id,
                        ))
                        new_hessa_id = cursor.lastrowid
                        for activity in lesson.get("activities", []):
                            cursor.execute("INSERT INTO nachat (title, time, hessa_id) VALUES (?, ?, ?)", (activity.get("title", ""), activity.get("time", ""), new_hessa_id))
                            new_nachat_id = cursor.lastrowid
                            for detail in activity.get("details", []):
                                cursor.execute("INSERT INTO nachat_detail (key_name, value_text, nachat_id) VALUES (?, ?, ?)", (detail.get("key_name", ""), detail.get("value_text", ""), new_nachat_id))
                    con.commit()
                qd = QDate.fromString(target_date, "yyyy-MM-dd")
                if qd.isValid():
                    self.date_edit.setDate(qd)
                self.load_day()
                QMessageBox.information(self, "نجاح الاستيراد", f"تم استيراد بيانات يوم {target_date} بنجاح وتسجيلها في التطبيق.")
        except Exception as e:
            QMessageBox.critical(self, "خطأ في الاستيراد", f"حدث خطأ أثناء قراءة أو استيراد ملف JSON:\n{e}")

    def clear_current_day_lessons(self):
        if not self.ensure_school_year_writable():
            return
        if not self.day_id:
            return
        if self.confirm("هل أنت متأكد من مسح جميع حصص اليوم الحالي مع أنشطتها وخصائصها؟"):
            try:
                with self.db.connect() as con:
                    cursor = con.cursor()
                    cursor.execute("DELETE FROM hessa WHERE day_data_id = ?", (self.day_id,))
                    con.commit()
                self.selected_lesson_id = None
                self.selected_activity_id = None
                self.load_day()
                self.statusBar().showMessage("تم مسح جميع حصص اليوم بنجاح.", 3000)
            except Exception as e:
                QMessageBox.critical(self, "خطأ", f"حدث خطأ أثناء مسح حصص اليوم: {e}")

    def clear_all_lessons_in_db(self):
        if not self.ensure_school_year_writable():
            return
        if self.confirm("تحذير خطير: هل أنت متأكد من مسح جميع الحصص والأنشطة والخصائص في قاعدة البيانات بالكامل؟"):
            try:
                with self.db.connect() as con:
                    cursor = con.cursor()
                    cursor.execute("DELETE FROM nachat_detail")
                    cursor.execute("DELETE FROM nachat")
                    cursor.execute("DELETE FROM hessa")
                    con.commit()
                self.selected_lesson_id = None
                self.selected_activity_id = None
                self.load_day()
                self.statusBar().showMessage("تم مسح جميع الحصص في قاعدة البيانات بالكامل.", 3000)
            except Exception as e:
                QMessageBox.critical(self, "خطأ", f"حدث خطأ أثناء مسح جميع الحصص: {e}")

    def clear_all_activities_for_lesson(self):
        if not self.ensure_school_year_writable():
            return
        if not self.selected_lesson_id:
            return self.warn_select("حصة أولاً")
        if self.confirm("هل أنت متأكد من مسح جميع أنشطة هذه الحصة وخصائصها؟"):
            try:
                with self.db.connect() as con:
                    cursor = con.cursor()
                    cursor.execute("DELETE FROM nachat WHERE hessa_id = ?", (self.selected_lesson_id,))
                    con.commit()
                self.selected_activity_id = None
                self.refresh_activities()
                self.detail_table.setRowCount(0)
                self.statusBar().showMessage("تم مسح جميع أنشطة الحصة بنجاح.", 3000)
            except Exception as e:
                QMessageBox.critical(self, "خطأ", f"حدث خطأ أثناء مسح أنشطة الحصة: {e}")

    def clear_all_details_for_activity(self):
        if not self.ensure_school_year_writable():
            return
        if not self.selected_activity_id:
            return self.warn_select("نشاط أولاً")
        if self.confirm("هل أنت متأكد من مسح جميع خصائص هذا النشاط؟"):
            try:
                with self.db.connect() as con:
                    cursor = con.cursor()
                    cursor.execute("DELETE FROM nachat_detail WHERE nachat_id = ?", (self.selected_activity_id,))
                    con.commit()
                self.refresh_details()
                self.statusBar().showMessage("تم مسح جميع خصائص النشاط بنجاح.", 3000)
            except Exception as e:
                QMessageBox.critical(self, "خطأ", f"حدث خطأ أثناء مسح خصائص النشاط: {e}")

    def copy_activity_details(self):
        if not self.selected_activity_id:
            return self.warn_select("نشاط أولاً لنسخ خصائصه")
        details = self.db.details(self.selected_activity_id)
        if not details:
            QMessageBox.information(self, "تنبيه", "النشاط المحدد لا يحتوي على خصائص لنسخها.")
            return
        self.copied_details = [(d[1], d[2]) for d in details]
        self.statusBar().showMessage(f"تم نسخ {len(self.copied_details)} خاصية بنجاح.", 3000)

    def paste_activity_details(self):
        if not self.ensure_school_year_writable():
            return
        if not self.selected_activity_id:
            return self.warn_select("نشاط أولاً لصق الخصائص فيه")
        if not self.copied_details:
            QMessageBox.warning(self, "تنبيه", "لا توجد خصائص منسوخة حالياً.")
            return
        try:
            with self.db.connect() as con:
                cursor = con.cursor()
                for key_name, value_text in self.copied_details:
                    cursor.execute("INSERT INTO nachat_detail (key_name, value_text, nachat_id) VALUES (?, ?, ?)", (key_name, value_text, self.selected_activity_id))
                con.commit()
            self.refresh_details()
            self.statusBar().showMessage("تم لصق الخصائص بنجاح.", 3000)
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"حدث خطأ أثناء لصق الخصائص: {e}")

    def export_all_days_for_year(self):
        import json
        dlg = SchoolYearSelectionDialog(self.db, parent=self)
        if not dlg.exec():
            return
        
        year_id, year_name = dlg.get_selected_year_info()
        if not year_id:
            QMessageBox.warning(self, "تنبيه", "لم يتم تحديد سنة دراسية.")
            return

        try:
            with self.db.connect() as conn:
                cursor = conn.cursor()
                columns = ", ".join(SCHOOL_INFO_COLUMNS)
                cursor.execute(
                    f"SELECT {columns} FROM ann_scolaire WHERE id = ?",
                    (year_id,)
                )
                school_info_row = cursor.fetchone()
                school_info = dict(zip(
                    SCHOOL_INFO_COLUMNS,
                    school_info_row or ("", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", 0)
                ))

                cursor.execute("SELECT id, date FROM day_data WHERE ann_scolaire_id = ? ORDER BY date ASC", (year_id,))
                days = cursor.fetchall()
                
                if not days:
                    QMessageBox.information(self, "تنبيه", f"لا توجد أيام مسجلة للسنة الدراسية: {year_name}")
                    return

                all_days_data = {
                    "school_info": school_info,
                    "days": []
                }

                for day in days:
                    day_id = day[0]
                    day_date = day[1]
                    
                    day_entry = {
                        "date": day_date,
                        "lessons": []
                    }

                    cursor.execute("""
                        SELECT h.id, c.name, l.name, m.name, h.heure, h.time,
                            h.wehda_number, h.hessa_number, lm.name
                        FROM hessa h
                        JOIN classe c ON c.id=h.classe_id
                        JOIN level l ON l.id=c.level_id
                        LEFT JOIN majal m ON m.id=h.majal_id
                        LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                        WHERE h.day_data_id=? ORDER BY h.id ASC
                    """, (day_id,))
                    hessa_rows = cursor.fetchall()
                    
                    for h in hessa_rows:
                        lesson_dict = {
                            "id": h[0], "classe": h[1], "classe_level": h[2],
                            "majal": h[3], "heure": h[4], "time": h[5],
                            "wehda_number": h[6], "hessa_number": h[7],
                            "namat_taallom": h[8], "activities": []
                        }
                        
                        cursor.execute("SELECT id, title, time FROM nachat WHERE hessa_id = ? ORDER BY id ASC", (h[0],))
                        nachat_rows = cursor.fetchall()
                        
                        for n in nachat_rows:
                            activity_dict = {"id": n[0], "title": n[1], "time": n[2], "details": []}
                            cursor.execute("SELECT key_name, value_text FROM nachat_detail WHERE nachat_id = ? ORDER BY id ASC", (n[0],))
                            detail_rows = cursor.fetchall()
                            for d in detail_rows:
                                activity_dict["details"].append({"key_name": d[0], "value_text": d[1]})
                            lesson_dict["activities"].append(activity_dict)
                        day_entry["lessons"].append(lesson_dict)
                        
                    all_days_data["days"].append(day_entry)

                docs_dir = Path.home() / "Documents"
                docs_dir.mkdir(parents=True, exist_ok=True)
                safe_year_name = year_name.replace("/", "-")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = docs_dir / f"school_year_{safe_year_name}_export_{timestamp}.json"

                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(all_days_data, f, ensure_ascii=False, indent=4)

                QMessageBox.information(
                    self, 
                    "نجاح التصدير الشامل", 
                    f"تم تصدير جميع أيام السنة الدراسية ({year_name}) بنجاح إلى مجلد المستندات:\n{filename}"
                )
                return True

        except Exception as e:
            QMessageBox.critical(self, "خطأ في التصدير", f"حدث خطأ أثناء تصدير بيانات السنة الدراسية:\n{e}")
            return False
        

    def update_prog_actions_state(self):
        """التصدير والاستيراد الشاملان لا يتطلبان تحديد مادة بعينها."""
        self.export_prog_action.setEnabled(True)
        self.import_prog_action.setEnabled(True)

    def export_programs_json(self):
        """تصدير كامل البرامج الرسمية، من المستويات إلى المحتويات، في ملف JSON واحد."""
        from PySide6.QtWidgets import QFileDialog

        documents_dir = Path.home() / "Documents"
        start_dir = documents_dir if documents_dir.exists() else Path.home()
        default_name = start_dir / f"البرامج_الرسمية_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        try:
            with self.db.connect() as con:
                levels = [row[0] for row in con.execute("SELECT name FROM level ORDER BY id").fetchall()]
                majals = [row[0] for row in con.execute("SELECT name FROM majal ORDER BY id").fetchall()]

                majal_comp_rows = con.execute(
                    """SELECT cmj.id, cmj.competence, m.name, l.name
                       FROM competence_majal cmj
                       JOIN majal m ON m.id = cmj.id_majal
                       JOIN level l ON l.id = cmj.id_level
                       ORDER BY l.id, m.id, cmj.id"""
                ).fetchall()
                matiere_rows = con.execute(
                    """SELECT id, matiere, competence, id_competence_majal
                       FROM competence_matiere ORDER BY id"""
                ).fetchall()
                mouk_rows = con.execute(
                    """SELECT id, competence, id_competence_matiere
                       FROM moukawan_kifaya ORDER BY id"""
                ).fetchall()
                objectif_rows = con.execute(
                    """SELECT id, objectif_specifique, description, id_moukawa_kifaya
                       FROM objectif_specifique ORDER BY id"""
                ).fetchall()
                content_rows = con.execute(
                    """SELECT content, description, id_objectif_specifique
                       FROM content ORDER BY id"""
                ).fetchall()

            contents_by_objectif = {}
            for content_text, description, objectif_id in content_rows:
                contents_by_objectif.setdefault(objectif_id, []).append({
                    "content": content_text or "", "description": description or ""
                })

            objectifs_by_mouk = {}
            for objectif_id, objectif_text, description, mouk_id in objectif_rows:
                objectifs_by_mouk.setdefault(mouk_id, []).append({
                    "objectif_specifique": objectif_text or "",
                    "description": description or "",
                    "content": contents_by_objectif.get(objectif_id, []),
                })

            mouk_by_matiere = {}
            for mouk_id, competence, matiere_id in mouk_rows:
                mouk_by_matiere.setdefault(matiere_id, []).append({
                    "competence": competence or "",
                    "objectif_specifique": objectifs_by_mouk.get(mouk_id, []),
                })

            matieres_by_competence_majal = {}
            for matiere_id, matiere, competence, competence_majal_id in matiere_rows:
                matieres_by_competence_majal.setdefault(competence_majal_id, []).append({
                    "matiere": matiere or "",
                    "competence_matiere": competence or "",
                    "moukawan_kifaya": mouk_by_matiere.get(matiere_id, []),
                })

            programs = []
            for cmj_id, competence_majal, majal_name, level_name in majal_comp_rows:
                programs.append({
                    "level": level_name,
                    "majal": majal_name,
                    "competence_majal": competence_majal or "",
                    "matieres": matieres_by_competence_majal.get(cmj_id, []),
                })

            payload = {
                "format": "almo3in_official_programs",
                "version": 1,
                "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "levels": levels,
                "majals": majals,
                "programs": programs,
            }

            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "تصدير البرامج الرسمية كاملة",
                str(default_name),
                "ملفات JSON (*.json);;كل الملفات (*)",
            )
            if not file_path:
                return
            if Path(file_path).suffix.lower() != ".json":
                file_path += ".json"
            target = Path(file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")

            QMessageBox.information(
                self, "تمّ التصدير",
                f"تمّ تصدير البرامج الرسمية كاملة بنجاح.\nعدد المستويات: {len(levels)}\nعدد المجالات: {len(majals)}\nعدد كفايات المجالات: {len(programs)}\nالملف: {target}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "تعذّر التصدير", f"حدث خطأ أثناء تصدير البرامج الرسمية:\n{exc}")

    def import_programs_json(self, file_path=None):
        """استيراد نسخة كاملة، مع دعم ملفات الشجرة القديمة للحفاظ على التوافق."""
        from PySide6.QtWidgets import QFileDialog

        if not file_path:
            documents_dir = Path.home() / "Documents"
            start_dir = documents_dir if documents_dir.exists() else Path.home()
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "استيراد البرامج الرسمية من ملف JSON",
                str(start_dir),
                "ملفات JSON (*.json);;كل الملفات (*)",
            )
            if not file_path:
                return

        try:
            with open(file_path, "r", encoding="utf-8-sig") as source:
                data = json.load(source)
            payload_type = self._classify_json_payload(data)

            if payload_type == "official_full":
                answer = QMessageBox.question(
                    self,
                    "تأكيد استبدال البرامج الرسمية",
                    "سيُستبدل محتوى البرامج الرسمية الحالي بالكامل بالمحتوى الموجود في الملف، بما يشمل المستويات والمجالات والكفايات والأهداف والمحتويات.\n\nيُنصح بتصدير نسخة احتياطية قبل المتابعة. هل تريد مواصلة الاستيراد؟",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
                counts = self._restore_full_official_programs(data)
                self.pages_stack.setCurrentWidget(self.official_programs_page)
                self.official_programs_page.load_levels_and_majals()
                QMessageBox.information(
                    self, "تمّ الاستيراد",
                    "تمّ استيراد البرامج الرسمية بنجاح.\n"
                    f"المستويات: {counts['levels']}، المجالات: {counts['majals']}، "
                    f"كفايات المجالات: {counts['competences_majal']}، المواد: {counts['matieres']}، "
                    f"مكوّنات الكفاية: {counts['moukawan']}، الأهداف: {counts['objectifs']}، المحتويات: {counts['contents']}."
                )
                return

            if payload_type == "official_tree":
                page = self.official_programs_page
                matiere_id = page.selected_competence_matiere_id
                if not matiere_id:
                    QMessageBox.warning(
                        self, "لم تُحدّد المادة",
                        "هذا الملف يحتوي على شجرة كفاية واحدة. افتح البرامج الرسمية وحدّد المستوى والمجال والمادة المستهدفة، ثم أعد الاستيراد.",
                    )
                    return
                imported = self._import_single_competence_tree(data, matiere_id)
                if not imported:
                    return
                page.load_moukawan_data()
                QMessageBox.information(self, "تمّ الاستيراد", "تمّ استيراد شجرة الكفاية المحددة بنجاح.")
                return

            QMessageBox.warning(
                self, "ملف غير صالح",
                "لا يحتوي الملف على نسخة برامج رسمية معروفة أو شجرة كفاية قابلة للاستيراد.",
            )
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            QMessageBox.critical(self, "تعذّر قراءة الملف", f"لم نتمكّن من قراءة ملف JSON:\n{exc}")
        except Exception as exc:
            QMessageBox.critical(self, "تعذّر الاستيراد", f"حدث خطأ أثناء استيراد البرامج الرسمية:\n{exc}")

    def _restore_full_official_programs(self, data):
        """استعادة نسخة كاملة داخل معاملة واحدة؛ يُلغى التغيير كله عند وقوع خطأ."""
        if not isinstance(data, dict):
            raise ValueError("يجب أن تكون بيانات ملف النسخة الاحتياطية كائن JSON.")
        if data.get("format") not in (None, "almo3in_official_programs"):
            raise ValueError("صيغة ملف البرامج الرسمية غير مدعومة.")
        if data.get("format") == "almo3in_official_programs" and data.get("version") != 1:
            raise ValueError("إصدار ملف البرامج الرسمية غير مدعوم.")
        if not all(key in data for key in ("levels", "majals", "programs")):
            raise ValueError("ملف النسخة الاحتياطية ناقص؛ لم يقع تغيير قاعدة البيانات.")
        levels_data = data["levels"]
        majals_data = data["majals"]
        programs = data["programs"]
        if not all(isinstance(value, list) for value in (levels_data, majals_data, programs)):
            raise ValueError("بنية ملف النسخة الاحتياطية غير سليمة.")

        def get_name(value):
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, dict):
                return str(value.get("name", "")).strip()
            return ""

        level_names = list(dict.fromkeys(name for name in map(get_name, levels_data) if name))
        majal_names = list(dict.fromkeys(name for name in map(get_name, majals_data) if name))
        for program in programs:
            if not isinstance(program, dict):
                raise ValueError("توجد كفاية مجال بصيغة غير سليمة داخل الملف.")
            level_name = str(program.get("level", "")).strip()
            majal_name = str(program.get("majal", "")).strip()
            if level_name and level_name not in level_names:
                level_names.append(level_name)
            if majal_name and majal_name not in majal_names:
                majal_names.append(majal_name)
            if not isinstance(program.get("matieres", []), list):
                raise ValueError("قائمة المواد في الملف غير سليمة.")

        counts = {
            "levels": 0, "majals": 0, "competences_majal": 0, "matieres": 0,
            "moukawan": 0, "objectifs": 0, "contents": 0,
        }
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # حذف مرتب لتبقى العلاقات سليمة حتى عند تعطيل بعض قيود الحماية خارج التطبيق.
            con.execute("DELETE FROM content")
            con.execute("DELETE FROM objectif_specifique")
            con.execute("DELETE FROM moukawan_kifaya")
            con.execute("DELETE FROM competence_matiere")
            con.execute("DELETE FROM competence_majal")
            con.execute("""
                DELETE FROM level
                WHERE id NOT IN (SELECT level_id FROM classe)
            """)
            con.execute("""
                DELETE FROM majal
                WHERE id NOT IN (
                    SELECT majal_id FROM hessa WHERE majal_id IS NOT NULL
                )
                AND id NOT IN (SELECT majal_id FROM teaching_plan)
            """)

            level_ids = {}
            for name in level_names:
                con.execute("INSERT OR IGNORE INTO level(name) VALUES(?)", (name,))
                level_ids[name] = con.execute("SELECT id FROM level WHERE name=?", (name,)).fetchone()[0]
            counts["levels"] = len(level_ids)

            majal_ids = {}
            for name in majal_names:
                con.execute("INSERT OR IGNORE INTO majal(name) VALUES(?)", (name,))
                majal_ids[name] = con.execute("SELECT id FROM majal WHERE name=?", (name,)).fetchone()[0]
            counts["majals"] = len(majal_ids)

            for program in programs:
                level_name = str(program.get("level", "")).strip()
                majal_name = str(program.get("majal", "")).strip()
                competence_majal = str(program.get("competence_majal", "")).strip()
                if not level_name or not majal_name or not competence_majal:
                    # تجاهل كفايات المجال غير المكتملة بدلاً من إنشاء سجلات عديمة الفائدة.
                    continue
                level_id = level_ids[level_name]
                majal_id = majal_ids[majal_name]
                con.execute(
                    "INSERT OR IGNORE INTO competence_majal(competence, id_majal, id_level) VALUES(?,?,?)",
                    (competence_majal, majal_id, level_id),
                )
                cmj_id = con.execute(
                    "SELECT id FROM competence_majal WHERE competence=? AND id_majal=? AND id_level=?",
                    (competence_majal, majal_id, level_id),
                ).fetchone()[0]
                counts["competences_majal"] += 1

                for matiere in program.get("matieres", []):
                    if not isinstance(matiere, dict):
                        raise ValueError("توجد مادة بصيغة غير سليمة داخل الملف.")
                    matiere_name = str(matiere.get("matiere", "")).strip()
                    competence_matiere = str(matiere.get("competence_matiere", "")).strip()
                    if not matiere_name or not competence_matiere:
                        continue
                    con.execute(
                        "INSERT OR IGNORE INTO competence_matiere(matiere, competence, id_competence_majal) VALUES(?,?,?)",
                        (matiere_name, competence_matiere, cmj_id),
                    )
                    matiere_id = con.execute(
                        "SELECT id FROM competence_matiere WHERE matiere=? AND competence=? AND id_competence_majal=?",
                        (matiere_name, competence_matiere, cmj_id),
                    ).fetchone()[0]
                    counts["matieres"] += 1

                    mouk_list = matiere.get("moukawan_kifaya", [])
                    if not isinstance(mouk_list, list):
                        raise ValueError("قائمة مكوّنات الكفاية غير سليمة.")
                    for mouk in mouk_list:
                        if isinstance(mouk, str):
                            mouk_text, objectifs = mouk.strip(), []
                        elif isinstance(mouk, dict):
                            mouk_text = str(mouk.get("competence", "")).strip()
                            objectifs = mouk.get("objectif_specifique", [])
                            if not isinstance(objectifs, list):
                                raise ValueError("قائمة الأهداف المميزة غير سليمة.")
                        else:
                            raise ValueError("يوجد مكوّن كفاية بصيغة غير سليمة.")
                        if not mouk_text:
                            continue
                        con.execute(
                            "INSERT OR IGNORE INTO moukawan_kifaya(competence, id_competence_matiere) VALUES(?,?)",
                            (mouk_text, matiere_id),
                        )
                        mouk_id = con.execute(
                            "SELECT id FROM moukawan_kifaya WHERE competence=? AND id_competence_matiere=?",
                            (mouk_text, matiere_id),
                        ).fetchone()[0]
                        counts["moukawan"] += 1

                        for objectif in objectifs:
                            if isinstance(objectif, str):
                                objectif_text, description, contents = objectif.strip(), "", []
                            elif isinstance(objectif, dict):
                                objectif_text = str(objectif.get("objectif_specifique", "")).strip()
                                description = str(objectif.get("description", "") or "")
                                contents = objectif.get("content", [])
                                if not isinstance(contents, list):
                                    raise ValueError("قائمة المحتويات غير سليمة.")
                            else:
                                raise ValueError("يوجد هدف مميز بصيغة غير سليمة.")
                            if not objectif_text:
                                continue
                            con.execute(
                                "INSERT OR IGNORE INTO objectif_specifique(objectif_specifique, id_moukawa_kifaya, description) VALUES(?,?,?)",
                                (objectif_text, mouk_id, description),
                            )
                            objectif_id = con.execute(
                                "SELECT id FROM objectif_specifique WHERE objectif_specifique=? AND id_moukawa_kifaya=?",
                                (objectif_text, mouk_id),
                            ).fetchone()[0]
                            counts["objectifs"] += 1

                            for content in contents:
                                if isinstance(content, str):
                                    content_text, content_description = content.strip(), ""
                                elif isinstance(content, dict):
                                    content_text = str(content.get("content", "")).strip()
                                    content_description = str(content.get("description", "") or "")
                                else:
                                    raise ValueError("يوجد محتوى بصيغة غير سليمة.")
                                if not content_text:
                                    continue
                                con.execute(
                                    "INSERT OR IGNORE INTO content(content, id_objectif_specifique, description) VALUES(?,?,?)",
                                    (content_text, objectif_id, content_description),
                                )
                                counts["contents"] += 1
            con.commit()
        return counts

    def _import_single_competence_tree(self, data, matiere_id):
        """توافق رجعي مع ملفات JSON القديمة التي تصدّر شجرة مادة واحدة."""
        file_majal = str(data.get("majal", "")).strip()
        file_matiere = str(data.get("matiere", "")).strip()
        with self.db.connect() as con:
            current_info = con.execute(
                """SELECT m.name, cm.matiere
                   FROM competence_matiere cm
                   JOIN competence_majal cmj ON cmj.id = cm.id_competence_majal
                   JOIN majal m ON m.id = cmj.id_majal
                   WHERE cm.id = ?""",
                (matiere_id,),
            ).fetchone()
            if not current_info:
                raise ValueError("المادة المحددة لم تعد موجودة في قاعدة البيانات.")
            current_majal, current_matiere = current_info[0].strip(), current_info[1].strip()
            if (file_majal and file_majal != current_majal) or (file_matiere and file_matiere != current_matiere):
                raise ValueError(
                    "لا تتطابق بيانات الملف مع المادة المحددة.\n"
                    f"الملف: المجال {file_majal}، المادة {file_matiere}.\n"
                    f"التحديد الحالي: المجال {current_majal}، المادة {current_matiere}."
                )
            tree = data.get("moukawan_kifaya", [])
            if not isinstance(tree, list):
                raise ValueError("قائمة مكوّنات الكفاية في الملف غير سليمة.")
            answer = QMessageBox.question(
                self,
                "تأكيد استبدال شجرة الكفاية",
                "سيتم حذف الشجرة الحالية للمادة المحددة وتعويضها بالشجرة الموجودة في الملف. هل تواصل؟",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False

            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM moukawan_kifaya WHERE id_competence_matiere = ?", (matiere_id,))
            for mouk in tree:
                if isinstance(mouk, str):
                    mouk_text, objectifs = mouk.strip(), []
                elif isinstance(mouk, dict):
                    mouk_text = str(mouk.get("competence", "")).strip()
                    objectifs = mouk.get("objectif_specifique", [])
                else:
                    raise ValueError("يوجد مكوّن كفاية بصيغة غير سليمة.")
                if not mouk_text:
                    continue
                cur = con.execute(
                    "INSERT INTO moukawan_kifaya(competence, id_competence_matiere) VALUES(?,?)",
                    (mouk_text, matiere_id),
                )
                new_mouk_id = cur.lastrowid
                if not isinstance(objectifs, list):
                    raise ValueError("قائمة الأهداف المميزة غير سليمة.")
                for obj in objectifs:
                    if isinstance(obj, str):
                        obj_text, obj_desc, contents = obj.strip(), "", []
                    elif isinstance(obj, dict):
                        obj_text = str(obj.get("objectif_specifique", "")).strip()
                        obj_desc = str(obj.get("description", "") or "")
                        contents = obj.get("content", [])
                    else:
                        raise ValueError("يوجد هدف مميز بصيغة غير سليمة.")
                    if not obj_text:
                        continue
                    if not isinstance(contents, list):
                        raise ValueError("قائمة المحتويات غير سليمة.")
                    obj_cur = con.execute(
                        "INSERT INTO objectif_specifique(objectif_specifique, id_moukawa_kifaya, description) VALUES(?,?,?)",
                        (obj_text, new_mouk_id, obj_desc),
                    )
                    new_obj_id = obj_cur.lastrowid
                    for cnt in contents:
                        if isinstance(cnt, str):
                            cnt_text, cnt_desc = cnt.strip(), ""
                        elif isinstance(cnt, dict):
                            cnt_text = str(cnt.get("content", "")).strip()
                            cnt_desc = str(cnt.get("description", "") or "")
                        else:
                            raise ValueError("يوجد محتوى بصيغة غير سليمة.")
                        if cnt_text:
                            con.execute(
                                "INSERT INTO content(content, id_objectif_specifique, description) VALUES(?,?,?)",
                                (cnt_text, new_obj_id, cnt_desc),
                            )
            con.commit()
        return True


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setLayoutDirection(Qt.RightToLeft)
    arabic_dialog_translator = ArabicDialogTranslator()
    app.installTranslator(arabic_dialog_translator)

    custom_font_family = load_custom_font()
    app.setFont(QFont(custom_font_family, 10))

    style_file = APP_DIR / "style_1.qss"
    if style_file.exists():
        with open(style_file, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())

    try:
        window = SchoolPlanner()
    except sqlite3.Error as exc:
        QMessageBox.critical(
            None,
            "تعذّر فتح قاعدة البيانات",
            f"تعذّر فتح قاعدة البيانات:\n{exc}",
        )
        sys.exit(1)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
