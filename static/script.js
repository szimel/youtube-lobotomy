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
  openJevButton: document.querySelector("#open-jev"),
  jevDialog: document.querySelector("#jev-dialog"),
  jevForm: document.querySelector("#jev-form"),
  jevProfile: document.querySelector("#jev-profile"),
  jevPositives: document.querySelector("#jev-positives"),
  jevDisqualifiers: document.querySelector("#jev-disqualifiers"),
  jevPositivesCount: document.querySelector("#jev-positives-count"),
  jevDisqualifiersCount: document.querySelector("#jev-disqualifiers-count"),
  jevAddPositive: document.querySelector("#jev-add-positive"),
  jevAddDisqualifier: document.querySelector("#jev-add-disqualifier"),
  jevRatingEnabled: document.querySelector("#jev-rating-enabled"),
  jevRatingInstruction: document.querySelector("#jev-rating-instruction"),
  jevRatingCriteria: document.querySelector("#jev-rating-criteria"),
  jevRatingMinimum: document.querySelector("#jev-rating-minimum"),
  jevTranscriptMin: document.querySelector("#jev-transcript-min"),
  jevTranscriptMax: document.querySelector("#jev-transcript-max"),
  jevRestoreDefaults: document.querySelector("#jev-restore-defaults"),
  jevStatus: document.querySelector("#jev-status"),
  watchLogBanner: document.querySelector("#watch-log-banner"),
  watchLogBannerTitle: document.querySelector("#watch-log-banner-title"),
  watchLogBannerDetail: document.querySelector("#watch-log-banner-detail"),
  watchLogBannerFix: document.querySelector("#watch-log-banner-fix"),
  watchLogBannerRecheck: document.querySelector("#watch-log-banner-recheck"),
  watchLogForm: document.querySelector("#watch-log-form"),
  watchLogStatus: document.querySelector("#watch-log-status"),
  watchLogDetail: document.querySelector("#watch-log-detail"),
  hideWatched: document.querySelector("#hide-watched"),
  runStats: document.querySelector("#run-stats"),
  runNote: document.querySelector("#run-note"),
  filteredDrawer: document.querySelector("#filtered-drawer"),
  filteredSummary: document.querySelector("#filtered-summary"),
  filteredList: document.querySelector("#filtered-list"),
  historyDrawer: document.querySelector("#history-drawer"),
  historyList: document.querySelector("#history-list"),
  trustSuggestions: document.querySelector("#trust-suggestions"),
  jevPreviewButton: document.querySelector("#jev-preview"),
  jevPreviewResult: document.querySelector("#jev-preview-result"),
};

const WATCH_LOG_STORAGE_KEY = "productivity-feed.watch-log";
const HIDE_WATCHED_STORAGE_KEY = "productivity-feed.hide-watched";
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
// Refresh on open once the feed is older than this, so the app is already
// showing today's videos instead of making the first visit wait.
const STALE_FEED_MS = 6 * 60 * 60 * 1000;

let feed = [];
let watchLater = [];
let trustedCreators = [];
let watched = [];
let lastRun = null;
let trustSuggestions = [];
let settings = {
  refresh_candidate_limit: 20,
  search_candidate_limit: 20,
  max_candidate_limit: 20,
  max_transcript_tokens: 20000,
  jev: null,
  default_jev: null,
};

let youtubePlayerApi = null;
let activePlayers = [];
const playback = new Map();
let watchLog = loadWatchLog();
let verifyInFlight = false;
let watchLogError = null;
let lastCheckedAt = null;
let historySize = null;
let recentHistory = [];
let hideWatched = loadHideWatched();
// Watches are reported in batches so a page with several videos playing does
// not generate a request per pause.
let watchReportQueue = new Map();
let watchReportTimer = null;

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

function formatDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) {
    return "";
  }
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  return hours
    ? `${hours}:${pad(minutes)}:${pad(secs)}`
    : `${minutes}:${pad(secs)}`;
}

function watchedIds() {
  return new Set(
    watched
      .filter((record) => Number(record?.seconds) >= MIN_WATCHED_SECONDS)
      .map((record) => record.video_id),
  );
}

