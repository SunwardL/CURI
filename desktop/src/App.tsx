import { useCallback, useEffect, useMemo, useState } from "react";
import { invoke, isTauri } from "@tauri-apps/api/core";
import type { EChartsOption } from "echarts";
import ReactECharts from "echarts-for-react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Check,
  ChevronDown,
  CircleHelp,
  Clock3,
  Command,
  Database,
  Gauge,
  Layers3,
  RefreshCw,
  Router,
  ShieldCheck,
  TerminalSquare,
} from "lucide-react";

type Turn = {
  ts: string | null;
  project: string;
  model: string;
  input_tokens: number;
  cached_tokens: number;
  output_tokens: number;
  error?: string | null;
};

type RelayEvent = {
  ts: string | null;
  requested_model: string | null;
  reported_model: string | null;
  status: number | null;
  attempts: number | null;
  first_byte_ms: number | null;
  duration_ms: number | null;
  error_class: string | null;
  stream_terminal: string | null;
};

type Summary = {
  meta: { generated_at: string; last_scan: string | null; files: unknown[] };
  coverage: { first: string | null; last: string | null; turns_total: number };
  today: {
    turns: number;
    input_tokens: number;
    cached_tokens: number;
    output_tokens: number;
    reasoning_tokens: number;
    model_calls: number;
    errors: number;
    capacity_errors: number;
    avg_duration_ms: number | null;
  };
  summary: {
    turns: number;
    input_tokens: number;
    output_tokens: number;
    errors: number;
    avg_duration_ms: number | null;
  };
  daily: { date: string; input_tokens: number; output_tokens: number }[];
  models: { model: string; turns: number }[];
  projects: { project: string; turns: number }[];
  turns: Turn[];
  tools: { total: number; by_category: Record<string, number>; recent: unknown[] };
  quota: { timestamp: string | null; windows: Record<string, unknown> | null };
  relay: {
    total: number;
    success: number;
    retries: number;
    model_differences: number;
    recent: RelayEvent[];
  };
};

type Page = "overview" | "models" | "relay";
type Connection = "connecting" | "online" | "offline";

const numberFormat = new Intl.NumberFormat();
const chartColors = ["#168575", "#e5a149", "#527fc3", "#bd6171", "#8b75b8"];

