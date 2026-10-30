(() => {
  "use strict";

  const state = {
    snapshot: null,
    selectedSignalId: null,
    renderedTicketId: null,
    userTouchedOverride: false,
    manualSubView: null, // null = follow mode default, or 'TICKET' | 'BLOTTER'
    lastMaxSignalId: 0,
  };

  const PRESETS = {
    btoIdx: 0,
    chatterIdx: 0,
    btoList: [
      { caller: "dk_flow", text: "BTO QQQ 10/30 505C @ 1.38 reclaim VWAP, stop 1.15" },
      { caller: "vega_desk", text: "BTO AAPL 11/06 232.5C @ 1.62 clean flag break" },
      { caller: "dk_flow", text: "BTO AMD 11/06 160C @ 1.94 sweep entry" },
      { caller: "macro_dan", text: "BTO MSFT 11/06 430P @ 2.05 failed bounce" },
    ],
    chatterList: [
      { caller: "macro_dan", text: "watching IWM 222 level closely if buyers step up, no entry yet" },
      { caller: "dk_flow", text: "choppy tape into the half hour, sitting on hands for clean structure" },
      { caller: "vega_desk", text: "looking at SPX gamma wall at 5860, patience here" },
    ],
  };

  // DOM References
  const el = {
    topBar: document.getElementById("top-bar"),
    dotDiscord: document.getElementById("dot-discord"),
    lblDiscord: document.getElementById("lbl-discord"),
    dotQuote: document.getElementById("dot-quote"),
    lblQuote: document.getElementById("lbl-quote"),
    dotParser: document.getElementById("dot-parser"),
    lblLatency: document.getElementById("lbl-latency"),
    lblParserTag: document.getElementById("lbl-parser-tag"),
    btnOpenConfigLatency: document.getElementById("btn-open-config-latency"),

    modeBtns: Array.from(document.querySelectorAll(".mode-btn")),
    livePopover: document.getElementById("live-confirm-popover"),
    liveKeyWarning: document.getElementById("live-key-warning"),
    btnCancelLive: document.getElementById("btn-cancel-live"),
    btnConfirmLive: document.getElementById("btn-confirm-live"),

    lblDailyRisk: document.getElementById("lbl-daily-risk"),
    inputMaxSlip: document.getElementById("input-max-slip"),
    btnPauseEntries: document.getElementById("btn-pause-entries"),
    btnOpenSettings: document.getElementById("btn-open-settings"),

    lblChannelFilter: document.getElementById("lbl-channel-filter"),
    signalTape: document.getElementById("signal-tape"),
    injectForm: document.getElementById("signal-inject-form"),
    inputCaller: document.getElementById("input-caller"),
    inputRawSignal: document.getElementById("input-raw-signal"),

    workspaceTitle: document.getElementById("workspace-title"),
    workspaceBadge: document.getElementById("workspace-badge"),
    tabAutoView: document.getElementById("tab-auto-view"),
    tabBlotterView: document.getElementById("tab-blotter-view"),
    lblWorkingCount: document.getElementById("lbl-working-count"),
    crossModeBanner: document.getElementById("cross-mode-banner"),
    crossModeText: document.getElementById("cross-mode-text"),
    btnSwitchToTicket: document.getElementById("btn-switch-to-ticket"),

    copilotWorkspace: document.getElementById("copilot-workspace"),
    blotterWorkspace: document.getElementById("blotter-workspace"),
    ordersTbody: document.getElementById("orders-tbody"),

    lblOpenCount: document.getElementById("lbl-open-count"),
    lblUnrealizedPnl: document.getElementById("lbl-unrealized-pnl"),
    lblRealizedPnl: document.getElementById("lbl-realized-pnl"),
    positionsTbody: document.getElementById("positions-tbody"),

    settingsBackdrop: document.getElementById("settings-backdrop"),
    btnCloseSettings: document.getElementById("btn-close-settings"),
    settingsForm: document.getElementById("settings-form"),
    cfgParserProvider: document.getElementById("cfg-parser-provider"),
    btnTestParser: document.getElementById("btn-test-parser"),
    parserTestOutput: document.getElementById("parser-test-output"),
    cfgGeminiKey: document.getElementById("cfg-gemini-key"),
    cfgGeminiModel: document.getElementById("cfg-gemini-model"),
    cfgOpenaiKey: document.getElementById("cfg-openai-key"),
    cfgOpenaiModel: document.getElementById("cfg-openai-model"),
    btnTestAlpacaPaper: document.getElementById("btn-test-alpaca-paper"),
    alpacaTestOutput: document.getElementById("alpaca-test-output"),
    cfgAlpacaPaperKey: document.getElementById("cfg-alpaca-paper-key"),
    cfgAlpacaPaperSecret: document.getElementById("cfg-alpaca-paper-secret"),
    cfgAlpacaLiveKey: document.getElementById("cfg-alpaca-live-key"),
    cfgAlpacaLiveSecret: document.getElementById("cfg-alpaca-live-secret"),
    cfgDailyCap: document.getElementById("cfg-daily-cap"),
    cfgTradeCap: document.getElementById("cfg-trade-cap"),
    cfgCancelSec: document.getElementById("cfg-cancel-sec"),
    cfgChannelFilter: document.getElementById("cfg-channel-filter"),
    btnResetDemo: document.getElementById("btn-reset-demo"),

    toastStatus: document.getElementById("toast-status"),
  };

  let toastTimer = null;
  function showToast(msg) {
    el.toastStatus.textContent = msg;
    el.toastStatus.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.toastStatus.classList.add("hidden");
    }, 2100);
  }

  function playSubtleCue() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "triangle";
      osc.frequency.setValueAtTime(660, ctx.currentTime);
      gain.gain.setValueAtTime(0.035, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.065);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.065);
    } catch (_) {}
  }

  async function apiPost(path, body = {}) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  }

  async function fetchState() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      if (!res.ok) return;
      const snap = await res.json();
      renderSnapshot(snap);
    } catch (_) {}
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtMoney(val, signed = false) {
    const num = Number(val || 0);
    const absStr = Math.abs(num).toFixed(2);
    if (!signed) return `$${absStr}`;
    if (num > 0.004) return `+$${absStr}`;
    if (num < -0.004) return `-$${absStr}`;
    return `$0.00`;
  }

  function fmtPx(val) {
    return Number(val || 0).toFixed(2);
  }

  function fmtSlip(val) {
    const n = Number(val || 0);
    return `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`;
  }

  function shortParserLabel(provider) {
    if (!provider) return "LOCAL";
    if (provider.startsWith("GEMINI_API")) return "GEMINI";
    if (provider.startsWith("OPENAI_API")) return "OPENAI";
    if (provider === "CODEX_CLI") return "CODEX-CLI";
    if (provider === "GEMINI_CLI") return "GEMINI-CLI";
    return "LOCAL";
  }

  function getActivePendingSignal(signals) {
    const pending = signals.filter((s) => s.ticket_status === "PENDING");
    if (!pending.length) return null;
    if (state.selectedSignalId) {
      const match = pending.find((s) => s.id === state.selectedSignalId);
      if (match) return match;
    }
    return pending[pending.length - 1];
  }

  function renderSnapshot(snap) {
    state.snapshot = snap;
    const { settings, signals, positions, orders, metrics, health, server_epoch } = snap;

    // Play subtle audio tick when a brand new actionable signal arrives
    const maxSigId = signals.length ? signals[signals.length - 1].id : 0;
    if (state.lastMaxSignalId > 0 && maxSigId > state.lastMaxSignalId) {
      const newest = signals[signals.length - 1];
      if (newest.is_actionable) {
        playSubtleCue();
        state.selectedSignalId = newest.id;
        state.userTouchedOverride = false;
      }
    }
    state.lastMaxSignalId = maxSigId;

    // 1. Top Utility Bar
    const mode = settings.execution_mode || "COPILOT";
    el.topBar.classList.toggle("live-armed-bar", mode === "LIVE_AUTO");

    el.lblDiscord.textContent = health.discord_status;
    el.dotDiscord.className = `status-dot ${health.discord_connected ? "dot-green" : "dot-amber"}`;

    el.lblQuote.textContent = health.quote_feed;
    el.dotQuote.className = "status-dot dot-green";

    el.lblLatency.textContent = `${settings.last_parse_latency_ms || 18}ms`;
    el.lblParserTag.textContent = shortParserLabel(settings.parser_provider);

    el.modeBtns.forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === mode);
    });

    const usedRisk = Math.round(Number(metrics.daily_risk_used || 0));
    const capRisk = Math.round(Number(settings.daily_risk_cap || 500));
    el.lblDailyRisk.textContent = `$${usedRisk} / $${capRisk}`;
    el.lblDailyRisk.style.color = usedRisk >= capRisk ? "var(--red)" : "var(--text-primary)";

    if (document.activeElement !== el.inputMaxSlip) {
      el.inputMaxSlip.value = Number(settings.max_slippage_pct || 5.0).toFixed(1);
    }

    const isPaused = settings.pause_entries === "true";
    el.btnPauseEntries.classList.toggle("paused", isPaused);
    el.btnPauseEntries.textContent = isPaused ? "Entries Paused" : "Pause Entries";

    el.lblChannelFilter.textContent = `#${settings.discord_channel_filter || "options-alerts"}`;

    // 2. Render Left Signal Tape
    renderSignalTape(signals);

    // 3. Render Top-Right Active Execution Workspace
    renderWorkspace(mode, signals, orders, settings, server_epoch);

    // 4. Render Bottom-Right Open Positions Ledger
    renderPositions(positions, metrics);
  }

  let prevTapeSignature = null;
  function renderSignalTape(signals) {
    const sigKey = signals
      .map((s) => `${s.id}:${s.ticket_status}:${s.live_ask}:${s.slippage_pct}:${state.selectedSignalId === s.id}`)
      .join("|");
    if (sigKey === prevTapeSignature) return;

    const wasNearBottom =
      el.signalTape.scrollHeight - el.signalTape.scrollTop - el.signalTape.clientHeight < 60;
    const prevCount = el.signalTape.childElementCount;
    prevTapeSignature = sigKey;

    if (!signals.length) {
      el.signalTape.innerHTML = `
        <div class="empty-workspace" style="height:100%;">
          <div class="mono">TAPE EMPTY</div>
          <div>Waiting for incoming Discord notifications, or use SIM buttons / bottom bar to test.</div>
        </div>
      `;
      return;
    }

    el.signalTape.innerHTML = signals
      .map((s) => {
        if (!s.is_actionable) {
          return `
            <div class="sig-row sig-ignored">
              <div class="sig-meta">
                <div class="sig-meta-left">
                  <span class="sig-ts mono">${escapeHtml(s.ts_ms_display)}</span>
                  <span class="sig-author">${escapeHtml(s.author)}</span>
                </div>
                <span class="sig-status-tag mono">IGNORE</span>
              </div>
              <div class="sig-raw">${escapeHtml(s.raw_text)}</div>
            </div>
          `;
        }

        const slipCls = s.slippage_verdict === "REJECT" ? "slip-reject" : "slip-pass";
        const isSel = state.selectedSignalId === s.id ? "sig-selected" : "";
        const priceMetric =
          s.action === "BTO"
            ? `Alert: ${fmtPx(s.alert_price)} <span class="parse-dot">&middot;</span> Live Ask: ${fmtPx(s.live_ask)} <span class="parse-dot">&middot;</span> <span class="${slipCls}">Slip: ${fmtSlip(s.slippage_pct)}</span>`
            : s.action === "STOP_UPDATE"
            ? `Update: ${escapeHtml(s.stop_note)}`
            : `Exit Bid: ${fmtPx(s.live_bid)} <span class="parse-dot">&middot;</span> Venue: ${escapeHtml(s.routed_origin)}`;

        return `
          <div class="sig-row sig-actionable ${isSel}" data-sig-id="${s.id}">
            <div class="sig-meta">
              <div class="sig-meta-left">
                <span class="sig-ts mono">${escapeHtml(s.ts_ms_display)}</span>
                <span class="sig-author">${escapeHtml(s.author)}</span>
              </div>
              <span class="sig-status-tag sig-status-${escapeHtml(s.ticket_status)} mono">${escapeHtml(s.ticket_status)}</span>
            </div>
            <div class="sig-raw">${escapeHtml(s.raw_text)}</div>
            <div class="sig-parse-strip mono">
              <span class="parse-act act-${escapeHtml(s.action)}">${escapeHtml(s.action)}</span>
              <span class="parse-dot">&middot;</span>
              <span class="parse-contract">${escapeHtml(s.tape_code || s.display_contract)}</span>
              <span class="parse-dot">&middot;</span>
              <span>${priceMetric}</span>
            </div>
          </div>
        `;
      })
      .join("");

    if (wasNearBottom || signals.length > prevCount || prevCount === 0) {
      el.signalTape.scrollTop = el.signalTape.scrollHeight;
    }
  }

  function renderWorkspace(mode, signals, orders, settings, serverEpoch) {
    const workingCount = orders.filter((o) => o.status === "WORKING").length;
    el.lblWorkingCount.textContent = String(workingCount);

    const pendingTicket = getActivePendingSignal(signals);

    // Determine whether to display Co-Pilot Ticket or Orders Blotter
    const defaultView = mode === "COPILOT" ? "TICKET" : "BLOTTER";
    const effectiveView = state.manualSubView || defaultView;

    el.tabAutoView.textContent = mode === "COPILOT" ? "Co-Pilot Ticket" : "Mode Default (Blotter)";
    el.tabAutoView.classList.toggle("active", effectiveView === defaultView && state.manualSubView === null);
    el.tabBlotterView.classList.toggle("active", effectiveView === "BLOTTER" && (mode === "COPILOT" || state.manualSubView === "BLOTTER"));

    if (mode === "COPILOT") {
      el.workspaceTitle.textContent =
        effectiveView === "TICKET"
          ? "ACTIVE EXECUTION WORKSPACE \u00B7 CO-PILOT TRADE TICKET"
          : "ACTIVE EXECUTION WORKSPACE \u00B7 ORDERS BLOTTER";
      el.workspaceBadge.textContent = "CO-PILOT \u00B7 RH-MANUAL";
      el.workspaceBadge.className = "mode-pill pill-neutral mono";
    } else if (mode === "PAPER_AUTO") {
      el.workspaceTitle.textContent = "ACTIVE EXECUTION WORKSPACE \u00B7 WORKING & RECENT ORDERS";
      el.workspaceBadge.textContent = "PAPER AUTO \u00B7 ALPACA SANDBOX";
      el.workspaceBadge.className = "mode-pill pill-amber mono";
    } else {
      el.workspaceTitle.textContent = "ACTIVE EXECUTION WORKSPACE \u00B7 LIVE BROKER ORDERS";
      el.workspaceBadge.textContent = "LIVE AUTO \u00B7 ARMED";
      el.workspaceBadge.className = "mode-pill pill-red mono";
    }

    // Cross-mode safety banner: e.g. operator is in Auto mode, but an RH-MANUAL exit ticket is waiting
    if (effectiveView === "BLOTTER" && pendingTicket) {
      el.crossModeBanner.classList.remove("hidden");
      el.crossModeText.textContent = `Pending manual action (${pendingTicket.routed_origin || "RH-MANUAL"}): ${pendingTicket.action} ${pendingTicket.display_contract}`;
    } else {
      el.crossModeBanner.classList.add("hidden");
    }

    if (effectiveView === "TICKET") {
      el.copilotWorkspace.classList.remove("hidden");
      el.blotterWorkspace.classList.add("hidden");
      renderCopilotTicket(pendingTicket, settings);
    } else {
      el.copilotWorkspace.classList.add("hidden");
      el.blotterWorkspace.classList.remove("hidden");
      state.renderedTicketId = null;
      renderOrdersBlotter(orders, serverEpoch);
    }
  }

  function renderCopilotTicket(sig, settings) {
    if (!sig) {
      state.renderedTicketId = null;
      el.copilotWorkspace.innerHTML = `
        <div class="empty-workspace">
          <div class="mono">NO PENDING CO-PILOT TICKET</div>
          <div>Listening on Discord channel #${escapeHtml(settings.discord_channel_filter || "options-alerts")}. Use SIM presets or paste a message to test.</div>
        </div>
      `;
      return;
    }

    const maxSlip = Number(settings.max_slippage_pct || 5.0);
    const maxLimit = Number(sig.alert_price || 0) * (1 + maxSlip / 100);
    const spread = Math.max(0, Number(sig.live_ask) - Number(sig.live_bid));
    const refExecPx = sig.action === "BTO" ? Number(sig.live_ask) : Number(sig.live_bid);
    const estDollar = Math.round(refExecPx * 100 * Number(sig.suggested_qty || 1));
    const isReject = sig.action === "BTO" && sig.slippage_verdict === "REJECT";
    const verdictText =
      sig.action === "BTO"
        ? isReject
          ? `REJECT ${fmtSlip(sig.slippage_pct)} CHASE`
          : `PASS ${fmtSlip(sig.slippage_pct)}`
        : `EXIT ROUTE \u00B7 ${sig.routed_origin || "RH-MANUAL"}`;

    if (state.renderedTicketId !== sig.id) {
      state.renderedTicketId = sig.id;
      state.userTouchedOverride = false;
      const sideLabel =
        sig.action === "BTO"
          ? "BUY TO OPEN"
          : sig.action === "TRIM"
          ? `SELL TRIM (${Math.round(sig.trim_pct || 50)}%)`
          : "SELL TO CLOSE";

      el.copilotWorkspace.innerHTML = `
        <div class="ticket-shell">
          <div class="ticket-top-row">
            <div class="ticket-contract-group">
              <div class="ticket-side-line">
                <span class="side-badge side-${escapeHtml(sig.action)} mono">${escapeHtml(sideLabel)}</span>
                <span class="ticket-occ-sub mono">Caller: ${escapeHtml(sig.author)} &middot; Origin: ${escapeHtml(sig.routed_origin || "RH-MANUAL")}</span>
              </div>
              <div class="ticket-contract-main mono">${escapeHtml(sig.display_contract)}</div>
              <div class="ticket-occ-sub mono">OCC: ${escapeHtml(sig.occ_symbol)} &middot; RH: ${escapeHtml(sig.rh_clipboard)}</div>
            </div>
            <div id="t-verdict-box" class="verdict-box ${isReject ? "verdict-reject" : "verdict-pass"}">
              <div class="verdict-label">SLIPPAGE VERDICT</div>
              <div id="t-verdict-val" class="verdict-value mono">${escapeHtml(verdictText)}</div>
            </div>
          </div>

          <div class="ticket-metrics-grid">
            <div class="t-metric">
              <span class="micro-label">CALLER ALERT</span>
              <span class="t-metric-val mono">$${fmtPx(sig.alert_price)}</span>
            </div>
            <div class="t-metric">
              <span class="micro-label">LIVE BID &times; ASK</span>
              <span id="t-bid-ask" class="t-metric-val mono">
                <span class="val-bid">${fmtPx(sig.live_bid)}</span> &times; <span class="val-ask">${fmtPx(sig.live_ask)}</span>
              </span>
            </div>
            <div class="t-metric">
              <span class="micro-label">NBBO SPREAD</span>
              <span id="t-spread" class="t-metric-val mono">$${fmtPx(spread)}</span>
            </div>
            <div class="t-metric">
              <span class="micro-label">MAX ALLOWED LIMIT</span>
              <span class="t-metric-val mono">${sig.action === "BTO" ? `$${fmtPx(maxLimit)}` : "BID MKT"}</span>
            </div>
            <div class="t-metric">
              <span class="micro-label">SUGGESTED SIZE</span>
              <span id="t-size-risk" class="t-metric-val mono">${sig.suggested_qty} ct &middot; $${estDollar}</span>
            </div>
          </div>

          <div class="ticket-footer-row">
            <div class="ticket-override-inputs">
              <label class="override-field">
                <span class="micro-label">QTY</span>
                <input id="ticket-qty-input" class="ticket-num-input mono" type="number" min="1" max="100" value="${sig.suggested_qty || 1}" />
              </label>
              <label class="override-field">
                <span class="micro-label">FILL PX</span>
                <input id="ticket-px-input" class="ticket-num-input mono" type="number" step="0.01" min="0.01" value="${fmtPx(refExecPx)}" />
              </label>
            </div>
            <div class="ticket-actions">
              <button id="btn-ticket-copy" class="btn btn-neutral" type="button">
                Copy Contract <kbd>C</kbd>
              </button>
              <button id="btn-ticket-skip" class="btn btn-neutral" type="button">
                Skip <kbd>Esc</kbd>
              </button>
              <button id="btn-ticket-fill" class="btn ${isReject ? "btn-danger" : "btn-green"}" type="button">
                Mark Filled <kbd>Enter</kbd>
              </button>
            </div>
          </div>
        </div>
      `;

      const qtyIn = document.getElementById("ticket-qty-input");
      const pxIn = document.getElementById("ticket-px-input");
      qtyIn.addEventListener("input", () => { state.userTouchedOverride = true; });
      pxIn.addEventListener("input", () => { state.userTouchedOverride = true; });

      document.getElementById("btn-ticket-copy").addEventListener("click", () => copyActiveContract(sig));
      document.getElementById("btn-ticket-skip").addEventListener("click", () => submitTicketDecision(sig.id, "SKIP"));
      document.getElementById("btn-ticket-fill").addEventListener("click", () => submitTicketDecision(sig.id, "FILL"));
      return;
    }

    // Surgical live quote update without disrupting operator input focus
    const bidAskEl = document.getElementById("t-bid-ask");
    const spreadEl = document.getElementById("t-spread");
    const verdictBoxEl = document.getElementById("t-verdict-box");
    const verdictValEl = document.getElementById("t-verdict-val");
    const pxIn = document.getElementById("ticket-px-input");

    if (bidAskEl) {
      bidAskEl.innerHTML = `<span class="val-bid">${fmtPx(sig.live_bid)}</span> &times; <span class="val-ask">${fmtPx(sig.live_ask)}</span>`;
    }
    if (spreadEl) spreadEl.textContent = `$${fmtPx(spread)}`;
    if (verdictBoxEl) verdictBoxEl.className = `verdict-box ${isReject ? "verdict-reject" : "verdict-pass"}`;
    if (verdictValEl) verdictValEl.textContent = verdictText;
    if (pxIn && !state.userTouchedOverride && document.activeElement !== pxIn) {
      pxIn.value = fmtPx(refExecPx);
    }
  }

  function renderOrdersBlotter(orders, serverEpoch) {
    if (!orders.length) {
      el.ordersTbody.innerHTML = `
        <tr><td colspan="11" class="mono" style="color: var(--text-muted); text-align:center;">No working or historical orders in session.</td></tr>
      `;
      return;
    }

    const nowSec = Number(serverEpoch || Date.now() / 1000);
    el.ordersTbody.innerHTML = orders
      .map((o) => {
        const remSec = o.status === "WORKING" ? Math.max(0, Math.ceil(Number(o.expires_at_epoch || 0) - nowSec)) : 0;
        let statusHtml = `<span class="ord-status-${escapeHtml(o.status)} mono">${escapeHtml(o.status)}</span>`;
        if (o.status === "WORKING") {
          statusHtml = `<span class="ord-status-WORKING mono">WORKING &middot; ${remSec}s</span>`;
        } else if (o.status === "REJECTED" && o.reject_reason) {
          statusHtml = `<span class="ord-status-REJECTED mono">REJECTED (${escapeHtml(o.reject_reason)})</span>`;
        } else if (o.status === "CANCELED") {
          statusHtml = `<span class="ord-status-CANCELED mono">CANCELED</span>`;
        }

        const liveOrFill = o.status === "FILLED" ? `$${fmtPx(o.fill_price)}` : `$${fmtPx(o.live_ref_price)}`;
        const slipClass = Number(o.slippage_pct) > 5.0 ? "pnl-neg" : "pnl-pos";
        const actionCell =
          o.status === "WORKING"
            ? `<button class="btn btn-danger btn-xs btn-cancel-order" data-order-id="${o.id}" type="button">Cancel</button>`
            : `<span class="mono" style="color: var(--text-muted);">--</span>`;

        return `
          <tr>
            <td class="mono" style="color: var(--text-secondary);">${escapeHtml(o.ts_display)}</td>
            <td class="mono"><span class="parse-act act-${escapeHtml(o.side)}">${escapeHtml(o.side)}</span></td>
            <td class="mono">${escapeHtml(o.display_contract)}</td>
            <td><span class="origin-badge origin-${escapeHtml(o.venue)} mono">${escapeHtml(o.venue)}</span></td>
            <td class="num mono">${o.qty}</td>
            <td class="num mono">$${fmtPx(o.caller_price)}</td>
            <td class="num mono">$${fmtPx(o.limit_price)}</td>
            <td class="num mono">${liveOrFill}</td>
            <td class="num mono ${slipClass}">${fmtSlip(o.slippage_pct)}</td>
            <td>${statusHtml}</td>
            <td class="right">${actionCell}</td>
          </tr>
        `;
      })
      .join("");
  }

  function renderPositions(positions, metrics) {
    el.lblOpenCount.textContent = `${positions.length} OPEN`;

    let totalUnrealized = 0;
    const rowsHtml = positions
      .map((p) => {
        const mark = (Number(p.live_bid) + Number(p.live_ask)) / 2;
        const pnlDollar = (mark - Number(p.avg_entry)) * 100 * Number(p.qty);
        const pnlPct = ((mark - Number(p.avg_entry)) / Math.max(0.01, Number(p.avg_entry))) * 100;
        totalUnrealized += pnlDollar;

        const pnlCls = pnlDollar > 0.5 ? "pnl-pos" : pnlDollar < -0.5 ? "pnl-neg" : "pnl-flat";
        const trimLabel = p.origin === "RH-MANUAL" ? "Log 50% Trim" : "Trim 50%";
        const closeLabel = p.origin === "RH-MANUAL" ? "Mark Flat" : "Close";

        return `
          <tr>
            <td class="mono" style="font-weight: 500;">${escapeHtml(p.display_contract)}</td>
            <td><span class="origin-badge origin-${escapeHtml(p.origin)} mono">${escapeHtml(p.origin)}</span></td>
            <td class="num mono">${p.qty}</td>
            <td class="num mono">$${fmtPx(p.avg_entry)}</td>
            <td class="num mono">
              <span class="val-bid">${fmtPx(p.live_bid)}</span> / <span class="val-ask">${fmtPx(p.live_ask)}</span>
            </td>
            <td class="mono" style="color: var(--text-secondary);">${escapeHtml(p.stop_note || "--")}</td>
            <td class="num mono ${pnlCls}">${fmtMoney(pnlDollar, true)} (${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%)</td>
            <td class="right">
              <div class="row-actions">
                <button class="btn btn-neutral btn-xs btn-pos-action" data-pos-id="${p.id}" data-act="TRIM_50" type="button">${trimLabel}</button>
                <button class="btn btn-danger btn-xs btn-pos-action" data-pos-id="${p.id}" data-act="CLOSE_ALL" type="button">${closeLabel}</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");

    el.positionsTbody.innerHTML =
      rowsHtml ||
      `<tr><td colspan="8" class="mono" style="color: var(--text-muted); text-align:center;">No open option positions.</td></tr>`;

    el.lblUnrealizedPnl.textContent = fmtMoney(totalUnrealized, true);
    el.lblUnrealizedPnl.className = totalUnrealized > 0.5 ? "pnl-pos" : totalUnrealized < -0.5 ? "pnl-neg" : "pnl-flat";

    const realPnl = Number(metrics.realized_pnl || 0);
    el.lblRealizedPnl.textContent = fmtMoney(realPnl, true);
    el.lblRealizedPnl.className = realPnl > 0.5 ? "pnl-pos" : realPnl < -0.5 ? "pnl-neg" : "pnl-flat";
  }

  async function copyActiveContract(sig) {
    if (!sig) return;
    const text = `${sig.rh_clipboard} (${sig.occ_symbol})`;
    try {
      await navigator.clipboard.writeText(text);
      showToast(`Copied: ${text}`);
    } catch (_) {
      showToast(`Contract: ${text}`);
    }
  }

  async function submitTicketDecision(signalId, decision) {
    const qtyEl = document.getElementById("ticket-qty-input");
    const pxEl = document.getElementById("ticket-px-input");
    const qty = qtyEl ? Number(qtyEl.value || 1) : undefined;
    const px = pxEl ? Number(pxEl.value || 0) : undefined;
    await apiPost("/api/copilot/decision", {
      signal_id: signalId,
      decision,
      qty_override: qty,
      price_override: px,
    });
    state.selectedSignalId = null;
    state.userTouchedOverride = false;
    showToast(decision === "FILL" ? "Logged RH-MANUAL fill to ledger" : "Ticket skipped");
    await fetchState();
  }

  // Event Wiring
  el.modeBtns.forEach((btn) => {
    btn.addEventListener("click", async () => {
      const targetMode = btn.dataset.mode;
      const curMode = state.snapshot?.settings?.execution_mode || "COPILOT";
      if (targetMode === curMode) return;

      if (targetMode === "LIVE_AUTO") {
        const hasLiveKeys = Boolean(state.snapshot?.settings?.alpaca_live_key_configured);
        el.liveKeyWarning.textContent = hasLiveKeys
          ? "Status: Live Alpaca API credentials configured. Real limit orders will transmit."
          : "Notice: No Live API keys saved yet. Orders will run against local live-mode guardrails until keys are entered in Connections.";
        el.livePopover.classList.remove("hidden");
        return;
      }

      el.livePopover.classList.add("hidden");
      state.manualSubView = null;
      await apiPost("/api/settings", { execution_mode: targetMode, live_armed: "false" });
      showToast(`Switched mode: ${targetMode}`);
      await fetchState();
    });
  });

  el.btnCancelLive.addEventListener("click", () => {
    el.livePopover.classList.add("hidden");
  });

  el.btnConfirmLive.addEventListener("click", async () => {
    el.livePopover.classList.add("hidden");
    state.manualSubView = null;
    await apiPost("/api/settings", { execution_mode: "LIVE_AUTO", live_armed: "true" });
    showToast("LIVE AUTO ARMED");
    await fetchState();
  });

  el.inputMaxSlip.addEventListener("change", async () => {
    const v = Math.max(0.5, Math.min(50, Number(el.inputMaxSlip.value || 5.0)));
    el.inputMaxSlip.value = v.toFixed(1);
    await apiPost("/api/settings", { max_slippage_pct: String(v) });
    await fetchState();
  });

  el.btnPauseEntries.addEventListener("click", async () => {
    const cur = state.snapshot?.settings?.pause_entries === "true";
    await apiPost("/api/settings", { pause_entries: cur ? "false" : "true" });
    showToast(!cur ? "New entries paused (exits remain active)" : "Entry ingestion resumed");
    await fetchState();
  });

  el.tabAutoView.addEventListener("click", () => {
    state.manualSubView = null;
    if (state.snapshot) renderSnapshot(state.snapshot);
  });

  el.tabBlotterView.addEventListener("click", () => {
    state.manualSubView = "BLOTTER";
    if (state.snapshot) renderSnapshot(state.snapshot);
  });

  el.btnSwitchToTicket.addEventListener("click", () => {
    state.manualSubView = "TICKET";
    if (state.snapshot) renderSnapshot(state.snapshot);
  });

  el.signalTape.addEventListener("click", (e) => {
    const row = e.target.closest(".sig-actionable");
    if (!row) return;
    const id = Number(row.dataset.sigId);
    const sig = state.snapshot?.signals?.find((s) => s.id === id);
    if (sig && sig.ticket_status === "PENDING") {
      state.selectedSignalId = id;
      state.manualSubView = "TICKET";
      renderSnapshot(state.snapshot);
    }
  });

  el.ordersTbody.addEventListener("click", async (e) => {
    const btn = e.target.closest(".btn-cancel-order");
    if (!btn) return;
    const orderId = Number(btn.dataset.orderId);
    await apiPost("/api/orders/cancel", { order_id: orderId });
    showToast(`Canceled order #${orderId}`);
    await fetchState();
  });

  el.positionsTbody.addEventListener("click", async (e) => {
    const btn = e.target.closest(".btn-pos-action");
    if (!btn) return;
    const posId = Number(btn.dataset.posId);
    const act = btn.dataset.act;
    await apiPost("/api/positions/action", { pos_id: posId, action: act });
    showToast(act === "TRIM_50" ? "Recorded 50% trim" : "Position closed / marked flat");
    await fetchState();
  });

  // Manual signal injection form
  el.injectForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = el.inputRawSignal.value.trim();
    if (!raw) return;
    const author = el.inputCaller.value.trim() || "dk_flow";
    el.inputRawSignal.value = "";
    await apiPost("/api/signals/inject", { raw_text: raw, author, sim_bias: "normal" });
    await fetchState();
  });

  // Preset scenario buttons
  document.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const kind = btn.dataset.preset;
      if (kind === "bto") {
        const item = PRESETS.btoList[PRESETS.btoIdx % PRESETS.btoList.length];
        PRESETS.btoIdx += 1;
        await apiPost("/api/signals/inject", { raw_text: item.text, author: item.caller, sim_bias: "normal" });
      } else if (kind === "chase") {
        await apiPost("/api/signals/inject", {
          raw_text: "BTO META 11/06 600C @ 2.10 fast momentum lotto",
          author: "dk_flow",
          sim_bias: "chase",
        });
      } else if (kind === "trim") {
        const openPos = state.snapshot?.positions?.[0];
        const tkr = openPos ? openPos.ticker : "NVDA";
        const caller = openPos?.caller || "dk_flow";
        await apiPost("/api/signals/inject", {
          raw_text: `Trimming half ${tkr} here, locking in gains`,
          author: caller,
          sim_bias: "normal",
        });
      } else if (kind === "stop") {
        const openPos = state.snapshot?.positions?.[0];
        const tkr = openPos ? openPos.ticker : "NVDA";
        await apiPost("/api/signals/inject", {
          raw_text: `Stops at BE on ${tkr} runners`,
          author: openPos?.caller || "dk_flow",
          sim_bias: "normal",
        });
      } else if (kind === "chatter") {
        const item = PRESETS.chatterList[PRESETS.chatterIdx % PRESETS.chatterList.length];
        PRESETS.chatterIdx += 1;
        await apiPost("/api/signals/inject", { raw_text: item.text, author: item.caller, sim_bias: "normal" });
      }
      await fetchState();
    });
  });

  // Settings / Connections Modal
  function openSettingsModal() {
    const s = state.snapshot?.settings || {};
    el.cfgParserProvider.value = s.parser_provider || "LOCAL_FAST";
    el.cfgGeminiKey.value = s.gemini_api_key || "";
    el.cfgGeminiModel.value = s.gemini_model || "gemini-2.5-flash";
    el.cfgOpenaiKey.value = s.openai_api_key || "";
    el.cfgOpenaiModel.value = s.openai_model || "gpt-4.1-mini";
    el.cfgAlpacaPaperKey.value = s.alpaca_paper_key || "";
    el.cfgAlpacaPaperSecret.value = s.alpaca_paper_secret || "";
    el.cfgAlpacaLiveKey.value = s.alpaca_live_key || "";
    el.cfgAlpacaLiveSecret.value = s.alpaca_live_secret || "";
    el.cfgDailyCap.value = s.daily_risk_cap || "500";
    el.cfgTradeCap.value = s.max_risk_per_trade || "250";
    el.cfgCancelSec.value = s.auto_cancel_seconds || "15";
    el.cfgChannelFilter.value = s.discord_channel_filter || "options-alerts";
    el.parserTestOutput.classList.add("hidden");
    el.alpacaTestOutput.classList.add("hidden");
    el.settingsBackdrop.classList.remove("hidden");
  }

  el.btnOpenSettings.addEventListener("click", openSettingsModal);
  el.btnOpenConfigLatency.addEventListener("click", openSettingsModal);
  el.btnCloseSettings.addEventListener("click", () => el.settingsBackdrop.classList.add("hidden"));

  el.btnTestAlpacaPaper.addEventListener("click", async () => {
    el.alpacaTestOutput.classList.remove("hidden");
    el.alpacaTestOutput.style.color = "var(--text-secondary)";
    el.alpacaTestOutput.textContent = "Verifying Alpaca Paper account credentials...";

    const res = await apiPost("/api/alpaca/test", {
      paper: true,
      alpaca_paper_key: el.cfgAlpacaPaperKey.value.trim(),
      alpaca_paper_secret: el.cfgAlpacaPaperSecret.value.trim(),
    });

    if (res.ok) {
      el.alpacaTestOutput.style.color = "var(--green)";
      const eq = Number(res.equity || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      el.alpacaTestOutput.textContent = `OK \u00B7 Status: ${res.status} \u00B7 Paper Equity: $${eq} \u00B7 Options Level: ${res.options_approved_level}`;
    } else {
      el.alpacaTestOutput.style.color = "var(--red)";
      el.alpacaTestOutput.textContent = `FAILED: ${res.error}`;
    }
  });

  el.settingsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    await apiPost("/api/settings", {
      parser_provider: el.cfgParserProvider.value,
      gemini_api_key: el.cfgGeminiKey.value.trim(),
      gemini_model: el.cfgGeminiModel.value.trim() || "gemini-2.5-flash",
      openai_api_key: el.cfgOpenaiKey.value.trim(),
      openai_model: el.cfgOpenaiModel.value.trim() || "gpt-4.1-mini",
      alpaca_paper_key: el.cfgAlpacaPaperKey.value.trim(),
      alpaca_paper_secret: el.cfgAlpacaPaperSecret.value.trim(),
      alpaca_live_key: el.cfgAlpacaLiveKey.value.trim(),
      alpaca_live_secret: el.cfgAlpacaLiveSecret.value.trim(),
      daily_risk_cap: el.cfgDailyCap.value,
      max_risk_per_trade: el.cfgTradeCap.value,
      auto_cancel_seconds: el.cfgCancelSec.value,
      discord_channel_filter: el.cfgChannelFilter.value.trim() || "options-alerts",
    });
    el.settingsBackdrop.classList.add("hidden");
    showToast("Saved parser & broker settings");
    await fetchState();
  });

  el.btnTestParser.addEventListener("click", async () => {
    el.parserTestOutput.classList.remove("hidden");
    el.parserTestOutput.style.color = "var(--text-secondary)";
    el.parserTestOutput.textContent = "Probing selected parser with sample alert...";

    const res = await apiPost("/api/parser/test", {
      parser_provider: el.cfgParserProvider.value,
      gemini_api_key: el.cfgGeminiKey.value.trim(),
      gemini_model: el.cfgGeminiModel.value.trim() || "gemini-2.5-flash",
      openai_api_key: el.cfgOpenaiKey.value.trim(),
      openai_model: el.cfgOpenaiModel.value.trim() || "gpt-4.1-mini",
    });

    if (res.ok) {
      el.parserTestOutput.style.color = "var(--green)";
      el.parserTestOutput.textContent = `OK (${res.latency_ms}ms) via ${res.provider} -> action=${res.parsed.action} ${res.parsed.ticker} ${res.parsed.strike}${res.parsed.right}`;
    } else {
      el.parserTestOutput.style.color = "var(--red)";
      el.parserTestOutput.textContent = `FAILED: ${res.error}`;
    }
  });

  async function clearAllSessionData() {
    await apiPost("/api/reset", { seed: false });
    el.settingsBackdrop.classList.add("hidden");
    state.selectedSignalId = null;
    state.renderedTicketId = null;
    prevTapeSignature = null;
    showToast("Cleared all signals, orders, and positions");
    await fetchState();
  }

  el.btnResetDemo.addEventListener("click", clearAllSessionData);
  const btnClearTape = document.getElementById("btn-clear-tape");
  if (btnClearTape) {
    btnClearTape.addEventListener("click", clearAllSessionData);
  }

  // Global Execution Keyboard Shortcuts
  document.addEventListener("keydown", (e) => {
    if (!el.settingsBackdrop.classList.contains("hidden")) {
      if (e.key === "Escape") {
        e.preventDefault();
        el.settingsBackdrop.classList.add("hidden");
      }
      return;
    }

    if (!el.livePopover.classList.contains("hidden")) {
      if (e.key === "Escape") {
        e.preventDefault();
        el.livePopover.classList.add("hidden");
      }
      return;
    }

    const tag = (document.activeElement?.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      return;
    }

    const signals = state.snapshot?.signals || [];
    const pending = getActivePendingSignal(signals);
    if (!pending) return;

    if (e.key === "Enter") {
      e.preventDefault();
      submitTicketDecision(pending.id, "FILL");
    } else if (e.key === "Escape") {
      e.preventDefault();
      submitTicketDecision(pending.id, "SKIP");
    } else if (e.key === "c" || e.key === "C" || e.key === " ") {
      e.preventDefault();
      copyActiveContract(pending);
    }
  });

  // Initial load + 800ms live quote/order tick loop
  fetchState();
  setInterval(fetchState, 800);
})();

