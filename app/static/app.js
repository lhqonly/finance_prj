const $ = (s) => document.querySelector(s);
let timer = null;
let batchId = null;

// 启动时检查后端依赖是否就绪
fetch("/api/status").then((r) => r.json()).then((s) => {
  const need = [];
  if (!s["yt-dlp"]) need.push("yt-dlp");
  if (!s["whisper"]) need.push("whisper");
  if (!s["ffmpeg"]) need.push("ffmpeg");
  if (!s["f2"]) need.push("f2(抖音用)");
  const env = $("#env");
  if (need.length) {
    env.innerHTML = `<span class="bad">⚠ 缺少依赖：${need.join("、")}。请先运行 scripts/setup.sh</span>`;
  } else {
    const ck = s.cookies ? "，已加载 cookies.txt" : "，未配置 cookies（B站AI字幕可能需要）";
    const fw = s["faster-whisper"] ? "，⚡ faster-whisper 加速已启用" : "，未装 faster-whisper（转写较慢）";
    env.innerHTML = `<span class="good">✓ 依赖就绪${ck}${fw}</span>`;
  }
});

$("#go").addEventListener("click", start);
$("#zip").addEventListener("click", () => {
  if (batchId) location.href = `/api/batches/${batchId}/zip`;
});

async function start() {
  const urls = $("#urls").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!urls.length) { alert("请粘贴至少一个视频链接"); return; }

  $("#go").disabled = true;
  $("#list").innerHTML = "";
  $("#overall").classList.remove("hidden");
  $("#zip").classList.add("hidden");

  const res = await fetch("/api/batches", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls, model: $("#model").value }),
  });
  if (!res.ok) { alert("提交失败"); $("#go").disabled = false; return; }
  batchId = (await res.json()).batch_id;

  if (timer) clearInterval(timer);
  timer = setInterval(poll, 1500);
  poll();
}

async function poll() {
  if (!batchId) return;
  const r = await fetch(`/api/batches/${batchId}`);
  if (!r.ok) return;
  const data = await r.json();

  $("#overallFill").style.width = data.overall + "%";
  $("#overallText").textContent =
    `总进度 ${data.overall}%（完成 ${data.done}/${data.total}${data.error ? `，失败 ${data.error}` : ""}）`;

  render(data.items);

  const finished = data.done + data.error === data.total;
  if (data.done > 0) $("#zip").classList.remove("hidden");
  if (finished) {
    clearInterval(timer); timer = null;
    $("#go").disabled = false;
  }
}

function render(items) {
  const list = $("#list");
  list.innerHTML = items.map((it, i) => {
    const cls = it.state === "done" ? "done" : it.state === "error" ? "error" : "";
    const stLabel = { queued: "排队", running: "处理中", done: "✓ 完成", error: "✗ 失败" }[it.state] || it.state;
    const dl = it.file
      ? `<a class="dl link" href="/api/download/${encodeURIComponent(it.file)}">⬇ 下载 ${escapeHtml(it.file)}</a>`
      : "";
    return `<li class="item">
      <div class="top">
        <span class="url">${i + 1}. ${escapeHtml(it.url)}</span>
        <span class="st ${cls}">${stLabel}</span>
      </div>
      <div class="msg">${escapeHtml(it.message || "")}</div>
      <div class="bar"><div class="fill ${cls}" style="width:${it.percent}%"></div></div>
      ${dl}
    </li>`;
  }).join("");
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
