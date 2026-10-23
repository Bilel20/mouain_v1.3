# -*- coding: utf-8 -*-
"""Extracted module app_config."""
from pathlib import Path
import sys

from PySide6.QtCore import QTranslator
from PySide6.QtGui import QFontDatabase

BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR
ICON_PATH = BASE_DIR / "icons" / "icon.ico"
FONTS_DIR = BASE_DIR / "fonts"
TEMPLATE_PATH = BASE_DIR / "template.odt"

def get_db_path():
    """Return the database path beside the application, independent of cwd."""
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = BASE_DIR
    return str(base_dir / "school_data.db")

DB_PATH = get_db_path()

SCHOOL_INFO_COLUMNS = (
    "nom_annee",
    "nom_ecole",
    "nom_enseignant",
    "nom_inspecteur",
    "grade",
    "nom_assistant_pedagogique",
    "diplomes_scientifiques",
    "date_recrutement",
    "date_titularisation",
    "date_derniere_visite",
    "note_pedagogique",
    "resume_rapport_visite",
    "langue",
    "telephone",
    "email",
    "situation_familiale",
    "nombre_enfants",
)

ACTIVITIES_BY_MAJAL = {
    "اللغة العربية": ["تواصل شفوي", "قراءة", "إنتاج كتابي", "استعمال قواعد اللغة",
                      "محفوظات", "وحدة اللغة العربية", "رسم وإملاء", "إملاء", "رسم", "مطالعة", "حوار منظم"],
    "العلوم والتكنولوجيا": ["رياضيات", "إيقاظ علمي", "تربية تكنولوجية"],
    "التنشئة الفنية": ["تربية تشكيلية", "تربية موسيقية"],
    "التنشئة الاجتماعية": ["تربية إسلامية", "هدي قرآني", "عقيدة", "أخلاق", "عبادات"],
    "التنشئة البدنية": ["رياضة"],
}
DETAIL_KEYS_BY_NAMAT = {
    "منهجي": ["المحتوى", "هدف الحصة", "السند الرئيسي", "مفهوم صحي أو سكاني", "المهارة الحياتية", "القصيدة"],
    "إدماج": ["الكفايات المستهدفة", "مفهوم صحي أو سكاني", "المهارة الحياتية"],
    "تقييم": ["الأداء المنتظر", "مفهوم صحي أو سكاني", "المهارة الحياتية"],
    "دعم وعلاج": ["المعايير المستهدفة", "مفهوم صحي أو سكاني", "المهارة الحياتية"],
}

PLANS_NAMES = {
    "وحدة اللغة العربية": {"المجال":"اللغة العربية"},
    "رياضيات": {"المجال":"العلوم والتكنولوجيا"},
    "إيقاظ علمي": {"المجال":"العلوم والتكنولوجيا"},
    "تربية تكنولوجية": {"المجال":"العلوم والتكنولوجيا"},
    "تربية تشكيلية": {"المجال":"التنشئة الفنية"},
    "تربية موسيقية": {"المجال":"التنشئة الفنية"},
    "تربية إسلامية": {"المجال":"التنشئة الاجتماعية"},
    "تاريخ": {"المجال":"التنشئة الاجتماعية"},
    "جغرافيا": {"المجال":"التنشئة الاجتماعية"},
    "تربية مدنية": {"المجال":"التنشئة الاجتماعية"},
    "مواد لاجتماعية": {"المجال":"التنشئة الاجتماعية"},
    "تنشئة فنية": {"المجال":"التنشئة الفنية"},
    "رياضة": {"المجال":"التنشئة البدنية"},
}


def set_dialog_button_action_types(button_box):
    for button in button_box.buttons():
        text = button.text().replace("&", "")
        if "إغلاق" in text or "اغلاق" in text:
            button.setProperty("actionType", "close")
        elif any(label in text for label in ("تراجع", "رجوع", "عودة")):
            button.setProperty("actionType", "back")
        elif "إلغاء" in text or "الغاء" in text:
            button.setProperty("actionType", "cancel")
        elif any(label in text for label in ("حفظ", "موافق", "نعم", "متابعة الاستيراد", "إضافة الحصص")):
            button.setProperty("actionType", "save")


def load_custom_font():
    if FONTS_DIR.exists() and FONTS_DIR.is_dir():
        font_files = list(FONTS_DIR.glob("*.ttf")) + list(FONTS_DIR.glob("*.otf"))
        if font_files:
            font_path = str(font_files[0])
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id != -1:
                font_families = QFontDatabase.applicationFontFamilies(font_id)
                if font_families:
                    return font_families[0]
    return "Noto Naskh Arabic"


class ArabicDialogTranslator(QTranslator):
    BUTTON_TRANSLATIONS = {
        "&Yes": "&نعم",
        "Yes": "نعم",
        "&No": "&لا",
        "No": "لا",
        "&OK": "&حسناً",
        "OK": "حسناً",
        "&Cancel": "&إلغاء",
        "Cancel": "إلغاء",
        "&Close": "&إغلاق",
        "Close": "إغلاق",
        "&Save": "&حفظ",
        "Save": "حفظ",
        "Save All": "حفظ الكل",
        "Don't Save": "عدم الحفظ",
        "&Open": "&فتح",
        "Open": "فتح",
        "&Apply": "&تطبيق",
        "Apply": "تطبيق",
        "&Discard": "&تجاهل",
        "Discard": "تجاهل",
        "&Retry": "&إعادة المحاولة",
        "Retry": "إعادة المحاولة",
        "&Abort": "&إيقاف",
        "Abort": "إيقاف",
        "&Ignore": "&تجاهل",
        "Ignore": "تجاهل",
        "Yes to All": "نعم للكل",
        "No to All": "لا للكل",
        "Restore Defaults": "استعادة الافتراضي",
        "&Help": "&مساعدة",
        "Help": "مساعدة",
        "&Reset": "&إعادة التعيين",
        "Reset": "إعادة التعيين",
    }

    def translate(self, context, source_text, disambiguation=None, n=-1):
        return self.BUTTON_TRANSLATIONS.get(source_text, "")
