"use strict";
const $ = (selector) => document.querySelector(selector);
let history = [];
let busy = false;
let token = sessionStorage.getItem("manualpilot-token") || "";
$("#api-token").value = token;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json();
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "请求参数无效，请检查输入";
    throw new Error(response.status === 401 ? "请在左侧连接设置填写服务访问令牌" : detail);
  }
  return data;
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  setTimeout(() => { $("#toast").hidden = true; }, 4500);
}

async function refresh() {
  try {
    const [status, documents] = await Promise.all([api("/api/status"), api("/api/documents")]);
    $("#connection-dot").style.background = "#b2ca8c";
    $("#connection-text").textContent = "服务已连接";
    $("#mode").textContent = status.llm_mode === "demo" ? "◉ 离线演示" : `◉ ${status.model}`;
    $("#device-mode").textContent = `设备 · ${status.serial_mode === "mock" ? "模拟" : "真实串口"}`;
    $("#doc-count").textContent = `${documents.length} 份文档`;
    $("#drop-zone small").textContent = `PDF / Word / TXT / MD · 最大 ${status.max_upload_mb} MB`;
    $("#disclaimer").textContent = status.llm_mode === "demo"
      ? "离线模式提供原文摘录与规则工具演示。真实问答需配置大模型 API。"
      : "回答基于检索资料与工具结果。使用前请核对引用和实际器件资料。";
    $("#document-list").replaceChildren();
    for (const doc of documents) {
      const row = element("div", "document-item");
      row.append(element("span", "doc-icon", doc.name.split(".").pop().toUpperCase().slice(0, 4)));
      const info = element("div", "doc-info");
      info.append(element("strong", "", doc.name), element("small", "", `${doc.chunks} 个片段 · 已索引`));
      row.append(info);
      const remove = element("button", "delete-document", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `删除 ${doc.name}`);
      remove.addEventListener("click", async () => {
        if (!confirm(`删除文档“${doc.name}”及其索引？`)) return;
        remove.disabled = true;
        try { await api(`/api/documents/${doc.id}`, { method: "DELETE" }); await refresh(); }
        catch (error) { toast(error.message); remove.disabled = false; }
      });
      row.append(remove);
      $("#document-list").append(row);
    }
  } catch (error) {
    $("#connection-dot").style.background = "#cf967e";
    $("#connection-text").textContent = "连接待检查";
    toast(error.message);
  }
}

async function uploadFiles(files) {
  for (const file of files) {
    $("#upload-status").textContent = `正在索引 ${file.name}…`;
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await api("/api/documents", { method: "POST", body: form });
      $("#upload-status").textContent = result.status === "unchanged"
        ? `${file.name} 未变化，已跳过` : `${file.name} 已索引 · 新增 ${result.embedded_chunks} / 复用 ${result.reused_chunks} 个向量`;
      await refresh();
    } catch (error) { $("#upload-status").textContent = error.message; }
  }
  $("#file-input").value = "";
}

$("#file-input").addEventListener("change", (event) => uploadFiles(event.target.files));
$("#drop-zone").addEventListener("dragover", (event) => {
  event.preventDefault(); $("#drop-zone").classList.add("dragover");
});
$("#drop-zone").addEventListener("dragleave", () => $("#drop-zone").classList.remove("dragover"));
$("#drop-zone").addEventListener("drop", (event) => {
  event.preventDefault(); $("#drop-zone").classList.remove("dragover"); uploadFiles(event.dataTransfer.files);
});
$("#save-token").addEventListener("click", () => {
  token = $("#api-token").value.trim();
  if (token) sessionStorage.setItem("manualpilot-token", token);
  else sessionStorage.removeItem("manualpilot-token");
  refresh();
});