// A card loads no player until it is clicked: a grid of twenty iframes is slow,
// and tearing them all down to re-render is what used to reset a video that was
// already playing.
function createVideoCard(video, saved) {
  const card = document.createElement("article");
  card.className = "video-card";
  card.dataset.videoId = video.video_id;

  const player = document.createElement("div");
  player.className = "player";
  const poster = document.createElement("button");
  poster.type = "button";
  poster.className = "player-poster";
  poster.setAttribute("aria-label", `Play ${video.title}`);

  if (typeof video.thumbnail_url === "string" && video.thumbnail_url) {
    const image = document.createElement("img");
    image.className = "player-thumbnail";
    image.src = video.thumbnail_url;
    image.alt = "";
    image.loading = "lazy";
    image.referrerPolicy = "strict-origin-when-cross-origin";
    poster.append(image);
  }

  const playBadge = document.createElement("span");
  playBadge.className = "player-play";
  playBadge.textContent = "▶";
  poster.append(playBadge);

  const duration = formatDuration(video.duration_seconds);
  if (duration) {
    const durationBadge = document.createElement("span");
    durationBadge.className = "player-duration";
    durationBadge.textContent = duration;
    poster.append(durationBadge);
  }

  const playerTarget = document.createElement("div");
  playerTarget.className = "player-target";
  poster.addEventListener("click", () => activatePlayer(player, poster, video));
  player.append(poster, playerTarget);

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
    const saveButton = createButton("Watch later", "button-secondary", () =>
      addToWatchLater(video, saveButton),
    );
    saveButton.classList.add("save-later");
    actions.append(saveButton);
  }

  card.append(player, details, actions);
  markCardWatchState(card, video);
  return card;
}

function markCardWatchState(card, video) {
  const isWatched = watchedIds().has(video.video_id);
  card.classList.toggle("is-watched", isWatched);

  const existing = card.querySelector(".watched-chip");
  if (existing) existing.remove();
  if (isWatched) {
    const chip = document.createElement("span");
    chip.className = "watched-chip";
    chip.textContent = "Watched";
    card.querySelector(".video-details")?.prepend(chip);
  }

  // Only the feed's own save button changes wording; Watch later's buttons mean
  // something else entirely.
  const saveButton = card.querySelector(".video-actions .save-later");
  if (saveButton instanceof HTMLButtonElement) {
    const alreadySaved = watchLater.some(
      (item) => item.video_id === video.video_id,
    );
    saveButton.textContent = isWatched
      ? "Watched"
      : alreadySaved
        ? "Saved"
        : "Watch later";
    saveButton.disabled = isWatched || alreadySaved;
  }
}

// Clicking the poster opens the player and stops there. YouTube's IFrame API
// reference is explicit: "A playback only counts toward a video's official view
// count if it is initiated via a native play button in the player." Calling
// playVideo() here would be programmatic playback, so the video would play
// without ever registering -- which is the opposite of the point of watching it
// here. The click that follows, on the player's own play button, is the one
// that counts.
function activatePlayer(player, poster, video) {
  if (player.classList.contains("is-playing")) return;
  const target = player.querySelector(".player-target");
  if (!target) return;
  player.classList.add("is-playing");
  poster.hidden = true;
  target.replaceChildren();

  loadYouTubePlayerApi()
    .then((YT) => {
      const mount = document.createElement("div");
      target.append(mount);
      const instance = new YT.Player(mount, {
        videoId: video.video_id,
        playerVars: { rel: 0, playsinline: 1, autoplay: 0 },
        events: {
          onStateChange: (event) => handlePlayerState(video, event),
        },
      });
      activePlayers.push(instance);
    })
    .catch(() => {
      // Without the API the plain embed still plays; only observation is lost.
      const fallback = document.createElement("iframe");
      fallback.src = `https://www.youtube.com/embed/${encodeURIComponent(
        video.video_id,
      )}`;
      fallback.title = video.title;
      fallback.allow =
        "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture";
      fallback.allowFullscreen = true;
      target.replaceChildren(fallback);
    });
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
}

function visibleFeed() {
  const valid = feed.filter(isVideo);
  if (!hideWatched) return valid;
  const seen = watchedIds();
  return valid.filter((video) => !seen.has(video.video_id));
}

function render() {
  destroyPlayers();
  const visible = visibleFeed();
  renderList(
    elements.feedList,
    visible,
    false,
    feed.length && !visible.length
      ? "You have watched everything in this feed. Refresh for more."
      : "No videos in the current feed.",
  );
  renderList(
    elements.watchLaterList,
    watchLater,
    true,
    "Nothing saved for later.",
  );
  const hidden = feed.filter(isVideo).length - visible.length;
  elements.feedCount.textContent =
    `${visible.length} video${visible.length === 1 ? "" : "s"}` +
    (hidden > 0 ? ` · ${hidden} watched hidden` : "");
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
  const [
    nextFeed,
    nextWatchLater,
    nextTrustedCreators,
    nextSettings,
    nextWatched,
    nextRun,
    nextSuggestions,
    nextRuns,
  ] = await Promise.all([
    request("/api/feed"),
    request("/api/watch-later"),
    request("/api/trusted-creators"),
    request("/api/settings"),
    request("/api/watched"),
    request("/api/last-run"),
    request("/api/trust-suggestions"),
    request("/api/runs"),
  ]);
  feed = Array.isArray(nextFeed) ? nextFeed : [];
  watchLater = Array.isArray(nextWatchLater) ? nextWatchLater : [];
  trustedCreators = Array.isArray(nextTrustedCreators)
    ? nextTrustedCreators
    : [];
  watched = Array.isArray(nextWatched) ? nextWatched : [];
  lastRun = nextRun?.run && typeof nextRun.run === "object" ? nextRun.run : null;
  trustSuggestions = Array.isArray(nextSuggestions) ? nextSuggestions : [];
  if (nextSettings && typeof nextSettings === "object") {
    settings = nextSettings;
  }
  render();
  renderTrustedCreators();
  renderSettings();
  renderRunSummary();
  renderRunHistory(Array.isArray(nextRuns) ? nextRuns : []);
  renderTrustSuggestions();
}

