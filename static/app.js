/* Claude Code session dashboard — vanilla, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  tab: "live",
  live: [],
  sessions: [],
  projects: [],
  mode: "filter",
  detailId: null,
};

/* ---------------- formatting ---------------- */

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );

const int = (n) => (n ?? 0).toLocaleString("ko-KR");

const money = (n) =>
  n == null ? "—" : n >= 10 ? `$${Math.round(n).toLocaleString("ko-KR")}` : `$${n.toFixed(2)}`;

function tokens(n) {
  n = n ?? 0;
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return String(n);
}

function duration(seconds) {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}시간 ${m}분` : `${m}분`;
}

function ago(ms) {
  if (ms == null) return "—";
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return "방금";
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return `${Math.floor(s / 86400)}일 전`;
}

const agoIso = (iso) => (iso ? ago(Date.parse(iso)) : "—");

function clock(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const pct = (used, limit) => (limit ? Math.min(100, (used / limit) * 100) : 0);
const heat = (ratio) => (ratio >= 85 ? "hot" : ratio >= 60 ? "warn" : "");

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${response.status} ${path}`);
  return response.json();
}

/* ---------------- charts ---------------- */

function areaChart(series, limit) {
  if (series.length < 2) return '<p class="hint">턴이 부족해 그래프를 그릴 수 없습니다.</p>';

  const W = 720;
  const H = 180;
  const pad = { top: 12, right: 12, bottom: 20, left: 52 };
  const innerW = W - pad.left - pad.right;
  const innerH = H - pad.top - pad.bottom;

  // Scale to the curve, not the limit: a 1M window would flatten a 200k session.
  const peak = Math.max(...series.map((d) => d.tokens));
  const top = peak * 1.12;

  const x = (i) => pad.left + (i / (series.length - 1)) * innerW;
  const y = (v) => pad.top + innerH - (v / top) * innerH;

  const line = series.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.tokens).toFixed(1)}`).join(" ");
  const area = `${line} L${x(series.length - 1).toFixed(1)},${pad.top + innerH} L${pad.left},${pad.top + innerH} Z`;

  const ticks = [0, 0.5, 1].map((f) => {
    const value = top * f;
    const py = y(value);
    return `<line class="grid" x1="${pad.left}" y1="${py}" x2="${W - pad.right}" y2="${py}"></line>
      <text x="${pad.left - 8}" y="${py + 3}" text-anchor="end">${tokens(Math.round(value))}</text>`;
  });

  const limitLine =
    limit && limit <= top
      ? `<line class="limit" x1="${pad.left}" y1="${y(limit)}" x2="${W - pad.right}" y2="${y(limit)}"></line>
         <text x="${W - pad.right}" y="${y(limit) - 5}" text-anchor="end">한계 ${tokens(limit)}</text>`
      : "";

  const last = series[series.length - 1];

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="컨텍스트 사용량 추이">
    ${ticks.join("")}
    <path class="area" d="${area}"></path>
    <path class="line" d="${line}"></path>
    ${limitLine}
    <circle class="dot-end" cx="${x(series.length - 1).toFixed(1)}" cy="${y(last.tokens).toFixed(1)}" r="3"></circle>
    <text x="${pad.left}" y="${H - 5}">턴 1</text>
    <text x="${W - pad.right}" y="${H - 5}" text-anchor="end">턴 ${series.length}</text>
  </svg>`;
}

