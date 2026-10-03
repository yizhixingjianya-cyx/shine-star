/*
 * emoticon_manager 管理页面脚本（原生 JS，无任何第三方依赖）
 *
 * 通过 AstrBot Plugin Pages 桥接层（window.AstrBotPluginPage）与后端通信，
 * 桥接层 SDK 由 AstrBot Dashboard 自动注入，无需手动引入。
 */
const bridge = window.AstrBotPluginPage;

const MAX_SIZE = 8 * 1024 * 1024; // 与后端保持一致：8MB
const ALLOWED_EXT = /\.(jpe?g|png|gif|zip)$/i; // 允许的图片 / 压缩包后缀
const CONFIRM_MS = 3000; // 删除按钮二次确认时长（毫秒）

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
  return bytes + " B";
}

let statusTimer = null;
function showStatus(text, isError = false) {
  const el = $("uploadStatus");
  el.hidden = false;
  el.textContent = text;
  el.className = "upload-status" + (isError ? " error" : " ok");
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => {
    el.hidden = true;
  }, isError ? 6000 : 3000);
}

/* ---------------- 列表加载与渲染 ---------------- */
async function loadList() {
  try {
    const data = await bridge.apiGet("list", {});
    const files = Array.isArray(data.files) ? data.files : [];
    $("total").textContent = `共 ${files.length} 个`;
    renderGrid(files);
  } catch (err) {
    $("grid").innerHTML = "";
    $("empty").hidden = false;
    $("empty").innerHTML = `<p>❌ 加载失败</p><p class="hint">${escapeHtml(err.message || err)}</p>`;
  }
}

function renderGrid(files) {
  const grid = $("grid");
  grid.innerHTML = "";
  $("empty").hidden = files.length > 0;

  for (const f of files) {
    const card = document.createElement("div");
    card.className = "card";

    const previewHtml = f.preview
      ? `<img src="${f.preview}" alt="${escapeHtml(f.filename)}" loading="lazy" />`
      : `<div class="placeholder" title="文件超过预览大小限制">🖼️<br/><small>文件过大<br/>点击下载查看</small></div>`;

    card.innerHTML = `
      <div class="preview">${previewHtml}</div>
      <div class="meta">
        <div class="name" title="${escapeHtml(f.filename)}">${escapeHtml(f.filename)}</div>
        <div class="sub">${formatSize(f.size)}</div>
      </div>
      <div class="actions">
        ${f.preview ? "" : `<button class="btn small dl" data-filename="${escapeHtml(f.filename)}">下载</button>`}
        <button class="btn small danger del" data-filename="${escapeHtml(f.filename)}">删除</button>
      </div>`;
    grid.appendChild(card);
  }

  grid.querySelectorAll(".del").forEach((btn) => {
    btn.addEventListener("click", () => onDelete(btn));
  });
  grid.querySelectorAll(".dl").forEach((btn) => {
    btn.addEventListener("click", () => {
      bridge
        .download("file", { filename: btn.dataset.filename })
        .catch((err) => showStatus(`下载失败：${err.message || err}`, true));
    });
  });
}

/* ---------------- 删除（点击两次确认，避免误删） ---------------- */
function onDelete(btn) {
  if (btn.dataset.armed !== "1") {
    btn.dataset.armed = "1";
    btn.textContent = "确认删除？";
    clearTimeout(btn._timer);
    btn._timer = setTimeout(() => {
      btn.dataset.armed = "0";
      btn.textContent = "删除";
    }, CONFIRM_MS);
    return;
  }

  clearTimeout(btn._timer);
  btn.disabled = true;
  const filename = btn.dataset.filename;
  bridge
    .apiPost("delete", { filename })
    .then(() => {
      const card = btn.closest(".card");
      if (card) card.remove();
      showStatus(`✅ 已删除：${filename}`);
      loadList(); // 重新加载以刷新总数
    })
    .catch((err) => showStatus(`删除失败：${err.message || err}`, true))
    .finally(() => {
      btn.dataset.armed = "0";
      btn.textContent = "删除";
      btn.disabled = false;
    });
}

/* ---------------- 上传（点击 / 拖拽，支持多选） ---------------- */
function handleFiles(fileList) {
  if (typeof bridge.upload !== "function") {
    showStatus(
      "❌ 当前 AstrBot 版本不支持页面上传（桥接层缺少 upload 方法），" +
        "请升级 AstrBot 或先把图片放入 static/emoticons/ 目录",
      true,
    );
    return;
  }
  const files = Array.from(fileList || []);
  if (!files.length) return;

  let ok = 0;
  let fail = 0;
  (async () => {
    for (const file of files) {
      if (!ALLOWED_EXT.test(file.name)) {
        showStatus(`❌ ${file.name}：仅支持 jpg / jpeg / png / gif 与 zip`, true);
        fail++;
        continue;
      }
      if (file.size > MAX_SIZE) {
        showStatus(`❌ ${file.name}：超过 8MB 大小限制`, true);
        fail++;
        continue;
      }
      try {
        const result = await bridge.upload("upload", file);
        ok++;
        if (result && result.type === "zip") {
          const got = result.extracted_count || 0;
          const skip = result.skipped_count || 0;
          showStatus(
            got > 0
              ? `✅ ${file.name}：提取 ${got} 张${skip ? `，跳过 ${skip} 个` : ""}`
              : `❌ ${file.name}：未提取到有效图片${skip ? `（跳过 ${skip} 个）` : ""}`,
            got === 0,
          );
        } else {
          showStatus(`✅ 已上传：${result.filename || file.name}`);
        }
      } catch (err) {
        fail++;
        showStatus(`❌ ${file.name}：${err.message || err}`, true);
      }
    }
    showStatus(`上传完成：成功 ${ok} 个，失败 ${fail} 个${fail ? "" : " 🎉"}`);
    await loadList();
  })();
}

function init() {
  if (!bridge) {
    document.body.innerHTML =
      '<div class="empty"><p>❌ 无法连接 AstrBot 页面桥接层</p>' +
      '<p class="hint">请通过 AstrBot WebUI 的插件详情页打开本页面</p></div>';
    return;
  }

  const zone = $("uploadZone");
  const input = $("fileInput");

  zone.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => {
    handleFiles(e.target.files);
    input.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.add("dragging");
    }),
  );
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.remove("dragging");
    }),
  );
  zone.addEventListener("drop", (e) => handleFiles(e.dataTransfer.files));

  $("refreshBtn").addEventListener("click", loadList);

  // 等待桥接层就绪，读取页面上下文（插件名 / 页面标题 / 主题等）
  bridge
    .ready()
    .then((ctx) => {
      if (ctx && ctx.pageTitle) document.title = ctx.pageTitle;
    })
    .catch(() => {});

  loadList();
}

init();