function loadHideWatched() {
  try {
    return window.localStorage.getItem(HIDE_WATCHED_STORAGE_KEY) === "true";
  } catch (error) {
    return false;
  }
}

function renderRunSummary() {
  const stats = lastRun?.stats;
  elements.filteredDrawer.hidden = true;
  elements.filteredList.replaceChildren();
  elements.filteredSummary.textContent = "Filtered out";

  if (!stats) {
    elements.runStats.textContent = "";
    elements.runNote.textContent =
      "Nothing yet — refresh the feed and this will show what the curator did with each candidate.";
    return;
  }

  const when = lastRun.ran_at
    ? new Date(lastRun.ran_at * 1000).toLocaleString()
    : "unknown time";
  const cost = Number(stats.estimated_cost_usd || 0);
  elements.runStats.textContent =
    `${stats.approved} kept of ${stats.candidates}`;
  const parts = [
    `${lastRun.kind === "search" ? "Search" : "Refresh"} at ${when}`,
    `${stats.judged} judged`,
  ];
  if (stats.skipped_watched) parts.push(`${stats.skipped_watched} already watched`);
  if (stats.with_transcript) parts.push(`${stats.with_transcript} with transcripts`);
  if (stats.input_tokens) {
    parts.push(`${stats.input_tokens.toLocaleString()} tokens (~$${cost.toFixed(6)})`);
  }
  if (stats.run_seconds) parts.push(`${stats.run_seconds}s`);
  if (stats.model) parts.push(stats.model);
  // A run that could not judge everything is not a strict run, and saying so is
  // the whole point of keeping these numbers.
  if (stats.unjudged) {
    parts.push(`${stats.unjudged} could not be judged`);
  }
  elements.runNote.textContent = parts.join(" · ");

  const candidates = Array.isArray(lastRun.candidates) ? lastRun.candidates : [];
  const rejected = candidates.filter(
    (candidate) => candidate?.decision && !candidate.decision.approved,
  );
  if (!candidates.length) return;

  elements.filteredDrawer.hidden = false;
  elements.filteredSummary.textContent =
    `Judged ${candidates.length} candidates · ${rejected.length} filtered out`;

  const list = document.createDocumentFragment();
  candidates.forEach((candidate) => {
    const video = candidate.video || {};
    const decision = candidate.decision;
    const item = document.createElement("article");
    item.className = "filtered-item";

    const heading = document.createElement("p");
    heading.className = "filtered-title";
    heading.textContent = video.title || "(untitled)";
    item.append(heading);

    const meta = document.createElement("p");
    meta.className = "filtered-meta";
    const bits = [];
    if (video.channel_name) bits.push(video.channel_name);
    if (candidate.used_transcript) {
      bits.push(`transcript ~${candidate.transcript_tokens} tokens`);
    } else {
      bits.push("title only");
    }
    if (candidate.error) bits.push("could not be judged");
    meta.textContent = bits.join(" · ");
    item.append(meta);

    const verdict = document.createElement("p");
    verdict.className = `filtered-verdict ${
      !decision ? "is-unknown" : decision.approved ? "is-approved" : "is-rejected"
    }`;
    verdict.textContent = decision
      ? decision.summary
      : `Jev did not answer for this video${candidate.error ? `: ${candidate.error}` : ""}.`;
    item.append(verdict);

    const checks = Array.isArray(decision?.checks) ? decision.checks : [];
    if (checks.length) {
      const checkList = document.createElement("ul");
      checkList.className = "filtered-checks";
      checks.forEach((check) => {
        const value =
          typeof check.value === "number" ? check.value.toFixed(2) : "unanswered";
        const line = document.createElement("li");
        line.className =
          check.answered === false || check.passed === null
            ? "is-unanswered"
            : check.passed
              ? "is-pass"
              : "is-fail";
        line.textContent =
          `${check.kind === "positive" ? "must" : "flag"} · ${check.name}: ` +
          `${value} (${check.kind === "positive" ? "needs" : "limit"} ${Number(
            check.threshold,
          ).toFixed(2)})`;
        checkList.append(line);
      });
      item.append(checkList);
    }

    list.append(item);
  });
  elements.filteredList.append(list);
}