function dailyChart(daily, key, label) {
  if (!daily.length) return '<p class="hint">데이터가 없습니다.</p>';

  const W = 720;
  const H = 150;
  const pad = { top: 12, right: 12, bottom: 22, left: 52 };
  const innerW = W - pad.left - pad.right;
  const innerH = H - pad.top - pad.bottom;

  const top = Math.max(...daily.map((d) => d[key])) || 1;
  const slot = innerW / daily.length;
  const width = Math.max(2, slot - 3);

  const bars = daily
    .map((d, i) => {
      const height = (d[key] / top) * innerH;
      const bx = pad.left + i * slot + (slot - width) / 2;
      const by = pad.top + innerH - height;
      const dim = i === daily.length - 1 ? "" : "";
      return `<rect class="bar ${dim}" x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${width.toFixed(1)}" height="${Math.max(1, height).toFixed(1)}" rx="1">
        <title>${d.date} · ${int(d[key])} ${label}</title>
      </rect>`;
    })
    .join("");

  const step = Math.ceil(daily.length / 6);
  const labels = daily
    .map((d, i) =>
      i % step === 0 || i === daily.length - 1
        ? `<text x="${(pad.left + i * slot + slot / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle">${d.date.slice(5)}</text>`
        : ""
    )
    .join("");

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="일별 ${label}">
    <line class="grid" x1="${pad.left}" y1="${pad.top}" x2="${W - pad.right}" y2="${pad.top}"></line>
    <text x="${pad.left - 8}" y="${pad.top + 3}" text-anchor="end">${tokens(top)}</text>
    <line class="grid" x1="${pad.left}" y1="${pad.top + innerH}" x2="${W - pad.right}" y2="${pad.top + innerH}"></line>
    <text x="${pad.left - 8}" y="${pad.top + innerH + 3}" text-anchor="end">0</text>
    ${bars}${labels}
  </svg>`;
}

function barList(rows, valueKey, format = int) {
  if (!rows.length) return '<p class="hint">데이터가 없습니다.</p>';
  const top = Math.max(...rows.map((r) => r[valueKey])) || 1;
  return `<div class="bars">${rows
    .map(
      (r) => `<div class="bar-row">
        <div class="bar-head"><span>${esc(r.name)}</span><span>${format(r[valueKey])}</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${((r[valueKey] / top) * 100).toFixed(1)}%"></div></div>
      </div>`
    )
    .join("")}</div>`;
}

/* ---------------- live ---------------- */

function renderLive() {
  const host = $("#live-cards");
  const sessions = state.live;

  $("#live-hint").textContent = sessions.length
    ? `${sessions.length}개 실행 중 · ${sessions.filter((s) => s.status === "busy").length}개 작업 중`
    : "";

  if (!sessions.length) {
    host.innerHTML = '<div class="empty">실행 중인 세션이 없습니다.</div>';
    return;
  }

  host.innerHTML = sessions
    .map((s) => {
      const context = s.context || {};
      const ratio = pct(context.tokens, context.limit);
      return `<button class="card ${esc(s.status)}" data-session="${esc(s.sessionId)}">
        <div class="card-top">
          <span class="card-name">${esc(s.name)}</span>
          <span class="badge ${esc(s.status)}">${esc(s.status)}</span>
        </div>
        <div class="card-title">${esc(s.title || s.lastPrompt || "새 세션")}</div>
        <div class="meta-row"><span>${esc(s.project)}</span><span>${esc(s.branch || "—")}</span></div>
        <div class="gauge"><i class="${heat(ratio)}" style="width:${ratio.toFixed(1)}%"></i></div>
        <div class="meta-row">
          <span>ctx ${tokens(context.tokens)} · ${ratio.toFixed(0)}%</span>
          <span>${ago(s.updatedAt)}</span>
        </div>
        <div class="meta-row">
          <span>tools ${int(s.toolCalls)} · out ${tokens(s.output)}</span>
          <span>v${esc(s.version)}</span>
        </div>
      </button>`;
    })
    .join("");
}

/* ---------------- sessions ---------------- */

// A session the user named with /rename leads with that name; the derived
// title still follows when it says something different.
function sessionLabel(s) {
  const named = s.name ? `<span class="named">${esc(s.name)}</span>` : "";
  const title = !s.name || s.name !== s.title ? esc(s.title) : "";
  return [named, title].filter(Boolean).join(" ");
}

function sessionRow(s) {
  return `<button class="row" data-session="${esc(s.sessionId)}">
    <div class="row-title">${sessionLabel(s)}${s.archived ? ' <span class="pill expired">원본 만료</span>' : ""}</div>
    <div class="row-meta">
      <span class="pill">${esc(s.project)}</span>
      <span>${esc(s.branch || "—")}</span>
      <span>${clock(s.start)}</span>
      <span>활성 ${duration(s.activeSec)}</span>
    </div>
    <div class="row-right">
      ${int(s.userMsgs)}·${int(s.assistantMsgs)} msg<br>
      ${int(s.toolCalls)} tools · ${tokens(s.tokens.output)} out
    </div>
  </button>`;
}

async function loadSessions() {
  const params = new URLSearchParams();
  if (state.mode === "filter" && $("#q").value.trim()) params.set("q", $("#q").value.trim());
  if ($("#project").value) params.set("project", $("#project").value);
  if ($("#days").value) params.set("days", $("#days").value);
  if ($("#nonempty").checked) params.set("nonempty", "1");

  const data = await api(`/api/sessions?${params}`);
  state.sessions = data.sessions;

  if (!state.projects.length) {
    state.projects = data.projects;
    $("#project").insertAdjacentHTML(
      "beforeend",
      data.projects.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("")
    );
  }

  $("#sessions-hint").textContent =
    `${int(data.total)} / ${int(data.indexed)}개` +
    (data.archived ? ` · 아카이브 ${int(data.archived)}개` : "");
  $("#sessions-body").innerHTML = data.sessions.length
    ? `<div class="rows">${data.sessions.map(sessionRow).join("")}</div>`
    : '<div class="empty">조건에 맞는 세션이 없습니다.</div>';
}

function highlight(text, query) {
  const safe = esc(text);
  if (!query) return safe;
  const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  return safe.replace(pattern, (match) => `<mark>${match}</mark>`);
}

async function runSearch() {
  const query = $("#q").value.trim();
  if (query.length < 2) {
    $("#sessions-body").innerHTML = '<div class="empty">두 글자 이상 입력하세요.</div>';
    return;
  }

  $("#sessions-body").innerHTML = '<div class="empty">검색 중…</div>';
  const params = new URLSearchParams({ q: query });
  if ($("#project").value) params.set("project", $("#project").value);
  const data = await api(`/api/search?${params}`);

  $("#sessions-hint").textContent = `${data.total}개 세션 · ${data.scanned}개 파일 · ${data.elapsed}s`;
  $("#sessions-body").innerHTML = data.results.length
    ? `<div class="rows">${data.results
        .map(
          (r) => `<button class="row" data-session="${esc(r.sessionId)}">
            <div class="row-title">${sessionLabel(r)}</div>
            <div class="row-meta">
              <span class="pill">${esc(r.project)}</span>
              <span>${esc(r.branch || "—")}</span>
              <span>${clock(r.start)}</span>
            </div>
          </button>
          ${r.matches
            .map(
              (m) => `<div class="hit"><b>${esc(m.role)}</b>${highlight(m.snippet, data.query)}</div>`
            )
            .join("")}`
        )
        .join("")}</div>`
    : '<div class="empty">일치하는 세션이 없습니다.</div>';
}

/* ---------------- detail ---------------- */

async function openDetail(sessionId) {
  state.detailId = sessionId;
  show("detail", { updateHash: false });
  $("#detail-title").textContent = "불러오는 중…";
  $("#detail-body").innerHTML = "";

  const data = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
  const s = data.summary;
  $("#detail-title").textContent =
    s.name && s.name !== s.title ? `${s.name} — ${s.title}` : s.name || s.title;

  const expired = data.expired;
  const ratio = pct(s.contextTokens, s.contextLimit);
  const toolRows = Object.entries(s.tools)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 12);

  const tiles = [
    ["컨텍스트", `${ratio.toFixed(0)}%`, `${int(s.contextTokens)} / ${tokens(s.contextLimit)}`],
    ["최대 컨텍스트", tokens(s.contextPeak), int(s.contextPeak)],
    ["출력 토큰", tokens(s.tokens.output), `입력 ${tokens(s.tokens.input)}`],
    ["캐시 읽기", tokens(s.tokens.cacheRead), `생성 ${tokens(s.tokens.cacheCreation)}`],
    ["메시지", int(s.userMsgs + s.assistantMsgs), `프롬프트 ${int(s.userMsgs)}`],
    ["툴 호출", int(s.toolCalls), `서브에이전트 ${int(s.subagents)}`],
    [
      "비용",
      money(s.costUSD),
      s.costUSD == null ? "기록 없음" : "Claude Code 정산",
    ],
    ["활성 시간", duration(s.activeSec), `전체 ${duration(s.durationSec)}`],
  ];

  $("#detail-body").innerHTML = `
    <div class="tiles">${tiles
      .map(
        ([label, value, sub]) => `<div class="tile">
          <div class="tile-label">${esc(label)}</div>
          <div class="tile-value">${esc(value)}</div>
          <div class="tile-sub">${esc(sub)}</div>
        </div>`
      )
      .join("")}</div>

    <p class="hint mono">${esc(s.modelId || Object.keys(s.models)[0] || "model?")} · ${esc(s.project)} · ${esc(s.branch || "브랜치 없음")} · v${esc(s.version)} · ${clock(s.start)} → ${clock(s.end)}</p>
    <p class="hint mono">${esc(s.sessionId)}${
      s.tokenSource === "computed" ? " · 토큰은 트랜스크립트에서 재구성한 추정치" : ""
    }</p>

    ${
      expired
        ? `<div class="notice">
             <b>원본 만료</b>
             Claude Code가 30일이 지난 트랜스크립트를 지웠습니다. 위 집계는 삭제 전에
             보관해 둔 요약이고, 대화 원문·타임라인·컨텍스트 추이는 남아있지 않습니다.
           </div>`
        : `<h2>컨텍스트 사용량 추이</h2>
           <div class="panel">${areaChart(data.contextSeries, s.contextLimit)}</div>`
    }

    <div class="two-col">
      <div>
        <h2>툴 호출</h2>
        <div class="panel">${barList(toolRows, "count")}</div>
      </div>
      <div>
        <h2>모델</h2>
        <div class="panel">${barList(
          Object.entries(s.models)
            .filter(([name]) => name !== "<synthetic>")
            .map(([name, t]) => ({ name, output: t.output }))
            .sort((a, b) => b.output - a.output),
          "output",
          tokens
        )}</div>
      </div>
    </div>

    ${
      expired
        ? ""
        : `<h2>대화 타임라인${data.truncated ? " (앞부분만)" : ""}</h2>
           <div class="timeline">${data.timeline.map(turnHtml).join("")}</div>`
    }`;
}

function turnHtml(turn) {
  const tools = (turn.tools || []).length
    ? `<div class="tool-list">${turn.tools
        .map((t) => `<div class="tool"><b>${esc(t.name)}</b><span>${esc(t.input)}</span></div>`)
        .join("")}</div>`
    : "";

  const marks = [
    turn.role === "user" ? "프롬프트" : esc(turn.model || "assistant"),
    clock(turn.ts),
    turn.thinking ? "thinking" : "",
    turn.outputTokens ? `${tokens(turn.outputTokens)} out` : "",
  ].filter(Boolean);

  return `<article class="turn ${esc(turn.role)}">
    <div class="turn-head"><b>${marks[0]}</b>${marks
      .slice(1)
      .map((m) => `<span>${esc(m)}</span>`)
      .join("")}</div>
    ${turn.text ? `<div class="turn-text">${esc(turn.text)}${turn.clipped ? "\n…" : ""}</div>` : ""}
    ${tools}
  </article>`;
}

/* ---------------- activity ---------------- */

async function loadActivity() {
  const data = await api("/api/stats?days=45");
  const t = data.totals;

  $("#activity-hint").textContent =
    `${int(t.sessions)}개 세션 · 원본 ${(t.bytes / 1e6).toFixed(0)}MB` +
    (t.archived ? ` · 요약만 남은 세션 ${int(t.archived)}개` : "");

  const tiles = [
    ["세션", int(t.sessions), `${data.projects.length}개 프로젝트`],
    ["메시지", int(t.messages), `툴 호출 ${int(t.toolCalls)}`],
    ["서브에이전트", int(t.subagents), "누적"],
    ["아카이브", int(t.archived), t.archived ? "원본 만료됨" : "아직 없음"],
    ["출력 토큰", tokens(t.output), int(t.output)],
    ["비용", money(t.costUSD), `${int(t.costed)}개 세션에 기록됨`],
  ];

  $("#activity-body").innerHTML = `
    <div class="tiles">${tiles
      .map(
        ([label, value, sub]) => `<div class="tile">
          <div class="tile-label">${esc(label)}</div>
          <div class="tile-value">${esc(value)}</div>
          <div class="tile-sub">${esc(sub)}</div>
        </div>`
      )
      .join("")}</div>

    <h2>일별 메시지</h2>
    <div class="panel">${dailyChart(data.daily, "messages", "메시지")}</div>

    <h2>일별 출력 토큰</h2>
    <div class="panel">${dailyChart(data.daily, "output", "토큰")}</div>

    <div class="two-col">
      <div>
        <h2>프로젝트 · 비용</h2>
        <div class="panel">${barList(
          data.projects.filter((p) => p.cost > 0).slice(0, 10),
          "cost",
          money
        )}</div>
      </div>
      <div>
        <h2>브랜치 · 활성 시간</h2>
        <div class="panel">${barList(
          data.branches.slice(0, 10).map((b) => ({ name: b.name, hours: Math.round(b.seconds / 360) / 10 })),
          "hours",
          (v) => `${v}h`
        )}</div>
      </div>
    </div>

    <div class="two-col">
      <div>
        <h2>모델 출력 토큰</h2>
        <div class="panel">${barList(data.models, "output", tokens)}</div>
      </div>
      <div>
        <h2>툴</h2>
        <div class="panel">${barList(data.tools.slice(0, 12), "count")}</div>
      </div>
    </div>`;
}

/* ---------------- shell ---------------- */

const TABS = ["live", "sessions", "activity"];

function show(tab, { updateHash = true } = {}) {
  state.tab = tab;
  $$(".view").forEach((view) => {
    view.hidden = view.id !== `view-${tab}`;
  });
  $$(".tab").forEach((button) => {
    // The detail view belongs to Sessions, so keep that tab lit while it is open.
    const lit = button.dataset.tab === (tab === "detail" ? "sessions" : tab);
    button.setAttribute("aria-selected", String(lit));
  });
  if (updateHash && TABS.includes(tab) && location.hash.slice(1) !== tab) {
    history.replaceState(null, "", `#${tab}`);
  }
}

