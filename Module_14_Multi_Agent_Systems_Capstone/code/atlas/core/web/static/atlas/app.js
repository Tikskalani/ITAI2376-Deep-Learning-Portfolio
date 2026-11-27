function atlas() {
  return {
    heroGone: false,
    messages: [], chatThread: null, chatNew: false, chatLoaded: false, threads: [], historyOpen: false,
    draft: "", pending: false, streaming: false, convoMode: false, error: "", nextId: 1,
    cost: { today: null, month: null, global_daily_cap: 0 },
    episodes: [], decisions: [], agents: [], learnings: [], skills: [],
    approvals: [], inbox: [], inboxAlertsOnly: false, briefs: [], signals: [],
    jobs: { funnel: {}, applications: [], postings: [] }, jobsErr: "", jobsLoaded: false,
    jobForm: { open: false, company: "", role: "", url: "", busy: false, err: "" },
    answers: { standard: [], learned: [], pending: [], missing_required: [] }, answersErr: "", answersLoaded: false,
    queue: { items: [], building: false, queued_today: 0, daily_cap: 5, min_score: 80 }, queueErr: "", queueLoaded: false, queueBusy: false,
    approvalsErr: "", approvalsLoaded: false, health: null, projectsErr: "", projectsLoaded: false, memHits: [],
    modal: null,
    standard: null, standardBusy: false,
    night: null, nightBusy: false, nightAdding: false, nightErr: "", nightForm: { title: "", dod: "", project: "", agent: "" },
    live: { connected: false, lastId: 0, agents: {}, feed: [] },
    rec: { active: false, busy: false, started: 0, elapsed: "0:00", kind: "lecture", title: "", err: "", chunks: [], mime: "" }, recordings: [],
    replay: { data: null, i: 0, playing: false, timer: null },
    plan: null, planStart: null, planErr: "", planLoaded: false, news: null, newsBusy: false, outcomes: null,
    memory: null, memoryQ: "", newFact: "", newNote: { kind: "fact", content: "", days: "" },
    trust: null, story: null, _storyDone: false, money: null, moneyBusy: false,
    kb: null, kbBusy: false, kbCap: "",
    connect: null, connectBusy: "", cx: { gid: "", gsecret: "", gReplace: false, ghToken: "", ghRepos: [], cvUrl: "", cvToken: "", alarm: "", mcpName: "zapier", mcpUrl: "", mcpToken: "", mcpAgents: "ceo" },
    quick: { text: "", hint: null, busy: false, err: "", timer: null },
    pushSupported: false, pushOn: false, pushBusy: false, pushMsg: "", _swReg: null,
    graph: { nodes: [], edges: [] },
    graphLegend: [],
    focusedNodeId: "",
    _nodePositions: null,
    spark: [],
    clock: "",
    wavePhase: 0,
    paletteOpen: false, paletteQuery: "", paletteIdx: 0,
    speakOn: false, voiceMenu: false,
    listening: false,
    _recog: null,
    attachments: [],
    learnMode: false,
    learnCount: 0,
    _simRunning: false,
    view: "home",
    room: "bridge",
    sub: { comms: "channel", plan: "week", career: "queue", staff: "agents", inbox: "approvals", memory: "facts", settings: "connections" },
    agentSel: "", agentPage: null, agentErr: "", factsAll: false,
    paste: { url: "", company: "", title: "", apply: true, busy: false, err: "", done: "", ask: false },
    k2c: { sources: [], versions: [], url: "", busy: false, err: "", msg: "", off: false, cap_usd: 0.5 },
    ring: { open: false, x: 0, y: 0, hi: -1 },
    day: null, dial: null, dialSvg: { done: "", next: "", nodes: "", needle: "" },
    workflows: { names: [], runs: [], err: "", busy: "" }, mcpTools: {}, setup: null, setupBusy: false, costPanel: null,
    film: { on: false, data: null, i: 0, playing: false, timer: null }, replanned: false, demo: !!window.ATLAS_DEMO,
    focus: false,
    _reducedMotion: false,
    projects: [], askDraft: "", narrow: window.matchMedia("(max-width: 640px)").matches, scrolled: false,
    starters: [
      { title: "Plan my day", sub: "What to do first, in order", prompt: "Plan my day. What needs my attention first, in order, with time estimates?" },
      { title: "What's slipping?", sub: "Across clients, college and the job search", prompt: "What's slipping right now across my projects? Be specific and name the next action for each." },
      { title: "Draft a client update", sub: "For the project with the most movement", prompt: "Draft a short status update for the client project with the most recent activity. Draft only, do not send." },
      { title: "Brief me", sub: "Everything that arrived since this morning", prompt: "Give me a brief of everything that arrived since this morning and what I should do about it." },
    ],

    init() {
      // Alpine calls init() itself; a second call would double every timer
      // and listener (it did: the dashboard polled twice as often as meant).
      if (this._inited) return;
      this._inited = true;
      this._reducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      try { this.focus = localStorage.getItem("atlas.focus") === "1"; } catch (e) { /* private mode */ }
      document.body.dataset.focus = this.focus ? "1" : "0";
      setTimeout(() => { this.heroGone = true; }, this._reducedMotion ? 0 : 900);
      try {
        const saved = JSON.parse(localStorage.getItem("atlas.sub") || "{}");
        for (const [r, s] of Object.entries(saved)) if ((this.roomSubs()[r] || []).includes(s)) this.sub[r] = s;
        const r = localStorage.getItem("atlas.room");
        if (r in this.roomSubs() && !location.hash) this.goRoom(r);
      } catch (e) { /* private mode */ }
      if (window.AtlasShell) window.AtlasShell.init(this);
      this.refresh(true);
      this.fetchSetup();
      this.pwaInit();
      this.startClock();
      if (!this._reducedMotion) {
        setInterval(() => { if (this.pending) this.wavePhase = (this.wavePhase + 0.5) % (Math.PI * 4); }, 80);
      }
      // 15 s ticks; skipped while the tab is hidden and while the last round
      // is still in flight, so a slow server is not buried under requests.
      setInterval(() => { if (!document.hidden) this.refresh(false); }, 15_000);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) this.refresh(true); });
      document.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
          e.preventDefault(); this.openPalette();
          return;
        }
        // A character typed while the palette is open but its box is not yet
        // focusable (the frame after Ctrl+K, before Alpine shows it) is kept.
        const box = this.$refs.paletteInput;
        if (this.paletteOpen && box && document.activeElement !== box && e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
          e.preventDefault(); this.paletteQuery += e.key; this.searchMemory();
          this.$nextTick(() => box.focus());
        }
        if (e.altKey && e.key.toLowerCase() === "m") {
          e.preventDefault(); this.toggleMic();
        }
        if (e.key === "Escape" && this.focusedNodeId) {
          this.clearGraphFocus();
        }
      });
    },

    // Shell helpers: the top bar's clock, the corner dial's needle, the links count, the ring.
    clockLine() {
      const d = new Date(), c = this.clock;  // reading this.clock re-runs it on every tick
      return d.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short" }).toUpperCase() + " · " + c;
    },
    nowDeg() {
      const [h, m] = String(this.clock || "0:0").split(":").map(Number);
      return (((h || 0) * 60 + (m || 0)) / 1440) * 360;
    },
    linksUp() {
      const c = this.connect; if (!c) return "…";
      const up = ["google", "github", "canvas", "alarm"].filter(k => c[k] && c[k].ok === true).length;
      const mcp = (c.mcp && c.mcp.servers || []).length;
      return String(up + mcp);
    },
    ringPaths() { return this._ring || (this._ring = window.AtlasShell ? window.AtlasShell.ringPaths(this.rooms().length) : []); },
    // The summon ring's segments as SVG markup; labels are Atlas's own room names.
    ringSvg() {
      const rooms = this.rooms(), hi = this.ring.hi;
      return this.ringPaths().map((p, i) => `<g class="${hi === i ? "hi" : ""}"><path d="${p.d}" class="ring-seg"/>` +
        `<text x="${p.lx}" y="${(p.ly + 4).toFixed(1)}" text-anchor="middle" class="ring-lbl">${(rooms[i].short || rooms[i].label).toUpperCase()}</text></g>`).join("");
    },

    // Focus mode: the glyph field and the scanlines stop, for reading.
    toggleFocus() {
      this.focus = !this.focus;
      document.body.dataset.focus = this.focus ? "1" : "0";
      try { localStorage.setItem("atlas.focus", this.focus ? "1" : "0"); } catch (e) { /* private mode */ }
    },

    // ============= ROOMS AND NAVIGATION =============
    // Eight rooms, each with sub-tabs (audit 2026-10-22, finding 09). `view`
    // is kept in step for the code that still asks which v2 view is showing.
    roomSubs() {
      return { bridge: [], comms: ["channel", "briefings", "recordings"], plan: ["week", "night", "routines"],
        career: ["queue", "pipeline", "discover", "profile"], staff: ["agents", "runs", "playbook"],
        inbox: ["approvals", "skills", "signals", "notifications"], memory: ["facts", "people", "projects", "map"],
        settings: ["connections", "trust", "ledger"] };
    },
    rooms() {
      const pc = this.plan ? this.plan.counts : null;
      return [
        { id: "bridge", label: "Today", key: "1", icon: "r-bridge" },
        { id: "comms", label: "Chat", key: "2", icon: "r-comms", count: this.pending ? "…" : 0, tone: "acc" },
        { id: "plan", label: "Plan", key: "3", icon: "r-missions", count: pc ? (pc.overdue || pc.today) : 0, tone: pc && pc.overdue ? "hot" : "acc" },
        { id: "career", label: "Career", key: "4", icon: "cap", count: this.jobsDue(), tone: "hot" },
        { id: "staff", label: "Staff", key: "5", icon: "agents", count: this.agents.filter(a => a.recentlyActive).length, tone: "acc" },
        // Finding 12: the badge is everything that waits on him, alerts included.
        { id: "inbox", label: "Inbox", key: "6", icon: "r-inbox", count: this.needsYou() + this.learnings.length + (this.outcomes ? this.outcomes.candidates.length : 0) + this.highAlerts(), tone: "hot" },
        { id: "memory", label: "Memory", key: "7", icon: "r-memory" },
        { id: "settings", label: "Settings", key: "8", icon: "r-links", count: this.connect ? this.connectMissing() : 0, tone: "hot" },
      ];
    },
    roomLabel() { return (this.rooms().find(r => r.id === this.room) || {}).label || "Atlas"; },
    // Finding 08: the address says what the screen says. Two room ids predate
    // their labels; the hash uses the label, and the old ids still route.
    ROOM_SLUG: { bridge: "today", comms: "chat" },
    roomHash() {
      const s = this.sub[this.room];
      return "#" + (this.ROOM_SLUG[this.room] || this.room) + (s && this.roomSubs()[this.room].length ? "/" + s : "");
    },
    goRoom(room, sub) {
      const subs = this.roomSubs();
      if (!(room in subs)) return;
      this.room = room;
      if (sub && subs[room].includes(sub)) this.sub[room] = sub;
      const s = this.sub[room];
      this.view = { bridge: "home", comms: "chat", inbox: "inbox", plan: "plan", career: "jobs", staff: "agents",
        memory: "agents", settings: { connections: "connect", trust: "inbox", ledger: "ledger" }[s] || "connect" }[room];
      try { localStorage.setItem("atlas.room", room); localStorage.setItem("atlas.sub", JSON.stringify(this.sub)); } catch (e) { /* private mode */ }
      if (!/^#(approval|brief|run|replay)\//.test(location.hash)) history.replaceState(null, "", this.roomHash());
      this.$nextTick(() => {
        const m = this.$refs.main;
        if (m && !(room === "comms" && s === "channel")) m.scrollTop = 0;
        if (room === "comms" && s === "channel" && !this.chatLoaded) this.openLatestChat();
        if (room === "comms" && s === "channel") { this.scrollLog(); if (window.matchMedia("(min-width: 1181px)").matches) document.querySelector(".composer textarea")?.focus(); }
        if (room === "memory" && !this.memory) this.fetchMemory();
        if (room === "memory" && s === "map") this.maybeRenderGraph();
        if (room === "settings" && s === "trust" && !this.trust) this.fetchTrust();
        if (room === "settings" && s === "connections") { this.fetchSetup(); this.fetchConnect().then(() => this.fetchMcpTools()); }
        if (room === "plan" && s === "routines") this.fetchWorkflows();
        if (room === "staff" && s === "agents" && this.agentSel && !this.agentPage) this.fetchAgentPage();
      });
    },

    // ---- Staff: one agent's record (finding 09); the Today nodes open it ----
    openAgent(name) {
      if (!name) return;
      if (this.agentSel !== name) { this.agentSel = name; this.agentPage = null; this.agentErr = ""; }
      this.goRoom("staff", "agents");
      history.replaceState(null, "", "#agent/" + encodeURIComponent(name));
      this.fetchAgentPage();
    },
    async fetchAgentPage() {
      const name = this.agentSel;
      if (!name) return;
      try {
        const page = await this._getJson("/api/agents/" + encodeURIComponent(name));
        if (this.agentSel === name) { this.agentPage = page; this.agentErr = ""; }
      } catch (e) { if (this.agentSel === name) this.agentErr = this.friendlyError(e); }
    },
    // ---- Settings, Connections: each tile carries its own checklist item (finding 13) ----
    setupItem(id) { return ((this.setup && this.setup.items) || []).find(i => i.id === id) || null; },
    setupExtra() { return ((this.setup && this.setup.items) || []).filter(i => i.id === "push" || i.id === "submitter"); },
    tileOrder(id) {
      const rank = { google: 1, push: 2, submitter: 3, canvas: 4, github: 5, alarm: 6, mcp: 7, kb: 8 }[id] || 9;
      const it = this.setupItem(id), c = this.connect;
      const done = it ? it.done
        : id === "mcp" ? !!(c && c.mcp && c.mcp.servers.length)
        : id === "kb" ? !!(this.kb && this.kb.facts_total) : false;
      return rank + (done ? 20 : 0);
    },

    // ---- Routines: the workflows in workflows/*.yml and their recent runs ----
    async fetchWorkflows() {
      try {
        const [w, r] = await Promise.all([this._getJson("/api/workflows"), this._getJson("/api/workflows/runs?limit=12")]);
        this.workflows = { names: w.workflows || [], runs: r.runs || [], err: "" };
      } catch (e) { this.workflows = { ...this.workflows, err: this.friendlyError(e) }; }
    },
    async runWorkflow(name) {
      if (!window.confirm(`Run ${name.replace(/_/g, " ")} now? Anything it sends still waits for your yes.`)) return;
      this.workflows.busy = name; this.workflows.err = "";
      try { await this._postJson(`/api/workflows/${encodeURIComponent(name)}/run`, { owner_input: "" }); }
      catch (e) { this.workflows.err = this.friendlyError(e); }
      finally { this.workflows.busy = ""; this.fetchWorkflows(); }
    },
    async cancelRun(run) {
      try { await this._postJson(`/api/workflows/runs/${run.id}/cancel`); } catch (e) { this.workflows.err = this.friendlyError(e); }
      this.fetchWorkflows();
    },
    wfTone(s) { return { completed: "blue", done: "blue", running: "blue", waiting: "amber", failed: "bad", cancelled: "dim" }[s] || "dim"; },

    // ---- Links: what each MCP server lets Atlas do, grouped by app ----
    async fetchMcpTools() {
      for (const s of (this.connect && this.connect.mcp && this.connect.mcp.servers) || []) {
        if (this.mcpTools[s.name] && !this.mcpTools[s.name].err) continue;
        this.mcpTools = { ...this.mcpTools, [s.name]: { loading: true, groups: [] } };
        try {
          // The server gives up on a remote after 20 s; the page stops waiting a little later.
          const r = await fetch(`/api/connect/mcp/${encodeURIComponent(s.name)}/tools`, { signal: AbortSignal.timeout(25000) });
          if (!r.ok) {
            let d = ""; try { d = (await r.json()).detail || ""; } catch (_) { /* not json */ }
            // A bare 404 means this web app predates the route, not that the server is gone.
            throw new Error(r.status === 404 && (!d || d === "Not Found")
              ? "This Atlas web app is older than the page and cannot list tools yet. Restart Atlas (scripts\\deploy.bat), then reload."
              : (d || `The server answered ${r.status}.`));
          }
          this.mcpTools = { ...this.mcpTools, [s.name]: await r.json() };
        }
        catch (e) {
          const err = e && e.name === "TimeoutError" ? `${s.name} did not answer within 25 seconds. Check it is running, then reload.` : this.friendlyError(e);
          this.mcpTools = { ...this.mcpTools, [s.name]: { err, groups: [] } };
        }
      }
    },
    // ---- Links: the setup checklist, one live status per chore ----
    async fetchSetup(recheck = false) {
      this.setupBusy = recheck;
      try { this.setup = await this._getJson("/api/setup" + (recheck ? "?recheck=true" : "")); }
      catch (e) { this.setup = { err: this.friendlyError(e), items: [] }; }
      finally { this.setupBusy = false; }
    },
    async sendTestPush() {
      try { await this._postJson("/api/push/test"); this.toast("Test push sent. It should arrive on the phone in a few seconds."); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    // The v2 view names, still used by older calls, mapped onto rooms.
    views() { return ["home", "chat", "inbox", "plan", "jobs", "agents", "learn", "connect", "ledger"]; },
    navItems() {
      const pc = this.plan ? this.plan.counts : null;
      return [
        { id: "home", label: "Home", icon: "home", tab: true },
        { id: "chat", label: "Chat", icon: "chat", tab: true, count: this.pending ? "…" : 0, tone: "acc" },
        { id: "inbox", label: "Inbox", icon: "inbox", tab: true, count: this.needsYou() + this.inbox.length, tone: this.needsYou() ? "hot" : "" },
        { id: "plan", label: "Plan", icon: "cal", tab: true, count: pc ? (pc.overdue || pc.today) : 0, tone: pc && pc.overdue ? "hot" : "acc" },
        { id: "jobs", label: "Jobs", icon: "cap", tab: true, count: this.jobsDue(), tone: this.jobsDue() ? "hot" : "" },
        { id: "agents", label: "Agents", icon: "agents", count: this.agents.filter(a => a.recentlyActive).length, tone: "acc" },
        { id: "learn", label: "Learn", icon: "learn", count: this.learnings.length, tone: "acc" },
        { id: "connect", label: "Connect", icon: "bolt", count: this.connect ? this.connectMissing() : 0, tone: "hot" },
        { id: "ledger", label: "Ledger", icon: "ledger" },
      ];
    },
    // Which of the named sources are not connected, by display name, for an empty
    // state that says why it is empty ("Not connected: Gmail and Calendar").
    // Google's sign-in only returns to the laptop's own address, so Connect Google works there alone.
    googleHere() { return ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname); },
    notConnected(keys) {
      const c = this.connect; if (!c) return [];
      const names = { google: "Gmail and Calendar", github: "GitHub", canvas: "Canvas" };
      return keys.filter(k => c[k] && c[k].ok !== true).map(k => names[k] || k);
    },
    // The Connections badge is the setup checklist's count of steps left, so the
    // badge and "X of Y done" never disagree; before the checklist loads, the tiles.
    connectMissing() {
      if (this.setup && Array.isArray(this.setup.items) && this.setup.items.length) return this.setup.items.filter(i => !i.done).length;
      const c = this.connect; if (!c) return 0;
      return ["google", "github", "canvas", "alarm"].filter(k => c[k] && c[k].ok !== true).length;
    },
    viewTitle() {
      return { home: "Home", chat: "Chat", inbox: "Inbox", plan: "Plan", jobs: "Jobs", agents: "Agents", learn: "Learn", connect: "Connect", ledger: "Ledger" }[this.view] || "Atlas";
    },
    viewSub() {
      return { home: "", chat: "with the CEO", inbox: "approvals, decisions, notifications, briefs", plan: "tasks, classes, calendar and the night shift, one week at a time",
        jobs: "applications, found roles, drafts", agents: "roster, runs and memory",
        learn: "from reels, videos and runs into the playbook", connect: "one tap per app; everything stays on this laptop", ledger: "what Atlas costs" }[this.view] || "";
    },
    setView(v) {
      const to = { home: ["bridge"], chat: ["comms", "channel"], inbox: ["inbox"], plan: ["plan", "week"], jobs: ["career", "queue"],
        agents: ["staff", "agents"], learn: ["inbox", "skills"], ledger: ["settings", "ledger"], connect: ["settings", "connections"] }[v];
      if (to) this.goRoom(...to);
    },
    askSend(text) {
      const t = (text || this.askDraft || "").trim();
      if (!t || this.pending) return;
      this.askDraft = "";
      this.draft = t;
      this.setView("chat");
      this.send();
    },

    // ============= HOME HELPERS =============
    greeting() {
      const h = new Date().getHours();
      const part = h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
      return `${part}, Trilok`;
    },
    todayLine() {
      const d = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
      const n = this.needsYou(), u = this.urgentCount();
      let tail = "Nothing needs you right now.";
      if (n && u) tail = `${n} ${n === 1 ? "thing needs" : "things need"} you, ${u} urgent ${u === 1 ? "signal" : "signals"}.`;
      else if (n) tail = `${n} ${n === 1 ? "thing needs" : "things need"} you.`;
      else if (u) tail = `${u} urgent ${u === 1 ? "signal" : "signals"} arrived.`;
      return `${d}. ${tail}`;
    },
    needsYou() { return this.approvals.length + this.decisions.length; },
    // A date stored as midnight UTC is a day, not a moment: shown in UTC it stays
    // the day it was entered (Oct 7), where Houston time would make it Oct 6.
    dayAgo(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      const dayOnly = d.getUTCHours() === 0 && d.getUTCMinutes() === 0 && d.getUTCSeconds() === 0;
      if (!dayOnly) return this.ago(iso);
      const days = Math.floor((Date.now() - d.getTime()) / 86400000);
      if (days < 1) return "today";
      if (days < 7) return `${days}d ago`;
      return d.toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" });
    },
    // The follow-up line is stored once ("7 days since..."); the count is redrawn
    // from the last word or the application date every time it is shown.
    nextAction(a) {
      const m = /^Follow up: \d+ days since you last heard from (.+)$/.exec(a.next_action || "");
      if (!m) return a.next_action;
      const last = [a.last_contact_at, a.applied_at].filter(Boolean).map(s => new Date(s).getTime());
      if (!last.length) return a.next_action;
      const days = Math.floor((Date.now() - Math.max(...last)) / 86400000);
      return `Follow up: ${this.plural(days, "day")} since you last heard from ${m[1]}`;
    },
    urgentCount() { return this.signals.filter(s => (s.urgency || 0) >= 3).length; },
    ago(iso) {
      if (!iso) return "";
      const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
      if (s < 60) return "just now";
      const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`;
      const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`;
      const d = Math.floor(h / 24); if (d < 7) return `${d}d ago`;
      return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    },
    monogram(name) {
      const n = String(name || "?").replace(/^llm\//, "");
      if (n.length <= 3) return n.toUpperCase();
      const parts = n.split(/[_\-\s/]+/).filter(Boolean);
      if (parts.length > 1) return (parts[0][0] + parts[1][0]).toUpperCase();
      return n[0].toUpperCase() + n[1];
    },
    // Agent badges stay inside the HUD's cold blues; the name tells them apart.
    avStyle(name) {
      const hues = [188, 196, 204, 212, 220, 182];
      let h = 0; for (const c of String(name || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
      const hue = hues[h % hues.length];
      return `background: hsl(${hue} 90% 60% / 0.10); color: hsl(${hue} 95% 74%); box-shadow: inset 0 0 0 1px hsl(${hue} 90% 60% / 0.35);`;
    },
    // The HUD is dark only; kept for the avatar tint helper.
    isLight() { return false; },
    sourceIcon(src) { return { github: "branch", gmail: "mail", canvas: "cap", gcal: "cal", capture: "bolt" }[src] || "dot"; },
    sourceLabel(src) { return { github: "GitHub", gmail: "Gmail", canvas: "Canvas", gcal: "Calendar", capture: "Captured" }[src] || src; },
    urgencyLabel(u) { return u >= 3 ? "Urgent" : u === 2 ? "Soon" : u === 1 ? "This week" : "FYI"; },
    urgencyTone(u) { return u >= 3 ? "urgent" : u === 2 ? "soon" : ""; },
    kindLabel(k) { return this.cap(String(k || "").replace("brief/", "").replace(/_/g, " ")); },
    agentName(n) {
      const s = String(n || "").replace(/^llm\//, "");
      if (/^(ceo|cfo|coo|cto|cmo)$/i.test(s)) return s.toUpperCase();
      return this.cap(s.replace(/_/g, " "));
    },
    kindColor(kind) {
      return ({ client: "var(--accent)", college: "var(--violet)", own: "var(--amber)", atlas: "var(--sky)",
        hobby: "var(--green)", personal: "var(--rose)" })[kind] || "var(--t3)";
    },
    capRatio() {
      const cap = this.cost.global_daily_cap || 0;
      const t = this.cost.today ? parseFloat(this.cost.today.total) || 0 : 0;
      return cap > 0 ? Math.min(1, t / cap) : 0;
    },
    capTone() { const r = this.capRatio(); return r >= 0.9 ? "bad" : r >= 0.7 ? "warn" : ""; },
    gaugeDash() { const c = 2 * Math.PI * 26; return `${(c * this.capRatio()).toFixed(1)} ${c.toFixed(1)}`; },
    chartPts(w, h, pad) {
      if (!this.spark.length) return [];
      const max = Math.max(...this.spark.map(p => p.y), 0.01);
      const n = Math.max(this.spark.length - 1, 1);
      return this.spark.map((p, i) => ({ x: (i / n) * w, y: h - pad - (p.y / max) * (h - pad * 2) }));
    },
    chartLine(w, h, pad) {
      const pts = this.chartPts(w, h, pad); if (!pts.length) return "";
      let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i], cx = (a.x + b.x) / 2;
        d += ` C ${cx.toFixed(1)} ${a.y.toFixed(1)} ${cx.toFixed(1)} ${b.y.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
      }
      return d;
    },
    chartArea(w, h, pad) {
      const pts = this.chartPts(w, h, pad); if (!pts.length) return "";
      return `${this.chartLine(w, h, pad)} L ${pts[pts.length - 1].x.toFixed(1)} ${h} L ${pts[0].x.toFixed(1)} ${h} Z`;
    },
    sparkMax() { return this.spark.length ? Math.max(...this.spark.map(p => p.y)) : 0; },
    sparkLabel(i) {
      if (!this.spark.length) return "";
      const p = this.spark.at(i); if (!p || !p.label) return "";
      return new Date(p.label + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
    },
    async fetchProjects() {
      try {
        const j = await this._getJson("/api/projects?status=active");
        this.projects = j.projects || []; this.projectsErr = ""; this.projectsLoaded = true;
      } catch (e) { this.projectsErr = this.friendlyError(e); }
    },
    go(id) {
      const roomOf = { chat: ["comms", "channel"], agents: ["staff", "agents"], graph: ["memory", "map"], activity: ["staff", "runs"],
        approvals: ["inbox", "approvals"], decisions: ["inbox", "approvals"], needs: ["inbox", "approvals"], inbox: ["inbox", "notifications"],
        today: ["inbox", "signals"], briefs: ["comms", "briefings"], news: ["comms", "briefings"], recordings: ["comms", "recordings"],
        cost: ["settings", "ledger"], money: ["settings", "ledger"], standard: ["settings", "ledger"], learnings: ["inbox", "skills"],
        misses: ["inbox", "skills"], playbook: ["staff", "playbook"], plan: ["plan", "week"], week: ["plan", "week"], night: ["plan", "night"],
        queue: ["career", "queue"], applications: ["career", "pipeline"], roles: ["career", "discover"], answers: ["career", "profile"],
        memory: ["memory", "facts"], trust: ["settings", "trust"] };
      if (roomOf[id]) this.goRoom(...roomOf[id]);
      this.$nextTick(() => {
        const el = document.getElementById(id);
        if (el && id !== "chat") el.scrollIntoView({ behavior: this._reducedMotion ? "auto" : "smooth", block: "start" });
      });
    },

    startClock() {
      const tick = () => {
        const d = new Date();
        const hh = String(d.getHours()).padStart(2,"0");
        const mm = String(d.getMinutes()).padStart(2,"0");
        this.clock = `${hh}:${mm}`;
      };
      tick();
      setInterval(tick, 30_000);
    },

    wavePath() {
      const w = 48, h = 14, amp = this.pending ? 4.5 : 1.2, mid = h / 2, pts = 16;
      let d = `M 0 ${mid}`;
      for (let i = 1; i <= pts; i++) {
        const x = (w / pts) * i;
        const y = mid + Math.sin(this.wavePhase + i * 0.55) * amp;
        d += ` L ${x.toFixed(1)} ${y.toFixed(1)}`;
      }
      return d;
    },

    fmtTime(iso) {
      if (!iso) return "?";
      const d = new Date(iso);
      return `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;
    },

    agentLastLabel(a) {
      if (!a.last_active) return "idle";
      const ms = Date.now() - new Date(a.last_active).getTime();
      const m = Math.floor(ms / 60000);
      if (m < 1) return "now";
      if (m < 60) return m + "m ago";
      const h = Math.floor(m / 60);
      if (h < 24) return h + "h ago";
      return Math.floor(h / 24) + "d ago";
    },
    capPct(a) {
      if (!a.monthly_cap) return 0;
      const cap = parseFloat(a.monthly_cap);
      const sp = parseFloat(a.month_spent || "0");
      if (cap === 0) return 100;
      return Math.min(100, (sp / cap) * 100).toFixed(0);
    },
    capClass(a) {
      const p = parseFloat(this.capPct(a));
      if (p >= 90) return "bad";
      if (p >= 70) return "warn";
      return "";
    },

    // "1 story", "17 stories", "3 days": a consonant before a final y takes -ies.
    plural(n, word) { return n === 1 ? `${n} ${word}` : `${n} ${/[^aeiou]y$/i.test(word) ? word.slice(0, -1) + "ies" : word + "s"}`; },
    cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); },
    friendlyError(e) {
      const m = String((e && e.message) || e || "");
      // fetch() rejects with these when the server is down or restarting.
      if (/Failed to fetch|NetworkError|Load failed|network connection was lost/i.test(m)) {
        return "Atlas isn't answering right now. It may be restarting; try again in a minute.";
      }
      return m;
    },
    dueLabel(d) {
      if (!d) return "";
      const due = new Date(String(d).slice(0, 10) + "T12:00:00"), now = new Date(); now.setHours(12, 0, 0, 0);
      const days = Math.round((due - now) / 86400000);
      if (days === 0) return "due today";
      if (days === 1) return "due tomorrow";
      if (days === -1) return "due yesterday";
      if (days < 0) return `${-days} days overdue`;
      if (days < 7) return "due " + due.toLocaleDateString(undefined, { weekday: "long" });
      return "due " + due.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    },
    askAbout(p) {
      this.draft = `Where does ${p.name} stand, and what is my next action?`;
      this.setView("chat");
    },
    costNumHtml() {
      if (!this.cost.today) return "$0<span class=\"cents\">.00</span>";
      const v = parseFloat(this.cost.today.total) || 0;
      if (v > 0 && v < 0.01) return "&lt;$0<span class=\"cents\">.01</span>";
      const [whole, cents] = this._cents(v).toFixed(2).split(".");
      return `$${Number(whole).toLocaleString()}<span class="cents">.${cents}</span>`;
    },

    // Round half up to the cent once, so the header, tiles and ledger agree.
    _cents(v) { return Math.round((Number(v) || 0) * 100 + 1e-6) / 100; },
    fmtMoney(n) { const v = Number(n) || 0; return v > 0 && v < 0.01 ? "<$0.01" : "$" + this._cents(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); },
    _costRows(obj, limit) {
      const entries = Object.entries(obj || {})
        .map(([key, v]) => ({ key, val: parseFloat(v) || 0 }))
        .filter(e => e.val > 0)
        .sort((a, b) => b.val - a.val);
      const total = entries.reduce((s, e) => s + e.val, 0) || 1;
      return entries.slice(0, limit).map(e => ({
        key: e.key, val: e.val,
        pct: Math.max(3, (e.val / total) * 100),
      }));
    },
    costByModel() { return this._costRows(this.cost.today && this.cost.today.by_model, 3); },
    costByAgent() { return this._costRows(this.cost.today && this.cost.today.by_agent, 4); },
    modelColor(family) {
      return ({ opus: "var(--accent-2)", sonnet: "var(--accent)", haiku: "var(--gold)" })[family]
        || "var(--text-dim)";
    },
    caps() {
      return (this.cost.agent_caps || []).map(c => {
        const cap = parseFloat(c.monthly_cap) || 0;
        const spent = parseFloat(c.month_spent) || 0;
        return { agent: c.agent, cap, spent,
          pct: cap > 0 ? Math.min(100, (spent / cap) * 100) : 0 };
      }).sort((a, b) => b.pct - a.pct);
    },
    capColor(pct) {
      if (pct >= 90) return "var(--rose)";
      if (pct >= 70) return "var(--amber)";
      return "var(--accent)";
    },

    async refresh(full = false) {
      if (this._refreshing) return;
      this._refreshing = true;
      this._tick = (this._tick || 0) + 1;
      // Three tiers: what needs you (every tick, 15 s), the working lists
      // (every minute), and the heavy views (every two minutes).
      const mid = full || this._tick % 4 === 0;
      const heavy = full || this._tick % 8 === 0;
      const jobs = [this.fetchApprovals(), this.fetchInbox(), this.fetchHealth()];
      // A queue that is drafting changes every few seconds; follow it closely.
      if (!mid && this.view === "jobs" && (this.queueWorking() || this.queue.submitting)) jobs.push(this.fetchQueue());
      if (mid) {
        jobs.push(this.fetchEpisodes(), this.fetchDecisions(), this.fetchLearnings(), this.fetchSkills(),
                  this.fetchAgents(), this.fetchSignals(), this.fetchBriefs(), this.fetchProjects(), this.fetchCost(),
                  this.fetchJobs(), this.fetchAnswers(), this.fetchQueue(), this.fetchStandard(), this.fetchCostPanel(), this.fetchNight(),
                  this.fetchRecordings(), this.fetchPlan(), this.fetchNews(), this.fetchOutcomes(), this.fetchMoney(),
                  this.fetchDay(), this.fetchCapabilities());
      }
      if (heavy) jobs.push(this.fetchCostHistory(), this.fetchGraph(), this.fetchMemory(), this.fetchTrust());
      if (heavy && !this.connect) jobs.push(this.fetchConnect(false));
      if (heavy) jobs.push(this.fetchSetup());
      if (mid && !this._storyDone) { this._storyDone = true; jobs.push(this.fetchStory()); }
      try { await Promise.all(jobs); } finally { this._refreshing = false; }
      if (heavy) this.maybeRenderGraph();
    },

    maybeRenderGraph() {
      // Only redraw (and re-run the force layout) when the graph actually
      // changed. Redrawing every poll was the main source of dashboard jank.
      const sig = this.graph.nodes.map(n => n.id).join(",") + "|" +
                  ((this.graph.edges || []).length);
      if (sig === this._graphSig) return;
      this._graphSig = sig;
      this.renderGraph();
    },

    async fetchCost() {
      try { const r = await fetch("/api/cost"); if (r.ok) this.cost = await r.json(); }
      catch (e) { console.warn(e); }
    },
    // Mission control: one live stream of everything Atlas does, from every
    // process. Backfill first, then Server-Sent Events; reconnect with `since`.
    async connectEvents() {
      if (this._es) return;
      if (!this.live.feed.length) {
        try { const r = await fetch("/api/events?limit=30"); if (r.ok) { const evs = (await r.json()).events; this.live.feed = evs.slice().reverse(); this.live.lastId = evs.length ? evs[evs.length - 1].id : 0; } }
        catch (e) { /* the stream will fill it */ }
      }
      const es = new EventSource("/api/events/stream" + (this.live.lastId ? `?since=${this.live.lastId}` : ""));
      this._es = es;
      es.onopen = () => { this.live.connected = true; };
      es.onmessage = (m) => { let ev; try { ev = JSON.parse(m.data); } catch (e) { return; } this.onEvent(ev); };
      es.onerror = () => { this.live.connected = false; es.close(); this._es = null; setTimeout(() => this.connectEvents(), 3000); };
    },
    onEvent(ev) {
      if ((ev.id || 0) <= (this.live.lastId || 0)) return;
      this.live.lastId = ev.id;
      this.live.feed.unshift(ev); if (this.live.feed.length > 40) this.live.feed.length = 40;
      const a = ev.agent;
      if (ev.kind === "run.started" && a) this.live.agents[a] = { step: "starting", at: ev.at, episode_id: ev.episode_id, title: ev.title };
      else if (ev.kind === "run.tool" && a) this.live.agents[a] = { ...(this.live.agents[a] || {}), step: ev.title, at: ev.at, episode_id: ev.episode_id };
      else if (ev.kind === "run.finished" && a) {
        this.live.agents[a] = { ...(this.live.agents[a] || {}), step: "done", done: true, episode_id: ev.episode_id };
        setTimeout(() => { const cur = this.live.agents[a]; if (cur && cur.done) delete this.live.agents[a]; }, 8000);
      }
      if (ev.kind === "approval.asked" || ev.kind === "approval.decided") this.fetchApprovals();
      else if (ev.kind === "notified") this.fetchInbox();
      else if (ev.kind.startsWith("submit.")) this.fetchQueue();
      else if (ev.kind === "night.task") { this.fetchNight(); this.fetchPlan(); }
      else if (ev.kind === "recording") { this.fetchRecordings(); this.fetchPlan(); }
      else if (ev.kind === "news.brief") { this.fetchNews(); this.fetchBriefs(); }
      else if (ev.kind === "outcome") { this.fetchOutcomes(); this.fetchSkills(); this.fetchStandard(); }
      else if (ev.kind === "run.finished") { this.fetchEpisodes(); this.fetchCost(); }
      // The dial moves with anything that finishes; one fetch per burst.
      if (["run.finished", "night.task", "approval.finished", "submit.status", "recording", "news.brief"].includes(ev.kind)) {
        clearTimeout(this._dayT); this._dayT = setTimeout(() => this.fetchDay(), 1200);
      }
    },

    // ============= THE BRIDGE =============
    async fetchDay() {
      try { const r = await fetch("/api/day"); if (r.ok) this.day = await r.json(); }
      catch (e) { console.warn(e); }
    },
    // Up to four agents for the dial: the live ones, then the most recent.
    bridgeAgents() {
      const live = this.liveAgents().map(w => ({ name: w.name, live: !w.done, episode_id: w.episode_id || null,
        step: w.done ? "finished" : (w.step === "starting" ? "starting" : "using " + w.step) }));
      const seen = new Set(live.map(a => a.name));
      const rest = this.agents.filter(a => !seen.has(a.name) && a.registered !== false)
        .sort((a, b) => new Date(b.last_active || 0) - new Date(a.last_active || 0))
        .map(a => ({ name: a.name, live: false, step: a.last_active ? "last run " + this.agentLastLabel(a) : "standby" }));
      return [...live, ...rest].slice(0, 4);
    },
    liveCount() { return this.liveAgents().filter(w => !w.done).length; },
    // Called from x-effect on the dial: recomputes when the day, the agents,
    // the clock or the film's frame move.
    drawDial() {
      const c = this.clock, agents = this.bridgeAgents(), frame = this.film.i, on = this.film.on;
      if (!this.day || !window.AtlasDial) return;
      let day = this.day, list = agents, nowIso = new Date().toISOString();
      if (on && this.film.data) {
        // The film: the needle walks the night, events light up as they happen.
        const tl = this.film.data.timeline, ev = tl[frame];
        day = { ...this.day, now: ev.at, next: [], night: this.film.data.window,
          done: tl.slice(0, frame + 1).map(e => ({ at: e.at, kind: e.kind, agent: e.agent, title: e.title, episode_id: e.episode_id })) };
        const seen = [];
        for (const e of tl.slice(0, frame + 1).reverse()) if (e.agent && e.agent !== "owner" && !seen.find(s => s.agent === e.agent)) seen.push(e);
        list = seen.slice(0, 4).map((e, k) => ({ name: e.agent, live: k === 0, step: k === 0 ? this.eventLabel(e).replace(/:$/, "") : "earlier", episode_id: e.episode_id }));
        nowIso = ev.at;
      }
      const L = window.AtlasDial.layout(day, list, 600, nowIso);
      this.dial = L;
      this.dialSvg = window.AtlasDial.render(L, this.day.tz);
      return c;
    },
    dialClick(ev) {
      if (this._dialDragged) { this._dialDragged = false; return; }
      const el = ev.target.closest && ev.target.closest("[data-ep]");
      const ep = el && el.getAttribute("data-ep");
      if (ep) this.openTrace(ep);
    },

    // ---- The night as a film, on the dial: Replay plays it; dragging the ring scrubs it ----
    lastNightDate() {
      const n = this.night;
      if (!n) return null;
      if (n.phase === "morning") return n.night;
      const d = new Date(n.night + "T12:00:00");
      d.setDate(d.getDate() - 1);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    },
    async filmStart(autoplay = true) {
      this.filmPause();
      try {
        const night = this.lastNightDate();
        const d = await this._getJson("/api/night/replay" + (night ? `?night=${encodeURIComponent(night)}` : ""));
        const tl = (d.timeline || []).filter(e => e.at);
        if (!tl.length) { this.toast(`Nothing happened on the night of ${d.night}. The film covers 22:00 to 09:00.`); return false; }
        this.film = { on: true, data: { ...d, timeline: tl }, i: 0, playing: false, timer: null };
        if (autoplay) this.filmPlay();
        return true;
      } catch (e) { this.toast(this.friendlyError(e)); return false; }
    },
    filmPlay() {
      if (!this.film.on) return;
      if (this.film.i >= this.film.data.timeline.length - 1) this.film.i = 0;
      this.film.playing = true;
      clearInterval(this.film.timer);
      this.film.timer = setInterval(() => {
        if (!this.film.on || this.film.i >= this.film.data.timeline.length - 1) { this.filmPause(); return; }
        this.film.i += 1;
      }, this._reducedMotion ? 1600 : 650);
    },
    filmPause() { if (this.film.timer) clearInterval(this.film.timer); this.film.playing = false; this.film.timer = null; },
    filmStop() { this.filmPause(); this.film = { on: false, data: null, i: 0, playing: false, timer: null }; },
    filmEvent() { return this.film.on && this.film.data ? this.film.data.timeline[this.film.i] : null; },
    filmSeek(deg) {
      if (!this.film.on || !window.AtlasDial) return;
      const tz = this.day ? this.day.tz : "UTC";
      let best = 0, bestD = 1e9;
      this.film.data.timeline.forEach((e, k) => {
        const a = window.AtlasDial.angleOf(e.at, tz), d = Math.abs(((a - deg + 540) % 360) - 180);
        if (d < bestD) { bestD = d; best = k; }
      });
      this.film.i = best;
    },
    // Pointer on the dial's ring: grab the needle and scrub the night.
    async dialDown(ev) {
      const svg = ev.currentTarget, m = svg.getScreenCTM && svg.getScreenCTM();
      if (!m) return;
      const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse());
      const dx = p.x - 300, dy = p.y - 300, r = Math.hypot(dx, dy);
      if (r < 130 || r > 235) return;
      ev.preventDefault();
      const deg = (Math.atan2(dy, dx) * 180 / Math.PI + 90 + 360) % 360;
      this._dialDrag = { svg, id: ev.pointerId, moved: false };
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* not capturable */ }
      if (!this.film.on && !(await this.filmStart(false))) { this._dialDrag = null; return; }
      this.filmPause();
      this.filmSeek(deg);
    },
    dialMove(ev) {
      const g = this._dialDrag;
      if (!g || !this.film.on) return;
      const m = g.svg.getScreenCTM();
      const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse());
      g.moved = true;
      this.filmSeek((Math.atan2(p.y - 300, p.x - 300) * 180 / Math.PI + 90 + 360) % 360);
    },
    dialUp() { if (this._dialDrag) { this._dialDragged = this._dialDrag.moved; this._dialDrag = null; } },
    // After he talks to Atlas the plan may have moved: refetch and flash the dial.
    async replanPulse() {
      await Promise.all([this.fetchDay(), this.fetchPlan()]);
      this.replanned = true;
      const svg = document.querySelector(".dial");
      setTimeout(() => { this.replanned = false; }, 1600);
    },
    // Last night's results and the day's finished runs, newest first, for the report.
    overnight() {
      const n = this.night;
      const tasks = n ? (n.phase === "morning" ? n.tasks : n.previous) || [] : [];
      const fin = tasks.filter(t => ["done", "needs_you", "failed", "held"].includes(t.status))
        .map(t => ({ id: "t" + t.id, kind: "night", at: t.finished_at || t.created_at, title: t.title, agent: t.agent, task: t }));
      const taskEps = new Set(tasks.map(t => t.episode_id).filter(Boolean));
      const runs = ((this.day && this.day.done) || []).filter(d => d.kind !== "night.task" && !taskEps.has(d.episode_id)).slice().reverse()
        .map((d, i) => ({ id: "d" + i + d.at, kind: d.kind, at: d.at, agent: d.agent, episode_id: d.episode_id,
          title: String(d.title || "").replace(/^[a-z_]+:\s*/i, "") }));
      return [...fin, ...runs].slice(0, 7);
    },
    // What each line of the report lets him do: take it back, see why, or see the proof.
    actionsFor(it) {
      if (it.kind === "night") {
        const t = it.task, out = [];
        if (["done", "needs_you"].includes(t.status) && !t.review) {
          out.push({ label: "ACCEPT", tone: "", act: () => this.nightReview(t, "accepted") });
          out.push({ label: "UNDO", tone: "amber", title: "Send it back as rework", act: () => this.nightReview(t, "rework") });
        }
        if (t.episode_id) out.push({ label: "WHY", tone: "", act: () => this.openTrace(t.episode_id) });
        return out;
      }
      if (it.kind === "submit.status") return [{ label: "PROOF", tone: "", act: () => this.goRoom("career", "queue") }];
      if (it.kind === "news.brief") return [{ label: "READ", tone: "", act: () => this.goRoom("comms", "briefings") }];
      if (it.kind === "recording") return [{ label: "NOTES", tone: "", act: () => this.goRoom("comms", "recordings") }];
      return it.episode_id ? [{ label: "WHY", tone: "", act: () => this.openTrace(it.episode_id) }] : [];
    },
    dayLine() {
      const d = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
      if (!this.day) return d + ".";
      const t = this.day.totals.done, n = this.needsYou();
      const done = t ? `${t} ${t === 1 ? "thing" : "things"} got done in the last 24 hours` : "Nothing finished in the last 24 hours";
      return `${d}. ${done}, ${n ? `${n} ${n === 1 ? "needs" : "need"} your yes` : "nothing needs you"}.`;
    },
    nightCap() { return this.night ? Number(this.night.cap) || 0 : 0; },
    spendLine() {
      const t = this.cost.today ? Number(this.cost.today.total) || 0 : 0, cap = Number(this.cost.global_daily_cap) || 0;
      return this.fmtMoney(t) + (cap ? " / " + this.fmtMoney(cap) + " DAILY CAP" : " TODAY");
    },
    // The reliability SLO from /api/health: briefs on time, the loop ticking.
    sloRows() { return (this.health && this.health.slo && this.health.slo.objectives) || []; },
    trustLine() {
      if (!this.trust) return "TRUST LADDER LOADING";
      const c = this.trust.counts || {};
      return `${c.approve || 0} ASK FIRST · ${c.auto || 0} ON THEIR OWN · ${c.deny || 0} NEVER`;
    },
    // Record: a lecture or a meeting, from the PWA or a Voice Memo upload.
    async fetchRecordings() {
      try { const r = await fetch("/api/recordings"); if (r.ok) this.recordings = (await r.json()).recordings; }
      catch (e) { console.warn(e); }
    },
    recLabel(s) { return { uploaded: "Saved", transcribing: "Transcribing…", transcribed: "Transcribed", noting: "Writing notes…", done: "Notes ready", failed: "Failed" }[s] || s; },
    recTone(s) { return { done: "green", failed: "urgent", transcribing: "acc", noting: "acc" }[s] || ""; },
    recAgent(kind) { return { lecture: "academic", meeting: "coo", note: "ceo" }[kind] || "ceo"; },
    async recToggle() { if (this.rec.active) return this.recStop(); await this.recStart(); },
    async recStart() {
      if (this.rec.active) return;
      this.rec.err = "";
      if (!navigator.mediaDevices || !window.MediaRecorder) { this.rec.err = "This browser cannot record. Upload a Voice Memo instead."; return; }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find(m => MediaRecorder.isTypeSupported(m)) || "";
        const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        this.rec.chunks = [];
        mr.ondataavailable = (e) => { if (e.data && e.data.size) this.rec.chunks.push(e.data); };
        mr.onstop = () => { stream.getTracks().forEach(t => t.stop()); this.recFinish(); };
        mr.start(5000);
        this._mr = mr; this.rec.mime = mr.mimeType || mime; this.rec.active = true; this.rec.started = Date.now(); this.rec.elapsed = "0:00";
        this._recTimer = setInterval(() => { const s = Math.floor((Date.now() - this.rec.started) / 1000); this.rec.elapsed = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }, 1000);
        try { this._wake = await navigator.wakeLock.request("screen"); } catch (e) { /* not supported */ }
      } catch (e) { this.rec.err = "Microphone access was refused. Allow it, or upload a Voice Memo."; }
    },
    recStop() {
      clearInterval(this._recTimer);
      if (this._wake) { try { this._wake.release(); } catch (e) { /* ignore */ } this._wake = null; }
      if (this._mr && this._mr.state !== "inactive") this._mr.stop(); else this.rec.active = false;
    },
    async recFinish() {
      this.rec.active = false;
      const blob = new Blob(this.rec.chunks, { type: this.rec.mime || "audio/webm" });
      this.rec.chunks = [];
      if (!blob.size) { this.rec.err = "Nothing was recorded."; return; }
      await this.recUpload(new File([blob], /mp4/.test(blob.type) ? "recording.m4a" : "recording.webm", { type: blob.type }));
    },
    async recUpload(file) {
      if (!file) return;
      this.rec.busy = true; this.rec.err = "";
      try {
        const fd = new FormData(); fd.append("file", file); fd.append("kind", this.rec.kind); fd.append("title", this.rec.title || "");
        const r = await fetch("/api/recordings", { method: "POST", body: fd });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `Server answered ${r.status}`);
        this.rec.title = ""; this.pushMsg = "Saved. Transcribing on this laptop; notes land in Comms, Recordings.";
        this.goRoom("comms", "recordings");
        await this.fetchRecordings();
      } catch (e) { this.rec.err = this.friendlyError(e); }
      finally { this.rec.busy = false; }
    },
    async recOpen(r) {
      try { const j = await this._getJson(`/api/recordings/${r.id}`); this.modal = { type: "recording", row: j.recording }; }
      catch (e) { this.rec.err = this.friendlyError(e); }
    },
    async recRetry(r) {
      try { await this._postJson(`/api/recordings/${r.id}/process`); await this.fetchRecordings(); }
      catch (e) { this.rec.err = this.friendlyError(e); }
    },
    liveAgents() { return Object.entries(this.live.agents).map(([name, s]) => ({ name, ...s })); },
    eventLabel(ev) {
      const who = ev.agent ? this.agentName(ev.agent) : "Atlas";
      const ok = ev.data && ev.data.ok !== false;
      return ({
        "run.started": `${who} started:`, "run.tool": `${who} is using`, "run.tool.done": ok ? `${who} got back from` : `${who} hit an error in`,
        "run.finished": `${who} finished:`, "dispatch": `${who}`, "approval.asked": "Waiting on you:",
        "approval.decided": `You said ${(ev.data && ev.data.decision) || "…"}:`, "notified": "Sent you:",
        "approval.finished": ok ? `${who} finished on your yes:` : `${who} could not finish:`,
        "submit.shot": "Screenshot:", "submit.status": "Application:", "night.task": `Night shift (${who}):`, "workflow.step": "Workflow:",
        "recording": "Recording:", "news.brief": "Morning AI brief:",
        "web.restarted": "Watchdog:", "web.down": "Watchdog:", "web.recovered": "Watchdog:",
        "outcome": `Outcome (${(ev.data && ev.data.verdict) || "…"}):`,
      })[ev.kind] || ev.kind;
    },
    // The night shift card on Home.
    async fetchNight() {
      try { const r = await fetch("/api/night"); if (r.ok) this.night = await r.json(); }
      catch (e) { console.warn(e); }
    },
    nightOpen() { return ((this.night && this.night.tasks) || []).filter(t => t.status === "proposed"); },
    nightLabel(s) { return { proposed: "Proposed", approved: "Approved", skipped: "Skipped", running: "Running", done: "Done", needs_you: "Needs you", failed: "Failed", held: "Held" }[s] || s; },
    nightTone(s) { return { approved: "acc", running: "acc", done: "green", needs_you: "soon", failed: "urgent", held: "soon" }[s] || ""; },
    async nightDecide(t, decision) {
      t._busy = decision === "approve" ? "Approving…" : "Skipping…"; t._err = "";
      try { await this._postJson(`/api/night/tasks/${t.id}/decide`, { decision }); t._busy = ""; await this.fetchNight(); }
      catch (e) { t._busy = ""; t._err = this.friendlyError(e); }
    },
    async nightReview(t, verdict) {
      t._busy = "Saving…"; t._err = "";
      try { await this._postJson(`/api/night/tasks/${t.id}/review`, { verdict }); t._busy = ""; await Promise.all([this.fetchNight(), this.fetchStandard()]); }
      catch (e) { t._busy = ""; t._err = this.friendlyError(e); }
    },
    async nightApproveAll() {
      this.nightBusy = true;
      try { await this._postJson("/api/night/approve-all"); await this.fetchNight(); }
      catch (e) { this.nightErr = this.friendlyError(e); }
      finally { this.nightBusy = false; }
    },
    async nightPropose() {
      this.nightBusy = true;
      try { this.night = await this._postJson("/api/night/propose"); }
      catch (e) { this.nightErr = this.friendlyError(e); }
      finally { this.nightBusy = false; }
    },
    async nightRun() {
      this.nightBusy = true; this.nightErr = "";
      try { await this._postJson("/api/night/run"); await Promise.all([this.fetchNight(), this.fetchCost()]); }
      catch (e) { this.nightErr = this.friendlyError(e); }
      finally { this.nightBusy = false; }
    },
    async nightAdd() {
      this.nightBusy = true; this.nightErr = "";
      try {
        await this._postJson("/api/night/tasks", { title: this.nightForm.title, definition_of_done: this.nightForm.dod,
          project: this.nightForm.project || null, agent: this.nightForm.agent || null });
        this.nightForm = { title: "", dod: "", project: "", agent: "" }; this.nightAdding = false;
        await this.fetchNight();
      } catch (e) { this.nightErr = this.friendlyError(e); }
      finally { this.nightBusy = false; }
    },
    // The small efficiency card beside the Standard (r12).
    async fetchCostPanel() {
      try { this.costPanel = await this._getJson("/api/cost/panel"); } catch (e) { console.warn(e); }
    },
    // The Atlas Standard card on Home.
    async fetchStandard() {
      try { const r = await fetch("/api/standard"); if (r.ok) this.standard = await r.json(); }
      catch (e) { console.warn(e); }
    },
    async standardStart() {
      this.standardBusy = true;
      try { this.standard = await this._postJson("/api/standard/start"); }
      catch (e) { this.error = this.friendlyError(e); }
      finally { this.standardBusy = false; }
    },
    standardDay() {
      if (!this.standard) return "";
      return this.standard.started ? `Day ${this.standard.day} of ${this.standard.days}` : "Not started";
    },
    standardSummary() {
      if (!this.standard) return "";
      const s = this.standard;
      return `${s.measured} of ${s.total} measured today · ${s.passing} passing · the rest say what they are waiting on`;
    },
    async fetchCostHistory() {
      try {
        const r = await fetch("/api/cost/history?days=14");
        if (r.ok) {
          const data = await r.json();
          this.spark = data.series.map((p, i) => ({ x: i, y: parseFloat(p.total), label: p.day }));
        }
      } catch (e) { console.warn(e); }
    },

    sparkPoints() {
      if (!this.spark.length) return [];
      const w = 220, h = 56, pad = 6;
      const max = Math.max(...this.spark.map(p => p.y), 0.01);
      return this.spark.map((p, i) => ({
        x: (i / (this.spark.length - 1)) * (w - pad * 2) + pad,
        y: h - pad - (p.y / max) * (h - pad * 2),
      }));
    },
    sparkLinePath() {
      const pts = this.sparkPoints();
      if (!pts.length) return "";
      let d = `M ${pts[0].x} ${pts[0].y}`;
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i-1], b = pts[i];
        const cx = (a.x + b.x) / 2;
        d += ` Q ${cx} ${a.y} ${cx} ${(a.y + b.y) / 2}`;
        d += ` Q ${cx} ${b.y} ${b.x} ${b.y}`;
      }
      return d;
    },
    sparkAreaPath() {
      const line = this.sparkLinePath();
      const pts = this.sparkPoints();
      if (!pts.length) return "";
      return `${line} L ${pts[pts.length-1].x} 56 L ${pts[0].x} 56 Z`;
    },

    // ---- PWA: push, approvals, inbox, briefs, signals, hash routes ----
    fmtWhen(iso) {
      if (!iso) return "?";
      const d = new Date(iso), now = new Date();
      const hm = `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;
      if (d.toDateString() === now.toDateString()) return hm;
      return `${d.toLocaleDateString(undefined, {month: "short", day: "numeric"})} ${hm}`;
    },
    pretty(obj) {
      try { return typeof obj === "string" ? obj : JSON.stringify(obj, null, 2); }
      catch (_) { return String(obj); }
    },
    b64ToBytes(b64) {
      const pad = "=".repeat((4 - b64.length % 4) % 4);
      const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
      return Uint8Array.from(raw, (c) => c.charCodeAt(0));
    },

    async pwaInit() {
      this.pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
      if ("serviceWorker" in navigator) {
        try {
          this._swReg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
          const sub = await this._swReg.pushManager.getSubscription();
          this.pushOn = !!sub;
        } catch (e) { this.pushMsg = "service worker: " + (e.message || e); }
      }
      window.addEventListener("hashchange", () => this.routeHash());
      this.connectEvents();
      this.routeHash();
    },
    pushTitle() {
      if (!this.pushSupported) return "Push needs Atlas installed from Safari: Share, Add to Home Screen";
      return this.pushOn ? "Push is on for this device. Click to turn it off" : "Turn on push notifications on this device";
    },
    async togglePush() {
      if (this.pushBusy) return;
      if (!this.pushSupported) { this.pushMsg = "Install Atlas to the Home Screen first (Share, Add to Home Screen), then enable push."; return; }
      this.pushBusy = true;
      try {
        const reg = this._swReg || await navigator.serviceWorker.ready;
        if (this.pushOn) {
          const sub = await reg.pushManager.getSubscription();
          if (sub) {
            await fetch("/api/push/subscribe", { method: "DELETE", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({ endpoint: sub.endpoint }) });
            await sub.unsubscribe();
          }
          this.pushOn = false; this.pushMsg = "";
        } else {
          const perm = await Notification.requestPermission();
          if (perm !== "granted") { this.pushMsg = "Notifications were not allowed for Atlas."; return; }
          const key = await (await fetch("/api/push/vapid-public-key")).json();
          if (!key.configured) { this.pushMsg = "Push is not configured on the server yet (ATLAS_VAPID_* in .env)."; return; }
          const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: this.b64ToBytes(key.key) });
          const j = sub.toJSON();
          const r = await fetch("/api/push/subscribe", { method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ endpoint: j.endpoint, keys: j.keys, user_agent: navigator.userAgent }) });
          if (!r.ok) throw new Error(await r.text());
          this.pushOn = true; this.pushMsg = "";
        }
      } catch (e) { this.pushMsg = "push: " + (e.message || e); }
      finally {
        this.pushBusy = false;
        // The server-side count moves now: the health warning and the checklist follow.
        this.fetchHealth(); if (this.setup) this.fetchSetup();
      }
    },

    _merge(prev, next) {
      // Keep transient UI flags across the 10 s refresh.
      const flags = Object.fromEntries(prev.map(p => [p.id, { _busy: p._busy, _open: p._open, _err: p._err }]));
      return next.map(n => Object.assign(n, flags[n.id] || {}));
    },
    // Fails closed: an error is shown as an error, never as "You're clear".
    async fetchApprovals() {
      try {
        const r = await fetch("/api/approvals/pending");
        if (!r.ok) throw new Error(`The server answered ${r.status}. Pending approvals may exist; reload to retry.`);
        const j = await r.json();
        this.approvals = this._merge(this.approvals, j.approvals || []);
        this.approvalsErr = ""; this.approvalsLoaded = true;
      } catch (e) {
        this.approvalsErr = this.friendlyError(e) + (/answered/.test(String(e && e.message)) ? "" : " Pending approvals may exist.");
      }
    },
    async _getJson(url) {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`${url} answered ${r.status}`);
      return r.json();
    },
    async fetchInbox() {
      try { const j = await this._getJson("/api/notifications?unread=true&limit=30"); this.inbox = this._merge(this.inbox, j.notifications || []); } catch (e) { console.warn(e); }
    },
    async fetchBriefs() {
      try { const j = await this._getJson("/api/briefs?limit=7"); this.briefs = j.briefs || []; } catch (e) { console.warn(e); }
    },
    async fetchSignals() {
      try { const j = await this._getJson("/api/signals?hours=24&limit=30"); this.signals = j.signals || []; } catch (e) { console.warn(e); }
    },
    async fetchJobs() {
      if ((this.jobs.applications || []).some(a => a._busy || a._open) || (this.jobs.postings || []).some(p => p._busy)) return;
      try { this.jobs = await this._getJson("/api/jobs"); this.jobsErr = ""; this.jobsLoaded = true; }
      catch (e) { this.jobsErr = this.friendlyError(e); }
    },
    async fetchQueue() {
      if ((this.queue.items || []).some(q => q._busy || q._cover)) return;
      try { this.queue = await this._getJson("/api/jobs/queue"); this.queueErr = ""; this.queueLoaded = true; }
      catch (e) { this.queueErr = this.friendlyError(e); }
    },
    queueReady() { return (this.queue.items || []).filter(q => q.status === "ready"); },
    queueWorking() { return this.queue.building || (this.queue.items || []).some(q => q.status === "drafting"); },
    queueLabel(s) { return { drafting: "Drafting", ready: "Ready", needs_attention: "Needs a look", approved: "Approved", skipped: "Skipped", closed: "Closed", submitted: "Submitted" }[s] || s; },
    queueTone(s) { return { ready: "acc", needs_attention: "soon", approved: "green", submitted: "green" }[s] || ""; },
    // ---- Capabilities (core/capabilities/): a tutorial becomes a checked, versioned procedure ----
    async fetchCapabilities() {
      try {
        const r = await fetch("/api/capabilities");
        if (r.status === 404) { this.k2c.off = true; return; }
        if (!r.ok) return;
        const j = await r.json();
        Object.assign(this.k2c, { sources: j.sources || [], versions: j.versions || [], cap_usd: j.cap_usd, off: false });
      } catch (e) { /* the next tick tries again */ }
    },
    capToReview() { return this.k2c.versions.filter(v => !v.review); },
    capTone(s) { return { done: "var(--green)", processing: "var(--h)", queued: "var(--h)", held_budget: "var(--h2)", failed: "var(--bad)" }[s] || "var(--t3)"; },
    capLine(s) {
      const stage = { capture: "downloading", transcript: "transcribing", frames: "reading the screen", resources: "following its links",
        claims: "finding the claims", checks: "checking claims", procedure: "writing the procedure" }[s.stage] || s.stage || "";
      const state = { queued: "waiting", processing: stage ? "working: " + stage : "working", done: "done", failed: "failed", held_budget: "held for budget" }[s.status] || s.status;
      return [state, s.error || "", "$" + Number(s.cost_usd || 0).toFixed(2), this.ago(s.created_at)].filter(Boolean).join(" · ");
    },
    capCheckLabel(s) { return { partially_checked: "partly checked", not_checked: "not checked" }[s] || s || ""; },
    capFidelity(f) { return { quoted: "quoted in the source", paraphrased: "paraphrased", from_visual: "on screen", from_resource: "in the linked resource", not_located: "not located in the source" }[f] || f; },
    capVerdict(v) { return { supported: "supported", contradicted: "contradicted", inconclusive: "inconclusive", not_checked: "not checked" }[v] || "not checked"; },
    capTime(s) { const t = Number(s) || 0; return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`; },
    async shareCapability() {
      const c = this.k2c;
      if (!c.url.trim() || c.busy) return;
      c.busy = true; c.err = ""; c.msg = "";
      try {
        const out = await this._postJson("/api/capabilities/sources", { url: c.url.trim() });
        c.msg = { new: "Reading it now: download, transcript, the screen, its links, the claims, then a procedure for you to review.",
          reused: "Already read: the result is reused and nothing is charged.",
          held: "This one failed or was held before. Tap Try again to run it again (that is charged)." }[out.action] || "Started.";
        c.url = "";
        this.fetchCapabilities();
      } catch (e) { c.err = this.friendlyError(e); } finally { c.busy = false; }
    },
    async retryCapability(s) {
      try { await this._postJson(`/api/capabilities/sources/${s.id}/retry`); this.k2c.msg = "Trying again."; this.fetchCapabilities(); }
      catch (e) { this.k2c.err = this.friendlyError(e); }
    },
    async openCapability(v) {
      try {
        const j = await this._getJson(`/api/capabilities/versions/${v.id}`);
        this.modal = { type: "capability", v: j.version, evidence: j.evidence, claims: j.claims, ev: null,
          exported: !!v.exported, busy: "", err: "", done: "" };
      } catch (e) { this.k2c.err = this.friendlyError(e); }
    },
    capShow(ref) {
      if (!this.modal || this.modal.type !== "capability") return;
      this.modal.ev = (this.modal.evidence || []).find(e => e.ref === ref) || null;
    },
    async reviewCapability(decision) {
      const m = this.modal;
      if (decision === "withdrawn" && !window.confirm("Withdraw this procedure? Agents stop seeing it and it is removed from Claude Code. Its history stays.")) return;
      m.busy = decision === "approved" ? "Approving…" : "Withdrawing…"; m.err = "";
      try {
        const out = await this._postJson(`/api/capabilities/versions/${m.v.id}/review`, { decision });
        m.v = { ...m.v, review: decision };
        if (decision === "withdrawn") m.exported = false;
        m.done = decision === "approved" ? "Approved. The CTO can look it up as reference." : `Withdrawn${out.skills_removed ? " and removed from Claude Code" : ""}.`;
        this.fetchCapabilities();
      } catch (e) { m.err = this.friendlyError(e); } finally { m.busy = ""; }
    },
    async claudeInstall() {
      const m = this.modal;
      m.busy = "Installing…"; m.err = "";
      try { const out = await this._postJson(`/api/capabilities/versions/${m.v.id}/claude`); m.exported = true; m.done = "Installed in Claude Code: " + out.installed; this.fetchCapabilities(); }
      catch (e) { m.err = this.friendlyError(e); } finally { m.busy = ""; }
    },
    async claudeRemove() {
      const m = this.modal;
      m.busy = "Removing…"; m.err = "";
      try {
        const r = await fetch(`/api/capabilities/versions/${m.v.id}/claude`, { method: "DELETE" });
        if (!r.ok) throw new Error(`Server answered ${r.status}`);
        m.exported = false; m.done = "Removed from Claude Code."; this.fetchCapabilities();
      } catch (e) { m.err = this.friendlyError(e); } finally { m.busy = ""; }
    },
    // Paste a job link: Atlas finds it, tracks it, drafts, and (when asked) applies.
    async pasteJob() {
      const p = this.paste;
      if (!p.url.trim() || p.busy) return;
      p.busy = true; p.err = ""; p.done = "";
      try {
        const out = await this._postJson("/api/jobs/paste", { url: p.url.trim(), company: p.company.trim(), title: p.title.trim(), apply: p.apply });
        const where = { board: "from its board", "careers page": "from the company's careers page", linkedin: "via LinkedIn", page: "from the page" }[out.via] || "";
        p.done = out.duplicate ? `${out.title} at ${out.company} is already in your queue.`
          : `${out.title} at ${out.company} (${where}): tracked; drafting the resume and cover note now` +
            (p.apply && out.applies ? ", then Atlas applies." : out.applies ? "." : ". Atlas cannot fill this site's form, so you apply with the drafts.");
        p.url = ""; p.company = ""; p.title = ""; p.ask = false;
        this.fetchQueue(); this.fetchJobs();
      } catch (e) {
        p.err = this.friendlyError(e);
        if (/company and role/.test(p.err)) p.ask = true;
      } finally { p.busy = false; }
    },
    // The filled form in a viewer, page by page at a readable width (core/jobs/submit/shots.py).
    openShots(q) {
      const sub = q.submission || {};
      const pages = (sub.screenshots || []).map(s => `/api/jobs/submissions/${sub.id}/files/${s}`);
      if (!pages.length) return;
      this.modal = { type: "shots", title: `${q.title} at ${q.company}`, pages,
        note: this.subLabel(sub.status) + (sub.reason ? ": " + sub.reason : "") };
    },
    subLabel(s) { return { checking: "Checking", needs_answer: "Waiting on an answer", needs_click: "Needs your click", blocked: "Stopped by the pre-submit check", dry_run: "Dry run: filled, not sent", submitting: "Sent, outcome unknown: check the board", submitted: "Submitted", confirmed: "Confirmed by email" }[s] || s; },
    async queueSubmit(q) {
      q._busy = "Starting…"; q._err = "";
      try { const j = await this._postJson(`/api/jobs/queue/${q.id}/submit`); if (!j.started) q._err = "Already filling a form; try again in a minute."; q._busy = ""; await this.fetchQueue(); }
      catch (e) { q._busy = ""; q._err = this.friendlyError(e); }
    },
    async queueDecide(q, decision) {
      q._busy = decision === "apply" ? "Approving…" : "Skipping…"; q._err = "";
      try { await this._postJson(`/api/jobs/queue/${q.id}/decide`, { decision }); q._busy = ""; await Promise.all([this.fetchQueue(), this.fetchJobs()]); }
      catch (e) { q._err = this.friendlyError(e); q._busy = ""; }
    },
    async queueApplyAll() {
      this.queueBusy = true; this.queueErr = "";
      try { await this._postJson("/api/jobs/queue/apply-all"); await Promise.all([this.fetchQueue(), this.fetchJobs()]); }
      catch (e) { this.queueErr = this.friendlyError(e); }
      finally { this.queueBusy = false; }
    },
    async queueBuild() {
      this.queueBusy = true; this.queueErr = "";
      try {
        const j = await this._postJson("/api/jobs/queue/build");
        if (!j.started) this.queueErr = j.reason === "today's queue is full" ? "Today's queue is already full." : "Already working on it.";
        await this.fetchQueue();
      } catch (e) { this.queueErr = this.friendlyError(e); }
      finally { this.queueBusy = false; }
    },
    async queueRedraft(q) {
      q._busy = "Drafting again… a minute or two"; q._err = "";
      try { await this._postJson(`/api/jobs/queue/${q.id}/redraft`); q._busy = ""; await this.fetchQueue(); }
      catch (e) { q._err = this.friendlyError(e); q._busy = ""; }
    },
    async fetchAnswers() {
      // Never overwrite a field mid-edit or mid-save.
      const a = this.answers;
      const busy = [...(a.standard || []), ...(a.pending || []), ...(a.learned || [])].some(x => x._busy || (x._draft || "").trim());
      if (busy || (document.activeElement && document.activeElement.id.startsWith("ans-"))) return;
      try { this.answers = await this._getJson("/api/jobs/answers"); this.answersErr = ""; this.answersLoaded = true; }
      catch (e) { this.answersErr = this.friendlyError(e); }
    },
    answersRequired() { return (this.answers.standard || []).filter(a => a.required).length; },
    answersDone() { return this.answersRequired() - (this.answers.missing_required || []).length; },
    async saveAnswer(item, value) {
      value = (value || "").trim();
      if (value === (item.answer || "") && !item._draft) return;
      item._busy = true; item._err = ""; item._ok = false;
      try {
        const j = await this._postJson("/api/jobs/answers", { key: item.key, answer: value, question: item.question || null });
        item.answer = j.answer.answer; item._ok = true; item._draft = "";
        setTimeout(() => { item._ok = false; }, 1500);
        if (item.question && value) {  // a pending question just got its answer
          this.answers.pending = this.answers.pending.filter(p => p.key !== item.key);
          if (!this.answers.learned.some(l => l.key === item.key)) this.answers.learned.unshift(item);
        }
        const missing = new Set(this.answers.missing_required || []);
        if (item.required) { if (j.answer.answer) missing.delete(item.key); else missing.add(item.key); }
        this.answers.missing_required = [...missing];
      } catch (e) { item._err = this.friendlyError(e); }
      finally { item._busy = false; }
    },
    async forgetAnswer(item) {
      item._busy = true; item._err = "";
      try {
        await this._postJson("/api/jobs/answers/forget", { key: item.key });
        this.answers.pending = this.answers.pending.filter(p => p.key !== item.key);
        this.answers.learned = this.answers.learned.filter(l => l.key !== item.key);
      } catch (e) { item._err = this.friendlyError(e); item._busy = false; }
    },
    jobsOpen() { return (this.jobs.applications || []).filter(a => !["rejected", "withdrawn", "ghosted"].includes(a.status)); },
    jobsDue() { const now = Date.now(); return this.jobsOpen().filter(a => a.next_action && (!a.next_action_at || new Date(a.next_action_at).getTime() <= now)).length; },
    jobTone(s) { return { interview: "green", offer: "green", screening: "acc", interested: "", applied: "", rejected: "soon" }[s] || ""; },
    async _postJson(url, body) {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail ? (typeof j.detail === "string" ? j.detail : "Check the fields.") : `Server answered ${r.status}`);
      return j;
    },
    async addJob(status = "applied") {
      const f = this.jobForm;
      if (!f.company.trim() || !f.role.trim()) { f.err = "Company and role, please."; return; }
      f.busy = true; f.err = "";
      try {
        await this._postJson("/api/jobs/applications", { company: f.company.trim(), role: f.role.trim(), url: f.url.trim() || null, status });
        Object.assign(f, { company: "", role: "", url: "", open: false });
        await this.fetchJobs();
      } catch (e) { f.err = this.friendlyError(e); }
      finally { f.busy = false; }
    },
    async moveJob(a, status) {
      if (!status) return;
      a._busy = "Saving…"; a._err = "";
      try { await this._postJson(`/api/jobs/applications/${a.id}/status`, { status }); a._busy = ""; await this.fetchJobs(); }
      catch (e) { a._err = this.friendlyError(e); a._busy = ""; }
    },
    async openJob(a) {
      if (a._open) { a._open = false; return; }
      try { a._detail = await this._getJson(`/api/jobs/applications/${a.id}`); a._open = true; }
      catch (e) { a._err = this.friendlyError(e); }
    },
    async draftJob(a, kind) {
      a._busy = { prep: "Writing your prep pack… (a minute or two)", followup: "Drafting the follow-up…", cover: "Drafting the cover note…" }[kind] || "Drafting…";
      a._err = "";
      try {
        await this._postJson(`/api/jobs/applications/${a.id}/draft`, { kind });
        a._detail = await this._getJson(`/api/jobs/applications/${a.id}`); a._open = true;
      } catch (e) { a._err = this.friendlyError(e); }
      finally { a._busy = ""; }
    },
    async trackPosting(p, status) {
      p._busy = "Saving…"; p._err = "";
      try { await this._postJson(`/api/jobs/postings/${p.id}/track`, { status }); p._busy = ""; await this.fetchJobs(); }
      catch (e) { p._err = this.friendlyError(e); p._busy = ""; }
    },
    async dismissPosting(p) {
      p._busy = "Hiding…";
      try { await this._postJson(`/api/jobs/postings/${p.id}/dismiss`); this.jobs.postings = this.jobs.postings.filter(x => x.id !== p.id); }
      catch (e) { p._err = this.friendlyError(e); p._busy = ""; }
    },
    async copyText(text, a) {
      try { await navigator.clipboard.writeText(text); a._busy = "Copied"; setTimeout(() => { a._busy = ""; }, 1200); }
      catch (e) { a._err = "Copy failed; select the text instead."; }
    },
    async fetchHealth() {
      try {
        const r = await fetch("/api/health");
        this.health = await r.json();  // 503 still carries the reason
      } catch (e) {
        this.health = { ok: false, status: "unreachable", problems: [this.friendlyError(e)] };
      }
    },
    sysLabel() {
      if (this.pending) return "CEO thinking";
      const h = this.health;
      if (!h) return "Checking";
      if (h.status === "unreachable") return "Atlas offline";
      if (h.ok) return h.worker && h.worker.state === "starting" ? "Starting up" : "All systems running";
      const w = h.worker || {};
      if (w.state === "stalled" && w.age_min != null) return `Loop stalled ${this.minsLabel(w.age_min)}`;
      if (w.state === "never") return "Loop not running";
      return h.db !== "ok" ? "Database down" : "Needs attention";
    },
    sysBannerText() {
      const h = this.health || {};
      const what = (h.problems || []).join(" ");
      if (h.status === "unreachable") return " The dashboard cannot reach Atlas. If this lasts, the laptop may be asleep or offline.";
      const loop = h.worker && ["stalled", "never"].includes(h.worker.state);
      return " " + what + (loop ? " Briefs, reminders and mail checks are paused. The worker restarts itself; if this stays, check the laptop is awake and online." : "");
    },
    sysTone() { return this.pending ? "live" : (!this.health ? "" : (this.health.ok ? "ok" : "bad")); },
    // Normal only when health is ok, it has no warnings, and no high-priority alert is
    // unread: the same rule the Today room's status uses (core/core_view.py health()).
    // Finding 04: a queue built days ago says so, not "today".
    queueOldest() {
      const open = (this.queue.items || []).filter(q => ["ready", "needs_attention", "drafting", "approved"].includes(q.status) && q.created_at);
      if (!open.length) return "";
      const first = open.map(q => q.created_at).sort()[0];
      return new Date(first).toDateString() === new Date().toDateString() ? "" : this.fmtDay(first);
    },
    highAlerts() { return (this.inbox || []).filter(n => n.priority === "high").length; },
    sysNormal() { const h = this.health; return !!(h && h.ok && !(h.warnings || []).length && !this.highAlerts()); },
    sysTitle() {
      const h = this.health;
      if (!h) return "";
      const alerts = this.highAlerts() ? [`${this.plural(this.highAlerts(), "health alert")} unread in Inbox.`] : [];
      return [...(h.problems || []), ...(h.warnings || []), ...alerts].join(" ") || "Web, database and background loop are healthy.";
    },
    minsLabel(m) {
      m = Math.round(m);
      if (m < 120) return `${m} min`;
      if (m < 2880) return `${Math.round(m / 6) / 10} h`;
      return `${Math.round(m / 1440)} days`;
    },
    async decide(a, decision, fromModal = false) {
      a._busy = true; a._err = "";
      try {
        const r = await fetch(`/api/approvals/${a.id}/decide`, { method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ decision }) });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.statusText); }
        this.approvals = this.approvals.filter(x => x.id !== a.id);
        if (fromModal) { a.status = decision; setTimeout(() => this.closeModal(), 500); }
        this.fetchInbox();
      } catch (e) { a._err = e.message || String(e); }
      finally { a._busy = false; }
    },
    // The alerts-only view of Notifications, and clearing the whole list in one call.
    inboxShown() { return this.inboxAlertsOnly ? this.inbox.filter(n => n.priority === "high") : this.inbox; },
    async markAllRead() {
      const ids = this.inboxShown().map(n => n.id);
      if (!ids.length) return;
      try {
        const r = await fetch("/api/notifications/read", { method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ ids }) });
        if (!r.ok) throw new Error("Atlas answered " + r.status);
        this.inbox = this.inbox.filter(x => !ids.includes(x.id));
        this.toast(this.plural(ids.length, "notification") + " marked read");
      } catch (e) { this.toast(this.friendlyError(e)); }
    },
    async markRead(n) {
      try {
        await fetch("/api/notifications/read", { method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ ids: [n.id] }) });
      } catch (_) {}
      this.inbox = this.inbox.filter(x => x.id !== n.id);
    },
    // Done on a promise in a brief: it leaves every later brief.
    async closeOwed(o) {
      o._busy = true;
      try { await this._postJson(`/api/owes/${encodeURIComponent(o.key)}/close`); o._done = "Done"; }
      catch (e) { o._done = this.friendlyError(e); }
      finally { o._busy = false; }
    },
    async reviewDecision(d, verdict) {
      try {
        const r = await fetch(`/api/decisions/${d.id}/review`, { method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ assessment: verdict }) });
        const j = await r.json();
        d._done = j.recorded ? verdict + " · lesson recorded" : "already resolved";
        this.fetchDecisions();
      } catch (e) { d._done = "failed: " + (e.message || e); }
    },
    async openApproval(id) {
      try {
        const r = await fetch(`/api/approvals/${id}`);
        if (!r.ok) throw new Error("no such approval");
        this.modal = { type: "approval", row: await r.json() };
      } catch (e) { this.modal = { type: "notification", row: { title: "Approval not found", body: String(e.message || e), kind: "approval" } }; }
    },
    async openBrief(id) {
      try {
        const r = await fetch(`/api/briefs/${id}`);
        if (!r.ok) throw new Error("no such brief");
        this.modal = { type: "brief", row: await r.json() };
      } catch (e) { this.modal = { type: "notification", row: { title: "Brief not found", body: String(e.message || e), kind: "brief" } }; }
    },
    // The trace: why a run did what it did. Opens from any run id (Agents,
    // Jobs, Chat) or a #run/<id> link.
    async openTrace(id) {
      this.modal = { type: "trace", row: null };
      try { history.replaceState(null, "", "#run/" + String(id).slice(0, 36)); } catch (e) { /* ignore */ }
      try {
        const r = await fetch(`/api/trace/${encodeURIComponent(String(id))}`);
        if (!r.ok) throw new Error(r.status === 404 ? "No run matches that id." : "HTTP " + r.status);
        const row = await r.json();
        row.steps.forEach(s => { s._open = false; });
        this.modal = { type: "trace", row };
      } catch (e) {
        this.modal = { type: "notification", row: { title: "Run not found", body: this.friendlyError(e), kind: "run", created_at: new Date().toISOString() } };
      }
    },
    async rateTrace(verdict) {
      const row = this.modal && this.modal.row; if (!row) return;
      await this.rateEpisode({ id: row.episode.id }, verdict);
      row.feedback.push({ verdict, created_at: new Date().toISOString() });
      this.fetchOutcomes();
    },
    // A heading, not the whole summary: the task's first sentence, else the
    // summary's, without the "agent:" prefix the runtime adds.
    traceTitle(row) {
      const raw = (row.trigger && row.trigger.task) || row.episode.summary || "run";
      const first = raw.replace(/^[a-z_/]+:\s*/i, "").replace(/\s*\((?:application|posting)[^)]*\)/gi, "")
        .split(/(?<=[.!?])\s|\s->\s|\n/)[0].trim();
      const short = first.length > 110 ? first.slice(0, 109) + "…" : first;
      return this.agentName(row.episode.agent) + ": " + short;
    },
    traceDuration(ep) { const s = Number(ep.duration_s); if (!ep.ended_at || !isFinite(s)) return "still running"; return s < 60 ? Math.round(s) + "s" : (s / 60).toFixed(1) + " min"; },
    traceTokens(t) {
      t = t || {};
      const k = n => (Number(n || 0) >= 1000 ? (Number(n) / 1000).toFixed(1) + "k" : String(Number(n || 0)));
      return "in " + k((t.input_tokens || 0) + (t.cache_read_input_tokens || 0) + (t.cache_creation_input_tokens || 0)) + " · out " + k(t.output_tokens);
    },
    traceTone(f) { return /refused|failed|errored|said no/.test(f) ? "urgent" : /waiting|unknown|cap|running|not finish/.test(f) ? "soon" : "acc"; },
    ledgerLabel(s) { return { done: "done once; a retry would not repeat it", pending: "waiting for your approval", rejected: "you said no", running: "outcome unknown; never retried", failed: "failed", expired: "approval expired" }[s] || s; },
    // A short line at the bottom of the screen; the push toast element carries it.
    toast(msg) {
      this.pushMsg = msg; clearTimeout(this._toastT);
      this._toastT = setTimeout(() => { if (this.pushMsg === msg) this.pushMsg = ""; }, 4000);
    },
    briefLabel(k) { return { morning: "Morning brief", evening: "Evening brief", weekly: "Weekly review", ai_news: "AI this morning" }[k] || this.cap(String(k || "brief")); },
    // Connect: one tap per app.
    async fetchConnect(check = true) {
      try { this.connect = await this._getJson("/api/connect?check=" + (check ? "true" : "false")); }
      catch (e) { this.toast(this.friendlyError(e)); }
      this.fetchKb();
    },
    // The knowledge base from Claude.
    async fetchKb() {
      try { this.kb = await this._getJson("/api/connect/claude"); } catch (e) { console.warn(e); }
      const st = this.kb && this.kb.import && this.kb.import.status;
      clearTimeout(this._kbTimer);
      if (st === "running" || st === "queued") this._kbTimer = setTimeout(() => this.fetchKb(), 15000);
    },
    async kbUpload() {
      const input = this.$refs.kbFile; const file = input && input.files && input.files[0];
      if (!file) return;
      this.kbBusy = true;
      try {
        const fd = new FormData(); fd.append("file", file, file.name);
        if (this.kbCap !== "" && this.kbCap != null) fd.append("cap_usd", String(this.kbCap));
        const r = await fetch("/api/connect/claude", { method: "POST", headers: { "X-Atlas-Client": "1" }, body: fd });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || r.statusText);
        input.value = "";
        this.toast(`Queued ${j.items} to read (about $${Number(j.estimate_usd).toFixed(2)}${j.already_read ? `, ${j.already_read} read before` : ""}). The worker starts within a minute.`);
        await this.fetchKb();
      } catch (e) { this.toast(this.friendlyError(e)); }
      finally { this.kbBusy = false; }
    },
    async kbControl(action, cap) {
      if (!this.kb || !this.kb.import) return;
      try { await this._postJson(`/api/connect/claude/${this.kb.import.id}/${action}`, cap != null ? { cap_usd: cap } : {}); await this.fetchKb(); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    async _connectPost(key, url, body, after) {
      this.connectBusy = key;
      try { const r = await this._postJson(url, body); if (after) after(r); await this.fetchConnect(); }
      catch (e) { this.toast(this.friendlyError(e)); }
      finally { this.connectBusy = ""; }
    },
    connectGoogleClient() {
      return this._connectPost("google", "/api/connect/google/client", { client_id: this.cx.gid.trim(), client_secret: this.cx.gsecret },
        r => { this.cx.gid = ""; this.cx.gsecret = ""; this.cx.gReplace = false; this.toast(r.tokens_cleared ? "Client replaced and the old consent dropped. Now tap Connect Google." : "Client saved. Now tap Connect Google."); });
    },
    disconnectGoogle() { return this._connectPost("google", "/api/connect/google/disconnect", {}, r => this.toast(r.revoked ? "Google disconnected; the token was revoked at Google too." : "Google disconnected.")); },
    connectGoogle() {
      const h = location.hostname;
      if (!["127.0.0.1", "localhost", "[::1]"].includes(h)) { this.toast("Google consent has to start on the laptop: open http://127.0.0.1:8000/#connect there and tap again."); return; }
      window.location.href = "/api/connect/google/start";
    },
    connectGithub() { return this._connectPost("github", "/api/connect/github", { token: this.cx.ghToken }, r => { this.cx.ghToken = ""; this.cx.ghRepos = r.repos || []; this.toast(r.detail); }); },
    connectCanvas() { return this._connectPost("canvas", "/api/connect/canvas", { url: this.cx.cvUrl.trim(), token: this.cx.cvToken }, r => { this.cx.cvToken = ""; this.toast(r.detail); }); },
    connectAlarm() { return this._connectPost("alarm", "/api/connect/alarm", { url: this.cx.alarm.trim() }, r => { this.cx.alarm = ""; this.toast(r.detail); }); },
    connectMcp() {
      const agents = this.cx.mcpAgents.split(/[,\s]+/).map(a => a.trim().toLowerCase()).filter(Boolean);
      return this._connectPost("mcp", "/api/connect/mcp", { name: this.cx.mcpName.trim(), url: this.cx.mcpUrl.trim(), token: this.cx.mcpToken || null, agents: agents.length ? agents : null },
        r => { this.cx.mcpUrl = ""; this.cx.mcpToken = ""; this.toast(`${r.server}: ${this.plural(r.count, "tool")} for ${r.agents.map(a => this.agentName(a)).join(", ")}`); });
    },
    removeMcp(s) { return this._connectPost("mcp", "/api/connect/mcp/remove", { name: s.name }); },
    // Money on Home: the statements he drops, read-only.
    async fetchMoney() {
      try { this.money = await this._getJson("/api/money"); } catch (e) { console.warn(e); }
    },
    async moneyUpload(file) {
      if (!file) return;
      this.moneyBusy = true;
      try {
        const fd = new FormData(); fd.append("file", file, file.name);
        const r = await fetch("/api/money/import", { method: "POST", headers: { "X-Atlas-Client": "1" }, body: fd });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || r.statusText);
        this.toast(j.imported ? `Filed ${j.row_count} rows from ${j.kind === "robinhood" ? "Robinhood" : j.account}` : (j.reason || "Nothing new in that file"));
        await this.fetchMoney();
      } catch (e) { this.toast(this.friendlyError(e)); }
      finally { this.moneyBusy = false; }
    },
    // The trust ladder: what each tool may do on its own.
    async fetchTrust() {
      try { this.trust = await this._getJson("/api/trust"); } catch (e) { console.warn(e); }
    },
    async setGate(r, gate) {
      if (gate === r.gate) return;
      if (r.gate === "deny" && !confirm(`${r.pattern} is never automated today. Move it to "${({approve: "asks first", auto: "on its own"})[gate]}"?`)) return;
      try { await this._postJson("/api/trust", { pattern: r.pattern, gate, note: r.note || null }); this.toast(`${r.pattern}: ${({deny: "never", approve: "asks first", auto: "on its own"})[gate]}`); await this.fetchTrust(); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    async unfence(r) {
      if (r.gate === "deny" && !confirm(`Remove the rule that keeps ${r.pattern} from ever running on its own?`)) return;
      try { await this._postJson("/api/trust/remove", { pattern: r.pattern }); await this.fetchTrust(); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    // While you were away: the story since the last time this screen was looked at.
    async fetchStory() {
      let seenId = 0, seenAt = null;
      try { seenId = Number(localStorage.getItem("atlas.seenId") || 0); seenAt = localStorage.getItem("atlas.seenAt"); } catch (e) { /* private mode */ }
      if (!this._storyListener) {
        this._storyListener = true;
        document.addEventListener("visibilitychange", () => { if (document.hidden) this.markSeen(); });
        window.addEventListener("pagehide", () => this.markSeen());
      }
      if (!seenId) { this.markSeen(); return; }
      try {
        const r = await this._getJson(`/api/events?since=${seenId}&limit=200`);
        this.story = this.storyLine(r.events || [], seenAt);
      } catch (e) { console.warn(e); }
    },
    markSeen() {
      const last = this.live && this.live.lastId;
      try {
        if (last) localStorage.setItem("atlas.seenId", String(last));
        localStorage.setItem("atlas.seenAt", new Date().toISOString());
      } catch (e) { /* private mode */ }
    },
    storyLine(events, seenAt) {
      if (!events.length) return null;
      const runs = {}, parts = [];
      let night = 0, briefs = 0, moved = 0, notes = 0, restarts = 0, finished = 0;
      for (const ev of events) {
        if (ev.kind === "run.finished" && ev.agent && ev.agent !== "owner") runs[ev.agent] = (runs[ev.agent] || 0) + 1;
        else if (ev.kind === "night.task" && ev.data && ev.data.status === "done") night++;
        else if (ev.kind === "news.brief") briefs++;
        else if (ev.kind === "submit.status") moved++;
        else if (ev.kind === "recording") notes++;
        else if (ev.kind === "web.restarted") restarts++;
        else if (ev.kind === "approval.finished") finished++;
      }
      const agents = Object.entries(runs).sort((a, b) => b[1] - a[1]);
      if (agents.length) parts.push(agents.slice(0, 3).map(([a, n]) => `${this.agentName(a)} finished ${this.plural(n, "run")}`).join(", ") + (agents.length > 3 ? ` and ${agents.length - 3} more agents worked` : ""));
      if (night) parts.push(`the night shift finished ${this.plural(night, "task")}`);
      if (briefs) parts.push("the AI brief landed");
      if (moved) parts.push(`${this.plural(moved, "application")} moved`);
      if (notes) parts.push("notes from a recording are ready");
      if (finished) parts.push(`${this.plural(finished, "late approval")} finished`);
      if (restarts) parts.push("the web app restarted itself");
      const waiting = this.needsYou();
      if (waiting) parts.push(`${this.plural(waiting, "thing")} need${waiting === 1 ? "s" : ""} you`);
      if (!parts.length) return null;
      const since = seenAt ? ` (since ${this.fmtDay(seenAt) === "today" ? this.fmtTime(seenAt) : this.fmtDay(seenAt)})` : "";
      const list = parts.length > 1 ? parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1] : parts[0];
      return `While you were away${since}: ${list}.`;
    },
    // What Atlas believes about him: the profile, the people, the facts.
    async fetchMemory() {
      try { this.memory = await this._getJson("/api/memory" + (this.memoryQ.trim() ? "?q=" + encodeURIComponent(this.memoryQ.trim()) : "")); }
      catch (e) { console.warn(e); }
    },
    async addFact() {
      const content = this.newFact.trim(); if (content.length < 3) return;
      try { await this._postJson("/api/memory/facts", { content }); this.newFact = ""; this.toast("Remembered"); await this.fetchMemory(); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    cancelEdit(f) { f._edit = false; },
    async saveFact(f) {
      const content = (f._draft || "").trim(); if (content.length < 3) return;
      try {
        const r = await fetch(`/api/memory/facts/${f.id}`, { method: "PATCH", headers: { "Content-Type": "application/json", "X-Atlas-Client": "1" }, body: JSON.stringify({ content }) });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
        f._edit = false; await this.fetchMemory();
      } catch (e) { this.toast(this.friendlyError(e)); }
    },
    async forgetFact(f) {
      try {
        const r = await fetch(`/api/memory/facts/${f.id}`, { method: "DELETE", headers: { "X-Atlas-Client": "1" } });
        if (!r.ok) throw new Error(r.statusText);
        this.toast("Forgotten"); await this.fetchMemory();
      } catch (e) { this.toast(this.friendlyError(e)); }
    },
    async addNote() {
      const content = this.newNote.content.trim(); if (!content) return;
      const body = { content, kind: this.newNote.kind };
      if (this.newNote.days) body.expires_in_days = Number(this.newNote.days);
      try { await this._postJson("/api/memory/profile", body); this.newNote.content = ""; this.newNote.days = ""; await this.fetchMemory(); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    async removeNote(n) {
      try {
        const r = await fetch(`/api/memory/profile/${n.id}`, { method: "DELETE", headers: { "X-Atlas-Client": "1" } });
        if (!r.ok) throw new Error(r.statusText);
        await this.fetchMemory();
      } catch (e) { this.toast(this.friendlyError(e)); }
    },
    // Outcome learning: the misses waiting to become eval cases.
    async fetchOutcomes() {
      try { this.outcomes = await this._getJson("/api/outcomes"); } catch (e) { console.warn(e); }
    },
    async dismissCandidate(c) {
      try { await this._postJson(`/api/outcomes/candidates/${c.id}/dismiss`, {}); await this.fetchOutcomes(); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    // The morning AI brief.
    async fetchNews() {
      try { this.news = await this._getJson("/api/news?limit=8"); } catch (e) { console.warn(e); }
    },
    async newsRun() {
      this.newsBusy = true;
      try {
        const r = await this._postJson("/api/news/run", {});
        this.toast(r.brief_id ? `AI brief ready: ${r.picked} of ${r.considered} stories kept` : `Nothing worth a brief (${r.considered} new stories)`);
        await Promise.all([this.fetchNews(), this.fetchBriefs(), this.fetchInbox()]);
      } catch (e) { this.toast(this.friendlyError(e)); }
      finally { this.newsBusy = false; }
    },
    // The Planner: one line in, the week out.
    async fetchPlan() {
      try {
        const q = this.planStart ? `?start=${this.planStart}&days=7` : "?days=7";
        this.plan = await this._getJson("/api/plan" + q); this.planErr = ""; this.planLoaded = true;
      } catch (e) { this.planErr = this.friendlyError(e); }
    },
    planShift(days) {
      const base = this.planStart || (this.plan ? this.plan.start : new Date().toISOString().slice(0, 10));
      const d = new Date(base + "T12:00:00"); d.setDate(d.getDate() + days);
      this.planStart = d.toISOString().slice(0, 10);
      this.fetchPlan();
    },
    planToday() { this.planStart = null; this.fetchPlan(); },
    planTitle() {
      if (!this.plan) return "This week";
      const a = new Date(this.plan.start + "T12:00:00"), b = new Date(this.plan.end + "T12:00:00");
      const f = (d, m) => d.toLocaleDateString(undefined, m ? { month: "short", day: "numeric" } : { day: "numeric" });
      return this.planStart ? `${f(a, true)} – ${f(b, a.getMonth() !== b.getMonth())}` : "This week";
    },
    planKindLabel(k) { return { task: "task", event: "calendar", class: "class", night: "night shift", next: "next action" }[k] || k; },
    fmtDay(iso) {
      if (!iso) return "";
      // A full timestamp is read in local time (its UTC date is tomorrow every
      // evening in Houston); a bare date is that date.
      const s = String(iso), src = s.length > 10 ? new Date(s) : new Date(s + "T12:00:00");
      const d = new Date(src.getFullYear(), src.getMonth(), src.getDate(), 12), t = new Date(); t.setHours(12, 0, 0, 0);
      const diff = Math.round((d - t) / 86400000);
      if (diff === 0) return "today"; if (diff === 1) return "tomorrow"; if (diff === -1) return "yesterday";
      return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    },
    quickHint() {
      clearTimeout(this.quick.timer); this.quick.err = "";
      const text = this.quick.text.trim();
      if (!text) { this.quick.hint = null; return; }
      this.quick.timer = setTimeout(async () => {
        try { this.quick.hint = await this._getJson("/api/plan/parse?text=" + encodeURIComponent(text)); } catch (e) { /* the hint is a courtesy */ }
      }, 180);
    },
    async quickAdd() {
      const text = this.quick.text.trim(); if (!text || this.quick.busy) return;
      this.quick.busy = true; this.quick.err = "";
      try {
        const t = await this._postJson("/api/plan/tasks", { text });
        this.quick.text = ""; this.quick.hint = null;
        this.toast(t.rule ? `Repeats ${t.rule.describe}: ${t.title}${t.when ? " · next " + this.fmtDay(t.when) : ""}`
                          : `Added: ${t.title}${t.when ? " · " + this.fmtDay(t.when) : " · someday"}`);
        await this.fetchPlan();
      } catch (e) { this.quick.err = this.friendlyError(e); }
      finally { this.quick.busy = false; }
    },
    async taskAction(it, action, body) {
      try { await this._postJson(`/api/plan/tasks/${it.id}/${action}`, body || {}); await this.fetchPlan(); }
      catch (e) { this.toast(this.friendlyError(e)); await this.fetchPlan(); }
    },
    // The tick flips on the tap; the server catches up, or the list reverts.
    taskToggle(it) { const was = it.done; it.done = !was; it.status = was ? "open" : "done"; return this.taskAction(it, was ? "reopen" : "done"); },
    async ruleStop(r) {
      try { await this._postJson(`/api/plan/rules/${r.id}/stop`, {}); this.toast(`Stopped: ${r.title}`); await this.fetchPlan(); }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    taskDrop(it) { return this.taskAction(it, "drop"); },
    taskMove(it, days) {
      const d = new Date(); d.setDate(d.getDate() + days);
      return this.taskAction(it, "reschedule", { due_date: d.toISOString().slice(0, 10) });
    },
    // The night as a film: every event of the shift, played back.
    async openReplay(night) {
      this.replayPause();
      this.modal = { type: "replay", row: null };
      try {
        const d = await this._getJson("/api/night/replay" + (night ? `?night=${encodeURIComponent(night)}` : ""));
        try { history.replaceState(null, "", "#replay/" + d.night); } catch (e) { /* ignore */ }
        this.replay = { data: d, i: 0, playing: false, timer: null };
        this.modal = { type: "replay", row: d };
        if (d.timeline.length > 1) this.replayPlay();
      } catch (e) {
        this.modal = { type: "notification", row: { title: "No film for that night", body: this.friendlyError(e), kind: "run", created_at: new Date().toISOString() } };
      }
    },
    replayEvent() { const d = this.replay.data; return d ? d.timeline[this.replay.i] : null; },
    replayGo(i) { const n = this.replay.data ? this.replay.data.timeline.length : 0; this.replay.i = Math.max(0, Math.min(i, n - 1)); },
    replayPlay() {
      const d = this.replay.data; if (!d || !d.timeline.length || this.replay.playing) return;
      if (this.replay.i >= d.timeline.length - 1) this.replay.i = 0;
      this.replay.playing = true;
      this.replay.timer = setInterval(() => {
        if (this.replay.i >= d.timeline.length - 1) return this.replayPause();
        const cur = d.timeline[this.replay.i];
        this.replay.i += 1;
        // Linger on a screenshot or a finished run; skim the rest.
        if (cur && (cur.kind === "submit.shot" || cur.kind === "run.finished")) { this.replayPause(); setTimeout(() => this.replayPlay(), 1600); }
      }, 1000);
    },
    replayPause() { this.replay.playing = false; if (this.replay.timer) { clearInterval(this.replay.timer); this.replay.timer = null; } },
    closeModal() {
      this.replayPause();
      this.modal = null;
      if (/^#(approval|brief|inbox|run|replay)\//.test(location.hash)) history.replaceState(null, "", this.roomHash());
    },
    // Where a room hash leads: the label (#today, #settings/trust), the old
    // route ids (#bridge, #comms, #links) and every address from before the
    // eight rooms (#missions/jobs, #memory/ledger, #inbox/trust…), so a push,
    // a bookmark or a link in old mail still lands on the right screen.
    roomFor(slug, sub = "") {
      const moved = {
        "missions/": ["plan", "week"], "missions/plan": ["plan", "week"], "missions/night": ["plan", "night"],
        "missions/routines": ["plan", "routines"], "missions/jobs": ["career", "queue"], "plan/plan": ["plan", "week"],
        "memory/about": ["memory", "facts"], "memory/skills": ["inbox", "skills"], "memory/ledger": ["settings", "ledger"],
        "inbox/trust": ["settings", "trust"], "links/": ["settings", "connections"], "connections/": ["settings", "connections"],
        "connect/": ["settings", "connections"], "approvals/": ["inbox", "approvals"], "brief/": ["comms", "briefings"],
        "jobs/": ["career", "queue"], "agents/": ["staff", "agents"], "ledger/": ["settings", "ledger"], "trust/": ["settings", "trust"],
      }[slug + "/" + sub];
      if (moved) return moved;
      const id = { today: "bridge", chat: "comms" }[slug] || slug;
      const subs = this.roomSubs();
      if (!(id in subs)) return null;
      return [id, subs[id].includes(sub) ? sub : undefined];
    },
    // Room hashes (#career/queue) and the deep links pushes and old
    // bookmarks use (#approval/<id>, #run/<id>, #replay, #connect…).
    routeHash() {
      const hash = location.hash || "";
      const th = hash.match(/^#thread\/([0-9a-f-]{36})$/i);
      if (th) { this.goRoom("comms", "channel"); this.openThread(th[1]); return; }
      const ag = hash.match(/^#agent\/([a-z][a-z0-9_]{0,40})$/i);
      if (ag) { this.openAgent(ag[1].toLowerCase()); return; }
      const r = hash.match(/^#([a-z]+)(?:\/([a-z]+))?$/i);
      if (r) {
        const where = this.roomFor(r[1].toLowerCase(), r[2] ? r[2].toLowerCase() : "");
        if (where) { this.goRoom(...where); return; }
      }
      const m = hash.match(/^#(approval|approvals|brief|inbox|today|run|replay|connect)(?:\/([0-9a-f-]+))?$/i);
      if (!m) return;
      const kind = m[1].toLowerCase(), id = m[2];
      if (kind === "run") { if (id) this.openTrace(id); return; }
      if (kind === "connect") { this.goRoom("settings", "connections"); return; }
      if (kind === "replay") { this.openReplay(id || null); return; }
      const where = { approval: ["inbox", "approvals"], approvals: ["inbox", "approvals"], today: ["inbox", "signals"],
        inbox: ["inbox", "notifications"], brief: ["comms", "briefings"] }[kind];
      this.goRoom(...where);
      if (kind === "approval" && id) return this.openApproval(id);
      if (kind === "brief" && id) return this.openBrief(id);
      if (kind === "inbox" && id) {
        fetch(`/api/notifications/${id}`).then(r => r.ok ? r.json() : null)
          .then(row => { if (row) this.modal = { type: "notification", row }; }).catch(() => {});
      }
    },

    async fetchEpisodes() {
      try { const r = await fetch("/api/episodes?limit=12");
            if (r.ok) this.episodes = (await r.json()).episodes; }
      catch (e) { console.warn(e); }
    },
    async fetchDecisions() {
      try { const r = await fetch("/api/decisions");
            if (r.ok) this.decisions = (await r.json()).decisions; }
      catch (e) { console.warn(e); }
    },
    async fetchLearnings() {
      // Don't clobber the list while a card is mid-action (draft/adopt/skip).
      if ((this.learnings || []).some(l => l._busy)) return;
      try { const r = await fetch("/api/learnings");
            if (r.ok) this.learnings = (await r.json()).proposed; }
      catch (e) { console.warn(e); }
    },
    async buildLearning(l) {
      l._busy = "drafting"; l._err = "";
      try {
        const r = await fetch(`/api/learnings/${l.id}/draft`, { method: "POST" });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "draft failed");
        l.draft = d.draft; l.status = "drafted";
      } catch (e) { l._err = "Draft failed: " + (e.message || e); }
      finally { l._busy = null; }
    },
    async adoptLearning(l) {
      l._busy = "adopting"; l._err = "";
      try {
        const r = await fetch(`/api/learnings/${l.id}/adopt`, { method: "POST" });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "adopt failed");
        l._busy = null;
        await this.fetchLearnings();
        await this.fetchSkills();
      } catch (e) { l._err = "Adopt failed: " + (e.message || e); l._busy = null; }
    },
    async fetchSkills() {
      if ((this.skills || []).some(k => k._busy)) return;
      try { const r = await fetch("/api/skills");
            if (r.ok) this.skills = (await r.json()).skills.filter(k => k.status !== 'retired'); }
      catch (e) { console.warn(e); }
    },
    async keepSkill(k) {
      k._busy = true;
      try { await fetch(`/api/skills/${k.id}/keep`, { method: "POST" }); } catch (e) { /* ignore */ }
      k._busy = false;
      await this.fetchSkills();
    },
    provenanceLabel(k) {
      const by = { user_authored: "Written by you", agent_reflection: "Written by Atlas from its runs",
                   external_internet: "From the internet", unknown: "Origin unknown" }[k.provenance] || "Origin unknown";
      return k.review === "approved" ? by + " · approved by you" : by;
    },
    async reviewSkill(k, decision) {
      k._busy = true;
      try { await fetch(`/api/skills/${k.id}/review`, { method: "POST",
              headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision }) }); }
      catch (e) { /* ignore */ }
      k._busy = false;
      await this.fetchSkills();
    },
    async retireSkill(k) {
      k._busy = true;
      try { await fetch(`/api/skills/${k.id}/retire`, { method: "POST",
              headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: "retired from the dashboard" }) }); }
      catch (e) { /* ignore */ }
      k._busy = false;
      await this.fetchSkills();
    },
    async rateEpisode(e, verdict) {
      try {
        const r = await fetch(`/api/episodes/${e.id}/feedback`, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ verdict }) });
        if (r.ok) { e._rated = verdict; this.fetchSkills(); }
      } catch (err) { console.warn(err); }
    },
    async skipLearning(l) {
      l._busy = "skipping";
      try { await fetch(`/api/learnings/${l.id}/reject`, { method: "POST" }); }
      catch (e) { /* ignore */ }
      l._busy = null;
      await this.fetchLearnings();
    },
    async fetchAgents() {
      try {
        const r = await fetch("/api/agents");
        if (!r.ok) return;
        const data = await r.json();
        const now = Date.now();
        data.agents.forEach(a => {
          a.recentlyActive = a.last_active &&
            (now - new Date(a.last_active).getTime()) < 5 * 60 * 1000;
        });
        data.agents.sort((a, b) => {
          if (a.recentlyActive !== b.recentlyActive) return a.recentlyActive ? -1 : 1;
          return b.runs_7d - a.runs_7d;
        });
        this.agents = data.agents;
      } catch (e) { console.warn(e); }
    },
    async fetchGraph() {
      try {
        const r = await fetch("/api/graph?limit=60");
        if (r.ok) this.graph = await r.json();
      } catch (e) { console.warn(e); }
    },

    renderGraph() {
      const svg = this.$refs.graphSvg;
      if (!svg || !window.d3) return;
      if (!this.graph.nodes.length) return;
      const d3 = window.d3;
      // The Map fills most of the Memory room, so the canvas is larger than
      // the old card's; the viewBox in memory.html matches.
      const W = 900, H = 600;
      const PAD = 36;

      // Wipe rendered layer, keep <defs>.
      [...svg.querySelectorAll(".graph-layer")].forEach(el => el.remove());

      // Position cache so layout doesn't reshuffle between re-renders.
      if (!this._nodePositions) this._nodePositions = new Map();
      const cache = this._nodePositions;

      // Copy nodes; rehydrate cached x/y/vx/vy when we've seen this id before.
      const nodes = this.graph.nodes.map(n => {
        const c = cache.get(n.id);
        const base = { ...n, degree: 0 };
        if (c) { base.x = c.x; base.y = c.y; base.vx = c.vx; base.vy = c.vy; }
        return base;
      });
      const idIndex = new Map(nodes.map(n => [n.id, n]));
      const edges = this.graph.edges
        .filter(e => idIndex.has(e.source) && idIndex.has(e.target))
        .map(e => ({ ...e }));
      edges.forEach(e => {
        idIndex.get(e.source).degree++;
        idIndex.get(e.target).degree++;
      });

      // Hub labels are always on. The rest reveal on hover/focus.
      const hubCount = Math.min(8, Math.max(3, Math.ceil(nodes.length * 0.2)));
      const hubIds = new Set(
        [...nodes].sort((a, b) => b.degree - a.degree)
          .slice(0, hubCount).map(n => n.id)
      );

      // Graph palette: one hue per type.
      const colorByType = {
        person:   "#E3F6FF",
        business: "#7FB2FF",
        property: "#8FA8FF",
        course:   "#7FE6D4",
        account:  "#5BB8FF",
        supplier: "#3A9FCF",
        product:  "#9FC3D4",
        agency:   "#B4C8FF",
        tool:     "#6FE3FF",
        concept:  "#6A8A9A",
      };
      const colorFor = d => colorByType[d.type] || "#6A8A9A";

      // Adjacency for focus mode.
      const neighbors = new Map(nodes.map(n => [n.id, new Set()]));
      edges.forEach(e => {
        neighbors.get(e.source).add(e.target);
        neighbors.get(e.target).add(e.source);
      });

      const nodeRadius = d => 4.2 + Math.sqrt(d.degree || 0) * 2.4;

      const sim = d3.forceSimulation(nodes)
        .force("link", d3.forceLink(edges).id(d => d.id)
          .distance(d => {
            const sd = (typeof d.source === "object" ? d.source.degree : 0);
            const td = (typeof d.target === "object" ? d.target.degree : 0);
            return 62 + Math.min(24, (sd + td) * 1.5);
          })
          .strength(0.35))
        // Repulsion scales down as the graph grows, and loose nodes (no links)
        // are held on an outer orbit instead of being pushed into the walls.
        .force("charge", d3.forceManyBody().strength(d => (-60 - d.degree * 10) * (nodes.length > 40 ? 0.6 : 1)))
        .force("center", d3.forceCenter(W / 2, H / 2))
        .force("x", d3.forceX(W / 2).strength(0.03))
        .force("y", d3.forceY(H / 2).strength(0.05))
        .force("collide", d3.forceCollide()
          .radius(d => nodeRadius(d) + 14)
          .strength(0.92))
        .force("radial", d3.forceRadial(d => (d.degree ? 0.2 : 0.42) * Math.min(W, H), W / 2, H / 2)
          .strength(d => (d.degree ? 0.05 : 0.12)))
        .alphaDecay(0.028);

      const layer = d3.select(svg).append("g").attr("class", "graph-layer");

      // Edges: muted base + directional arrowhead at target ring.
      const linkSel = layer.append("g").attr("class", "links")
        .selectAll("path").data(edges).enter().append("path")
        .attr("class", "link")
        .attr("marker-end", "url(#arrowhead)");

      // Edge relation labels, hidden until focus/hover reveals them.
      const linkLabelSel = layer.append("g").attr("class", "link-labels")
        .selectAll("text").data(edges).enter().append("text")
        .attr("class", "link-label")
        .attr("text-anchor", "middle")
        .attr("dy", -3)
        .text(d => (d.relation || "").toUpperCase());

      // Nodes
      const nodeGroup = layer.append("g").attr("class", "nodes")
        .selectAll("g").data(nodes).enter().append("g")
        .attr("class", "node-group")
        .attr("tabindex", 0)
        .attr("role", "button")
        .attr("aria-label", d =>
          `${d.name}, ${d.type || "entity"}, ${d.degree} connections`);

      nodeGroup.append("circle").attr("class", "node-halo")
        .attr("r", d => nodeRadius(d) + 8)
        .attr("fill", colorFor);

      nodeGroup.append("circle").attr("class", "node-ring")
        .attr("r", d => nodeRadius(d) + 2.5)
        .attr("fill", "none")
        .attr("stroke", colorFor)
        .attr("stroke-opacity", 0.38)
        .attr("stroke-width", 0.9);

      nodeGroup.append("circle").attr("class", "node")
        .attr("r", nodeRadius)
        .attr("fill", colorFor);

      nodeGroup.append("text").attr("class", "node-label")
        .attr("dy", d => -(nodeRadius(d) + 8))
        .attr("text-anchor", "middle")
        .text(d => d.name)
        .classed("hub", d => hubIds.has(d.id));

      const self = this;
      if (typeof self.focusedNodeId !== "string") self.focusedNodeId = "";

      function applyFocus(fid) {
        self.focusedNodeId = fid || "";
        const active = fid ? new Set([fid, ...neighbors.get(fid)]) : null;

        nodeGroup
          .classed("is-focused", d => d.id === fid)
          .classed("is-neighbor", d => !!active && d.id !== fid && active.has(d.id))
          .classed("is-dimmed", d => !!active && !active.has(d.id));

        nodeGroup.select(".node-label").attr("opacity", d => {
          if (!active) return hubIds.has(d.id) ? 1 : 0;
          return active.has(d.id) ? 1 : 0;
        });

        linkSel
          .classed("is-active", d => !!active && (d.source.id === fid || d.target.id === fid))
          .classed("is-dimmed", d => !!active && d.source.id !== fid && d.target.id !== fid);

        linkLabelSel.attr("opacity", d =>
          active && (d.source.id === fid || d.target.id === fid) ? 1 : 0
        );
      }

      // Expose for the document-level Escape key handler.
      self.clearGraphFocus = () => applyFocus("");

      nodeGroup
        .on("click", function(ev, d) {
          ev.stopPropagation();
          applyFocus(d.id);
          self.draft = `What do you know about ${d.name}? Pull facts and any relationships you have.`;
          self.setView("chat");
          self.$nextTick(() => {
            const ta = document.querySelector(".composer textarea");
            if (ta) ta.focus();
          });
        })
        .on("keydown", function(ev) {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            d3.select(this).dispatch("click");
          } else if (ev.key === "Escape") {
            ev.preventDefault();
            applyFocus("");
          }
        })
        .on("mouseenter", function(ev, d) {
          if (self.focusedNodeId) return;
          const nbrs = neighbors.get(d.id);
          d3.select(this).select(".node-label").attr("opacity", 1);
          nodeGroup.filter(n => nbrs.has(n.id))
            .select(".node-label").attr("opacity", 0.7);
        })
        .on("mouseleave", function() {
          if (self.focusedNodeId) return;
          nodeGroup.select(".node-label")
            .attr("opacity", n => hubIds.has(n.id) ? 1 : 0);
        });

      // Click on empty SVG clears focus; stopPropagation on nodes guards this.
      d3.select(svg).on("click.graphclear", () => applyFocus(""));
      // SVG-level Escape (in case keyboard focus is outside any node).
      d3.select(svg).on("keydown.graphclear", (ev) => {
        if (ev.key === "Escape") applyFocus("");
      });

      sim.on("tick", () => {
        nodes.forEach(n => {
          const r = nodeRadius(n);
          n.x = Math.max(PAD + r, Math.min(W - PAD - r, n.x));
          n.y = Math.max(PAD + r, Math.min(H - PAD - r, n.y));
        });

        linkSel.attr("d", d => {
          const dx = d.target.x - d.source.x;
          const dy = d.target.y - d.source.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const tr = nodeRadius(d.target) + 4;
          const ex = d.target.x - (dx / dist) * tr;
          const ey = d.target.y - (dy / dist) * tr;
          const dr = dist * 1.8;
          return `M ${d.source.x} ${d.source.y} A ${dr} ${dr} 0 0 1 ${ex} ${ey}`;
        });

        linkLabelSel.attr("transform", d => {
          const mx = (d.source.x + d.target.x) / 2;
          const my = (d.source.y + d.target.y) / 2;
          let angle = Math.atan2(d.target.y - d.source.y, d.target.x - d.source.x)
            * 180 / Math.PI;
          if (angle > 90 || angle < -90) angle += 180;
          return `translate(${mx}, ${my}) rotate(${angle})`;
        });

        nodeGroup.attr("transform", d => `translate(${d.x}, ${d.y})`);

        // Stash positions back into the cache every tick so we have fresh
        // values to rehydrate from on the next re-render.
        nodes.forEach(n => cache.set(n.id, { x: n.x, y: n.y, vx: n.vx, vy: n.vy }));
      });

      // Legend: only types actually present in this graph.
      const present = [...new Set(nodes.map(n => n.type).filter(Boolean))].sort();
      self.graphLegend = present.map(t => ({
        type: t, color: colorByType[t] || "#6A8A9A",
      }));

      // Default visibility or re-apply prior focus across re-renders.
      if (self.focusedNodeId && idIndex.has(self.focusedNodeId)) {
        applyFocus(self.focusedNodeId);
      } else {
        nodeGroup.select(".node-label")
          .attr("opacity", d => hubIds.has(d.id) ? 1 : 0);
        linkLabelSel.attr("opacity", 0);
      }

      setTimeout(() => sim.alphaTarget(0.012).restart(), 1500);
    },

    // ============= AGENT TILE CLICK =============
    onAgentClick(a) {
      this.setView("chat");
      const prefix = `Dispatch ${a.name} on: `;
      // If the textarea already starts with a different dispatch, replace it.
      // Otherwise just prepend.
      const cur = this.draft.trim();
      if (/^Dispatch \w+ on:/.test(cur)) {
        this.draft = prefix;
      } else if (cur) {
        this.draft = `${prefix}${cur}`;
      } else {
        this.draft = prefix;
      }
      this.$nextTick(() => {
        const ta = document.querySelector(".composer textarea");
        if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
      });
    },

    // ============= FILE UPLOAD =============
    async onFilePicked(ev) {
      const file = ev.target.files && ev.target.files[0];
      if (!file) return;
      ev.target.value = ""; // allow re-picking same file
      // Show a placeholder chip immediately so the user sees something happen.
      const placeholder = { filename: file.name, chars: 0, _uploading: true };
      this.attachments.push(placeholder);
      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch("/api/upload", { method: "POST", body: fd });
        // Remove the placeholder regardless of outcome.
        const idx = this.attachments.indexOf(placeholder);
        if (idx >= 0) this.attachments.splice(idx, 1);
        if (!r.ok) {
          const t = await r.text();
          this.error = `Upload failed (${r.status}): ${t.slice(0,200)}`;
          return;
        }
        const data = await r.json();
        this.attachments.push(data);
        this.error = "";
        // Focus the textarea so the user can immediately type their question.
        this.$nextTick(() => {
          const ta = document.querySelector(".composer textarea");
          if (ta) ta.focus();
        });
      } catch (e) {
        const idx = this.attachments.indexOf(placeholder);
        if (idx >= 0) this.attachments.splice(idx, 1);
        this.error = "Upload error: " + this.friendlyError(e);
      }
    },
    removeAttachment(i) {
      this.attachments.splice(i, 1);
    },

    composerPlaceholder() {
      if (this.listening) return "Listening... speak now.";
      if (this.learnMode) return "Tell Atlas a fact. Each message is saved to memory.";
      return "Message the CEO";
    },

    toggleLearn() {
      this.learnMode = !this.learnMode;
      if (this.learnMode) {
        this.learnCount = 0;
        this.error = "";
      }
    },

    async saveAsFact() {
      const text = this.draft.trim();
      if (!text || this.pending) return;
      this.error = "";
      this.pending = true;
      try {
        const r = await fetch("/api/learn", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: text }),
        });
        if (!r.ok) {
          const t = await r.text();
          throw new Error(`HTTP ${r.status}: ${t.slice(0,200)}`);
        }
        const data = await r.json();
        this.messages.push({
          id: this.nextId++, who: "saved", body: text,
          meta: `fact ${data.fact_id.slice(0,8)} saved`,
        });
        this.learnCount += 1;
        this.draft = "";
      } catch (e) {
        this.error = "Could not save: " + this.friendlyError(e);
      } finally {
        this.pending = false;
        this.$nextTick(() => this.scrollLog());
      }
    },

    async send() {
      if (this.learnMode) {
        return this.saveAsFact();
      }
      let text = this.draft.trim();
      // Prepend any attached file content as a fenced block. The CEO sees
      // the file body as part of the user turn; one-shot context, not
      // persisted.
      if (this.attachments.length) {
        const blocks = this.attachments.map(a =>
          `[Attached file: ${a.filename} (${a.chars} chars${a.truncated ? ", truncated" : ""})]\n` +
          "```\n" + a.text + "\n```"
        );
        text = blocks.join("\n\n") + "\n\n" + text;
        // Clear after capturing so the next message starts fresh.
        this.attachments = [];
      }
      if (!text || this.pending) return;
      this.error = "";
      // Display version: collapse fenced file blocks to a short marker so
      // the chat log stays readable. The CEO still sees the full text.
      const display = text.replace(/```[\s\S]*?```/g, "[file content]");
      this.messages.push({ id: this.nextId++, who: "user", body: display });
      this.draft = "";
      this.pending = true;
      this.$nextTick(() => this.scrollLog());
      this.streaming = true;
      this.messages.push({ id: this.nextId++, who: "ceo", body: "", meta: "" });
      const ceoMsg = this.messages[this.messages.length - 1];  // reactive proxy ref
      let full = "", doneEv = null;
      try {
        const r = await fetch("/api/chat/stream", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, thread_id: this.chatThread, new_thread: this.chatNew }),
        });
        this.chatNew = false;
        if (!r.ok || !r.body) {
          const t = r.body ? await r.text() : "";
          throw new Error(`HTTP ${r.status}: ${t.slice(0, 200)}`);
        }
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const rawline = buf.slice(0, idx); buf = buf.slice(idx + 2);
            const dline = rawline.split("\n").find(l => l.startsWith("data:"));
            if (!dline) continue;
            let ev; try { ev = JSON.parse(dline.slice(5).trim()); } catch (e) { continue; }
            if (ev.type === "thread") {
              this.chatThread = ev.id;
            } else if (ev.type === "delta") {
              full += ev.text; ceoMsg.body = full; this.$nextTick(() => this.scrollLog());
            } else if (ev.type === "status") {
              ceoMsg.meta = ev.text;
            } else if (ev.type === "done") {
              doneEv = ev;
            } else if (ev.type === "error") {
              this.error = ev.text;
            }
          }
        }
        const cleaned = ((doneEv && doneEv.text) ? doneEv.text : full)
          .replace(/<tool_call>[\s\S]*?<\/tool_call>/g, "")
          .replace(/\n{3,}/g, "\n\n").trim()
          || "(the CEO finished without a reply)";
        ceoMsg.body = cleaned;
        if (doneEv) ceoMsg.meta = `${doneEv.turns}t · ${doneEv.stop_reason} · ${String(doneEv.episode_id).slice(0,8)}`;
        if (doneEv) ceoMsg.episode_id = doneEv.episode_id;
        const spoke = this.speakText(cleaned);
        if (this.convoMode && !spoke) this.convoResume();
        this.refresh();
        this.replanPulse();
      } catch (e) {
        this.error = this.friendlyError(e);
        if (!ceoMsg.body) ceoMsg.body = "(no reply: " + this.friendlyError(e) + ")";
        this.convoResume();
      } finally {
        this.pending = false;
        this.streaming = false;
        this.$nextTick(() => this.scrollLog());
      }
    },
    // ---- Chat history (core/chat_history.py): the channel keeps its conversations ----
    async loadThreads() {
      try { this.threads = (await this._getJson("/api/chat/threads?limit=30")).threads; }
      catch (e) { this.toast(this.friendlyError(e)); }
    },
    async openLatestChat() {
      this.chatLoaded = true;
      if (this.messages.length) return;
      await this.loadThreads();
      if (this.threads.length) await this.openThread(this.threads[0].id);
    },
    async openThread(id) {
      if (this.pending) return;
      try {
        const t = await this._getJson("/api/chat/threads/" + encodeURIComponent(id));
        this.chatThread = t.id; this.chatNew = false; this.chatLoaded = true; this.historyOpen = false;
        this.messages = t.messages.map(m => ({ id: this.nextId++, who: m.role === "you" ? "user" : "ceo", body: m.body,
          meta: m.role === "ceo" ? this.ago(m.created_at) : "", episode_id: m.episode_id }));
        this.$nextTick(() => this.scrollLog());
      } catch (e) { this.toast(this.friendlyError(e)); }
    },
    newChat() {
      if (this.pending) return;
      this.messages = []; this.chatThread = null; this.chatNew = true; this.chatLoaded = true; this.historyOpen = false;
    },
    async toggleHistory() {
      this.historyOpen = !this.historyOpen;
      if (this.historyOpen) await this.loadThreads();
    },
    scrollLog() {
      const el = this.$refs.log; if (el) el.scrollTop = el.scrollHeight;
    },

    // ============= VOICE INPUT (Web Speech API) =============
    toggleMic() {
      if (this.pending) return;
      if (this.listening) {
        this.stopMic();
      } else {
        this.startMic();
      }
    },
    startMic() {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) {
        this.error = "Voice input is not available in this browser. Use Chrome or Edge.";
        return;
      }
      // Cancel any currently-speaking CEO reply so the user can interrupt.
      if (window.speechSynthesis) window.speechSynthesis.cancel();
      const r = new SR();
      r.lang = "en-US";
      r.interimResults = true;
      r.continuous = false;
      r.maxAlternatives = 1;
      this._recog = r;
      let finalText = "";
      r.onresult = (e) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const res = e.results[i];
          if (res.isFinal) finalText += res[0].transcript + " ";
          else interim += res[0].transcript;
        }
        this.draft = (finalText + interim).trim();
      };
      r.onerror = (e) => {
        if (e.error !== "no-speech" && e.error !== "aborted") {
          this.error = "Mic error: " + e.error;
        }
        this.stopMic();
      };
      r.onend = () => {
        this.listening = false;
        // Auto-send if we got something and the user did not bail.
        const t = this.draft.trim();
        if (t && finalText.trim()) {
          this.send();
        }
      };
      try {
        r.start();
        this.listening = true;
        this.error = "";
      } catch (err) {
        this.error = "Could not start mic: " + err.message;
        this.listening = false;
      }
    },
    stopMic() {
      if (this._recog) {
        try { this._recog.stop(); } catch (e) {}
        this._recog = null;
      }
      this.listening = false;
    },

    // ============= VOICE OUTPUT (SpeechSynthesis) =============
    toggleSpeak() {
      this.speakOn = !this.speakOn;
      if (!this.speakOn && window.speechSynthesis) {
        window.speechSynthesis.cancel();
      }
    },
    speakText(text) {
      if (!this.speakOn || !text || !window.speechSynthesis) return false;
      try {
        window.speechSynthesis.cancel();
        const cleaned = text
          .replace(/\*\*/g, "")
          .replace(/`+/g, "")
          .replace(/\[.*?\]/g, "")
          .replace(/\s+/g, " ")
          .trim();
        if (!cleaned) return false;
        const chunks = cleaned.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [cleaned];
        const voices = window.speechSynthesis.getVoices();
        const pref = voices.find(v => /Google US English|Microsoft.*Aria|Microsoft.*Guy|Daniel|Alex/i.test(v.name));
        chunks.forEach((c, i) => {
          const u = new SpeechSynthesisUtterance(c.trim());
          u.lang = "en-US";
          u.rate = 1.02;
          u.pitch = 1.0;
          if (pref) u.voice = pref;
          // When the last chunk finishes speaking, resume listening (convo mode).
          if (i === chunks.length - 1) u.onend = () => this.convoResume();
          window.speechSynthesis.speak(u);
        });
        return true;
      } catch (e) { console.warn("speak failed", e); return false; }
    },

    // ============= HANDS-FREE CONVERSATION =============
    toggleConvo() {
      this.convoMode = !this.convoMode;
      if (this.convoMode) {
        this.speakOn = true;          // conversation implies spoken replies
        this.error = "";
        this.convoListen();
      } else {
        this.stopMic();
        if (window.speechSynthesis) window.speechSynthesis.cancel();
        if (this._convoResumeT) { clearTimeout(this._convoResumeT); this._convoResumeT = null; }
      }
    },
    convoResume() {
      if (!this.convoMode || this._convoResumeT) return;
      this._convoResumeT = setTimeout(() => {
        this._convoResumeT = null;
        if (this.convoMode && !this.pending) this.convoListen();
      }, 450);
    },
    convoListen() {
      if (!this.convoMode || this.pending) return;
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) { this.error = "Voice needs Chrome or Edge."; this.convoMode = false; return; }
      if (window.speechSynthesis) window.speechSynthesis.cancel();
      const r = new SR();
      r.lang = "en-US"; r.interimResults = true; r.continuous = false; r.maxAlternatives = 1;
      this._recog = r;
      this.listening = true;
      let finalText = "";
      r.onresult = (e) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const res = e.results[i];
          if (res.isFinal) finalText += res[0].transcript + " "; else interim += res[0].transcript;
        }
        this.draft = (finalText + interim).trim();
      };
      r.onerror = (e) => { if (e.error !== "no-speech" && e.error !== "aborted") this.error = "Mic: " + e.error; };
      r.onend = () => {
        this.listening = false; this._recog = null;
        if (!this.convoMode) return;
        if (finalText.trim()) { this.send(); }   // streams + speaks; resumes on TTS end
        else { this.convoResume(); }              // silence: keep waiting
      };
      try { r.start(); this.error = ""; } catch (err) { this.listening = false; this.convoResume(); }
    },

    // The quotes in Today's quiet corners: a per-browser setting (Atlas has no
    // settings store yet), read by neural-core.js.
    quotesOn() { try { return localStorage.getItem("atlas.quotes") !== "0"; } catch (e) { return true; } },
    toggleQuotes() {
      try { localStorage.setItem("atlas.quotes", this.quotesOn() ? "0" : "1"); } catch (e) { /* private mode */ }
      document.dispatchEvent(new Event("atlas:quotes"));
    },

    // PALETTE
    paletteCommands() {
      const ask = (t) => () => { this.closePalette(); this.askSend(t); };
      const c = [];
      for (const r of this.rooms()) {
        c.push({ id: "go-" + r.id, kind: "Go to", label: r.label, icon: r.icon, hint: "room · " + r.key,
          action: () => { this.closePalette(); this.goRoom(r.id); } });
        for (const s of this.roomSubs()[r.id]) c.push({ id: `go-${r.id}-${s}`, kind: "Go to", icon: r.icon, hint: r.label,
          label: `${r.label}: ${this.cap(s)}`, action: () => { this.closePalette(); this.goRoom(r.id, s); } });
      }
      c.push(
        { id: "focus", kind: "setting", icon: "monitor", label: this.focus ? "Leave Focus mode" : "Focus mode (calm the screen)", hint: "F",
          action: () => { this.closePalette(); this.toggleFocus(); } },
        { id: "demo", kind: "setting", icon: "learn", label: this.demo ? "Leave demo mode (back to real data)" : "Demo mode (invented data, safe to show anyone)",
          hint: "demo", action: () => { location.href = "/?demo=" + (this.demo ? "0" : "1"); } },
        { id: "film", kind: "ask", icon: "clock", label: "Replay last night", hint: "film",
          action: () => { this.closePalette(); this.openReplay(null); } },
        { id: "quotes", kind: "setting", icon: "book", label: this.quotesOn() ? "Hide the quotes on Today" : "Show quotes on Today",
          hint: "Today", action: () => { this.closePalette(); this.toggleQuotes(); } },
        { id: "brief-morning", kind: "ask", icon: "doc", label: "Ask for a morning brief", action: ask("Give me a morning brief now.") },
        { id: "plan-day", kind: "ask", icon: "clock", label: "Plan my day", action: ask(this.starters[0].prompt) },
        { id: "slipping", kind: "ask", icon: "alert", label: "What's slipping?", action: ask(this.starters[1].prompt) },
        { id: "review", kind: "ask", icon: "scale", label: "Run the decision review", action: ask("Run /review now.") },
        { id: "push", kind: "setting", icon: "bell", label: this.pushOn ? "Turn off push on this device" : "Turn on push on this device",
          action: () => { this.closePalette(); this.togglePush(); } },
      );
      this.agents.filter(a => a.registered !== false).forEach(a => c.push({ id: "agent-" + a.name, kind: "dispatch", icon: "agents",
        label: "Dispatch " + this.agentName(a.name), hint: a.tier,
        action: () => { this.closePalette(); this.onAgentClick(a); } }));
      return c;
    },
    paletteFiltered() {
      const raw = this.paletteQuery.trim();
      const q = raw.toLowerCase();
      const all = this.paletteCommands();
      if (!q) return all;
      const hits = all.filter(c =>
        c.label.toLowerCase().includes(q) ||
        c.kind.toLowerCase().includes(q) ||
        (c.hint || "").toLowerCase().includes(q)
      );
      // Commands first, then what memory holds, then asking Atlas outright.
      return [...hits, ...(this.memHits || []),
        { id: "ask-free", kind: "ask", icon: "chat", label: `Ask Atlas: ${raw}`, hint: "chat",
          action: () => { this.closePalette(); this.askSend(raw); } }];
    },
    openPalette(seed = "") {
      this.paletteOpen = true; this.paletteQuery = seed; this.paletteIdx = 0; this.memHits = [];
      // Focus at once (the input is always in the DOM) and again after Alpine
      // shows it, so keys typed right after Ctrl+K land in the box.
      this.$refs.paletteInput?.focus();
      this.$nextTick(() => this.$refs.paletteInput?.focus());
    },
    // Memory hits for the palette: free (local embeddings), debounced.
    searchMemory() {
      clearTimeout(this._memTimer);
      const q = (this.paletteQuery || "").trim();
      if (q.length < 3) { this.memHits = []; return; }
      this._memTimer = setTimeout(async () => {
        try {
          const j = await this._getJson("/api/memory/search?limit=5&q=" + encodeURIComponent(q));
          if ((this.paletteQuery || "").trim() !== q) return;
          this.memHits = [
            ...(j.facts || []).map(f => ({ id: "fact-" + f.id, kind: "memory", icon: "book", label: f.content, hint: "fact",
              action: () => { this.closePalette(); this.draft = "About this: " + f.content; this.setView("chat"); } })),
            ...(j.episodes || []).map(e => ({ id: "ep-" + e.id, kind: "memory", icon: "clock", label: e.summary, hint: this.agentName(e.agent || "") + " run",
              action: () => { this.closePalette(); this.setView("agents"); } })),
          ];
        } catch (e) { this.memHits = []; }
      }, 250);
    },
    closePalette() { this.paletteOpen = false; },
    paletteMove(d) {
      const max = this.paletteFiltered().length;
      if (!max) return;
      this.paletteIdx = (this.paletteIdx + d + max) % max;
    },
    paletteSelect() {
      const item = this.paletteFiltered()[this.paletteIdx];
      if (item && item.action) item.action();
    },
  };
}
