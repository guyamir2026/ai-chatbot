"""
WSGI entrypoint for production servers (e.g. gunicorn).

Render can run this with:
  gunicorn admin.wsgi:app --bind 0.0.0.0:$PORT

(הנתיב הישן `ai_chatbot.admin.wsgi:app` ממשיך לעבוד — ai_chatbot הוא
namespace של aliases לשורש, ראה ai_chatbot/__init__.py.)
"""

from admin.app import create_admin_app

app = create_admin_app()
