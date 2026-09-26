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
};

let feed = [];
let watchLater = [];
let trustedCreators = [];
let settings = { refresh_candidate_limit: 30, search_candidate_limit: 30 };

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
  const iframe = document.createElement("iframe");
  iframe.src = `https://www.youtube.com/embed/${encodeURIComponent(video.video_id)}`;
  iframe.title = video.title;
  iframe.loading = "lazy";
  iframe.referrerPolicy = "strict-origin-when-cross-origin";
  iframe.allow =
    "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture";
  iframe.allowFullscreen = true;
  player.append(iframe);

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
    actions.append(
      saveButton,
      createButton("Remove", "button-quiet", () => removeFromFeed(video.video_id)),
    );
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
}

function render() {
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

async function removeFromFeed(videoId) {
  try {
    const result = await request(`/api/feed/${encodeURIComponent(videoId)}`, {
      method: "DELETE",
    });
    if (result.removed) {
      feed = feed.filter((video) => video.video_id !== videoId);
      render();
      setStatus("Removed from Current feed", "success");
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
    setStatus(`${result.added} new videos added to Current feed`, "success");
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
      `${result.added} new videos added for ${result.query}`,
      "success",
    );
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    elements.searchInput.disabled = false;
  }
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

loadCollections().then(
  () => setStatus("Ready"),
  (error) => setStatus(error.message, "error"),
);