function renderRunHistory(runs) {
  elements.historyList.replaceChildren();
  elements.historyDrawer.hidden = !runs.length;
  if (!runs.length) return;

  runs.forEach((run) => {
    const line = document.createElement("p");
    line.className = "history-line";
    const when = run.ran_at
      ? new Date(run.ran_at * 1000).toLocaleString()
      : "unknown time";
    const cost = Number(run.estimated_cost_usd || 0);
    const bits = [
      when,
      run.kind === "search" ? `search “${run.query || ""}”` : "refresh",
      `${run.approved ?? 0} of ${run.candidates ?? 0} kept`,
    ];
    if (run.skipped_watched) bits.push(`${run.skipped_watched} already watched`);
    if (run.with_transcript !== undefined && run.candidates) {
      bits.push(`${run.with_transcript} with transcripts`);
    }
    if (run.unjudged) bits.push(`${run.unjudged} unjudged`);
    if (run.input_tokens) {
      bits.push(`${run.input_tokens.toLocaleString()} tok (~$${cost.toFixed(6)})`);
    }
    if (run.run_seconds) bits.push(`${run.run_seconds}s`);
    line.textContent = bits.join(" · ");
    elements.historyList.append(line);
  });
}

function renderTrustSuggestions() {
  elements.trustSuggestions.replaceChildren();
  if (!trustSuggestions.length) {
    elements.trustSuggestions.hidden = true;
    return;
  }

  elements.trustSuggestions.hidden = false;
  const heading = document.createElement("p");
  heading.className = "settings-hint";
  heading.textContent =
    "Channels you keep choosing. Trusting one sends its videos straight to the feed.";
  elements.trustSuggestions.append(heading);

  trustSuggestions.forEach((suggestion) => {
    const row = document.createElement("div");
    row.className = "trust-suggestion";
    const label = document.createElement("span");
    label.textContent = `${suggestion.name} — ${suggestion.reason}`;
    const addButton = createButton("Trust", "button-secondary", async () => {
      addButton.disabled = true;
      try {
        await request("/api/trusted-creators", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: suggestion.name }),
        });
        trustedCreators = [...trustedCreators, suggestion.name];
        trustSuggestions = trustSuggestions.filter(
          (item) => item.name !== suggestion.name,
        );
        renderTrustedCreators();
        renderTrustSuggestions();
        setStatus(`Trusting ${suggestion.name}`, "success");
      } catch (error) {
        addButton.disabled = false;
        setStatus(error.message, "error", elements.trustedCreatorsStatus);
      }
    });
    row.append(label, addButton);
    elements.trustSuggestions.append(row);
  });
}