function formatNumber(value: number | null | undefined): string {
  return value == null ? "-" : numberFormat.format(value);
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "No data";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function getWindowEntries(windows: Record<string, unknown> | null | undefined) {
  if (!windows) return [];
  return Object.entries(windows).flatMap(([key, value]) => {
    if (!value || typeof value !== "object") return [];
    const record = value as Record<string, unknown>;
    if (record.used_percent == null) return [];
    const used = Number(record.used_percent);
    if (!Number.isFinite(used)) return [];
    return [{
      key,
      name: typeof record.name === "string" ? record.name : key,
      used,
      resetsAt: typeof record.resets_at === "string" ? record.resets_at : null,
      duration: typeof record.window_minutes === "number" ? record.window_minutes : null,
    }];
  });
}

async function loadSummary(): Promise<Summary> {
  let baseUrl = import.meta.env.VITE_CURI_API_URL || "http://127.0.0.1:8792";
  if (isTauri()) {
    let port: number | null;
    try {
      port = await invoke<number | null>("backend_port");
    } catch {
      port = null;
    }
    if (!port) throw new Error("The local scanner is starting up.");
    baseUrl = `http://127.0.0.1:${port}`;
  }

  const response = await fetch(`${baseUrl}/api/summary`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Local API returned ${response.status}.`);
  return (await response.json()) as Summary;
}

function Metric({
  label,
  value,
  detail,
  icon: Icon,
  tone = "green",
}: {
  label: string;
  value: string;
  detail: string;
  icon: typeof Activity;
  tone?: "green" | "amber" | "blue" | "red";
}) {
  return (
    <article className="metric">
      <div className="metric-topline">
        <span>{label}</span>
        <span className={`metric-icon tone-${tone}`}><Icon size={16} strokeWidth={1.8} /></span>
      </div>
      <strong className="metric-value">{value}</strong>
      <span className="metric-detail">{detail}</span>
    </article>
  );
}

function PanelHeading({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="panel-heading">
      <div>
        <h2>{title}</h2>
        {detail && <p>{detail}</p>}
      </div>
    </div>
  );
}

function EmptyState({ children }: { children: string }) {
  return <div className="empty-state"><Database size={17} /><span>{children}</span></div>;
}

function App() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState("");
  const [page, setPage] = useState<Page>("overview");
  const [modelFilter, setModelFilter] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [refreshToken, setRefreshToken] = useState(0);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let active = true;
    let timer = 0;
    let loading = false;

    const refresh = async () => {
      if (loading) return;
      loading = true;
      setRefreshing(true);
      try {
        const next = await loadSummary();
        if (active) {
          setSummary(next);
          setConnection("online");
          setError("");
        }
      } catch (reason) {
        if (active) {
          setConnection("offline");
          setError(reason instanceof Error ? reason.message : "Could not connect to the local scanner.");
        }
      } finally {
        loading = false;
        if (active) {
          setRefreshing(false);
          timer = window.setTimeout(refresh, 3000);
        }
      }
    };

    void refresh();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [refreshToken]);

  const filteredTurns = useMemo(() => {
    const turns = summary?.turns ?? [];
    return turns.filter((turn) =>
      (!modelFilter || turn.model === modelFilter) &&
      (!projectFilter || turn.project === projectFilter),
    );
  }, [summary, modelFilter, projectFilter]);

  const trend = useMemo(() => {
    if (!modelFilter && !projectFilter) {
      return (summary?.daily ?? []).slice(-14).map((day) => [
        day.date,
        { input: day.input_tokens, output: day.output_tokens },
      ] as [string, { input: number; output: number }]);
    }
    const buckets = new Map<string, { input: number; output: number }>();
    for (const turn of filteredTurns) {
      const date = turn.ts?.slice(0, 10) || "Unknown";
      const current = buckets.get(date) ?? { input: 0, output: 0 };
      current.input += turn.input_tokens || 0;
      current.output += turn.output_tokens || 0;
      buckets.set(date, current);
    }
    return [...buckets.entries()].slice(-14);
  }, [filteredTurns, modelFilter, projectFilter, summary]);

  const trendOption = useMemo<EChartsOption>(() => ({
    color: chartColors.slice(0, 2),
    animationDuration: 350,
    grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
    legend: { top: 0, right: 4, itemWidth: 9, itemHeight: 9, textStyle: { color: "#65716e", fontSize: 11 } },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category",
      data: trend.map(([date]) => date === "Unknown" ? date : date.slice(5)),
      boundaryGap: false,
      axisLine: { lineStyle: { color: "#dfe5e2" } },
      axisTick: { show: false },
      axisLabel: { color: "#78837f", fontSize: 10 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#edf0ee" } },
      axisLabel: { color: "#78837f", fontSize: 10, formatter: (value: number) => numberFormat.format(value) },
    },
    series: [
      { name: "Input", type: "line", smooth: 0.25, symbol: "circle", symbolSize: 5, data: trend.map(([, values]) => values.input), areaStyle: { opacity: 0.08 } },
      { name: "Output", type: "line", smooth: 0.25, symbol: "circle", symbolSize: 5, data: trend.map(([, values]) => values.output), areaStyle: { opacity: 0.08 } },
    ],
  }), [trend]);

  const toolEntries = Object.entries(summary?.tools.by_category ?? {}).sort((a, b) => b[1] - a[1]);
  const toolOption = useMemo<EChartsOption>(() => ({
    color: chartColors,
    tooltip: { trigger: "item" },
    series: [{
      type: "pie",
      radius: ["62%", "84%"],
      center: ["50%", "50%"],
      avoidLabelOverlap: true,
      label: { show: false },
      itemStyle: { borderColor: "#ffffff", borderWidth: 3 },
      data: toolEntries.map(([name, value]) => ({ name, value })),
    }],
  }), [toolEntries]);

  const pageTitle = page === "overview" ? "Overview" : page === "models" ? "Models" : "Relay activity";
  const today = summary?.today;
  const quotaWindows = getWindowEntries(summary?.quota.windows);
  const relay = summary?.relay;

  const refreshNow = useCallback(() => setRefreshToken((value) => value + 1), []);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Command size={19} strokeWidth={2.1} /></div>
          <div><strong>CURI</strong><span>Usage intelligence</span></div>
        </div>

        <div className="nav-label">WORKSPACE</div>
        <nav className="primary-nav" aria-label="Main navigation">
          <button className={page === "overview" ? "nav-item active" : "nav-item"} onClick={() => setPage("overview")}>
            <Gauge size={17} /><span>Overview</span>
          </button>
          <button className={page === "models" ? "nav-item active" : "nav-item"} onClick={() => setPage("models")}>
            <Layers3 size={17} /><span>Models</span>
          </button>
          <button className={page === "relay" ? "nav-item active" : "nav-item"} onClick={() => setPage("relay")}>
            <Router size={17} /><span>Relay activity</span>
          </button>
        </nav>

        <div className="sidebar-bottom">
          <div className="privacy-note">
            <ShieldCheck size={17} />
            <div><strong>Local by design</strong><span>Usage data stays on this device.</span></div>
          </div>
          <div className="sidebar-version">CURI desktop <span>0.1.0</span></div>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <div className="breadcrumb">CURI <span>/</span> Local analytics</div>
            <h1>{pageTitle}</h1>
          </div>
          <div className="top-actions">
            <div className={`connection-pill ${connection}`}>
              <span className="connection-dot" />
              {connection === "online" ? "Scanner connected" : connection === "connecting" ? "Starting scanner" : "Scanner unavailable"}
            </div>
            <button className="icon-button refresh-button" onClick={refreshNow} title="Refresh data" aria-label="Refresh data" disabled={refreshing}>
              <RefreshCw size={16} className={refreshing ? "spin" : ""} />
            </button>
          </div>
        </header>

        {connection === "offline" && (
          <div className="connection-banner">
            <AlertTriangle size={17} />
            <div><strong>Local scanner is not responding</strong><span>{error} Retrying automatically.</span></div>
          </div>
        )}

        {page === "overview" && (
          <Overview
            summary={summary}
            today={today}
            quotaWindows={quotaWindows}
            trend={trend}
            trendOption={trendOption}
            toolEntries={toolEntries}
            toolOption={toolOption}
            filteredTurns={filteredTurns}
            modelFilter={modelFilter}
            projectFilter={projectFilter}
            setModelFilter={setModelFilter}
            setProjectFilter={setProjectFilter}
            relay={relay}
          />
        )}
        {page === "models" && <ModelsPage summary={summary} />}
        {page === "relay" && <RelayPage relay={relay} />}

        <footer className="workspace-footer">
          <span><Database size={13} /> Local SQLite</span>
          <span><Clock3 size={13} /> Last scan {formatDate(summary?.meta.last_scan)}</span>
          {connection === "online" && <span className="footer-online"><Check size={13} /> Up to date</span>}
        </footer>
      </main>
    </div>
  );
}

function Overview({
  summary,
  today,
  quotaWindows,
  trend,
  trendOption,
  toolEntries,
  toolOption,
  filteredTurns,
  modelFilter,
  projectFilter,
  setModelFilter,
  setProjectFilter,
  relay,
}: {
  summary: Summary | null;
  today: Summary["today"] | undefined;
  quotaWindows: ReturnType<typeof getWindowEntries>;
  trend: [string, { input: number; output: number }][];
  trendOption: EChartsOption;
  toolEntries: [string, number][];
  toolOption: EChartsOption;
  filteredTurns: Turn[];
  modelFilter: string;
  projectFilter: string;
  setModelFilter: (value: string) => void;
  setProjectFilter: (value: string) => void;
  relay: Summary["relay"] | undefined;
}) {
  const successPercent = relay?.total ? Math.round((relay.success / relay.total) * 100) : null;
  const recentRelay = relay?.recent.slice(0, 5) ?? [];
  const models = summary?.models ?? [];
  const projects = summary?.projects ?? [];

  return (
    <div className="page-content">
      <section className="metrics-grid" aria-label="Today's usage">
        <Metric
          label="Tokens today"
          value={formatNumber((today?.input_tokens ?? 0) + (today?.output_tokens ?? 0))}
          detail={`${formatNumber(today?.input_tokens)} input / ${formatNumber(today?.output_tokens)} output`}
          icon={Activity}
          tone="green"
        />
        <Metric
          label="Turns today"
          value={formatNumber(today?.turns)}
          detail={`${formatNumber(today?.model_calls)} with identifiable model`}
          icon={BarChart3}
          tone="blue"
        />
        <Metric
          label="Relay success"
          value={successPercent == null ? "-" : `${successPercent}%`}
          detail={relay?.total ? `${formatNumber(relay.total)} observed requests` : "No relay events recorded"}
          icon={Router}
          tone="amber"
        />
        <Metric
          label="Errors today"
          value={formatNumber(today?.errors)}
          detail={`${formatNumber(today?.capacity_errors)} capacity errors`}
          icon={AlertTriangle}
          tone={today?.errors ? "red" : "green"}
        />
      </section>

      <section className="panel trend-panel">
        <div className="panel-heading trend-heading">
          <div><h2>Token usage</h2><p>{modelFilter || projectFilter ? "Filtered across the latest 500 turns" : "Daily input and output across all indexed turns"}</p></div>
          <div className="filter-group">
            <label><span>Model</span><select value={modelFilter} onChange={(event) => setModelFilter(event.target.value)}>
              <option value="">All models</option>
              {models.map((item) => <option key={item.model} value={item.model}>{item.model}</option>)}
            </select><ChevronDown size={13} /></label>
            <label><span>Project</span><select value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
              <option value="">All projects</option>
              {projects.map((item) => <option key={item.project} value={item.project}>{item.project}</option>)}
            </select><ChevronDown size={13} /></label>
          </div>
        </div>
        {trend.length ? <ReactECharts option={trendOption} notMerge lazyUpdate style={{ height: 260 }} /> : <EmptyState>No session usage has been indexed yet.</EmptyState>}
      </section>

      <section className="overview-lower-grid">
        <div className="panel quota-panel">
          <PanelHeading title="Quota windows" detail={summary?.quota.timestamp ? `Snapshot ${formatDate(summary.quota.timestamp)}` : "Latest observed rate limits"} />
          {quotaWindows.length ? (
            <div className="quota-list">
              {quotaWindows.map((window) => (
                <div className="quota-item" key={window.key}>
                  <div className="quota-topline"><strong>{window.name}</strong><span>{Math.round(window.used)}%</span></div>
                  <div className="quota-track"><span style={{ width: `${Math.min(100, Math.max(0, window.used))}%` }} /></div>
                  <div className="quota-meta"><span>{window.duration ? `${window.duration} min window` : "Usage observed"}</span><span>{window.resetsAt ? `Resets ${formatDate(window.resetsAt)}` : "Reset time unknown"}</span></div>
                </div>
              ))}
            </div>
          ) : <EmptyState>No quota snapshot available.</EmptyState>}
        </div>

        <div className="panel tools-panel">
          <PanelHeading title="Tool activity" detail={`${formatNumber(summary?.tools.total)} calls observed`} />
          {toolEntries.length ? (
            <div className="tool-chart-layout">
              <ReactECharts option={toolOption} notMerge lazyUpdate style={{ height: 172, width: 172 }} />
              <div className="tool-legend">
                {toolEntries.map(([name, count], index) => (
                  <div className="legend-row" key={name}><span className="legend-dot" style={{ background: chartColors[index % chartColors.length] }} /><span>{name}</span><strong>{formatNumber(count)}</strong></div>
                ))}
              </div>
            </div>
          ) : <EmptyState>No tool calls observed yet.</EmptyState>}
        </div>
      </section>

      <section className="panel recent-panel">
        <PanelHeading title="Recent relay requests" detail="Most recent metadata recorded by the local relay" />
        {recentRelay.length ? <RelayTable rows={recentRelay} /> : <EmptyState>No relay events have been recorded.</EmptyState>}
      </section>

      <div className="coverage-strip">
        <div><Database size={16} /><span><strong>{formatNumber(summary?.meta.files.length)}</strong> source files</span></div>
        <div><Clock3 size={16} /><span>Coverage <strong>{formatDate(summary?.coverage.first)}</strong> to <strong>{formatDate(summary?.coverage.last)}</strong></span></div>
        <div><CircleHelp size={16} /><span><strong>{formatNumber(summary?.coverage.turns_total)}</strong> tracked turns</span></div>
      </div>
      <span className="sr-only">{formatNumber(filteredTurns.length)} turns in selected view</span>
    </div>
  );
}

function ModelsPage({ summary }: { summary: Summary | null }) {
  const models = summary?.models ?? [];
  const projects = summary?.projects ?? [];
  const maxTurns = Math.max(1, ...models.map((item) => item.turns));

  return (
    <div className="page-content">
      <section className="metrics-grid compact-metrics">
        <Metric label="Observed models" value={formatNumber(models.length)} detail="Models seen in local sessions" icon={Layers3} tone="blue" />
        <Metric label="Tracked turns" value={formatNumber(summary?.coverage.turns_total)} detail="Across indexed session files" icon={Activity} tone="green" />
        <Metric label="Projects" value={formatNumber(projects.length)} detail="Working directories identified" icon={TerminalSquare} tone="amber" />
        <Metric label="Avg. duration" value={summary?.summary.avg_duration_ms == null ? "-" : `${(summary.summary.avg_duration_ms / 1000).toFixed(1)}s`} detail="Completed turns" icon={Clock3} tone="blue" />
      </section>

      <section className="panel list-panel">
        <PanelHeading title="Model usage" detail="Turns by observed model" />
        {models.length ? (
          <div className="model-list">
            {models.map((item) => (
              <div className="model-row" key={item.model}>
                <div className="model-identity"><span className="model-glyph"><Layers3 size={16} /></span><strong>{item.model}</strong></div>
                <div className="model-bar-area"><span className="model-bar-track"><span style={{ width: `${Math.max(2, (item.turns / maxTurns) * 100)}%` }} /></span><span className="model-turns">{formatNumber(item.turns)} turns</span></div>
              </div>
            ))}
          </div>
        ) : <EmptyState>No model activity indexed yet.</EmptyState>}
      </section>

      <section className="panel list-panel">
        <PanelHeading title="Projects observed" detail="Derived from session working directories" />
        {projects.length ? (
          <div className="project-table">
            {projects.map((item) => <div className="project-row" key={item.project}><span><TerminalSquare size={15} />{item.project}</span><strong>{formatNumber(item.turns)} turns</strong></div>)}
          </div>
        ) : <EmptyState>No project names found in session metadata.</EmptyState>}
      </section>
    </div>
  );
}

function RelayPage({ relay }: { relay: Summary["relay"] | undefined }) {
  const events = relay?.recent ?? [];
  const successRate = relay?.total ? `${Math.round((relay.success / relay.total) * 100)}%` : "-";

  return (
    <div className="page-content">
      <section className="metrics-grid compact-metrics">
        <Metric label="Observed requests" value={formatNumber(relay?.total)} detail="Recent relay metadata" icon={Router} tone="blue" />
        <Metric label="Successful" value={successRate} detail={`${formatNumber(relay?.success)} successful responses`} icon={Check} tone="green" />
        <Metric label="Retry attempts" value={formatNumber(relay?.retries)} detail="Additional upstream attempts" icon={RefreshCw} tone="amber" />
        <Metric label="Model differences" value={formatNumber(relay?.model_differences)} detail="Requested and reported models differ" icon={AlertTriangle} tone="red" />
      </section>

      <section className="panel recent-panel relay-full-panel">
        <PanelHeading title="Request history" detail="Up to 20 most recent relay events" />
        {events.length ? <RelayTable rows={events} /> : <EmptyState>No relay events have been recorded.</EmptyState>}
      </section>

      <div className="relay-note"><ShieldCheck size={16} /><span>CURI stores relay metadata only. Request bodies, prompts, responses and API keys are not shown here.</span></div>
    </div>
  );
}

function RelayTable({ rows }: { rows: RelayEvent[] }) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr><th>Time</th><th>Requested model</th><th>Reported model</th><th>Status</th><th>Attempts</th><th>Duration</th></tr></thead>
        <tbody>
          {rows.map((row, index) => {
            const success = row.status != null && row.status >= 200 && row.status < 400;
            return (
              <tr key={`${row.ts ?? "event"}-${index}`}>
                <td className="nowrap">{formatDate(row.ts)}</td>
                <td>{row.requested_model || "Unknown"}</td>
                <td>{row.reported_model || "Unknown"}</td>
                <td><span className={`status-label ${success ? "status-success" : "status-failure"}`}>{row.status ?? "Unknown"}</span></td>
                <td>{formatNumber(row.attempts ?? 1)}</td>
                <td>{row.duration_ms == null ? "-" : `${formatNumber(row.duration_ms)} ms`}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default App;
