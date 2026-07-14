"""
ai_chatbot — namespace של aliases לקוד שבשורש הריפו.

קוד המקור חי בשורש (config.py, database.py, admin/, bot/, rag/ ...), אבל
חלקים מהקוד מייבאים דרך `ai_chatbot.*`. היסטורית זה נפתר עם קבצי wrapper
של `from X import *`, מה שיצר **שני אובייקטי מודול לכל מודול** — עם עותקי
state נפרדים ברמת המודול (ראה ההערה ההיסטורית ב-tests/conftest.py על
double-patching של DB_PATH).

הפתרון כאן הוא meta-path finder: כל ייבוא של `ai_chatbot.X` מחזיר את
**אותו אובייקט מודול** של `X` מהשורש. אין קבצי wrapper, אין כפילות state,
ומודול חדש בשורש זמין אוטומטית גם כ-`ai_chatbot.<שם>` בלי שום צעד נוסף.

איך זה עובד: ה-loader נותן למכונת הייבוא ליצור מודול זמני ריק, וב-
exec_module מחליף את הרשומה ב-sys.modules במודול השורש. מכונת הייבוא
קוראת מחדש את sys.modules אחרי exec (התנהגות מתועדת של importlib), כך
שהמייבא מקבל את מודול השורש עצמו. חשוב: לא מחזירים את מודול השורש
מ-create_module — זה היה גורם ל-importlib לשכתב לו את __name__/__spec__.
"""

import importlib
import importlib.abc
import importlib.machinery
import sys

__version__ = "1.0.0"

# מודולים שנשארים קבצים פיזיים בתוך החבילה (לא alias לשורש)
_PHYSICAL_SUBMODULES = {"__main__"}


class _RootAliasLoader(importlib.abc.Loader):
    """טוען שמחליף את המודול הזמני במודול השורש המקביל (אותו אובייקט)."""

    def create_module(self, spec):
        return None  # ברירת המחדל — מודול זמני ריק שיוחלף ב-exec_module

    def exec_module(self, module):
        fullname = module.__spec__.name
        root_name = fullname.split(".", 1)[1]
        try:
            root_module = importlib.import_module(root_name)
        except ModuleNotFoundError as exc:
            # שגיאה בשם המלא כדי שהמייבא יבין מה באמת חסר
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (alias of root module {root_name!r})",
                name=fullname,
            ) from exc
        sys.modules[fullname] = root_module


class _RootAliasFinder(importlib.abc.MetaPathFinder):
    """ממפה `ai_chatbot.<name>` למודול `<name>` בשורש הריפו."""

    _PREFIX = __name__ + "."

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self._PREFIX):
            return None
        root_name = fullname[len(self._PREFIX):]
        if root_name.split(".", 1)[0] in _PHYSICAL_SUBMODULES:
            return None  # ייטען מהקובץ הפיזי שבתוך החבילה
        return importlib.machinery.ModuleSpec(fullname, _RootAliasLoader())


# רישום פעם אחת (ייבוא חוזר של החבילה לא מכפיל את ה-finder)
if not any(isinstance(f, _RootAliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _RootAliasFinder())