function renderSettings() {
  elements.refreshLimitInput.value = settings.refresh_candidate_limit;
  elements.searchLimitInput.value = settings.search_candidate_limit;
  // The server owns these limits, so the form cannot invite a value it rejects.
  const maxCandidates = Number(settings.max_candidate_limit);
  if (Number.isFinite(maxCandidates) && maxCandidates > 0) {
    elements.refreshLimitInput.max = String(maxCandidates);
    elements.searchLimitInput.max = String(maxCandidates);
  }
  const maxTranscript = Number(settings.max_transcript_tokens);
  if (Number.isFinite(maxTranscript) && maxTranscript > 0) {
    elements.jevTranscriptMin.max = String(maxTranscript);
    elements.jevTranscriptMax.max = String(maxTranscript);
  }
  renderJevSettings(settings.jev || settings.default_jev);
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
  const decision = result?.decision;
  const approved = Boolean(decision?.approved);

  elements.testVideoResult.replaceChildren();
  const outcome = document.createElement("section");
  outcome.className = `test-video-outcome ${approved ? "is-approved" : "is-rejected"}`;

  const heading = document.createElement("h3");
  heading.textContent = approved ? "Approved" : "Not approved";
  outcome.append(heading);

  const title =
    typeof video?.title === "string" ? `“${video.title}”` : "This video";
  const summary = document.createElement("p");
  summary.textContent =
    typeof decision?.summary === "string" && decision.summary
      ? decision.summary
      : `${title} has no verdict yet.`;
  outcome.append(summary);

  const transcriptNote = document.createElement("p");
  transcriptNote.className = "test-video-note";
  transcriptNote.textContent = video?.used_transcript
    ? `Judged with a transcript (~${video.transcript_tokens} tokens).`
    : "Judged on the title and description only — no usable transcript.";
  outcome.append(transcriptNote);

  const checks = Array.isArray(decision?.checks) ? decision.checks : [];
  if (checks.length) {
    const list = document.createElement("ul");
    list.className = "test-video-checks";
    checks.forEach((check) => {
      const item = document.createElement("li");
      // `passed` is null when Jev never answered the question, which is not the
      // same as a rule failing -- and not the same as a rule being satisfied.
      const unanswered = check.answered === false || check.passed === null;
      item.className = unanswered
        ? "is-unanswered"
        : check.passed
          ? "is-pass"
          : "is-fail";
      const value = typeof check.value === "number" ? check.value.toFixed(2) : "no answer";
      const verdict = unanswered ? "unanswered" : check.passed ? "pass" : "fail";
      item.textContent =
        `${check.kind === "positive" ? "Must be true" : "Red flag"} · ` +
        `${check.name}: ${value} — ${verdict}`;
      list.append(item);
    });
    outcome.append(list);
  }

  const rating = decision?.rating;
  if (rating && typeof rating.score === "number") {
    const ratingLine = document.createElement("p");
    ratingLine.className = "test-video-note";
    ratingLine.textContent =
      `Rating ${rating.score} (minimum ${rating.minimum}` +
      (typeof rating.confidence === "number"
        ? `, confidence ${rating.confidence})`
        : ")");
    outcome.append(ratingLine);
  }

  const usage = result?.usage;
  if (usage && typeof usage.input_tokens === "number") {
    const usageLine = document.createElement("p");
    usageLine.className = "test-video-note";
    const cost = Number(usage.estimated_cost_usd || 0);
    const model =
      typeof usage.model === "string" && usage.model ? ` · ${usage.model}` : "";
    usageLine.textContent =
      `Cost ${usage.input_tokens.toLocaleString()} input tokens ` +
      `(~$${cost.toFixed(6)})${model}`;
    outcome.append(usageLine);
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

const JEV_THRESHOLD_LABELS = {
  positives: "must score at least",
  disqualifiers: "reject if at least",
};

function toFiniteNumber(value, fallback) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function createJevRuleRow(kind, rule) {
  const source = rule || {};
  const row = document.createElement("div");
  row.className = "jev-rule";
  row.dataset.kind = kind;

  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.className = "jev-rule-enabled";
  toggle.checked = source.enabled !== false;
  toggle.title = "Rule enabled";
  row.append(toggle);

  const fields = document.createElement("div");
  fields.className = "jev-rule-fields";

  const name = document.createElement("input");
  name.type = "text";
  name.className = "jev-rule-name";
  name.maxLength = 80;
  name.placeholder = "Rule name";
  name.value = source.name || "";

  const instruction = document.createElement("input");
  instruction.type = "text";
  instruction.className = "jev-rule-instruction";
  instruction.maxLength = 1000;
  instruction.placeholder = "The yes/no question Jev answers about each video";
  instruction.value = source.instruction || "";

  const thresholdWrap = document.createElement("label");
  thresholdWrap.className = "jev-rule-threshold";
  const thresholdLabel = document.createElement("span");
  thresholdLabel.textContent = JEV_THRESHOLD_LABELS[kind] || "threshold";
  const threshold = document.createElement("input");
  threshold.type = "number";
  threshold.className = "jev-rule-threshold-input";
  threshold.min = "0";
  threshold.max = "1";
  threshold.step = "0.05";
  threshold.value = source.threshold ?? 0.5;
  thresholdWrap.append(thresholdLabel, threshold);

  fields.append(name, instruction, thresholdWrap);
  row.append(fields);

  const remove = createButton("Remove", "button-quiet", () => {
    row.remove();
    updateJevCounts();
  });
  remove.classList.add("jev-rule-remove");
  row.append(remove);

  return row;
}

function updateJevCounts() {
  const summarise = (container) => {
    const rows = Array.from(container.querySelectorAll(".jev-rule"));
    const active = rows.filter((row) => {
      const toggle = row.querySelector(".jev-rule-enabled");
      return toggle && toggle.checked;
    }).length;
    return `${active} of ${rows.length} on`;
  };
  elements.jevPositivesCount.textContent = summarise(elements.jevPositives);
  elements.jevDisqualifiersCount.textContent = summarise(elements.jevDisqualifiers);
}

function readJevRules(container) {
  return Array.from(container.querySelectorAll(".jev-rule")).map((row) => ({
    name: row.querySelector(".jev-rule-name").value.trim(),
    instruction: row.querySelector(".jev-rule-instruction").value.trim(),
    threshold: toFiniteNumber(
      row.querySelector(".jev-rule-threshold-input").value,
      0.5,
    ),
    enabled: row.querySelector(".jev-rule-enabled").checked,
  }));
}

function renderJevSettings(config) {
  elements.jevPreviewResult.replaceChildren();
  const jev = config || {};
  elements.jevProfile.value = jev.profile || "";

  elements.jevPositives.replaceChildren();
  (jev.positives || []).forEach((rule) =>
    elements.jevPositives.append(createJevRuleRow("positives", rule)),
  );
  elements.jevDisqualifiers.replaceChildren();
  (jev.disqualifiers || []).forEach((rule) =>
    elements.jevDisqualifiers.append(createJevRuleRow("disqualifiers", rule)),
  );

  const rating = jev.rating || {};
  elements.jevRatingEnabled.checked = Boolean(rating.enabled);
  elements.jevRatingInstruction.value = rating.instruction || "";
  elements.jevRatingCriteria.value = (rating.criteria || []).join("\n");
  elements.jevRatingMinimum.value = rating.minimum ?? 0;

  elements.jevTranscriptMin.value = jev.transcript_min_tokens ?? 1500;
  elements.jevTranscriptMax.value = jev.transcript_max_tokens ?? 15000;

  updateJevCounts();
}

function collectJevConfig() {
  return {
    profile: elements.jevProfile.value.trim(),
    positives: readJevRules(elements.jevPositives),
    disqualifiers: readJevRules(elements.jevDisqualifiers),
    rating: {
      enabled: elements.jevRatingEnabled.checked,
      instruction: elements.jevRatingInstruction.value.trim(),
      criteria: elements.jevRatingCriteria.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
      minimum: toFiniteNumber(elements.jevRatingMinimum.value, 0),
    },
    transcript_min_tokens: Math.round(
      toFiniteNumber(elements.jevTranscriptMin.value, 1500),
    ),
    transcript_max_tokens: Math.round(
      toFiniteNumber(elements.jevTranscriptMax.value, 15000),
    ),
  };
}

async function saveJevSettings(event) {
  event.preventDefault();
  const submitButton = elements.jevForm.querySelector("button[type='submit']");
  if (submitButton) submitButton.disabled = true;
  setStatus("Saving rules...", "neutral", elements.jevStatus);
  try {
    const result = await request("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jev: collectJevConfig() }),
    });
    settings = result;
    renderJevSettings(settings.jev);
    setStatus("Rules saved", "success", elements.jevStatus);
  } catch (error) {
    setStatus(error.message, "error", elements.jevStatus);
  } finally {
    if (submitButton) submitButton.disabled = false;
  }
}

// Rules are worth testing before they are saved: a threshold that looks
// reasonable can quietly halve the feed. A threshold or name edit is answered
// instantly from the last run's own answers; a new question is asked again.
async function previewJevRules() {
  elements.jevPreviewButton.disabled = true;
  elements.jevPreviewResult.replaceChildren();
  setStatus("Testing your rules...", "neutral", elements.jevStatus);
  try {
    const body = await request("/api/rules/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectJevConfig()),
    });

    let preview = body?.preview;
    if (!preview) {
      const job = await followJob((running) =>
        setStatus(describeJob(running), "neutral", elements.jevStatus),
      );
      if (!job) throw new Error("The preview did not start.");
      if (job.state === "error") throw new Error(job.detail || "The preview failed.");
      preview = job.result?.preview;
    }
    if (!preview) throw new Error("The preview returned nothing.");
    renderRulePreview(preview);
    setStatus(preview.headline, "success", elements.jevStatus);
  } catch (error) {
    setStatus(error.message, "error", elements.jevStatus);
  } finally {
    elements.jevPreviewButton.disabled = false;
  }
}

