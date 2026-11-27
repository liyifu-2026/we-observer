(() => {
  "use strict";

  const state = {
    token: new URLSearchParams(location.search).get("token") || sessionStorage.getItem("wx_obs_token") || "",
    accounts: [],
    selectedAccount: null,
    chatType: "all",
    chatQuery: "",
    chats: [],
    checkedChats: new Set(),
    watchRunning: true,
    cursorId: "ui",
    eventSource: null,
    chatsReqToken: 0,
    configReqToken: 0,
  };

  if (state.token) {
    sessionStorage.setItem("wx_obs_token", state.token);
  }

  const $ = (id) => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function showError(msg) {
    $("bannerErrorText").textContent = msg;
    $("bannerError").classList.remove("hidden");
  }

  function hideError() {
    $("bannerError").classList.add("hidden");
  }

  function setStatus(kind, text) {
    const pill = $("liveStatusPill");
    pill.className = "status-pill " + (kind === "live" ? "status-live" : kind === "error" ? "status-error" : "status-idle");
    $("liveStatusText").textContent = text;
  }

  function formatTime(ts) {
    if (!ts) return "";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  async function apiFetch(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (state.token) {
      headers["Authorization"] = `Bearer ${state.token}`;
    }
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    if (resp.status === 401) {
      $("tokenModal").classList.remove("hidden");
      throw new Error("需要访问令牌 (401)");
    }
    const data = await resp.json();
    if (!resp.ok || data.ok === false) {
      throw new Error(data.message || data.error || `请求失败 (${resp.status})`);
    }
    return data;
  }

  function renderAccounts() {
    const select = $("accountSelect");
    select.textContent = "";
    if (!state.accounts.length) {
      const opt = el("option", "", "未检测到微信账号");
      opt.value = "";
      select.appendChild(opt);
      return;
    }
    for (const acct of state.accounts) {
      const tag = acct.online ? `在线 PID:${acct.pids.join(",")}` : "离线";
      const label = acct.display_name && acct.display_name !== acct.account_id
        ? `${acct.display_name} (${acct.account_id}) [${tag}]`
        : `${acct.account_id} [${tag}]`;
      const opt = el("option", "", label);
      opt.value = acct.account_id;
      if (acct.account_id === state.selectedAccount) {
        opt.selected = true;
      }
      select.appendChild(opt);
    }
  }

  async function loadAccountsAndAutoSelect() {
    hideError();
    const data = await apiFetch("/api/accounts");
    state.accounts = data.accounts || [];
    const online = state.accounts.filter((a) => a.online);

    let target = state.selectedAccount;
    if (!target || !state.accounts.some((a) => a.account_id === target)) {
      target = online.length === 1 ? online[0].account_id : (data.selected_account || (state.accounts[0] && state.accounts[0].account_id) || null);
    }

    state.selectedAccount = target;
    renderAccounts();

    if (target) {
      const chosen = state.accounts.find((a) => a.account_id === target);
      if (chosen && (chosen.online || online.length === 0)) {
        await selectAccount(target, chosen.pids && chosen.pids.length === 1 ? chosen.pids[0] : null);
      } else if (online.length > 1) {
        setStatus("idle", "请在左上角选择在线账号");
      }
    } else {
      setStatus("error", data.reason || "未检测到微信账号");
      if (data.reason) showError(data.reason);
    }
  }

  async function selectAccount(accountId, pid = null) {
    hideError();
    stopStream();
    state.checkedChats.clear();
    updateCheckedCounter();

    const res = await apiFetch("/api/accounts/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account: accountId, pid }),
    });
    state.selectedAccount = res.account.account_id;
    for (const a of state.accounts) {
      if (a.account_id === res.account.account_id) {
        a.display_name = res.account.display_name || a.display_name;
      }
    }
    renderAccounts();
    $("watchMetaInfo").textContent = `已解锁库 ${res.keyed_dbs}/${res.total_dbs}`;

    const stRes = await apiFetch(`/api/status?cursor_id=${encodeURIComponent(state.cursorId)}&account=${encodeURIComponent(state.selectedAccount)}`);
    if (stRes.status) {
      syncControlsFromStatus(stRes.status);
    }
    await loadChats();
    if (state.watchRunning) {
      startStream();
    }
  }

  function updateCheckedCounter() {
    const count = state.checkedChats.size;
    $("checkedCountText").textContent = count > 0 ? `已勾选 ${count} 个会话` : "未勾选（按右侧范围监控）";
    $("clearCheckedBtn").classList.toggle("hidden", count === 0);
  }

  function syncControlsFromStatus(st) {
    const wf = (st && st.watch_filter) || {};
    if (wf.mode) {
      $("watchModeSelect").value = wf.mode;
    }
    if (typeof wf.include_self === "boolean") {
      $("watchIncludeSelf").checked = wf.include_self;
    }
    if (wf.mode === "allowlist" && Array.isArray(wf.chats)) {
      state.checkedChats = new Set(wf.chats);
    } else if (wf.mode !== "allowlist") {
      state.checkedChats.clear();
    }
    updateCheckedCounter();
  }

  function renderChats() {
    const list = $("chatList");
    list.textContent = "";
    if (!state.chats.length) {
      list.appendChild(el("div", "empty-state", "暂无匹配的会话"));
      return;
    }

    for (const chat of state.chats) {
      const checked = state.checkedChats.has(chat.chat_id);
      const row = el("div", "chat-item" + (checked ? " checked" : ""));
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = checked;

      const main = el("div", "chat-item-main");
      const top = el("div", "chat-item-top");
      const titleWrap = el("div", "chat-title-wrap");
      const kindTag = el("span", "kind-tag" + (chat.chat_type === "group" ? " group" : ""), chat.chat_type === "group" ? "群" : "私");
      titleWrap.append(kindTag, el("span", "chat-name", chat.display_name || chat.chat_id));
      top.append(titleWrap, el("span", "muted", formatTime(chat.last_timestamp)));
      main.append(top, el("div", "chat-summary", chat.summary || chat.chat_id));

      const toggle = () => {
        if (state.checkedChats.has(chat.chat_id)) {
          state.checkedChats.delete(chat.chat_id);
        } else {
          state.checkedChats.add(chat.chat_id);
        }
        $("watchModeSelect").value = state.checkedChats.size > 0 ? "allowlist" : "all";
        updateCheckedCounter();
        renderChats();
        applyWatchRules().catch((e) => showError(e.message));
      };

      cb.addEventListener("click", (ev) => {
        ev.stopPropagation();
        toggle();
      });
      row.addEventListener("click", toggle);

      row.append(cb, main);
      list.appendChild(row);
    }
  }

  async function loadChats() {
    if (!state.selectedAccount) return;
    const token = ++state.chatsReqToken;
    const qs = new URLSearchParams({
      account: state.selectedAccount,
      type: state.chatType,
      limit: "200",
    });
    if (state.chatQuery.trim()) {
      qs.set("query", state.chatQuery.trim());
    }
    const data = await apiFetch(`/api/chats?${qs.toString()}`);
    if (token !== state.chatsReqToken) return;
    state.chats = data.chats || [];
    renderChats();
  }

  function buildMessageCard(msg) {
    const card = el("div", "msg-card" + (msg.is_self ? " is-self" : ""));
    const meta = el("div", "msg-meta");
    const left = el("div", "msg-meta-left");

    const sender = msg.sender_name || msg.sender_id || (msg.is_self ? "我" : "未知发送者");
    left.appendChild(el("span", "msg-sender", sender));
    left.appendChild(el("span", "msg-chat", `${msg.chat_type === "group" ? "群" : "私"} · ${msg.chat_name || msg.chat_id}`));
    left.appendChild(el("span", "msg-type", msg.msg_type || "text"));
    meta.append(left, el("span", "muted", formatTime(msg.timestamp) || msg.iso_time || ""));
    card.appendChild(meta);

    if (msg.quote && msg.quote.excerpt) {
      const qSender = msg.quote.sender_name || msg.quote.sender_id || "";
      card.appendChild(el("div", "msg-quote", qSender ? `${qSender}: ${msg.quote.excerpt}` : msg.quote.excerpt));
    }

    card.appendChild(el("div", "msg-body", msg.text || `[${msg.msg_type}]`));

    if (msg.metadata && Object.keys(msg.metadata).length > 0) {
      const parts = [];
      if (msg.metadata.filename) parts.push(`file=${msg.metadata.filename}`);
      if (msg.metadata.duration_ms) parts.push(`duration=${Math.round(msg.metadata.duration_ms / 1000)}s`);
      if (msg.metadata.duration_sec) parts.push(`duration=${msg.metadata.duration_sec}s`);
      if (msg.metadata.url) parts.push(`url=${msg.metadata.url}`);
      if (parts.length) {
        card.appendChild(el("div", "msg-extra", parts.join(" | ")));
      }
    }
    return card;
  }

  function appendWatchMessage(msg) {
    const feed = $("watchFeed");
    const empty = feed.querySelector(".empty-state");
    if (empty) empty.remove();
    const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
    feed.appendChild(buildMessageCard(msg));
    while (feed.children.length > 500) {
      feed.removeChild(feed.firstElementChild);
    }
    if (nearBottom) {
      feed.scrollTop = feed.scrollHeight;
    }
  }

  function stopStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    if (!state.watchRunning) {
      setStatus("idle", "监控已暂停");
    }
  }

  function startStream() {
    if (!state.selectedAccount) return;
    stopStream();
    const interval = $("watchIntervalSelect").value || "1.0";
    const qs = new URLSearchParams({
      cursor_id: state.cursorId,
      interval,
      account: state.selectedAccount,
    });
    if (state.token) {
      qs.set("token", state.token);
    }
    setStatus("idle", "连接中...");
    const es = new EventSource(`/api/watch/stream?${qs.toString()}`);
    state.eventSource = es;

    es.addEventListener("ready", (ev) => {
      if (state.eventSource !== es) return;
      try {
        const info = JSON.parse(ev.data);
        const st = info.status || {};
        setStatus("live", `实时监控中 (${st.tracked_chats || 0} 会话)`);
      } catch {
        setStatus("live", "实时监控中");
      }
    });

    es.addEventListener("message", (ev) => {
      if (state.eventSource !== es) return;
      try {
        appendWatchMessage(JSON.parse(ev.data));
      } catch {
        // ignore
      }
    });

    es.addEventListener("error", (ev) => {
      if (state.eventSource !== es) return;
      if (ev.data) {
        try {
          const err = JSON.parse(ev.data);
          showError(err.message || err.error || "监控异常");
          setStatus("error", "监控异常");
          stopStream();
          return;
        } catch {
          // ignore
        }
      }
      setStatus(es.readyState === EventSource.CLOSED ? "error" : "idle", es.readyState === EventSource.CLOSED ? "连接已断开" : "重连中...");
    });
  }

  async function applyWatchRules() {
    if (!state.selectedAccount) return;
    hideError();
    const mode = $("watchModeSelect").value;
    if (mode === "allowlist" && state.checkedChats.size === 0) {
      showError("已在“仅已勾选会话”模式，请在左侧勾选至少一个私聊或群聊。");
      return;
    }
    const token = ++state.configReqToken;
    const includeSelf = $("watchIncludeSelf").checked;
    const chats = mode === "allowlist" ? Array.from(state.checkedChats) : [];
    const res = await apiFetch("/api/watch/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account: state.selectedAccount,
        cursor_id: state.cursorId,
        mode,
        chats,
        include_self: includeSelf,
      }),
    });
    if (token !== state.configReqToken) return;
    const st = res.status || {};
    $("watchMetaInfo").textContent = `已解锁 ${st.keyed_dbs}/${st.total_dbs} · 模式:${mode}`;
  }

  function bindEvents() {
    $("bannerDismissBtn").addEventListener("click", hideError);

    $("refreshAccountsBtn").addEventListener("click", () => {
      loadAccountsAndAutoSelect().catch((e) => showError(e.message));
    });

    $("accountSelect").addEventListener("change", (ev) => {
      const acctId = ev.target.value;
      if (!acctId) return;
      const acct = state.accounts.find((a) => a.account_id === acctId);
      const pid = acct && acct.pids && acct.pids.length === 1 ? acct.pids[0] : null;
      selectAccount(acctId, pid).catch((e) => showError(e.message));
    });

    $("toggleWatchBtn").addEventListener("click", () => {
      state.watchRunning = !state.watchRunning;
      $("toggleWatchBtn").textContent = state.watchRunning ? "暂停" : "继续";
      if (state.watchRunning) {
        startStream();
      } else {
        stopStream();
      }
    });

    $("tokenForm").addEventListener("submit", (ev) => {
      ev.preventDefault();
      state.token = $("tokenInput").value.trim();
      sessionStorage.setItem("wx_obs_token", state.token);
      $("tokenModal").classList.add("hidden");
      loadAccountsAndAutoSelect().catch((e) => showError(e.message));
    });

    for (const btn of $("chatTypeTabs").querySelectorAll(".seg")) {
      btn.addEventListener("click", () => {
        for (const b of $("chatTypeTabs").querySelectorAll(".seg")) b.classList.remove("active");
        btn.classList.add("active");
        state.chatType = btn.dataset.type;
        loadChats().catch((e) => showError(e.message));
      });
    }

    let queryTimer = null;
    $("chatQueryInput").addEventListener("input", () => {
      clearTimeout(queryTimer);
      queryTimer = setTimeout(() => {
        state.chatQuery = $("chatQueryInput").value;
        loadChats().catch((e) => showError(e.message));
      }, 180);
    });

    $("refreshChatsBtn").addEventListener("click", () => {
      loadChats().catch((e) => showError(e.message));
    });

    $("clearCheckedBtn").addEventListener("click", () => {
      state.checkedChats.clear();
      if ($("watchModeSelect").value === "allowlist") {
        $("watchModeSelect").value = "all";
      }
      updateCheckedCounter();
      renderChats();
      applyWatchRules().catch((e) => showError(e.message));
    });

    $("watchModeSelect").addEventListener("change", () => {
      if ($("watchModeSelect").value !== "allowlist" && state.checkedChats.size > 0) {
        state.checkedChats.clear();
        updateCheckedCounter();
        renderChats();
      }
      applyWatchRules().catch((e) => showError(e.message));
    });

    $("watchIncludeSelf").addEventListener("change", () => {
      applyWatchRules().catch((e) => showError(e.message));
    });

    $("watchIntervalSelect").addEventListener("change", () => {
      if (state.watchRunning) {
        startStream();
      }
    });

    $("clearFeedBtn").addEventListener("click", () => {
      const feed = $("watchFeed");
      feed.textContent = "";
      feed.appendChild(el("div", "empty-state", "屏显已清空，继续等待新消息..."));
    });
  }

  bindEvents();
  loadAccountsAndAutoSelect().catch((e) => showError(e.message));
})();
