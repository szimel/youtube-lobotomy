import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fetcher
from fetcher import (
    LiveSearchUnavailableError,
    ProviderConfigurationError,
    ProviderRequestError,
    YouTubeInnerTubeClient,
    apply_transcript_policy,
    build_questions,
    build_state,
    decide,
    extract_videos,
    search_and_filter_videos,
)


VIDEO = {
    "video_id": "dQw4w9WgXcQ",
    "title": "A useful hardware project",
    "channel_name": "Workshop",
    "description": "A repair guide.",
}


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


def noul(value):
    return {"type": "noul", "noul": value}


def score(value, confidence=0.9):
    return {"type": "score", "score": value, "confidence": confidence}


def jev_config(**overrides):
    config = {
        "profile": "",
        "positives": [
            {
                "name": "Teaches",
                "instruction": "Does it teach?",
                "threshold": 0.5,
                "enabled": True,
            }
        ],
        "disqualifiers": [
            {
                "name": "Drama",
                "instruction": "Is it drama?",
                "threshold": 0.5,
                "enabled": True,
            }
        ],
        "rating": {
            "enabled": False,
            "instruction": "How good?",
            "criteria": ["low", "high"],
            "minimum": 1.0,
        },
        "transcript_min_tokens": 1500,
        "transcript_max_tokens": 15000,
    }
    config.update(overrides)
    return config


def positive(name="Teaches", threshold=0.5, enabled=True):
    return {
        "name": name,
        "instruction": "Does it teach?",
        "threshold": threshold,
        "enabled": enabled,
    }


def disqualifier(name="Red flag", threshold=0.5, enabled=True):
    return {
        "name": name,
        "instruction": "Is it a red flag?",
        "threshold": threshold,
        "enabled": enabled,
    }


