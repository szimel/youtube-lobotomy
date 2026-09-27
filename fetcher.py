"""Authenticated YouTube retrieval and Jev-based curation."""

from __future__ import annotations

import copy
import hashlib
import http.cookiejar
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, NotRequired, Sequence, TypedDict

import yt_dlp


MAX_CANDIDATES = 20
MAX_CANDIDATE_LIMIT = 20
MAX_HISTORY_ENTRIES = 50
JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
JEV_DEFAULT_MODEL = "jev-latest"
JEV_MAX_CRITERIA = 10
JEV_TIMEOUT_SECONDS = 60.0
JEV_WORKERS = 4
TRANSCRIPT_CACHE_LIMIT = 120
TRANSCRIPT_WORKERS = 4
# Captions come from a rate-limited endpoint, so the transcript pass runs against
# a deadline: whatever has arrived by then is used, and the rest is judged on the
# title and description alone rather than making the user wait indefinitely.
TRANSCRIPT_BUDGET_SECONDS = 90.0
CAPTION_PAUSE_SECONDS = 0.4
CAPTION_RETRIES = 2
CAPTION_BACKOFF_SECONDS = 2.0
YOUTUBE_SOCKET_TIMEOUT = 15

# Jev 1.13 documents a 64k context, of which 32k covers `state` plus the longest
# question. The state also carries the viewer profile, title and description, so
# a transcript has to stay well below that ceiling: a 30k-token transcript with a
# long description overflows the budget, and every call then fails with HTTP 400
# only after the transcript pass has already been paid for.
JEV_MAX_STATE_TOKENS = 32_000
JEV_TRANSCRIPT_TOKEN_CEILING = 20_000
# Jev bills input only, at $0.042 per million tokens; output is free.
JEV_INPUT_USD_PER_MTOK = 0.042
# Any one of these proves the export came from a signed-in session. They are what
# the SAPISIDHASH Authorization header is derived from.
_AUTH_COOKIE_NAMES = frozenset({"SAPISID", "__Secure-3PAPISID", "APISID"})

# Called as progress(step, detail, completed, total) while a pipeline runs.
# Counts are optional: omit them when a step has nothing to count.
ProgressCallback = Callable[[str, str, "int | None", "int | None"], None]
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_ORIGIN = "https://www.youtube.com"
WATCH_HISTORY_URL = f"{YOUTUBE_ORIGIN}/feed/history"
WATCH_URL_TEMPLATE = YOUTUBE_ORIGIN + "/watch?v={video_id}"
YOUTUBE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# YouTube rotates account session cookies on any browser tab left open on
# youtube.com, so a cookies.txt exported from a regular, actively-used browser
# session is stale almost immediately even though it still looks well-formed.
# The fix (per yt-dlp's maintainers) is to export from a private/incognito
# session that is never reused for browsing afterward.
COOKIE_REFRESH_GUIDANCE = (
    "YouTube rotates account session cookies on any browser tab left open on "
    "youtube.com, so cookies exported from your regular, daily-driver browser "
    "session go stale almost immediately - re-exporting from that same tab "
    "will not help. Instead: open a new private/incognito window, log into "
    "YouTube there, in that same tab immediately visit "
    "https://www.youtube.com/robots.txt (do not browse anywhere else), export "
    "the youtube.com cookies to ./data/cookies.txt, then close that incognito "
    "window for good and never reopen the session in a browser again."
)

DEFAULT_JEV_CONFIG: dict[str, Any] = {
    "profile": (
        "The viewer is an adult who wants to discover niche hobbies, tangible skills and "
        "inspiring ideas. They are prone to doom-scrolling and want substance rather than "
        "entertainment."
    ),
    "positives": [
        {
            "name": "Teaches something real",
            "instruction": (
                "The video's main purpose is to teach the viewer a tangible, practical skill "
                "or to explain how something works in technical or factual depth. Qualifying "
                "examples: hardware modification, repair and restoration, server or homelab "
                "configuration, networking, programming, woodworking, cooking technique, and "
                "science or engineering explained in depth."
            ),
            "threshold": 0.5,
            "enabled": True,
        }
    ],
    "disqualifiers": [
        {
            "name": "Reaction or drama",
            "instruction": (
                "The video is primarily a reaction to, commentary on, or gossip about other "
                "people, other creators, or online drama."
            ),
            "threshold": 0.5,
            "enabled": True,
        },
        {
            "name": "Gaming or stream highlights",
            "instruction": (
                "The video is primarily gameplay of a video game, a Let's Play, or a "
                "highlight compilation of a live stream."
            ),
            "threshold": 0.5,
            "enabled": True,
        },
        {
            "name": "Vlog or lifestyle",
            "instruction": (
                "The video is primarily a personal vlog, lifestyle content, or a display of "
                "wealth and possessions."
            ),
            "threshold": 0.5,
            "enabled": True,
        },
        {
            "name": "Clickbait or outrage",
            "instruction": (
                "The video's title promises substantially more than the content delivers, "
                "or the video relies on sensationalism or outrage rather than substance."
            ),
            "threshold": 0.5,
            "enabled": True,
        },
    ],
    "rating": {
        "enabled": False,
        "instruction": (
            "How much lasting educational or practical value does this video deliver to "
            "this viewer?"
        ),
        "criteria": [
            "Pure time-waster: reaction, drama, gossip or outrage with no substance",
            "Entertainment only: gameplay, vlog or lifestyle with no practical value",
            "Light entertainment with a little incidental information",
            "Mildly informative but shallow; the title overpromises the content",
            "Some practical information, but mostly padding or promotion",
            "Moderately useful; a few concrete takeaways",
            "Useful; teaches something concrete but without much depth",
            "Solid: clear practical instruction or a well-researched explanation",
            "Strong: in-depth teaching of a tangible skill or a well-supported essay",
            "Exceptional: deep, durable knowledge or genuine skill transfer",
        ],
        "minimum": 7.0,
    },
    "transcript_min_tokens": 1500,
    "transcript_max_tokens": 15000,
}


class Video(TypedDict):
    video_id: str
    title: str
    channel_name: str
    description: str
    thumbnail_url: NotRequired[str]
    published_at: NotRequired[str]
    duration_seconds: NotRequired[int]
    curation_reason: NotRequired[str]


@dataclass
class JevAnswer:
    """One Jev reply, kept whole so cost and model drift stay visible."""

    answers: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


@dataclass
class RunStats:
    """What a pipeline run actually did, as opposed to what it decided.

    Every one of these numbers was previously thrown away, which made a bad run
    (calls failing, captions rate-limited) indistinguishable from a strict one.
    """

    candidates: int = 0
    trusted: int = 0
    judged: int = 0
    unjudged: int = 0
    approved: int = 0
    rejected: int = 0
    with_transcript: int = 0
    skipped_watched: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None
    run_seconds: float = 0.0
    unjudged_titles: list[str] = field(default_factory=list)

    @property
    def estimated_cost_usd(self) -> float:
        """Jev bills input only, so this is the whole cost of the run."""
        return round(self.input_tokens / 1_000_000 * JEV_INPUT_USD_PER_MTOK, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "trusted": self.trusted,
            "judged": self.judged,
            "unjudged": self.unjudged,
            "approved": self.approved,
            "rejected": self.rejected,
            "with_transcript": self.with_transcript,
            "skipped_watched": self.skipped_watched,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "run_seconds": round(self.run_seconds, 1),
            "estimated_cost_usd": self.estimated_cost_usd,
            "unjudged_titles": list(self.unjudged_titles),
        }


