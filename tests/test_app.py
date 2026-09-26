import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from fetcher import (
    LiveFeedUnavailableError,
    LiveSearchUnavailableError,
    LiveTestUnavailableError,
    ProviderRequestError,
)


VIDEO = {
    "video_id": "dQw4w9WgXcQ",
    "title": "A useful hardware project",
    "channel_name": "Workshop",
    "description": "A repair guide.",
    "curation_reason": "It teaches a concrete repair technique.",
}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_directory = Path(self.temporary_directory.name)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.data_directory})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_empty_collections_are_arrays(self):
        self.assertEqual(self.client.get("/api/feed").get_json(), [])
        self.assertEqual(self.client.get("/api/watch-later").get_json(), [])

    def test_index_renders_the_productivity_feed(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<title>Productivity Feed</title>", response.data)
        self.assertIn(b'id="search-query"', response.data)

    def test_watch_later_addition_is_persistent_and_deduplicated(self):
        first_response = self.client.post("/api/watch-later", json=VIDEO)
        second_response = self.client.post("/api/watch-later", json=VIDEO)

        self.assertEqual(first_response.status_code, 201)
        self.assertFalse(first_response.get_json()["already_saved"])
        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(second_response.get_json()["already_saved"])

        with (self.data_directory / "watch_later.json").open(encoding="utf-8") as file:
            self.assertEqual(json.load(file), [VIDEO])

    def test_watch_later_removal_is_idempotent(self):
        self.client.post("/api/watch-later", json=VIDEO)

        self.assertTrue(
            self.client.delete(f"/api/watch-later/{VIDEO['video_id']}").get_json()["removed"]
        )
        self.assertFalse(
            self.client.delete(f"/api/watch-later/{VIDEO['video_id']}").get_json()["removed"]
        )

    def test_refresh_does_not_change_the_feed(self):
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")

        with patch(
            "app.fetch_latest_feed",
            side_effect=LiveFeedUnavailableError("Live feed is not configured."),
        ):
            response = self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()["error"], "feed_refresh_not_configured")
        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [VIDEO])

    def test_refresh_replaces_the_ephemeral_feed_after_success(self):
        with patch("app.fetch_latest_feed", return_value=[VIDEO]):
            response = self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"refreshed": 1, "added": 1})
        self.assertEqual(
            json.loads((self.data_directory / "feed.json").read_text(encoding="utf-8")),
            [VIDEO],
        )

    def test_refresh_appends_only_unseen_videos_to_the_feed(self):
        existing_video = dict(VIDEO, video_id="aaaaaaaaaaa")
        new_video = dict(VIDEO, video_id="bbbbbbbbbbb")
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([existing_video]), encoding="utf-8")

        with patch("app.fetch_latest_feed", return_value=[existing_video, new_video]):
            response = self.client.post("/api/refresh")

        self.assertEqual(response.get_json(), {"refreshed": 2, "added": 1})
        self.assertEqual(
            json.loads(feed_path.read_text(encoding="utf-8")),
            [existing_video, new_video],
        )

    def test_refresh_returns_a_provider_failure(self):
        with patch(
            "app.fetch_latest_feed",
            side_effect=ProviderRequestError("YouTube returned HTTP 403."),
        ):
            response = self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "feed_refresh_failed")

    def test_search_does_not_change_the_feed_before_configuration(self):
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")

        with patch(
            "app.search_and_filter_videos",
            side_effect=LiveSearchUnavailableError("Search is not configured."),
        ):
            response = self.client.post("/api/search", json={"query": "home servers"})

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()["error"], "search_not_configured")
        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [VIDEO])

    def test_search_appends_the_ephemeral_feed_after_success(self):
        with patch("app.search_and_filter_videos", return_value=[VIDEO]) as search:
            response = self.client.post("/api/search", json={"query": "home servers"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(), {"query": "home servers", "approved": 1, "added": 1}
        )
        search.assert_called_once_with(
            "home servers",
            [],
            30,
            self.client.get("/api/settings").get_json()["curation_prompt"],
        )
        self.assertEqual(
            json.loads((self.data_directory / "feed.json").read_text(encoding="utf-8")),
            [VIDEO],
        )

    def test_feed_removal_is_persistent_and_idempotent(self):
        self.client.post("/api/refresh")
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")

        first_response = self.client.delete(f"/api/feed/{VIDEO['video_id']}")
        second_response = self.client.delete(f"/api/feed/{VIDEO['video_id']}")

        self.assertTrue(first_response.get_json()["removed"])
        self.assertFalse(second_response.get_json()["removed"])
        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [])

    def test_feed_removal_rejects_invalid_video_ids(self):
        response = self.client.delete("/api/feed/invalid")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_video_id")

    def test_search_rejects_an_empty_query(self):
        response = self.client.post("/api/search", json={"query": "   "})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_search_query")

    def test_invalid_video_is_rejected(self):
        response = self.client.post("/api/watch-later", json={"video_id": "invalid"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_video")

    def test_trusted_creators_start_empty(self):
        self.assertEqual(self.client.get("/api/trusted-creators").get_json(), [])

    def test_trusted_creator_addition_is_persistent_and_deduplicated(self):
        first_response = self.client.post("/api/trusted-creators", json={"name": "Workshop"})
        second_response = self.client.post(
            "/api/trusted-creators", json={"name": "  workshop  "}
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertFalse(first_response.get_json()["already_trusted"])
        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(second_response.get_json()["already_trusted"])

        with (self.data_directory / "trusted_creators.json").open(encoding="utf-8") as file:
            self.assertEqual(json.load(file), ["Workshop"])

    def test_trusted_creator_removal_is_case_insensitive_and_idempotent(self):
        self.client.post("/api/trusted-creators", json={"name": "Workshop"})

        self.assertTrue(
            self.client.delete("/api/trusted-creators/WORKSHOP").get_json()["removed"]
        )
        self.assertFalse(
            self.client.delete("/api/trusted-creators/WORKSHOP").get_json()["removed"]
        )

    def test_trusted_creator_name_is_required(self):
        response = self.client.post("/api/trusted-creators", json={"name": "   "})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_creator_name")

    def test_refresh_passes_trusted_creators_to_the_fetcher(self):
        self.client.post("/api/trusted-creators", json={"name": "Workshop"})

        with patch("app.fetch_latest_feed", return_value=[VIDEO]) as fetch:
            self.client.post("/api/refresh")

        fetch.assert_called_once_with(
            ["Workshop"],
            30,
            self.client.get("/api/settings").get_json()["curation_prompt"],
        )

    def test_settings_start_at_defaults(self):
        settings = self.client.get("/api/settings").get_json()

        self.assertEqual(settings["refresh_candidate_limit"], 30)
        self.assertEqual(settings["search_candidate_limit"], 30)
        self.assertEqual(settings["curation_prompt"], settings["default_curation_prompt"])

    def test_settings_partial_update_is_persisted(self):
        response = self.client.post("/api/settings", json={"refresh_candidate_limit": 10})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["refresh_candidate_limit"], 10)
        self.assertEqual(response.get_json()["search_candidate_limit"], 30)
        self.assertEqual(
            self.client.get("/api/settings").get_json()["refresh_candidate_limit"], 10
        )

    def test_settings_saves_a_custom_curation_prompt(self):
        prompt = "Only approve videos about woodworking."
        response = self.client.post("/api/settings", json={"curation_prompt": prompt})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["curation_prompt"], prompt)
        self.assertEqual(self.client.get("/api/settings").get_json()["curation_prompt"], prompt)

    def test_settings_reject_an_empty_curation_prompt(self):
        response = self.client.post("/api/settings", json={"curation_prompt": "   "})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_settings_reject_out_of_range_values(self):
        response = self.client.post("/api/settings", json={"refresh_candidate_limit": 0})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_settings_reject_non_integer_values(self):
        response = self.client.post("/api/settings", json={"search_candidate_limit": "30"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_refresh_uses_the_configured_candidate_limit(self):
        self.client.post("/api/settings", json={"refresh_candidate_limit": 5})

        with patch("app.fetch_latest_feed", return_value=[VIDEO]) as fetch:
            self.client.post("/api/refresh")

        fetch.assert_called_once_with([], 5, self.client.get("/api/settings").get_json()["curation_prompt"])

    def test_search_uses_the_configured_candidate_limit(self):
        self.client.post("/api/settings", json={"search_candidate_limit": 7})

        with patch("app.search_and_filter_videos", return_value=[VIDEO]) as search:
            self.client.post("/api/search", json={"query": "home servers"})

        search.assert_called_once_with(
            "home servers", [], 7, self.client.get("/api/settings").get_json()["curation_prompt"]
        )

    def test_refresh_uses_the_saved_curation_prompt(self):
        prompt = "Only approve videos about programming."
        self.client.post("/api/settings", json={"curation_prompt": prompt})

        with patch("app.fetch_latest_feed", return_value=[VIDEO]) as fetch:
            self.client.post("/api/refresh")

        fetch.assert_called_once_with([], 30, prompt)

    def test_video_test_rejects_a_missing_url(self):
        response = self.client.post("/api/video-test", json={})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_video_url")

    def test_video_test_returns_the_raw_curation_response(self):
        result = {"video": VIDEO, "llm_response": {"approved_videos": []}}
        with patch("app.evaluate_video", return_value=result) as evaluate:
            response = self.client.post(
                "/api/video-test", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), result)
        evaluate.assert_called_once_with(
            "https://youtu.be/dQw4w9WgXcQ",
            self.client.get("/api/settings").get_json()["curation_prompt"],
        )

    def test_video_test_uses_the_saved_curation_prompt(self):
        prompt = "Only approve videos about repair."
        self.client.post("/api/settings", json={"curation_prompt": prompt})
        result = {"video": VIDEO, "llm_response": {"approved_videos": []}}

        with patch("app.evaluate_video", return_value=result) as evaluate:
            self.client.post("/api/video-test", json={"url": "dQw4w9WgXcQ"})

        evaluate.assert_called_once_with("dQw4w9WgXcQ", prompt)

    def test_video_test_does_not_touch_the_feed(self):
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")
        result = {"video": VIDEO, "llm_response": {"approved_videos": []}}

        with patch("app.evaluate_video", return_value=result):
            self.client.post("/api/video-test", json={"url": "dQw4w9WgXcQ"})

        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [VIDEO])

    def test_video_test_returns_a_provider_failure(self):
        with patch(
            "app.evaluate_video",
            side_effect=ProviderRequestError("YouTube did not return details."),
        ):
            response = self.client.post("/api/video-test", json={"url": "dQw4w9WgXcQ"})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "video_test_failed")

    def test_video_test_returns_a_configuration_failure(self):
        with patch(
            "app.evaluate_video",
            side_effect=LiveTestUnavailableError("Not configured."),
        ):
            response = self.client.post("/api/video-test", json={"url": "dQw4w9WgXcQ"})

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()["error"], "video_test_not_configured")


if __name__ == "__main__":
    unittest.main()