function openTab(tab) {
  show(tab);
  if (tab === "sessions") (state.mode === "filter" ? loadSessions : runSearch)();
  if (tab === "activity") loadActivity();
  if (tab === "live") renderLive();
}

const SESSION_ROUTE = "session/";

// Every navigation goes through the hash so the browser's back/forward buttons replay it.
function navigate(hash) {
  if (location.hash.slice(1) === hash) route();
  else location.hash = hash;
}

function route() {
  const hash = location.hash.slice(1);
  if (hash.startsWith(SESSION_ROUTE)) {
    openDetail(decodeURIComponent(hash.slice(SESSION_ROUTE.length)));
    return;
  }
  const tab = TABS.includes(hash) ? hash : "live";
  // Returning from a detail view keeps the already-loaded list and its scroll position.
  if (tab === "sessions" && state.tab === "detail" && state.sessions.length) show(tab);
  else openTab(tab);
}

function connectEvents() {
  const source = new EventSource("/events");
  const dot = $("#conn");

  source.addEventListener("live", async (event) => {
    dot.className = "dot on";
    const payload = JSON.parse(event.data);
    state.live = payload.sessions;
    if (state.tab === "live") renderLive();

    // Context numbers come from the index, so pull the enriched roster too.
    try {
      const enriched = await api("/api/live");
      state.live = enriched.sessions;
      if (state.tab === "live") renderLive();
    } catch (error) {
      /* the SSE roster is already rendered */
    }
  });

  source.onopen = () => (dot.className = "dot on");
  source.onerror = () => (dot.className = "dot off");
}