function addMessage(role, text, extraClass = "") {
  $("#welcome")?.remove();
  const message = element("article", `message ${role} ${extraClass}`);
  const label = element("div", "message-label");
  label.append(element("b", "", role === "user" ? "YOU" : "M↗"),
    element("span", "", role === "user" ? "你" : "ManualPilot"));
  message.append(label, element("div", "message-content", text));
  $("#messages").append(message);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return message;
}

function showEvidence(result) {
  $("#source-count").textContent = result.sources.length;
  $("#tool-count").textContent = result.tool_calls.length;
  $("#sources-panel").replaceChildren();
  $("#tools-panel").replaceChildren();
  if (!result.sources.length) $("#sources-panel").append(element("p", "inspector-intro", "本轮未召回足够相关的片段。"));
  if (!result.tool_calls.length) $("#tools-panel").append(element("p", "inspector-intro", "本轮没有调用工具。"));
  for (const source of result.sources) {
    const card = element("details", "source-card");
    card.open = true;
    card.append(element("div", "source-label", `[${source.source_id}] · 相似度 ${(source.score * 100).toFixed(0)}%`),
      element("summary", "", `${source.name} · 第 ${source.page} 页`), element("p", "", source.text));
    $("#sources-panel").append(card);
  }
  for (const tool of result.tool_calls) {
    const card = element("details", `tool-card ${tool.status}`);
    card.open = true;
    card.append(element("div", "source-label", `${tool.status === "success" ? "已完成" : "未完成"} · ${tool.duration_ms} ms`),
      element("summary", "", tool.name), element("div", "tool-result-label", "调用参数"),
      element("pre", "", JSON.stringify(tool.arguments, null, 2)), element("div", "tool-result-label", "实际结果"),
      element("pre", "", JSON.stringify(tool.result, null, 2)));
    $("#tools-panel").append(card);
  }
  const timings = result.timings;
  $("#latency").hidden = false;
  $("#latency").replaceChildren(element("strong", "", `${(timings.total_ms / 1000).toFixed(2)} s`),
    element("div", "", `检索 ${timings.retrieval_ms} ms · 模型 ${timings.llm_ms} ms`),
    element("div", "", `工具 ${timings.tools_ms} ms · ${result.mode === "demo" ? "离线演示" : "API 实测"}`));
}

function selectTab(tools) {
  $("#sources-panel").hidden = tools;
  $("#tools-panel").hidden = !tools;
  $("#sources-tab").classList.toggle("selected", !tools);
  $("#tools-tab").classList.toggle("selected", tools);
}
$("#sources-tab").addEventListener("click", () => selectTab(false));
$("#tools-tab").addEventListener("click", () => selectTab(true));
document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => {
  $("#question").value = button.dataset.question;
  $("#question").focus();
}));

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("#question").value.trim();
  if (!question || busy) return;
  busy = true;
  $("#send").disabled = true;
  const allowControl = $("#allow-control").checked;
  $("#allow-control").checked = false;
  $("#question").value = "";
  addMessage("user", question);
  const pending = addMessage("assistant", "正在检索文档与处理任务…", "pending");
  try {
    const result = await api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history, allow_control: allowControl }) });
    pending.classList.remove("pending");
    pending.querySelector(".message-content").textContent = result.answer;
    pending.append(element("div", "message-meta", `${result.status === "completed" ? "已完成" : "需检查执行记录"} · ${(result.timings.total_ms / 1000).toFixed(2)} s · ${result.request_id.slice(0, 8)}`));
    showEvidence(result);
    history.push({ role: "user", content: question }, { role: "assistant", content: result.answer.slice(0, 4000) });
    history = history.slice(-6);
  } catch (error) {
    pending.classList.remove("pending"); pending.classList.add("error");
    pending.querySelector(".message-content").textContent = error.message;
  } finally {
    busy = false; $("#send").disabled = false;
    $("#messages").scrollTop = $("#messages").scrollHeight;
    $("#question").focus();
  }
});
$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); $("#chat-form").requestSubmit();
  }
});
refresh();
