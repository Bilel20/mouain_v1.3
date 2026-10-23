# -*- coding: utf-8 -*-
"""Extracted module database."""
import re
import secrets
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from app_config import ACTIVITIES_BY_MAJAL, DB_PATH, SCHOOL_INFO_COLUMNS
from database_connection import connect_database
import sqlite3


PLAN_ACTIVITY_ALIASES = {
    "تواصل شفوي": ("التواصل الشفوي",),
    "قراءة": ("القراءة",),
    "إنتاج كتابي": ("الإنتاج الكتابي",),
    "استعمال قواعد اللغة": ("استعمال قواعد اللغة والرسم",),
}


def duration_minutes(value):
    match = re.fullmatch(
        r"\s*(\d+)\s*(?:دقيقة|دقائق)?\s*", str(value or "")
    )
    return int(match.group(1)) if match else None


def required_activity_properties(learning_mode):
    return {
        "منهجي": ("المحتوى",),
        "إدماج": ("الكفايات المستهدفة",),
        "تقييم": ("الكفايات المستهدفة", "الأداء المنتظر"),
        "دعم وعلاج": (),
    }.get(str(learning_mode or "").strip(), ())


def activity_completion_issues(details, learning_mode):
    filled_keys = {
        str(detail[1] or "").strip()
        for detail in details
        if str(detail[2] or "").strip()
    }
    return [
        f"الخاصية «{key}» غير موجودة أو قيمتها فارغة"
        for key in required_activity_properties(learning_mode)
        if key not in filled_keys
    ]


def lesson_completion_issues(lesson):
    activities = lesson.get("activities", {})
    issues = []
    if not activities:
        issues.append("لا يوجد نشاط واحد على الأقل")

    lesson_minutes = duration_minutes(lesson.get("time"))
    activity_minutes = 0
    learning_mode = lesson.get("learning_mode", "")
    for activity in activities.values():
        duration = duration_minutes(activity.get("time"))
        if duration is None:
            issues.append(
                f"مدة النشاط «{activity['title']}» غير صالحة"
            )
        else:
            activity_minutes += duration
        issues.extend(
            f"النشاط «{activity['title']}»: {detail_issue}"
            for detail_issue in activity_completion_issues(
                activity.get("details", []), learning_mode
            )
        )

    if lesson_minutes is None:
        issues.append("مدة الحصة غير صالحة")
    elif activities and activity_minutes != lesson_minutes:
        issues.append(
            f"مجموع مدد الأنشطة {activity_minutes} دقيقة، "
            f"ومدة الحصة {lesson_minutes} دقيقة"
        )
    return issues


def calculate_plan_end_date(
    start_date, lesson_count, weekdays, holiday_dates
):
    """Return the final lesson date for a weekly weekday/count schedule."""
    start = date.fromisoformat(str(start_date))
    count = int(lesson_count)
    if isinstance(weekdays, dict):
        lessons_per_day = {
            int(day): int(daily_count)
            for day, daily_count in weekdays.items()
        }
    else:
        lessons_per_day = {int(day): 1 for day in weekdays}
    if count < 1:
        raise ValueError("عدد الحصص يجب أن يكون أكبر من صفر.")
    if not lessons_per_day or any(
        day < 0 or day > 6 or daily_count < 1
        for day, daily_count in lessons_per_day.items()
    ):
        raise ValueError("يجب اختيار يوم تدريس واحد على الأقل.")

    holidays = set()
    for holiday in holiday_dates:
        if isinstance(holiday, (tuple, list)):
            first_day, last_day = holiday
        else:
            first_day = last_day = holiday
        first_day = date.fromisoformat(str(first_day))
        last_day = date.fromisoformat(str(last_day or first_day))
        if last_day < first_day:
            raise ValueError("تاريخ نهاية العطلة يجب ألا يسبق تاريخ بدايتها.")
        holidays.update(
            date.fromordinal(day)
            for day in range(first_day.toordinal(), last_day.toordinal() + 1)
        )
    current = start
    scheduled = 0
    while True:
        if current.weekday() in lessons_per_day and current not in holidays:
            scheduled += lessons_per_day[current.weekday()]
            if scheduled == count:
                return current.isoformat()
            if scheduled > count:
                return current.isoformat()
        current = current.fromordinal(current.toordinal() + 1)


