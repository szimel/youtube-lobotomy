import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from app import MAX_WATCH_LOG_IDS, create_app
from fetcher import (
    CandidateRecord,
    CurateResult,
    LiveFeedUnavailableError,
    LiveSearchUnavailableError,
    LiveTestUnavailableError,
    LiveWatchLogUnavailableError,
    ProviderRequestError,
    RunOutcome,
    RunStats,
)


class FakeJevResponse:
    """Stands in for the response object urlopen yields."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, *args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


VIDEO = {
    "video_id": "dQw4w9WgXcQ",
    "title": "A useful hardware project",
    "channel_name": "Workshop",
    "description": "A repair guide.",
    "curation_reason": "It teaches a concrete repair technique.",
}


def outcome(videos, **stats):
    """The pipeline now reports approved videos plus how the run actually went."""
    return RunOutcome(
        videos=list(videos),
        stats=RunStats(candidates=len(videos), approved=len(videos), **stats),
        candidates=[
            CandidateRecord(
                video=video,
                decision={
                    "approved": True,
                    "checks": [],
                    "rating": None,
                    "summary": video.get("curation_reason", "Approved."),
                    "unanswered": [],
                },
                answers={"positive_0": {"noul": 0.9}},
            )
            for video in videos
        ],
    )


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_directory = Path(self.temporary_directory.name)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.data_directory})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def wait_for_job(self, timeout: float = 10.0):
        """Refresh and search run in a worker thread; wait for it to settle."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get("/api/progress").get_json().get("job")
            if job and job["state"] != "running":
                return job
            time.sleep(0.005)
        self.fail("the background job never finished")

    def write_last_run(self, candidates, config=None):
        settings = self.client.get("/api/settings").get_json()
        run = {
            "kind": "refresh",
            "ran_at": time.time(),
            "stats": {"candidates": len(candidates), "approved": 1},
            "jev": config if config is not None else settings["jev"],
            "candidates": candidates,
        }
        (self.data_directory / "last_run.json").write_text(
            json.dumps(run), encoding="utf-8"
        )
        return run

    def test_rule_preview_needs_a_previous_run(self):
        response = self.client.post("/api/rules/preview", json={"profile": "x"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "no_run_to_preview")

    def test_rule_preview_lowers_a_threshold_without_asking_jev_again(self):
        settings = self.client.get("/api/settings").get_json()
        threshold = settings["jev"]["positives"][0]["threshold"]
        # A score between the lowered and the original threshold flips the verdict.
        self.write_last_run(
            [
                {
                    "video": VIDEO,
                    "answers": {
                        "positive_0": {"type": "noul", "noul": threshold - 0.1},
                        "disqualifier_0": {"type": "noul", "noul": 0.0},
                    },
                    "decision": {"approved": False, "checks": [], "summary": "no"},
                    "used_transcript": True,
                }
            ]
        )
        proposed = json.loads(json.dumps(settings["jev"]))
        proposed["positives"][0]["threshold"] = round(threshold - 0.2, 2)

        with patch("fetcher.urllib.request.urlopen") as urlopen:
            response = self.client.post("/api/rules/preview", json=proposed)

        urlopen.assert_not_called()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["mode"], "instant")
        self.assertEqual(body["preview"]["gained"], 1)
        self.assertEqual(body["preview"]["mode"], "reused")
        self.assertTrue(body["preview"]["changes"][0]["now_approved"])

    def test_rule_preview_leaves_the_saved_rules_and_the_feed_alone(self):
        settings = self.client.get("/api/settings").get_json()
        self.write_last_run(
            [
                {
                    "video": VIDEO,
                    "answers": {
                        "positive_0": {"type": "noul", "noul": 0.99},
                        "disqualifier_0": {"type": "noul", "noul": 0.0},
                    },
                    "decision": {"approved": True, "checks": [], "summary": "yes"},
                }
            ]
        )
        before_settings = self.client.get("/api/settings").get_json()
        proposed = json.loads(json.dumps(settings["jev"]))
        proposed["positives"][0]["threshold"] = 0.99
        proposed["profile"] = "a brand new profile"

        response = self.client.post("/api/rules/preview", json=proposed)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").get_json(), before_settings)
        self.assertEqual(self.client.get("/api/feed").get_json(), [])

    def test_rule_preview_rejects_invalid_rules(self):
        self.write_last_run([])

        response = self.client.post(
            "/api/rules/preview", json={"positives": [{"name": "no question"}]}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_rule_preview_reasks_jev_when_a_rule_is_added(self):
        settings = self.client.get("/api/settings").get_json()
        self.write_last_run(
            [
                {
                    "video": VIDEO,
                    "answers": {
                        "positive_0": {"type": "noul", "noul": 0.9},
                        "disqualifier_0": {"type": "noul", "noul": 0.0},
                    },
                    "decision": {"approved": True, "checks": [], "summary": "yes"},
                }
            ]
        )
        proposed = json.loads(json.dumps(settings["jev"]))
        proposed["positives"].append(
            {
                "name": "Practical",
                "instruction": "Does it teach something practical?",
                "threshold": 0.5,
                "enabled": True,
            }
        )
        payload = {
            "answers": {
                "positive_0": {"type": "noul", "noul": 0.9},
                "positive_1": {"type": "noul", "noul": 0.9},
                "disqualifier_0": {"type": "noul", "noul": 0.0},
                "disqualifier_1": {"type": "noul", "noul": 0.0},
                "disqualifier_2": {"type": "noul", "noul": 0.0},
                "disqualifier_3": {"type": "noul", "noul": 0.0},
            },
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 5000, "output_tokens": 10},
        }

        with patch(
            "fetcher.fetch_video_context",
            return_value={
                "title": VIDEO["title"],
                "channel_name": VIDEO["channel_name"],
                "description": VIDEO["description"],
                "transcript": "word " * 4000,
            },
        ), patch(
            "fetcher.urllib.request.urlopen", return_value=FakeJevResponse(payload)
        ), patch.dict("os.environ", {"JEV_API_KEY": "test-key"}):
            response = self.client.post("/api/rules/preview", json=proposed)
            self.assertEqual(response.status_code, 202)
            job = self.wait_for_job()

        self.assertEqual(job["state"], "done")
        preview = job["result"]["preview"]
        self.assertEqual(preview["mode"], "reasked")
        self.assertEqual(preview["usage"]["input_tokens"], 5000)
        self.assertIn("re-asked Jev", job["detail"])

    def test_rule_preview_refuses_to_start_alongside_a_running_job(self):
        release = threading.Event()

        def blocked_feed(trusted, limit, config, progress, skip_video_ids=None):
            release.wait(5)
            return outcome([])

        self.write_last_run([])
        settings = self.client.get("/api/settings").get_json()
        proposed = json.loads(json.dumps(settings["jev"]))
        proposed["positives"][0]["instruction"] = "A different question entirely?"

        try:
            with patch("app.fetch_latest_feed", side_effect=blocked_feed):
                self.client.post("/api/refresh")
                response = self.client.post("/api/rules/preview", json=proposed)
        finally:
            release.set()
            # Let the unblocked refresh finish before the data directory is
            # removed, or the worker thread writes into a directory being deleted.
            self.wait_for_job()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "job_in_progress")

    def test_a_cross_site_write_is_refused(self):
        # A bodyless POST is a "simple request" that any web page can send to
        # localhost without a preflight, so it must not be able to do anything.
        response = self.client.post(
            "/api/refresh", headers={"Sec-Fetch-Site": "cross-site"}
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "cross_site_request")
        self.assertIsNone(self.client.get("/api/progress").get_json()["job"])

    def test_a_write_from_a_foreign_origin_is_refused(self):
        response = self.client.post(
            "/api/watch-later",
            json=VIDEO,
            headers={"Origin": "https://example.com"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/watch-later").get_json(), [])

    def test_a_write_from_the_app_itself_is_allowed(self):
        response = self.client.post(
            "/api/watch-later",
            json=VIDEO,
            headers={
                "Origin": "http://localhost",
                "Sec-Fetch-Site": "same-origin",
            },
        )

        self.assertEqual(response.status_code, 201)

    def test_reading_is_never_blocked(self):
        response = self.client.get(
            "/api/feed", headers={"Sec-Fetch-Site": "cross-site"}
        )

        self.assertEqual(response.status_code, 200)

    def test_healthz_answers_without_touching_any_data(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_a_write_through_a_proxy_that_rewrites_host_is_allowed(self):
        # `tailscale serve` and Caddy with a rewritten Host both reach the app
        # as localhost while the browser's page is on the public name.
        response = self.client.post(
            "/api/watch-later",
            json=VIDEO,
            headers={
                "Origin": "https://feed.example.ts.net",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Host": "feed.example.ts.net",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.client.get("/api/watch-later").get_json()), 1)

    def test_a_declared_public_origin_is_allowed(self):
        with patch.dict("os.environ", {"PUBLIC_ORIGINS": "https://feed.example.com"}):
            response = self.client.post(
                "/api/watch-later",
                json=VIDEO,
                headers={
                    "Origin": "https://feed.example.com",
                    "Sec-Fetch-Site": "same-origin",
                },
            )

        self.assertEqual(response.status_code, 201)

    def test_a_forwarded_host_does_not_allow_a_cross_site_write(self):
        # The first check is the one that matters: a page on another site
        # cannot send this header at all without a preflight, which is never
        # approved, and its own Sec-Fetch-Site gives it away regardless.
        response = self.client.post(
            "/api/watch-later",
            json=VIDEO,
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "X-Forwarded-Host": "evil.example",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/watch-later").get_json(), [])

    def test_each_run_is_added_to_the_history(self):
        with patch("app.fetch_latest_feed", return_value=outcome([VIDEO])):
            self.client.post("/api/refresh")
            self.wait_for_job()

        history = self.client.get("/api/runs").get_json()

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["kind"], "refresh")
        self.assertEqual(history[0]["candidates"], 1)
        self.assertEqual(history[0]["approved"], 1)
        self.assertIn("estimated_cost_usd", history[0])

    def test_the_run_history_is_newest_first_and_bounded(self):
        self.client.post("/api/watch-later", json=VIDEO)
        for index in range(3):
            with patch("app.fetch_latest_feed", return_value=outcome([VIDEO])):
                self.client.post("/api/refresh")
                self.wait_for_job()
            with patch("app.search_and_filter_videos", return_value=outcome([VIDEO])):
                self.client.post("/api/search", json={"query": f"query {index}"})
                self.wait_for_job()

        history = self.client.get("/api/runs").get_json()

        self.assertEqual(history[0]["kind"], "search")
        self.assertEqual(history[0]["query"], "query 2")
        self.assertEqual(history[-1]["kind"], "refresh")
        self.assertLessEqual(len(history), 10)

    def test_the_run_history_is_empty_before_anything_runs(self):
        self.assertEqual(self.client.get("/api/runs").get_json(), [])

    def test_empty_collections_are_arrays(self):
        self.assertEqual(self.client.get("/api/feed").get_json(), [])
        self.assertEqual(self.client.get("/api/watch-later").get_json(), [])
        self.assertEqual(self.client.get("/api/watched").get_json(), [])

    def test_a_watched_video_is_recorded_and_merges_the_longest_time(self):
        self.client.post(
            "/api/watched",
            json={"video_id": VIDEO["video_id"], "seconds": 42, "title": VIDEO["title"]},
        )
        self.client.post(
            "/api/watched", json={"video_id": VIDEO["video_id"], "seconds": 10}
        )
        response = self.client.post(
            "/api/watched", json={"video_id": VIDEO["video_id"], "seconds": 90}
        )

        self.assertEqual(response.status_code, 201)
        watched = self.client.get("/api/watched").get_json()
        self.assertEqual(len(watched), 1)
        self.assertEqual(watched[0]["seconds"], 90)
        self.assertEqual(watched[0]["title"], VIDEO["title"])
        self.assertGreater(watched[0]["watched_at"], 0)

    def test_a_batch_of_watches_is_recorded_at_once(self):
        response = self.client.post(
            "/api/watched",
            json={
                "entries": [
                    {"video_id": "aaaaaaaaaaa", "seconds": 60},
                    {"video_id": "bbbbbbbbbbb", "seconds": 5},
                ]
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.client.get("/api/watched").get_json()), 2)

    def test_a_brief_look_does_not_count_as_watched(self):
        self.client.post(
            "/api/watched", json={"video_id": "aaaaaaaaaaa", "seconds": 4}
        )

        # Recorded, but not enough to keep it out of the next feed.
        self.assertEqual(len(self.client.get("/api/watched").get_json()), 1)

    def test_watch_reporting_rejects_nonsense(self):
        self.assertEqual(
            self.client.post("/api/watched", json={"video_id": "short", "seconds": 60}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/watched", json={"video_id": "aaaaaaaaaaa", "seconds": -5}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/watched",
                json={
                    "entries": [
                        {"video_id": "aaaaaaaaaaa", "seconds": 60}
                    ]
                    * (MAX_WATCH_LOG_IDS + 1)
                },
            ).status_code,
            400,
        )

    def test_a_watch_can_be_forgotten_again(self):
        self.client.post(
            "/api/watched", json={"video_id": "aaaaaaaaaaa", "seconds": 60}
        )

        removed = self.client.delete("/api/watched/aaaaaaaaaaa")

        self.assertTrue(removed.get_json()["removed"])
        self.assertEqual(self.client.get("/api/watched").get_json(), [])
        self.assertFalse(
            self.client.delete("/api/watched/aaaaaaaaaaa").get_json()["removed"]
        )

    def test_a_refresh_skips_videos_already_watched_here(self):
        watched_video = dict(VIDEO, video_id="aaaaaaaaaaa")
        fresh_video = dict(VIDEO, video_id="bbbbbbbbbbb")
        self.client.post(
            "/api/watched", json={"video_id": "aaaaaaaaaaa", "seconds": 120}
        )

        with patch(
            "app.fetch_latest_feed", return_value=outcome([fresh_video])
        ) as fetch:
            self.client.post("/api/refresh")
            job = self.wait_for_job()

        self.assertEqual(job["state"], "done")
        self.assertEqual(fetch.call_args.kwargs["skip_video_ids"], {"aaaaaaaaaaa"})
        self.assertEqual(
            [video["video_id"] for video in self.client.get("/api/feed").get_json()],
            ["bbbbbbbbbbb"],
        )

    def test_a_search_still_shows_a_video_that_was_already_watched(self):
        self.client.post(
            "/api/watched", json={"video_id": "aaaaaaaaaaa", "seconds": 120}
        )

        with patch("app.search_and_filter_videos", return_value=outcome([VIDEO])):
            self.client.post("/api/search", json={"query": "home servers"})
            job = self.wait_for_job()

        self.assertEqual(job["state"], "done")
        self.assertEqual(len(self.client.get("/api/feed").get_json()), 1)

    def test_trust_suggestions_skip_channels_already_trusted(self):
        for index in range(3):
            self.client.post(
                "/api/watch-later",
                json=dict(VIDEO, video_id=f"aaaaaaaaaa{index}", channel_name="Workshop"),
            )
        self.client.post(
            "/api/watch-later",
            json=dict(VIDEO, video_id="bbbbbbbbbbb", channel_name="Someone Else"),
        )

        suggestions = self.client.get("/api/trust-suggestions").get_json()

        self.assertEqual([item["name"] for item in suggestions], ["Workshop"])
        self.assertEqual(suggestions[0]["saved"], 3)
        self.assertIn("kept 3 videos", suggestions[0]["reason"])

        self.client.post("/api/trusted-creators", json={"name": "Workshop"})

        self.assertEqual(self.client.get("/api/trust-suggestions").get_json(), [])

    def test_watch_history_alone_can_suggest_a_creator(self):
        for index in range(6):
            self.client.post(
                "/api/watched",
                json={
                    "video_id": f"aaaaaaaaaa{index}",
                    "seconds": 90,
                    "channel_name": "Deep Dives",
                },
            )

        suggestions = self.client.get("/api/trust-suggestions").get_json()

        self.assertEqual([item["name"] for item in suggestions], ["Deep Dives"])
        self.assertEqual(suggestions[0]["watched"], 6)

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
            job = self.wait_for_job()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"]["code"], "feed_refresh_not_configured")
        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [VIDEO])

    def test_refresh_replaces_the_feed_after_success(self):
        with patch("app.fetch_latest_feed", return_value=outcome([VIDEO])):
            response = self.client.post("/api/refresh")
            job = self.wait_for_job()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["refreshed"], 1)
        self.assertIn("Feed replaced with 1 video", job["detail"])
        self.assertEqual(
            json.loads((self.data_directory / "feed.json").read_text(encoding="utf-8")),
            [VIDEO],
        )

    def test_refresh_overwrites_existing_feed_videos(self):
        stale_video = dict(VIDEO, video_id="aaaaaaaaaaa")
        new_video = dict(VIDEO, video_id="bbbbbbbbbbb")
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([stale_video]), encoding="utf-8")

        with patch("app.fetch_latest_feed", return_value=outcome([new_video])):
            self.client.post("/api/refresh")
            job = self.wait_for_job()

        self.assertEqual(job["result"]["refreshed"], 1)
        self.assertEqual(
            json.loads(feed_path.read_text(encoding="utf-8")),
            [new_video],
        )

    def test_refresh_empties_the_feed_when_nothing_is_approved(self):
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")

        with patch("app.fetch_latest_feed", return_value=outcome([])):
            self.client.post("/api/refresh")
            job = self.wait_for_job()

        self.assertEqual(job["result"]["refreshed"], 0)
        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [])

    def test_refresh_returns_a_provider_failure(self):
        with patch(
            "app.fetch_latest_feed",
            side_effect=ProviderRequestError("YouTube returned HTTP 403."),
        ):
            self.client.post("/api/refresh")
            job = self.wait_for_job()

        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"]["code"], "feed_refresh_failed")
        self.assertIn("HTTP 403", job["detail"])

    def test_refresh_reports_progress_while_running(self):
        def slow_feed(trusted, limit, config, progress, skip_video_ids=None):
            progress("Fetching transcripts", "1 of 4 · some video", 1, 4)
            return outcome([VIDEO])

        with patch("app.fetch_latest_feed", side_effect=slow_feed):
            self.client.post("/api/refresh")
            job = self.wait_for_job()

        self.assertEqual(job["state"], "done")
        self.assertEqual(job["completed"], 1)
        self.assertEqual(job["total"], 4)

    def test_refresh_refuses_to_start_a_second_job(self):
        release = threading.Event()

        def blocked_feed(trusted, limit, config, progress, skip_video_ids=None):
            release.wait(5)
            return outcome([])

        with patch("app.fetch_latest_feed", side_effect=blocked_feed):
            first = self.client.post("/api/refresh")
            second = self.client.post("/api/refresh")
            release.set()
            self.wait_for_job()

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"], "job_in_progress")

    def test_search_does_not_change_the_feed_before_configuration(self):
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")

        with patch(
            "app.search_and_filter_videos",
            side_effect=LiveSearchUnavailableError("Search is not configured."),
        ):
            response = self.client.post("/api/search", json={"query": "home servers"})
            job = self.wait_for_job()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"]["code"], "search_not_configured")
        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [VIDEO])

    def test_search_replaces_the_feed_after_success(self):
        stale_video = dict(VIDEO, video_id="aaaaaaaaaaa")
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([stale_video]), encoding="utf-8")

        with patch("app.search_and_filter_videos", return_value=outcome([VIDEO])) as search:
            response = self.client.post("/api/search", json={"query": "home servers"})
            job = self.wait_for_job()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["approved"], 1)
        self.assertEqual(job["result"]["query"], "home servers")
        search.assert_called_once_with(
            "home servers",
            [],
            20,
            self.client.get("/api/settings").get_json()["jev"],
            ANY,
        )
        self.assertEqual(
            json.loads((self.data_directory / "feed.json").read_text(encoding="utf-8")),
            [VIDEO],
        )

    def test_feed_removal_is_persistent_and_idempotent(self):
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

        with patch("app.fetch_latest_feed", return_value=outcome([VIDEO])) as fetch:
            self.client.post("/api/refresh")
            self.wait_for_job()

        fetch.assert_called_once_with(
            ["Workshop"],
            20,
            self.client.get("/api/settings").get_json()["jev"],
            ANY,
            skip_video_ids=set(),
        )

    def test_watch_log_verify_reports_which_watches_reached_youtube(self):
        history = [
            {"video_id": "aaaaaaaaaaa", "title": "Logged one", "channel_name": "C1"},
            {"video_id": "bbbbbbbbbbb", "title": "Logged two", "channel_name": "C2"},
        ]

        with patch("app.fetch_watch_history", return_value=history) as fetch:
            response = self.client.post(
                "/api/watch-log/verify",
                json={"video_ids": ["aaaaaaaaaaa", "ccccccccccc"]},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload["results"], {"aaaaaaaaaaa": True, "ccccccccccc": False}
        )
        self.assertEqual(payload["history_size"], 2)
        self.assertEqual(payload["recent"][0]["video_id"], "aaaaaaaaaaa")
        self.assertIsInstance(payload["checked_at"], float)
        fetch.assert_called_once_with()

    def test_watch_log_verify_reuses_a_recent_history_read(self):
        with patch("app.fetch_watch_history", return_value=[]) as fetch:
            self.client.post(
                "/api/watch-log/verify", json={"video_ids": ["aaaaaaaaaaa"]}
            )
            self.client.post(
                "/api/watch-log/verify", json={"video_ids": ["bbbbbbbbbbb"]}
            )

        fetch.assert_called_once_with()

    def test_watch_log_verify_rejects_bad_video_ids(self):
        payloads = (
            {},
            {"video_ids": []},
            {"video_ids": "aaaaaaaaaaa"},
            {"video_ids": ["too-short"]},
            {"video_ids": [12345678901]},
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/watch-log/verify", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "invalid_video_ids")

    def test_watch_log_verify_rejects_too_many_video_ids(self):
        video_ids = [f"{index:011d}" for index in range(MAX_WATCH_LOG_IDS + 1)]

        response = self.client.post("/api/watch-log/verify", json={"video_ids": video_ids})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_video_ids")

    def test_watch_log_verify_returns_a_configuration_failure(self):
        with patch(
            "app.fetch_watch_history",
            side_effect=LiveWatchLogUnavailableError("No cookies.txt."),
        ):
            response = self.client.post(
                "/api/watch-log/verify", json={"video_ids": ["aaaaaaaaaaa"]}
            )

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()["error"], "watch_log_not_configured")

    def test_watch_log_verify_returns_a_provider_failure(self):
        with patch(
            "app.fetch_watch_history",
            side_effect=ProviderRequestError("YouTube returned HTTP 403."),
        ):
            response = self.client.post(
                "/api/watch-log/verify", json={"video_ids": ["aaaaaaaaaaa"]}
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "watch_log_failed")

    def test_watch_log_verify_does_not_touch_the_feed(self):
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")

        with patch("app.fetch_watch_history", return_value=[]):
            self.client.post(
                "/api/watch-log/verify", json={"video_ids": ["aaaaaaaaaaa"]}
            )

        self.assertEqual(json.loads(feed_path.read_text(encoding="utf-8")), [VIDEO])

    def test_settings_start_at_defaults(self):
        settings = self.client.get("/api/settings").get_json()

        self.assertEqual(settings["refresh_candidate_limit"], 20)
        self.assertEqual(settings["search_candidate_limit"], 20)
        self.assertEqual(settings["jev"], settings["default_jev"])
        self.assertEqual(settings["max_candidate_limit"], 20)
        self.assertTrue(settings["jev"]["positives"])
        self.assertTrue(settings["jev"]["disqualifiers"])

    def test_settings_partial_update_is_persisted(self):
        response = self.client.post("/api/settings", json={"refresh_candidate_limit": 10})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["refresh_candidate_limit"], 10)
        self.assertEqual(response.get_json()["search_candidate_limit"], 20)
        self.assertEqual(
            self.client.get("/api/settings").get_json()["refresh_candidate_limit"], 10
        )

    def test_settings_saves_custom_jev_rules(self):
        jev = self.client.get("/api/settings").get_json()["jev"]
        jev["profile"] = "Only woodworking, please."
        jev["positives"] = [
            {
                "name": "Woodworking",
                "instruction": "Is this about woodworking?",
                "threshold": 0.7,
                "enabled": True,
            }
        ]
        jev["disqualifiers"] = []
        jev["transcript_min_tokens"] = 900
        jev["transcript_max_tokens"] = 9000

        response = self.client.post("/api/settings", json={"jev": jev})

        self.assertEqual(response.status_code, 200)
        stored = self.client.get("/api/settings").get_json()["jev"]
        self.assertEqual(stored["profile"], "Only woodworking, please.")
        self.assertEqual(stored["positives"][0]["name"], "Woodworking")
        self.assertEqual(stored["positives"][0]["threshold"], 0.7)
        self.assertEqual(stored["transcript_min_tokens"], 900)
        self.assertEqual(stored["transcript_max_tokens"], 9000)

    def test_settings_reject_a_rule_without_a_question(self):
        jev = self.client.get("/api/settings").get_json()["jev"]
        jev["positives"] = [
            {"name": "Broken", "instruction": "   ", "threshold": 0.5, "enabled": True}
        ]

        response = self.client.post("/api/settings", json={"jev": jev})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_settings_reject_an_out_of_range_threshold(self):
        jev = self.client.get("/api/settings").get_json()["jev"]
        jev["positives"][0]["threshold"] = 1.4

        response = self.client.post("/api/settings", json={"jev": jev})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_settings_reject_a_rating_with_too_many_levels(self):
        jev = self.client.get("/api/settings").get_json()["jev"]
        jev["rating"] = {
            "enabled": True,
            "instruction": "How good?",
            "criteria": [f"level {index}" for index in range(11)],
            "minimum": 1,
        }

        response = self.client.post("/api/settings", json={"jev": jev})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_settings_reject_a_transcript_maximum_below_the_minimum(self):
        jev = self.client.get("/api/settings").get_json()["jev"]
        jev["transcript_min_tokens"] = 5000
        jev["transcript_max_tokens"] = 100

        response = self.client.post("/api/settings", json={"jev": jev})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_settings")

    def test_settings_clamp_a_stored_limit_above_the_cap(self):
        settings_path = self.data_directory / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"refresh_candidate_limit": 30, "search_candidate_limit": 4}),
            encoding="utf-8",
        )

        settings = self.client.get("/api/settings").get_json()

        self.assertEqual(settings["refresh_candidate_limit"], 20)
        self.assertEqual(settings["search_candidate_limit"], 4)

    def test_settings_reject_a_candidate_limit_above_the_cap(self):
        response = self.client.post("/api/settings", json={"refresh_candidate_limit": 21})

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

        with patch("app.fetch_latest_feed", return_value=outcome([VIDEO])) as fetch:
            self.client.post("/api/refresh")
            self.wait_for_job()

        fetch.assert_called_once_with(
            [],
            5,
            self.client.get("/api/settings").get_json()["jev"],
            ANY,
            skip_video_ids=set(),
        )

    def test_search_uses_the_configured_candidate_limit(self):
        self.client.post("/api/settings", json={"search_candidate_limit": 7})

        with patch("app.search_and_filter_videos", return_value=outcome([VIDEO])) as search:
            self.client.post("/api/search", json={"query": "home servers"})
            self.wait_for_job()

        search.assert_called_once_with(
            "home servers",
            [],
            7,
            self.client.get("/api/settings").get_json()["jev"],
            ANY,
        )

    def test_refresh_uses_the_saved_jev_rules(self):
        jev = self.client.get("/api/settings").get_json()["jev"]
        jev["profile"] = "Only programming content."
        self.client.post("/api/settings", json={"jev": jev})

        with patch("app.fetch_latest_feed", return_value=outcome([VIDEO])) as fetch:
            self.client.post("/api/refresh")
            self.wait_for_job()

        fetch.assert_called_once_with([], 20, jev, ANY, skip_video_ids=set())

    def test_video_test_rejects_a_missing_url(self):
        response = self.client.post("/api/video-test", json={})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_video_url")

    def test_video_test_returns_the_jev_decision(self):
        result = {
            "video": {"video_id": "dQw4w9WgXcQ", "title": "Test", "used_transcript": True},
            "decision": {"approved": False, "checks": [], "rating": None, "summary": "no"},
        }
        with patch("app.evaluate_video", return_value=result) as evaluate:
            response = self.client.post(
                "/api/video-test", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), result)
        evaluate.assert_called_once_with(
            "https://youtu.be/dQw4w9WgXcQ",
            self.client.get("/api/settings").get_json()["jev"],
        )

    def test_video_test_uses_the_saved_jev_rules(self):
        jev = self.client.get("/api/settings").get_json()["jev"]
        jev["profile"] = "Only repair content."
        self.client.post("/api/settings", json={"jev": jev})
        result = {"video": {"video_id": "dQw4w9WgXcQ"}, "decision": {"approved": True}}

        with patch("app.evaluate_video", return_value=result) as evaluate:
            self.client.post("/api/video-test", json={"url": "dQw4w9WgXcQ"})

        evaluate.assert_called_once_with("dQw4w9WgXcQ", jev)

    def test_video_test_does_not_touch_the_feed(self):
        feed_path = self.data_directory / "feed.json"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps([VIDEO]), encoding="utf-8")
        result = {"video": {"video_id": "dQw4w9WgXcQ"}, "decision": {"approved": True}}

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