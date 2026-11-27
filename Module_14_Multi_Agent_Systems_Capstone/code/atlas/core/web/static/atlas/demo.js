// Demo mode (spec 9): /?demo=1 turns it on for this browser, /?demo=0 off.
// Every /api call is answered here from an invented world (no real mail,
// money, people or companies), the live stream plays a believable morning,
// the night replays on the dial, and the CEO answers chat from a script.
// Writes change the invented world in memory and never reach the server.
// A DEMO DATA tag sits in the top bar so a screenshot cannot pass for real.
(() => {
  const q = new URLSearchParams(location.search);
  try { if (q.has("demo")) localStorage.setItem("atlas.demo", q.get("demo") === "0" ? "0" : "1"); } catch (e) { /* private mode */ }
  let on = q.get("demo") === "1";
  try { on = localStorage.getItem("atlas.demo") === "1"; } catch (e) { /* private mode */ }
  window.ATLAS_DEMO = on;
  if (!on) return;

  // ---------- time helpers: everything is placed relative to now ----------
  const NOW = new Date();
  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago";
  const iso = (d) => d.toISOString();
  const minsAgo = (m) => iso(new Date(NOW.getTime() - m * 60000));
  const today = (h, m = 0, dayOffset = 0) => { const d = new Date(NOW); d.setDate(d.getDate() + dayOffset); d.setHours(h, m, 0, 0); return d; };
  const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const morning = NOW.getHours() < 12;
  const nightDate = morning ? today(12, 0, -1) : today(12, 0, 0);           // the evening the current shift belongs to
  const lastNight = morning ? today(12, 0, -1) : today(12, 0, -1);           // the shift whose results are in
  const shiftStart = today(22, 0, morning ? -1 : -1);                        // last night's 22:00
  const at = (minAfter) => iso(new Date(shiftStart.getTime() + minAfter * 60000));
  let uid = 1000;
  const id = () => "d" + (uid++).toString(16).padStart(7, "0") + "-0000-4000-8000-" + String(uid).padStart(12, "0");

  // ---------- the invented world ----------
  const PROJECTS = [
    { id: id(), slug: "northwind", name: "Northwind HVAC portal", kind: "client", status: "active", priority: 1, objective: "Ship the technician scheduling portal", next_action: "Send the status update with the new dispatch screen", next_action_due: ymd(today(0, 0, 1)), owner_agent: "client_delivery", repos: [], metadata: {} },
    { id: id(), slug: "bayou", name: "Bayou Realty site", kind: "client", status: "active", priority: 2, objective: "Launch the listings site", next_action: "Review the photo gallery build", next_action_due: ymd(today(0, 0, 2)), owner_agent: "client_delivery", repos: [], metadata: {} },
    { id: id(), slug: "college", name: "AI and Robotics, fall term", kind: "college", status: "active", priority: 2, objective: "Finish the term with an A", next_action: "Submit the vision lab writeup", next_action_due: ymd(today(0, 0, 3)), owner_agent: "academic", repos: [], metadata: {} },
    { id: id(), slug: "job-search", name: "Job search", kind: "own", status: "active", priority: 1, objective: "An AI engineering offer by December", next_action: "Follow up with Helix Data", next_action_due: ymd(today(0, 0, 1)), owner_agent: "career", repos: [], metadata: {} },
    { id: id(), slug: "atlas", name: "Atlas", kind: "atlas", status: "active", priority: 3, objective: "The accountable agent", next_action: "Ship the HUD", next_action_due: null, owner_agent: "cto", repos: [], metadata: {} },
  ];
  const AGENTS = [
    ["ceo", "ceo", 6], ["coo", "specialist", 14], ["career", "specialist", 22], ["cto", "specialist", 9], ["cfo", "specialist", 3],
    ["academic", "specialist", 5], ["client_delivery", "specialist", 7], ["opportunity_scout", "specialist", 11], ["news_discovery", "specialist", 7],
  ].map(([name, tier, runs], i) => ({ name, tier, registered: true, last_active: minsAgo(12 + i * 37), runs_7d: runs, monthly_cap: null, month_spent: null }));

  // Last night, as the event stream would have recorded it.
  const NIGHT = [];
  let evId = 5000;
  const ev = (min, kind, agent, title, detail = "", data = {}, ep = null) => {
    const e = { id: evId++, at: at(min), kind, agent, episode_id: ep, title, detail, data };
    NIGHT.push(e); return e;
  };
  const tasks = [
    { agent: "coo", title: "Triage the inbox and file the job mail", tools: ["gmail_find_email", "label_email", "file_signal"], result: "Filed 14 emails; 2 need you: the Helix Data recruiter and the Bayou Lending reply.", cost: 0.31 },
    { agent: "career", title: "Tailor resumes for three strong roles", tools: ["read_resume", "score_posting", "write_resume_pdf", "draft_cover_note"], result: "Three resumes and cover notes ready in Jobs; dry-run screenshots saved.", cost: 0.58 },
    { agent: "client_delivery", title: "Draft the Northwind status update", tools: ["get_project_context", "github_find_issue", "draft_email"], result: "Draft ready: dispatch screen shipped, two open issues, next demo Friday.", cost: 0.22 },
    { agent: "academic", title: "Make a study guide for the vision lab", tools: ["canvas_get_assignment", "write_file"], result: "Study guide written: 12 questions with the formulas you need.", cost: 0.19 },
    { agent: "cto", title: "Fix the flaky planner test and open a PR", tools: ["read_file", "run_tests", "write_file", "open_pull_request"], result: "PR opened with the fix; CI green.", cost: 0.41 },
  ];
  let minute = 30;
  const nightTasks = [];
  tasks.forEach((t, k) => {
    const ep = id(), tid = id();
    ev(minute, "night.task", t.agent, t.title, "", { task_id: tid, status: "running" }, ep);
    ev(minute + 1, "run.started", t.agent, t.title, "", {}, ep);
    t.tools.forEach((tool, j) => {
      ev(minute + 3 + j * 6, "run.tool", t.agent, tool, "", {}, ep);
      ev(minute + 5 + j * 6, "run.tool.done", t.agent, tool, "", { ok: true }, ep);
    });
    const end = minute + 6 + t.tools.length * 6;
    if (k === 0) ev(end - 2, "approval.asked", "coo", "Send the reply to Dana at Bayou Lending", "Drafted at 00:48; waits for your yes", {}, ep);
    ev(end, "run.finished", t.agent, t.title, t.result, { cost: String(t.cost), status: "completed" }, ep);
    ev(end + 1, "night.task", t.agent, t.title, t.result, { task_id: tid, status: "done", cost: String(t.cost) }, ep);
    nightTasks.push({ id: tid, night: ymd(lastNight), project_id: null, title: t.title, definition_of_done: "A result you can use by morning, saved where you will find it.",
      agent: t.agent, engine: "api", budget_usd: null, status: "done", proposed_by: "ceo", episode_id: ep, result: "DONE: " + t.result, error: null,
      cost_usd: String(t.cost), review: null, review_note: null, reviewed_at: null, reported_at: null, created_at: at(-60), started_at: at(minute), finished_at: at(end),
      project_name: null });
    minute = end + 18;
  });
  ev(minute + 20, "notified", null, "Sent you: the overnight report", "6 done, 2 need you");
  ev(525, "news.brief", "news_discovery", "AI this morning: 4 stories", "4 picks from 61 read");
  const NIGHT_COST = tasks.reduce((s, t) => s + t.cost, 0);

  const APPROVALS = [
    { id: id(), agent_name: "coo", action_summary: "Send the reply to Dana at Bayou Lending", status: "pending", created_at: at(160), expires_at: iso(today(18, 0)),
      action_payload: { tool: "send_email", to: "dana@bayou-lending.example", subject: "Re: closing documents", body: "Hi Dana, I'll have the signed documents to you by Friday at 3pm. Thanks for the extension." } },
    { id: id(), agent_name: "career", action_summary: "Submit the application to Helix Data (AI Engineer)", status: "pending", created_at: minsAgo(40), expires_at: iso(today(23, 0)),
      action_payload: { tool: "submit_application", company: "Helix Data", role: "AI Engineer", board: "greenhouse", dry_run_screenshots: 4 } },
  ];

  const PLAN_ITEMS = [
    { kind: "calendar", title: "Lecture: computer vision", h: 10, m: 0, d: 0 },
    { kind: "task", title: "Email the lender the signed documents", h: 14, m: 0, d: 0, project: "bayou" },
    { kind: "canvas", title: "Vision lab writeup due", h: 23, m: 59, d: 2, project: "college" },
    { kind: "task", title: "Northwind demo prep", h: 9, m: 30, d: 1, project: "northwind" },
    { kind: "task", title: "Follow up with Helix Data", h: 11, m: 0, d: 1, project: "job-search" },
  ];
  const doneTasks = new Set();

  const GRAPH = (() => {
    const names = [["Trilok", "person"], ["Northwind HVAC", "business"], ["Bayou Realty", "business"], ["Bayou Lending", "business"], ["Dana Park", "person"],
      ["Helix Data", "business"], ["Orbital Labs", "business"], ["Quanta AI", "business"], ["Greenhouse", "tool"], ["Zapier", "tool"], ["Gmail", "tool"],
      ["Google Calendar", "tool"], ["Canvas", "tool"], ["Computer vision", "course"], ["Robotics II", "course"], ["Dispatch screen", "product"],
      ["Listings site", "product"], ["Atlas", "product"], ["Night shift", "concept"], ["Trust ladder", "concept"], ["Mia Chen", "person"],
      ["Raj Patel", "person"], ["HVAC technicians", "concept"], ["Closing documents", "concept"], ["Resume", "concept"], ["Cover notes", "concept"],
      ["Kirby Supply", "supplier"], ["Austin", "property"], ["Studio apartment", "property"], ["Varsity tutoring", "agency"]];
    const nodes = names.map(([name, type]) => ({ id: name.toLowerCase().replace(/\W+/g, "-"), name, type, canonical: name.toLowerCase() }));
    const E = [["trilok", "atlas"], ["trilok", "northwind-hvac"], ["trilok", "bayou-realty"], ["bayou-realty", "listings-site"], ["northwind-hvac", "dispatch-screen"],
      ["northwind-hvac", "hvac-technicians"], ["northwind-hvac", "mia-chen"], ["bayou-lending", "dana-park"], ["trilok", "bayou-lending"], ["bayou-lending", "closing-documents"],
      ["trilok", "helix-data"], ["helix-data", "greenhouse"], ["orbital-labs", "greenhouse"], ["trilok", "resume"], ["resume", "cover-notes"], ["trilok", "computer-vision"],
      ["computer-vision", "canvas"], ["robotics-ii", "canvas"], ["atlas", "night-shift"], ["atlas", "trust-ladder"], ["atlas", "zapier"], ["zapier", "gmail"],
      ["zapier", "google-calendar"], ["raj-patel", "quanta-ai"], ["northwind-hvac", "kirby-supply"]];
    return { nodes, edges: E.map(([s, t]) => ({ source: s, target: t, type: "related_to" })) };
  })();

  const FACTS = ["Prefers short emails with the ask in the first line.", "Classes are Tuesday and Thursday mornings this term.",
    "Northwind's demo day is every other Friday.", "Will not apply to roles that require relocation.", "Wants the night shift capped at two dollars."]
    .map((c, i) => ({ id: id(), content: c, source: i < 2 ? "claude:import" : "chat", confidence: 1, created_at: minsAgo(600 + i * 300), last_accessed: minsAgo(30), episode_id: null, project_id: null }));

  // ---------- route table ----------
  const nightTotals = () => ({ events: NIGHT.length, runs: tasks.length, tool_calls: NIGHT.filter(e => e.kind === "run.tool").length, approvals_asked: 1,
    screenshots: 0, pushes: 1, cost: NIGHT_COST.toFixed(2), agents: [...new Set(tasks.map(t => t.agent))], done: tasks.length, needs_you: 0 });
  const DONE_KINDS = ["run.finished", "approval.finished", "submit.status", "night.task", "recording", "news.brief"];
  const live = [];  // events the fake stream has emitted this session

  function planItem(p, i) {
    const d = today(p.h, p.m, p.d);
    const proj = PROJECTS.find(x => x.slug === p.project);
    return { kind: p.kind, id: "plan" + i, title: p.title, when: ymd(d), at: p.kind === "canvas" ? null : iso(d), priority: "normal", status: "open",
      done: doneTasks.has("plan" + i), source: p.kind, source_ref: null, repeats: false, definition_of_done: null, notes: null,
      project: proj ? { slug: proj.slug, name: proj.name } : null, created_at: minsAgo(3000), completed_at: null };
  }

  function plan() {
    const days = [];
    for (let k = 0; k < 7; k++) {
      const d = today(12, 0, k);
      days.push({ date: ymd(d), label: d.toLocaleDateString("en-US", { weekday: "short" }) + " " + d.getDate(), today: k === 0,
        items: PLAN_ITEMS.map(planItem).filter(it => it.when === ymd(d)) });
    }
    return { today: ymd(NOW), start: ymd(NOW), end: ymd(today(12, 0, 6)), days, overdue: [], someday: [{ ...planItem({ kind: "task", title: "Rewrite the portfolio README", h: 0, m: 0, d: 0 }, 99), when: null, at: null }],
      rules: [{ id: id(), title: "Weekly job calibration", describe: "every Friday", time: "16:00", next: ymd(today(12, 0, (12 - NOW.getDay()) % 7 || 7)), project: null }],
      counts: { today: days[0].items.length, week: days.reduce((s, d) => s + d.items.length, 0), overdue: 0, someday: 1 } };
  }

  function day() {
    const start = NOW.getTime() - 86400000, end = NOW.getTime() + 86400000;
    const all = [...NIGHT, ...live];
    const kept = all.filter(e => DONE_KINDS.includes(e.kind) && new Date(e.at).getTime() >= start && new Date(e.at) <= new Date() &&
      !(e.kind === "night.task" && e.data.status !== "done"));
    const taskEps = new Set(kept.filter(e => e.kind === "night.task").map(e => e.episode_id));
    const done = kept.filter(e => !(e.kind === "run.finished" && taskEps.has(e.episode_id)))
      .map(e => ({ at: e.at, kind: e.kind, agent: e.agent, title: e.title, episode_id: e.episode_id }));
    const next = PLAN_ITEMS.map((p, i) => planItem(p, i)).filter(it => it.at && new Date(it.at) > NOW && new Date(it.at).getTime() <= end)
      .map(it => ({ at: it.at, kind: it.kind, title: it.title, all_day: false }));
    for (const [h, m, t] of [[22, 30, "Night shift proposes tonight's work"], [23, 30, "Night shift runs"]]) {
      for (const off of [0, 1]) { const d = today(h, m, off); if (d > NOW && d.getTime() <= end) next.push({ at: iso(d), kind: "night", title: t, all_day: false }); }
    }
    next.sort((a, b) => a.at.localeCompare(b.at));
    return { now: iso(new Date()), tz: TZ, night: { start: iso(today(22, 0, morning ? -1 : 0)), end: iso(today(9, 0, morning ? 0 : 1)) },
      done, next, totals: { done: done.length, runs: kept.filter(d => d.kind === "run.finished").length, cost: NIGHT_COST.toFixed(2), hours: 6.4 } };
  }

  function trace(epId) {
    const t = tasks.find((x, k) => NIGHT.some(e => e.episode_id === epId && e.agent === x.agent && e.title === x.title)) || tasks[0];
    const evs = NIGHT.filter(e => e.episode_id === epId);
    const steps = evs.filter(e => e.kind === "run.tool").map((e, i) => ({ id: i + 1, at: e.at, tool: e.title, ok: true, gated: false, approved_by: null,
      input: { note: "invented for the demo" }, result: { ok: true }, error: null }));
    const s = evs[0] ? evs[0].at : at(30), e2 = evs.length ? evs[evs.length - 1].at : at(60);
    return { episode: { id: epId, agent: t.agent, started_at: s, ended_at: e2, duration_s: (new Date(e2) - new Date(s)) / 1000, summary: t.title,
      outcome: "completed", status: "completed", result: "DONE: " + t.result, participants: ["owner", t.agent], tags: ["night"], turns: steps.length + 1,
      stop_reason: "end_turn", tools_used: t.tools }, project: null, trigger: { by: "night", at: s, task: t.title }, steps, ledger: [], approvals: [],
      skills: [], feedback: [], produced: { lessons: [], facts: [], decisions: [], children: [] },
      cost: { model: "claude-sonnet", tokens: { input_tokens: 18200, output_tokens: 2400 }, usd: t.cost }, flags: [] };
  }

  const R = {
    "/api/health": () => ({ ok: true, status: "ok", version: "demo", db: "ok", schema: "current", worker: { last_tick: minsAgo(1), started: minsAgo(300), age_min: 1, fresh: true, state: "running" }, push: true, push_devices: 1, problems: [], slo: { days: 7, brief_within_min: 15, ok: true, objectives: [{ objective: "brief/morning", label: "Morning brief on time", passed: 7, total: 7, last: { ok: true, detail: "Delivered at 08:02, 2 min after 08:00." } }, { objective: "brief/evening", label: "Evening brief on time", passed: 6, total: 7, last: { ok: true, detail: "Delivered at 20:02, 2 min after 20:00." } }, { objective: "heartbeat", label: "Loop heartbeat fresh", passed: 6, total: 7, last: { ok: true, detail: "Longest wait between loop ticks: 31 min (limit 65)." } }] } }),
    "/api/approvals/pending": () => ({ count: APPROVALS.length, approvals: APPROVALS }),
    "/api/notifications": () => ({ count: 2, notifications: [
      { id: id(), kind: "brief/morning", title: "Your morning brief", body: "Six things got done overnight. Two need you: the lender reply and the Helix Data application.", priority: "normal", data: {}, created_at: minsAgo(55), delivered_via: ["push"], read_at: null },
      { id: id(), kind: "approval", title: "Send the reply to Dana at Bayou Lending?", body: "Drafted overnight by the COO.", priority: "high", data: { approval_id: APPROVALS[0] && APPROVALS[0].id }, created_at: at(160), delivered_via: ["push"], read_at: null }] }),
    "/api/episodes": () => ({ count: tasks.length, episodes: tasks.map((t, k) => ({ id: NIGHT.find(e => e.title === t.title && e.kind === "run.started").episode_id, agent: t.agent,
      started_at: NIGHT.find(e => e.title === t.title).at, ended_at: NIGHT.find(e => e.title === t.title && e.kind === "run.finished").at, summary: t.result, outcome: "completed", status: "completed", result: t.result })).reverse() }),
    "/api/decisions": () => ({ count: 0, decisions: [] }),
    "/api/learnings": () => ({ count: 0, proposed: [] }),
    "/api/skills": () => ({ count: 1, skills: [{ id: id(), slug: "short-emails", title: "Lead with the ask", body: "Put the request in the first line of every email you draft.", domain: "writing", applies_to: ["coo", "client_delivery"], status: "active", uses: 12, good: 11, bad: 0, source_url: "", procedure_path: null, last_used_at: minsAgo(200), created_at: minsAgo(20000) }] }),
    "/api/agents": () => ({ count: AGENTS.length, agents: AGENTS }),
    "/api/signals": () => ({ hours: 24, count: 3, signals: [
      { id: id(), source: "gmail", subject: "Helix Data: next steps", triage_notes: "The recruiter wants two times for a call this week", project_slug: "job-search", project_name: "Job search", occurred_at: minsAgo(140), urgency: 3, url: null, deadline: null },
      { id: id(), source: "canvas", subject: "Vision lab writeup", triage_notes: "Due in three days; rubric posted", project_slug: "college", project_name: "AI and Robotics, fall term", occurred_at: minsAgo(400), urgency: 2, url: null, deadline: ymd(today(0, 0, 3)) },
      { id: id(), source: "github", subject: "Northwind: dispatch screen merged", triage_notes: "Merged; the client can see it on staging", project_slug: "northwind", project_name: "Northwind HVAC portal", occurred_at: minsAgo(900), urgency: 1, url: null, deadline: null }] }),
    "/api/briefs": () => ({ count: 1, briefs: [{ id: id(), kind: "morning", body: "Good morning. Six things got done overnight for $" + NIGHT_COST.toFixed(2) + ".\n\nTwo need you: the reply to Dana at Bayou Lending (drafted, waiting on your yes) and the Helix Data application (dry run passed).\n\nToday: the vision lecture at 10, the lender documents by 3. The night shift proposes tonight's work at 22:30.", created_at: minsAgo(55), notification_id: null, read_at: null }] }),
    "/api/projects": () => ({ count: PROJECTS.length, projects: PROJECTS }),
    "/api/cost": () => ({ today: { day: ymd(NOW), total: "0.84", episode_count: 9, by_model: { sonnet: "0.62", haiku: "0.08", opus: "0.14" }, by_agent: { career: "0.31", coo: "0.22", ceo: "0.14", cto: "0.17" } },
      month: { month: ymd(NOW).slice(0, 7), total: "23.40", episode_count: 412 }, global_daily_cap: 8, agent_caps: [] }),
    "/api/cost/history": () => ({ days: 14, series: Array.from({ length: 14 }, (_, k) => ({ day: ymd(today(12, 0, k - 13)), total: (0.6 + Math.abs(Math.sin(k)) * 1.9).toFixed(2), runs: 20 + k })) }),
    "/api/jobs": () => ({ funnel: { interested: 6, applied: 11, screening: 3, interview: 1, offer: 0, rejected: 2, withdrawn: 0, ghosted: 3 },
      applications: [{ id: id(), company: "Helix Data", role: "AI Engineer", status: "screening", applied_at: minsAgo(4000), last_contact_at: minsAgo(140), next_action: "Send two times for the call", next_action_at: iso(today(11, 0, 1)), url: null, drafts: [] },
        { id: id(), company: "Orbital Labs", role: "Forward Deployed Engineer", status: "applied", applied_at: minsAgo(9000), last_contact_at: null, next_action: "Follow up Monday", next_action_at: null, url: null, drafts: [] }],
      postings: [{ id: id(), company: "Quanta AI", title: "Applied AI Engineer", location: "Austin, TX", remote: true, salary_min: 140000, salary_max: 175000, score: 91, reason: "Agents in production, Python, remote: matches your Atlas work directly.", url: "#", status: "new", first_seen_at: minsAgo(500) }] }),
    "/api/jobs/answers": () => ({ standard: [], learned: [], pending: [], missing_required: [] }),
    "/api/jobs/queue": () => ({ items: [], building: false, submitting: false, dry_run: true, queued_today: 3, daily_cap: 5, min_score: 80 }),
    "/api/cost/panel": () => ({ days: 14, brief: { full: 12, quiet: 9, avg_usd: "0.180", last_usd: "0.170", target_usd: "0.20" },
      spend_usd: "4.10", cache_write_share: 0.31, today_usd: "0.21", cap_usd: "8.00", month_usd: "6.40",
      top: [{ agent: "ceo", usd: "2.90" }, { agent: "career", usd: "0.80" }, { agent: "llm/jobs_score", usd: "0.10" }] }),
    "/api/standard": () => ({ started: ymd(today(0, 0, -5)), day: 6, days: 14, measured: 4, passing: 3, total: 6, rows: [
      { key: "yield", name: "Overnight yield", bar: "80% of night tasks accepted", value: "5 of 5", status: "pass", detail: "Every task last night met its definition of done." },
      { key: "never_twice", name: "Never twice", bar: "no outward action repeats", value: "0 repeats", status: "pass", detail: "The ledger caught 2 retries." },
      { key: "receipts", name: "Receipts", bar: "every action explains itself", value: "100%", status: "pass", detail: "Every run has a trace." },
      { key: "spend", name: "Spend", bar: "under the night cap", value: "$" + NIGHT_COST.toFixed(2) + " of $2.00", status: "pass", detail: "" }] }),
    "/api/night": () => ({ phase: morning ? "morning" : "evening", night: ymd(nightDate), tasks: morning ? nightTasks : [], previous: morning ? [] : nightTasks,
      cap: 2, spent: morning ? NIGHT_COST.toFixed(2) : "0.00", agents: AGENTS.map(a => a.name), projects: PROJECTS.map(p => ({ slug: p.slug, name: p.name })) }),
    "/api/night/replay": () => ({ night: ymd(lastNight), window: { start: iso(shiftStart), end: iso(new Date(shiftStart.getTime() + 11 * 3600000)) }, tasks: nightTasks, timeline: NIGHT, totals: nightTotals() }),
    "/api/recordings": () => ({ recordings: [{ id: id(), kind: "lecture", title: "Computer vision: feature matching", path: "", bytes: 0, duration_s: 4980, status: "done", project_id: null,
      notes: "SIFT finds scale-invariant keypoints; RANSAC throws out bad matches.", action_items: [{ text: "Read chapter 7 before Thursday", due: ymd(today(0, 0, 2)) }], episode_id: null, error: null, created_at: minsAgo(2900), updated_at: minsAgo(2800) }] }),
    "/api/plan": plan,
    "/api/plan/parse": (u) => ({ title: u.searchParams.get("text") || "", date: null, time: null, project: null, priority: "normal", rule: null }),
    "/api/news": () => ({ picks: [
      { id: id(), source: "Simon Willison", title: "Agents that keep receipts", url: "#", why: "The same idea as Atlas's ledger, with a tidy write-up you could cite.", published_at: minsAgo(700), picked_at: minsAgo(60), brief_id: "" },
      { id: id(), source: "Hugging Face", title: "A small vision model that runs on a laptop", url: "#", why: "Fits your vision lab and runs on your machine.", published_at: minsAgo(900), picked_at: minsAgo(60), brief_id: "" }],
      latest: { id: id(), created_at: minsAgo(60), considered: 61 } }),
    "/api/outcomes": () => ({ summary: { days: 14, by_kind: {}, this_week: { good: 9, bad: 1, good_ratio: 0.9 }, last_week: { good: 6, bad: 2, good_ratio: 0.75 }, candidates: { proposed: 0, written: 0, dismissed: 0 } }, candidates: [] }),
    "/api/money": () => ({ days: 30, start: ymd(today(0, 0, -30)), end: ymd(NOW), cash_flow: { income_total: "6420.00", expense_total: "4185.50", net: "2234.50", transaction_count: 84, by_category: { rent: "1650.00", groceries: "512.20", software: "96.00", fuel: "180.40" } },
      accounts: [{ account: "demo_checking", last_import: minsAgo(3000), rows: 84, files: 1 }], recent: [],
      portfolio: { positions: [], closed: [], totals: { deposits: "0", withdrawals: "0", dividends: "0", fees: "0", interest: "0", realized: "0", invested: "0" }, cost_basis: "0", last_activity: null, note: "Demo data.", account: "", last_import: null, trades: 0 }, empty: false }),
    "/api/day": day,
    "/api/graph": () => GRAPH,
    "/api/memory": () => ({ query: "", total: FACTS.length, facts: FACTS, profile: [{ id: id(), kind: "constraint", content: "No outward message without my yes.", expires_at: null, created_at: minsAgo(20000) }],
      people: [{ name: "Dana Park", tier: 1, org: "Bayou Lending", project: "bayou", note: "the lender" }, { name: "Mia Chen", tier: 1, org: "Northwind HVAC", project: "northwind", note: "client lead" }] }),
    "/api/memory/search": () => ({ facts: FACTS.slice(0, 2), episodes: [] }),
    "/api/trust": () => ({ days: 30, counts: { deny: 3, approve: 5, auto: 21 }, rungs: [
      { pattern: "send_email", gate: "approve", rung: "asks first", note: "every outward message", tools: ["send_email"], calls: 14, errors: 0, asked: 14, approved: 12, rejected: 2, expired: 0, last_at: minsAgo(800), fenced: true },
      { pattern: "submit_application", gate: "approve", rung: "asks first", note: "", tools: ["submit_application"], calls: 3, errors: 0, asked: 3, approved: 2, rejected: 1, expired: 0, last_at: minsAgo(3000), fenced: true },
      { pattern: "gmail_find_email", gate: "auto", rung: "on its own", note: "reading is safe", tools: ["gmail_find_email"], calls: 212, errors: 1, asked: 0, approved: 0, rejected: 0, expired: 0, last_at: minsAgo(20), fenced: true },
      { pattern: "move_money", gate: "deny", rung: "never", note: "never, by design", tools: [], calls: 0, errors: 0, asked: 0, approved: 0, rejected: 0, expired: 0, last_at: null, fenced: true }] }),
    "/api/setup": () => ({ done: 4, total: 5, complete: false, items: [
      { id: "google", label: "Connect Google", done: true, detail: "connected", steps: [], action: { kind: "tile" } },
      { id: "canvas", label: "Connect Canvas", done: true, detail: "connected", steps: [], action: { kind: "tile" } },
      { id: "github", label: "Connect GitHub", done: true, detail: "connected", steps: [], action: { kind: "tile" } },
      { id: "push", label: "Subscribe your phone to push", done: true, detail: "1 device subscribed; alerts reach the phone.", steps: [], action: { kind: "push" }, devices: 1 },
      { id: "submitter", label: "Install the submitter's browser", done: false, detail: "Playwright is installed but its Chromium browser (the headless shell) is not, for this Windows account.",
        steps: ["Run each command below, then click Check again."], commands: [".venv\\Scripts\\python.exe -m playwright install chromium"], action: { kind: "recheck" } }] }),
    "/api/connect": () => ({ checked: true,
      google: { ok: true, detail: "connected as demo@example.com", client: true, client_id_prefix: "demo-client", consented: true, needs_reconnect: false, connected_at: minsAgo(9000), last_refresh: minsAgo(30),
        scopes: [], scope_words: ["read Gmail", "create drafts", "read the calendar", "create and edit events"], testing_note: "", gmail: { last_ok_at: minsAgo(5) }, calendar: { last_ok_at: minsAgo(5) } },
      canvas: { ok: true, detail: "connected", url: "https://canvas.example.edu", configured: true, state: null },
      github: { ok: true, detail: "sees 6 repos", configured: true, state: null },
      alarm: { ok: true, detail: "pinging every loop", configured: true },
      mcp: { servers: [{ name: "zapier", host: "mcp.zapier.com", agents: ["ceo"], allowed_tools: [], has_token: true, tool_count: 14 }] } }),
    "/api/connect/claude": () => ({ import: { status: "done", filename: "claude-export.zip", items: 1840, done: 1840, facts: 312, notes: 11, cost_usd: "1.12", cap_usd: "10", estimate_usd: "1.20", error: null }, facts_total: 312,
      recent: FACTS.slice(0, 3).map(f => ({ content: f.content, created_at: f.created_at })) }),
    "/api/connect/mcp/zapier/tools": () => ({ name: "zapier", server: "zapier", count: 11, checked_at: iso(NOW), groups: [
      { app: "Gmail", tools: [{ name: "gmail_create_draft", action: "Create draft" }, { name: "gmail_find_email", action: "Find email" }] },
      { app: "Google Calendar", tools: [{ name: "google_calendar_create_detailed_event", action: "Create detailed event" }, { name: "google_calendar_find_events", action: "Find events" }] },
      { app: "Google Drive", tools: [{ name: "google_drive_find_a_file", action: "Find a file" }] },
      { app: "Slack", tools: [{ name: "slack_send_channel_message", action: "Send channel message" }] }] }),
    "/api/events": () => ({ events: [...NIGHT.slice(-12), ...live].slice(-30) }),
    "/api/workflows": () => ({ workflows: ["application_follow_up", "client_status_update", "monthly_pnl"] }),
    "/api/workflows/runs": () => ({ count: 1, runs: [{ id: id(), name: "client_status_update", status: "completed", created_at: minsAgo(1440), error: null }] }),
    "/api/push/vapid-public-key": () => ({ configured: false, key: "" }),
  };

  function routeGet(url) {
    const p = url.pathname;
    if (R[p]) return R[p](url);
    if (p.startsWith("/api/trace/")) return trace(decodeURIComponent(p.slice("/api/trace/".length)));
    if (p.startsWith("/api/approvals/")) return APPROVALS.find(a => a.id === p.split("/")[3]) || null;
    if (p.startsWith("/api/agents/")) {
      const a = AGENTS.find(x => x.name === decodeURIComponent(p.slice("/api/agents/".length)));
      if (!a) return null;
      const eps = R["/api/episodes"]().episodes.filter(e => e.agent === a.name);
      return { ...a, purpose: "An invented agent for the demo: nothing here is real.", runs_30d: a.runs_7d * 4, failed_7d: 0,
        month_spent: (a.runs_7d * 0.11).toFixed(2), tools: [], episodes: eps };
    }
    if (p.startsWith("/api/briefs/")) return R["/api/briefs"]().briefs[0];
    if (p.startsWith("/api/notifications/")) return R["/api/notifications"]().notifications[0];
    if (p.startsWith("/api/recordings/")) return { recording: R["/api/recordings"]().recordings[0] };
    return {};
  }

  function routeWrite(url, body) {
    const p = url.pathname;
    let m;
    if ((m = p.match(/^\/api\/approvals\/([^/]+)\/decide$/))) {
      const i = APPROVALS.findIndex(a => a.id === m[1]);
      if (i >= 0) {
        const [a] = APPROVALS.splice(i, 1);
        emit({ kind: "approval.decided", agent: a.agent_name, title: a.action_summary, data: { decision: body && body.decision } });
        if (body && body.decision === "approved") setTimeout(() => emit({ kind: "approval.finished", agent: a.agent_name, title: a.action_summary, data: { ok: true } }), 1500);
      }
      return { ok: true };
    }
    if ((m = p.match(/^\/api\/night\/tasks\/([^/]+)\/review$/))) {
      const t = nightTasks.find(x => x.id === m[1]); if (t) { t.review = body && body.verdict; t.reviewed_at = iso(new Date()); }
      return { ok: true };
    }
    if ((m = p.match(/^\/api\/plan\/tasks\/([^/]+)\/(\w+)$/))) { if (m[2] === "done") doneTasks.add(m[1]); else doneTasks.delete(m[1]); return { ok: true }; }
    if (p === "/api/plan/tasks") { PLAN_ITEMS.push({ kind: "task", title: (body && body.text) || "New task", h: 17, m: 0, d: 0 }); return { ok: true }; }
    return { ok: true };
  }

  // The CEO in demo mode: a short scripted answer, streamed like the real one.
  function reply(text) {
    const t = text.toLowerCase();
    if (/resume|helix|job|apply/.test(t)) return "Done. Career is on it now: tailoring the resume for Helix Data's AI Engineer role, then a cover note in your voice. I moved the lender email to 14:00 so the morning stays clear. The application waits for your yes before anything is submitted.";
    if (/plan|day|today/.test(t)) return "Here's today, in order. 1) Approve the reply to Dana (two minutes). 2) Vision lecture at 10; Record will take notes. 3) Lender documents by 14:00. 4) Northwind demo prep, about an hour. Tonight the night shift proposes three tasks at 22:30.";
    if (/slip/.test(t)) return "Two things are slipping. The Helix Data recruiter has waited a day for call times; I drafted a reply with Thursday 2pm and Friday 11am. The vision lab writeup is due in three days and the study guide is ready but unread.";
    return "On it. I've added it to the plan and I'll report back on the Bridge. Anything that leaves the laptop waits for your yes.";
  }

  function streamResponse(text) {
    const enc = new TextEncoder();
    const words = text.split(/(\s+)/);
    const epId = id();
    return new Response(new ReadableStream({
      async start(ctl) {
        ctl.enqueue(enc.encode(`data: ${JSON.stringify({ type: "status", text: "thinking" })}\n\n`));
        emit({ kind: "run.started", agent: "ceo", title: "Answering you", episode_id: epId });
        await new Promise(r => setTimeout(r, 500));
        for (let i = 0; i < words.length; i += 2) {
          ctl.enqueue(enc.encode(`data: ${JSON.stringify({ type: "delta", text: words.slice(i, i + 2).join("") })}\n\n`));
          await new Promise(r => setTimeout(r, 35));
        }
        ctl.enqueue(enc.encode(`data: ${JSON.stringify({ type: "done", text, turns: 2, stop_reason: "end_turn", episode_id: epId })}\n\n`));
        emit({ kind: "run.finished", agent: "ceo", title: "Answering you", episode_id: epId, data: { cost: "0.04" } });
        ctl.close();
      },
    }), { status: 200, headers: { "content-type": "text/event-stream" } });
  }

  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const realFetch = window.fetch;
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.origin);
    if (!url.pathname.startsWith("/api/")) return realFetch(input, init);
    const method = String(init.method || "GET").toUpperCase();
    await new Promise(r => setTimeout(r, 60 + Math.random() * 120));
    if (url.pathname === "/api/chat/stream") return streamResponse(reply(JSON.parse(init.body || "{}").message || ""));
    if (method === "GET") {
      const body = routeGet(url);
      return body === null ? json({ detail: "not found" }, 404) : json(body);
    }
    let parsed = null;
    try { parsed = typeof init.body === "string" ? JSON.parse(init.body) : null; } catch (e) { parsed = null; }
    return json(routeWrite(url, parsed));
  };

  // ---------- the live stream: agents at work this morning ----------
  const listeners = new Set();
  function emit(e) {
    const full = { id: evId++, at: iso(new Date()), agent: null, episode_id: null, detail: "", data: {}, ...e };
    live.push(full); if (live.length > 60) live.shift();
    for (const es of listeners) if (es.onmessage) es.onmessage({ data: JSON.stringify(full) });
  }
  const SCRIPT = [
    ["career", "Tailor the resume for Quanta AI", ["read_resume", "score_posting", "write_resume_pdf"], "0.12"],
    ["coo", "Sweep the inbox", ["gmail_find_email", "file_signal"], "0.05"],
    ["academic", "Pull the lab rubric from Canvas", ["canvas_get_assignment"], "0.03"],
    ["opportunity_scout", "Score two new postings", ["search_jobs", "score_posting"], "0.06"],
  ];
  let step = 0;
  setInterval(() => {
    if (document.hidden || !listeners.size) return;
    const [agent, title, tools, cost] = SCRIPT[Math.floor(step / 6) % SCRIPT.length];
    const k = step % 6, ep = "live-" + Math.floor(step / 6);
    if (k === 0) emit({ kind: "run.started", agent, title, episode_id: ep });
    else if (k <= tools.length) emit({ kind: "run.tool", agent, title: tools[k - 1], episode_id: ep });
    else if (k === 5) emit({ kind: "run.finished", agent, title, episode_id: ep, data: { cost, status: "completed" } });
    step += 1;
  }, 2600);

  class DemoEventSource {
    constructor() { this.readyState = 1; listeners.add(this); setTimeout(() => this.onopen && this.onopen(), 50); }
    close() { this.readyState = 2; listeners.delete(this); }
    addEventListener() { /* the dashboard only uses onmessage */ }
  }
  window.EventSource = DemoEventSource;
})();