function renderRulePreview(preview) {
  elements.jevPreviewResult.replaceChildren();
  const heading = document.createElement("p");
  heading.className = "jev-preview-headline";
  heading.textContent = preview.headline;
  elements.jevPreviewResult.append(heading);

  const note = document.createElement("p");
  note.className = "settings-hint";
  const cost = Number(preview.usage?.estimated_cost_usd || 0);
  const how =
    preview.mode === "reused"
      ? "Answered from the last run's own answers — nothing was sent to Jev."
      : `Jev was asked again about ${preview.previewed} videos` +
        (preview.with_transcript
          ? `, ${preview.with_transcript} of them with transcripts`
          : " without transcripts") +
        ` (~$${cost.toFixed(6)}).`;
  note.textContent = `${how} Nothing is saved until you press Save rules.`;
  elements.jevPreviewResult.append(note);

  const flips = (preview.changes || []).filter((change) => change.flipped);
  if (!flips.length) {
    const none = document.createElement("p");
    none.className = "jev-preview-none";
    none.textContent = "Every video keeps the verdict it has now.";
    elements.jevPreviewResult.append(none);
    return;
  }

  const list = document.createElement("ul");
  list.className = "jev-preview-list";
  flips.forEach((change) => {
    const item = document.createElement("li");
    item.className = change.now_approved ? "is-gained" : "is-lost";
    const label = document.createElement("span");
    label.className = "jev-preview-verdict";
    label.textContent = change.now_approved ? "would pass" : "would drop out";
    const title = document.createElement("span");
    title.textContent = ` ${change.title}`;
    const reason = document.createElement("span");
    reason.className = "jev-preview-reason";
    reason.textContent = ` — ${change.summary}`;
    item.append(label, title, reason);
    list.append(item);
  });
  elements.jevPreviewResult.append(list);
}