@dataclass
class CandidateRecord:
    """One candidate's outcome, kept so a run can be inspected and re-decided.

    `answers` is the reason this is cheap to store: re-applying edited rules is
    `decide(answers, new_config)`, so tuning a threshold costs nothing and needs
    no second call to Jev.
    """

    video: Video
    decision: dict[str, Any] | None
    answers: dict[str, Any]
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        transcript = str(self.video.get("transcript") or "")
        return {
            "video": {
                key: value
                for key, value in self.video.items()
                if key != "transcript"
            },
            "transcript_tokens": _estimate_tokens(transcript),
            "used_transcript": bool(transcript),
            "decision": self.decision,
            "answers": self.answers,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
        }


@dataclass
class CurateResult:
    """Approved videos plus one record per candidate, judged or not."""

    approved: list[Video] = field(default_factory=list)
    records: list[CandidateRecord] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(record.input_tokens for record in self.records)

    @property
    def output_tokens(self) -> int:
        return sum(record.output_tokens for record in self.records)

    @property
    def model(self) -> str | None:
        for record in self.records:
            if record.model:
                return record.model
        return None


@dataclass
class RunOutcome:
    """The approved videos plus everything needed to explain the run."""

    videos: list[Video]
    stats: RunStats
    candidates: list[CandidateRecord]


class ProviderConfigurationError(RuntimeError):
    """Raised when local credentials or cookies are unavailable or invalid."""


class ProviderRequestError(RuntimeError):
    """Raised when YouTube or Jev cannot complete a live request."""


class LiveFeedUnavailableError(RuntimeError):
    """Raised when the Home feed cannot start due to local configuration."""


class LiveSearchUnavailableError(RuntimeError):
    """Raised when manual search cannot start due to local configuration."""


class LiveTestUnavailableError(RuntimeError):
    """Raised when the single-video curation test cannot start."""


class LiveWatchLogUnavailableError(RuntimeError):
    """Raised when the account's YouTube watch history cannot be read."""


MAX_PROVIDER_ERROR_DETAIL = 300


def _redact_secrets(text: str) -> str:
    """Guarantees a provider message can never echo back the configured API key."""
    secret = os.environ.get("JEV_API_KEY", "").strip()
    if secret:
        text = text.replace(secret, "[redacted]")
    return text


def _short_error(text: str) -> str:
    detail = " ".join(_redact_secrets(text).split())
    if len(detail) > MAX_PROVIDER_ERROR_DETAIL:
        detail = detail[: MAX_PROVIDER_ERROR_DETAIL - 3] + "..."
    return detail


def _http_error_detail(error: urllib.error.HTTPError) -> str:
    """Surfaces what the provider actually complained about, minus any credential."""
    try:
        body = error.read(400).decode("utf-8", "replace")
    except Exception:
        body = ""
    return _short_error(body) if body.strip() else f"HTTP {error.code}"


def _duration_seconds(value: object) -> int | None:
    """Reads a video length from "12:34", "1:02:03" or a plain second count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None

    text = _text(value)
    if not text:
        return None
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total or None


@dataclass(frozen=True)
class InnerTubeConfig:
    api_key: str
    context: dict[str, Any]
    client_name: str
    client_version: str


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""

    simple_text = value.get("simpleText")
    if isinstance(simple_text, str):
        return simple_text.strip()

    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    return "".join(
        run.get("text", "") for run in runs if isinstance(run, dict)
    ).strip()


def _first_text(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _thumbnail_url(renderer: dict[str, Any]) -> str:
    thumbnail = renderer.get("thumbnail")
    if not isinstance(thumbnail, dict):
        return ""
    thumbnails = thumbnail.get("thumbnails")
    if not isinstance(thumbnails, list):
        return ""
    for item in reversed(thumbnails):
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            return item["url"]
    return ""


def _description(renderer: dict[str, Any]) -> str:
    description = _text(renderer.get("descriptionSnippet"))
    if description:
        return description

    snippets = renderer.get("detailedMetadataSnippets")
    if not isinstance(snippets, list):
        return ""
    return " ".join(
        _text(snippet.get("snippetText"))
        for snippet in snippets
        if isinstance(snippet, dict) and _text(snippet.get("snippetText"))
    )


def _video_from_renderer(renderer: dict[str, Any]) -> Video | None:
    video_id = renderer.get("videoId")
    title = _text(renderer.get("title"))
    if not isinstance(video_id, str) or not title:
        return None

    video: Video = {
        "video_id": video_id,
        "title": title,
        "channel_name": _first_text(
            renderer.get("ownerText"),
            renderer.get("longBylineText"),
            renderer.get("shortBylineText"),
        ),
        "description": _description(renderer),
    }
    thumbnail_url = _thumbnail_url(renderer)
    if thumbnail_url:
        video["thumbnail_url"] = thumbnail_url
    published_at = _text(renderer.get("publishedTimeText"))
    if published_at:
        video["published_at"] = published_at
    duration = _duration_seconds(renderer.get("lengthText"))
    if duration:
        video["duration_seconds"] = duration
    return video


def _video_renderers(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _video_renderers(item)
        return
    if not isinstance(value, dict):
        return

    renderer = value.get("videoRenderer")
    if isinstance(renderer, dict):
        yield renderer
    for key, item in value.items():
        if key != "videoRenderer":
            yield from _video_renderers(item)


def extract_videos(response: object, limit: int = MAX_CANDIDATES) -> list[Video]:
    videos: list[Video] = []
    seen_video_ids: set[str] = set()
    for renderer in _video_renderers(response):
        video = _video_from_renderer(renderer)
        if video is None or video["video_id"] in seen_video_ids:
            continue
        seen_video_ids.add(video["video_id"])
        videos.append(video)
        if len(videos) >= limit:
            break
    return videos


def _ytcfg(document: str) -> dict[str, Any]:
    marker = "ytcfg.set("
    search_from = 0
    while True:
        marker_index = document.find(marker, search_from)
        if marker_index == -1:
            break
        search_from = marker_index + len(marker)
        object_index = document.find("{", search_from)
        if object_index == -1:
            continue

        try:
            config, _ = json.JSONDecoder().raw_decode(document[object_index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(config, dict):
            continue
        if {"INNERTUBE_API_KEY", "INNERTUBE_CONTEXT"}.issubset(config):
            return config

    raise ProviderRequestError("YouTube did not provide its InnerTube configuration.")


def _innertube_config(document: str) -> InnerTubeConfig:
    config = _ytcfg(document)
    api_key = config.get("INNERTUBE_API_KEY")
    context = config.get("INNERTUBE_CONTEXT")
    if not isinstance(api_key, str) or not isinstance(context, dict):
        raise ProviderRequestError("YouTube did not provide its InnerTube credentials.")

    client = context.get("client")
    if not isinstance(client, dict):
        raise ProviderRequestError("YouTube did not provide its InnerTube client context.")

    client_name = config.get("INNERTUBE_CONTEXT_CLIENT_NAME", client.get("clientName"))
    client_version = config.get(
        "INNERTUBE_CONTEXT_CLIENT_VERSION", client.get("clientVersion")
    )
    if client_name is None or not isinstance(client_version, str):
        raise ProviderRequestError("YouTube did not provide its InnerTube client version.")

    return InnerTubeConfig(
        api_key=api_key,
        context=context,
        client_name=str(client_name),
        client_version=client_version,
    )


def _relative_environment_path(name: str, default: str) -> Path:
    value = os.environ.get(name, default)
    path = Path(value)
    if path.is_absolute():
        raise ProviderConfigurationError(f"{name} must use a relative path.")
    return path


class _WarningCollectingLogger:
    """Captures yt-dlp diagnostics so a rotated/expired cookie session can be detected."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.messages.append(message)

    def error(self, message: str) -> None:
        self.messages.append(message)


