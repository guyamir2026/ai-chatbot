"""
טסטים ללוגיקת מיזוג/סינון עמודים בזרימת ה-OAuth של מטא.

- `messaging.meta_graph_client.merge_pages_by_id` — ה-helper היחיד למיזוג
  עמודים (dedup לפי id + העדפת גרסה עם token). משמש גם בתוך
  list_business_pages וגם בזרימת ה-callback ב-admin/meta_oauth.py.
- `admin.meta_oauth._any_page_with_token` — התנאי שקובע אם יש עמוד שמיש.

שניהם נכתבו כדי לטפל בתרחישים שבהם /me/accounts מחזיר עמודים חלקיים /
בלי token בעוד עמודי תיק עסקי חסרים (הערות review של Bugbot על PR #305).
"""


class TestAnyPageWithToken:
    def test_true_when_token_present(self):
        from admin.meta_oauth import _any_page_with_token
        assert _any_page_with_token(
            [{"id": "P1"}, {"id": "P2", "access_token": "t"}]
        ) is True

    def test_false_when_no_tokens(self):
        from admin.meta_oauth import _any_page_with_token
        assert _any_page_with_token([{"id": "P1"}, {"id": "P2"}]) is False

    def test_false_when_empty(self):
        from admin.meta_oauth import _any_page_with_token
        assert _any_page_with_token([]) is False


class TestMergePagesById:
    def test_dedups_by_id_preserving_order(self):
        from messaging.meta_graph_client import merge_pages_by_id
        existing = [{"id": "P1", "access_token": "t1"}]
        new = [
            {"id": "P1", "access_token": "t1"},
            {"id": "P2", "access_token": "t2"},
        ]
        merged = merge_pages_by_id(existing, new)
        assert [p["id"] for p in merged] == ["P1", "P2"]

    def test_prefers_version_with_token(self):
        """עמוד בלי token מ-/me/accounts מוחלף בגרסה עם token מהתיק."""
        from messaging.meta_graph_client import merge_pages_by_id
        existing = [{"id": "P1"}]                       # בלי token
        new = [{"id": "P1", "access_token": "tok"}]     # עם token
        merged = merge_pages_by_id(existing, new)
        assert len(merged) == 1
        assert merged[0]["access_token"] == "tok"

    def test_keeps_existing_token_when_new_has_none(self):
        """כשהקיים עם token והחדש בלי — לא דורסים את הגרסה השמישה."""
        from messaging.meta_graph_client import merge_pages_by_id
        existing = [{"id": "P1", "access_token": "tok"}]
        new = [{"id": "P1"}]
        merged = merge_pages_by_id(existing, new)
        assert merged[0]["access_token"] == "tok"

    def test_cross_portfolio_dedup(self):
        """אותו עמוד משני תיקים שונים — מופיע פעם אחת (הערה 2 של Bugbot)."""
        from messaging.meta_graph_client import merge_pages_by_id
        from_biz_a = [{"id": "P1", "access_token": "t"}]
        from_biz_b = [
            {"id": "P1", "access_token": "t"},
            {"id": "P2", "access_token": "t2"},
        ]
        merged = merge_pages_by_id(from_biz_a, from_biz_b)
        assert [p["id"] for p in merged] == ["P1", "P2"]

    def test_filters_non_dict_and_idless(self):
        from messaging.meta_graph_client import merge_pages_by_id
        merged = merge_pages_by_id(
            [{"id": "OK", "access_token": "t"}],
            ["not-a-dict", {"name": "no-id"}, {"id": "P2", "access_token": "t2"}],
        )
        assert [p["id"] for p in merged] == ["OK", "P2"]

    def test_variadic_more_than_two_lists(self):
        """תומך במיזוג של יותר משתי רשימות (owned + client + ...)."""
        from messaging.meta_graph_client import merge_pages_by_id
        merged = merge_pages_by_id(
            [{"id": "A", "access_token": "a"}],
            [{"id": "B", "access_token": "b"}],
            [{"id": "A", "access_token": "a"}, {"id": "C", "access_token": "c"}],
        )
        assert [p["id"] for p in merged] == ["A", "B", "C"]

    def test_empty_inputs(self):
        from messaging.meta_graph_client import merge_pages_by_id
        assert merge_pages_by_id() == []
        assert merge_pages_by_id([], []) == []