function restoreJevDefaults() {
  renderJevSettings(settings.default_jev);
  setStatus("Defaults loaded. Save to apply them.", "neutral", elements.jevStatus);
}

function addJevRule(container, kind) {
  container.append(createJevRuleRow(kind, {}));
  updateJevCounts();
}

async function addToWatchLater(video, button) {
  try {
    const result = await request("/api/watch-later", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(video),
    });
    if (!result.already_saved) {
      watchLater = [...watchLater, result.video];
    }
    // Only this card's buttons change, so a video that is playing keeps playing.
    const card = button?.closest(".video-card");
    if (card) {
      markCardWatchState(card, video);
    } else {
      render();
    }
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
      const card = elements.watchLaterList.querySelector(
        `.video-card[data-video-id="${videoId}"]`,
      );
      // Removing one saved video should not rebuild (and restart) the feed.
      card?.remove();
      elements.watchLaterCount.textContent = `${watchLater.filter(isVideo).length} saved`;
      if (!watchLater.length) render();
      setStatus("Removed from Watch later", "success");
    }
  } catch (error) {
    setStatus(error.message, "error");
  }
}

const JOB_POLL_MS = 700;

function describeJob(job) {
  const detail = typeof job?.detail === "string" ? job.detail : "";
  return detail ? `${job.step} — ${detail}` : job?.step || "Working...";
}

// Long pipelines run on the server so the page can report each stage instead of
// sitting on a silent request for minutes.
async function followJob(onUpdate) {
  for (;;) {
    const result = await request("/api/progress");
    const job = result?.job;
    if (!job) return null;
    if (onUpdate) onUpdate(job);
    if (job.state !== "running") return job;
    await new Promise((resolve) => setTimeout(resolve, JOB_POLL_MS));
  }
}

async function refreshFeed() {
  elements.refreshButton.disabled = true;
  setStatus("Starting refresh...");
  try {
    await request("/api/refresh", { method: "POST" });
    const job = await followJob((running) =>
      setStatus(describeJob(running), "neutral"),
    );
    if (!job) {
      setStatus("The refresh did not start", "error");
      return;
    }
    if (job.state === "error") {
      setStatus(job.detail || "The refresh failed", "error");
      return;
    }
    await loadCollections();
    setStatus(job.detail || "Feed refreshed", "success");
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
    await request("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const job = await followJob((running) =>
      setStatus(describeJob(running), "neutral"),
    );
    if (!job) {
      setStatus("The search did not start", "error");
      return;
    }
    if (job.state === "error") {
      setStatus(job.detail || "The search failed", "error");
      return;
    }
    await loadCollections();
    setStatus(job.detail || `Feed replaced for ${query}`, "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    elements.searchInput.disabled = false;
  }
}

// If the page is reloaded while a refresh is still running, pick the progress
// back up rather than leaving the feed looking frozen.
async function resumeRunningJob() {
  try {
    const result = await request("/api/progress");
    if (!result?.job || result.job.state !== "running") return false;
    elements.refreshButton.disabled = true;
    const job = await followJob((running) => setStatus(describeJob(running), "neutral"));
    elements.refreshButton.disabled = false;
    if (job && job.state === "done") {
      await loadCollections();
      setStatus(job.detail, "success");
    } else if (job && job.state === "error") {
      setStatus(job.detail || "That run failed", "error");
    }
    return true;
  } catch (error) {
    elements.refreshButton.disabled = false;
    return false;
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

// Cards are activated one at a time by a click (see activatePlayer), so there is
// no eager mounting step any more.
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
  queueWatchReport(videoId);
}

// The server keeps its own record of what was watched so a later refresh can
// stop offering it. Reports are batched and best-effort: losing one only means
// the video may come back once.
function queueWatchReport(videoId) {
  const entry = watchLog.find((item) => item.video_id === videoId);
  if (!entry || entry.seconds < MIN_WATCHED_SECONDS) return;

  watchReportQueue.set(videoId, {
    video_id: videoId,
    seconds: entry.seconds,
    title: entry.title,
  });
  if (watchReportTimer) return;
  watchReportTimer = window.setTimeout(flushWatchReports, 2000);
}

async function flushWatchReports() {
  watchReportTimer = null;
  if (!watchReportQueue.size) return;

  const entries = [...watchReportQueue.values()].slice(0, 50);
  watchReportQueue.clear();
  try {
    await request("/api/watched", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries }),
    });
    await loadWatched();
    applyWatchStateToCards();
  } catch (error) {
    // Reporting is a convenience; the local watch log still drives the banner.
  }
}

