import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fetcher
from fetcher import (
    CURATION_SYSTEM_PROMPT,
    LiveSearchUnavailableError,
    ProviderConfigurationError,
    ProviderRequestError,
    VideoCurator,
    YouTubeInnerTubeClient,
    extract_videos,
    search_and_filter_videos,
)


VIDEO = {
    "video_id": "dQw4w9WgXcQ",
    "title": "A useful hardware project",
    "channel_name": "Workshop",
    "description": "A repair guide.",
}


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


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

    def test_curation_keeps_only_known_approved_candidates(self):
        completion = FakeCompletions(
            json.dumps(
                {
                    "approved_videos": [
                        {
                            "id": VIDEO["video_id"],
                            "title": "Incorrect title from the model",
                            "channel": "Incorrect channel from the model",
                            "reason": "It teaches a concrete hardware repair skill.",
                        },
                        {
                            "id": "aaaaaaaaaaa",
                            "title": "Hallucinated video",
                            "channel": "Nobody",
                            "reason": "This must not be stored.",
                        },
                    ]
                }
            )
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completion))

        approved = VideoCurator(client, "test-model").curate([VIDEO])

        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["title"], VIDEO["title"])
        self.assertEqual(approved[0]["channel_name"], VIDEO["channel_name"])
        self.assertEqual(
            approved[0]["curation_reason"],
            "It teaches a concrete hardware repair skill.",
        )
        self.assertEqual(completion.request["model"], "test-model")
        self.assertEqual(completion.request["temperature"], 0)
        self.assertEqual(
            completion.request["response_format"]["json_schema"]["schema"]["required"],
            ["approved_videos"],
        )
        self.assertEqual(completion.request["messages"][0]["content"], CURATION_SYSTEM_PROMPT)

    def test_curation_uses_a_custom_system_prompt(self):
        completion = FakeCompletions(json.dumps({"approved_videos": []}))
        client = SimpleNamespace(chat=SimpleNamespace(completions=completion))

        VideoCurator(client, "test-model", "Only approve baking videos.").curate([VIDEO])

        self.assertEqual(
            completion.request["messages"][0]["content"], "Only approve baking videos."
        )

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

    def test_missing_openai_key_prevents_youtube_search(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=True), patch(
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
        curate = Mock(return_value=[])
        fake_curator = SimpleNamespace(curate=curate)

        with patch(
            "fetcher.YouTubeInnerTubeClient.from_environment", return_value=fake_client
        ), patch("fetcher._curator_from_environment", return_value=fake_curator):
            videos = fetcher.refresh_feed(["Workshop"])

        self.assertEqual([video["video_id"] for video in videos], ["aaaaaaaaaaa"])
        curate.assert_called_once_with([other_video])

    def test_search_and_filter_videos_includes_trusted_creators(self):
        trusted_video = dict(VIDEO, video_id="aaaaaaaaaaa", channel_name="Workshop")
        fake_client = SimpleNamespace(
            search_videos=lambda query, limit=fetcher.MAX_CANDIDATES: [trusted_video]
        )
        curate = Mock(return_value=[])
        fake_curator = SimpleNamespace(curate=curate)

        with patch(
            "fetcher.YouTubeInnerTubeClient.from_environment", return_value=fake_client
        ), patch("fetcher._curator_from_environment", return_value=fake_curator):
            videos = search_and_filter_videos("home servers", ["Workshop"])

        self.assertEqual([video["video_id"] for video in videos], ["aaaaaaaaaaa"])
        curate.assert_called_once_with([])

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

    def test_evaluate_video_returns_the_raw_curation_response_for_one_video(self):
        fake_client = SimpleNamespace(video_details=lambda video_id: VIDEO)
        raw_response = {"approved_videos": []}
        evaluate = Mock(return_value=raw_response)
        fake_curator = SimpleNamespace(evaluate=evaluate)

        with patch(
            "fetcher.YouTubeInnerTubeClient.from_environment", return_value=fake_client
        ), patch("fetcher._curator_from_environment", return_value=fake_curator):
            result = fetcher.evaluate_video(VIDEO["video_id"])

        self.assertEqual(result, {"video": VIDEO, "llm_response": raw_response})
        evaluate.assert_called_once_with(VIDEO)


if __name__ == "__main__":
    unittest.main()