class FetcherTestCase(unittest.TestCase):
    def test_extract_videos_normalizes_and_deduplicates_renderers(self):
        response = {
            "contents": [
                {
                    "videoRenderer": {
                        "videoId": VIDEO["video_id"],
                        "title": {"runs": [{"text": VIDEO["title"]}]},
                        "ownerText": {"runs": [{"text": VIDEO["channel_name"]}]},
                        "descriptionSnippet": {
                            "runs": [{"text": VIDEO["description"]}]
                        },
                        "thumbnail": {
                            "thumbnails": [
                                {"url": "https://image/low"},
                                {"url": "https://image/high"},
                            ]
                        },
                        "publishedTimeText": {"simpleText": "2 days ago"},
                    }
                },
                {
                    "videoRenderer": {
                        "videoId": VIDEO["video_id"],
                        "title": {"simpleText": "Duplicate"},
                    }
                },
            ]
        }

        videos = extract_videos(response)

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["video_id"], VIDEO["video_id"])
        self.assertEqual(videos[0]["thumbnail_url"], "https://image/high")
        self.assertEqual(videos[0]["published_at"], "2 days ago")

    def test_build_questions_maps_rules_to_jev_questions(self):
        questions = build_questions(jev_config())

        self.assertEqual(questions["positive_0"]["type"], "noul")
        self.assertEqual(questions["positive_0"]["instructions"], "Does it teach?")
        self.assertEqual(questions["disqualifier_0"]["type"], "noul")
        self.assertEqual(questions["disqualifier_0"]["instructions"], "Is it drama?")
        self.assertNotIn("rating", questions)

    def test_build_questions_skips_disabled_rules(self):
        config = jev_config(
            rating={
                "enabled": True,
                "instruction": "Rate it",
                "criteria": ["low", "high"],
                "minimum": 1.0,
            }
        )
        config["positives"][0]["enabled"] = False

        questions = build_questions(config)

        self.assertNotIn("positive_0", questions)
        self.assertEqual(questions["rating"]["type"], "score")
        self.assertEqual(questions["rating"]["criteria"], ["low", "high"])

    def test_build_questions_caps_rating_levels_at_ten(self):
        config = jev_config(
            rating={
                "enabled": True,
                "instruction": "Rate it",
                "criteria": [f"level {index}" for index in range(14)],
                "minimum": 1.0,
            }
        )

        questions = build_questions(config)

        self.assertEqual(len(questions["rating"]["criteria"]), fetcher.JEV_MAX_CRITERIA)

    def test_build_state_includes_the_transcript_only_when_present(self):
        config = jev_config(profile="An adult learner.")

        without = build_state(VIDEO, config)
        self.assertEqual(without["viewer_profile"], "An adult learner.")
        self.assertNotIn("video_transcript", without)

        with_transcript = build_state(dict(VIDEO, transcript="hello there"), config)
        self.assertEqual(with_transcript["video_transcript"], "hello there")

    def test_decide_approves_when_every_rule_passes(self):
        result = decide(
            {"positive_0": noul(0.91), "disqualifier_0": noul(0.04)}, jev_config()
        )

        self.assertTrue(result["approved"])
        self.assertIn("Teaches 0.91", result["summary"])

    def test_decide_rejects_when_a_positive_is_too_low(self):
        result = decide(
            {"positive_0": noul(0.2), "disqualifier_0": noul(0.01)}, jev_config()
        )

        self.assertFalse(result["approved"])
        self.assertIn("Teaches", result["summary"])

    def test_decide_rejects_when_a_red_flag_triggers(self):
        result = decide(
            {"positive_0": noul(0.99), "disqualifier_0": noul(0.82)}, jev_config()
        )

        self.assertFalse(result["approved"])
        self.assertIn("Drama 0.82", result["summary"])

    def test_decide_honours_the_optional_rating(self):
        config = jev_config(
            rating={
                "enabled": True,
                "instruction": "How good?",
                "criteria": ["low", "mid", "high"],
                "minimum": 2.0,
            }
        )

        rejected = decide(
            {
                "positive_0": noul(0.9),
                "disqualifier_0": noul(0.0),
                "rating": score(1.4, 0.8),
            },
            config,
        )
        self.assertFalse(rejected["approved"])
        self.assertEqual(rejected["rating"]["score"], 1.4)

        approved = decide(
            {
                "positive_0": noul(0.9),
                "disqualifier_0": noul(0.0),
                "rating": score(2.5, 0.7),
            },
            config,
        )
        self.assertTrue(approved["approved"])

    def test_decide_reports_unanswered_questions_as_failures(self):
        result = decide({}, jev_config())

        self.assertFalse(result["approved"])
        self.assertIn("unanswered", result["summary"])

    def test_jev_curator_attaches_the_reason_summary(self):
        payload = {"answers": {"positive_0": noul(0.93), "disqualifier_0": noul(0.02)}}

        with patch(
            "fetcher.urllib.request.urlopen", return_value=FakeJevResponse(payload)
        ) as urlopen:
            result = fetcher.JevCurator("test-key").curate([VIDEO], jev_config())

        approved = result.approved
        self.assertEqual(len(approved), 1)
        self.assertIn("Teaches", approved[0]["curation_reason"])

        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["model"], fetcher.JEV_DEFAULT_MODEL)
        self.assertEqual(body["state"]["video_title"], VIDEO["title"])
        self.assertIn("positive_0", body["questions"])

    def test_jev_curator_drops_rejected_candidates(self):
        payload = {"answers": {"positive_0": noul(0.1), "disqualifier_0": noul(0.9)}}

        with patch("fetcher.urllib.request.urlopen", return_value=FakeJevResponse(payload)):
            result = fetcher.JevCurator("test-key").curate([VIDEO], jev_config())

        self.assertEqual(result.approved, [])
        # The rejection is still recorded, along with the answers behind it.
        self.assertEqual(len(result.records), 1)
        self.assertFalse(result.records[0].decision["approved"])
        self.assertIn("positive_0", result.records[0].answers)

    def test_jev_curator_records_usage_and_the_resolved_model(self):
        payload = {
            "answers": {"positive_0": noul(0.93), "disqualifier_0": noul(0.02)},
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 8591, "output_tokens": 0},
        }

        with patch("fetcher.urllib.request.urlopen", return_value=FakeJevResponse(payload)):
            result = fetcher.JevCurator("test-key").curate([VIDEO], jev_config())

        self.assertEqual(result.input_tokens, 8591)
        self.assertEqual(result.model, "jev-1.13.0")
        self.assertEqual(result.records[0].model, "jev-1.13.0")

    def test_jev_curator_marks_a_failed_call_apart_from_a_rejection(self):
        good = {"answers": {"positive_0": noul(0.9), "disqualifier_0": noul(0.01)}}
        responses = [FakeJevResponse(good), urllib.error.URLError("connection reset")]

        with patch("fetcher.urllib.request.urlopen", side_effect=responses):
            result = fetcher.JevCurator("test-key").curate(
                [VIDEO, dict(VIDEO, video_id="bbbbbbbbbbb")], jev_config()
            )

        # Exactly one of the two came back, so the run survives...
        self.assertEqual(len(result.approved), 1)
        # ...and the other is recorded as unjudged rather than quietly rejected.
        unjudged = [record for record in result.records if record.decision is None]
        self.assertEqual(len(unjudged), 1)
        self.assertIsNotNone(unjudged[0].error)

    def test_jev_curator_surfaces_provider_errors(self):
        error = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"too many criteria"}'),
        )

        with patch("fetcher.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ProviderRequestError) as context:
                fetcher.JevCurator("test-key").curate([VIDEO], jev_config())

        message = str(context.exception)
        self.assertIn("400", message)
        self.assertIn("too many criteria", message)
        error.close()

    def test_jev_curator_never_echoes_the_api_key(self):
        secret = "ts_super_secret_value"
        error = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone",
            401,
            "Unauthorized",
            {},
            io.BytesIO(f'{{"error":"bad key {secret}"}}'.encode("utf-8")),
        )

        with patch.dict("os.environ", {"JEV_API_KEY": secret}), patch(
            "fetcher.urllib.request.urlopen", side_effect=error
        ):
            with self.assertRaises(ProviderRequestError) as context:
                fetcher.JevCurator(secret).curate([VIDEO], jev_config())

        message = str(context.exception)
        self.assertNotIn(secret, message)
        self.assertIn("[redacted]", message)
        error.close()

    def test_jev_curator_requires_at_least_one_enabled_rule(self):
        config = jev_config(positives=[], disqualifiers=[])

        with self.assertRaises(ProviderConfigurationError):
            fetcher.JevCurator("test-key").curate([VIDEO], config)

    def test_apply_transcript_policy_ignores_short_transcripts(self):
        config = jev_config(transcript_min_tokens=1500)
        context = {"transcript": "too short", "description": "", "channel_name": ""}

        enriched = apply_transcript_policy(dict(VIDEO, description=""), context, config)

        self.assertNotIn("transcript", enriched)

    def test_apply_transcript_policy_truncates_long_transcripts(self):
        config = jev_config(transcript_min_tokens=10, transcript_max_tokens=20)

        enriched = apply_transcript_policy(
            dict(VIDEO), {"transcript": "word " * 400}, config
        )

        self.assertIn("transcript", enriched)
        self.assertLessEqual(len(enriched["transcript"]), 20 * 4)

    def test_apply_transcript_policy_fills_in_missing_metadata(self):
        context = {
            "transcript": "",
            "description": "Real description",
            "channel_name": "Real channel",
        }

        enriched = apply_transcript_policy(
            dict(VIDEO, description="", channel_name=""), context, jev_config()
        )

        self.assertEqual(enriched["description"], "Real description")
        self.assertEqual(enriched["channel_name"], "Real channel")

    def test_home_without_candidate_videos_is_a_provider_failure(self):
        client = object.__new__(YouTubeInnerTubeClient)
        client._cookie_jar = SimpleNamespace(filename="data/cookies.txt")

        with patch("fetcher._home_feed_entries", return_value=[]):
            with self.assertRaises(ProviderRequestError):
                client.home_videos()

    def test_home_videos_maps_and_deduplicates_yt_dlp_entries(self):
        client = object.__new__(YouTubeInnerTubeClient)
        client._cookie_jar = SimpleNamespace(filename="data/cookies.txt")
        entries = [
            {
                "id": VIDEO["video_id"],
                "title": VIDEO["title"],
                "channel": VIDEO["channel_name"],
                "thumbnails": [
                    {"url": "https://image/low", "width": 120, "height": 90},
                    {"url": "https://image/high", "width": 360, "height": 202},
                ],
            },
            {"id": VIDEO["video_id"], "title": "Duplicate"},
            {"id": "", "title": "Missing id is skipped"},
        ]

        with patch("fetcher._home_feed_entries", return_value=entries) as home_feed_entries:
            videos = client.home_videos(limit=5)

        home_feed_entries.assert_called_once_with("data/cookies.txt", 5)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["video_id"], VIDEO["video_id"])
        self.assertEqual(videos[0]["channel_name"], VIDEO["channel_name"])
        self.assertEqual(videos[0]["thumbnail_url"], "https://image/high")

    def test_video_details_maps_the_authenticated_player_response(self):
        client = object.__new__(YouTubeInnerTubeClient)
        player_response = {
            "videoDetails": {
                "videoId": VIDEO["video_id"],
                "title": VIDEO["title"],
                "author": VIDEO["channel_name"],
                "shortDescription": VIDEO["description"],
                "thumbnail": {
                    "thumbnails": [
                        {"url": "https://image/low", "width": 120, "height": 90},
                        {"url": "https://image/high", "width": 360, "height": 202},
                    ]
                },
            }
        }

        with patch.object(client, "_api_request", return_value=player_response) as request:
            video = client.video_details(VIDEO["video_id"])

        request.assert_called_once_with("player", {"videoId": VIDEO["video_id"]})
        self.assertEqual(video, {**VIDEO, "thumbnail_url": "https://image/high"})

    def test_home_feed_entries_detects_rotated_cookies(self):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                self.options["logger"].warning(
                    "The provided YouTube account cookies are no longer valid. "
                    "They have likely been rotated in the browser."
                )
                return {"entries": []}

        with patch("fetcher.yt_dlp.YoutubeDL", FakeYoutubeDL):
            with self.assertRaises(ProviderConfigurationError):
                fetcher._home_feed_entries("data/cookies.txt", 30)

    def test_missing_jev_key_prevents_youtube_search(self):
        with patch.dict("os.environ", {"JEV_API_KEY": ""}, clear=True), patch(
            "fetcher.YouTubeInnerTubeClient.from_environment"
        ) as youtube_client:
            with self.assertRaises(LiveSearchUnavailableError):
                search_and_filter_videos("home servers")

        youtube_client.assert_not_called()

    def test_partition_trusted_candidates_matches_case_insensitively(self):
        trusted_video = dict(VIDEO, video_id="aaaaaaaaaaa", channel_name="  workshop  ")
        other_video = dict(VIDEO, video_id="bbbbbbbbbbb", channel_name="Someone Else")

        trusted, remaining = fetcher._partition_trusted_candidates(
            [trusted_video, other_video], ["Workshop"]
        )

        self.assertEqual([video["video_id"] for video in trusted], ["aaaaaaaaaaa"])
        self.assertEqual(trusted[0]["curation_reason"], fetcher.TRUSTED_CREATOR_REASON)
        self.assertEqual([video["video_id"] for video in remaining], ["bbbbbbbbbbb"])

    def test_refresh_feed_includes_trusted_creators_without_ai_rejection(self):
        trusted_video = dict(VIDEO, video_id="aaaaaaaaaaa", channel_name="Workshop")
        other_video = dict(VIDEO, video_id="bbbbbbbbbbb", channel_name="Someone Else")
        fake_client = SimpleNamespace(
            home_videos=lambda limit=fetcher.MAX_CANDIDATES: [trusted_video, other_video]
        )
        curate = Mock(return_value=fetcher.CurateResult())
        fake_curator = SimpleNamespace(curate=curate)
        config = jev_config()

        with patch(
            "fetcher.YouTubeInnerTubeClient.from_environment", return_value=fake_client
        ), patch("fetcher._curator_from_environment", return_value=fake_curator), patch(
            "fetcher._enrich_with_transcripts",
            side_effect=lambda videos, config, progress=None: videos,
        ):
            outcome = fetcher.refresh_feed(["Workshop"], jev_config=config)

        self.assertEqual(
            [video["video_id"] for video in outcome.videos], ["aaaaaaaaaaa"]
        )
        self.assertEqual(outcome.stats.trusted, 1)
        self.assertEqual(outcome.stats.candidates, 2)
        curate.assert_called_once_with([other_video], config, None)

    def test_search_and_filter_videos_includes_trusted_creators(self):
        trusted_video = dict(VIDEO, video_id="aaaaaaaaaaa", channel_name="Workshop")
        fake_client = SimpleNamespace(
            search_videos=lambda query, limit=fetcher.MAX_CANDIDATES: [trusted_video]
        )
        curate = Mock(return_value=fetcher.CurateResult())
        fake_curator = SimpleNamespace(curate=curate)
        config = jev_config()

        with patch(
            "fetcher.YouTubeInnerTubeClient.from_environment", return_value=fake_client
        ), patch("fetcher._curator_from_environment", return_value=fake_curator), patch(
            "fetcher._enrich_with_transcripts",
            side_effect=lambda videos, config, progress=None: videos,
        ):
            outcome = search_and_filter_videos(
                "home servers", ["Workshop"], jev_config=config
            )

        self.assertEqual(
            [video["video_id"] for video in outcome.videos], ["aaaaaaaaaaa"]
        )
        curate.assert_called_once_with([], config, None)

    def test_watch_history_entries_requests_the_history_url_with_cookies(self):
        seen = {}

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                seen["url"] = url
                seen["options"] = self.options
                return {"entries": []}

        with patch("fetcher.yt_dlp.YoutubeDL", FakeYoutubeDL):
            fetcher._watch_history_entries("data/cookies.txt", 25)

        self.assertEqual(seen["url"], fetcher.WATCH_HISTORY_URL)
        self.assertEqual(seen["options"]["cookiefile"], "data/cookies.txt")
        self.assertEqual(seen["options"]["playlist_items"], "1:25")

    def test_watch_history_entries_detects_rotated_cookies(self):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                self.options["logger"].warning(
                    "The provided YouTube account cookies are no longer valid. "
                    "They have likely been rotated in the browser."
                )
                return {"entries": []}

        with patch("fetcher.yt_dlp.YoutubeDL", FakeYoutubeDL):
            with self.assertRaises(ProviderConfigurationError):
                fetcher._watch_history_entries("data/cookies.txt", 30)

    def test_fetch_watch_history_normalizes_and_deduplicates_entries(self):
        entries = [
            {
                "id": "aaaaaaaaaaa",
                "title": "Newest watch",
                "channel": "Channel A",
                "thumbnails": [
                    {"url": "https://image/low", "width": 120, "height": 90},
                    {"url": "https://image/high", "width": 360, "height": 202},
                ],
            },
            {"id": "aaaaaaaaaaa", "title": "Duplicate"},
            {"id": "bbbbbbbbbbb", "title": "Older watch", "channel": "Channel B"},
            {"id": "", "title": "Missing id is skipped"},
        ]

        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            with patch(
                "fetcher._environment_path", return_value=cookie_path
            ), patch("fetcher._watch_history_entries", return_value=entries) as history:
                videos = fetcher.fetch_watch_history(limit=7)

        self.assertEqual(history.call_args.args[1], 7)
        self.assertTrue(str(history.call_args.args[0]).endswith("cookies.txt"))
        self.assertEqual(
            [video["video_id"] for video in videos], ["aaaaaaaaaaa", "bbbbbbbbbbb"]
        )
        self.assertEqual(videos[0]["thumbnail_url"], "https://image/high")
        self.assertEqual(videos[0]["channel_name"], "Channel A")

    def test_fetch_watch_history_reports_missing_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "cookies.txt"
            with patch("fetcher._environment_path", return_value=missing_path):
                with self.assertRaises(fetcher.LiveWatchLogUnavailableError):
                    fetcher.fetch_watch_history()

    def test_fetch_watch_history_reports_rotated_cookies(self):
        with patch(
            "fetcher._watch_history_entries",
            side_effect=ProviderConfigurationError("cookies rotated"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                cookie_path = Path(directory) / "cookies.txt"
                cookie_path.write_text("placeholder", encoding="utf-8")
                with patch(
                    "fetcher._environment_path", return_value=cookie_path
                ):
                    with self.assertRaises(fetcher.LiveWatchLogUnavailableError):
                        fetcher.fetch_watch_history()

    def test_parse_video_id_accepts_bare_ids_and_common_url_shapes(self):
        video_id = "dQw4w9WgXcQ"
        self.assertEqual(fetcher.parse_video_id(video_id), video_id)
        self.assertEqual(
            fetcher.parse_video_id(f"https://www.youtube.com/watch?v={video_id}&t=10s"),
            video_id,
        )
        self.assertEqual(fetcher.parse_video_id(f"https://youtu.be/{video_id}"), video_id)
        self.assertEqual(
            fetcher.parse_video_id(f"https://www.youtube.com/embed/{video_id}"), video_id
        )
        self.assertEqual(
            fetcher.parse_video_id(f"https://www.youtube.com/shorts/{video_id}"), video_id
        )
        self.assertIsNone(fetcher.parse_video_id("not a video"))
        self.assertIsNone(fetcher.parse_video_id("https://example.com/watch?v=" + video_id))

    def test_evaluate_video_rejects_an_unparsable_url(self):
        with self.assertRaises(fetcher.LiveTestUnavailableError):
            fetcher.evaluate_video("not a valid url")

    def test_evaluate_video_reports_the_jev_decision(self):
        fake_client = SimpleNamespace(video_details=lambda video_id: dict(VIDEO))
        decision = {"approved": True, "checks": [], "rating": None, "summary": "ok"}
        judge = Mock(return_value=(decision, fetcher.JevAnswer(answers={})))
        fake_curator = SimpleNamespace(judge=judge, model="jev-latest")
        config = jev_config()

        with patch(
            "fetcher.YouTubeInnerTubeClient.from_environment", return_value=fake_client
        ), patch("fetcher._curator_from_environment", return_value=fake_curator), patch(
            "fetcher.fetch_video_context",
            return_value={"transcript": "", "description": "", "channel_name": ""},
        ):
            result = fetcher.evaluate_video(VIDEO["video_id"], config)

        self.assertTrue(result["decision"]["approved"])
        self.assertEqual(result["video"]["video_id"], VIDEO["video_id"])
        self.assertFalse(result["video"]["used_transcript"])
        judge.assert_called_once()

    def test_evaluate_video_marks_when_a_transcript_was_used(self):
        fake_client = SimpleNamespace(video_details=lambda video_id: dict(VIDEO))
        judge = Mock(
            return_value=(
                {"approved": False, "checks": [], "rating": None, "summary": "no"},
                fetcher.JevAnswer(answers={}, input_tokens=4200, model="jev-1.13.0"),
            )
        )
        fake_curator = SimpleNamespace(judge=judge, model="jev-latest")
        long_transcript = "word " * 2000

        with patch(
            "fetcher.YouTubeInnerTubeClient.from_environment", return_value=fake_client
        ), patch("fetcher._curator_from_environment", return_value=fake_curator), patch(
            "fetcher.fetch_video_context",
            return_value={
                "transcript": long_transcript,
                "description": "",
                "channel_name": "",
            },
        ):
            result = fetcher.evaluate_video(VIDEO["video_id"], jev_config())

        self.assertTrue(result["video"]["used_transcript"])
        self.assertGreater(result["video"]["transcript_tokens"], 0)
        # The test panel reports what the question cost, like a run does.
        self.assertEqual(result["usage"]["input_tokens"], 4200)
        self.assertEqual(result["usage"]["model"], "jev-1.13.0")


class UnansweredQuestionTestCase(unittest.TestCase):
    """A rule Jev never answered must not be presented as a satisfied rule."""

    def test_unanswered_disqualifier_is_not_reported_as_passed(self):
        config = jev_config(positives=[], disqualifiers=[disqualifier()])

        result = decide({}, config)

        check = result["checks"][0]
        self.assertIsNone(check["passed"])
        self.assertFalse(check["answered"])
        self.assertIn("Red flag", result["unanswered"])

    def test_answered_disqualifier_still_passes_when_below_its_threshold(self):
        config = jev_config(positives=[], disqualifiers=[disqualifier()])

        result = decide({"disqualifier_0": noul(0.01)}, config)

        self.assertTrue(result["checks"][0]["passed"])
        self.assertTrue(result["checks"][0]["answered"])

    def test_an_approved_video_says_which_rules_went_unanswered(self):
        config = jev_config(
            positives=[positive()],
            disqualifiers=[disqualifier()],
            rating={"enabled": False},
        )

        result = decide({"positive_0": noul(0.9)}, config)

        self.assertTrue(result["approved"])
        self.assertIn("Red flag", result["summary"])
        self.assertIn("unanswered", result["summary"])

    def test_a_fully_answered_approval_has_no_caveat(self):
        config = jev_config(positives=[positive()], disqualifiers=[disqualifier()])

        result = decide(
            {"positive_0": noul(0.9), "disqualifier_0": noul(0.01)}, config
        )

        self.assertTrue(result["approved"])
        self.assertEqual(result["unanswered"], [])
        self.assertNotIn("unanswered", result["summary"])


class TranscriptCeilingTestCase(unittest.TestCase):
    def test_a_configured_maximum_cannot_exceed_jev_s_state_budget(self):
        config = jev_config(
            transcript_min_tokens=0, transcript_max_tokens=30_000
        )
        context = {"transcript": "word " * 200_000, "description": "", "channel_name": ""}

        enriched = apply_transcript_policy(dict(VIDEO), context, config)

        self.assertEqual(
            len(enriched["transcript"]),
            fetcher.JEV_TRANSCRIPT_TOKEN_CEILING * 4,
        )
        self.assertLessEqual(
            fetcher._estimate_tokens(enriched["transcript"]),
            fetcher.JEV_MAX_STATE_TOKENS,
        )

    def test_a_smaller_configured_maximum_is_still_honoured(self):
        config = jev_config(transcript_min_tokens=0, transcript_max_tokens=2_000)
        context = {"transcript": "word " * 200_000, "description": "", "channel_name": ""}

        enriched = apply_transcript_policy(dict(VIDEO), context, config)

        self.assertEqual(len(enriched["transcript"]), 8_000)


class SessionFailureTestCase(unittest.TestCase):
    """A dead session must name the fix instead of blaming YouTube."""

    def _failing_ytdlp(self, message):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                raise RuntimeError(message)

        return FakeYoutubeDL

    def test_a_reload_required_error_is_reported_as_a_cookie_problem(self):
        with patch(
            "fetcher.yt_dlp.YoutubeDL",
            self._failing_ytdlp(
                "ERROR: [youtube] dQw4w9WgXcQ: The page needs to be reloaded"
            ),
        ):
            with self.assertRaises(ProviderConfigurationError) as context:
                fetcher._home_feed_entries("data/cookies.txt", 30)

        self.assertIn("cookies.txt", str(context.exception))

    def test_an_unrelated_failure_still_reports_the_cause(self):
        with patch(
            "fetcher.yt_dlp.YoutubeDL",
            self._failing_ytdlp("ERROR: unable to download webpage: timeout"),
        ):
            with self.assertRaises(ProviderRequestError) as context:
                fetcher._home_feed_entries("data/cookies.txt", 30)

        self.assertNotIn("cookies.txt", str(context.exception))
        self.assertIn("timeout", str(context.exception))

    def test_a_rotated_warning_after_a_successful_read_is_not_ignored(self):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                self.options["logger"].warning(
                    "The provided YouTube account cookies are no longer valid."
                )
                return {"entries": [{"id": "dQw4w9WgXcQ"}]}

        with patch("fetcher.yt_dlp.YoutubeDL", FakeYoutubeDL):
            with self.assertRaises(ProviderConfigurationError):
                fetcher._home_feed_entries("data/cookies.txt", 30)


class CookieJarAuthenticationTestCase(unittest.TestCase):
    """A signed-out jar must fail loudly rather than return a generic feed."""

    def _write_jar(self, directory, rows):
        path = Path(directory) / "cookies.txt"
        lines = ["# Netscape HTTP Cookie File"]
        lines.extend(rows)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_a_jar_without_a_session_cookie_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_jar(
                directory,
                [".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tsome-preference"],
            )
            with patch("fetcher._environment_path", return_value=path):
                with self.assertRaises(ProviderConfigurationError) as context:
                    fetcher.YouTubeInnerTubeClient.from_environment()

        self.assertIn("SAPISID", str(context.exception))
        self.assertIn("cookies.txt", str(context.exception))

    def test_a_jar_with_a_session_cookie_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_jar(
                directory,
                [".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\ta-session-value"],
            )
            with patch("fetcher._environment_path", return_value=path):
                client = fetcher.YouTubeInnerTubeClient.from_environment()

        self.assertIsNotNone(client._sapisid_authorization())
        self.assertNotIn("a-session-value", client._sapisid_authorization())


class TranscriptCacheTestCase(unittest.TestCase):
    """Captions are expensive and rate limited, so they are kept on disk."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data_directory = Path(self.directory.name)
        fetcher.set_data_directory(self.data_directory)

    def tearDown(self):
        fetcher.set_data_directory(None)
        self.directory.cleanup()

    def write_entry(self, video_id, context, fetched_at=None):
        path = self.data_directory / fetcher.TRANSCRIPT_CACHE_DIRNAME / f"{video_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": fetched_at if fetched_at is not None else 1_000_000.0,
            "context": {"video_id": video_id, **context},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def context(self, **overrides):
        context = {
            "title": "A useful hardware project",
            "channel_name": "Workshop",
            "description": "A repair guide.",
            "transcript": "word " * 400,
            "caption_kind": "manual",
            "transcript_tokens": 400,
        }
        context.update(overrides)
        return context

    def test_a_cached_transcript_avoids_touching_youtube(self):
        self.write_entry(VIDEO["video_id"], self.context())

        with patch("fetcher.yt_dlp.YoutubeDL") as downloader:
            context = fetcher.fetch_video_context(VIDEO["video_id"])

        downloader.assert_not_called()
        self.assertGreater(len(context["transcript"]), 0)

    def test_a_fresh_context_is_written_for_next_time(self):
        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return {
                    "title": "A useful hardware project",
                    "uploader": "Workshop",
                    "description": "A repair guide.",
                    "subtitles": {},
                    "automatic_captions": {},
                }

        with patch("fetcher.yt_dlp.YoutubeDL", FakeYoutubeDL), patch(
            "fetcher.time.sleep"
        ):
            fetcher.fetch_video_context(VIDEO["video_id"])

        path = (
            self.data_directory
            / fetcher.TRANSCRIPT_CACHE_DIRNAME
            / f"{VIDEO['video_id']}.json"
        )
        self.assertTrue(path.is_file())
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["context"]["caption_kind"],
            "none",
        )

    def test_a_failed_download_is_not_remembered_forever(self):
        stale = time.time() - fetcher.TRANSCRIPT_NEGATIVE_TTL_SECONDS - 60
        self.write_entry(
            VIDEO["video_id"],
            self.context(transcript="", caption_kind="unavailable"),
            fetched_at=stale,
        )

        self.assertIsNone(fetcher._read_cached_context(VIDEO["video_id"]))

    def test_a_recent_failed_download_is_still_reused(self):
        self.write_entry(
            VIDEO["video_id"],
            self.context(transcript="", caption_kind="unavailable"),
            fetched_at=time.time(),
        )

        self.assertIsNotNone(fetcher._read_cached_context(VIDEO["video_id"]))

    def test_the_cache_directory_is_pruned_to_the_newest_entries(self):
        with patch.object(fetcher, "TRANSCRIPT_CACHE_MAX_FILES", 3), patch(
            "fetcher.time.sleep"
        ):
            for index, video_id in enumerate(
                ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3", "aaaaaaaaaa4"]
            ):
                self.write_entry(video_id, self.context())
                os.utime(
                    self.data_directory
                    / fetcher.TRANSCRIPT_CACHE_DIRNAME
                    / f"{video_id}.json",
                    (1_000_000 + index, 1_000_000 + index),
                )
            fetcher._prune_transcript_cache(
                self.data_directory / fetcher.TRANSCRIPT_CACHE_DIRNAME
            )

        remaining = sorted(
            path.stem
            for path in (
                self.data_directory / fetcher.TRANSCRIPT_CACHE_DIRNAME
            ).glob("*.json")
        )
        self.assertEqual(remaining, ["aaaaaaaaaa2", "aaaaaaaaaa3", "aaaaaaaaaa4"])


class RulePreviewTestCase(unittest.TestCase):
    """Editing rules is the whole workflow, so it must be testable first."""

    def saved_run(self, config, decisions):
        return {
            "kind": "refresh",
            "jev": config,
            "candidates": [
                {
                    "video": dict(VIDEO, video_id=video_id),
                    "answers": answers,
                    "decision": {"approved": approved, "checks": []},
                    "used_transcript": True,
                }
                for video_id, answers, approved in decisions
            ],
        }

    def test_a_threshold_edit_is_answered_from_the_stored_answers(self):
        config = jev_config(transcript_min_tokens=0, transcript_max_tokens=15_000)
        saved = self.saved_run(
            config,
            [
                ("aaaaaaaaaaa", {"positive_0": noul(0.45), "disqualifier_0": noul(0.01)}, False),
                ("bbbbbbbbbbb", {"positive_0": noul(0.80), "disqualifier_0": noul(0.01)}, True),
            ],
        )
        lowered = jev_config(
            positives=[positive(threshold=0.4)],
            transcript_min_tokens=0,
            transcript_max_tokens=15_000,
        )

        with patch("fetcher.urllib.request.urlopen") as urlopen:
            preview = fetcher.preview_rules(lowered, saved)

        urlopen.assert_not_called()
        self.assertEqual(preview["mode"], "reused")
        self.assertEqual(preview["gained"], 1)
        self.assertEqual(preview["lost"], 0)
        self.assertEqual(preview["usage"]["input_tokens"], 0)
        self.assertIn("+1 would pass", preview["headline"])
        # The flip is listed first, with the reason it now passes.
        self.assertEqual(preview["changes"][0]["video_id"], "aaaaaaaaaaa")
        self.assertTrue(preview["changes"][0]["now_approved"])

    def test_a_tightened_threshold_reports_what_would_drop_out(self):
        config = jev_config()
        saved = self.saved_run(
            config, [("aaaaaaaaaaa", {"positive_0": noul(0.55)}, True)]
        )
        stricter = jev_config(positives=[positive(threshold=0.9)])

        preview = fetcher.preview_rules(stricter, saved)

        self.assertEqual(preview["lost"], 1)
        self.assertEqual(preview["gained"], 0)
        self.assertEqual(preview["mode"], "reused")

    def test_a_new_rule_is_asked_again_using_cached_transcripts(self):
        config = jev_config()
        saved = self.saved_run(
            config, [("aaaaaaaaaaa", {"positive_0": noul(0.9), "disqualifier_0": noul(0.01)}, True)]
        )
        added = jev_config(
            positives=[positive()],
            disqualifiers=[disqualifier(), disqualifier("Shorts")],
        )
        payload = {
            "answers": {
                "positive_0": noul(0.9),
                "disqualifier_0": noul(0.01),
                "disqualifier_1": noul(0.8),
            },
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 4321, "output_tokens": 12},
        }
        fake_curator = fetcher.JevCurator("test-key")

        with patch(
            "fetcher.fetch_video_context",
            return_value={
                "transcript": "word " * 4000,
                "description": "",
                "channel_name": "",
                "title": "x",
            },
        ), patch("fetcher._curator_from_environment", return_value=fake_curator), patch(
            "fetcher.urllib.request.urlopen", return_value=FakeJevResponse(payload)
        ):
            preview = fetcher.preview_rules(added, saved)

        self.assertEqual(preview["mode"], "reasked")
        self.assertEqual(preview["with_transcript"], 1)
        self.assertEqual(preview["usage"]["input_tokens"], 4321)
        # The new red flag now catches a video the old rules let through.
        self.assertEqual(preview["lost"], 1)
        self.assertIn("Shorts", preview["changes"][0]["summary"])

    def test_a_run_with_nothing_judged_cannot_be_previewed(self):
        saved = {"jev": jev_config(), "candidates": []}

        with self.assertRaises(fetcher.LiveTestUnavailableError):
            fetcher.preview_rules(jev_config(), saved)

    def test_trusted_creators_are_left_out_of_the_comparison(self):
        config = jev_config()
        saved = {
            "jev": config,
            "candidates": [
                {
                    "video": dict(VIDEO),
                    "answers": {},
                    "decision": {"approved": True, "trusted": True},
                }
            ],
        }

        with self.assertRaises(fetcher.LiveTestUnavailableError):
            fetcher.preview_rules(config, saved)

    def test_questions_unchanged_ignores_thresholds_but_not_wording(self):
        base = jev_config()

        self.assertTrue(
            fetcher.questions_unchanged(base, jev_config(positives=[positive(0.9)]))
        )
        self.assertFalse(
            fetcher.questions_unchanged(
                base,
                jev_config(
                    positives=[
                        {
                            "name": "Teaches",
                            "instruction": "Does it teach something practical?",
                            "threshold": 0.5,
                            "enabled": True,
                        }
                    ]
                ),
            )
        )


class DurationTestCase(unittest.TestCase):
    def test_video_lengths_are_read_from_the_usual_shapes(self):
        self.assertEqual(fetcher._duration_seconds("12:34"), 754)
        self.assertEqual(fetcher._duration_seconds("1:02:03"), 3723)
        self.assertEqual(fetcher._duration_seconds("0:45"), 45)
        self.assertEqual(fetcher._duration_seconds(754), 754)
        self.assertEqual(fetcher._duration_seconds(754.6), 754)
        self.assertEqual(fetcher._duration_seconds("754"), 754)

    def test_unusable_lengths_are_left_out_rather_than_guessed(self):
        for value in (None, "", "LIVE", "1:2:3:4", "abc", -5, True, {"simpleText": "1:00:00:00"}):
            self.assertIsNone(fetcher._duration_seconds(value), value)

    def test_a_home_feed_renderer_contributes_its_length(self):
        renderer = {
            "videoId": VIDEO["video_id"],
            "title": {"runs": [{"text": VIDEO["title"]}]},
            "ownerText": {"runs": [{"text": VIDEO["channel_name"]}]},
            "lengthText": {"simpleText": "18:07"},
        }

        video = fetcher._video_from_renderer(renderer)

        self.assertEqual(video["duration_seconds"], 1087)


if __name__ == "__main__":
    unittest.main()