async function loadWatched() {
  try {
    const records = await request("/api/watched");
    watched = Array.isArray(records) ? records : [];
  } catch (error) {
    watched = [];
  }
}

// Anything already in the browser's own watch log is sent once, so upgrading to
// a server-side record does not start from an empty history.
async function backfillWatched() {
  const known = new Set(watched.map((record) => record.video_id));
  const entries = watchLog
    .filter(
      (entry) =>
        entry.seconds >= MIN_WATCHED_SECONDS &&
        !known.has(entry.video_id) &&
        VIDEO_ID_PATTERN.test(entry.video_id),
    )
    .slice(0, 50)
    .map((entry) => ({
      video_id: entry.video_id,
      seconds: entry.seconds,
      title: entry.title,
    }));
  if (!entries.length) return;

  try {
    await request("/api/watched", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries }),
    });
    await loadWatched();
  } catch (error) {
    // Nothing to recover: the live path will report the next watch anyway.
  }
}

function applyWatchStateToCards() {
  elements.feedList.querySelectorAll(".video-card").forEach((card) => {
    const video = feed.find((item) => item.video_id === card.dataset.videoId);
    if (video) markCardWatchState(card, video);
  });
  if (hideWatched) render();
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
elements.openJevButton.addEventListener("click", () =>
  elements.jevDialog.showModal(),
);
elements.jevForm.addEventListener("submit", saveJevSettings);
elements.jevPreviewButton.addEventListener("click", previewJevRules);
elements.jevAddPositive.addEventListener("click", () =>
  addJevRule(elements.jevPositives, "positives"),
);
elements.jevAddDisqualifier.addEventListener("click", () =>
  addJevRule(elements.jevDisqualifiers, "disqualifiers"),
);
elements.jevRestoreDefaults.addEventListener("click", restoreJevDefaults);
elements.jevPositives.addEventListener("change", updateJevCounts);
elements.jevDisqualifiers.addEventListener("change", updateJevCounts);
elements.watchLogForm.addEventListener("submit", (event) => {
  event.preventDefault();
  verifyWatchLogging({ force: true });
});
elements.watchLogBannerRecheck.addEventListener("click", () =>
  verifyWatchLogging({ force: true }),
);
elements.watchLogBannerFix.addEventListener("click", openYouTubeToFix);
elements.hideWatched.addEventListener("change", () => {
  hideWatched = elements.hideWatched.checked;
  try {
    window.localStorage.setItem(HIDE_WATCHED_STORAGE_KEY, String(hideWatched));
  } catch (error) {
    // Without storage the setting simply does not persist.
  }
  render();
});

// A tab left open across a refresh should not keep showing yesterday's feed.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    maybeAutoRefresh();
  }
});

// "/" jumps to search and "r" refreshes, as long as the user is not typing.
document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  const typing =
    target instanceof HTMLElement &&
    (target.isContentEditable ||
      ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
  if (typing || event.key === "Escape") return;

  if (event.key === "/") {
    event.preventDefault();
    elements.searchInput.focus();
  } else if (event.key === "r" && !elements.refreshButton.disabled) {
    event.preventDefault();
    refreshFeed();
  }
});

setInterval(() => {
  flushPlayback();
  verifyWatchLogging();
}, VERIFY_INTERVAL_MS);

// Watches recorded before this tab closes still make it to the server.
window.addEventListener("pagehide", () => {
  flushPlayback();
  if (watchReportQueue.size) flushWatchReports();
});

async function maybeAutoRefresh() {
  if (document.visibilityState !== "visible") return;
  const ranAt = Number(lastRun?.ran_at) * 1000;
  if (!ranAt || Date.now() - ranAt < STALE_FEED_MS) return;
  if (elements.refreshButton.disabled) return;
  setStatus("Feed is stale — refreshing...", "neutral");
  await refreshFeed();
}

loadCollections().then(
  async () => {
    elements.hideWatched.checked = hideWatched;
    setStatus("Ready");
    renderWatchLogStatus();
    verifyWatchLogging();
    await backfillWatched();
    if (await resumeRunningJob()) return;
    maybeAutoRefresh();
  },
  (error) => setStatus(error.message, "error"),
);