# yt-dlp persists the (rotated) cookie jar back to the cookiefile when it
# closes, which keeps the exported session fresh. Serialising yt-dlp calls
# stops a background watch-log check and a feed refresh from writing that
# credential file at the same time.
_YTDLP_LOCK = threading.Lock()

# A dead session shows up in several different ways depending on which endpoint
# failed, and only one of them says "rotated". Every one of these means the same
# thing to the user, so all of them get the re-export instructions.
SESSION_FAILURE_MARKERS = (
    "rotated",
    "needs to be reloaded",
    "sign in",
    "signed in",
    "login",
    "cookie",
    "unauthorized",
    "forbidden",
    "http error 403",
)


def _looks_like_session_failure(*texts: str) -> bool:
    haystack = " ".join(texts).lower()
    return any(marker in haystack for marker in SESSION_FAILURE_MARKERS)


def _session_or_request_error(
    summary: str, error: BaseException, messages: Sequence[str]
) -> ProviderConfigurationError | ProviderRequestError:
    """Turns a yt-dlp failure into the most useful error the user can act on."""
    detail = f"{type(error).__name__}: {error}"
    if _looks_like_session_failure(detail, *messages):
        return ProviderConfigurationError(
            f"{summary} YouTube rejected the exported cookies. "
            + COOKIE_REFRESH_GUIDANCE
        )
    described = _short_error(detail)
    return ProviderRequestError(f"{summary} ({described})" if described else summary)


def _home_feed_entries(cookie_path: str, limit: int) -> list[dict[str, Any]]:
    logger = _WarningCollectingLogger()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "cookiefile": cookie_path,
        "playlist_items": f"1:{limit}",
        "logger": logger,
    }
    try:
        with _YTDLP_LOCK, yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(":ytrec", download=False)
    except Exception as error:
        raise _session_or_request_error(
            "YouTube's Home feed could not be loaded.", error, logger.messages
        ) from error

    # yt-dlp can warn that the session is dead and still hand back a feed, which
    # would quietly be a signed-out feed rather than the account's own.
    if _looks_like_session_failure(*logger.messages):
        raise ProviderConfigurationError(
            "YouTube's Home feed could not be loaded with the exported cookies. "
            + COOKIE_REFRESH_GUIDANCE
        )
    return list((info or {}).get("entries") or [])


def _watch_history_entries(cookie_path: str, limit: int) -> list[dict[str, Any]]:
    """Reads the signed-in account's watch history through yt-dlp.

    The embedded player gives no signal at all when it silently drops the
    viewer's identity, so the only dependable way to notice that a watch was
    never recorded is to read the account's history back and check for it.
    """
    logger = _WarningCollectingLogger()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "cookiefile": cookie_path,
        "playlist_items": f"1:{limit}",
        "logger": logger,
    }
    try:
        with _YTDLP_LOCK, yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(WATCH_HISTORY_URL, download=False)
    except Exception as error:
        raise _session_or_request_error(
            "YouTube's watch history could not be loaded.", error, logger.messages
        ) from error

    if _looks_like_session_failure(*logger.messages):
        raise ProviderConfigurationError(
            "YouTube's watch history could not be loaded with the exported "
            "cookies. " + COOKIE_REFRESH_GUIDANCE
        )
    return list((info or {}).get("entries") or [])


def fetch_watch_history(limit: int = MAX_HISTORY_ENTRIES) -> list[Video]:
    """Returns the account's most recent watch-history entries, newest first."""
    try:
        cookie_path = _relative_environment_path(
            "YOUTUBE_COOKIES_PATH", "./data/cookies.txt"
        )
    except ProviderConfigurationError as error:
        raise LiveWatchLogUnavailableError(str(error)) from error

    if not cookie_path.is_file():
        raise LiveWatchLogUnavailableError(
            "Add an exported YouTube cookies.txt file at ./data/cookies.txt so "
            "watch logging can be verified."
        )

    try:
        entries = _watch_history_entries(str(cookie_path), limit)
    except ProviderConfigurationError as error:
        raise LiveWatchLogUnavailableError(str(error)) from error

    videos: list[Video] = []
    seen_video_ids: set[str] = set()
    for entry in entries:
        video = _video_from_flat_entry(entry)
        if video is None or video["video_id"] in seen_video_ids:
            continue
        seen_video_ids.add(video["video_id"])
        videos.append(video)
    return videos


def _estimate_tokens(text: str) -> int:
    """Cheap English approximation: roughly four characters per token."""
    return round(len(text) / 4) if text else 0


def _anonymous_ytdlp_options() -> dict[str, Any]:
    """Options for cookie-free metadata and caption reads.

    The exported session is rejected for player requests ("The page needs to be
    reloaded"), while the same request without cookies succeeds. Captions and
    descriptions are public data, so they are read anonymously instead - which
    also means these calls never touch the credential file and can run in
    parallel with each other.
    """
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "js_runtimes": {"node": {}},
        "socket_timeout": YOUTUBE_SOCKET_TIMEOUT,
        "cachedir": False,
        "logger": _WarningCollectingLogger(),
    }


def _caption_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    events = payload.get("events")
    if not isinstance(events, list):
        return ""
    pieces: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if isinstance(segment, dict) and isinstance(segment.get("utf8"), str):
                pieces.append(segment["utf8"])
    return " ".join("".join(pieces).split())


