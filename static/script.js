const VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;

const elements = {
  feedList: document.querySelector("#feed-list"),
  watchLaterList: document.querySelector("#watch-later-list"),
  feedCount: document.querySelector("#feed-count"),
  watchLaterCount: document.querySelector("#watch-later-count"),
  refreshButton: document.querySelector("#refresh-feed"),
  searchForm: document.querySelector("#search-form"),
  searchInput: document.querySelector("#search-query"),
  status: document.querySelector("#status"),
  trustedCreatorsList: document.querySelector("#trusted-creators-list"),
  trustedCreatorsCount: document.querySelector("#trusted-creators-count"),
  trustedCreatorForm: document.querySelector("#trusted-creator-form"),
  trustedCreatorInput: document.querySelector("#trusted-creator-name"),
  trustedCreatorsStatus: document.querySelector("#trusted-creators-status"),
  settingsDialog: document.querySelector("#settings-dialog"),
  openSettingsButton: document.querySelector("#open-settings"),
  testVideoForm: document.querySelector("#test-video-form"),
  testVideoInput: document.querySelector("#test-video-url"),
  testVideoResult: document.querySelector("#test-video-result"),
  testVideoStatus: document.querySelector("#test-video-status"),
  candidateLimitsForm: document.querySelector("#candidate-limits-form"),
  refreshLimitInput: document.querySelector("#refresh-candidate-limit"),
  searchLimitInput: document.querySelector("#search-candidate-limit"),
  feedSettingsStatus: document.querySelector("#feed-settings-status"),
  curationPromptForm: document.querySelector("#curation-prompt-form"),
  curationPromptInput: document.querySelector("#curation-prompt"),
  restoreDefaultPromptButton: document.querySelector("#restore-default-prompt"),
  curationPromptStatus: document.querySelector("#curation-prompt-status"),
  watchLogBanner: document.querySelector("#watch-log-banner"),
  watchLogBannerTitle: document.querySelector("#watch-log-banner-title"),
  watchLogBannerDetail: document.querySelector("#watch-log-banner-detail"),
  watchLogBannerFix: document.querySelector("#watch-log-banner-fix"),
  watchLogBannerRecheck: document.querySelector("#watch-log-banner-recheck"),
  watchLogForm: document.querySelector("#watch-log-form"),
  watchLogStatus: document.querySelector("#watch-log-status"),
  watchLogDetail: document.querySelector("#watch-log-detail"),
};

const WATCH_LOG_STORAGE_KEY = "productivity-feed.watch-log";
const YOUTUBE_HOME_URL = "https://www.youtube.com/";
const PLAYER_PLAYING = 1;
// A view that never left the player cannot be confirmed, but neither should a
// freshly started video be reported as missing: YouTube needs a moment to file
// it, and the embed needs long enough to prove the person actually watched.
const MIN_WATCHED_SECONDS = 30;
const LOG_GRACE_MS = 150000;
const VERIFY_INTERVAL_MS = 60000;
// A view can still surface late, so a missing watch is re-checked periodically
// instead of leaving the warning up forever.
const RECHECK_MISSING_MS = 900000;
const MAX_REMEMBERED_WATCHES = 40;

let feed = [];
let watchLater = [];
let trustedCreators = [];
let settings = { refresh_candidate_limit: 30, search_candidate_limit: 30 };

let youtubePlayerApi = null;
let activePlayers = [];
const playback = new Map();
let watchLog = loadWatchLog();
let verifyInFlight = false;
let watchLogError = null;
let lastCheckedAt = null;
let historySize = null;
let recentHistory = [];

function isVideo(video) {
  return (
    video &&
    typeof video.video_id === "string" &&
    VIDEO_ID_PATTERN.test(video.video_id) &&
    typeof video.title === "string" &&
    video.title.trim().length > 0
  );
}

function setStatus(message, tone = "neutral", target = elements.status) {
  target.textContent = message;
  target.dataset.tone = tone;
}

async function request(url, options) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : null;

  if (!response.ok) {
    throw new Error(body?.message || "The request could not be completed.");
  }

  return body;
}

function createButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${className}`;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function createVideoCard(video, saved) {
  const card = document.createElement("article");
  card.className = "video-card";

  const player = document.createElement("div");
  player.className = "player";
  const playerTarget = document.createElement("div");
  playerTarget.className = "player-target";
  playerTarget.dataset.videoId = video.video_id;
  // A plain embed loads first so the video is always watchable; once the
  // playback-observation API is ready it swaps itself in for this element.
  const fallback = document.createElement("iframe");
  fallback.src = `https://www.youtube.com/embed/${encodeURIComponent(video.video_id)}`;
  fallback.title = video.title;
  fallback.loading = "lazy";
  fallback.referrerPolicy = "strict-origin-when-cross-origin";
  fallback.allow =
    "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture";
  fallback.allowFullscreen = true;
  playerTarget.append(fallback);
  player.append(playerTarget);

  const details = document.createElement("div");
  details.className = "video-details";
  const title = document.createElement("h3");
  title.textContent = video.title;
  details.append(title);

  if (video.channel_name) {
    const channel = document.createElement("p");
    channel.className = "channel-name";
    channel.textContent = video.channel_name;
    details.append(channel);
  }

  if (video.description) {
    const description = document.createElement("p");
    description.className = "description";
    description.textContent = video.description;
    details.append(description);
  }

  if (typeof video.curation_reason === "string" && video.curation_reason) {
    const reason = document.createElement("p");
    reason.className = "curation-reason";
    reason.textContent = video.curation_reason;
    details.append(reason);
  }

  const actions = document.createElement("div");
  actions.className = "video-actions";
  if (saved) {
    actions.append(
      createTrustCreatorButton(video),
      createButton("Remove", "button-quiet", () =>
        removeFromWatchLater(video.video_id),
      ),
    );
  } else {
    const alreadySaved = watchLater.some(
      (item) => item.video_id === video.video_id,
    );
    const saveButton = createButton(
      alreadySaved ? "Saved" : "Watch later",
      "button-secondary",
      () => addToWatchLater(video),
    );
    saveButton.disabled = alreadySaved;
    actions.append(saveButton);
  }

  card.append(player, details, actions);
  return card;
}

function renderList(container, videos, saved, emptyMessage) {
  container.replaceChildren();
  const validVideos = videos.filter(isVideo);
  if (!validVideos.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = emptyMessage;
    container.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  validVideos.forEach((video) =>
    fragment.append(createVideoCard(video, saved)),
  );
  container.append(fragment);
  mountPlayers(container, validVideos);
}

function render() {
  destroyPlayers();
  renderList(elements.feedList, feed, false, "No videos in the current feed.");
  renderList(
    elements.watchLaterList,
    watchLater,
    true,
    "Nothing saved for later.",
  );
  elements.feedCount.textContent = `${feed.filter(isVideo).length} videos`;
  elements.watchLaterCount.textContent = `${watchLater.filter(isVideo).length} saved`;
}

function renderTrustedCreators() {
  elements.trustedCreatorsList.replaceChildren();
  elements.trustedCreatorsCount.textContent = `${trustedCreators.length} creators`;

  if (!trustedCreators.length) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "No trusted creators yet.";
    elements.trustedCreatorsList.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  trustedCreators.forEach((name) => {
    const item = document.createElement("li");
    item.className = "creator-chip";

    const label = document.createElement("span");
    label.textContent = name;
    item.append(label);

    item.append(
      createButton("Remove", "button-quiet", () => removeTrustedCreator(name)),
    );

    fragment.append(item);
  });
  elements.trustedCreatorsList.append(fragment);
}

async function loadCollections() {
  const [nextFeed, nextWatchLater, nextTrustedCreators, nextSettings] =
    await Promise.all([
      request("/api/feed"),
      request("/api/watch-later"),
      request("/api/trusted-creators"),
      request("/api/settings"),
    ]);
  feed = Array.isArray(nextFeed) ? nextFeed : [];
  watchLater = Array.isArray(nextWatchLater) ? nextWatchLater : [];
  trustedCreators = Array.isArray(nextTrustedCreators)
    ? nextTrustedCreators
    : [];
  if (nextSettings && typeof nextSettings === "object") {
    settings = nextSettings;
  }
  render();
  renderTrustedCreators();
  renderSettings();
}

function renderSettings() {
  elements.refreshLimitInput.value = settings.refresh_candidate_limit;
  elements.searchLimitInput.value = settings.search_candidate_limit;
  elements.curationPromptInput.value = settings.curation_prompt;
}

function isTrustedCreator(channelName) {
  const normalized = (channelName || "").trim().toLowerCase();
  if (!normalized) return false;
  return trustedCreators.some(
    (creator) => creator.trim().toLowerCase() === normalized,
  );
}

// Watch later is where a video has already earned a second look, so that is the
// only list that offers a one-click shortcut to trust its creator.
function createTrustCreatorButton(video) {
  const channelName = (video.channel_name || "").trim();
  const alreadyTrusted = isTrustedCreator(channelName);
  const button = createButton(
    alreadyTrusted ? "Trusted" : "Trust creator",
    "button-secondary",
    () => trustCreator(channelName, button),
  );
  button.dataset.trustCreator = channelName;
  button.disabled = alreadyTrusted || !channelName;
  if (!channelName) {
    button.title = "This video has no channel name to trust.";
  }
  return button;
}

function markCreatorTrusted(channelName) {
  const normalized = channelName.trim().toLowerCase();
  document.querySelectorAll("[data-trust-creator]").forEach((button) => {
    if ((button.dataset.trustCreator || "").trim().toLowerCase() === normalized) {
      button.textContent = "Trusted";
      button.disabled = true;
    }
  });
}

async function trustCreator(channelName, button) {
  if (!channelName) return;
  if (button) button.disabled = true;

  try {
    const result = await request("/api/trusted-creators", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: channelName }),
    });
    if (!result.already_trusted) {
      trustedCreators = [...trustedCreators, result.name];
      renderTrustedCreators();
    }
    markCreatorTrusted(result.name || channelName);
    setStatus(
      result.already_trusted
        ? `${channelName} is already trusted`
        : `Now trusting ${result.name || channelName}`,
      "success",
    );
  } catch (error) {
    if (button) button.disabled = false;
    setStatus(error.message, "error");
  }
}

