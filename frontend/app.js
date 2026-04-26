(() => {
  const DEFAULT_API =
    location.hostname === "localhost" || location.hostname === "127.0.0.1"
      ? "http://localhost:8000"
      : "";

  // Allow overriding at build/deploy time by setting window.API_BASE in a
  // small inline script, or via ?api= query string for quick testing.
  const params = new URLSearchParams(location.search);
  const API_BASE =
    params.get("api") || window.API_BASE || DEFAULT_API || location.origin;

  const form = document.getElementById("urlForm");
  const urlInput = document.getElementById("urlInput");
  const statusEl = document.getElementById("status");
  const preview = document.getElementById("preview");
  const thumb = document.getElementById("thumb");
  const titleEl = document.getElementById("title");
  const channelEl = document.getElementById("channel");
  const durationEl = document.getElementById("duration");
  const formatSelect = document.getElementById("formatSelect");
  const qualitySelect = document.getElementById("qualitySelect");
  const qualityControl = document.getElementById("qualityControl");
  const downloadBtn = document.getElementById("downloadBtn");
  const fetchBtn = document.getElementById("fetchBtn");

  let currentUrl = "";

  const setStatus = (msg, isError = false) => {
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("error", !!isError);
  };

  const formatDuration = (s) => {
    if (!s || Number.isNaN(s)) return "";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.floor(s % 60);
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  };

  const populateQualities = (qualities) => {
    qualitySelect.innerHTML = "";
    const preferred = ["1080p", "720p", "480p", "360p"];
    const list = qualities && qualities.length ? qualities : preferred;
    list.forEach((q) => {
      const opt = document.createElement("option");
      opt.value = q;
      opt.textContent = q;
      qualitySelect.appendChild(opt);
    });
    // pick the highest <= 1080p by default
    const prefer = list.find((q) => parseInt(q, 10) <= 1080) || list[0];
    if (prefer) qualitySelect.value = prefer;
  };

  const fetchInfo = async (url) => {
    const resp = await fetch(
      `${API_BASE}/api/info?url=${encodeURIComponent(url)}`,
    );
    if (!resp.ok) {
      let msg = `Request failed (${resp.status})`;
      try {
        const data = await resp.json();
        if (data.detail) msg = data.detail;
      } catch (_) {}
      throw new Error(msg);
    }
    return resp.json();
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = urlInput.value.trim();
    if (!url) return;
    currentUrl = url;
    preview.hidden = true;
    setStatus("Fetching video info…");
    fetchBtn.disabled = true;
    try {
      const info = await fetchInfo(url);
      titleEl.textContent = info.title || "Untitled";
      channelEl.textContent = info.channel || "";
      const dur = formatDuration(info.duration);
      durationEl.textContent = dur
        ? `${dur}${info.is_short ? " · Short" : ""}`
        : info.is_short
          ? "Short"
          : "";
      thumb.src = info.thumbnail || "";
      thumb.alt = info.title || "";
      populateQualities(info.qualities);
      preview.hidden = false;
      setStatus("");
    } catch (err) {
      setStatus(err.message || "Failed to fetch video info", true);
    } finally {
      fetchBtn.disabled = false;
    }
  });

  formatSelect.addEventListener("change", () => {
    const isAudio = formatSelect.value === "audio";
    qualityControl.style.display = isAudio ? "none" : "flex";
  });

  downloadBtn.addEventListener("click", async () => {
    if (!currentUrl) return;
    const fmt = formatSelect.value;
    const quality = qualitySelect.value;
    const qs = new URLSearchParams({ url: currentUrl, format: fmt });
    if (fmt === "video" && quality) qs.set("quality", quality);

    downloadBtn.classList.add("loading");
    downloadBtn.disabled = true;
    setStatus(
      fmt === "audio"
        ? "Preparing audio… this can take a moment."
        : "Preparing video… larger qualities take longer.",
    );

    try {
      const resp = await fetch(`${API_BASE}/api/download?${qs.toString()}`);
      if (!resp.ok) {
        let msg = `Download failed (${resp.status})`;
        try {
          const data = await resp.json();
          if (data.detail) msg = data.detail;
        } catch (_) {}
        throw new Error(msg);
      }
      const blob = await resp.blob();
      const disp = resp.headers.get("Content-Disposition") || "";
      const match = /filename="?([^"]+)"?/.exec(disp);
      const filename =
        (match && match[1]) ||
        `${titleEl.textContent || "video"}.${fmt === "audio" ? "mp3" : "mp4"}`;

      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 10_000);
      setStatus("Saved to your device.");
    } catch (err) {
      setStatus(err.message || "Download failed", true);
    } finally {
      downloadBtn.classList.remove("loading");
      downloadBtn.disabled = false;
    }
  });
})();