def _caption_tracks(info: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    manual = info.get("subtitles")
    automatic = info.get("automatic_captions")
    manual_en = manual.get("en") if isinstance(manual, dict) else None
    automatic_en = automatic.get("en") if isinstance(automatic, dict) else None
    if manual_en:
        return list(manual_en), "manual"
    if automatic_en:
        return list(automatic_en), "auto"
    return [], "none"


def _download_caption(track: dict[str, Any]) -> str:
    """Downloads one caption track, backing off when YouTube rate limits us."""
    url = str(track.get("url", "")).replace("&fmt=json3", "")
    if not url:
        return ""
    separator = "&" if "?" in url else "?"
    last_error: Exception | None = None

    for attempt in range(CAPTION_RETRIES):
        try:
            request = urllib.request.Request(
                f"{url}{separator}fmt=json3",
                headers={"User-Agent": YOUTUBE_USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return _caption_text(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code != 429:
                break
            time.sleep(CAPTION_BACKOFF_SECONDS * (attempt + 1))
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            break

    if last_error is not None:
        raise ProviderRequestError(
            f"YouTube did not return captions ({_short_error(str(last_error))})."
        ) from last_error
    return ""


_CONTEXT_CACHE: dict[str, dict[str, Any]] = {}

# Captions do not change once published, and re-fetching them is both the slow
# part of a refresh (~26s for 20 videos) and the part YouTube rate limits (~20%
# of downloads come back 429). Keeping them on disk makes a repeat refresh, a
# rule preview, or a restart nearly free of caption traffic.
TRANSCRIPT_CACHE_DIRNAME = "transcripts"
TRANSCRIPT_CACHE_MAX_FILES = 200
# A *failed* caption download must not be remembered as "this video has no
# captions" forever, or one rate-limited moment would blind the curator to that
# video for good.
TRANSCRIPT_NEGATIVE_TTL_SECONDS = 3600.0

_transcript_cache_directory: Path | None = None
_transcript_cache_lock = threading.Lock()


def set_data_directory(directory: str | Path | None) -> None:
    """Points the transcript cache at the application's data directory."""
    global _transcript_cache_directory
    with _transcript_cache_lock:
        _transcript_cache_directory = Path(directory) if directory else None
        _CONTEXT_CACHE.clear()


def _transcript_cache_file(video_id: str) -> Path | None:
    directory = _transcript_cache_directory
    if directory is None or not VIDEO_ID_PATTERN.fullmatch(video_id):
        return None
    return directory / TRANSCRIPT_CACHE_DIRNAME / f"{video_id}.json"


def _read_cached_context(video_id: str) -> dict[str, Any] | None:
    path = _transcript_cache_file(video_id)
    if path is None or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            stored = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, dict):
        return None
    context = stored.get("context")
    if not isinstance(context, dict):
        return None

    fetched_at = stored.get("fetched_at")
    fetched_at = float(fetched_at) if isinstance(fetched_at, (int, float)) else 0.0
    if not context.get("transcript"):
        if time.time() - fetched_at > TRANSCRIPT_NEGATIVE_TTL_SECONDS:
            return None
    return context


def _prune_transcript_cache(directory: Path) -> None:
    try:
        files = sorted(
            directory.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[TRANSCRIPT_CACHE_MAX_FILES:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _write_cached_context(context: dict[str, Any]) -> None:
    path = _transcript_cache_file(str(context.get("video_id") or ""))
    if path is None:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary_file:
            json.dump({"fetched_at": time.time(), "context": context}, temporary_file)
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    except OSError:
        # The cache is an optimisation; a read-only disk must not fail a refresh.
        return
    _prune_transcript_cache(path.parent)


def fetch_video_context(video_id: str) -> dict[str, Any]:
    """Title, channel, description and transcript for one video, without cookies."""
    cached = _CONTEXT_CACHE.get(video_id)
    if cached is not None:
        return dict(cached)

    stored = _read_cached_context(video_id)
    if stored is not None:
        _CONTEXT_CACHE[video_id] = stored
        return dict(stored)

    try:
        # Deliberately not holding _YTDLP_LOCK: these calls carry no cookiefile,
        # so there is no credential file for them to race over, and holding it
        # would serialise the whole transcript pass.
        with yt_dlp.YoutubeDL(_anonymous_ytdlp_options()) as downloader:
            info = downloader.extract_info(
                WATCH_URL_TEMPLATE.format(video_id=video_id), download=False
            )
    except Exception as error:
        raise ProviderRequestError(
            f"YouTube did not return details for {video_id}."
        ) from error

    if not isinstance(info, dict):
        raise ProviderRequestError(f"YouTube did not return details for {video_id}.")

    transcript = ""
    tracks, caption_kind = _caption_tracks(info)
    if tracks:
        preferred = next(
            (track for track in tracks if track.get("ext") == "json3"), tracks[0]
        )
        try:
            transcript = _download_caption(preferred)
        except ProviderRequestError:
            caption_kind = "unavailable"
    time.sleep(CAPTION_PAUSE_SECONDS)

    context = {
        "video_id": video_id,
        "title": str(info.get("title") or ""),
        "channel_name": str(info.get("uploader") or info.get("channel") or ""),
        "description": str(info.get("description") or "").strip(),
        "transcript": transcript,
        "caption_kind": caption_kind,
        "transcript_tokens": _estimate_tokens(transcript),
        "duration_seconds": _duration_seconds(info.get("duration")) or 0,
    }

    _CONTEXT_CACHE[video_id] = context
    while len(_CONTEXT_CACHE) > TRANSCRIPT_CACHE_LIMIT:
        _CONTEXT_CACHE.pop(next(iter(_CONTEXT_CACHE)))
    _write_cached_context(context)
    return dict(context)


def apply_transcript_policy(
    video: Video, context: dict[str, Any], config: dict[str, Any]
) -> Video:
    """Fills in missing metadata and attaches a transcript only if it is useful.

    A very short transcript (music, silent demonstrations) says less than the
    title does, and Jev reads literally, so those are left out entirely.
    """
    enriched = dict(video)

    if context.get("description") and not enriched.get("description"):
        enriched["description"] = context["description"]
    if context.get("channel_name") and not enriched.get("channel_name"):
        enriched["channel_name"] = context["channel_name"]
    if context.get("title") and not enriched.get("title"):
        enriched["title"] = context["title"]
    # Home-feed renderers carry no length, but the anonymous video lookup does.
    if context.get("duration_seconds") and not enriched.get("duration_seconds"):
        enriched["duration_seconds"] = context["duration_seconds"]

    transcript = str(context.get("transcript") or "")
    minimum = int(config.get("transcript_min_tokens") or 0)
    maximum = int(config.get("transcript_max_tokens") or 0)
    # A configured maximum above the ceiling is honoured up to the ceiling: the
    # profile, title and description share Jev's state budget with the transcript.
    maximum = min(maximum, JEV_TRANSCRIPT_TOKEN_CEILING) if maximum else 0
    if _estimate_tokens(transcript) < minimum:
        return enriched
    if maximum and _estimate_tokens(transcript) > maximum:
        transcript = transcript[: maximum * 4]

    enriched["transcript"] = transcript
    return enriched


def parse_video_id(value: str) -> str | None:
    """Extracts an 11-character video ID from a bare ID or common YouTube URL shapes."""
    value = value.strip()
    if VIDEO_ID_PATTERN.fullmatch(value):
        return value

    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host.startswith("www.") or host.startswith("m."):
        host = host.split(".", 1)[1]

    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in {"youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        else:
            segments = [segment for segment in parsed.path.split("/") if segment]
            if len(segments) >= 2 and segments[0] in {"embed", "shorts", "live", "v"}:
                candidate = segments[1]

    return candidate if VIDEO_ID_PATTERN.fullmatch(candidate) else None


def _video_from_flat_entry(entry: dict[str, Any]) -> Video | None:
    video_id = entry.get("id")
    title = entry.get("title")
    if not isinstance(video_id, str) or not video_id or not isinstance(title, str) or not title:
        return None

    video: Video = {
        "video_id": video_id,
        "title": title,
        "channel_name": entry.get("channel") or entry.get("uploader") or "",
        "description": entry.get("description") or "",
    }
    best_url = ""
    best_area = -1
    thumbnails = entry.get("thumbnails")
    for thumbnail in thumbnails if isinstance(thumbnails, list) else []:
        if not isinstance(thumbnail, dict) or not isinstance(thumbnail.get("url"), str):
            continue
        area = (thumbnail.get("width") or 0) * (thumbnail.get("height") or 0)
        if area >= best_area:
            best_area = area
            best_url = thumbnail["url"]
    if best_url:
        video["thumbnail_url"] = best_url
    duration = _duration_seconds(entry.get("duration"))
    if duration:
        video["duration_seconds"] = duration
    return video


def _video_from_player_response(response: dict[str, Any]) -> Video | None:
    details = response.get("videoDetails")
    if not isinstance(details, dict):
        return None

    video_id = details.get("videoId")
    title = details.get("title")
    if not isinstance(video_id, str) or not video_id or not isinstance(title, str) or not title:
        return None

    video: Video = {
        "video_id": video_id,
        "title": title,
        "channel_name": details.get("author")
        if isinstance(details.get("author"), str)
        else "",
        "description": details.get("shortDescription")
        if isinstance(details.get("shortDescription"), str)
        else "",
    }
    thumbnail = details.get("thumbnail")
    thumbnails = thumbnail.get("thumbnails") if isinstance(thumbnail, dict) else None
    best_url = ""
    best_area = -1
    for item in thumbnails if isinstance(thumbnails, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        area = (item.get("width") or 0) * (item.get("height") or 0)
        if area >= best_area:
            best_area = area
            best_url = item["url"]
    if best_url:
        video["thumbnail_url"] = best_url
    duration = _duration_seconds(details.get("lengthSeconds"))
    if duration:
        video["duration_seconds"] = duration
    return video


class YouTubeInnerTubeClient:
    """Minimal authenticated InnerTube client for Home and manual search results."""

    def __init__(self, cookie_jar: http.cookiejar.MozillaCookieJar):
        self._cookie_jar = cookie_jar
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        self._config: InnerTubeConfig | None = None

    @classmethod
    def from_environment(cls) -> YouTubeInnerTubeClient:
        cookie_path = _relative_environment_path(
            "YOUTUBE_COOKIES_PATH", "./data/cookies.txt"
        )
        if not cookie_path.is_file():
            raise ProviderConfigurationError(
                "Add an exported YouTube cookies.txt file at ./data/cookies.txt."
            )

        cookie_jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        try:
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as error:
            raise ProviderConfigurationError(
                "Could not read the YouTube cookies.txt file. Export it in Netscape format."
            ) from error

        if not any(
            cookie.domain.lstrip(".").endswith("youtube.com") for cookie in cookie_jar
        ):
            raise ProviderConfigurationError(
                "The cookies.txt file does not contain YouTube cookies."
            )

        # Search and single-video lookups go through InnerTube, where the
        # SAPISIDHASH header is the only thing that identifies the account.
        # Without it those requests go out signed out and still return plausible
        # results, so the app would curate a feed that is not the account's.
        if not any(cookie.name in _AUTH_COOKIE_NAMES for cookie in cookie_jar):
            raise ProviderConfigurationError(
                "The cookies.txt file has no signed-in session cookie (SAPISID), "
                "so YouTube would answer search and video lookups signed out. "
                + COOKIE_REFRESH_GUIDANCE
            )
        return cls(cookie_jar)

    def home_videos(self, limit: int = MAX_CANDIDATES) -> list[Video]:
        entries = _home_feed_entries(self._cookie_jar.filename, limit)
        videos: list[Video] = []
        seen_video_ids: set[str] = set()
        for entry in entries:
            video = _video_from_flat_entry(entry)
            if video is None or video["video_id"] in seen_video_ids:
                continue
            seen_video_ids.add(video["video_id"])
            videos.append(video)
        if not videos:
            raise ProviderRequestError(
                "YouTube's Home feed returned no recommendations for this account right now."
            )
        return videos

    def search_videos(self, query: str, limit: int = MAX_CANDIDATES) -> list[Video]:
        response = self._api_request("search", {"query": query})
        return extract_videos(response, limit)

    def video_details(self, video_id: str) -> Video:
        video = _video_from_player_response(
            self._api_request("player", {"videoId": video_id})
        )
        if video is None:
            raise ProviderRequestError("YouTube did not return details for that video.")
        return video

    def _api_request(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._bootstrap()
        request_payload = {"context": copy.deepcopy(config.context), **payload}
        query = urllib.parse.urlencode({"key": config.api_key, "prettyPrint": "false"})
        url = f"{YOUTUBE_ORIGIN}/youtubei/v1/{endpoint}?{query}"
        headers = {
            "Content-Type": "application/json",
            "Origin": YOUTUBE_ORIGIN,
            "User-Agent": YOUTUBE_USER_AGENT,
            "X-Goog-AuthUser": "0",
            "X-YouTube-Client-Name": config.client_name,
            "X-YouTube-Client-Version": config.client_version,
        }
        authorization = self._sapisid_authorization()
        if authorization:
            headers["Authorization"] = authorization

        request = urllib.request.Request(
            url,
            data=json.dumps(request_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response_text = self._open(request)
        try:
            response = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise ProviderRequestError("YouTube returned an unreadable InnerTube response.") from error
        if not isinstance(response, dict):
            raise ProviderRequestError("YouTube returned an invalid InnerTube response.")
        return response

    def _bootstrap(self) -> InnerTubeConfig:
        if self._config is None:
            request = urllib.request.Request(
                f"{YOUTUBE_ORIGIN}/",
                headers={"User-Agent": YOUTUBE_USER_AGENT},
            )
            self._config = _innertube_config(self._open(request))
        return self._config

    def _sapisid_authorization(self) -> str | None:
        sapisid = next(
            (
                cookie.value
                for cookie in self._cookie_jar
                if cookie.name in _AUTH_COOKIE_NAMES
                and cookie.domain.lstrip(".").endswith("youtube.com")
            ),
            None,
        )
        if not sapisid:
            return None

        timestamp = str(int(time.time()))
        digest = hashlib.sha1(
            f"{timestamp} {sapisid} {YOUTUBE_ORIGIN}".encode("utf-8")
        ).hexdigest()
        return f"SAPISIDHASH {timestamp}_{digest}"

    def _open(self, request: urllib.request.Request) -> str:
        try:
            with self._opener.open(request, timeout=20) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise ProviderRequestError(
                f"YouTube returned HTTP {error.code}. Refresh the exported cookies if needed."
            ) from error
        except (OSError, TimeoutError, UnicodeDecodeError) as error:
            raise ProviderRequestError("Could not reach YouTube.") from error


TRUSTED_CREATOR_REASON = "Trusted creator \u2014 always included regardless of AI score."


def _matches_trusted_creator(channel_name: str, trusted_creators: Iterable[str]) -> bool:
    normalized = channel_name.strip().casefold()
    if not normalized:
        return False
    return any(normalized == creator.strip().casefold() for creator in trusted_creators)


def _partition_trusted_candidates(
    candidates: list[Video], trusted_creators: Sequence[str]
) -> tuple[list[Video], list[Video]]:
    """Splits candidates into (always-included trusted, remaining for AI review)."""
    if not trusted_creators:
        return [], candidates

    trusted: list[Video] = []
    remaining: list[Video] = []
    for video in candidates:
        if _matches_trusted_creator(video["channel_name"], trusted_creators):
            trusted_video = dict(video)
            trusted_video["curation_reason"] = TRUSTED_CREATOR_REASON
            trusted.append(trusted_video)
        else:
            remaining.append(video)
    return trusted, remaining


def build_questions(config: dict[str, Any]) -> dict[str, Any]:
    """Turns the user's rules into Jev questions.

    Positives and disqualifiers become Noul (yes/no) questions whose ordering
    matches the configured lists, so the answers can be mapped back by index.
    """
    questions: dict[str, Any] = {}
    for index, check in enumerate(config.get("positives") or []):
        if check.get("enabled", True):
            questions[f"positive_{index}"] = {
                "type": "noul",
                "instructions": check["instruction"],
            }
    for index, check in enumerate(config.get("disqualifiers") or []):
        if check.get("enabled", True):
            questions[f"disqualifier_{index}"] = {
                "type": "noul",
                "instructions": check["instruction"],
            }

    rating = config.get("rating") or {}
    criteria = [
        str(level) for level in (rating.get("criteria") or []) if str(level).strip()
    ]
    if rating.get("enabled") and len(criteria) >= 2:
        questions["rating"] = {
            "type": "score",
            "instructions": rating["instruction"],
            "criteria": criteria[:JEV_MAX_CRITERIA],
        }
    return questions


def build_state(video: Video, config: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    profile = str(config.get("profile") or "").strip()
    if profile:
        state["viewer_profile"] = profile
    state["video_title"] = video["title"]
    state["video_channel"] = video["channel_name"]
    state["video_description"] = video["description"]
    transcript = video.get("transcript")
    if transcript:
        state["video_transcript"] = transcript
    return state


def _probability(answers: dict[str, Any], key: str) -> float | None:
    entry = answers.get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def decide(answers: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Applies the configured rules to Jev's answers.

    A video is approved when every positive passes, no disqualifier triggers and,
    if enabled, the rating clears its minimum.
    """
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    strengths: list[str] = []
    # Questions Jev never answered. Tracked separately from failures so the
    # summary cannot imply a rule was satisfied when it was never evaluated.
    unanswered: list[str] = []
    approved = True

    for index, check in enumerate(config.get("positives") or []):
        if not check.get("enabled", True):
            continue
        threshold = float(check["threshold"])
        value = _probability(answers, f"positive_{index}")
        passed = value is not None and value >= threshold
        checks.append(
            {
                "kind": "positive",
                "name": check["name"],
                "value": value,
                "threshold": threshold,
                "answered": value is not None,
                "passed": passed,
            }
        )
        if passed:
            strengths.append(f"{check['name']} {value:.2f}")
        else:
            approved = False
            if value is not None:
                failures.append(
                    f"{check['name']} only {value:.2f} (needs {threshold:.2f})"
                )
            else:
                unanswered.append(check["name"])
                failures.append(f"{check['name']} went unanswered")

    for index, check in enumerate(config.get("disqualifiers") or []):
        if not check.get("enabled", True):
            continue
        threshold = float(check["threshold"])
        value = _probability(answers, f"disqualifier_{index}")
        answered = value is not None
        triggered = answered and value >= threshold
        checks.append(
            {
                "kind": "disqualifier",
                "name": check["name"],
                "value": value,
                "threshold": threshold,
                "answered": answered,
                # `None` rather than True: an unanswered red flag is not a clean
                # pass, and showing it as one overstates the evidence.
                "passed": None if not answered else not triggered,
            }
        )
        if triggered:
            approved = False
            failures.append(f"{check['name']} {value:.2f} (limit {threshold:.2f})")
        elif not answered:
            unanswered.append(check["name"])

    rating_result: dict[str, Any] | None = None
    rating = config.get("rating") or {}
    if rating.get("enabled"):
        entry = answers.get("rating")
        score: float | None = None
        confidence: float | None = None
        if isinstance(entry, dict):
            raw_score = entry.get("score")
            if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
                score = round(float(raw_score), 2)
            raw_confidence = entry.get("confidence")
            if isinstance(raw_confidence, (int, float)) and not isinstance(
                raw_confidence, bool
            ):
                confidence = round(float(raw_confidence), 2)
        minimum = float(rating.get("minimum", 0))
        rating_result = {"score": score, "confidence": confidence, "minimum": minimum}
        if score is None:
            approved = False
            unanswered.append("the rating")
            failures.append("the rating went unanswered")
        elif score < minimum:
            approved = False
            failures.append(f"rating {score:g} (needs {minimum:g})")

    if approved:
        parts = list(strengths)
        if rating_result and rating_result["score"] is not None:
            parts.append(f"rating {rating_result['score']:g}")
        summary = "Passed every rule: " + ", ".join(parts) if parts else "Approved."
        if unanswered:
            summary += f" ({', '.join(unanswered)} went unanswered)"
    else:
        summary = "Rejected — " + "; ".join(failures)

    return {
        "approved": approved,
        "checks": checks,
        "rating": rating_result,
        "summary": summary,
        "unanswered": unanswered,
    }


class JevCurator:
    """Asks Jev one set of questions per video and decides in code."""

    def __init__(self, api_key: str, model: str = JEV_DEFAULT_MODEL):
        self._api_key = api_key
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def curate(
        self,
        candidates: list[Video],
        config: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> CurateResult:
        if not candidates:
            return CurateResult()

        total = len(candidates)
        replies: list[tuple[dict[str, Any] | None, JevAnswer | None, Exception | None]] = [
            (None, None, None)
        ] * total
        first_error: Exception | None = None
        completed = 0

        def evaluate(
            index: int, video: Video
        ) -> tuple[int, dict[str, Any] | None, JevAnswer | None, Exception | None]:
            try:
                decision, reply = self.judge(video, config)
                return index, decision, reply, None
            except ProviderRequestError as error:
                return index, None, None, error

        # Jev evaluates questions in parallel and allows 1,200 requests a minute,
        # so asking about several videos at once is both safe and much faster.
        with ThreadPoolExecutor(max_workers=JEV_WORKERS) as pool:
            futures = [
                pool.submit(evaluate, index, video)
                for index, video in enumerate(candidates)
            ]
            for future in as_completed(futures):
                index, decision, reply, error = future.result()
                replies[index] = (decision, reply, error)
                if error is not None and first_error is None:
                    first_error = error
                completed += 1
                if progress is not None:
                    progress(
                        "Asking Jev",
                        f"{completed} of {total} · {candidates[index]['title'][:48]}",
                        completed,
                        total,
                    )

        # One flaky call should not throw away the whole run, but if nothing at
        # all came back the caller deserves the real error.
        if first_error is not None and all(reply[0] is None for reply in replies):
            raise first_error

        approved: list[Video] = []
        records: list[CandidateRecord] = []
        for video, (decision, reply, error) in zip(candidates, replies):
            if decision is not None and decision["approved"]:
                curated = dict(video)
                curated["curation_reason"] = decision["summary"]
                approved.append(curated)
            records.append(
                CandidateRecord(
                    video=video,
                    decision=decision,
                    answers=reply.answers if reply is not None else {},
                    # A call that failed is recorded as such. Previously it was
                    # indistinguishable from a rejection, so an outage quietly
                    # emptied the feed and looked like the rules being strict.
                    error=(
                        _short_error(f"{type(error).__name__}: {error}")
                        if error is not None
                        else None
                    ),
                    input_tokens=reply.input_tokens if reply is not None else 0,
                    output_tokens=reply.output_tokens if reply is not None else 0,
                    model=reply.model if reply is not None else None,
                )
            )
        return CurateResult(approved=approved, records=records)

    def evaluate(self, video: Video, config: dict[str, Any]) -> dict[str, Any]:
        decision, _reply = self.judge(video, config)
        return decision

    def judge(
        self, video: Video, config: dict[str, Any]
    ) -> tuple[dict[str, Any], JevAnswer]:
        """The real unit of work: one decision plus the reply it came from."""
        questions = build_questions(config)
        if not questions:
            raise ProviderConfigurationError(
                "Enable at least one rule before curating with Jev."
            )
        reply = self._ask(build_state(video, config), questions)
        return decide(reply.answers, config), reply

    def _ask(self, state: dict[str, Any], questions: dict[str, Any]) -> JevAnswer:
        body = json.dumps(
            {"model": self._model, "state": state, "questions": questions}
        ).encode("utf-8")
        request = urllib.request.Request(
            JEV_API_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=JEV_TIMEOUT_SECONDS
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ProviderRequestError(
                "Jev could not filter the YouTube results. "
                f"(HTTP {error.code}: {_http_error_detail(error)})"
            ) from error
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderRequestError(
                f"Could not reach Jev ({error.__class__.__name__})."
            ) from error

        if not isinstance(payload, dict):
            raise ProviderRequestError("Jev returned an unexpected response.")
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise ProviderRequestError("Jev returned an unexpected response.")

        # `usage` and the resolved `model` used to be dropped on the floor, which
        # left the cost of a run invisible and hid the model behind the
        # `jev-latest` alias changing underneath the app.
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        model = payload.get("model")
        return JevAnswer(
            answers=answers,
            input_tokens=(
                int(input_tokens)
                if isinstance(input_tokens, (int, float))
                and not isinstance(input_tokens, bool)
                else 0
            ),
            output_tokens=(
                int(output_tokens)
                if isinstance(output_tokens, (int, float))
                and not isinstance(output_tokens, bool)
                else 0
            ),
            model=model if isinstance(model, str) and model else None,
        )


def _curator_from_environment() -> JevCurator:
    api_key = os.environ.get("JEV_API_KEY", "").strip()
    if not api_key:
        raise ProviderConfigurationError(
            "Set JEV_API_KEY in an ignored .env file before fetching videos."
        )
    model = os.environ.get("JEV_MODEL", JEV_DEFAULT_MODEL).strip() or JEV_DEFAULT_MODEL
    return JevCurator(api_key, model)


def _enrich_with_transcripts(
    videos: list[Video],
    config: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> list[Video]:
    """Adds real descriptions and transcripts, judged later only if long enough.

    Every candidate is enriched the same way, so all of them are judged under
    identical conditions rather than a subset getting a second, different pass.
    A video whose captions cannot be fetched is still judged, just without them,
    and the whole pass stops at a deadline so a slow endpoint cannot stall the
    refresh forever. The number that came back with a usable transcript is
    reported: a caption endpoint throttling the run is otherwise invisible, and
    every decision then quietly rests on the title alone.
    """
    if not videos:
        if progress is not None:
            progress("Fetching transcripts", "nothing to fetch", 0, 0)
        return []

    total = len(videos)
    deadline = time.monotonic() + TRANSCRIPT_BUDGET_SECONDS
    enriched: list[Video] = list(videos)
    completed = 0

    def enrich(index: int, video: Video) -> tuple[int, Video]:
        if time.monotonic() >= deadline:
            return index, video
        try:
            context = fetch_video_context(video["video_id"])
        except ProviderRequestError:
            return index, video
        return index, apply_transcript_policy(video, context, config)

    with ThreadPoolExecutor(max_workers=TRANSCRIPT_WORKERS) as pool:
        futures = [
            pool.submit(enrich, index, video) for index, video in enumerate(videos)
        ]
        for future in as_completed(futures):
            index, result = future.result()
            enriched[index] = result
            completed += 1
            if progress is not None:
                progress(
                    "Fetching transcripts",
                    f"{completed} of {total} · {videos[index]['title'][:48]}",
                    completed,
                    total,
                )

    if progress is not None:
        with_transcript = sum(1 for video in enriched if video.get("transcript"))
        missing = total - with_transcript
        detail = f"{with_transcript} of {total} had transcripts"
        if missing:
            detail += f" · {missing} judged on title alone"
        progress("Fetching transcripts", detail, total, total)
    return enriched


def questions_unchanged(previous_config: dict[str, Any], proposed_config: dict[str, Any]) -> bool:
    """True when edited rules ask Jev exactly the same questions as before.

    Thresholds and rule names never reach Jev -- only the instruction text does --
    so a threshold edit leaves every stored answer still valid.
    """
    return build_questions(previous_config) == build_questions(proposed_config)


def preview_rules(
    proposed_config: dict[str, Any],
    saved_run: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Re-judges the last run's candidates under edited rules.

    Editing a threshold does not change what Jev was asked, so the answers from
    the last run still stand and the result is immediate and free. Adding,
    removing or rewording a rule does change the questions, so those are asked
    again -- against the cached transcripts rather than fresh downloads.
    """
    candidates = saved_run.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    previous_config = saved_run.get("jev")
    previous_config = previous_config if isinstance(previous_config, dict) else {}

    judgeable = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and isinstance(candidate.get("video"), dict)
        and isinstance(candidate.get("answers"), dict)
        and candidate["answers"]
        # Trusted creators bypass the rules entirely, so no edit can move them.
        and not (candidate.get("decision") or {}).get("trusted")
    ]
    if not judgeable:
        raise LiveTestUnavailableError(
            "There is nothing to compare against yet. Refresh the feed first."
        )

    answers_by_id: dict[str, dict[str, Any]] = {}
    input_tokens = 0
    output_tokens = 0
    model: str | None = None
    with_transcript = 0
    reused = questions_unchanged(previous_config, proposed_config)

    if reused:
        for candidate in judgeable:
            answers_by_id[candidate["video"]["video_id"]] = candidate["answers"]
    else:
        try:
            curator = _curator_from_environment()
        except ProviderConfigurationError as error:
            raise LiveTestUnavailableError(str(error)) from error

        prepared: list[Video] = []
        for index, candidate in enumerate(judgeable, start=1):
            video = dict(candidate["video"])
            try:
                context = fetch_video_context(video["video_id"])
            except ProviderRequestError:
                context = None
            if context is not None:
                video = apply_transcript_policy(video, context, proposed_config)
            if video.get("transcript"):
                with_transcript += 1
            prepared.append(video)
            if progress is not None:
                progress(
                    "Re-reading transcripts",
                    f"{index} of {len(judgeable)} · {str(video.get('title'))[:48]}",
                    index,
                    len(judgeable),
                )

        result = curator.curate(prepared, proposed_config, progress)
        for record in result.records:
            answers_by_id[record.video["video_id"]] = record.answers
            input_tokens += record.input_tokens
            output_tokens += record.output_tokens
            model = model or record.model

    changes: list[dict[str, Any]] = []
    gained = 0
    lost = 0
    unchanged = 0
    for candidate in judgeable:
        video = candidate["video"]
        answers = answers_by_id.get(video["video_id"]) or {}
        decision = decide(answers, proposed_config)
        was_approved = (candidate.get("decision") or {}).get("approved")
        flipped = was_approved is not decision["approved"]
        if flipped and decision["approved"]:
            gained += 1
        elif flipped:
            lost += 1
        else:
            unchanged += 1
        changes.append(
            {
                "video_id": video["video_id"],
                "title": video.get("title") or "",
                "channel_name": video.get("channel_name") or "",
                "was_approved": was_approved,
                "now_approved": decision["approved"],
                "flipped": flipped,
                "summary": decision["summary"],
                "checks": decision["checks"],
                "used_transcript": bool(candidate.get("used_transcript")),
            }
        )

    # Flips first: those are the edits the user is actually looking for.
    changes.sort(key=lambda item: (not item["flipped"], not item["now_approved"]))

    notes: list[str] = []
    if gained:
        notes.append(f"+{gained} would pass")
    if lost:
        notes.append(f"-{lost} would drop out")
    if not notes:
        notes.append("no verdicts would change")
    if not reused:
        notes.append("re-asked Jev")
    headline = " · ".join(notes)

    return {
        "mode": "reused" if reused else "reasked",
        "headline": headline,
        "previewed": len(judgeable),
        "gained": gained,
        "lost": lost,
        "unchanged": unchanged,
        "with_transcript": with_transcript,
        "changes": changes,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": model,
            "estimated_cost_usd": round(
                input_tokens / 1_000_000 * JEV_INPUT_USD_PER_MTOK, 6
            ),
        },
    }


def _drop_watched(
    candidates: list[Video], skip_video_ids: set[str] | None
) -> tuple[list[Video], int]:
    """Removes videos already watched here, reporting how many were dropped."""
    if not skip_video_ids:
        return candidates, 0
    kept = [
        candidate
        for candidate in candidates
        if candidate["video_id"] not in skip_video_ids
    ]
    return kept, len(candidates) - len(kept)


def _build_outcome(
    *,
    trusted: list[Video],
    enriched: list[Video],
    result: CurateResult,
    started_at: float,
    skipped_watched: int = 0,
) -> RunOutcome:
    """Assembles the run record: approved videos plus the full audit trail."""
    records = list(result.records)
    # Trusted creators never reach Jev, so they carry no answers of their own.
    for video in trusted:
        records.append(
            CandidateRecord(
                video=video,
                decision={
                    "approved": True,
                    "checks": [],
                    "rating": None,
                    "summary": video.get("curation_reason") or TRUSTED_CREATOR_REASON,
                    "unanswered": [],
                    "trusted": True,
                },
                answers={},
            )
        )

    judged = [record for record in result.records if record.decision is not None]
    unjudged = [record for record in result.records if record.decision is None]
    stats = RunStats(
        candidates=len(trusted) + len(enriched) + skipped_watched,
        trusted=len(trusted),
        judged=len(judged),
        unjudged=len(unjudged),
        approved=len(result.approved) + len(trusted),
        rejected=sum(1 for record in judged if not record.decision["approved"]),
        with_transcript=sum(1 for video in enriched if video.get("transcript")),
        skipped_watched=skipped_watched,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        model=result.model,
        run_seconds=time.monotonic() - started_at,
        unjudged_titles=[record.video["title"] for record in unjudged],
    )
    return RunOutcome(
        videos=trusted + result.approved,
        stats=stats,
        candidates=records,
    )


def refresh_feed(
    trusted_creators: Sequence[str] | None = None,
    candidate_limit: int = MAX_CANDIDATES,
    jev_config: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
    skip_video_ids: set[str] | None = None,
) -> RunOutcome:
    config = jev_config or DEFAULT_JEV_CONFIG
    started_at = time.monotonic()

    def report(
        step: str, detail: str, completed: int | None = None, total: int | None = None
    ) -> None:
        if progress is not None:
            progress(step, detail, completed, total)

    report("Reading your home feed", "asking YouTube for recommendations")
    try:
        curator = _curator_from_environment()
        candidates = YouTubeInnerTubeClient.from_environment().home_videos(
            limit=candidate_limit
        )
    except ProviderConfigurationError as error:
        raise LiveFeedUnavailableError(str(error)) from error

    # Videos already watched here are dropped before they cost a transcript fetch
    # or a Jev call: a refresh exists to offer something new.
    candidates, skipped_watched = _drop_watched(candidates, skip_video_ids)
    if skipped_watched:
        report(
            "Skipping what you've already watched",
            f"{skipped_watched} skipped",
            skipped_watched,
            skipped_watched + len(candidates),
        )
    trusted, remaining = _partition_trusted_candidates(candidates, trusted_creators or [])
    if trusted:
        report(
            "Keeping trusted creators",
            f"{len(trusted)} skipped the filter",
            len(trusted),
            len(candidates),
        )
    report("Fetching transcripts", f"0 of {len(remaining)}", 0, len(remaining))
    enriched = _enrich_with_transcripts(remaining, config, progress)
    report("Asking Jev", f"0 of {len(enriched)}", 0, len(enriched))
    result = curator.curate(enriched, config, progress)
    return _build_outcome(
        trusted=trusted,
        enriched=enriched,
        result=result,
        started_at=started_at,
        skipped_watched=skipped_watched,
    )


def search_and_filter_videos(
    query: str,
    trusted_creators: Sequence[str] | None = None,
    candidate_limit: int = MAX_CANDIDATES,
    jev_config: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> RunOutcome:
    config = jev_config or DEFAULT_JEV_CONFIG
    started_at = time.monotonic()

    def report(
        step: str, detail: str, completed: int | None = None, total: int | None = None
    ) -> None:
        if progress is not None:
            progress(step, detail, completed, total)

    query = query.strip()
    if not query:
        raise LiveSearchUnavailableError("Enter a search topic.")

    report("Searching YouTube", f"looking for “{query}”")
    try:
        curator = _curator_from_environment()
        candidates = YouTubeInnerTubeClient.from_environment().search_videos(
            query, limit=candidate_limit
        )
    except ProviderConfigurationError as error:
        raise LiveSearchUnavailableError(str(error)) from error

    trusted, remaining = _partition_trusted_candidates(candidates, trusted_creators or [])
    if trusted:
        report(
            "Keeping trusted creators",
            f"{len(trusted)} skipped the filter",
            len(trusted),
            len(candidates),
        )
    report("Fetching transcripts", f"0 of {len(remaining)}", 0, len(remaining))
    enriched = _enrich_with_transcripts(remaining, config, progress)
    report("Asking Jev", f"0 of {len(enriched)}", 0, len(enriched))
    result = curator.curate(enriched, config, progress)
    return _build_outcome(
        trusted=trusted, enriched=enriched, result=result, started_at=started_at
    )


def evaluate_video(
    url_or_id: str, jev_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Fetches one video and reports how the configured rules judge it.

    This never touches the persisted feed; it exists purely so a user can see
    exactly why a specific video would or would not be approved.
    """
    config = jev_config or DEFAULT_JEV_CONFIG
    video_id = parse_video_id(url_or_id)
    if video_id is None:
        raise LiveTestUnavailableError(
            "Enter a valid YouTube video URL (or its 11-character video ID)."
        )

    try:
        curator = _curator_from_environment()
        client = YouTubeInnerTubeClient.from_environment()
    except ProviderConfigurationError as error:
        raise LiveTestUnavailableError(str(error)) from error

    video = client.video_details(video_id)
    try:
        context = fetch_video_context(video_id)
        video = apply_transcript_policy(video, context, config)
    except ProviderRequestError:
        pass

    transcript = video.get("transcript") or ""
    decision, reply = curator.judge(video, config)
    return {
        "video": {
            "video_id": video["video_id"],
            "title": video["title"],
            "channel_name": video["channel_name"],
            "transcript_tokens": _estimate_tokens(transcript),
            "used_transcript": bool(transcript),
        },
        "decision": decision,
        "usage": {
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "model": reply.model or curator.model,
            "estimated_cost_usd": round(
                reply.input_tokens / 1_000_000 * JEV_INPUT_USD_PER_MTOK, 6
            ),
        },
    }