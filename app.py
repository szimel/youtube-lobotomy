import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Collection
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

import fetcher
from fetcher import (
    LiveFeedUnavailableError,
    LiveSearchUnavailableError,
    LiveTestUnavailableError,
    LiveWatchLogUnavailableError,
    ProviderRequestError,
    evaluate_video,
    fetch_watch_history,
    refresh_feed as fetch_latest_feed,
    search_and_filter_videos,
)


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_CREATOR_NAME_LENGTH = 200
MIN_CANDIDATE_LIMIT = 1
MAX_CANDIDATE_LIMIT = fetcher.MAX_CANDIDATE_LIMIT
MAX_WATCH_LOG_IDS = 50
# A watch is only interesting once it is a real watch; the page uses the same
# threshold before it reports one, and YouTube's own history uses a similar one.
WATCHED_MIN_SECONDS = 30.0
MAX_WATCH_SECONDS = 86_400.0
MAX_WATCHED_ENTRIES = 500
MIN_SAVED_FOR_TRUST = 3
MIN_WATCHED_FOR_TRUST = 6
MAX_TRUST_SUGGESTIONS = 5
MAX_RUN_HISTORY_ENTRIES = 50
MAX_RUN_HISTORY_PREVIEW = 10
WATCH_HISTORY_CACHE_SECONDS = 45.0
MAX_JEV_RULES = 20
MAX_JEV_NAME_LENGTH = 80
MAX_JEV_INSTRUCTION_LENGTH = 1_000
MAX_JEV_PROFILE_LENGTH = 2_000
MAX_JEV_LEVEL_LENGTH = 300
MAX_TRANSCRIPT_TOKENS = fetcher.JEV_TRANSCRIPT_TOKEN_CEILING
DEFAULT_SETTINGS = {
    "refresh_candidate_limit": fetcher.MAX_CANDIDATES,
    "search_candidate_limit": fetcher.MAX_CANDIDATES,
    "jev": copy.deepcopy(fetcher.DEFAULT_JEV_CONFIG),
}


