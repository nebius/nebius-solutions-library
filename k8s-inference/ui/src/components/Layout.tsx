import type { ReactNode } from "react";
import { href } from "../router";
import { apiHost, settings } from "../api/client";
import { grafanaUrl } from "../config";

const Icon = {
  models: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" /><path d="M3 12l9 4.5 9-4.5" /><path d="M3 16.5 12 21l9-4.5" /></svg>,
  jobs: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9h10M7 13h6" /></svg>,
  endpoints: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="3" /><path d="M4 12h5M15 12h5M12 4v5M12 15v5" /></svg>,
  keys: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="8" cy="12" r="4" /><path d="M12 12h9M18 12v3M15 12v2" /></svg>,
  settings: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2" /></svg>,
};
const NAV: [string, string, ReactNode][] = [
  ["/models", "Models", Icon.models],
  ["/jobs", "Jobs", Icon.jobs],
  ["/endpoints", "Endpoints", Icon.endpoints],
  ["/keys", "API keys", Icon.keys],
];

export function Layout({ path, crumbs, children, onLogout }: { path: string; crumbs: { label: string; to?: string }[]; children: ReactNode; onLogout: () => void }) {
  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href={href("/models")}>
          <span className="brand__mark">N</span>
          <span><div className="brand__name">Nebius Serverless</div><div className="brand__sub">2.0 · Scientific AI</div></span>
        </a>
        <div className="sidebar__section">Compute</div>
        <nav className="nav">
          {NAV.map(([to, label, icon]) => <a key={to} href={href(to)} className={path === to || path.startsWith(to + "/") ? "active" : ""}>{icon}{label}</a>)}
        </nav>
        <div className="sidebar__section">Operator</div>
        <nav className="nav">
          {grafanaUrl() && <a href={grafanaUrl()} target="_blank" rel="noreferrer">{Icon.endpoints}Grafana</a>}
          <a href={href("/settings")} className={path === "/settings" ? "active" : ""}>{Icon.settings}Settings</a>
        </nav>
        <div className="sidebar__footer">
          <div>Key <span className="mono">{settings.key ? "..." + settings.key.slice(-6) : "none"}</span></div>
          <div>API <span className="mono">{apiHost()}</span></div>
          <div className="row"><button onClick={onLogout}>Sign out</button></div>
        </div>
      </aside>
      <div className="main">
        <div className="topbar">
          <div className="crumbs">
            <a href={href("/models")}>Serverless 2.0</a>
            {crumbs.map((c, i) => <span key={i} className="crumbs"><span>/</span>{c.to ? <a href={href(c.to)}>{c.label}</a> : <span className="current">{c.label}</span>}</span>)}
          </div>
          <div className="row small muted">
            <span>Regions: eu-north1 (hub) · eu-south1</span>
          </div>
        </div>
        <div className="content">{children}</div>
      </div>
    </div>
  );
}
