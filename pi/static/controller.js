(function () {
  "use strict";

  const videoFeed = document.getElementById("video-feed");
  const streamOffline = document.getElementById("stream-offline");
  const shutterFlash = document.getElementById("shutter-flash");
  const toastEl = document.getElementById("toast");

  const ev3Dot = document.getElementById("ev3-dot");
  const ev3Text = document.getElementById("ev3-text");
  const barLeft = document.getElementById("bar-left");
  const barRight = document.getElementById("bar-right");

  const recPill = document.getElementById("rec-pill");
  const recTimer = document.getElementById("rec-timer");
  const btnAi = document.getElementById("btn-ai");
  const aiCount = document.getElementById("ai-count");
  const btnGallery = document.getElementById("btn-gallery");
  const mediaCount = document.getElementById("media-count");

  const joyBase = document.getElementById("joystick-base");
  const joyKnob = document.getElementById("joystick-knob");
  const driveReadout = document.getElementById("drive-readout");

  const btnPhoto = document.getElementById("btn-photo");
  const btnRecord = document.getElementById("btn-record");
  const btnStop = document.getElementById("btn-stop");

  const galleryDrawer = document.getElementById("gallery-drawer");
  const galleryGrid = document.getElementById("gallery-grid");
  const btnCloseGallery = document.getElementById("btn-close-gallery");
  const btnToggleSelect = document.getElementById("btn-toggle-select");
  const btnSelectAll = document.getElementById("btn-select-all");
  const batchBar = document.getElementById("batch-bar");
  const batchCount = document.getElementById("batch-count");
  const btnBatchShare = document.getElementById("btn-batch-share");
  const btnBatchZip = document.getElementById("btn-batch-zip");
  const btnBatchDelete = document.getElementById("btn-batch-delete");
  const filterTabs = document.querySelectorAll(".tab-btn");

  const previewModal = document.getElementById("preview-modal");
  const previewBackdrop = document.getElementById("preview-backdrop");
  const previewTitle = document.getElementById("preview-title");
  const previewBody = document.getElementById("preview-body");
  const btnClosePreview = document.getElementById("btn-close-preview");
  const btnDownloadMedia = document.getElementById("btn-download-media");
  const btnDeleteMedia = document.getElementById("btn-delete-media");

  let maxSpeed = 900;
  let isRecording = false;
  let activePointerId = null;
  let joyVector = { x: 0, y: 0 };
  let lastDriveSentAt = 0;
  let lastSentKey = "0.00,0.00";
  let pendingDriveTimer = null;
  let toastTimer = null;
  let toastItem = null;
  let currentFilter = "all";
  let allMediaItems = [];
  let previewItem = null;
  let selectMode = false;
  const selectedKeys = new Set();
  const heldKeys = [];

  const KEY_TO_VECTOR = {
    w: { x: 0, y: 1, label: "FWD" },
    s: { x: 0, y: -1, label: "REV" },
    a: { x: -1, y: 0, label: "LEFT" },
    d: { x: 1, y: 0, label: "RIGHT" },
  };

  async function apiRequest(url, method, body) {
    const opts = { method: method || "GET", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    return resp.json();
  }

  function showToast(message, item) {
    toastItem = item || null;
    toastEl.textContent = message;
    toastEl.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastEl.classList.add("hidden");
      toastItem = null;
    }, 3200);
  }

  function triggerShutterFlash() {
    shutterFlash.classList.add("flash");
    requestAnimationFrame(() => {
      requestAnimationFrame(() => shutterFlash.classList.remove("flash"));
    });
  }

  function formatTimer(totalSeconds) {
    const secs = Math.max(0, Math.floor(totalSeconds));
    const mm = String(Math.floor(secs / 60)).padStart(2, "0");
    const ss = String(secs % 60).padStart(2, "0");
    return "REC " + mm + ":" + ss;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function updateThrustBar(el, speed) {
    const ratio = Math.min(1, Math.abs(speed) / Math.max(1, maxSpeed));
    const halfPct = ratio * 50;
    if (speed >= 0) {
      el.style.left = "50%";
      el.style.width = halfPct + "%";
      el.classList.remove("rev");
    } else {
      el.style.left = 50 - halfPct + "%";
      el.style.width = halfPct + "%";
      el.classList.add("rev");
    }
  }

  function updateKnobVisual(nx, ny, active) {
    const maxOffset = Math.max(20, joyBase.clientWidth / 2 - joyKnob.offsetWidth / 2 - 4);
    const dx = nx * maxOffset;
    const dy = -ny * maxOffset;
    joyKnob.style.transform = "translate3d(" + dx.toFixed(1) + "px, " + dy.toFixed(1) + "px, 0)";
    joyBase.classList.toggle("active", Boolean(active));

    const mag = Math.hypot(nx, ny);
    if (mag < 0.08) {
      driveReadout.textContent = "IDLE";
    } else if (Math.abs(ny) >= Math.abs(nx)) {
      driveReadout.textContent = (ny > 0 ? "FWD " : "REV ") + Math.round(mag * 100) + "%";
    } else {
      driveReadout.textContent = (nx > 0 ? "RIGHT " : "LEFT ") + Math.round(mag * 100) + "%";
    }
  }

  function sendDrive(nx, ny, immediate) {
    joyVector = { x: nx, y: ny };
    const key = nx.toFixed(2) + "," + ny.toFixed(2);
    if (!immediate && key === lastSentKey) return;

    const now = performance.now();
    const elapsed = now - lastDriveSentAt;
    if (!immediate && elapsed < 50) {
      if (!pendingDriveTimer) {
        pendingDriveTimer = setTimeout(() => {
          pendingDriveTimer = null;
          sendDrive(joyVector.x, joyVector.y, true);
        }, 50 - elapsed);
      }
      return;
    }

    lastDriveSentAt = now;
    lastSentKey = key;
    if (Math.hypot(nx, ny) < 0.08) {
      apiRequest("/api/stop", "POST").catch(() => {});
    } else {
      apiRequest("/api/drive", "POST", { x: nx, y: ny }).catch(() => {});
    }
  }

  function stopBoat() {
    if (pendingDriveTimer) {
      clearTimeout(pendingDriveTimer);
      pendingDriveTimer = null;
    }
    heldKeys.length = 0;
    joyVector = { x: 0, y: 0 };
    lastSentKey = "0.00,0.00";
    updateKnobVisual(0, 0, false);
    apiRequest("/api/stop", "POST").catch(() => {});
  }

  function handleJoystickPointer(clientX, clientY) {
    const rect = joyBase.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const maxR = Math.max(24, rect.width / 2 - 22);
    let dx = clientX - cx;
    let dy = clientY - cy;
    const dist = Math.hypot(dx, dy);
    if (dist > maxR) {
      dx = (dx / dist) * maxR;
      dy = (dy / dist) * maxR;
    }
    const nx = dx / maxR;
    const ny = -dy / maxR;
    updateKnobVisual(nx, ny, true);
    sendDrive(nx, ny, false);
  }

  joyBase.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    activePointerId = e.pointerId;
    if (joyBase.setPointerCapture) {
      try { joyBase.setPointerCapture(e.pointerId); } catch (_err) {}
    }
    handleJoystickPointer(e.clientX, e.clientY);
  });

  joyBase.addEventListener("pointermove", (e) => {
    if (e.pointerId !== activePointerId) return;
    e.preventDefault();
    handleJoystickPointer(e.clientX, e.clientY);
  });

  function endJoystick(e) {
    if (e.pointerId !== activePointerId) return;
    activePointerId = null;
    stopBoat();
  }

  joyBase.addEventListener("pointerup", endJoystick);
  joyBase.addEventListener("pointercancel", endJoystick);

  window.addEventListener("blur", stopBoat);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopBoat();
  });

  // Keyboard support (WASD + Space + P/V/I/G/Esc)
  function applyKeyboardVector() {
    if (heldKeys.length === 0) {
      updateKnobVisual(0, 0, false);
      sendDrive(0, 0, true);
      return;
    }
    let sx = 0;
    let sy = 0;
    for (const k of heldKeys) {
      sx += KEY_TO_VECTOR[k].x;
      sy += KEY_TO_VECTOR[k].y;
    }
    const mag = Math.hypot(sx, sy);
    const nx = mag > 1 ? sx / mag : sx;
    const ny = mag > 1 ? sy / mag : sy;
    updateKnobVisual(nx, ny, true);
    sendDrive(nx, ny, true);
  }

  window.addEventListener("keydown", (e) => {
    const key = e.key.toLowerCase();
    if (key === "escape") {
      if (!previewModal.classList.contains("hidden")) closePreview();
      else if (!galleryDrawer.classList.contains("hidden")) closeGallery();
      else stopBoat();
      return;
    }
    if (!galleryDrawer.classList.contains("hidden") || !previewModal.classList.contains("hidden")) {
      return;
    }
    if (key === " ") {
      e.preventDefault();
      stopBoat();
      return;
    }
    if (e.repeat) return;
    if (KEY_TO_VECTOR[key]) {
      e.preventDefault();
      if (!heldKeys.includes(key)) heldKeys.push(key);
      applyKeyboardVector();
    } else if (key === "p") {
      takePhoto();
    } else if (key === "v") {
      toggleRecording();
    } else if (key === "i") {
      toggleAI();
    } else if (key === "g") {
      openGallery();
    }
  });

  window.addEventListener("keyup", (e) => {
    const key = e.key.toLowerCase();
    const idx = heldKeys.indexOf(key);
    if (idx !== -1) {
      heldKeys.splice(idx, 1);
      applyKeyboardVector();
    }
  });

  // Camera & AI Actions
  async function takePhoto() {
    triggerShutterFlash();
    try {
      const data = await apiRequest("/api/photo", "POST");
      if (data.ok && data.item) {
        showToast("Saved " + data.item.filename + " (tap to view)", data.item);
        refreshStatus();
      }
    } catch (_err) {
      showToast("Photo capture failed");
    }
  }

  async function toggleRecording() {
    try {
      if (!isRecording) {
        await apiRequest("/api/video/start", "POST");
        isRecording = true;
        showToast("Video recording started");
      } else {
        const data = await apiRequest("/api/video/stop", "POST");
        isRecording = false;
        if (data.item) {
          showToast("Saved " + data.item.filename + " (tap to view)", data.item);
        } else {
          showToast("Recording stopped");
        }
      }
      refreshStatus();
    } catch (_err) {
      showToast("Video recording error");
    }
  }

  async function toggleAI() {
    try {
      const data = await apiRequest("/api/ai", "POST", {});
      if (data.ok) {
        btnAi.classList.toggle("active", data.ai_enabled);
        btnAi.setAttribute("aria-pressed", String(data.ai_enabled));
        showToast(data.ai_enabled ? "AI Detection ON" : "AI Detection OFF");
      }
    } catch (_err) {}
  }

  btnPhoto.addEventListener("click", takePhoto);
  btnRecord.addEventListener("click", toggleRecording);
  btnStop.addEventListener("click", stopBoat);
  btnAi.addEventListener("click", toggleAI);
  toastEl.addEventListener("click", () => {
    if (toastItem) openPreview(toastItem);
  });

  // Gallery & Preview
  function itemKey(item) {
    return item.kind + "/" + item.filename;
  }

  function getFilteredItems() {
    return allMediaItems.filter(
      (item) => currentFilter === "all" || item.kind === currentFilter
    );
  }

  function getSelectedItems() {
    return allMediaItems.filter((item) => selectedKeys.has(itemKey(item)));
  }

  function updateBatchUI() {
    const filtered = getFilteredItems();
    const selected = getSelectedItems();
    const allFilteredSelected =
      filtered.length > 0 && filtered.every((item) => selectedKeys.has(itemKey(item)));

    btnToggleSelect.classList.toggle("active", selectMode);
    btnToggleSelect.textContent = selectMode ? "Cancel" : "Select";
    btnSelectAll.textContent = allFilteredSelected ? "Clear All" : "Select All";
    batchBar.classList.toggle("hidden", !selectMode && selected.length === 0);
    batchCount.textContent = selected.length + " selected";

    const hasSelection = selected.length > 0;
    btnBatchShare.disabled = !hasSelection;
    btnBatchZip.disabled = !hasSelection;
    btnBatchDelete.disabled = !hasSelection;
  }

  async function openGallery() {
    stopBoat();
    galleryDrawer.classList.remove("hidden");
    galleryDrawer.setAttribute("aria-hidden", "false");
    await loadGallery();
  }

  function closeGallery() {
    selectMode = false;
    selectedKeys.clear();
    galleryDrawer.classList.add("hidden");
    galleryDrawer.setAttribute("aria-hidden", "true");
  }

  async function loadGallery() {
    try {
      const data = await apiRequest("/api/media", "GET");
      allMediaItems = data.items || [];
      const validKeys = new Set(allMediaItems.map(itemKey));
      for (const k of Array.from(selectedKeys)) {
        if (!validKeys.has(k)) selectedKeys.delete(k);
      }
      mediaCount.textContent = String(allMediaItems.length);
      renderGallery();
    } catch (_err) {
      galleryGrid.innerHTML = '<div class="gallery-empty">Could not load media.</div>';
    }
  }

  function renderGallery() {
    const filtered = getFilteredItems();
    updateBatchUI();
    if (filtered.length === 0) {
      galleryGrid.innerHTML = '<div class="gallery-empty">No saved photos or videos yet.</div>';
      return;
    }
    galleryGrid.innerHTML = "";
    for (const item of filtered) {
      const key = itemKey(item);
      const isSelected = selectedKeys.has(key);

      const card = document.createElement("div");
      card.className = "media-card" + (isSelected ? " selected" : "");

      if (selectMode || selectedKeys.size > 0) {
        const check = document.createElement("div");
        check.className = "select-check";
        check.textContent = "✓";
        card.appendChild(check);
      }

      const thumb = document.createElement("div");
      thumb.className = "media-thumb";
      if (item.kind === "photos") {
        const img = document.createElement("img");
        img.src = item.url;
        img.loading = "lazy";
        img.alt = item.filename;
        thumb.appendChild(img);
      } else {
        const badge = document.createElement("span");
        badge.className = "video-badge";
        badge.textContent = "VIDEO";
        thumb.appendChild(badge);
      }

      const meta = document.createElement("div");
      meta.className = "media-meta";
      const name = document.createElement("div");
      name.className = "media-name";
      name.textContent = item.filename;
      const size = document.createElement("div");
      size.className = "media-size";
      size.textContent = formatBytes(item.size);
      meta.appendChild(name);
      meta.appendChild(size);

      card.appendChild(thumb);
      card.appendChild(meta);
      card.addEventListener("click", () => {
        if (selectMode || selectedKeys.size > 0) {
          if (selectedKeys.has(key)) selectedKeys.delete(key);
          else selectedKeys.add(key);
          selectMode = true;
          renderGallery();
        } else {
          openPreview(item);
        }
      });
      galleryGrid.appendChild(card);
    }
  }

  async function downloadBatchZip(items) {
    if (!items.length) return;
    showToast("Preparing ZIP (" + items.length + " files)...");
    try {
      const resp = await fetch("/api/media/batch-zip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: items.map((it) => ({ kind: it.kind, filename: it.filename })),
        }),
      });
      if (!resp.ok) throw new Error("zip_failed");
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "drone_boat_media_" + Date.now() + ".zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      showToast("Downloaded ZIP (" + items.length + " files)");
    } catch (_err) {
      showToast("ZIP download failed");
    }
  }

  async function saveSelectedToIphone() {
    const items = getSelectedItems();
    if (!items.length) return;

    // On iPhone Safari, Web Share API with files opens the native iOS sheet ("Save X Images/Videos")
    if (navigator.share && navigator.canShare) {
      try {
        showToast("Loading " + items.length + " file(s) for iPhone Save...");
        const files = [];
        for (const it of items) {
          const resp = await fetch(it.url);
          const blob = await resp.blob();
          const mime =
            blob.type || (it.kind === "photos" ? "image/jpeg" : "video/mp4");
          files.push(new File([blob], it.filename, { type: mime }));
        }
        if (navigator.canShare({ files })) {
          await navigator.share({
            files,
            title: "Drone Boat Media (" + files.length + ")",
          });
          showToast("Saved/Shared " + files.length + " file(s)");
          return;
        }
      } catch (err) {
        if (err && err.name === "AbortError") return;
      }
    }

    // Fallback: if single item, direct download; if multiple, download all as one ZIP
    if (items.length === 1) {
      const a = document.createElement("a");
      a.href = items[0].download_url;
      a.download = items[0].filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } else {
      await downloadBatchZip(items);
    }
  }

  async function deleteSelectedItems() {
    const items = getSelectedItems();
    if (!items.length) return;
    try {
      const res = await apiRequest("/api/media/batch-delete", "POST", {
        items: items.map((it) => ({ kind: it.kind, filename: it.filename })),
      });
      if (res.ok) {
        selectedKeys.clear();
        await loadGallery();
        refreshStatus();
        showToast("Deleted " + (res.deleted || items.length) + " item(s)");
      }
    } catch (_err) {
      showToast("Batch delete failed");
    }
  }

  function openPreview(item) {
    previewItem = item;
    previewTitle.textContent = item.filename;
    btnDownloadMedia.href = item.download_url;
    btnDownloadMedia.download = item.filename;
    previewBody.innerHTML = "";

    if (item.kind === "photos") {
      const img = document.createElement("img");
      img.src = item.url;
      img.alt = item.filename;
      previewBody.appendChild(img);
    } else if (item.filename.toLowerCase().endsWith(".mp4")) {
      const vid = document.createElement("video");
      vid.src = item.url;
      vid.controls = true;
      vid.playsInline = true;
      vid.autoplay = true;
      previewBody.appendChild(vid);
    } else {
      const note = document.createElement("div");
      note.className = "gallery-empty";
      note.textContent = "MJPEG clip saved. Tap Save to download.";
      previewBody.appendChild(note);
    }

    previewModal.classList.remove("hidden");
    previewModal.setAttribute("aria-hidden", "false");
  }

  function closePreview() {
    previewBody.innerHTML = "";
    previewItem = null;
    previewModal.classList.add("hidden");
    previewModal.setAttribute("aria-hidden", "true");
  }

  btnGallery.addEventListener("click", openGallery);
  btnCloseGallery.addEventListener("click", closeGallery);
  btnClosePreview.addEventListener("click", closePreview);
  previewBackdrop.addEventListener("click", closePreview);

  btnToggleSelect.addEventListener("click", () => {
    selectMode = !selectMode;
    if (!selectMode) selectedKeys.clear();
    renderGallery();
  });

  btnSelectAll.addEventListener("click", () => {
    const filtered = getFilteredItems();
    if (!filtered.length) return;
    selectMode = true;
    const allSelected = filtered.every((item) => selectedKeys.has(itemKey(item)));
    for (const item of filtered) {
      const k = itemKey(item);
      if (allSelected) selectedKeys.delete(k);
      else selectedKeys.add(k);
    }
    renderGallery();
  });

  btnBatchShare.addEventListener("click", saveSelectedToIphone);
  btnBatchZip.addEventListener("click", () => downloadBatchZip(getSelectedItems()));
  btnBatchDelete.addEventListener("click", deleteSelectedItems);

  btnDeleteMedia.addEventListener("click", async () => {
    if (!previewItem) return;
    const target = previewItem;
    try {
      const res = await apiRequest(
        "/api/media/" + target.kind + "/" + encodeURIComponent(target.filename),
        "DELETE"
      );
      if (res.ok) {
        closePreview();
        await loadGallery();
        refreshStatus();
        showToast("Deleted " + target.filename);
      }
    } catch (_err) {
      showToast("Delete failed");
    }
  });

  for (const tab of filterTabs) {
    tab.addEventListener("click", () => {
      for (const t of filterTabs) t.classList.remove("active");
      tab.classList.add("active");
      currentFilter = tab.dataset.filter;
      renderGallery();
    });
  }

  // Stream recovery & telemetry polling
  videoFeed.addEventListener("load", () => {
    streamOffline.classList.add("hidden");
  });
  videoFeed.addEventListener("error", () => {
    streamOffline.classList.remove("hidden");
    setTimeout(() => {
      videoFeed.src = "/video_feed?t=" + Date.now();
    }, 1500);
  });

  async function refreshStatus() {
    try {
      const data = await apiRequest("/api/status", "GET");
      const ev3 = data.ev3 || {};
      const cam = data.camera || {};

      maxSpeed = ev3.max_speed || 900;
      ev3Dot.className = "dot " + (ev3.connected ? "ok" : "off");
      ev3Text.textContent = ev3.connected ? "EV3" : "NO EV3";
      updateThrustBar(barLeft, ev3.left_speed || 0);
      updateThrustBar(barRight, ev3.right_speed || 0);

      if (cam.ready === false && cam.error) {
        streamOffline.classList.remove("hidden");
        streamOffline.innerHTML = "<span>" + cam.error + "</span>";
      }

      isRecording = Boolean(cam.recording);
      btnRecord.classList.toggle("recording", isRecording);
      recPill.classList.toggle("hidden", !isRecording);
      if (isRecording) {
        recTimer.textContent = formatTimer(cam.recording_seconds || 0);
      }

      btnAi.classList.toggle("active", Boolean(cam.ai_enabled));
      btnAi.setAttribute("aria-pressed", String(Boolean(cam.ai_enabled)));
      const detCount = cam.detection_count || 0;
      aiCount.textContent = String(detCount);
      aiCount.classList.toggle("hidden", !cam.ai_enabled || detCount === 0);

      mediaCount.textContent = String(data.media_count || 0);
    } catch (_err) {
      ev3Dot.className = "dot off";
      ev3Text.textContent = "OFFLINE";
    }
  }

  setInterval(() => {
    if (Math.hypot(joyVector.x, joyVector.y) >= 0.08) {
      apiRequest("/api/heartbeat", "POST").catch(() => {});
    }
  }, 400);

  setInterval(refreshStatus, 1000);
  refreshStatus();
})();