async function addTrustedCreator(event) {
  event.preventDefault();
  const name = elements.trustedCreatorInput.value.trim();
  if (!name) {
    setStatus("Enter a channel name", "error");
    elements.trustedCreatorInput.focus();
    return;
  }

  try {
    const result = await request("/api/trusted-creators", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!result.already_trusted) {
      trustedCreators = [...trustedCreators, result.name];
      renderTrustedCreators();
    }
    elements.trustedCreatorInput.value = "";
    setStatus(
      result.already_trusted ? "Already trusted" : "Added trusted creator",
      "success",
      elements.trustedCreatorsStatus,
    );
  } catch (error) {
    setStatus(error.message, "error", elements.trustedCreatorsStatus);
  }
}

async function removeTrustedCreator(name) {
  try {
    const result = await request(
      `/api/trusted-creators/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    if (result.removed) {
      trustedCreators = trustedCreators.filter((creator) => creator !== name);
      renderTrustedCreators();
      setStatus(
        "Removed trusted creator",
        "success",
        elements.trustedCreatorsStatus,
      );
    }
  } catch (error) {
    setStatus(error.message, "error", elements.trustedCreatorsStatus);
  }
}

function renderVideoTestResult(result) {
  const video = result?.video;
  const approvedVideos = result?.llm_response?.approved_videos;
  const approval = Array.isArray(approvedVideos)
    ? approvedVideos.find((item) => item?.id === video?.video_id)
    : undefined;

  elements.testVideoResult.replaceChildren();
  const outcome = document.createElement("section");
  outcome.className = `test-video-outcome ${approval ? "is-approved" : "is-rejected"}`;

  const heading = document.createElement("h3");
  heading.textContent = approval ? "Approved" : "Not approved";
  outcome.append(heading);

  const summary = document.createElement("p");
  const title =
    typeof video?.title === "string" ? `“${video.title}”` : "This video";
  summary.textContent = approval
    ? `${title} would be included in the productivity feed.`
    : `${title} would not be included in the productivity feed.`;
  outcome.append(summary);

  if (
    approval &&
    typeof approval.reason === "string" &&
    approval.reason.trim()
  ) {
    const reason = document.createElement("p");
    reason.className = "test-video-reason";
    reason.textContent = approval.reason;
    outcome.append(reason);
  }

  elements.testVideoResult.append(outcome);
}

async function submitTestVideo(event) {
  event.preventDefault();
  const url = elements.testVideoInput.value.trim();
  if (!url) {
    setStatus("Enter a YouTube link", "error", elements.testVideoStatus);
    elements.testVideoInput.focus();
    return;
  }

  const submitButton = elements.testVideoForm.querySelector("button");
  submitButton.disabled = true;
  elements.testVideoResult.replaceChildren();
  setStatus("Testing video...", "neutral", elements.testVideoStatus);
  try {
    const result = await request("/api/video-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    renderVideoTestResult(result);
    setStatus("Done", "success", elements.testVideoStatus);
  } catch (error) {
    setStatus(error.message, "error", elements.testVideoStatus);
  } finally {
    submitButton.disabled = false;
  }
}

async function saveCandidateLimits(event) {
  event.preventDefault();
  const refreshLimit = Number.parseInt(elements.refreshLimitInput.value, 10);
  const searchLimit = Number.parseInt(elements.searchLimitInput.value, 10);
  if (!Number.isInteger(refreshLimit) || !Number.isInteger(searchLimit)) {
    setStatus(
      "Enter whole numbers for both limits",
      "error",
      elements.feedSettingsStatus,
    );
    return;
  }

  const submitButton = elements.candidateLimitsForm.querySelector("button");
  submitButton.disabled = true;
  setStatus("Saving...", "neutral", elements.feedSettingsStatus);
  try {
    const result = await request("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        refresh_candidate_limit: refreshLimit,
        search_candidate_limit: searchLimit,
      }),
    });
    settings = result;
    renderSettings();
    setStatus("Saved", "success", elements.feedSettingsStatus);
  } catch (error) {
    setStatus(error.message, "error", elements.feedSettingsStatus);
  } finally {
    submitButton.disabled = false;
  }
}

async function saveCurationPrompt(event) {
  event.preventDefault();
  const curationPrompt = elements.curationPromptInput.value.trim();
  if (!curationPrompt) {
    setStatus("Enter a curation prompt", "error", elements.curationPromptStatus);
    elements.curationPromptInput.focus();
    return;
  }

  const submitButton = elements.curationPromptForm.querySelector("button[type='submit']");
  submitButton.disabled = true;
  setStatus("Saving...", "neutral", elements.curationPromptStatus);
  try {
    const result = await request("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ curation_prompt: curationPrompt }),
    });
    settings = result;
    renderSettings();
    setStatus("Prompt saved", "success", elements.curationPromptStatus);
  } catch (error) {
    setStatus(error.message, "error", elements.curationPromptStatus);
  } finally {
    submitButton.disabled = false;
  }
}

function restoreDefaultCurationPrompt() {
  elements.curationPromptInput.value = settings.default_curation_prompt;
  setStatus("Default prompt restored. Save to apply it.", "neutral", elements.curationPromptStatus);
}

async function addToWatchLater(video) {
  try {
    const result = await request("/api/watch-later", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(video),
    });
    if (!result.already_saved) {
      watchLater = [...watchLater, result.video];
    }
    render();
    setStatus(
      result.already_saved ? "Already saved" : "Saved to Watch later",
      "success",
    );
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function removeFromWatchLater(videoId) {
  try {
    const result = await request(
      `/api/watch-later/${encodeURIComponent(videoId)}`,
      {
        method: "DELETE",
      },
    );
    if (result.removed) {
      watchLater = watchLater.filter((video) => video.video_id !== videoId);
      render();
      setStatus("Removed from Watch later", "success");
    }
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function refreshFeed() {
  elements.refreshButton.disabled = true;
  setStatus("Refreshing feed...");
  try {
    const result = await request("/api/refresh", { method: "POST" });
    await loadCollections();
    setStatus(`Feed replaced with ${result.refreshed} videos`, "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    elements.refreshButton.disabled = false;
  }
}

async function searchFeed(event) {
  event.preventDefault();
  const query = elements.searchInput.value.trim();
  if (!query) {
    setStatus("Enter a search topic", "error");
    elements.searchInput.focus();
    return;
  }

  elements.searchInput.disabled = true;
  setStatus(`Searching for ${query}...`);
  try {
    const result = await request("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    await loadCollections();
    setStatus(
      `Feed replaced with ${result.approved} videos for ${result.query}`,
      "success",
    );
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    elements.searchInput.disabled = false;
  }
}

function loadYouTubePlayerApi() {
  if (youtubePlayerApi) return youtubePlayerApi;

  youtubePlayerApi = new Promise((resolve, reject) => {
    if (window.YT && window.YT.Player) {
      resolve(window.YT);
      return;
    }

    const previous = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      if (typeof previous === "function") previous();
      resolve(window.YT);
    };

    const script = document.createElement("script");
    script.src = "https://www.youtube.com/iframe_api";
    script.async = true;
    script.addEventListener("error", () =>
      reject(new Error("The YouTube player API could not be loaded.")),
    );
    document.head.append(script);
  });

  return youtubePlayerApi;
}

// The API only observes the player the user already drives with its own play
// button; nothing here starts playback programmatically, because a view that
// the page initiates itself does not register with YouTube.
function mountPlayers(container, videos) {
  const targets = container.querySelectorAll("[data-video-id]");
  if (!targets.length) return;

  const videosById = new Map(videos.map((video) => [video.video_id, video]));
  loadYouTubePlayerApi()
    .then((YT) => {
      targets.forEach((target) => {
        const video = videosById.get(target.dataset.videoId);
        if (!video || target.isConnected === false) return;
        const player = new YT.Player(target, {
          videoId: video.video_id,
          playerVars: { rel: 0, playsinline: 1 },
          events: {
            onStateChange: (event) => handlePlayerState(video, event),
          },
        });
        activePlayers.push(player);
      });
    })
    .catch(() => {
      // The plain embed already in the page keeps working without observation.
    });
}

function destroyPlayers() {
  flushPlayback();
  activePlayers.forEach((player) => {
    try {
      player.destroy();
    } catch (error) {
      // The element may already be detached; nothing to clean up.
    }
  });
  activePlayers = [];
  playback.clear();
}

function handlePlayerState(video, event) {
  const state = playback.get(video.video_id) || { startedAt: null };
  const now = Date.now();

  if (event.data === PLAYER_PLAYING) {
    if (state.startedAt === null) {
      state.startedAt = now;
      rememberWatch(video);
    }
  } else if (state.startedAt !== null) {
    recordWatchSeconds(video.video_id, (now - state.startedAt) / 1000);
    state.startedAt = null;
  }

  playback.set(video.video_id, state);
  renderWatchLogStatus();
}

function flushPlayback() {
  const now = Date.now();
  playback.forEach((state, videoId) => {
    if (state.startedAt === null) return;
    recordWatchSeconds(videoId, (now - state.startedAt) / 1000);
    state.startedAt = now;
  });
}

function loadWatchLog() {
  try {
    const raw = window.localStorage.getItem(WATCH_LOG_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (entry) =>
        entry &&
        typeof entry.video_id === "string" &&
        VIDEO_ID_PATTERN.test(entry.video_id),
    );
  } catch (error) {
    return [];
  }
}

function saveWatchLog() {
  try {
    window.localStorage.setItem(WATCH_LOG_STORAGE_KEY, JSON.stringify(watchLog));
  } catch (error) {
    // Without storage, verification just cannot span page reloads.
  }
}

function rememberWatch(video) {
  const existing = watchLog.find((entry) => entry.video_id === video.video_id);
  if (existing) {
    existing.title = video.title;
    existing.last_played_at = Date.now();
    existing.logged = null;
    existing.verified_at = null;
  } else {
    watchLog.unshift({
      video_id: video.video_id,
      title: video.title,
      seconds: 0,
      last_played_at: Date.now(),
      logged: null,
      verified_at: null,
    });
  }

  if (watchLog.length > MAX_REMEMBERED_WATCHES) {
    watchLog.length = MAX_REMEMBERED_WATCHES;
  }
  saveWatchLog();
  renderWatchLogStatus();
}

function recordWatchSeconds(videoId, seconds) {
  const entry = watchLog.find((item) => item.video_id === videoId);
  if (!entry || !(seconds > 0)) return;
  entry.seconds = Math.round((entry.seconds + seconds) * 10) / 10;
  entry.last_played_at = Date.now();
  saveWatchLog();
}

async function verifyWatchLogging({ force = false } = {}) {
  if (verifyInFlight) return;

  const now = Date.now();
  const candidates = watchLog.filter((entry) => {
    if (entry.seconds < MIN_WATCHED_SECONDS) return false;
    if (!force && now - entry.last_played_at < LOG_GRACE_MS) return false;
    if (force) return true;
    if (entry.logged === null) return true;
    if (entry.logged === false) {
      return !entry.verified_at || now - entry.verified_at >= RECHECK_MISSING_MS;
    }
    return false;
  });

  if (!candidates.length) {
    renderWatchLogStatus();
    return;
  }

  verifyInFlight = true;
  setStatus("Checking YouTube logging...", "neutral", elements.watchLogStatus);
  try {
    const result = await request("/api/watch-log/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video_ids: candidates.map((entry) => entry.video_id).slice(0, 50),
      }),
    });

    candidates.forEach((entry) => {
      if (Object.prototype.hasOwnProperty.call(result.results, entry.video_id)) {
        entry.logged = Boolean(result.results[entry.video_id]);
        entry.verified_at = Date.now();
      }
    });

    watchLogError = null;
    lastCheckedAt = Date.now();
    historySize = result.history_size;
    recentHistory = Array.isArray(result.recent) ? result.recent : [];
    saveWatchLog();

    const missing = watchLog.filter((entry) => entry.logged === false).length;
    setStatus(
      missing
        ? `${missing} watch(es) missing from YouTube`
        : "Every watch reached YouTube",
      missing ? "error" : "success",
      elements.watchLogStatus,
    );
  } catch (error) {
    watchLogError = error.message;
    setStatus(error.message, "error", elements.watchLogStatus);
  } finally {
    verifyInFlight = false;
    renderWatchLogStatus();
  }
}

function renderWatchLogStatus() {
  const missing = watchLog.filter((entry) => entry.logged === false);
  const logged = watchLog.filter((entry) => entry.logged === true);
  const pending = watchLog.filter(
    (entry) => entry.logged === null && entry.seconds >= MIN_WATCHED_SECONDS,
  );

  if (missing.length) {
    const named = missing
      .slice(0, 3)
      .map((entry) => `“${entry.title}”`)
      .join(", ");
    elements.watchLogBanner.hidden = false;
    elements.watchLogBannerTitle.textContent =
      "Your watches aren't reaching YouTube";
    elements.watchLogBannerDetail.textContent =
      `${missing.length} video${missing.length === 1 ? "" : "s"} you watched here ` +
      `never appeared in your YouTube history` +
      (named ? ` (${named})` : "") +
      `, so ${missing.length === 1 ? "it" : "they"} never reached your ` +
      "recommendations. The embedded player has most likely lost your sign-in — " +
      "open YouTube, make sure you are still signed in, then watch the next " +
      "video here.";
  } else if (watchLogError) {
    elements.watchLogBanner.hidden = false;
    elements.watchLogBannerTitle.textContent =
      "YouTube logging can't be verified";
    elements.watchLogBannerDetail.textContent = watchLogError;
  } else {
    elements.watchLogBanner.hidden = true;
  }

  elements.watchLogDetail.replaceChildren();
  const summary = document.createElement("p");
  summary.textContent =
    `Watched here: ${watchLog.length} · confirmed logged: ${logged.length} · ` +
    `missing: ${missing.length} · still settling: ${pending.length}`;
  if (missing.length) summary.className = "is-missing";
  elements.watchLogDetail.append(summary);

  const lines = [];
  if (historySize !== null) {
    lines.push(`History window read back: ${historySize} videos`);
  }
  if (lastCheckedAt) {
    lines.push(`Last checked: ${new Date(lastCheckedAt).toLocaleTimeString()}`);
  }
  if (recentHistory.length) {
    const newest = recentHistory[0];
    lines.push(
      `Newest video in your history: ${newest.title} — ` +
        `${newest.channel_name || "unknown channel"}`,
    );
  }
  lines.forEach((text) => {
    const line = document.createElement("p");
    line.textContent = text;
    elements.watchLogDetail.append(line);
  });
}

function openYouTubeToFix() {
  window.open(YOUTUBE_HOME_URL, "_blank", "noopener");
  setStatus(
    "Sign in on YouTube if prompted, then check again.",
    "neutral",
    elements.watchLogStatus,
  );
}

elements.refreshButton.addEventListener("click", refreshFeed);
elements.searchForm.addEventListener("submit", searchFeed);
elements.trustedCreatorForm.addEventListener("submit", addTrustedCreator);
elements.openSettingsButton.addEventListener("click", () =>
  elements.settingsDialog.showModal(),
);
elements.testVideoForm.addEventListener("submit", submitTestVideo);
elements.candidateLimitsForm.addEventListener("submit", saveCandidateLimits);
elements.curationPromptForm.addEventListener("submit", saveCurationPrompt);
elements.restoreDefaultPromptButton.addEventListener("click", restoreDefaultCurationPrompt);
elements.watchLogForm.addEventListener("submit", (event) => {
  event.preventDefault();
  verifyWatchLogging({ force: true });
});
elements.watchLogBannerRecheck.addEventListener("click", () =>
  verifyWatchLogging({ force: true }),
);
elements.watchLogBannerFix.addEventListener("click", openYouTubeToFix);

setInterval(() => {
  flushPlayback();
  verifyWatchLogging();
}, VERIFY_INTERVAL_MS);

loadCollections().then(
  () => {
    setStatus("Ready");
    renderWatchLogStatus();
    verifyWatchLogging();
  },
  (error) => setStatus(error.message, "error"),
);