function bind() {
  $$(".tab").forEach((button) =>
    button.addEventListener("click", () => navigate(button.dataset.tab))
  );

  window.addEventListener("hashchange", route);

  $("#back").addEventListener("click", () => {
    if (state.navigated) history.back();
    else navigate("sessions");
  });

  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-session]");
    if (target) {
      state.navigated = true;
      navigate(SESSION_ROUTE + encodeURIComponent(target.dataset.session));
    }
  });

  const modeFilter = $("#mode-filter");
  const modeSearch = $("#mode-search");

  const setMode = (mode) => {
    state.mode = mode;
    modeFilter.setAttribute("aria-pressed", String(mode === "filter"));
    modeSearch.setAttribute("aria-pressed", String(mode === "search"));
    $("#q").placeholder = mode === "filter" ? "제목·브랜치로 거르기" : "대화 본문 전체 검색";
    mode === "filter" ? loadSessions() : runSearch();
  };

  modeFilter.addEventListener("click", () => setMode("filter"));
  modeSearch.addEventListener("click", () => setMode("search"));

  let timer;
  $("#q").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => (state.mode === "filter" ? loadSessions() : runSearch()), 250);
  });
  $("#q").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      clearTimeout(timer);
      state.mode === "filter" ? loadSessions() : runSearch();
    }
  });

  ["#project", "#days", "#nonempty"].forEach((sel) =>
    $(sel).addEventListener("change", () => (state.mode === "filter" ? loadSessions() : runSearch()))
  );
}

async function boot() {
  bind();

  const initial = location.hash.slice(1);
  if (initial.startsWith(SESSION_ROUTE)) route();
  else show(TABS.includes(initial) ? initial : "live");

  try {
    const live = await api("/api/live");
    state.live = live.sessions;
    renderLive();
  } catch (error) {
    $("#live-cards").innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }

  connectEvents();

  try {
    const stats = await api("/api/stats?days=1");
    $("#topstat").textContent =
      `세션 ${int(stats.totals.sessions)} · 메시지 ${int(stats.totals.messages)} · ` +
      `출력 ${tokens(stats.totals.output)}`;
  } catch (error) {
    $("#topstat").textContent = "";
  }

  if (state.tab === "activity") loadActivity().catch(() => {});
  loadSessions().catch(() => {});
}

boot();