class Database:
    def __init__(self, path=DB_PATH):
        self.path = str(path)
        self.init_db()

    @staticmethod
    def _normalized_plan_activity(activity_name):
        name = str(activity_name or "").strip().casefold()
        for canonical, aliases in PLAN_ACTIVITY_ALIASES.items():
            if name == canonical.casefold() or name in {
                alias.casefold() for alias in aliases
            }:
                return canonical.casefold()
        return name

    def connect(self):
        # إعادة استعمال العبارات وتقليل انتظار القفل عند فتح أكثر من نافذة.
        return connect_database(self.path)

    @contextmanager
    def managed_connection(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def init_db(self):
        with self.managed_connection() as c:
            self._backup_before_normalization(c)
            # WAL يحسن التوازي بين القراءة والكتابة دون تغيير بنية البيانات.
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS ann_scolaire (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom_annee TEXT NOT NULL DEFAULT '2026-2027',
                nom_ecole TEXT DEFAULT '',
                nom_enseignant TEXT DEFAULT '',
                nom_inspecteur TEXT DEFAULT '',
                grade TEXT DEFAULT '',
                nom_assistant_pedagogique TEXT DEFAULT '',
                diplomes_scientifiques TEXT DEFAULT '',
                date_recrutement TEXT DEFAULT '',
                date_titularisation TEXT DEFAULT '',
                date_derniere_visite TEXT DEFAULT '',
                note_pedagogique TEXT DEFAULT '',
                resume_rapport_visite TEXT DEFAULT '',
                langue TEXT DEFAULT '',
                telephone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                situation_familiale TEXT DEFAULT '',
                nombre_enfants INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS day_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ann_scolaire_id INTEGER NOT NULL,
                UNIQUE(date, ann_scolaire_id),
                FOREIGN KEY(ann_scolaire_id) REFERENCES ann_scolaire(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS hessa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                classe_id INTEGER NOT NULL,
                majal_id INTEGER,
                heure TEXT DEFAULT '',
                time TEXT DEFAULT '',
                wehda_number INTEGER DEFAULT 1,
                hessa_number INTEGER DEFAULT 1,
                learning_mode_id INTEGER,
                day_data_id INTEGER NOT NULL,
                FOREIGN KEY(classe_id) REFERENCES classe(id),
                FOREIGN KEY(majal_id) REFERENCES majal(id) ON DELETE RESTRICT,
                FOREIGN KEY(learning_mode_id) REFERENCES learning_mode(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(day_data_id) REFERENCES day_data(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS nachat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                time TEXT DEFAULT '',
                hessa_id INTEGER NOT NULL,
                FOREIGN KEY(hessa_id) REFERENCES hessa(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS nachat_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_name TEXT NOT NULL,
                value_text TEXT DEFAULT '',
                nachat_id INTEGER NOT NULL,
                FOREIGN KEY(nachat_id) REFERENCES nachat(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS level (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS classe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                level_id INTEGER NOT NULL,
                FOREIGN KEY(level_id) REFERENCES level(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS majal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS learning_mode (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS competence_majal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competence TEXT NOT NULL,
                id_majal INTEGER NOT NULL,
                id_level INTEGER NOT NULL,
                FOREIGN KEY(id_majal) REFERENCES majal(id) ON DELETE CASCADE,
                FOREIGN KEY(id_level) REFERENCES level(id) ON DELETE CASCADE,
                UNIQUE(competence, id_majal, id_level)
            );

            CREATE TABLE IF NOT EXISTS competence_matiere (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matiere TEXT NOT NULL,
                competence TEXT NOT NULL,
                id_competence_majal INTEGER NOT NULL,
                FOREIGN KEY(id_competence_majal) REFERENCES competence_majal(id) ON DELETE CASCADE,
                UNIQUE(matiere, competence, id_competence_majal)
            );

            CREATE TABLE IF NOT EXISTS moukawan_kifaya (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competence TEXT NOT NULL,
                id_competence_matiere INTEGER NOT NULL,
                FOREIGN KEY(id_competence_matiere) REFERENCES competence_matiere(id) ON DELETE CASCADE,
                UNIQUE(competence, id_competence_matiere)
            );

            CREATE TABLE IF NOT EXISTS objectif_specifique (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                objectif_specifique TEXT NOT NULL,
                id_moukawa_kifaya INTEGER NOT NULL,
                description TEXT DEFAULT '',
                FOREIGN KEY(id_moukawa_kifaya) REFERENCES moukawan_kifaya(id) ON DELETE CASCADE,
                UNIQUE(objectif_specifique, id_moukawa_kifaya)
            );

            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                id_objectif_specifique INTEGER NOT NULL,
                description TEXT DEFAULT '',
                FOREIGN KEY(id_objectif_specifique) REFERENCES objectif_specifique(id) ON DELETE CASCADE,
                UNIQUE(content, id_objectif_specifique)
            );

            CREATE TABLE IF NOT EXISTS concept (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                content TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS official_holiday (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                holiday_date TEXT NOT NULL UNIQUE,
                end_date TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS teaching_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                classe_id INTEGER NOT NULL,
                majal_id INTEGER NOT NULL,
                competence_majal_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                lesson_count INTEGER NOT NULL CHECK (lesson_count > 0),
                lesson_duration_minutes INTEGER NOT NULL DEFAULT 60
                    CHECK (lesson_duration_minutes IN (30, 60, 120)),
                methodical_lesson_count INTEGER NOT NULL DEFAULT 0,
                integration_lesson_count INTEGER NOT NULL DEFAULT 0,
                evaluation_lesson_count INTEGER NOT NULL DEFAULT 0,
                support_lesson_count INTEGER NOT NULL DEFAULT 0,
                plan_number INTEGER NOT NULL DEFAULT 0,
                plan_number_assigned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(classe_id) REFERENCES classe(id) ON DELETE CASCADE,
                FOREIGN KEY(majal_id) REFERENCES majal(id) ON DELETE RESTRICT,
                FOREIGN KEY(competence_majal_id) REFERENCES competence_majal(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_property (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                key_name TEXT NOT NULL,
                value_text TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS teaching_plan_weekday (
                plan_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                lesson_count INTEGER NOT NULL DEFAULT 1
                    CHECK (lesson_count > 0),
                PRIMARY KEY(plan_id, weekday),
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                activity_name TEXT NOT NULL,
                competence_matiere_id INTEGER,
                competence_text TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE,
                FOREIGN KEY(competence_matiere_id) REFERENCES competence_matiere(id) ON DELETE RESTRICT,
                UNIQUE(plan_id, activity_name, competence_text)
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_item_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_item_id INTEGER NOT NULL,
                competence_component_id INTEGER,
                competence_component_text TEXT NOT NULL,
                objective_id INTEGER,
                objective_text TEXT NOT NULL DEFAULT '',
                content_id INTEGER,
                content_text TEXT NOT NULL,
                FOREIGN KEY(plan_item_id) REFERENCES teaching_plan_item(id) ON DELETE CASCADE,
                FOREIGN KEY(competence_component_id) REFERENCES moukawan_kifaya(id) ON DELETE RESTRICT,
                FOREIGN KEY(objective_id) REFERENCES objectif_specifique(id) ON DELETE RESTRICT,
                FOREIGN KEY(content_id) REFERENCES content(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_mode_setting (
                plan_id INTEGER NOT NULL,
                learning_mode TEXT NOT NULL,
                expected_performance TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(plan_id, learning_mode),
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_mode_selection (
                plan_id INTEGER NOT NULL,
                learning_mode TEXT NOT NULL,
                plan_item_id INTEGER NOT NULL,
                objective_text TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(plan_id, learning_mode, plan_item_id, objective_text),
                FOREIGN KEY(plan_id, learning_mode)
                    REFERENCES teaching_plan_mode_setting(plan_id, learning_mode)
                    ON DELETE CASCADE,
                FOREIGN KEY(plan_item_id)
                    REFERENCES teaching_plan_item(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_mode_activity_selection (
                plan_id INTEGER NOT NULL,
                learning_mode TEXT NOT NULL,
                activity_name TEXT NOT NULL,
                objective_text TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(
                    plan_id, learning_mode, activity_name, objective_text
                ),
                FOREIGN KEY(plan_id, learning_mode)
                    REFERENCES teaching_plan_mode_setting(plan_id, learning_mode)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_integration_activity (
                plan_id INTEGER NOT NULL,
                activity_name TEXT NOT NULL,
                prompt_text TEXT NOT NULL DEFAULT '',
                template_text TEXT NOT NULL DEFAULT '',
                target_text TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(plan_id, activity_name),
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_integration_content (
                plan_id INTEGER NOT NULL,
                activity_name TEXT NOT NULL,
                content_text TEXT NOT NULL,
                PRIMARY KEY(plan_id, activity_name, content_text),
                FOREIGN KEY(plan_id, activity_name)
                    REFERENCES teaching_plan_integration_activity(
                        plan_id, activity_name
                    ) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_evaluation_activity (
                plan_id INTEGER NOT NULL,
                activity_name TEXT NOT NULL,
                prompt_text TEXT NOT NULL DEFAULT '',
                template_text TEXT NOT NULL DEFAULT '',
                target_text TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(plan_id, activity_name),
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_evaluation_content (
                plan_id INTEGER NOT NULL,
                activity_name TEXT NOT NULL,
                content_text TEXT NOT NULL,
                PRIMARY KEY(plan_id, activity_name, content_text),
                FOREIGN KEY(plan_id, activity_name)
                    REFERENCES teaching_plan_evaluation_activity(
                        plan_id, activity_name
                    ) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_support_activity (
                plan_id INTEGER NOT NULL,
                activity_name TEXT NOT NULL,
                target_criteria TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(plan_id, activity_name),
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                lesson_number INTEGER NOT NULL CHECK (lesson_number > 0),
                learning_mode TEXT NOT NULL,
                plan_item_id INTEGER,
                activity_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                lesson_goal TEXT NOT NULL DEFAULT '',
                specific_objective TEXT NOT NULL DEFAULT '',
                target_competencies TEXT NOT NULL DEFAULT '',
                evaluation TEXT NOT NULL DEFAULT '',
                expected_performance TEXT NOT NULL DEFAULT '',
                target_criteria TEXT NOT NULL DEFAULT '',
                demographic_concept TEXT NOT NULL DEFAULT '',
                health_concept TEXT NOT NULL DEFAULT '',
                life_skills TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER NOT NULL DEFAULT 60
                    CHECK (duration_minutes > 0),
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE,
                FOREIGN KEY(plan_item_id) REFERENCES teaching_plan_item(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS teaching_plan_activity_property (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER NOT NULL,
                key_name TEXT NOT NULL,
                value_text TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(activity_id) REFERENCES teaching_plan_activity(id) ON DELETE CASCADE
            );
            """)
            existing_columns = {
                row[1] for row in c.execute("PRAGMA table_info(ann_scolaire)")
            }
            new_columns = (
                ("nom_assistant_pedagogique", "TEXT DEFAULT ''"),
                ("diplomes_scientifiques", "TEXT DEFAULT ''"),
                ("date_recrutement", "TEXT DEFAULT ''"),
                ("date_titularisation", "TEXT DEFAULT ''"),
                ("date_derniere_visite", "TEXT DEFAULT ''"),
                ("note_pedagogique", "TEXT DEFAULT ''"),
                ("resume_rapport_visite", "TEXT DEFAULT ''"),
                ("langue", "TEXT DEFAULT ''"),
                ("telephone", "TEXT DEFAULT ''"),
                ("email", "TEXT DEFAULT ''"),
                ("situation_familiale", "TEXT DEFAULT ''"),
                ("nombre_enfants", "INTEGER NOT NULL DEFAULT 0"),
            )
            for column_name, column_definition in new_columns:
                if column_name not in existing_columns:
                    c.execute(
                        f"ALTER TABLE ann_scolaire ADD COLUMN "
                        f"{column_name} {column_definition}"
                    )
            holiday_columns = {
                row[1] for row in c.execute(
                    "PRAGMA table_info(official_holiday)"
                )
            }
            plan_columns = {
                row[1] for row in c.execute(
                    "PRAGMA table_info(teaching_plan)"
                )
            }
            activity_columns = {
                row[1] for row in c.execute(
                    "PRAGMA table_info(teaching_plan_activity)"
                )
            }
            if "duration_minutes" not in activity_columns:
                c.execute(
                    "ALTER TABLE teaching_plan_activity ADD COLUMN "
                    "duration_minutes INTEGER NOT NULL DEFAULT 60"
                )
            if "lesson_duration_minutes" not in plan_columns:
                c.execute(
                    "ALTER TABLE teaching_plan ADD COLUMN "
                    "lesson_duration_minutes INTEGER NOT NULL DEFAULT 60"
                )
                plan_columns.add("lesson_duration_minutes")
            mode_count_columns = (
                "methodical_lesson_count",
                "integration_lesson_count",
                "evaluation_lesson_count",
                "support_lesson_count",
            )
            for column_name in mode_count_columns:
                if column_name not in plan_columns:
                    c.execute(
                        "ALTER TABLE teaching_plan ADD COLUMN "
                        f"{column_name} INTEGER NOT NULL DEFAULT 0"
                    )
                    plan_columns.add(column_name)
            c.execute("""
                UPDATE teaching_plan
                SET methodical_lesson_count=lesson_count
                WHERE methodical_lesson_count + integration_lesson_count
                    + evaluation_lesson_count + support_lesson_count=0
            """)
            if "plan_number" not in plan_columns:
                c.execute(
                    "ALTER TABLE teaching_plan ADD COLUMN "
                    "plan_number INTEGER NOT NULL DEFAULT 0"
                )
                plan_ids = c.execute(
                    "SELECT id FROM teaching_plan ORDER BY id"
                ).fetchall()
                c.executemany(
                    "UPDATE teaching_plan SET plan_number=? WHERE id=?",
                    [
                        (number, row[0])
                        for number, row in enumerate(plan_ids, start=1)
                    ],
                )
                plan_columns.add("plan_number")
            if "plan_number_assigned" not in plan_columns:
                c.execute(
                    "ALTER TABLE teaching_plan ADD COLUMN "
                    "plan_number_assigned INTEGER NOT NULL DEFAULT 1"
                )
                plan_columns.add("plan_number_assigned")
            if "end_date" not in holiday_columns:
                c.execute(
                    "ALTER TABLE official_holiday "
                    "ADD COLUMN end_date TEXT NOT NULL DEFAULT ''"
                )
            c.execute(
                "UPDATE official_holiday SET end_date=holiday_date "
                "WHERE end_date=''"
            )

            c.executescript("""
            UPDATE classe
            SET level_id = (
                SELECT MIN(level.id) FROM level AS level
                WHERE level.name = (
                    SELECT duplicate.name FROM level AS duplicate
                    WHERE duplicate.id = classe.level_id
                )
            )
            WHERE level_id NOT IN (
                SELECT MIN(id) FROM level GROUP BY name
            );
            DELETE FROM level WHERE id NOT IN (
                SELECT MIN(id) FROM level GROUP BY name
            );
            DELETE FROM majal WHERE id NOT IN (
                SELECT MIN(id) FROM majal GROUP BY name
            );
            """)

            self._migrate_hessa_classes(c)
            self._migrate_normalized_tables(c)
            self._migrate_teaching_plan_activities(c)
            self._ensure_standalone_plan_activity_support(c)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_plan_activity_parent
                ON teaching_plan_activity(plan_id, lesson_number)
            """)

            # ترقية جدول concept القديم: إضافة قيد UNIQUE على name إن لم يكن موجوداً.
            concept_sql = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='concept'"
            ).fetchone()
            if concept_sql and "UNIQUE" not in (concept_sql[0] or "").upper():
                c.executescript("""
                CREATE TABLE concept_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    content TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                );
                INSERT OR IGNORE INTO concept_new(id, name, content, created_at)
                    SELECT id, name, content, created_at FROM concept;
                DROP TABLE concept;
                ALTER TABLE concept_new RENAME TO concept;
                """)

            # فهارس تخدم الاستعلامات المتكررة في دفتر الدروس والبرامج الرسمية.
            c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_day_data_year_date ON day_data(ann_scolaire_id, date);
            CREATE INDEX IF NOT EXISTS idx_hessa_day ON hessa(day_data_id);
            CREATE INDEX IF NOT EXISTS idx_hessa_majal ON hessa(majal_id);
            CREATE INDEX IF NOT EXISTS idx_hessa_learning_mode
                ON hessa(learning_mode_id);
            CREATE INDEX IF NOT EXISTS idx_nachat_hessa ON nachat(hessa_id);
            CREATE INDEX IF NOT EXISTS idx_nachat_title ON nachat(title);
            CREATE INDEX IF NOT EXISTS idx_nachat_detail_parent_key ON nachat_detail(nachat_id, key_name);
            CREATE INDEX IF NOT EXISTS idx_competence_majal_filter ON competence_majal(id_level, id_majal);
            CREATE INDEX IF NOT EXISTS idx_competence_matiere_parent ON competence_matiere(id_competence_majal);
            CREATE INDEX IF NOT EXISTS idx_moukawan_parent ON moukawan_kifaya(id_competence_matiere);
            CREATE INDEX IF NOT EXISTS idx_objectif_parent ON objectif_specifique(id_moukawa_kifaya);
            CREATE INDEX IF NOT EXISTS idx_content_parent ON content(id_objectif_specifique);
            CREATE INDEX IF NOT EXISTS idx_concept_name ON concept(name);
            CREATE INDEX IF NOT EXISTS idx_plan_class_domain ON teaching_plan(classe_id, majal_id);
            CREATE INDEX IF NOT EXISTS idx_plan_property_parent ON teaching_plan_property(plan_id);
            CREATE INDEX IF NOT EXISTS idx_plan_item_parent ON teaching_plan_item(plan_id);
            CREATE INDEX IF NOT EXISTS idx_plan_item_detail_parent ON teaching_plan_item_detail(plan_item_id);
            CREATE INDEX IF NOT EXISTS idx_plan_activity_parent ON teaching_plan_activity(plan_id, lesson_number);
            CREATE INDEX IF NOT EXISTS idx_plan_activity_property_parent ON teaching_plan_activity_property(activity_id);
            """)
            self._ensure_analytics_revision_tracking(c)

            current_year_name = self.school_year_name_for_date()
            row = c.execute(
                """SELECT id FROM ann_scolaire
                   WHERE nom_annee=? ORDER BY id DESC LIMIT 1""",
                (current_year_name,),
            ).fetchone()
            if not row:
                row = c.execute(
                    "SELECT id FROM ann_scolaire ORDER BY id LIMIT 1"
                ).fetchone()
            if row:
                self.year_id = row[0]
            else:
                cur = c.execute(
                    "INSERT INTO ann_scolaire(nom_annee) VALUES(?)",
                    (current_year_name,),
                )
                self.year_id = cur.lastrowid

    def _backup_before_normalization(self, connection):
        if self.path == ":memory:":
            return
        database_path = Path(self.path).resolve()
        if not database_path.is_file():
            return
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not tables.intersection({"hessa", "teaching_plan"}):
            return
        hessa_columns = (
            {row[1] for row in connection.execute("PRAGMA table_info(hessa)")}
            if "hessa" in tables else set()
        )
        plan_columns = (
            {row[1] for row in connection.execute(
                "PRAGMA table_info(teaching_plan)"
            )}
            if "teaching_plan" in tables else set()
        )
        needs_backup = (
            (
                ("majal" in hessa_columns or "namat_taallom" in hessa_columns)
                and (
                    "majal_id" not in hessa_columns
                    or "learning_mode_id" not in hessa_columns
                )
            )
            or "weekdays" in plan_columns
            or "plan_number_scope" in plan_columns
            or "plan_number_activity_name" in plan_columns
        )
        if not needs_backup:
            return

        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = database_path.with_name(
            f"{database_path.name}.pre-normalization-{stamp}-"
            f"{secrets.token_hex(4)}.bak"
        )
        backup = sqlite3.connect(str(backup_path), timeout=10)
        try:
            connection.backup(backup)
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"Pre-migration backup failed integrity check: {integrity}"
                )
        except Exception:
            backup.close()
            if backup_path.exists():
                backup_path.unlink()
            raise
        backup.close()

    @staticmethod
    def _migrate_normalized_tables(connection):
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS learning_mode (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS teaching_plan_weekday (
                    plan_id INTEGER NOT NULL,
                    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                    lesson_count INTEGER NOT NULL DEFAULT 1
                        CHECK (lesson_count > 0),
                    PRIMARY KEY(plan_id, weekday),
                    FOREIGN KEY(plan_id) REFERENCES teaching_plan(id)
                        ON DELETE CASCADE
                )
            """)
            weekday_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(teaching_plan_weekday)"
                )
            }
            if "lesson_count" not in weekday_columns:
                connection.execute("""
                    ALTER TABLE teaching_plan_weekday
                    ADD COLUMN lesson_count INTEGER NOT NULL DEFAULT 1
                        CHECK (lesson_count > 0)
                """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_majal_name_unique
                ON majal(name)
            """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_mode_name_unique
                ON learning_mode(name)
            """)

            plan_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(teaching_plan)"
                )
            }
            if "weekdays" in plan_columns:
                if sqlite3.sqlite_version_info < (3, 35, 0):
                    raise RuntimeError(
                        "Database normalization requires SQLite 3.35 or newer."
                    )
                legacy_weekdays = connection.execute(
                    "SELECT id, weekdays FROM teaching_plan ORDER BY id"
                ).fetchall()
                weekday_rows = []
                for plan_id, serialized_days in legacy_weekdays:
                    try:
                        days = sorted({
                            int(day) for day in str(serialized_days or "").split(",")
                            if day.strip()
                        })
                    except ValueError as error:
                        raise ValueError(
                            f"Teaching plan {plan_id} has invalid weekday data."
                        ) from error
                    if any(day < 0 or day > 6 for day in days):
                        raise ValueError(
                            f"Teaching plan {plan_id} has invalid weekday data."
                        )
                    weekday_rows.extend(
                        (plan_id, day) for day in days
                    )
                connection.executemany("""
                    INSERT OR IGNORE INTO teaching_plan_weekday(
                        plan_id, weekday
                    ) VALUES(?, ?)
                """, weekday_rows)
                connection.execute(
                    "ALTER TABLE teaching_plan DROP COLUMN weekdays"
                )

            obsolete_number_columns = (
                "plan_number_scope",
                "plan_number_activity_name",
            )
            if any(name in plan_columns for name in obsolete_number_columns):
                if sqlite3.sqlite_version_info < (3, 35, 0):
                    raise RuntimeError(
                        "Teaching-plan number migration requires SQLite 3.35 or newer."
                    )
                connection.execute(
                    "DROP INDEX IF EXISTS idx_teaching_plan_scoped_number"
                )
                connection.execute(
                    "DROP INDEX IF EXISTS idx_teaching_plan_number"
                )

            numbered_plans = connection.execute("""
                SELECT id, classe_id, title, plan_number
                FROM teaching_plan
                WHERE plan_number_assigned=1
                ORDER BY classe_id, title COLLATE NOCASE, plan_number,
                    created_at, id
            """).fetchall()
            used_numbers = {}
            for plan_id, class_id, title, number in numbered_plans:
                key = (class_id, str(title or "").strip().casefold())
                used = used_numbers.setdefault(key, set())
                number = int(number)
                if number < 1 or number in used:
                    number = max(used, default=0) + 1
                    while number in used:
                        number += 1
                    connection.execute(
                        "UPDATE teaching_plan SET plan_number=? WHERE id=?",
                        (number, plan_id),
                    )
                used.add(number)

            for column_name in obsolete_number_columns:
                if column_name in plan_columns:
                    connection.execute(
                        "ALTER TABLE teaching_plan DROP COLUMN "
                        f"{column_name}"
                    )
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_teaching_plan_class_title_number
                ON teaching_plan(
                    classe_id, title COLLATE NOCASE, plan_number
                )
                WHERE plan_number_assigned=1
            """)

            hessa_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(hessa)")
            }
            if (
                ("majal" in hessa_columns or "namat_taallom" in hessa_columns)
                and (
                    "majal_id" not in hessa_columns
                    or "learning_mode_id" not in hessa_columns
                )
            ):
                legacy_rows = connection.execute("""
                    SELECT id,
                        {majal_column},
                        {mode_column}
                    FROM hessa ORDER BY id
                """.format(
                    majal_column=(
                        "majal" if "majal" in hessa_columns else "NULL"
                    ),
                    mode_column=(
                        "namat_taallom"
                        if "namat_taallom" in hessa_columns else "NULL"
                    ),
                )).fetchall()
                for _lesson_id, majal_name, mode_name in legacy_rows:
                    if str(majal_name or "").strip():
                        connection.execute(
                            "INSERT OR IGNORE INTO majal(name) VALUES(?)",
                            (str(majal_name).strip(),),
                        )
                    if str(mode_name or "").strip():
                        connection.execute(
                            "INSERT OR IGNORE INTO learning_mode(name) VALUES(?)",
                            (str(mode_name).strip(),),
                        )

                connection.execute("DROP TABLE IF EXISTS hessa_normalized")
                connection.execute("""
                    CREATE TABLE hessa_normalized (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        classe_id INTEGER NOT NULL,
                        majal_id INTEGER,
                        heure TEXT DEFAULT '',
                        time TEXT DEFAULT '',
                        wehda_number INTEGER DEFAULT 1,
                        hessa_number INTEGER DEFAULT 1,
                        learning_mode_id INTEGER,
                        day_data_id INTEGER NOT NULL,
                        FOREIGN KEY(classe_id) REFERENCES classe(id),
                        FOREIGN KEY(majal_id) REFERENCES majal(id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY(learning_mode_id) REFERENCES learning_mode(id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY(day_data_id) REFERENCES day_data(id)
                            ON DELETE CASCADE
                    )
                """)
                majal_expression = (
                    "trim(COALESCE(h.majal, ''))"
                    if "majal" in hessa_columns else "''"
                )
                mode_expression = (
                    "trim(COALESCE(h.namat_taallom, ''))"
                    if "namat_taallom" in hessa_columns else "''"
                )
                connection.execute("""
                    INSERT INTO hessa_normalized(
                        id, classe_id, majal_id, heure, time, wehda_number,
                        hessa_number, learning_mode_id, day_data_id
                    )
                    SELECT h.id, h.classe_id,
                        (SELECT m.id FROM majal m
                         WHERE m.name={majal_expression}
                         ORDER BY m.id LIMIT 1),
                        h.heure, h.time, h.wehda_number, h.hessa_number,
                        (SELECT lm.id FROM learning_mode lm
                         WHERE lm.name={mode_expression}
                         ORDER BY lm.id LIMIT 1),
                        h.day_data_id
                    FROM hessa h
                """.format(
                    majal_expression=majal_expression,
                    mode_expression=mode_expression,
                ))
                connection.execute("DROP TABLE hessa")
                connection.execute(
                    "ALTER TABLE hessa_normalized RENAME TO hessa"
                )

            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _migrate_teaching_plan_activities(connection):
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(teaching_plan_activity)"
            )
        }
        if "competence_matiere_id" not in columns:
            return

        activities = connection.execute("""
            SELECT id, plan_id, lesson_number, learning_mode,
                competence_matiere_id, content, lesson_goal,
                specific_objective, target_competencies, evaluation,
                expected_performance, target_criteria, demographic_concept,
                health_concept, life_skills
            FROM teaching_plan_activity ORDER BY id
        """).fetchall()
        properties = connection.execute("""
            SELECT activity_id, key_name, value_text
            FROM teaching_plan_activity_property ORDER BY id
        """).fetchall()
        item_ids = {}
        for row in activities:
            plan_id, competence_id = row[1], row[4]
            item_key = (plan_id, competence_id)
            if item_key in item_ids:
                continue
            competence = connection.execute("""
                SELECT matiere, competence FROM competence_matiere WHERE id=?
            """, (competence_id,)).fetchone()
            if competence is None:
                raise sqlite3.IntegrityError(
                    f"Missing competence_matiere row {competence_id} "
                    f"required by teaching plan activity {row[0]}."
                )
            cursor = connection.execute("""
                INSERT OR IGNORE INTO teaching_plan_item(
                    plan_id, activity_name, competence_matiere_id, competence_text
                ) VALUES(?, ?, ?, ?)
            """, (plan_id, competence[0], competence_id, competence[1]))
            item = connection.execute("""
                SELECT id FROM teaching_plan_item
                WHERE plan_id=? AND activity_name=?
                    AND competence_matiere_id=?
            """, (plan_id, competence[0], competence_id)).fetchone()
            item_ids[item_key] = item[0] if item else cursor.lastrowid

        connection.execute("""
            CREATE TABLE teaching_plan_activity_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                lesson_number INTEGER NOT NULL CHECK (lesson_number > 0),
                learning_mode TEXT NOT NULL,
                plan_item_id INTEGER NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                lesson_goal TEXT NOT NULL DEFAULT '',
                specific_objective TEXT NOT NULL DEFAULT '',
                target_competencies TEXT NOT NULL DEFAULT '',
                evaluation TEXT NOT NULL DEFAULT '',
                expected_performance TEXT NOT NULL DEFAULT '',
                target_criteria TEXT NOT NULL DEFAULT '',
                demographic_concept TEXT NOT NULL DEFAULT '',
                health_concept TEXT NOT NULL DEFAULT '',
                life_skills TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE,
                FOREIGN KEY(plan_item_id) REFERENCES teaching_plan_item(id) ON DELETE RESTRICT
            )
        """)
        for row in activities:
            connection.execute("""
                INSERT INTO teaching_plan_activity_new(
                    id, plan_id, lesson_number, learning_mode, plan_item_id,
                    content, lesson_goal, specific_objective, target_competencies,
                    evaluation, expected_performance, target_criteria,
                    demographic_concept, health_concept, life_skills
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row[0], row[1], row[2], row[3], item_ids[(row[1], row[4])],
                *row[5:],
            ))
        connection.execute("DROP TABLE teaching_plan_activity")
        connection.execute(
            "ALTER TABLE teaching_plan_activity_new "
            "RENAME TO teaching_plan_activity"
        )
        if properties:
            connection.executemany("""
                INSERT INTO teaching_plan_activity_property(
                    activity_id, key_name, value_text
                ) VALUES(?, ?, ?)
            """, properties)

    @staticmethod
    def _ensure_standalone_plan_activity_support(connection):
        table_info = connection.execute(
            "PRAGMA table_info(teaching_plan_activity)"
        ).fetchall()
        columns = {row[1]: row for row in table_info}
        plan_item_column = columns.get("plan_item_id")
        if (
            plan_item_column is not None
            and not plan_item_column[3]
            and "activity_name" in columns
        ):
            return

        properties = connection.execute("""
            SELECT id, activity_id, key_name, value_text
            FROM teaching_plan_activity_property ORDER BY id
        """).fetchall()
        has_activity_name = "activity_name" in columns
        activity_name_sql = (
            "COALESCE(NULLIF(a.activity_name, ''), i.activity_name, '')"
            if has_activity_name else "COALESCE(i.activity_name, '')"
        )
        activities = connection.execute(f"""
            SELECT a.id, a.plan_id, a.lesson_number, a.learning_mode,
                a.plan_item_id, {activity_name_sql},
                a.content, a.lesson_goal, a.specific_objective,
                a.target_competencies, a.evaluation, a.expected_performance,
                a.target_criteria, a.demographic_concept, a.health_concept,
                a.life_skills, a.duration_minutes
            FROM teaching_plan_activity a
            LEFT JOIN teaching_plan_item i ON i.id=a.plan_item_id
            ORDER BY a.id
        """).fetchall()
        connection.execute("""
            CREATE TABLE teaching_plan_activity_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                lesson_number INTEGER NOT NULL CHECK (lesson_number > 0),
                learning_mode TEXT NOT NULL,
                plan_item_id INTEGER,
                activity_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                lesson_goal TEXT NOT NULL DEFAULT '',
                specific_objective TEXT NOT NULL DEFAULT '',
                target_competencies TEXT NOT NULL DEFAULT '',
                evaluation TEXT NOT NULL DEFAULT '',
                expected_performance TEXT NOT NULL DEFAULT '',
                target_criteria TEXT NOT NULL DEFAULT '',
                demographic_concept TEXT NOT NULL DEFAULT '',
                health_concept TEXT NOT NULL DEFAULT '',
                life_skills TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER NOT NULL DEFAULT 60
                    CHECK (duration_minutes > 0),
                FOREIGN KEY(plan_id) REFERENCES teaching_plan(id) ON DELETE CASCADE,
                FOREIGN KEY(plan_item_id) REFERENCES teaching_plan_item(id) ON DELETE RESTRICT
            )
        """)
        connection.executemany("""
            INSERT INTO teaching_plan_activity_new(
                id, plan_id, lesson_number, learning_mode, plan_item_id,
                activity_name, content, lesson_goal, specific_objective,
                target_competencies, evaluation, expected_performance,
                target_criteria, demographic_concept, health_concept,
                life_skills, duration_minutes
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, activities)
        connection.execute("DROP TABLE teaching_plan_activity")
        connection.execute(
            "ALTER TABLE teaching_plan_activity_new "
            "RENAME TO teaching_plan_activity"
        )
        if properties:
            connection.executemany("""
                INSERT INTO teaching_plan_activity_property(
                    id, activity_id, key_name, value_text
                ) VALUES(?, ?, ?, ?)
            """, properties)

    @staticmethod
    def _ensure_analytics_revision_tracking(connection):
        connection.execute("""
            CREATE TABLE IF NOT EXISTS app_data_revision (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                revision INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute(
            "INSERT OR IGNORE INTO app_data_revision(id, revision) VALUES(1, 0)"
        )
        tracked_tables = (
            "ann_scolaire", "day_data", "hessa", "classe", "level",
            "majal", "learning_mode", "nachat", "nachat_detail",
        )
        for table in tracked_tables:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                trigger_name = (
                    f"app_revision_{table}_{operation.lower()}"
                )
                connection.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    AFTER {operation} ON {table}
                    BEGIN
                        UPDATE app_data_revision
                        SET revision = revision + 1 WHERE id = 1;
                    END
                """)

    def analytics_revision(self):
        with self.connect() as c:
            row = c.execute(
                "SELECT revision FROM app_data_revision WHERE id=1"
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _infer_level_id(connection, class_name):
        digits = str(class_name or "").translate(
            str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
        )
        found = re.search(r"\d+", digits)
        if not found:
            raise ValueError(
                f"تعذر تحديد مستوى القسم «{class_name}». "
                "يجب اختيار المستوى عند تسجيل الأقسام الجديدة."
            )
        level_number = int(found.group())
        level_names = {
            1: "السنة الأولى",
            2: "السنة الثانية",
            3: "السنة الثالثة",
            4: "السنة الرابعة",
            5: "السنة الخامسة",
            6: "السنة السادسة",
        }
        level_name = level_names.get(level_number)
        if not level_name:
            raise ValueError(
                f"لا يوجد مستوى دراسي معروف للقسم «{class_name}»."
            )
        row = connection.execute(
            "SELECT id FROM level WHERE name=?", (level_name,)
        ).fetchone()
        if row:
            return row[0]
        cursor = connection.execute(
            "INSERT INTO level(name) VALUES(?)", (level_name,)
        )
        return cursor.lastrowid

    @classmethod
    def resolve_class_id(cls, connection, class_name, level_id=None):
        class_name = str(class_name or "").strip()
        if not class_name:
            raise ValueError("اسم القسم مطلوب.")

        if level_id is None:
            row = connection.execute(
                "SELECT id FROM classe WHERE name=?", (class_name,)
            ).fetchone()
            if row:
                return row[0]
            level_id = cls._infer_level_id(connection, class_name)
        elif isinstance(level_id, str) and not level_id.isdigit():
            row = connection.execute(
                "SELECT id FROM level WHERE name=?", (level_id.strip(),)
            ).fetchone()
            if not row:
                raise ValueError(f"المستوى الدراسي «{level_id}» غير مسجل.")
            level_id = row[0]

        existing = connection.execute(
            "SELECT id, level_id FROM classe WHERE name=?", (class_name,)
        ).fetchone()
        if existing:
            if existing[1] != int(level_id):
                raise ValueError(
                    f"القسم «{class_name}» مسجل بالفعل ضمن مستوى دراسي آخر."
                )
            return existing[0]
        cursor = connection.execute(
            "INSERT INTO classe(name, level_id) VALUES(?, ?)",
            (class_name, int(level_id)),
        )
        return cursor.lastrowid

    @staticmethod
    def resolve_lesson_lookup_ids(connection, majal_name, learning_mode_name):
        majal_name = str(majal_name or "").strip()
        learning_mode_name = str(learning_mode_name or "").strip()
        majal_id = None
        learning_mode_id = None
        if majal_name:
            connection.execute(
                "INSERT OR IGNORE INTO majal(name) VALUES(?)", (majal_name,)
            )
            majal_id = connection.execute(
                "SELECT id FROM majal WHERE name=?", (majal_name,)
            ).fetchone()[0]
        if learning_mode_name:
            connection.execute(
                "INSERT OR IGNORE INTO learning_mode(name) VALUES(?)",
                (learning_mode_name,),
            )
            learning_mode_id = connection.execute(
                "SELECT id FROM learning_mode WHERE name=?",
                (learning_mode_name,),
            ).fetchone()[0]
        return majal_id, learning_mode_id

    @classmethod
    def _migrate_hessa_classes(cls, connection):
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(hessa)")
        }
        if "classe" not in columns:
            return

        old_rows = connection.execute(
            "SELECT id, classe FROM hessa ORDER BY id"
        ).fetchall()
        class_ids = {}
        for _, class_name in old_rows:
            normalized_name = str(class_name or "").strip()
            if normalized_name and normalized_name not in class_ids:
                class_ids[normalized_name] = cls.resolve_class_id(
                    connection, normalized_name
                )

        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE hessa_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    classe_id INTEGER NOT NULL,
                    majal TEXT DEFAULT '',
                    heure TEXT DEFAULT '',
                    time TEXT DEFAULT '',
                    wehda_number INTEGER DEFAULT 1,
                    hessa_number INTEGER DEFAULT 1,
                    namat_taallom TEXT DEFAULT '',
                    day_data_id INTEGER NOT NULL,
                    FOREIGN KEY(classe_id) REFERENCES classe(id),
                    FOREIGN KEY(day_data_id) REFERENCES day_data(id) ON DELETE CASCADE
                )
            """)
            connection.executemany("""
                INSERT INTO hessa_new(
                    id, classe_id, majal, heure, time, wehda_number,
                    hessa_number, namat_taallom, day_data_id
                )
                SELECT id, ?, majal, heure, time, wehda_number,
                    hessa_number, namat_taallom, day_data_id
                FROM hessa WHERE id=?
            """, [
                (class_ids.get(str(class_name or "").strip()), lesson_id)
                for lesson_id, class_name in old_rows
            ])
            connection.execute("DROP TABLE hessa")
            connection.execute("ALTER TABLE hessa_new RENAME TO hessa")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        violations = [
            violation
            for violation in connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violation[2] in {"classe", "hessa"}
        ]
        if violations:
            raise sqlite3.IntegrityError(
                f"Class migration left foreign-key violations: {violations}"
            )

    @staticmethod
    def school_year_name_for_date(current_date=None):
        current_date = current_date or date.today()
        start_year = (
            current_date.year if current_date.month >= 7
            else current_date.year - 1
        )
        return f"{start_year}-{start_year + 1}"

    def school_years(self):
        with self.connect() as c:
            return c.execute(
                "SELECT id, nom_annee FROM ann_scolaire ORDER BY nom_annee DESC, id DESC"
            ).fetchall()

    def current_school_year_id(self):
        with self.connect() as c:
            row = c.execute(
                """SELECT id FROM ann_scolaire
                   WHERE nom_annee=? ORDER BY id DESC LIMIT 1""",
                (self.school_year_name_for_date(),),
            ).fetchone()
            return row[0] if row else None

    def create_school_year(self, values):
        if len(values) != len(SCHOOL_INFO_COLUMNS):
            raise ValueError(
                f"Expected {len(SCHOOL_INFO_COLUMNS)} school-info values, "
                f"received {len(values)}."
            )
        values = list(values)
        values[0] = str(values[0] or "").strip()
        if not values[0]:
            raise ValueError("اسم السنة الدراسية مطلوب.")

        columns = ", ".join(SCHOOL_INFO_COLUMNS)
        placeholders = ", ".join("?" for _ in SCHOOL_INFO_COLUMNS)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing_year = c.execute(
                "SELECT 1 FROM ann_scolaire WHERE nom_annee=? LIMIT 1",
                (values[0],),
            ).fetchone()
            if existing_year:
                raise ValueError(
                    f"السنة الدراسية {values[0]} مسجلة مسبقاً."
                )
            cursor = c.execute(
                f"INSERT INTO ann_scolaire ({columns}) VALUES ({placeholders})",
                values,
            )
            return cursor.lastrowid

    def get_day_id(self, day):
        with self.connect() as c:
            row = c.execute(
                "SELECT id FROM day_data WHERE date=? AND ann_scolaire_id=?",
                (day, self.year_id)
            ).fetchone()
            if row:
                return row[0]
            cur = c.execute(
                "INSERT INTO day_data(date, ann_scolaire_id) VALUES(?,?)",
                (day, self.year_id)
            )
            return cur.lastrowid

    def lessons(self, day_id):
        with self.connect() as c:
            return c.execute("""SELECT h.id, h.hessa_number, c.name, m.name,
                h.heure, h.time, h.wehda_number, lm.name FROM hessa h
                JOIN classe c ON c.id=h.classe_id
                LEFT JOIN majal m ON m.id=h.majal_id
                LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                WHERE h.day_data_id=? ORDER BY h.id ASC""", (day_id,)).fetchall()

    def recorded_day_id(self, day):
        with self.connect() as c:
            row = c.execute("""
                SELECT d.id
                FROM day_data d
                WHERE d.date=? AND d.ann_scolaire_id=?
                    AND EXISTS (SELECT 1 FROM hessa h WHERE h.day_data_id=d.id)
            """, (day, self.year_id)).fetchone()
            return row[0] if row else None

    def recorded_workdays(self, year_id=None):
        with self.connect() as c:
            return c.execute("""
                SELECT DISTINCT d.id, d.date
                FROM day_data d
                JOIN hessa h ON h.day_data_id=d.id
                WHERE d.ann_scolaire_id=?
                ORDER BY d.date DESC, d.id DESC
            """, (year_id or self.year_id,)).fetchall()

    def lessons_for_weekday(self, weekday_num, target_day):
        sqlite_weekday = (weekday_num + 1) % 7
        with self.connect() as c:
            row = c.execute("""
                SELECT d.id
                FROM day_data d
                WHERE d.ann_scolaire_id=?
                    AND CAST(strftime('%w', d.date) AS INTEGER)=?
                    AND d.date<>?
                    AND EXISTS (SELECT 1 FROM hessa h WHERE h.day_data_id=d.id)
                ORDER BY ABS(julianday(d.date)-julianday(?)),
                    CASE WHEN d.date<=? THEN 0 ELSE 1 END,
                    d.date DESC
                LIMIT 1
            """, (self.year_id, sqlite_weekday, target_day, target_day, target_day)).fetchone()
            if not row:
                return []
            return c.execute("""
                SELECT c.name, m.name, h.heure, h.time
                FROM hessa h JOIN classe c ON c.id=h.classe_id
                LEFT JOIN majal m ON m.id=h.majal_id
                WHERE h.day_data_id=?
                ORDER BY h.id
            """, (row[0],)).fetchall()

    def lesson_number_defaults(self, classe, majal):
        with self.connect() as c:
            rows = c.execute("""
                SELECT h.wehda_number, h.hessa_number, h.time
                FROM hessa h
                JOIN day_data d ON d.id=h.day_data_id
                JOIN classe c ON c.id=h.classe_id
                JOIN majal m ON m.id=h.majal_id
                WHERE d.ann_scolaire_id=? AND c.name=? AND m.name=?
                ORDER BY d.date DESC, h.id DESC
            """, (self.year_id, classe, majal)).fetchall()

        valid_rows = []
        for unit, lesson_number, duration in rows:
            try:
                unit = int(unit)
                lesson_number = int(lesson_number)
            except (TypeError, ValueError):
                continue
            if unit > 0 and lesson_number > 0:
                valid_rows.append((unit, lesson_number, duration))

        if not valid_rows:
            return 1, 1

        largest_unit = max(row[0] for row in valid_rows)
        rows_for_unit = [row for row in valid_rows if row[0] == largest_unit]
        largest_lesson = max(row[1] for row in rows_for_unit)
        last_lesson = next(row for row in rows_for_unit if row[1] == largest_lesson)
        is_arabic = "اللغة العربية" in majal.strip()
        has_120_minute_lesson = bool(re.search(r"\b120\b", str(last_lesson[2] or "")))
        next_lesson = largest_lesson + (2 if is_arabic and has_120_minute_lesson else 1)
        return largest_unit, next_lesson

    def lesson(self, lesson_id):
        with self.connect() as c:
            return c.execute("""SELECT h.id, c.name, m.name, h.heure, h.time,
                h.wehda_number, h.hessa_number, lm.name, h.day_data_id,
                c.level_id FROM hessa h JOIN classe c ON c.id=h.classe_id
                LEFT JOIN majal m ON m.id=h.majal_id
                LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                WHERE h.id=?""",
                (lesson_id,)).fetchone()

    def save_lessons(self, lessons, day_id):
        with self.connect() as c:
            for lesson in lessons:
                classe_id = self.resolve_class_id(
                    c, lesson[0], lesson[7] if len(lesson) > 7 else None
                )
                majal_id, learning_mode_id = self.resolve_lesson_lookup_ids(
                    c, lesson[1], lesson[6]
                )
                c.execute("""
                    INSERT INTO hessa(
                        classe_id, majal_id, heure, time, wehda_number,
                        hessa_number, learning_mode_id, day_data_id
                    )
                    VALUES(?,?,?,?,?,?,?,?)
                """, (
                    classe_id, majal_id, *lesson[2:6], learning_mode_id,
                    day_id,
                ))

    def save_lesson(self, values, day_id, lesson_id=None):
        with self.connect() as c:
            classe_id = self.resolve_class_id(c, values[0], values[7])
            majal_id, learning_mode_id = self.resolve_lesson_lookup_ids(
                c, values[1], values[6]
            )
            if lesson_id:
                c.execute("""
                    UPDATE hessa SET classe_id=?, majal_id=?, heure=?, time=?,
                        wehda_number=?, hessa_number=?, learning_mode_id=?
                    WHERE id=?
                """, (
                    classe_id, majal_id, *values[2:6], learning_mode_id,
                    lesson_id,
                ))
            else:
                c.execute("""
                    INSERT INTO hessa(
                        classe_id, majal_id, heure, time, wehda_number,
                        hessa_number, learning_mode_id, day_data_id
                    ) VALUES(?,?,?,?,?,?,?,?)
                """, (
                    classe_id, majal_id, *values[2:6], learning_mode_id,
                    day_id,
                ))
                return c.execute("SELECT last_insert_rowid()").fetchone()[0]
        return lesson_id

    def levels(self):
        with self.connect() as c:
            return c.execute(
                "SELECT id, name FROM level ORDER BY id"
            ).fetchall()

    def classes(self):
        with self.connect() as c:
            return c.execute(
                "SELECT id, name, level_id FROM classe ORDER BY name"
            ).fetchall()

    def delete_lesson(self, lesson_id):
        with self.connect() as c:
            c.execute("DELETE FROM hessa WHERE id=?", (lesson_id,))

    def activities(self, lesson_id):
        with self.connect() as c:
            return c.execute("SELECT id,title,time FROM nachat WHERE hessa_id=? ORDER BY id",
                             (lesson_id,)).fetchall()

    def activity_completion_data(self, lesson_ids):
        lesson_ids = tuple(lesson_ids)
        if not lesson_ids:
            return {}
        placeholders = ",".join("?" for _ in lesson_ids)
        with self.managed_connection() as c:
            rows = c.execute(f"""
                SELECT h.id, h.time, lm.name,
                    n.id, n.title, n.time,
                    d.id, d.key_name, d.value_text
                FROM hessa h
                LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                LEFT JOIN nachat n ON n.hessa_id = h.id
                LEFT JOIN nachat_detail d ON d.nachat_id = n.id
                WHERE h.id IN ({placeholders})
                ORDER BY h.id, n.id, d.id
            """, lesson_ids).fetchall()

        result = {}
        for (
            lesson_id, lesson_time, learning_mode,
            activity_id, title, activity_time,
            detail_id, key_name, value_text,
        ) in rows:
            lesson = result.setdefault(lesson_id, {
                "time": lesson_time,
                "learning_mode": learning_mode,
                "activities": {},
            })
            if activity_id is None:
                continue
            activity = lesson["activities"].setdefault(activity_id, {
                "title": title,
                "time": activity_time,
                "details": [],
            })
            if detail_id is not None:
                activity["details"].append(
                    (detail_id, key_name, value_text)
                )
        return result

    def incomplete_workday_dates(self, year_id=None):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT d.date, h.id
                FROM day_data d
                JOIN hessa h ON h.day_data_id = d.id
                WHERE d.ann_scolaire_id = ?
                ORDER BY d.date, h.id
            """, (year_id or self.year_id,)).fetchall()

        lesson_ids = [lesson_id for _, lesson_id in rows]
        completion_data = self.activity_completion_data(lesson_ids)
        incomplete_dates = set()
        for day, lesson_id in rows:
            if lesson_completion_issues(completion_data[lesson_id]):
                incomplete_dates.add(day)
        return incomplete_dates

    def save_activity(self, title, time, lesson_id, activity_id=None):
        with self.connect() as c:
            if activity_id:
                c.execute("UPDATE nachat SET title=?,time=? WHERE id=?",
                          (title, time, activity_id))
                return activity_id
            cur = c.execute("INSERT INTO nachat(title,time,hessa_id) VALUES(?,?,?)",
                            (title, time, lesson_id))
            return cur.lastrowid

    def delete_activity(self, activity_id):
        with self.connect() as c:
            c.execute("DELETE FROM nachat WHERE id=?", (activity_id,))

    def details(self, activity_id):
        with self.connect() as c:
            return c.execute("""SELECT id,key_name,value_text FROM nachat_detail
                WHERE nachat_id=? ORDER BY id""", (activity_id,)).fetchall()

    def teaching_diary_activity_properties(self, activity_title, learning_mode):
        activity_title = str(activity_title or "").strip()
        learning_mode = str(learning_mode or "").strip()
        if not activity_title or not learning_mode:
            return []
        normalized_name = self._normalized_plan_activity(activity_title)
        activity_names = {activity_title}
        for canonical, aliases in PLAN_ACTIVITY_ALIASES.items():
            if self._normalized_plan_activity(canonical) == normalized_name:
                activity_names.add(canonical)
                activity_names.update(aliases)
        placeholders = ", ".join("?" for _ in activity_names)
        with self.managed_connection() as c:
            rows = c.execute(f"""
                SELECT d.key_name, d.value_text
                FROM nachat_detail d
                JOIN nachat n ON n.id=d.nachat_id
                JOIN hessa h ON h.id=n.hessa_id
                LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                WHERE lower(trim(n.title)) IN ({placeholders})
                    AND lower(trim(lm.name))=lower(trim(?))
                ORDER BY d.id DESC
            """, (
                *(name.casefold() for name in activity_names),
                learning_mode,
            )).fetchall()
        suggestions = {}
        for key, value in rows:
            key = str(key or "").strip()
            value = str(value or "").strip()
            if not key:
                continue
            values = suggestions.setdefault(key, [])
            if value and value not in values and len(values) < 12:
                values.append(value)
        return list(suggestions.items())

    def save_detail(self, key, value, activity_id, detail_id=None):
        with self.connect() as c:
            if detail_id:
                c.execute("UPDATE nachat_detail SET key_name=?,value_text=? WHERE id=?",
                          (key, value, detail_id))
                return detail_id
            cur = c.execute("""INSERT INTO nachat_detail(key_name,value_text,nachat_id)
                VALUES(?,?,?)""", (key, value, activity_id))
            return cur.lastrowid

    def save_activity_details_bulk(self, updates, inserts, deleted_ids):
        update_rows = [
            (str(key).strip(), str(value).strip(), detail_id)
            for detail_id, key, value in updates
        ]
        insert_rows = [
            (str(key).strip(), str(value).strip(), activity_id)
            for activity_id, key, value in inserts
            if str(key).strip() and str(value).strip()
        ]
        with self.connect() as c:
            if deleted_ids:
                c.executemany(
                    "DELETE FROM nachat_detail WHERE id=?",
                    [(detail_id,) for detail_id in deleted_ids],
                )
            if update_rows:
                c.executemany(
                    """UPDATE nachat_detail SET key_name=?,value_text=?
                       WHERE id=?""",
                    update_rows,
                )
            if insert_rows:
                c.executemany(
                    """INSERT INTO nachat_detail(key_name,value_text,nachat_id)
                       VALUES(?,?,?)""",
                    insert_rows,
                )
        return len(update_rows) + len(insert_rows)

    def delete_detail(self, detail_id):
        with self.connect() as c:
            c.execute("DELETE FROM nachat_detail WHERE id=?", (detail_id,))

    def school_info(self):
        with self.connect() as c:
            columns = ", ".join(SCHOOL_INFO_COLUMNS)
            return c.execute(
                f"SELECT {columns} FROM ann_scolaire WHERE id=?",
                (self.year_id,)
            ).fetchone()

    def update_school_info(self, values):
        if len(values) != len(SCHOOL_INFO_COLUMNS):
            raise ValueError(
                f"Expected {len(SCHOOL_INFO_COLUMNS)} school-info values, "
                f"received {len(values)}."
            )
        assignments = ", ".join(
            f"{column}=?" for column in SCHOOL_INFO_COLUMNS
        )
        with self.connect() as c:
            c.execute(
                f"UPDATE ann_scolaire SET {assignments} WHERE id=?",
                (*values, self.year_id)
            )

    def get_latest_date(self):
        with self.connect() as c:
            row = c.execute(
                "SELECT date FROM day_data WHERE ann_scolaire_id=? ORDER BY date DESC LIMIT 1",
                (self.year_id,)
            ).fetchone()
            return row[0] if row else None
        
    def cleanup_empty_days(self):
        with self.connect() as c:
            c.execute("""
                DELETE FROM day_data 
                WHERE ann_scolaire_id=? 
                AND id NOT IN (SELECT DISTINCT day_data_id FROM hessa)
            """, (self.year_id,))

    def planning_classes(self):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT c.id, c.name, c.level_id, l.name
                FROM classe c JOIN level l ON l.id=c.level_id
                ORDER BY l.id, c.name
            """).fetchall()

    def calculate_teaching_plan_end_date(
        self, start_date, lesson_count, weekly_schedule
    ):
        with self.managed_connection() as c:
            holidays = [
                (row[0], row[1]) for row in c.execute(
                    "SELECT holiday_date, end_date FROM official_holiday"
                ).fetchall()
            ]
        return calculate_plan_end_date(
            start_date, lesson_count, weekly_schedule, holidays
        )

    def teaching_plan_weekday_counts(self, plan_id):
        with self.managed_connection() as c:
            return dict(c.execute("""
                SELECT weekday, lesson_count
                FROM teaching_plan_weekday
                WHERE plan_id=? ORDER BY weekday
            """, (plan_id,)).fetchall())

    def planning_majals(self):
        with self.managed_connection() as c:
            return c.execute("SELECT id, name FROM majal ORDER BY name").fetchall()

    def planning_competences_majal(self, classe_id, majal_id):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT cm.id, cm.competence
                FROM competence_majal cm
                JOIN classe c ON c.level_id=cm.id_level
                WHERE c.id=? AND cm.id_majal=?
                ORDER BY cm.id
            """, (classe_id, majal_id)).fetchall()

    def planning_competences_matiere(self, competence_majal_id):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT id, matiere, competence FROM competence_matiere
                WHERE id_competence_majal=? ORDER BY matiere, competence
            """, (competence_majal_id,)).fetchall()

    def teaching_plan_activity_names(self, plan_id):
        with self.managed_connection() as c:
            row = c.execute("""
                SELECT m.name FROM teaching_plan p
                JOIN majal m ON m.id=p.majal_id WHERE p.id=?
            """, (plan_id,)).fetchone()
        return list(ACTIVITIES_BY_MAJAL.get(row[0], ())) if row else []

    def teaching_plan_item_competences(self, plan_id, activity_name):
        activity_names = (
            activity_name,
            *PLAN_ACTIVITY_ALIASES.get(activity_name.strip(), ()),
        )
        placeholders = ", ".join("?" for _ in activity_names)
        with self.managed_connection() as c:
            official = c.execute(f"""
                SELECT cm.id, cm.matiere, cm.competence
                FROM teaching_plan p
                JOIN classe cl ON cl.id=p.classe_id
                JOIN competence_majal cmj ON cmj.id_level=cl.level_id
                    AND cmj.id_majal=p.majal_id
                JOIN competence_matiere cm
                    ON cm.id_competence_majal=cmj.id
                WHERE p.id=?
                    AND lower(trim(cm.matiere)) IN ({placeholders})
                ORDER BY cm.competence
            """, (plan_id, *(
                name.strip().casefold() for name in activity_names
            ))).fetchall()
            planned = c.execute(f"""
                SELECT NULL, i.activity_name, i.competence_text
                FROM teaching_plan current
                JOIN classe current_class
                    ON current_class.id=current.classe_id
                JOIN teaching_plan previous
                    ON previous.id<>current.id
                    AND previous.majal_id=current.majal_id
                JOIN classe previous_class
                    ON previous_class.id=previous.classe_id
                    AND previous_class.level_id=current_class.level_id
                JOIN teaching_plan_item i ON i.plan_id=previous.id
                WHERE current.id=?
                    AND lower(trim(i.activity_name)) IN ({placeholders})
                    AND trim(i.competence_text)<>''
                ORDER BY previous.created_at DESC, i.id DESC
            """, (plan_id, *(
                name.strip().casefold() for name in activity_names
            ))).fetchall()
            if not official and not planned:
                official = c.execute("""
                    SELECT cm.id, cm.matiere, cm.competence
                    FROM teaching_plan p
                    JOIN classe cl ON cl.id=p.classe_id
                    JOIN competence_majal cmj ON cmj.id_level=cl.level_id
                        AND cmj.id_majal=p.majal_id
                    JOIN competence_matiere cm
                        ON cm.id_competence_majal=cmj.id
                    WHERE p.id=?
                    ORDER BY cm.matiere, cm.competence
                """, (plan_id,)).fetchall()
                planned = c.execute("""
                    SELECT NULL, i.activity_name, i.competence_text
                    FROM teaching_plan current
                    JOIN classe current_class
                        ON current_class.id=current.classe_id
                    JOIN teaching_plan previous
                        ON previous.id<>current.id
                        AND previous.majal_id=current.majal_id
                    JOIN classe previous_class
                        ON previous_class.id=previous.classe_id
                        AND previous_class.level_id=current_class.level_id
                    JOIN teaching_plan_item i ON i.plan_id=previous.id
                    WHERE current.id=? AND trim(i.competence_text)<>''
                    ORDER BY previous.created_at DESC, i.id DESC
                """, (plan_id,)).fetchall()
        suggestions = []
        seen = set()
        for row in (*official, *planned):
            key = str(row[2] or "").strip().casefold()
            if key not in seen:
                seen.add(key)
                suggestions.append(row)
        return suggestions

    def teaching_plan_item_objective_history(self, plan_id):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT DISTINCT d.objective_text
                FROM teaching_plan current
                JOIN classe current_class
                    ON current_class.id=current.classe_id
                JOIN teaching_plan previous
                    ON previous.id<>current.id
                JOIN classe previous_class
                    ON previous_class.id=previous.classe_id
                    AND previous_class.level_id=current_class.level_id
                JOIN teaching_plan_item i ON i.plan_id=previous.id
                JOIN teaching_plan_item_detail d ON d.plan_item_id=i.id
                WHERE current.id=? AND trim(d.objective_text)<>''
                ORDER BY d.objective_text
            """, (plan_id,)).fetchall()

    def teaching_plan_item_suggestions(
        self, plan_id, competence_matiere_id=None, competence_text=None
    ):
        with self.managed_connection() as c:
            query = """
                SELECT cm.id, cm.matiere, cm.competence,
                    mk.id, mk.competence,
                    os.id, os.objectif_specifique,
                    cnt.id, cnt.content
                FROM teaching_plan p
                JOIN classe cl ON cl.id=p.classe_id
                JOIN competence_majal cmj ON cmj.id_level=cl.level_id
                    AND cmj.id_majal=p.majal_id
                JOIN competence_matiere cm
                    ON cm.id_competence_majal=cmj.id
                LEFT JOIN moukawan_kifaya mk
                    ON mk.id_competence_matiere=cm.id
                LEFT JOIN objectif_specifique os
                    ON os.id_moukawa_kifaya=mk.id
                LEFT JOIN content cnt
                    ON cnt.id_objectif_specifique=os.id
                WHERE p.id=?
            """
            params = [plan_id]
            if competence_matiere_id is not None:
                query += " AND cm.id=?"
                params.append(competence_matiere_id)
            elif competence_text:
                query += " AND lower(trim(cm.competence))=lower(trim(?))"
                params.append(competence_text)
            query += """
                ORDER BY cm.matiere, cm.competence, mk.id, os.id, cnt.id
            """
            official = c.execute(query, params).fetchall()
            if competence_text is None and official:
                competence_text = official[0][2]
            history = []
            if competence_text:
                history = c.execute("""
                    SELECT NULL, i.activity_name, i.competence_text,
                        NULL, d.competence_component_text,
                        NULL, d.objective_text,
                        NULL, d.content_text
                    FROM teaching_plan current
                    JOIN classe current_class
                        ON current_class.id=current.classe_id
                    JOIN teaching_plan previous
                        ON previous.id<>current.id
                        AND previous.majal_id=current.majal_id
                    JOIN classe previous_class
                        ON previous_class.id=previous.classe_id
                        AND previous_class.level_id=current_class.level_id
                    JOIN teaching_plan_item i ON i.plan_id=previous.id
                    JOIN teaching_plan_item_detail d ON d.plan_item_id=i.id
                    WHERE current.id=?
                        AND lower(trim(i.competence_text))=lower(trim(?))
                    ORDER BY previous.created_at DESC, i.id DESC, d.id
                """, (plan_id, competence_text)).fetchall()
            suggestions = []
            seen = set()
            for row in (*official, *history):
                key = tuple(str(value or "").strip().casefold()
                            for value in (row[2], row[4], row[6], row[8]))
                if key not in seen:
                    seen.add(key)
                    suggestions.append(row)
            return suggestions

    def teaching_plan_items(self, plan_id):
        with self.managed_connection() as c:
            rows = c.execute("""
                SELECT i.id, i.activity_name, i.competence_matiere_id,
                    i.competence_text, cm.matiere
                FROM teaching_plan_item i
                LEFT JOIN competence_matiere cm
                    ON cm.id=i.competence_matiere_id
                WHERE i.plan_id=? ORDER BY i.activity_name
            """, (plan_id,)).fetchall()
            details = c.execute("""
                SELECT id, plan_item_id, competence_component_text,
                    objective_text, content_text
                FROM teaching_plan_item_detail
                WHERE plan_item_id IN (
                    SELECT id FROM teaching_plan_item WHERE plan_id=?
                )
                ORDER BY id
            """, (plan_id,)).fetchall()
        by_item = {}
        for detail in details:
            by_item.setdefault(detail[1], []).append(detail)
        return [(row, by_item.get(row[0], [])) for row in rows]

    def teaching_plan_integration_contents(self, plan_id, activity_name):
        activity_name = str(activity_name or "").strip()
        if not activity_name:
            return []
        normalized_name = self._normalized_plan_activity(activity_name)
        aliases = [activity_name]
        for canonical, activity_aliases in PLAN_ACTIVITY_ALIASES.items():
            if canonical.casefold() == normalized_name:
                aliases.extend((canonical, *activity_aliases))
        aliases = tuple(dict.fromkeys(aliases))
        placeholders = ", ".join("?" for _ in aliases)
        params = tuple(name.casefold() for name in aliases)
        with self.managed_connection() as c:
            contents = c.execute(f"""
                SELECT DISTINCT trim(d.content_text)
                FROM teaching_plan_item i
                JOIN teaching_plan_item_detail d ON d.plan_item_id=i.id
                WHERE i.plan_id=?
                    AND lower(trim(i.activity_name)) IN ({placeholders})
                    AND trim(d.content_text)<>''
                ORDER BY trim(d.content_text)
            """, (plan_id, *params)).fetchall()
            if not contents:
                contents = c.execute("""
                    SELECT DISTINCT trim(d.content_text)
                    FROM teaching_plan_item i
                    JOIN teaching_plan_item_detail d ON d.plan_item_id=i.id
                    WHERE i.plan_id=? AND trim(d.content_text)<>''
                    ORDER BY trim(d.content_text)
                """, (plan_id,)).fetchall()
        return [row[0] for row in contents]

    def teaching_plan_integration_activities(self, plan_id):
        with self.managed_connection() as c:
            activities = c.execute("""
                SELECT activity_name, prompt_text, template_text, target_text
                FROM teaching_plan_integration_activity
                WHERE plan_id=? ORDER BY activity_name
            """, (plan_id,)).fetchall()
            contents = c.execute("""
                SELECT activity_name, content_text
                FROM teaching_plan_integration_content
                WHERE plan_id=? ORDER BY activity_name, content_text
            """, (plan_id,)).fetchall()
        content_by_activity = {}
        for activity_name, content_text in contents:
            content_by_activity.setdefault(activity_name, []).append(
                content_text
            )
        return [
            (
                activity_name, prompt_text, template_text, target_text,
                content_by_activity.get(activity_name, []),
            )
            for activity_name, prompt_text, template_text, target_text
            in activities
        ]

    def save_teaching_plan_integration_activity(
        self, plan_id, activity_name, prompt_text, template_text,
        target_text, selected_contents,
    ):
        activity_name = str(activity_name or "").strip()
        if not activity_name:
            raise ValueError("اسم نشاط الإدماج مطلوب.")
        valid_activities = {
            name.casefold()
            for name in self.teaching_plan_activity_names(plan_id)
        }
        if activity_name.casefold() not in valid_activities:
            raise ValueError("نشاط الإدماج لا يتبع مجال المخطط.")
        selected_contents = sorted({
            str(content or "").strip()
            for content in selected_contents if str(content or "").strip()
        })
        prompt_text = str(prompt_text or "").strip()
        template_text = str(template_text or "").strip()
        target_text = str(target_text or "")
        with self.managed_connection() as c:
            c.execute("""
                INSERT INTO teaching_plan_integration_activity(
                    plan_id, activity_name, prompt_text, template_text,
                    target_text
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(plan_id, activity_name) DO UPDATE SET
                    prompt_text=excluded.prompt_text,
                    template_text=excluded.template_text,
                    target_text=excluded.target_text
            """, (
                plan_id, activity_name, prompt_text, template_text,
                target_text,
            ))
            c.execute("""
                DELETE FROM teaching_plan_integration_content
                WHERE plan_id=? AND activity_name=?
            """, (plan_id, activity_name))
            c.executemany("""
                INSERT INTO teaching_plan_integration_content(
                    plan_id, activity_name, content_text
                ) VALUES(?, ?, ?)
            """, [
                (plan_id, activity_name, content)
                for content in selected_contents
            ])

    def delete_teaching_plan_integration_activity(
        self, plan_id, activity_name
    ):
        with self.managed_connection() as c:
            c.execute("""
                DELETE FROM teaching_plan_integration_activity
                WHERE plan_id=? AND activity_name=?
            """, (plan_id, activity_name))

    def teaching_plan_evaluation_activities(self, plan_id):
        with self.managed_connection() as c:
            activities = c.execute("""
                SELECT activity_name, prompt_text, template_text, target_text
                FROM teaching_plan_evaluation_activity
                WHERE plan_id=? ORDER BY activity_name
            """, (plan_id,)).fetchall()
            contents = c.execute("""
                SELECT activity_name, content_text
                FROM teaching_plan_evaluation_content
                WHERE plan_id=? ORDER BY activity_name, content_text
            """, (plan_id,)).fetchall()
        content_by_activity = {}
        for activity_name, content_text in contents:
            content_by_activity.setdefault(activity_name, []).append(
                content_text
            )
        return [
            (
                activity_name, prompt_text, template_text, target_text,
                content_by_activity.get(activity_name, []),
            )
            for activity_name, prompt_text, template_text, target_text
            in activities
        ]

    def save_teaching_plan_evaluation_activity(
        self, plan_id, activity_name, prompt_text, template_text,
        target_text, selected_contents,
    ):
        activity_name = str(activity_name or "").strip()
        if not activity_name:
            raise ValueError("اسم نشاط التقييم مطلوب.")
        valid_activities = {
            name.casefold()
            for name in self.teaching_plan_activity_names(plan_id)
        }
        if activity_name.casefold() not in valid_activities:
            raise ValueError("نشاط التقييم لا يتبع مجال المخطط.")
        selected_contents = sorted({
            str(content or "").strip()
            for content in selected_contents if str(content or "").strip()
        })
        prompt_text = str(prompt_text or "").strip()
        template_text = str(template_text or "").strip()
        target_text = str(target_text or "")
        with self.managed_connection() as c:
            c.execute("""
                INSERT INTO teaching_plan_evaluation_activity(
                    plan_id, activity_name, prompt_text, template_text,
                    target_text
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(plan_id, activity_name) DO UPDATE SET
                    prompt_text=excluded.prompt_text,
                    template_text=excluded.template_text,
                    target_text=excluded.target_text
            """, (
                plan_id, activity_name, prompt_text, template_text,
                target_text,
            ))
            c.execute("""
                DELETE FROM teaching_plan_evaluation_content
                WHERE plan_id=? AND activity_name=?
            """, (plan_id, activity_name))
            c.executemany("""
                INSERT INTO teaching_plan_evaluation_content(
                    plan_id, activity_name, content_text
                ) VALUES(?, ?, ?)
            """, [
                (plan_id, activity_name, content)
                for content in selected_contents
            ])

    def delete_teaching_plan_evaluation_activity(
        self, plan_id, activity_name
    ):
        with self.managed_connection() as c:
            c.execute("""
                DELETE FROM teaching_plan_evaluation_activity
                WHERE plan_id=? AND activity_name=?
            """, (plan_id, activity_name))

    def teaching_plan_support_activities(self, plan_id):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT activity_name, target_criteria
                FROM teaching_plan_support_activity
                WHERE plan_id=? ORDER BY activity_name
            """, (plan_id,)).fetchall()

    def save_teaching_plan_support_activity(
        self, plan_id, activity_name, target_criteria
    ):
        activity_name = str(activity_name or "").strip()
        if not activity_name:
            raise ValueError("اسم نشاط الدعم والعلاج مطلوب.")
        valid_activities = {
            name.casefold()
            for name in self.teaching_plan_activity_names(plan_id)
        }
        if activity_name.casefold() not in valid_activities:
            raise ValueError("نشاط الدعم والعلاج لا يتبع مجال المخطط.")
        with self.managed_connection() as c:
            c.execute("""
                INSERT INTO teaching_plan_support_activity(
                    plan_id, activity_name, target_criteria
                ) VALUES(?, ?, ?)
                ON CONFLICT(plan_id, activity_name) DO UPDATE SET
                    target_criteria=excluded.target_criteria
            """, (
                plan_id, activity_name, str(target_criteria or ""),
            ))

    def delete_teaching_plan_support_activity(self, plan_id, activity_name):
        with self.managed_connection() as c:
            c.execute("""
                DELETE FROM teaching_plan_support_activity
                WHERE plan_id=? AND activity_name=?
            """, (plan_id, activity_name))

    def teaching_plan_mode_settings(self, plan_id):
        with self.managed_connection() as c:
            settings = c.execute("""
                SELECT learning_mode, expected_performance, is_default
                FROM teaching_plan_mode_setting WHERE plan_id=?
            """, (plan_id,)).fetchall()
            selections = c.execute("""
                SELECT learning_mode, plan_item_id, objective_text
                FROM teaching_plan_mode_selection WHERE plan_id=?
                ORDER BY plan_item_id, objective_text
            """, (plan_id,)).fetchall()
            activity_selections = c.execute("""
                SELECT learning_mode, activity_name, objective_text
                FROM teaching_plan_mode_activity_selection WHERE plan_id=?
                ORDER BY activity_name, objective_text
            """, (plan_id,)).fetchall()
        by_mode = {
            mode: {
                "expected_performance": performance,
                "is_default": bool(is_default),
                "selected_items": set(),
                "selected_objectives": {},
                "selected_activities": {},
            }
            for mode, performance, is_default in settings
        }
        for mode, item_id, objective in selections:
            setting = by_mode.setdefault(mode, {
                "expected_performance": "",
                "is_default": False,
                "selected_items": set(),
                "selected_objectives": {},
                "selected_activities": {},
            })
            setting["selected_items"].add(item_id)
            if objective:
                setting["selected_objectives"].setdefault(
                    item_id, set()
                ).add(objective)
        for mode, activity_name, objective in activity_selections:
            setting = by_mode.setdefault(mode, {
                "expected_performance": "",
                "is_default": False,
                "selected_items": set(),
                "selected_objectives": {},
                "selected_activities": {},
            })
            setting["selected_activities"].setdefault(
                activity_name, set()
            )
            if objective:
                setting["selected_activities"][activity_name].add(objective)
        default_mode = next(
            (
                mode for mode, setting in by_mode.items()
                if setting["is_default"]
            ),
            "منهجي",
        )
        return {"default_mode": default_mode, "modes": by_mode}

    def save_teaching_plan_mode_settings(
        self, plan_id, learning_mode, selections, expected_performance="",
    ):
        valid_modes = ("منهجي", "إدماج", "تقييم", "دعم وعلاج")
        if learning_mode not in valid_modes:
            raise ValueError("نمط التعلم المختار غير صالح.")
        normalized_selections = []
        normalized_activity_selections = []
        for item_key, objectives in selections.items():
            normalized_objectives = sorted({
                str(objective or "").strip()
                for objective in objectives if str(objective or "").strip()
            })
            if isinstance(item_key, int):
                normalized_selections.append(
                    (item_key, normalized_objectives)
                )
            else:
                activity_name = str(item_key or "").strip()
                if not activity_name:
                    raise ValueError("اسم النشاط المختار غير صالح.")
                normalized_activity_selections.append(
                    (activity_name, normalized_objectives)
                )
        expected_performance = str(expected_performance or "").strip()

        valid_activity_names = {
            name.casefold()
            for name in self.teaching_plan_activity_names(plan_id)
        }
        with self.managed_connection() as c:
            plan_items = {
                row[0] for row in c.execute(
                    "SELECT id FROM teaching_plan_item WHERE plan_id=?",
                    (plan_id,),
                ).fetchall()
            }
            if any(item_id not in plan_items
                   for item_id, _objectives in normalized_selections):
                raise ValueError("أحد الأنشطة المختارة لا يتبع هذا المخطط.")
            if any(
                activity_name.casefold() not in valid_activity_names
                for activity_name, _objectives
                in normalized_activity_selections
            ):
                raise ValueError("أحد الأنشطة المختارة لا يتبع مجال المخطط.")
            c.execute("""
                UPDATE teaching_plan_mode_setting SET is_default=0
                WHERE plan_id=?
            """, (plan_id,))
            c.execute("""
                INSERT INTO teaching_plan_mode_setting(
                    plan_id, learning_mode, expected_performance, is_default
                ) VALUES(?, ?, ?, 1)
                ON CONFLICT(plan_id, learning_mode) DO UPDATE SET
                    expected_performance=excluded.expected_performance,
                    is_default=1
            """, (plan_id, learning_mode, expected_performance))
            c.execute("""
                DELETE FROM teaching_plan_mode_selection
                WHERE plan_id=? AND learning_mode=?
            """, (plan_id, learning_mode))
            c.execute("""
                DELETE FROM teaching_plan_mode_activity_selection
                WHERE plan_id=? AND learning_mode=?
            """, (plan_id, learning_mode))
            for item_id, objectives in normalized_selections:
                if objectives:
                    c.executemany("""
                        INSERT INTO teaching_plan_mode_selection(
                            plan_id, learning_mode, plan_item_id, objective_text
                        ) VALUES(?, ?, ?, ?)
                    """, [
                        (plan_id, learning_mode, item_id, objective)
                        for objective in objectives
                    ])
                else:
                    c.execute("""
                        INSERT INTO teaching_plan_mode_selection(
                            plan_id, learning_mode, plan_item_id, objective_text
                        ) VALUES(?, ?, ?, '')
                    """, (plan_id, learning_mode, item_id))
            for activity_name, objectives in normalized_activity_selections:
                if objectives:
                    c.executemany("""
                        INSERT INTO teaching_plan_mode_activity_selection(
                            plan_id, learning_mode, activity_name,
                            objective_text
                        ) VALUES(?, ?, ?, ?)
                    """, [
                        (plan_id, learning_mode, activity_name, objective)
                        for objective in objectives
                    ])
                else:
                    c.execute("""
                        INSERT INTO teaching_plan_mode_activity_selection(
                            plan_id, learning_mode, activity_name,
                            objective_text
                        ) VALUES(?, ?, ?, '')
                    """, (plan_id, learning_mode, activity_name))

    def teaching_plan_item(self, item_id):
        with self.managed_connection() as c:
            row = c.execute("""
                SELECT id, plan_id, activity_name, competence_matiere_id,
                    competence_text
                FROM teaching_plan_item WHERE id=?
            """, (item_id,)).fetchone()
            if row is None:
                return None
            details = c.execute("""
                SELECT id, competence_component_id, competence_component_text,
                    objective_id, objective_text, content_id, content_text
                FROM teaching_plan_item_detail WHERE plan_item_id=? ORDER BY id
            """, (item_id,)).fetchall()
        return row, details

    def teaching_plan_items_ready(self, plan_id):
        items = self.teaching_plan_items(plan_id)
        return bool(items) and all(
            row[1].strip() and row[3].strip() and details
            for row, details in items
        )

    def save_teaching_plan_item(
        self, plan_id, activity_name, competence_matiere_id, competence_text,
        details, item_id=None,
    ):
        activity_name = str(activity_name or "").strip()
        competence_text = str(competence_text or "").strip()
        normalized_details = []
        for detail in details:
            component_id, component_text, objective_id, objective_text, content_id, content_text = detail
            component_text = str(component_text or "").strip()
            objective_text = str(objective_text or "").strip()
            content_text = str(content_text or "").strip()
            normalized_details.append((
                component_id, component_text, objective_id, objective_text,
                content_id, content_text,
            ))
        if not activity_name:
            raise ValueError("اسم النشاط مطلوب.")
        if not competence_text:
            raise ValueError("كفاية النشاط مطلوبة.")
        if not normalized_details:
            raise ValueError("أضف مكوّناً واحداً ومحتوى واحداً على الأقل.")

        with self.managed_connection() as c:
            plan = c.execute("""
                SELECT p.competence_majal_id, cl.level_id, p.majal_id,
                    m.name
                FROM teaching_plan p
                JOIN classe cl ON cl.id=p.classe_id
                JOIN majal m ON m.id=p.majal_id
                WHERE p.id=?
            """, (plan_id,)).fetchone()
            if plan is None:
                raise ValueError("المخطط المحدد غير موجود.")
            if competence_matiere_id is not None:
                competence = c.execute("""
                    SELECT cm.competence, cm.matiere, cm.id_competence_majal
                    FROM competence_matiere cm
                    JOIN competence_majal cmj
                        ON cmj.id=cm.id_competence_majal
                    WHERE cm.id=? AND cmj.id_level=? AND cmj.id_majal=?
                """, (
                    competence_matiere_id, plan[1], plan[2],
                )).fetchone()
                if competence is None:
                    raise ValueError("كفاية المادة لا تتبع مستوى ومجال المخطط.")
                if competence_text != competence[0]:
                    raise ValueError("نص الكفاية لا يطابق السجل الرسمي المحدد.")

            for component_id, component_text, objective_id, objective_text, content_id, content_text in normalized_details:
                if component_id is not None:
                    component = c.execute("""
                        SELECT mk.competence, cm.id_competence_majal
                        FROM moukawan_kifaya mk
                        JOIN competence_matiere cm
                            ON cm.id=mk.id_competence_matiere
                        JOIN competence_majal cmj
                            ON cmj.id=cm.id_competence_majal
                        WHERE mk.id=? AND cmj.id_level=? AND cmj.id_majal=?
                    """, (component_id, plan[1], plan[2])).fetchone()
                    if component is None or component[0] != component_text:
                        raise ValueError("مكوّن الكفاية لا يطابق البرنامج الرسمي.")
                    if (
                        competence_matiere_id is not None
                        and c.execute(
                            "SELECT id FROM moukawan_kifaya "
                            "WHERE id=? AND id_competence_matiere=?",
                            (component_id, competence_matiere_id),
                        ).fetchone() is None
                    ):
                        raise ValueError("مكوّن الكفاية لا يتبع كفاية النشاط المختارة.")
                if objective_id is not None:
                    objective = c.execute("""
                        SELECT os.objectif_specifique, os.id_moukawa_kifaya,
                            cm.id
                        FROM objectif_specifique os
                        JOIN moukawan_kifaya mk
                            ON mk.id=os.id_moukawa_kifaya
                        JOIN competence_matiere cm
                            ON cm.id=mk.id_competence_matiere
                        JOIN competence_majal cmj
                            ON cmj.id=cm.id_competence_majal
                        WHERE os.id=? AND cmj.id_level=? AND cmj.id_majal=?
                    """, (objective_id, plan[1], plan[2])).fetchone()
                    if objective is None or objective[0] != objective_text:
                        raise ValueError(
                            "الهدف المميز لا يطابق برنامج هذا المجال والمستوى."
                        )
                    if (
                        component_id is not None
                        and objective[1] != component_id
                    ):
                        raise ValueError("الهدف المميز لا يتبع مكوّن الكفاية المختار.")
                    if (
                        competence_matiere_id is not None
                        and objective[2] != competence_matiere_id
                    ):
                        raise ValueError("الهدف المميز لا يتبع كفاية النشاط المختارة.")
                if content_id is not None:
                    content = c.execute(
                        """
                        SELECT cnt.content, cnt.id_objectif_specifique,
                            os.id_moukawa_kifaya, cm.id
                        FROM content cnt
                        JOIN objectif_specifique os
                            ON os.id=cnt.id_objectif_specifique
                        JOIN moukawan_kifaya mk
                            ON mk.id=os.id_moukawa_kifaya
                        JOIN competence_matiere cm
                            ON cm.id=mk.id_competence_matiere
                        JOIN competence_majal cmj
                            ON cmj.id=cm.id_competence_majal
                        WHERE cnt.id=? AND cmj.id_level=? AND cmj.id_majal=?
                        """,
                        (content_id, plan[1], plan[2]),
                    ).fetchone()
                    if content is None or content[0] != content_text:
                        raise ValueError(
                            "المحتوى لا يطابق برنامج هذا المجال والمستوى."
                        )
                    if (
                        objective_id is not None
                        and content[1] != objective_id
                    ):
                        raise ValueError("المحتوى لا يتبع الهدف المميز المختار.")
                    if (
                        component_id is not None
                        and content[2] != component_id
                    ):
                        raise ValueError("المحتوى لا يتبع مكوّن الكفاية المختار.")
                    if (
                        competence_matiere_id is not None
                        and content[3] != competence_matiere_id
                    ):
                        raise ValueError("المحتوى لا يتبع كفاية النشاط المختارة.")

            if item_id is None:
                existing_item = c.execute("""
                    SELECT id FROM teaching_plan_item
                    WHERE plan_id=? AND activity_name=? AND competence_text=?
                """, (
                    plan_id, activity_name, competence_text,
                )).fetchone()
                if existing_item is not None:
                    item_id = existing_item[0]

            if item_id is None:
                cursor = c.execute("""
                    INSERT INTO teaching_plan_item(
                        plan_id, activity_name, competence_matiere_id,
                        competence_text
                    ) VALUES(?, ?, ?, ?)
                """, (
                    plan_id, activity_name, competence_matiere_id,
                    competence_text,
                ))
                item_id = cursor.lastrowid
            else:
                existing_item = c.execute(
                    "SELECT 1 FROM teaching_plan_item WHERE id=? AND plan_id=?",
                    (item_id, plan_id),
                ).fetchone()
                if existing_item is None:
                    raise ValueError("النشاط المحدد لا يتبع هذا المخطط.")
                c.execute("""
                    UPDATE teaching_plan_item
                    SET activity_name=?, competence_matiere_id=?, competence_text=?
                    WHERE id=? AND plan_id=?
                """, (
                    activity_name, competence_matiere_id, competence_text,
                    item_id, plan_id,
                ))
                c.execute(
                    "DELETE FROM teaching_plan_item_detail WHERE plan_item_id=?",
                    (item_id,),
                )
            c.executemany("""
                INSERT INTO teaching_plan_item_detail(
                    plan_item_id, competence_component_id,
                    competence_component_text, objective_id, objective_text,
                    content_id, content_text
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """, [
                (item_id, *detail) for detail in normalized_details
            ])
            return item_id

    def delete_teaching_plan_item(self, item_id):
        with self.managed_connection() as c:
            row = c.execute(
                "SELECT plan_id FROM teaching_plan_item WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                return
            c.execute("DELETE FROM teaching_plan_item WHERE id=?", (item_id,))

    def official_holidays(self):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT id, holiday_date, end_date, name
                FROM official_holiday ORDER BY holiday_date
            """).fetchall()

    def save_official_holiday(
        self, start_date, end_date, name="", holiday_id=None
    ):
        normalized_start = date.fromisoformat(str(start_date)).isoformat()
        normalized_end = date.fromisoformat(str(end_date)).isoformat()
        if normalized_end < normalized_start:
            raise ValueError("تاريخ نهاية العطلة يجب ألا يسبق تاريخ بدايتها.")
        name = str(name or "").strip()
        with self.managed_connection() as c:
            if holiday_id is None:
                overlap = c.execute("""
                    SELECT 1 FROM official_holiday
                    WHERE holiday_date<=? AND end_date>=?
                    LIMIT 1
                """, (normalized_end, normalized_start)).fetchone()
                if overlap:
                    raise ValueError("تتداخل فترة العطلة مع عطلة رسمية مسجلة.")
                cursor = c.execute(
                    "INSERT INTO official_holiday(holiday_date, end_date, name) "
                    "VALUES(?, ?, ?)",
                    (normalized_start, normalized_end, name),
                )
                return cursor.lastrowid
            overlap = c.execute("""
                SELECT 1 FROM official_holiday
                WHERE holiday_date<=? AND end_date>=? AND id<>?
                LIMIT 1
            """, (normalized_end, normalized_start, holiday_id)).fetchone()
            if overlap:
                raise ValueError("تتداخل فترة العطلة مع عطلة رسمية مسجلة.")
            c.execute(
                "UPDATE official_holiday SET holiday_date=?, end_date=?, name=? "
                "WHERE id=?",
                (normalized_start, normalized_end, name, holiday_id),
            )
            return holiday_id

    def delete_official_holiday(self, holiday_id):
        with self.managed_connection() as c:
            c.execute("DELETE FROM official_holiday WHERE id=?", (holiday_id,))

    def next_teaching_plan_number(
        self, title, classe_id, plan_id=None
    ):
        title = str(title or "").strip()
        if not title:
            raise ValueError("اسم المخطط مطلوب لتحديد عدده.")
        with self.managed_connection() as c:
            highest_number = c.execute("""
                SELECT MAX(plan_number) FROM teaching_plan
                WHERE classe_id=? AND title=? COLLATE NOCASE
                    AND plan_number_assigned=1
                    AND (? IS NULL OR id<>?)
            """, (classe_id, title, plan_id, plan_id)).fetchone()[0]
        return (highest_number or 0) + 1

    def save_teaching_plan_number(self, plan_id, plan_number):
        try:
            number = int(plan_number)
        except (TypeError, ValueError) as exc:
            raise ValueError("عدد المخطط يجب أن يكون رقماً صحيحاً.") from exc
        if number < 1:
            raise ValueError("عدد المخطط يجب أن يكون أكبر من صفر.")
        with self.managed_connection() as c:
            plan = c.execute("""
                SELECT classe_id, title FROM teaching_plan WHERE id=?
            """, (plan_id,)).fetchone()
            if plan is None:
                raise ValueError("المخطط المحدد غير موجود.")
            duplicate = c.execute("""
                SELECT 1 FROM teaching_plan
                WHERE classe_id=? AND title=? COLLATE NOCASE
                    AND plan_number=? AND plan_number_assigned=1 AND id<>?
            """, (plan[0], plan[1], number, plan_id)).fetchone()
            if duplicate:
                raise ValueError(
                    "عدد المخطط مستخدم في مخطط سابق بالاسم والقسم نفسيهما."
                )
            c.execute("""
                UPDATE teaching_plan
                SET plan_number=?, plan_number_assigned=1
                WHERE id=?
            """, (number, plan_id))
        return number

    def save_teaching_plan(
        self, title, classe_id, majal_id, competence_majal_id,
        start_date, lesson_count, weekdays, plan_id=None, properties=(),
        lesson_duration_minutes=60, mode_lesson_counts=None,
    ):
        title = str(title or "").strip()
        if not title:
            raise ValueError("اسم المخطط مطلوب.")
        start = date.fromisoformat(str(start_date)).isoformat()
        if isinstance(weekdays, dict):
            weekly_schedule = {
                int(day): int(daily_count)
                for day, daily_count in weekdays.items()
            }
        else:
            weekly_schedule = {
                int(day): 1 for day in set(weekdays)
            }
        if int(lesson_count) < 1:
            raise ValueError("عدد الحصص يجب أن يكون أكبر من صفر.")
        lesson_duration_minutes = int(lesson_duration_minutes)
        if lesson_duration_minutes not in (30, 60, 120):
            raise ValueError("مدة الحصة يجب أن تكون 30 أو 60 أو 120 دقيقة.")
        mode_lesson_counts = mode_lesson_counts or {
            "منهجي": int(lesson_count),
            "إدماج": 0,
            "تقييم": 0,
            "دعم وعلاج": 0,
        }
        mode_lesson_counts = {
            mode: int(mode_lesson_counts.get(mode, 0))
            for mode in ("منهجي", "إدماج", "تقييم", "دعم وعلاج")
        }
        if any(count < 0 for count in mode_lesson_counts.values()):
            raise ValueError("عدد حصص كل نمط لا يمكن أن يكون سالباً.")
        if sum(mode_lesson_counts.values()) != int(lesson_count):
            raise ValueError(
                "يجب أن يساوي مجموع حصص الأنماط الأربعة عدد الحصص الكلي."
            )
        if not weekly_schedule or any(
            day < 0 or day > 6 or daily_count < 1
            for day, daily_count in weekly_schedule.items()
        ):
            raise ValueError(
                "يجب اختيار يوم تدريس واحد وتحديد حصة واحدة على الأقل له."
            )
        with self.managed_connection() as c:
            valid_competence = c.execute("""
                SELECT 1
                FROM competence_majal cm
                JOIN classe cl ON cl.level_id=cm.id_level
                WHERE cm.id=? AND cm.id_majal=? AND cl.id=?
            """, (competence_majal_id, majal_id, classe_id)).fetchone()
            if not valid_competence:
                raise ValueError("كفاية المجال لا تتوافق مع القسم والمجال المختارين.")
            if plan_id is not None:
                highest_lesson = c.execute("""
                    SELECT MAX(lesson_number) FROM teaching_plan_activity
                    WHERE plan_id=?
                """, (plan_id,)).fetchone()[0]
                if highest_lesson is not None and highest_lesson > int(lesson_count):
                    raise ValueError(
                        "عدد الحصص أقل من رقم حصة مسجل في المخطط."
                    )
                incompatible_activities = c.execute("""
                    SELECT COUNT(*)
                    FROM teaching_plan_item i
                    JOIN competence_matiere cm
                        ON cm.id=i.competence_matiere_id
                    WHERE i.plan_id=?
                        AND cm.id_competence_majal<>?
                """, (plan_id, competence_majal_id)).fetchone()[0]
                if incompatible_activities:
                    raise ValueError(
                        "غيّر أو احذف أنشطة المخطط قبل تغيير كفاية المجال."
                    )
            values = (
                title, classe_id, majal_id, competence_majal_id, start,
                int(lesson_count), lesson_duration_minutes,
                mode_lesson_counts["منهجي"],
                mode_lesson_counts["إدماج"],
                mode_lesson_counts["تقييم"],
                mode_lesson_counts["دعم وعلاج"],
            )
            if plan_id is None:
                cursor = c.execute("""
                    INSERT INTO teaching_plan(
                        title, classe_id, majal_id, competence_majal_id,
                        start_date, lesson_count,
                        lesson_duration_minutes, methodical_lesson_count,
                        integration_lesson_count, evaluation_lesson_count,
                        support_lesson_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, values)
                saved_plan_id = cursor.lastrowid
            else:
                existing_plan = c.execute("""
                    SELECT title, classe_id, plan_number_assigned
                    FROM teaching_plan WHERE id=?
                """, (plan_id,)).fetchone()
                if existing_plan is None:
                    raise ValueError("المخطط المحدد غير موجود.")
                number_scope_change = (
                    existing_plan[:2] != (title, classe_id)
                )
                next_number = 0
                if number_scope_change and existing_plan[2]:
                    highest_number = c.execute("""
                        SELECT MAX(plan_number) FROM teaching_plan
                        WHERE classe_id=? AND title=? COLLATE NOCASE
                            AND plan_number_assigned=1 AND id<>?
                    """, (classe_id, title, plan_id)).fetchone()[0]
                    next_number = (highest_number or 0) + 1
                c.execute("""
                    UPDATE teaching_plan
                    SET title=?, classe_id=?, majal_id=?, competence_majal_id=?,
                        start_date=?, lesson_count=?,
                        lesson_duration_minutes=?,
                        methodical_lesson_count=?,
                        integration_lesson_count=?,
                        evaluation_lesson_count=?,
                        support_lesson_count=?,
                        plan_number=CASE WHEN ? THEN ? ELSE plan_number END
                    WHERE id=?
                """, (
                    *values, number_scope_change, next_number, plan_id,
                ))
                saved_plan_id = plan_id
            c.execute(
                "DELETE FROM teaching_plan_weekday WHERE plan_id=?",
                (saved_plan_id,),
            )
            c.executemany("""
                INSERT INTO teaching_plan_weekday(
                    plan_id, weekday, lesson_count
                ) VALUES(?, ?, ?)
            """, [
                (saved_plan_id, weekday, daily_count)
                for weekday, daily_count in sorted(weekly_schedule.items())
            ])
            c.execute(
                "DELETE FROM teaching_plan_property WHERE plan_id=?",
                (saved_plan_id,),
            )
            c.executemany("""
                INSERT INTO teaching_plan_property(plan_id, key_name, value_text)
                VALUES(?, ?, ?)
            """, [
                (saved_plan_id, str(key).strip(), str(value).strip())
                for key, value in properties
                if str(key).strip()
            ])
            return saved_plan_id

    def teaching_plans(self):
        with self.managed_connection() as c:
            rows = c.execute("""
                SELECT p.id, p.title, p.classe_id, cl.name, p.majal_id, m.name,
                    p.competence_majal_id, cm.competence, p.start_date,
                    p.lesson_count,
                    COALESCE((
                        SELECT group_concat(weekday, ',')
                        FROM (
                            SELECT weekday FROM teaching_plan_weekday
                            WHERE plan_id=p.id ORDER BY weekday
                        )
                    ), ''),
                        CASE WHEN p.plan_number_assigned=1
                            THEN p.plan_number ELSE NULL END,
                        p.lesson_duration_minutes,
                        p.methodical_lesson_count,
                        p.integration_lesson_count,
                        p.evaluation_lesson_count,
                        p.support_lesson_count,
                        COALESCE((
                            SELECT group_concat(
                                weekday || ':' || lesson_count, ','
                            )
                            FROM (
                                SELECT weekday, lesson_count
                                FROM teaching_plan_weekday
                                WHERE plan_id=p.id ORDER BY weekday
                            )
                        ), '')
                FROM teaching_plan p
                JOIN classe cl ON cl.id=p.classe_id
                JOIN majal m ON m.id=p.majal_id
                JOIN competence_majal cm ON cm.id=p.competence_majal_id
                ORDER BY p.start_date DESC, p.id DESC
            """).fetchall()
            holidays = [
                (row[0], row[1]) for row in c.execute(
                    "SELECT holiday_date, end_date FROM official_holiday"
                ).fetchall()
            ]
        plans = []
        for row in rows:
            weekly_schedule = {
                int(day): int(daily_count)
                for day, daily_count in (
                    entry.split(":", 1)
                    for entry in row[17].split(",") if entry
                )
            }
            plans.append((
                *row[:17],
                calculate_plan_end_date(
                    row[8], row[9], weekly_schedule, holidays
                ),
            ))
        return plans

    def teaching_plan(self, plan_id):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT p.id, p.title, p.classe_id, p.majal_id,
                    p.competence_majal_id, p.start_date, p.lesson_count,
                    COALESCE((
                        SELECT group_concat(weekday, ',')
                        FROM (
                            SELECT weekday FROM teaching_plan_weekday
                            WHERE plan_id=p.id ORDER BY weekday
                        )
                    ), ''),
                    CASE WHEN p.plan_number_assigned=1
                        THEN p.plan_number ELSE NULL END,
                    p.lesson_duration_minutes,
                    p.methodical_lesson_count,
                    p.integration_lesson_count,
                    p.evaluation_lesson_count,
                    p.support_lesson_count
                FROM teaching_plan p WHERE p.id=?
            """, (plan_id,)).fetchone()

    def suggest_teaching_plan_duration(self, classe_id, majal_id):
        with self.managed_connection() as c:
            domain = c.execute(
                "SELECT name FROM majal WHERE id=?", (majal_id,)
            ).fetchone()
            if not domain:
                return None, "لا توجد ملاحظات سابقة؛ اختر المدة المناسبة."
            domain_name = domain[0]
            observations = c.execute("""
                SELECT h.time
                FROM hessa h
                JOIN day_data d ON d.id=h.day_data_id
                JOIN majal m ON m.id=h.majal_id
                WHERE h.classe_id=?
                    AND lower(trim(m.name))=lower(trim(?))
                ORDER BY d.date DESC, h.id DESC
                LIMIT 100
            """, (classe_id, domain_name)).fetchall()
            source = "ملاحظات الحصص السابقة للقسم والمجال"
            if not observations:
                observations = c.execute("""
                    SELECT h.time
                    FROM hessa h
                    JOIN day_data d ON d.id=h.day_data_id
                    JOIN majal m ON m.id=h.majal_id
                    WHERE lower(trim(m.name))=lower(trim(?))
                    ORDER BY d.date DESC, h.id DESC
                    LIMIT 100
                """, (domain_name,)).fetchall()
                source = "ملاحظات الحصص السابقة للمجال"
            if not observations:
                observations = c.execute("""
                    SELECT lesson_duration_minutes
                    FROM teaching_plan
                    WHERE classe_id=? AND majal_id=?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 100
                """, (classe_id, majal_id)).fetchall()
                source = "مدد المخططات السابقة للقسم والمجال"
            if not observations:
                observations = c.execute("""
                    SELECT lesson_duration_minutes
                    FROM teaching_plan
                    WHERE majal_id=?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 100
                """, (majal_id,)).fetchall()
                source = "مدد المخططات السابقة للمجال"

        durations = []
        for (value,) in observations:
            parsed = duration_minutes(value)
            if parsed in (30, 60, 120):
                durations.append(parsed)
        if not durations:
            return None, "لا توجد ملاحظات سابقة؛ اختر المدة المناسبة."
        counts = {}
        for duration in durations:
            counts[duration] = counts.get(duration, 0) + 1
        suggested = max(counts, key=counts.get)
        return suggested, f"اقتراح بناءً على {source}."

    def teaching_plan_number(self, plan_id):
        with self.managed_connection() as c:
            row = c.execute(
                "SELECT CASE WHEN plan_number_assigned=1 "
                "THEN plan_number ELSE NULL END FROM teaching_plan WHERE id=?",
                (plan_id,),
            ).fetchone()
        return row[0] if row else None

    def teaching_plan_properties(self, plan_id):
        with self.managed_connection() as c:
            return c.execute("""
                SELECT id, key_name, value_text FROM teaching_plan_property
                WHERE plan_id=? ORDER BY id
            """, (plan_id,)).fetchall()

    def export_teaching_plan(self, plan_id):
        with self.managed_connection() as c:
            plan = c.execute("""
                SELECT p.title, cl.name, l.name, m.name, cm.competence,
                    p.start_date, p.lesson_count,
                    COALESCE((
                        SELECT group_concat(weekday, ',')
                        FROM (
                            SELECT weekday FROM teaching_plan_weekday
                            WHERE plan_id=p.id ORDER BY weekday
                        )
                    ), ''),
                    p.lesson_duration_minutes, p.methodical_lesson_count,
                    p.integration_lesson_count, p.evaluation_lesson_count,
                    p.support_lesson_count, p.plan_number,
                    p.plan_number_assigned
                FROM teaching_plan p
                JOIN classe cl ON cl.id=p.classe_id
                JOIN level l ON l.id=cl.level_id
                JOIN majal m ON m.id=p.majal_id
                JOIN competence_majal cm ON cm.id=p.competence_majal_id
                WHERE p.id=?
            """, (plan_id,)).fetchone()
            if plan is None:
                raise ValueError("المخطط المحدد غير موجود.")

            weekly_schedule = dict(c.execute("""
                SELECT weekday, lesson_count
                FROM teaching_plan_weekday
                WHERE plan_id=? ORDER BY weekday
            """, (plan_id,)).fetchall())
            properties = c.execute("""
                SELECT key_name, value_text
                FROM teaching_plan_property
                WHERE plan_id=? ORDER BY id
            """, (plan_id,)).fetchall()
            items = c.execute("""
                SELECT i.id, i.activity_name, cm.matiere, i.competence_text
                FROM teaching_plan_item i
                LEFT JOIN competence_matiere cm
                    ON cm.id=i.competence_matiere_id
                WHERE i.plan_id=? ORDER BY i.id
            """, (plan_id,)).fetchall()
            item_details = c.execute("""
                SELECT d.plan_item_id, mk.competence,
                    d.competence_component_text, os.objectif_specifique,
                    d.objective_text, cnt.content, d.content_text
                FROM teaching_plan_item_detail d
                LEFT JOIN moukawan_kifaya mk
                    ON mk.id=d.competence_component_id
                LEFT JOIN objectif_specifique os
                    ON os.id=d.objective_id
                LEFT JOIN content cnt ON cnt.id=d.content_id
                JOIN teaching_plan_item i ON i.id=d.plan_item_id
                WHERE i.plan_id=? ORDER BY d.id
            """, (plan_id,)).fetchall()

            mode_settings = c.execute("""
                SELECT learning_mode, expected_performance, is_default
                FROM teaching_plan_mode_setting WHERE plan_id=?
                ORDER BY learning_mode
            """, (plan_id,)).fetchall()
            mode_selections = c.execute("""
                SELECT learning_mode, plan_item_id, objective_text
                FROM teaching_plan_mode_selection WHERE plan_id=?
                ORDER BY learning_mode, plan_item_id, objective_text
            """, (plan_id,)).fetchall()
            mode_activity_selections = c.execute("""
                SELECT learning_mode, activity_name, objective_text
                FROM teaching_plan_mode_activity_selection WHERE plan_id=?
                ORDER BY learning_mode, activity_name, objective_text
            """, (plan_id,)).fetchall()

            integration_activities = c.execute("""
                SELECT activity_name, prompt_text, template_text, target_text
                FROM teaching_plan_integration_activity
                WHERE plan_id=? ORDER BY activity_name
            """, (plan_id,)).fetchall()
            integration_contents = c.execute("""
                SELECT activity_name, content_text
                FROM teaching_plan_integration_content
                WHERE plan_id=? ORDER BY activity_name, content_text
            """, (plan_id,)).fetchall()
            evaluation_activities = c.execute("""
                SELECT activity_name, prompt_text, template_text, target_text
                FROM teaching_plan_evaluation_activity
                WHERE plan_id=? ORDER BY activity_name
            """, (plan_id,)).fetchall()
            evaluation_contents = c.execute("""
                SELECT activity_name, content_text
                FROM teaching_plan_evaluation_content
                WHERE plan_id=? ORDER BY activity_name, content_text
            """, (plan_id,)).fetchall()
            support_activities = c.execute("""
                SELECT activity_name, target_criteria
                FROM teaching_plan_support_activity
                WHERE plan_id=? ORDER BY activity_name
            """, (plan_id,)).fetchall()
            activities = c.execute("""
                SELECT a.id, a.lesson_number, a.learning_mode, a.plan_item_id,
                    COALESCE(NULLIF(a.activity_name, ''), i.activity_name, ''),
                    content, lesson_goal, specific_objective,
                    target_competencies, evaluation, expected_performance,
                    target_criteria, demographic_concept, health_concept,
                    life_skills, duration_minutes
                FROM teaching_plan_activity a
                LEFT JOIN teaching_plan_item i ON i.id=a.plan_item_id
                WHERE a.plan_id=? ORDER BY a.lesson_number, a.id
            """, (plan_id,)).fetchall()
            activity_properties = c.execute("""
                SELECT p.activity_id, p.key_name, p.value_text
                FROM teaching_plan_activity_property p
                JOIN teaching_plan_activity a ON a.id=p.activity_id
                WHERE a.plan_id=? ORDER BY p.id
            """, (plan_id,)).fetchall()

        details_by_item = {}
        for (
            item_id, component_name, component_text, objective_name,
            objective_text, content_name, content_text,
        ) in item_details:
            details_by_item.setdefault(item_id, []).append({
                "component_name": component_name,
                "component_text": component_text,
                "objective_name": objective_name,
                "objective_text": objective_text,
                "content_name": content_name,
                "content_text": content_text,
            })
        item_records = [{
            "source_id": item_id,
            "activity_name": activity_name,
            "subject": subject,
            "competence_text": competence_text,
            "details": details_by_item.get(item_id, []),
        } for item_id, activity_name, subject, competence_text in items]
        items_by_id = {item_id: item for item_id, *item in items}
        selections_by_mode = {}
        for mode, item_id, objective in mode_selections:
            selection = selections_by_mode.setdefault(mode, {
                "items": {}, "activities": {},
            })
            selection["items"].setdefault(str(item_id), []).append(objective)
        for mode, activity_name, objective in mode_activity_selections:
            selection = selections_by_mode.setdefault(mode, {
                "items": {}, "activities": {},
            })
            selection["activities"].setdefault(
                activity_name, []
            ).append(objective)
        settings = []
        for mode, performance, is_default in mode_settings:
            selection = selections_by_mode.get(
                mode, {"items": {}, "activities": {}}
            )
            settings.append({
                "learning_mode": mode,
                "expected_performance": performance,
                "is_default": bool(is_default),
                "selected_items": [
                    {
                        "source_id": int(item_id),
                        "activity_name": items_by_id[int(item_id)][0],
                        "objectives": objectives,
                    }
                    for item_id, objectives in selection["items"].items()
                ],
                "selected_activities": [
                    {"activity_name": activity_name, "objectives": objectives}
                    for activity_name, objectives
                    in selection["activities"].items()
                ],
            })
        integration_content_by_activity = {}
        for activity_name, content in integration_contents:
            integration_content_by_activity.setdefault(
                activity_name, []
            ).append(content)
        evaluation_content_by_activity = {}
        for activity_name, content in evaluation_contents:
            evaluation_content_by_activity.setdefault(
                activity_name, []
            ).append(content)
        activity_properties_by_id = {}
        for activity_id, key, value in activity_properties:
            activity_properties_by_id.setdefault(activity_id, []).append({
                "key": key, "value": value,
            })
        activity_records = [{
            "lesson_number": lesson_number,
            "learning_mode": mode,
            "item_source_id": item_id,
            "activity_name": activity_name,
            "content": content,
            "lesson_goal": lesson_goal,
            "specific_objective": specific_objective,
            "target_competencies": target_competencies,
            "evaluation": evaluation,
            "expected_performance": expected_performance,
            "target_criteria": target_criteria,
            "demographic_concept": demographic_concept,
            "health_concept": health_concept,
            "life_skills": life_skills,
            "duration_minutes": duration,
            "properties": activity_properties_by_id.get(activity_id, []),
        } for (
            activity_id, lesson_number, mode, item_id, activity_name, content,
            lesson_goal, specific_objective, target_competencies, evaluation,
            expected_performance, target_criteria, demographic_concept,
            health_concept, life_skills, duration,
        ) in activities]

        return {
            "format": "jrnl_teaching_plan",
            "version": 1,
            "plan": {
                "title": plan[0],
                "class_name": plan[1],
                "level_name": plan[2],
                "domain_name": plan[3],
                "domain_competence": plan[4],
                "start_date": plan[5],
                "lesson_count": plan[6],
                "weekdays": [
                    int(day) for day in str(plan[7]).split(",") if day
                ],
                "lessons_per_day": {
                    str(day): count
                    for day, count in weekly_schedule.items()
                },
                "lesson_duration_minutes": plan[8],
                "mode_lesson_counts": {
                    "منهجي": plan[9],
                    "إدماج": plan[10],
                    "تقييم": plan[11],
                    "دعم وعلاج": plan[12],
                },
                "plan_number": plan[13] if plan[14] else None,
            },
            "properties": [
                {"key": key, "value": value} for key, value in properties
            ],
            "items": item_records,
            "mode_settings": settings,
            "integration_activities": [{
                "activity_name": name,
                "prompt_text": prompt,
                "template_text": template,
                "target_text": target,
                "contents": integration_content_by_activity.get(name, []),
            } for name, prompt, template, target in integration_activities],
            "evaluation_activities": [{
                "activity_name": name,
                "prompt_text": prompt,
                "template_text": template,
                "target_text": target,
                "contents": evaluation_content_by_activity.get(name, []),
            } for name, prompt, template, target in evaluation_activities],
            "support_activities": [
                {"activity_name": name, "target_criteria": criteria}
                for name, criteria in support_activities
            ],
            "activities": activity_records,
        }

    @staticmethod
    def _json_list(value, label):
        if not isinstance(value, list):
            raise ValueError(f"بيانات «{label}» يجب أن تكون قائمة.")
        return value

    @classmethod
    def _json_records(cls, value, label):
        records = cls._json_list(value, label)
        if any(not isinstance(record, dict) for record in records):
            raise ValueError(f"تحتوي بيانات «{label}» على سجل غير صالح.")
        return records

    def import_teaching_plan(self, snapshot, replace_plan_id=None):
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("format") != "jrnl_teaching_plan"
            or snapshot.get("version") != 1
        ):
            raise ValueError("صيغة ملف JSON غير مدعومة للمخططات التعليمية.")
        plan_data = snapshot.get("plan")
        if not isinstance(plan_data, dict):
            raise ValueError("بيانات المخطط غير صالحة في ملف JSON.")
        title = str(plan_data.get("title") or "").strip()
        class_name = str(plan_data.get("class_name") or "").strip()
        level_name = str(plan_data.get("level_name") or "").strip()
        domain_name = str(plan_data.get("domain_name") or "").strip()
        domain_competence = str(
            plan_data.get("domain_competence") or ""
        ).strip()
        if not all((title, class_name, level_name, domain_name, domain_competence)):
            raise ValueError("يفتقد ملف JSON بيانات المخطط أو القسم أو المجال.")

        properties = self._json_records(
            snapshot.get("properties", []), "خصائص المخطط"
        )
        items = self._json_records(snapshot.get("items", []), "الأنشطة")
        settings = self._json_records(
            snapshot.get("mode_settings", []), "إعدادات الأنماط"
        )
        integration = self._json_records(
            snapshot.get("integration_activities", []), "أنشطة الإدماج"
        )
        evaluation = self._json_records(
            snapshot.get("evaluation_activities", []), "أنشطة التقييم"
        )
        support = self._json_records(
            snapshot.get("support_activities", []), "أنشطة الدعم والعلاج"
        )
        activities = self._json_records(
            snapshot.get("activities", []), "التوزيعات"
        )
        try:
            weekdays = sorted({
                int(day) for day in self._json_list(
                    plan_data.get("weekdays", []), "أيام التدريس"
                )
            })
            raw_daily_counts = plan_data.get("lessons_per_day")
            if raw_daily_counts is None:
                weekly_schedule = {day: 1 for day in weekdays}
            elif isinstance(raw_daily_counts, dict):
                weekly_schedule = {
                    int(day): int(count)
                    for day, count in raw_daily_counts.items()
                }
            else:
                raise ValueError("أعداد الحصص اليومية غير صالحة.")
            lesson_count = int(plan_data.get("lesson_count", 0))
            duration = int(plan_data.get("lesson_duration_minutes", 0))
            mode_counts = plan_data.get("mode_lesson_counts")
            if not isinstance(mode_counts, dict):
                raise ValueError("أعداد الحصص حسب النمط غير صالحة.")
            modes = ("منهجي", "إدماج", "تقييم", "دعم وعلاج")
            mode_counts = {
                mode: int(mode_counts.get(mode, 0)) for mode in modes
            }
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("تحتوي بيانات الحصص على قيم غير صالحة.") from error
        if (
            not weekdays or any(day < 0 or day > 6 for day in weekdays)
            or set(weekly_schedule) != set(weekdays)
            or any(
                day < 0 or day > 6 or daily_count < 1
                for day, daily_count in weekly_schedule.items()
            )
        ):
            raise ValueError("أيام التدريس في ملف JSON غير صالحة.")
        if lesson_count < 1 or duration not in (30, 60, 120):
            raise ValueError("عدد الحصص أو مدتها في ملف JSON غير صالحة.")
        if (
            any(count < 0 for count in mode_counts.values())
            or sum(mode_counts.values()) != lesson_count
        ):
            raise ValueError("أعداد الحصص حسب الأنماط لا تطابق الإجمالي.")
        try:
            date.fromisoformat(str(plan_data.get("start_date") or ""))
        except ValueError as error:
            raise ValueError("تاريخ انطلاق المخطط في الملف غير صالح.") from error

        with self.managed_connection() as c:
            class_row = c.execute("""
                SELECT cl.id, cl.level_id
                FROM classe cl JOIN level l ON l.id=cl.level_id
                WHERE lower(trim(cl.name))=lower(trim(?))
                    AND lower(trim(l.name))=lower(trim(?))
                ORDER BY cl.id LIMIT 1
            """, (class_name, level_name)).fetchone()
            domain_row = c.execute(
                "SELECT id FROM majal WHERE lower(trim(name))=lower(trim(?)) "
                "ORDER BY id LIMIT 1",
                (domain_name,),
            ).fetchone()
            if class_row is None or domain_row is None:
                raise ValueError(
                    "القسم أو المستوى أو المجال الموجود في الملف غير موجود بقاعدة البيانات."
                )
            competence_row = c.execute("""
                SELECT id FROM competence_majal
                WHERE id_level=? AND id_majal=?
                    AND lower(trim(competence))=lower(trim(?))
                ORDER BY id LIMIT 1
            """, (class_row[1], domain_row[0], domain_competence)).fetchone()
            if competence_row is None:
                raise ValueError(
                    "كفاية المجال الموجودة في الملف لا تتوافق مع القسم والمجال."
                )

            target_plan_id = replace_plan_id
            if target_plan_id is not None:
                exists = c.execute(
                    "SELECT 1 FROM teaching_plan WHERE id=?",
                    (target_plan_id,),
                ).fetchone()
                if exists is None:
                    raise ValueError("المخطط المحدد للاستبدال لم يعد موجوداً.")
                c.execute(
                    "DELETE FROM teaching_plan_activity WHERE plan_id=?",
                    (target_plan_id,),
                )
                c.execute(
                    "DELETE FROM teaching_plan WHERE id=?",
                    (target_plan_id,),
                )

            plan_number = plan_data.get("plan_number")
            number_assigned = bool(plan_number is not None)
            number = int(plan_number) if number_assigned else 0
            if number_assigned:
                conflict = number < 1 or c.execute("""
                    SELECT 1 FROM teaching_plan
                    WHERE classe_id=? AND title=? COLLATE NOCASE
                        AND plan_number=? AND plan_number_assigned=1
                """, (class_row[0], title, number)).fetchone()
                if conflict:
                    highest_number = c.execute("""
                        SELECT MAX(plan_number) FROM teaching_plan
                        WHERE classe_id=? AND title=? COLLATE NOCASE
                            AND plan_number_assigned=1
                    """, (class_row[0], title)).fetchone()[0]
                    number = (highest_number or 0) + 1
            values = (
                title, class_row[0], domain_row[0], competence_row[0],
                str(plan_data.get("start_date") or ""), lesson_count,
                duration,
                mode_counts["منهجي"], mode_counts["إدماج"],
                mode_counts["تقييم"], mode_counts["دعم وعلاج"],
                number, int(number_assigned),
            )
            if target_plan_id is None:
                cursor = c.execute("""
                    INSERT INTO teaching_plan(
                        title, classe_id, majal_id, competence_majal_id,
                        start_date, lesson_count,
                        lesson_duration_minutes, methodical_lesson_count,
                        integration_lesson_count, evaluation_lesson_count,
                        support_lesson_count, plan_number,
                        plan_number_assigned
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, values)
                target_plan_id = cursor.lastrowid
            else:
                c.execute("""
                    INSERT INTO teaching_plan(
                        id, title, classe_id, majal_id, competence_majal_id,
                        start_date, lesson_count,
                        lesson_duration_minutes, methodical_lesson_count,
                        integration_lesson_count, evaluation_lesson_count,
                        support_lesson_count, plan_number,
                        plan_number_assigned
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (target_plan_id, *values))

            c.executemany("""
                INSERT INTO teaching_plan_weekday(
                    plan_id, weekday, lesson_count
                ) VALUES(?, ?, ?)
            """, [
                (target_plan_id, weekday, weekly_schedule[weekday])
                for weekday in weekdays
            ])
            c.executemany("""
                INSERT INTO teaching_plan_property(plan_id, key_name, value_text)
                VALUES(?, ?, ?)
            """, [
                (target_plan_id, str(row.get("key") or "").strip(),
                 str(row.get("value") or "").strip())
                for row in properties if str(row.get("key") or "").strip()
            ])
            imported_item_ids = {}
            for item in items:
                activity_name = str(item.get("activity_name") or "").strip()
                competence_text = str(item.get("competence_text") or "").strip()
                if not activity_name or not competence_text:
                    raise ValueError("يوجد نشاط أو كفاية نشاط فارغة في الملف.")
                subject = str(item.get("subject") or "").strip()
                competence_matiere_id = None
                if subject:
                    match = c.execute("""
                        SELECT cm.id FROM competence_matiere cm
                        WHERE cm.id_competence_majal=?
                            AND lower(trim(cm.matiere))=lower(trim(?))
                            AND lower(trim(cm.competence))=lower(trim(?))
                        ORDER BY cm.id LIMIT 1
                    """, (
                        competence_row[0], subject, competence_text,
                    )).fetchone()
                    competence_matiere_id = match[0] if match else None
                cursor = c.execute("""
                    INSERT INTO teaching_plan_item(
                        plan_id, activity_name, competence_matiere_id,
                        competence_text
                    ) VALUES(?, ?, ?, ?)
                """, (
                    target_plan_id, activity_name, competence_matiere_id,
                    competence_text,
                ))
                new_item_id = cursor.lastrowid
                source_id = item.get("source_id")
                if source_id is not None:
                    imported_item_ids[str(source_id)] = new_item_id
                for detail in self._json_records(
                    item.get("details", []), "تفاصيل النشاط"
                ):
                    component_text = str(
                        detail.get("component_text") or ""
                    ).strip()
                    objective_text = str(
                        detail.get("objective_text") or ""
                    ).strip()
                    content_text = str(
                        detail.get("content_text") or ""
                    ).strip()
                    component_id = None
                    if competence_matiere_id:
                        component = c.execute("""
                            SELECT id FROM moukawan_kifaya
                            WHERE id_competence_matiere=?
                                AND lower(trim(competence))=lower(trim(?))
                            ORDER BY id LIMIT 1
                        """, (
                            competence_matiere_id, component_text,
                        )).fetchone()
                        component_id = component[0] if component else None
                    objective_id = None
                    content_id = None
                    if component_id is not None:
                        objective = c.execute("""
                            SELECT id FROM objectif_specifique
                            WHERE id_moukawa_kifaya=?
                                AND lower(trim(objectif_specifique))=lower(trim(?))
                            ORDER BY id LIMIT 1
                        """, (component_id, objective_text)).fetchone()
                        objective_id = objective[0] if objective else None
                    if objective_id is not None:
                        content = c.execute("""
                            SELECT id FROM content
                            WHERE id_objectif_specifique=?
                                AND lower(trim(content))=lower(trim(?))
                            ORDER BY id LIMIT 1
                        """, (objective_id, content_text)).fetchone()
                        content_id = content[0] if content else None
                    c.execute("""
                        INSERT INTO teaching_plan_item_detail(
                            plan_item_id, competence_component_id,
                            competence_component_text, objective_id,
                            objective_text, content_id, content_text
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """, (
                        new_item_id, component_id, component_text,
                        objective_id, objective_text, content_id, content_text,
                    ))

            for mode_data in settings:
                mode = str(mode_data.get("learning_mode") or "")
                if mode not in modes:
                    raise ValueError("نمط تعلم غير صالح في إعدادات الملف.")
                c.execute("""
                    INSERT INTO teaching_plan_mode_setting(
                        plan_id, learning_mode, expected_performance, is_default
                    ) VALUES(?, ?, ?, ?)
                """, (
                    target_plan_id, mode,
                    str(mode_data.get("expected_performance") or ""),
                    int(bool(mode_data.get("is_default"))),
                ))
                for selection in self._json_records(
                    mode_data.get("selected_items", []), "اختيارات الأنشطة"
                ):
                    new_item_id = imported_item_ids.get(
                        str(selection.get("source_id"))
                    )
                    if new_item_id is None:
                        raise ValueError(
                            "اختيار نشاط يشير إلى نشاط غير موجود بالملف."
                        )
                    objectives = {
                        str(value or "").strip()
                        for value in self._json_list(
                            selection.get("objectives", []),
                            "أهداف النشاط المختارة",
                        )
                    } or {""}
                    c.executemany("""
                        INSERT INTO teaching_plan_mode_selection(
                            plan_id, learning_mode, plan_item_id, objective_text
                        ) VALUES(?, ?, ?, ?)
                    """, [
                        (target_plan_id, mode, new_item_id, objective)
                        for objective in objectives
                    ])
                for selection in self._json_records(
                    mode_data.get("selected_activities", []),
                    "اختيارات الأنشطة المستقلة",
                ):
                    activity_name = str(
                        selection.get("activity_name") or ""
                    ).strip()
                    if not activity_name:
                        raise ValueError("اسم نشاط مختار غير صالح.")
                    objectives = {
                        str(value or "").strip()
                        for value in self._json_list(
                            selection.get("objectives", []),
                            "أهداف النشاط المختارة",
                        )
                    } or {""}
                    c.executemany("""
                        INSERT INTO teaching_plan_mode_activity_selection(
                            plan_id, learning_mode, activity_name,
                            objective_text
                        ) VALUES(?, ?, ?, ?)
                    """, [
                        (target_plan_id, mode, activity_name, objective)
                        for objective in objectives
                    ])

            for record in integration:
                activity_name = str(record.get("activity_name") or "").strip()
                if not activity_name:
                    raise ValueError("اسم نشاط الإدماج في الملف غير صالح.")
                c.execute("""
                    INSERT INTO teaching_plan_integration_activity(
                        plan_id, activity_name, prompt_text, template_text,
                        target_text
                    ) VALUES(?, ?, ?, ?, ?)
                """, (
                    target_plan_id, activity_name,
                    str(record.get("prompt_text") or ""),
                    str(record.get("template_text") or ""),
                    str(record.get("target_text") or ""),
                ))
                c.executemany("""
                    INSERT INTO teaching_plan_integration_content(
                        plan_id, activity_name, content_text
                    ) VALUES(?, ?, ?)
                """, [
                    (target_plan_id, activity_name, str(content))
                    for content in self._json_list(
                        record.get("contents", []), "محتويات الإدماج"
                    )
                ])
            for record in evaluation:
                activity_name = str(record.get("activity_name") or "").strip()
                if not activity_name:
                    raise ValueError("اسم نشاط التقييم في الملف غير صالح.")
                c.execute("""
                    INSERT INTO teaching_plan_evaluation_activity(
                        plan_id, activity_name, prompt_text, template_text,
                        target_text
                    ) VALUES(?, ?, ?, ?, ?)
                """, (
                    target_plan_id, activity_name,
                    str(record.get("prompt_text") or ""),
                    str(record.get("template_text") or ""),
                    str(record.get("target_text") or ""),
                ))
                c.executemany("""
                    INSERT INTO teaching_plan_evaluation_content(
                        plan_id, activity_name, content_text
                    ) VALUES(?, ?, ?)
                """, [
                    (target_plan_id, activity_name, str(content))
                    for content in self._json_list(
                        record.get("contents", []), "محتويات التقييم"
                    )
                ])
            for record in support:
                activity_name = str(record.get("activity_name") or "").strip()
                if not activity_name:
                    raise ValueError("اسم نشاط الدعم والعلاج في الملف غير صالح.")
                c.execute("""
                    INSERT INTO teaching_plan_support_activity(
                        plan_id, activity_name, target_criteria
                    ) VALUES(?, ?, ?)
                """, (
                    target_plan_id,
                    activity_name,
                    str(record.get("target_criteria") or ""),
                ))

            for record in activities:
                mode = str(record.get("learning_mode") or "")
                activity_name = str(record.get("activity_name") or "").strip()
                try:
                    lesson_number = int(record.get("lesson_number", 0))
                    duration_minutes = int(record.get("duration_minutes", 0))
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError(
                        "يوجد توزيع حصة غير صالح في ملف JSON."
                    ) from error
                if (
                    mode not in modes or not activity_name
                    or not 1 <= lesson_number <= lesson_count
                    or not 1 <= duration_minutes <= 300
                ):
                    raise ValueError("يوجد توزيع حصة غير صالح في ملف JSON.")
                source_item_id = record.get("item_source_id")
                new_item_id = (
                    imported_item_ids.get(str(source_item_id))
                    if source_item_id is not None else None
                )
                if source_item_id is not None and new_item_id is None:
                    raise ValueError(
                        "توزيع الحصة يشير إلى نشاط غير موجود بالملف."
                    )
                if new_item_id is None and mode == "منهجي":
                    raise ValueError(
                        "النشاط المنهجي في الملف غير مرتبط بنشاط مخطط."
                    )
                field_names = (
                    "content", "lesson_goal", "specific_objective",
                    "target_competencies", "evaluation",
                    "expected_performance", "target_criteria",
                    "demographic_concept", "health_concept", "life_skills",
                )
                field_values = [
                    str(record.get(key) or "") for key in field_names
                ]
                cursor = c.execute("""
                    INSERT INTO teaching_plan_activity(
                        plan_id, lesson_number, learning_mode, plan_item_id,
                        activity_name, content, lesson_goal,
                        specific_objective, target_competencies, evaluation,
                        expected_performance, target_criteria,
                        demographic_concept, health_concept, life_skills,
                        duration_minutes
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    target_plan_id, lesson_number, mode, new_item_id,
                    activity_name, *field_values, duration_minutes,
                ))
                new_activity_id = cursor.lastrowid
                activity_props = self._json_records(
                    record.get("properties", []), "خصائص النشاط"
                )
                c.executemany("""
                    INSERT INTO teaching_plan_activity_property(
                        activity_id, key_name, value_text
                    ) VALUES(?, ?, ?)
                """, [
                    (
                        new_activity_id,
                        str(prop.get("key") or "").strip(),
                        str(prop.get("value") or ""),
                    )
                    for prop in activity_props
                    if str(prop.get("key") or "").strip()
                ])
            return target_plan_id

    def delete_teaching_plan(self, plan_id):
        with self.managed_connection() as c:
            c.execute("DELETE FROM teaching_plan WHERE id=?", (plan_id,))

    def teaching_plan_activities(self, plan_id):
        with self.managed_connection() as c:
            rows = c.execute("""
                SELECT a.id, a.lesson_number, a.learning_mode, a.plan_item_id,
                    COALESCE(NULLIF(a.activity_name, ''), i.activity_name, ''),
                    COALESCE(i.competence_text, ''), a.content, a.lesson_goal,
                    a.specific_objective, a.target_competencies, a.evaluation,
                    a.expected_performance, a.target_criteria, a.demographic_concept,
                    a.health_concept, a.life_skills, a.duration_minutes
                FROM teaching_plan_activity a
                LEFT JOIN teaching_plan_item i ON i.id=a.plan_item_id
                WHERE a.plan_id=?
                ORDER BY a.lesson_number, a.id
            """, (plan_id,)).fetchall()
            properties = c.execute("""
                SELECT p.activity_id, p.id, p.key_name, p.value_text
                FROM teaching_plan_activity_property p
                JOIN teaching_plan_activity a ON a.id=p.activity_id
                WHERE a.plan_id=? ORDER BY p.id
            """, (plan_id,)).fetchall()
        by_activity = {}
        for activity_id, property_id, key, value in properties:
            by_activity.setdefault(activity_id, []).append(
                (property_id, key, value)
            )
        return [(row, by_activity.get(row[0], [])) for row in rows]

    def save_teaching_plan_activity(
        self, plan_id, lesson_number, learning_mode, plan_item_id,
        fields, properties=(), activity_id=None, duration_minutes=60,
        activity_name=None,
    ):
        mode = str(learning_mode or "").strip()
        if mode not in ("منهجي", "إدماج", "تقييم", "دعم وعلاج"):
            raise ValueError("نمط التعلم المختار غير صالح.")
        lesson_number = int(lesson_number)
        if lesson_number < 1:
            raise ValueError("رقم الحصة يجب أن يكون أكبر من صفر.")
        duration_minutes = int(duration_minutes)
        if not 1 <= duration_minutes <= 300:
            raise ValueError("مدة النشاط يجب أن تكون بين دقيقة و300 دقيقة.")
        with self.managed_connection() as c:
            if plan_item_id is None:
                if mode not in ("إدماج", "تقييم", "دعم وعلاج"):
                    raise ValueError(
                        "يجب ربط النشاط بنشاط مخطط قبل توزيعه."
                    )
                activity_name = str(activity_name or "").strip()
                if mode == "إدماج":
                    mode_activity = c.execute("""
                        SELECT target_text
                        FROM teaching_plan_integration_activity
                        WHERE plan_id=?
                            AND lower(trim(activity_name))=lower(trim(?))
                    """, (plan_id, activity_name)).fetchone()
                elif mode == "تقييم":
                    mode_activity = c.execute("""
                        SELECT target_text
                        FROM teaching_plan_evaluation_activity
                        WHERE plan_id=?
                            AND lower(trim(activity_name))=lower(trim(?))
                    """, (plan_id, activity_name)).fetchone()
                else:
                    mode_activity = c.execute("""
                        SELECT target_criteria
                        FROM teaching_plan_support_activity
                        WHERE plan_id=?
                            AND lower(trim(activity_name))=lower(trim(?))
                    """, (plan_id, activity_name)).fetchone()
                plan = c.execute(
                    "SELECT lesson_count FROM teaching_plan WHERE id=?",
                    (plan_id,),
                ).fetchone()
                if not activity_name or mode_activity is None or plan is None:
                    raise ValueError(
                        "النشاط غير موجود في إعدادات المخطط للنمط المحدد."
                    )
                valid_item = (plan[0], activity_name)
            else:
                valid_item = c.execute("""
                    SELECT p.lesson_count, i.activity_name
                    FROM teaching_plan p
                    JOIN teaching_plan_item i ON i.plan_id=p.id AND i.id=?
                    WHERE p.id=? AND i.competence_text<>''
                """, (plan_item_id, plan_id)).fetchone()
                if not valid_item:
                    raise ValueError(
                        "النشاط غير موجود أو لم تُستكمل كفايته."
                    )
                activity_name = valid_item[1]
            if lesson_number > valid_item[0]:
                raise ValueError("رقم الحصة يتجاوز عدد حصص المخطط.")
            if (
                mode == "إدماج"
                and not str(fields.get("target_competencies", "") or "").strip()
            ):
                integration_activity = c.execute("""
                    SELECT target_text
                    FROM teaching_plan_integration_activity
                    WHERE plan_id=? AND lower(trim(activity_name))=lower(trim(?))
                """, (plan_id, activity_name)).fetchone()
                if integration_activity:
                    fields = dict(fields)
                    fields["target_competencies"] = integration_activity[0]
            if (
                mode == "تقييم"
                and not str(fields.get("expected_performance", "") or "").strip()
            ):
                evaluation_activity = c.execute("""
                    SELECT target_text
                    FROM teaching_plan_evaluation_activity
                    WHERE plan_id=? AND lower(trim(activity_name))=lower(trim(?))
                """, (plan_id, activity_name)).fetchone()
                if evaluation_activity:
                    fields = dict(fields)
                    fields["expected_performance"] = evaluation_activity[0]
            if (
                mode == "دعم وعلاج"
                and not str(fields.get("target_criteria", "") or "").strip()
            ):
                support_activity = c.execute("""
                    SELECT target_criteria
                    FROM teaching_plan_support_activity
                    WHERE plan_id=? AND lower(trim(activity_name))=lower(trim(?))
                """, (plan_id, activity_name)).fetchone()
                if support_activity:
                    fields = dict(fields)
                    fields["target_criteria"] = support_activity[0]

            field_names = (
                "content", "lesson_goal", "specific_objective",
                "target_competencies", "evaluation", "expected_performance",
                "target_criteria", "demographic_concept", "health_concept",
                "life_skills",
            )
            values = [str(fields.get(key, "") or "").strip() for key in field_names]
            required = {
                "منهجي": ("content",),
                "إدماج": ("target_competencies",),
                "تقييم": ("expected_performance",),
                "دعم وعلاج": (),
            }[mode]
            for key in required:
                if not str(fields.get(key, "") or "").strip():
                    labels = dict(self._plan_activity_field_labels())
                    raise ValueError(
                        f"الخاصية المطلوبة «{labels[key]}» فارغة."
                    )

            if activity_id is None:
                cursor = c.execute("""
                    INSERT INTO teaching_plan_activity(
                        plan_id, lesson_number, learning_mode,
                        plan_item_id, activity_name, content, lesson_goal,
                        specific_objective, target_competencies, evaluation,
                        expected_performance, target_criteria, demographic_concept,
                        health_concept, life_skills, duration_minutes
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    plan_id, lesson_number, mode, plan_item_id, activity_name,
                    *values, duration_minutes,
                ))
                activity_id = cursor.lastrowid
            else:
                c.execute("""
                    UPDATE teaching_plan_activity
                    SET lesson_number=?, learning_mode=?, plan_item_id=?,
                        activity_name=?,
                        content=?, lesson_goal=?, specific_objective=?,
                        target_competencies=?, evaluation=?, expected_performance=?,
                        target_criteria=?, demographic_concept=?, health_concept=?,
                        life_skills=?, duration_minutes=?
                    WHERE id=? AND plan_id=?
                """, (
                    lesson_number, mode, plan_item_id, activity_name, *values,
                    duration_minutes,
                    activity_id, plan_id,
                ))
                c.execute(
                    "DELETE FROM teaching_plan_activity_property WHERE activity_id=?",
                    (activity_id,),
                )
            c.executemany("""
                INSERT INTO teaching_plan_activity_property(
                    activity_id, key_name, value_text
                ) VALUES(?, ?, ?)
            """, [
                (activity_id, str(key).strip(), str(value).strip())
                for key, value in properties
                if str(key).strip()
            ])
            return activity_id

    @staticmethod
    def _plan_activity_field_labels():
        return (
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

    def delete_teaching_plan_activity(self, activity_id):
        with self.managed_connection() as c:
            c.execute(
                "DELETE FROM teaching_plan_activity WHERE id=?", (activity_id,)
            )

    # ---------------------------------------------------------
    # المفاهيم الجديدة (concept): اسم العنصر + محتوى العنصر
    # ---------------------------------------------------------
    def concepts(self):
        with self.connect() as c:
            return c.execute(
                "SELECT id, name, content FROM concept ORDER BY id ASC"
            ).fetchall()

    def save_concept(self, name, content, concept_id=None):
        """إضافة/تعديل مفهوم. يرفع ValueError إذا كان الاسم مكرراً."""
        name = (name or "").strip()
        if not name:
            raise ValueError("اسم المفهوم مطلوب")
        with self.connect() as c:
            duplicate = c.execute(
                "SELECT id FROM concept WHERE name = ? AND id IS NOT ?",
                (name, concept_id),
            ).fetchone()
            if duplicate:
                raise ValueError(f"المفهوم '{name}' موجود مسبقاً")
            if concept_id:
                c.execute(
                    "UPDATE concept SET name=?, content=? WHERE id=?",
                    (name, content, concept_id),
                )
                return concept_id
            cur = c.execute(
                "INSERT INTO concept(name, content) VALUES(?, ?)",
                (name, content),
            )
            return cur.lastrowid

    def delete_concept(self, concept_id):
        with self.connect() as c:
            c.execute("DELETE FROM concept WHERE id=?", (concept_id,))

    def predict_next_detail_key(self, activity_title, namat_taallom, current_count):
        with self.connect() as c:
            rows = c.execute("""
                SELECT n.id, d.key_name
                FROM nachat_detail d
                JOIN nachat n ON d.nachat_id = n.id
                JOIN hessa h ON n.hessa_id = h.id
                LEFT JOIN learning_mode lm ON lm.id=h.learning_mode_id
                WHERE n.title = ? AND lm.name = ?
                ORDER BY n.id, d.id
            """, (activity_title, namat_taallom)).fetchall()

        sequences = {}
        for nachat_id, key_name in rows:
            sequences.setdefault(nachat_id, []).append(key_name)

        candidates = [
            sequence[current_count]
            for sequence in sequences.values()
            if len(sequence) > current_count
        ]
        if candidates:
            from collections import Counter
            return Counter(candidates).most_common(1)[0][0]
        return ""


class SchoolDataFormatter:
    def __init__(self, db_name=DB_PATH):
        self.db_name = str(db_name)

    def get_connection(self):
        conn = connect_database(self.db_name)
        conn.row_factory = sqlite3.Row
        return conn

    def normalize_date(self, date_str: str) -> str:
        date_str = date_str.strip()
        formats_to_try = ["%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"]
        for fmt in formats_to_try:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return date_str
