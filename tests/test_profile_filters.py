from datetime import datetime
import unittest

from profile_ui import profile_filter_reason


class ProfileFilterTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 11, 0, 0, 0)
        self.end = datetime(2026, 8, 11, 23, 59, 59, 999999)

    def post(self, published: datetime | None, comments: int = 20) -> dict:
        return {
            "publish_timestamp": int(published.timestamp()) if published else 0,
            "comment_count": comments,
        }

    def test_low_comment_count_is_filtered(self):
        result = profile_filter_reason(self.post(self.start, 3), 10, None, None)
        self.assertEqual(result, ("low_comments", "评论数不足：3＜10"))

    def test_today_is_allowed(self):
        self.assertIsNone(
            profile_filter_reason(self.post(datetime(2026, 8, 11, 12, 30)), 10, self.start, self.end)
        )

    def test_yesterday_is_filtered(self):
        result = profile_filter_reason(
            self.post(datetime(2026, 8, 10, 12, 30)), 10, self.start, self.end
        )
        self.assertEqual(result[0], "time_filtered")
        self.assertIn("2026-08-10 12:30:00", result[1])

    def test_custom_range_includes_boundaries(self):
        self.assertIsNone(profile_filter_reason(self.post(self.start), 0, self.start, self.end))
        self.assertIsNone(profile_filter_reason(self.post(self.end), 0, self.start, self.end))

    def test_unknown_publish_time_is_filtered_when_time_limited(self):
        self.assertEqual(
            profile_filter_reason(self.post(None), 0, self.start, self.end),
            ("time_filtered", "发布时间未知"),
        )


if __name__ == "__main__":
    unittest.main()