def read_video_list(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    return value if isinstance(value, list) else []


def read_string_list(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalize_jev_rule(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "each rule must be an object."

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "every rule needs a name."
    name = name.strip()
    if len(name) > MAX_JEV_NAME_LENGTH:
        return None, f"rule names must be {MAX_JEV_NAME_LENGTH} characters or fewer."

    instruction = payload.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return None, f"the rule '{name}' needs a question for Jev to answer."
    instruction = " ".join(instruction.split())
    if len(instruction) > MAX_JEV_INSTRUCTION_LENGTH:
        return None, (
            f"the question for '{name}' must be "
            f"{MAX_JEV_INSTRUCTION_LENGTH} characters or fewer."
        )

    threshold = payload.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return None, f"the threshold for '{name}' must be a number between 0 and 1."
    threshold = float(threshold)
    if not (0.0 <= threshold <= 1.0):
        return None, f"the threshold for '{name}' must be between 0 and 1."

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, f"the enabled flag for '{name}' must be true or false."

    return (
        {
            "name": name,
            "instruction": instruction,
            "threshold": round(threshold, 2),
            "enabled": enabled,
        },
        None,
    )


def _normalize_jev_rating(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "the rating must be an object."

    instruction = payload.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return None, "the rating needs a question for Jev to answer."
    instruction = " ".join(instruction.split())
    if len(instruction) > MAX_JEV_INSTRUCTION_LENGTH:
        return None, (
            f"the rating question must be {MAX_JEV_INSTRUCTION_LENGTH} characters or fewer."
        )

    criteria = payload.get("criteria")
    if not isinstance(criteria, list):
        return None, "the rating needs a list of levels."
    levels = [str(level).strip() for level in criteria if str(level).strip()]
    if not (2 <= len(levels) <= fetcher.JEV_MAX_CRITERIA):
        return None, (
            f"the rating needs between 2 and {fetcher.JEV_MAX_CRITERIA} levels, "
            "lowest first."
        )
    for level in levels:
        if len(level) > MAX_JEV_LEVEL_LENGTH:
            return None, (
                f"each rating level must be {MAX_JEV_LEVEL_LENGTH} characters or fewer."
            )

    minimum = payload.get("minimum")
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
        return None, "the minimum rating must be a number."
    minimum = float(minimum)
    if not (0.0 <= minimum <= len(levels) - 1):
        return None, (
            f"the minimum rating must be between 0 and {len(levels) - 1} "
            f"for {len(levels)} levels."
        )

    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        return None, "the rating enabled flag must be true or false."

    return (
        {
            "enabled": enabled,
            "instruction": instruction,
            "criteria": levels,
            "minimum": minimum,
        },
        None,
    )


def normalize_jev_config(payload: object, current: dict) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "jev must be an object."

    updated = copy.deepcopy(current)

    if "profile" in payload:
        profile = payload["profile"]
        if not isinstance(profile, str):
            return None, "profile must be text."
        profile = profile.strip()
        if len(profile) > MAX_JEV_PROFILE_LENGTH:
            return None, (
                f"profile must be {MAX_JEV_PROFILE_LENGTH} characters or fewer."
            )
        updated["profile"] = profile

    for key in ("positives", "disqualifiers"):
        if key not in payload:
            continue
        rules = payload[key]
        if not isinstance(rules, list):
            return None, f"{key} must be a list of rules."
        if len(rules) > MAX_JEV_RULES:
            return None, f"{key} supports at most {MAX_JEV_RULES} rules."
        cleaned: list[dict] = []
        for rule in rules:
            parsed, error = _normalize_jev_rule(rule)
            if error:
                return None, f"{key}: {error}"
            cleaned.append(parsed)
        updated[key] = cleaned

    if "rating" in payload:
        rating, error = _normalize_jev_rating(payload["rating"])
        if error:
            return None, f"rating: {error}"
        updated["rating"] = rating

    for key in ("transcript_min_tokens", "transcript_max_tokens"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"{key} must be a whole number."
        if not (0 <= value <= MAX_TRANSCRIPT_TOKENS):
            return None, f"{key} must be between 0 and {MAX_TRANSCRIPT_TOKENS}."
        updated[key] = value

    if updated["transcript_max_tokens"] < updated["transcript_min_tokens"]:
        return None, "the transcript maximum must be at least the minimum."

    return updated, None


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def origin_host(value: str) -> str:
    """The "host:port" of an origin, a bare hostname, or "" if it is unusable."""
    value = value.strip().rstrip("/")
    if not value:
        return ""
    return urlsplit(value if "//" in value else f"//{value}").netloc


def allowed_write_hosts(host: str, headers=None) -> set[str]:
    """The hosts whose pages may change this app's state.

    The page's own host is always allowed. A reverse proxy in front of the app
    (Caddy with a rewritten Host, or `tailscale serve`) reports the address the
    browser actually used in X-Forwarded-Host, and comparing only against the
    proxied Host would make every button return 403. Page script cannot forge
    that header: anything outside the CORS-safelisted set forces a preflight,
    and this app answers no preflights.

    PUBLIC_ORIGINS covers a proxy that forwards neither: the public address can
    be declared outright, as PUBLIC_ORIGINS=https://feed.example.com
    """
    hosts = {host}
    if headers is not None:
        forwarded = headers.get("X-Forwarded-Host", "")
        hosts.update(filter(None, map(origin_host, forwarded.split(","))))
    hosts.update(
        filter(None, map(origin_host, os.environ.get("PUBLIC_ORIGINS", "").split(",")))
    )
    return hosts


def is_cross_site_write(method: str, headers, hosts: Collection[str]) -> bool:
    """True when a state-changing request came from somewhere other than this app.

    Every route here is unauthenticated because the app is meant to be reachable
    only from this machine. That still leaves a hole: a page on any website the
    user happens to visit can POST to http://127.0.0.1:5000, and a bodyless POST
    is a "simple request" that browsers send without a preflight. Refreshing the
    feed costs money and replaces the feed, so those writes are refused.
    """
    if method in SAFE_METHODS:
        return False

    # Chrome, Edge and Firefox all send this; it cannot be set by page script.
    if headers.get("Sec-Fetch-Site") == "cross-site":
        return True

    origin = headers.get("Origin")
    if origin:
        origin_host = urlsplit(origin).netloc
        if origin_host and origin_host not in hosts:
            return True

    return False


def read_settings(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = {}

    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if isinstance(value, dict):
        for key in ("refresh_candidate_limit", "search_candidate_limit"):
            stored = value.get(key)
            if isinstance(stored, bool) or not isinstance(stored, int):
                continue
            # A limit saved before the cap changed must not be served back as an
            # unusable value the UI would then refuse to re-save.
            if MIN_CANDIDATE_LIMIT <= stored <= MAX_CANDIDATE_LIMIT:
                settings[key] = stored
        stored_jev = value.get("jev")
        if isinstance(stored_jev, dict):
            # A transcript length saved while the ceiling was higher is clamped
            # rather than rejected: discarding it would silently throw away every
            # rule the user had written.
            stored_jev = dict(stored_jev)
            for key in ("transcript_min_tokens", "transcript_max_tokens"):
                stored_value = stored_jev.get(key)
                if isinstance(stored_value, int) and not isinstance(stored_value, bool):
                    stored_jev[key] = max(0, min(stored_value, MAX_TRANSCRIPT_TOKENS))
            merged, error = normalize_jev_config(stored_jev, settings["jev"])
            if error is None and merged is not None:
                settings["jev"] = merged
    return settings


def write_video_list(path: Path, value: list[dict] | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary_file:
        json.dump(value, temporary_file, indent=2)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)

    os.replace(temporary_path, path)


def normalize_watch_entries(payload: object) -> tuple[list[dict] | None, str | None]:
    """Watches reported by the page, as one batch or a single entry."""
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    entries = payload.get("entries")
    if entries is None and "video_id" in payload:
        entries = [payload]
    if not isinstance(entries, list) or not entries:
        return None, "entries must be a non-empty list."

    if len(entries) > MAX_WATCH_LOG_IDS:
        return None, f"entries must contain {MAX_WATCH_LOG_IDS} or fewer watches."

    cleaned: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None, "every watch must be an object."

        video_id = entry.get("video_id")
        if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.fullmatch(video_id):
            return None, "every watch needs an 11-character YouTube video ID."

        seconds = entry.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None, "every watch needs a number of seconds."
        seconds = float(seconds)
        if not (0 <= seconds <= MAX_WATCH_SECONDS):
            return None, f"seconds must be between 0 and {MAX_WATCH_SECONDS}."

        record = {"video_id": video_id, "seconds": round(seconds, 1)}
        for field in ("title", "channel_name"):
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                record[field] = value.strip()[:MAX_CREATOR_NAME_LENGTH]
        cleaned.append(record)

    return cleaned, None


def merge_watch_log(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merges reported watches, keeping the longest time seen for each video."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for record in list(existing) + list(incoming):
        if not isinstance(record, dict):
            continue
        video_id = record.get("video_id")
        if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.fullmatch(video_id):
            continue
        if video_id not in merged:
            merged[video_id] = {
                "video_id": video_id,
                "seconds": 0.0,
                "title": "",
                "channel_name": "",
                "watched_at": 0.0,
            }
            order.append(video_id)
        current = merged[video_id]
        seconds = record.get("seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            current["seconds"] = round(max(current["seconds"], float(seconds)), 1)
        for field in ("title", "channel_name"):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                current[field] = value.strip()
        watched_at = record.get("watched_at")
        if isinstance(watched_at, (int, float)) and not isinstance(watched_at, bool):
            current["watched_at"] = float(watched_at)

    now = time.time()
    for video_id in order:
        if not merged[video_id]["watched_at"]:
            merged[video_id]["watched_at"] = now

    # Newest first, so trimming keeps what the user most recently watched.
    records = sorted(
        merged.values(), key=lambda item: item["watched_at"], reverse=True
    )
    return records[:MAX_WATCHED_ENTRIES]


def watched_video_ids(records: list[dict]) -> set[str]:
    """Only a real watch counts, not a glance at the first few seconds."""
    return {
        record["video_id"]
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("video_id"), str)
        and isinstance(record.get("seconds"), (int, float))
        and not isinstance(record.get("seconds"), bool)
        and record["seconds"] >= WATCHED_MIN_SECONDS
    }


def trust_suggestions(
    watch_later: list[dict], watched: list[dict], trusted: list[str]
) -> list[dict]:
    """Channels the user keeps choosing, which they may want to trust outright."""
    saved: dict[str, int] = {}
    seen: dict[str, int] = {}
    for video in watch_later:
        name = str(video.get("channel_name") or "").strip()
        if name:
            saved[name] = saved.get(name, 0) + 1
    for record in watched:
        name = str(record.get("channel_name") or "").strip()
        if name:
            seen[name] = seen.get(name, 0) + 1

    trusted_names = {name.casefold() for name in trusted}
    suggestions: list[dict] = []
    for name in set(saved) | set(seen):
        if name.casefold() in trusted_names:
            continue
        kept = saved.get(name, 0)
        viewed = seen.get(name, 0)
        if kept < MIN_SAVED_FOR_TRUST and viewed < MIN_WATCHED_FOR_TRUST:
            continue
        reasons: list[str] = []
        if kept:
            reasons.append(f"kept {kept} video{'' if kept == 1 else 's'}")
        if viewed:
            reasons.append(f"watched {viewed}")
        suggestions.append(
            {"name": name, "saved": kept, "watched": viewed, "reason": " · ".join(reasons)}
        )

    suggestions.sort(key=lambda item: (-item["saved"], -item["watched"], item["name"]))
    return suggestions[:MAX_TRUST_SUGGESTIONS]


def append_run_history(path: Path, kind: str, outcome, query: str | None = None) -> None:
    """Keeps a short history of runs: what they cost and how much they could see."""
    try:
        with path.open("r", encoding="utf-8") as file:
            history = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        history = []
    if not isinstance(history, list):
        history = []

    stats = outcome.stats
    history.append(
        {
            "kind": kind,
            "query": query,
            "ran_at": time.time(),
            "candidates": stats.candidates,
            "approved": stats.approved,
            "unjudged": stats.unjudged,
            "with_transcript": stats.with_transcript,
            "skipped_watched": stats.skipped_watched,
            "input_tokens": stats.input_tokens,
            "estimated_cost_usd": stats.estimated_cost_usd,
            "run_seconds": round(stats.run_seconds, 1),
            "model": stats.model,
        }
    )
    try:
        write_video_list(path, history[-MAX_RUN_HISTORY_ENTRIES:])
    except OSError:
        # History is a convenience; it must never fail a good run.
        pass


def run_summary(outcome, base: str) -> str:
    """Appends what the run could not do to an otherwise rosy one-liner.

    A refresh that silently failed to judge half its candidates looks exactly
    like a refresh with strict rules, so the difference is stated out loud.
    """
    notes: list[str] = []
    stats = outcome.stats
    if stats.unjudged:
        plural = "" if stats.unjudged == 1 else "s"
        notes.append(f"{stats.unjudged} video{plural} could not be judged")
    if stats.skipped_watched:
        notes.append(f"{stats.skipped_watched} already watched")
    if not outcome.videos and stats.candidates:
        if stats.judged:
            notes.append("nothing passed the rules")
        elif stats.skipped_watched:
            notes.append("nothing new — you have watched it all")
    return base + (" · " + " · ".join(notes) if notes else "")


def write_last_run(
    path: Path, kind: str, outcome, settings: dict, query: str | None = None
) -> None:
    """Records the whole run so it can be inspected and re-decided later.

    Each candidate keeps the raw answers Jev gave, which is what makes editing a
    threshold a local re-decision rather than another paid round trip.
    """
    payload = {
        "kind": kind,
        "query": query,
        "ran_at": time.time(),
        "stats": outcome.stats.as_dict(),
        "jev": copy.deepcopy(settings["jev"]),
        "candidates": [record.as_dict() for record in outcome.candidates],
    }
    try:
        write_video_list(path, payload)
    except OSError:
        # The audit trail is a convenience; it must never fail a good run.
        pass


def normalize_video(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    video_id = payload.get("video_id")
    title = payload.get("title")
    if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.fullmatch(video_id):
        return None, "video_id must be an 11-character YouTube video ID."
    if not isinstance(title, str) or not title.strip():
        return None, "title is required."

    video = {
        "video_id": video_id,
        "title": title.strip(),
        "channel_name": "",
        "description": "",
    }
    for field in (
        "channel_name",
        "description",
        "thumbnail_url",
        "published_at",
        "curation_reason",
    ):
        value = payload.get(field)
        if isinstance(value, str):
            video[field] = value.strip()

    duration = payload.get("duration_seconds")
    if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
        video["duration_seconds"] = duration

    return video, None


def normalize_search_query(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    query = payload.get("query")
    if not isinstance(query, str):
        return None, "query is required."

    query = query.strip()
    if not query:
        return None, "query is required."
    if len(query) > 200:
        return None, "query must be 200 characters or fewer."

    return query, None


def normalize_creator_name(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    name = payload.get("name")
    if not isinstance(name, str):
        return None, "name is required."

    name = name.strip()
    if not name:
        return None, "name is required."
    if len(name) > MAX_CREATOR_NAME_LENGTH:
        return None, f"name must be {MAX_CREATOR_NAME_LENGTH} characters or fewer."

    return name, None


def normalize_video_url(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        return None, "url is required."

    return url.strip(), None


def normalize_video_ids(payload: object) -> tuple[list[str] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    video_ids = payload.get("video_ids")
    if not isinstance(video_ids, list) or not video_ids:
        return None, "video_ids must be a non-empty list."
    if len(video_ids) > MAX_WATCH_LOG_IDS:
        return None, f"video_ids must contain {MAX_WATCH_LOG_IDS} or fewer IDs."

    for video_id in video_ids:
        if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.fullmatch(video_id):
            return None, "video_ids must be 11-character YouTube video IDs."

    return list(dict.fromkeys(video_ids)), None


def normalize_settings(
    payload: object, current: dict
) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    updated = copy.deepcopy(current)
    for key in ("refresh_candidate_limit", "search_candidate_limit"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"{key} must be a whole number."
        if not (MIN_CANDIDATE_LIMIT <= value <= MAX_CANDIDATE_LIMIT):
            return None, (
                f"{key} must be between {MIN_CANDIDATE_LIMIT} and {MAX_CANDIDATE_LIMIT}."
            )
        updated[key] = value

    if "jev" in payload:
        jev, error = normalize_jev_config(payload["jev"], updated["jev"])
        if error:
            return None, error
        updated["jev"] = jev

    return updated, None


def settings_response(settings: dict) -> dict:
    return {
        **settings,
        "default_jev": copy.deepcopy(fetcher.DEFAULT_JEV_CONFIG),
        "max_candidate_limit": MAX_CANDIDATE_LIMIT,
        "max_jev_rules": MAX_JEV_RULES,
        "max_transcript_tokens": MAX_TRANSCRIPT_TOKENS,
    }


def create_app(config: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__)
    # Static files are re-read from disk on every request, but templates are
    # cached unless this is on -- which makes editing the page look like it did
    # nothing until the server is restarted.
    app.config.from_mapping(DATA_DIR=Path("./data"), TEMPLATES_AUTO_RELOAD=True)
    if config:
        app.config.update(config)

    def data_path(filename: str) -> Path:
        return Path(app.config["DATA_DIR"]) / filename

    def read_last_run() -> dict | None:
        try:
            with data_path("last_run.json").open("r", encoding="utf-8") as file:
                value = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    # The transcript cache lives beside the data it belongs to, so a test or an
    # alternative DATA_DIR never reads or writes the real one.
    fetcher.set_data_directory(app.config["DATA_DIR"])

    # Reading the account's history costs a ~1.5s network round trip, and the
    # browser may verify several videos at once, so reuse a recent read.
    watch_history_cache = {"videos": [], "fetched_at": 0.0}

    def cached_watch_history() -> list[dict]:
        now = time.monotonic()
        if now - watch_history_cache["fetched_at"] >= WATCH_HISTORY_CACHE_SECONDS:
            watch_history_cache["videos"] = fetch_watch_history()
            watch_history_cache["fetched_at"] = now
        return watch_history_cache["videos"]

    # Refreshing and searching now take tens of seconds (transcripts have to be
    # fetched before Jev sees anything), which is far too long for the browser to
    # sit on a silent request. They run in a worker thread instead, and the page
    # polls /api/progress so it can say what is happening.
    job_lock = threading.Lock()
    job_state: dict = {"job": None}

    def update_job(job_id: str, **changes) -> None:
        with job_lock:
            job = job_state["job"]
            if job is not None and job["id"] == job_id:
                job.update(changes)

    def current_job() -> dict | None:
        with job_lock:
            job = job_state["job"]
            return dict(job) if job else None

    def fail_job(job_id: str, code: str, message: str) -> None:
        update_job(
            job_id,
            state="error",
            step="Failed",
            detail=message,
            error={"code": code, "message": message},
            finished_at=time.time(),
        )

    def make_progress(job_id: str):
        def progress(
            step: str, detail: str, completed: int | None = None, total: int | None = None
        ) -> None:
            changes: dict = {"step": step, "detail": detail}
            # Counts are only touched when the caller actually knows them, so a
            # later step cannot wipe out the progress the previous one reported.
            if completed is not None:
                changes["completed"] = completed
            if total is not None:
                changes["total"] = total
            update_job(job_id, **changes)

        return progress

    def start_job(kind: str, step: str, detail: str, runner) -> dict | None:
        with job_lock:
            existing = job_state["job"]
            if existing is not None and existing["state"] == "running":
                return None
            job = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "state": "running",
                "step": step,
                "detail": detail,
                "completed": 0,
                "total": 0,
                "started_at": time.time(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            job_state["job"] = job
            snapshot = dict(job)

        threading.Thread(target=runner, args=(job["id"],), daemon=True).start()
        return snapshot

    def run_refresh(job_id: str) -> None:
        progress = make_progress(job_id)
        try:
            trusted_creators = read_string_list(data_path("trusted_creators.json"))
            settings = read_settings(data_path("settings.json"))
            # A refresh is meant to offer something new, so videos already
            # watched here are dropped before they cost a transcript or a Jev
            # call. A search is explicit, so it still shows them.
            already_watched = watched_video_ids(
                read_video_list(data_path("watched.json"))
            )
            outcome = fetch_latest_feed(
                trusted_creators,
                settings["refresh_candidate_limit"],
                settings["jev"],
                progress,
                skip_video_ids=already_watched,
            )
            videos = outcome.videos
            progress("Saving the feed", f"{len(videos)} approved")
            write_video_list(data_path("feed.json"), list(videos))
            write_last_run(data_path("last_run.json"), "refresh", outcome, settings)
            append_run_history(data_path("runs.json"), "refresh", outcome)
            plural = "" if len(videos) == 1 else "s"
            update_job(
                job_id,
                state="done",
                step="Done",
                detail=run_summary(outcome, f"Feed replaced with {len(videos)} video{plural}"),
                result={"refreshed": len(videos), "stats": outcome.stats.as_dict()},
                finished_at=time.time(),
            )
        except LiveFeedUnavailableError as error:
            fail_job(job_id, "feed_refresh_not_configured", str(error))
        except ProviderRequestError as error:
            fail_job(job_id, "feed_refresh_failed", str(error))
        except Exception as error:  # surfaced to the browser rather than swallowed
            fail_job(job_id, "feed_refresh_failed", f"{type(error).__name__}: {error}")

    def run_rule_preview(job_id: str, proposed: dict) -> None:
        progress = make_progress(job_id)
        try:
            preview = fetcher.preview_rules(
                proposed, read_last_run() or {}, progress
            )
            update_job(
                job_id,
                state="done",
                step="Done",
                detail=preview["headline"],
                result={"preview": preview},
                finished_at=time.time(),
            )
        except LiveTestUnavailableError as error:
            fail_job(job_id, "preview_unavailable", str(error))
        except ProviderRequestError as error:
            fail_job(job_id, "preview_failed", str(error))
        except Exception as error:  # surfaced to the browser rather than swallowed
            fail_job(job_id, "preview_failed", f"{type(error).__name__}: {error}")

    def run_search(job_id: str, query: str) -> None:
        progress = make_progress(job_id)
        try:
            trusted_creators = read_string_list(data_path("trusted_creators.json"))
            settings = read_settings(data_path("settings.json"))
            outcome = search_and_filter_videos(
                query,
                trusted_creators,
                settings["search_candidate_limit"],
                settings["jev"],
                progress,
            )
            videos = outcome.videos
            progress("Saving the feed", f"{len(videos)} approved")
            write_video_list(data_path("feed.json"), list(videos))
            write_last_run(data_path("last_run.json"), "search", outcome, settings, query)
            append_run_history(data_path("runs.json"), "search", outcome, query)
            update_job(
                job_id,
                state="done",
                step="Done",
                detail=run_summary(
                    outcome,
                    f"Feed replaced with {len(videos)} video"
                    + ("" if len(videos) == 1 else "s")
                    + f" for “{query}”",
                ),
                result={
                    "query": query,
                    "approved": len(videos),
                    "stats": outcome.stats.as_dict(),
                },
                finished_at=time.time(),
            )
        except LiveSearchUnavailableError as error:
            fail_job(job_id, "search_not_configured", str(error))
        except ProviderRequestError as error:
            fail_job(job_id, "search_failed", str(error))
        except Exception as error:  # surfaced to the browser rather than swallowed
            fail_job(job_id, "search_failed", f"{type(error).__name__}: {error}")

    @app.before_request
    def block_cross_site_writes():
        hosts = allowed_write_hosts(request.host, request.headers)
        if is_cross_site_write(request.method, request.headers, hosts):
            return (
                jsonify(
                    {
                        "error": "cross_site_request",
                        "message": (
                            "This app only accepts changes made from its own page."
                        ),
                    }
                ),
                403,
            )
        return None

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        """Liveness probe for Docker, Caddy and anything else supervising this.

        Deliberately touches no data file and starts no work, so a container
        orchestrator can poll it cheaply without disturbing a running job.
        """
        return jsonify({"status": "ok"})

    @app.get("/api/feed")
    def get_feed():
        return jsonify(read_video_list(data_path("feed.json")))

    @app.get("/api/watched")
    def get_watched():
        return jsonify(read_video_list(data_path("watched.json")))

    @app.post("/api/watched")
    def record_watched():
        """Records what the page actually played, so the feed can remember it."""
        entries, error = normalize_watch_entries(request.get_json(silent=True))
        if error:
            return jsonify({"error": "invalid_watch", "message": error}), 400

        path = data_path("watched.json")
        merged = merge_watch_log(read_video_list(path), entries)
        write_video_list(path, merged)
        return jsonify({"recorded": len(entries), "watched": len(merged)}), 201

    @app.delete("/api/watched/<video_id>")
    def forget_watched(video_id: str):
        """Forgets one watch, so the video can reach the feed again."""
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            return jsonify({"error": "invalid_video_id"}), 400

        path = data_path("watched.json")
        records = read_video_list(path)
        remaining = [
            record for record in records if record.get("video_id") != video_id
        ]
        removed = len(remaining) != len(records)
        if removed:
            write_video_list(path, remaining)

        return jsonify({"removed": removed})

    @app.get("/api/trust-suggestions")
    def get_trust_suggestions():
        return jsonify(
            trust_suggestions(
                read_video_list(data_path("watch_later.json")),
                read_video_list(data_path("watched.json")),
                read_string_list(data_path("trusted_creators.json")),
            )
        )

    @app.get("/api/last-run")
    def get_last_run():
        """The most recent refresh or search, including what it filtered out."""
        return jsonify({"run": read_last_run()})

    @app.get("/api/runs")
    def get_runs():
        """Recent runs, newest first, for a sense of cost and coverage."""
        try:
            with data_path("runs.json").open("r", encoding="utf-8") as file:
                history = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            history = []
        if not isinstance(history, list):
            history = []
        return jsonify(list(reversed(history))[:MAX_RUN_HISTORY_PREVIEW])

    @app.post("/api/rules/preview")
    def preview_rules():
        """Re-judges the last run under edited rules, before saving them."""
        path = data_path("settings.json")
        current = read_settings(path)
        proposed, error = normalize_jev_config(
            request.get_json(silent=True), current["jev"]
        )
        if error:
            return jsonify({"error": "invalid_settings", "message": error}), 400

        saved = read_last_run()
        if saved is None:
            return (
                jsonify(
                    {
                        "error": "no_run_to_preview",
                        "message": (
                            "Refresh the feed first — a preview compares edited "
                            "rules against the videos from the last run."
                        ),
                    }
                ),
                400,
            )

        previous = saved.get("jev")
        previous = previous if isinstance(previous, dict) else {}
        try:
            # A threshold or name edit asks Jev nothing new, so the answers from
            # the last run still stand and the whole preview is local and free.
            if fetcher.questions_unchanged(previous, proposed):
                return jsonify(
                    {
                        "mode": "instant",
                        "preview": fetcher.preview_rules(proposed, saved),
                    }
                )
        except LiveTestUnavailableError as error:
            return jsonify({"error": "preview_unavailable", "message": str(error)}), 400

        started = start_job(
            "preview",
            "Testing your rules",
            "re-asking Jev about the last candidates",
            lambda job_id: run_rule_preview(job_id, proposed),
        )
        if started is None:
            return (
                jsonify(
                    {
                        "error": "job_in_progress",
                        "message": "A refresh or search is already running.",
                    }
                ),
                409,
            )
        return jsonify({"job": started}), 202

    @app.get("/api/watch-later")
    def get_watch_later():
        return jsonify(read_video_list(data_path("watch_later.json")))

    @app.delete("/api/feed/<video_id>")
    def remove_feed_video(video_id: str):
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            return jsonify({"error": "invalid_video_id"}), 400

        path = data_path("feed.json")
        feed = read_video_list(path)
        updated_feed = [item for item in feed if item.get("video_id") != video_id]
        removed = len(updated_feed) != len(feed)
        if removed:
            write_video_list(path, updated_feed)

        return jsonify({"removed": removed})

    @app.post("/api/watch-later")
    def add_watch_later():
        video, error = normalize_video(request.get_json(silent=True))
        if error:
            return jsonify({"error": "invalid_video", "message": error}), 400

        path = data_path("watch_later.json")
        watch_later = read_video_list(path)
        if any(item.get("video_id") == video["video_id"] for item in watch_later):
            return jsonify({"video": video, "already_saved": True})

        watch_later.append(video)
        write_video_list(path, watch_later)
        return jsonify({"video": video, "already_saved": False}), 201

    @app.delete("/api/watch-later/<video_id>")
    def remove_watch_later(video_id: str):
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            return jsonify({"error": "invalid_video_id"}), 400

        path = data_path("watch_later.json")
        watch_later = read_video_list(path)
        updated_watch_later = [
            item for item in watch_later if item.get("video_id") != video_id
        ]
        removed = len(updated_watch_later) != len(watch_later)
        if removed:
            write_video_list(path, updated_watch_later)

        return jsonify({"removed": removed})

    @app.get("/api/trusted-creators")
    def get_trusted_creators():
        return jsonify(read_string_list(data_path("trusted_creators.json")))

    @app.post("/api/trusted-creators")
    def add_trusted_creator():
        name, error = normalize_creator_name(request.get_json(silent=True))
        if error:
            return jsonify({"error": "invalid_creator_name", "message": error}), 400

        path = data_path("trusted_creators.json")
        trusted_creators = read_string_list(path)
        if any(creator.casefold() == name.casefold() for creator in trusted_creators):
            return jsonify({"name": name, "already_trusted": True})

        trusted_creators.append(name)
        write_video_list(path, trusted_creators)
        return jsonify({"name": name, "already_trusted": False}), 201

    @app.delete("/api/trusted-creators/<path:name>")
    def remove_trusted_creator(name: str):
        path = data_path("trusted_creators.json")
        trusted_creators = read_string_list(path)
        updated_trusted_creators = [
            creator for creator in trusted_creators if creator.casefold() != name.casefold()
        ]
        removed = len(updated_trusted_creators) != len(trusted_creators)
        if removed:
            write_video_list(path, updated_trusted_creators)

        return jsonify({"removed": removed})

    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings_response(read_settings(data_path("settings.json"))))

    @app.post("/api/settings")
    def update_settings():
        path = data_path("settings.json")
        settings, error = normalize_settings(request.get_json(silent=True), read_settings(path))
        if error:
            return jsonify({"error": "invalid_settings", "message": error}), 400

        write_video_list(path, settings)
        return jsonify(settings_response(settings))

    @app.post("/api/video-test")
    def test_video():
        url, error = normalize_video_url(request.get_json(silent=True))
        if error:
            return jsonify({"error": "invalid_video_url", "message": error}), 400

        try:
            settings = read_settings(data_path("settings.json"))
            result = evaluate_video(url, settings["jev"])
        except LiveTestUnavailableError as error:
            return jsonify({"error": "video_test_not_configured", "message": str(error)}), 501
        except ProviderRequestError as error:
            return jsonify({"error": "video_test_failed", "message": str(error)}), 502

        return jsonify(result)

    @app.post("/api/refresh")
    def refresh_feed():
        started = start_job(
            "refresh",
            "Reading your home feed",
            "asking YouTube for recommendations",
            run_refresh,
        )
        if started is None:
            return (
                jsonify(
                    {
                        "error": "job_in_progress",
                        "message": "A refresh or search is already running.",
                    }
                ),
                409,
            )
        return jsonify({"job": started}), 202

    @app.get("/api/progress")
    def progress():
        return jsonify({"job": current_job()})

    @app.post("/api/search")
    def search_feed():
        query, error = normalize_search_query(request.get_json(silent=True))
        if error:
            return jsonify({"error": "invalid_search_query", "message": error}), 400

        started = start_job(
            "search",
            "Searching YouTube",
            f"looking for “{query}”",
            lambda job_id: run_search(job_id, query),
        )
        if started is None:
            return (
                jsonify(
                    {
                        "error": "job_in_progress",
                        "message": "A refresh or search is already running.",
                    }
                ),
                409,
            )
        return jsonify({"job": started}), 202

    @app.post("/api/watch-log/verify")
    def verify_watch_log():
        video_ids, error = normalize_video_ids(request.get_json(silent=True))
        if error:
            return jsonify({"error": "invalid_video_ids", "message": error}), 400

        try:
            history = cached_watch_history()
        except LiveWatchLogUnavailableError as error:
            return jsonify({"error": "watch_log_not_configured", "message": str(error)}), 501
        except ProviderRequestError as error:
            return jsonify({"error": "watch_log_failed", "message": str(error)}), 502

        history_ids = {video["video_id"] for video in history}
        return jsonify(
            {
                "checked_at": time.time(),
                "history_size": len(history_ids),
                "results": {
                    video_id: video_id in history_ids for video_id in video_ids
                },
                "recent": [
                    {
                        "video_id": video["video_id"],
                        "title": video["title"],
                        "channel_name": video["channel_name"],
                    }
                    for video in history[:5]
                ],
            }
        )

    return app


app = create_app()


if __name__ == "__main__":
    # Deliberately not debug=True: the Werkzeug debugger is a remote code
    # execution console for anything that can reach the port, and the reloader
    # would start a second process alongside the refresh worker.
    app.run(host="127.0.0.1", port=5000)