import json
import os
import re
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

import fetcher
from fetcher import (
    LiveFeedUnavailableError,
    LiveSearchUnavailableError,
    LiveTestUnavailableError,
    ProviderRequestError,
    evaluate_video,
    refresh_feed as fetch_latest_feed,
    search_and_filter_videos,
)


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_CREATOR_NAME_LENGTH = 200
MIN_CANDIDATE_LIMIT = 1
MAX_CANDIDATE_LIMIT = 50
MAX_CURATION_PROMPT_LENGTH = 20_000
DEFAULT_SETTINGS = {
    "refresh_candidate_limit": fetcher.MAX_CANDIDATES,
    "search_candidate_limit": fetcher.MAX_CANDIDATES,
    "curation_prompt": fetcher.CURATION_SYSTEM_PROMPT,
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


def read_settings(path: Path) -> dict[str, int | str]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = {}

    settings = dict(DEFAULT_SETTINGS)
    if isinstance(value, dict):
        for key in ("refresh_candidate_limit", "search_candidate_limit"):
            if isinstance(value.get(key), int) and not isinstance(value.get(key), bool):
                settings[key] = value[key]
        prompt = value.get("curation_prompt")
        if isinstance(prompt, str) and prompt.strip():
            settings["curation_prompt"] = prompt
    return settings


def write_video_list(path: Path, videos: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary_file:
        json.dump(videos, temporary_file, indent=2)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)

    os.replace(temporary_path, path)


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


def normalize_settings(
    payload: object, current: dict[str, int | str]
) -> tuple[dict[str, int | str] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    updated = dict(current)
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

    if "curation_prompt" in payload:
        prompt = payload["curation_prompt"]
        if not isinstance(prompt, str):
            return None, "curation_prompt must be text."
        prompt = prompt.strip()
        if not prompt:
            return None, "curation_prompt cannot be empty."
        if len(prompt) > MAX_CURATION_PROMPT_LENGTH:
            return None, (
                f"curation_prompt must be {MAX_CURATION_PROMPT_LENGTH} characters or fewer."
            )
        updated["curation_prompt"] = prompt

    return updated, None


def settings_response(settings: dict[str, int | str]) -> dict[str, int | str]:
    return {**settings, "default_curation_prompt": fetcher.CURATION_SYSTEM_PROMPT}


def create_app(config: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__)
    app.config.from_mapping(DATA_DIR=Path("./data"))
    if config:
        app.config.update(config)

    def data_path(filename: str) -> Path:
        return Path(app.config["DATA_DIR"]) / filename

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/feed")
    def get_feed():
        return jsonify(read_video_list(data_path("feed.json")))

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
            result = evaluate_video(url, settings["curation_prompt"])
        except LiveTestUnavailableError as error:
            return jsonify({"error": "video_test_not_configured", "message": str(error)}), 501
        except ProviderRequestError as error:
            return jsonify({"error": "video_test_failed", "message": str(error)}), 502

        return jsonify(result)

    @app.post("/api/refresh")
    def refresh_feed():
        trusted_creators = read_string_list(data_path("trusted_creators.json"))
        settings = read_settings(data_path("settings.json"))
        try:
            videos = fetch_latest_feed(
                trusted_creators,
                settings["refresh_candidate_limit"],
                settings["curation_prompt"],
            )
        except LiveFeedUnavailableError as error:
            return jsonify({"error": "feed_refresh_not_configured", "message": str(error)}), 501
        except ProviderRequestError as error:
            return jsonify({"error": "feed_refresh_failed", "message": str(error)}), 502

        # The Current feed is a snapshot of the most recent results rather than
        # an accumulating list, so a refresh replaces it outright.
        write_video_list(data_path("feed.json"), list(videos))
        return jsonify({"refreshed": len(videos)})

    @app.post("/api/search")
    def search_feed():
        query, error = normalize_search_query(request.get_json(silent=True))
        if error:
            return jsonify({"error": "invalid_search_query", "message": error}), 400

        trusted_creators = read_string_list(data_path("trusted_creators.json"))
        settings = read_settings(data_path("settings.json"))
        try:
            videos = search_and_filter_videos(
                query,
                trusted_creators,
                settings["search_candidate_limit"],
                settings["curation_prompt"],
            )
        except LiveSearchUnavailableError as error:
            return jsonify({"error": "search_not_configured", "message": str(error)}), 501
        except ProviderRequestError as error:
            return jsonify({"error": "search_failed", "message": str(error)}), 502

        # Search results replace the Current feed for the same reason.
        write_video_list(data_path("feed.json"), list(videos))
        return jsonify({"query": query, "approved": len(videos)})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)