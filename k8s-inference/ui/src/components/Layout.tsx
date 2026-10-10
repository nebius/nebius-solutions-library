import { useState, type ReactNode } from "react";
import {
  Bars,
  Cube,
  Gear,
  Key,
  SquareListUl,
  PlugConnection,
} from "@gravity-ui/icons";
import { Button, Icon } from "@gravity-ui/uikit";
import { href } from "../router";
import { useSession } from "./Session";
import { previewEnabled } from "../api/client";
import { ErrorBox, Loading } from "./ui";

const NAV = [
  ["/endpoints", "Endpoints", PlugConnection],
  ["/jobs", "Jobs", SquareListUl],
  ["/models", "Saved definitions", Cube],
] as const;
const MANAGE = [
  ["/keys", "API keys", Key],
  ["/fleet", "Fleet", Cube],
  ["/settings", "Settings", Gear],
] as const;

export function Layout({
  path,
  crumbs,
  children,
  onLogout,
}: {
  path: string;
  crumbs: { label: string; to?: string }[];
  children: ReactNode;
  onLogout: () => void;
}) {
  const { key, fleet, error, reload } = useSession();
  const [open, setOpen] = useState(false);
  if (!key)
    return (
      <div className="session-loading">
        {error ? (
          <>
            <ErrorBox msg={error} retry={reload} />
            <Button onClick={onLogout}>Use another key</Button>
          </>
        ) : (
          <Loading />
        )}
      </div>
    );
  return (
    <div
      className="shell"
      onKeyDown={(event) => {
        if (event.key === "Escape") setOpen(false);
      }}
    >
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to content
      </a>
      <aside
        id="serverless-navigation"
        className={`sidebar ${open ? "sidebar--open" : ""}`}
      >
        <a
          className="brand"
          href={href("/endpoints")}
          aria-label="Nebius Serverless home"
        >
          <img src="/nebius-logo.svg" alt="Nebius" width="118" height="24" />
        </a>
        <div className="sidebar__section">Serverless AI</div>
        <nav className="nav" aria-label="Main navigation">
          {NAV.map(([to, label, icon]) => (
            <a
              key={to}
              href={href(to)}
              className={path === to ? "active" : ""}
              aria-current={path === to ? "page" : undefined}
              onClick={() => setOpen(false)}
            >
              <Icon data={icon} size={16} />
              {label}
            </a>
          ))}
        </nav>
        <div className="sidebar__section">Manage</div>
        <nav className="nav" aria-label="Management">
          {MANAGE.map(([to, label, icon]) => (
            <a
              key={to}
              href={href(to)}
              className={path === to ? "active" : ""}
              onClick={() => setOpen(false)}
            >
              <Icon data={icon} size={16} />
              {label}
            </a>
          ))}
        </nav>
        <div className="sidebar__footer">
          <div>Serverless inference fleet</div>
          <div className="small">{key?.tenant ?? "Connecting…"}</div>
          <button onClick={onLogout}>Sign out</button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="context">
            <Button
              className="mobile-menu"
              view="flat"
              aria-label="Toggle navigation"
              aria-expanded={open}
              aria-controls="serverless-navigation"
              onClick={() => setOpen(!open)}
            >
              <Icon data={Bars} />
            </Button>
            <Icon data={Cube} size={16} />
            <span>{key?.tenant ?? "Serverless"}</span>
            <span className="context-separator">/</span>
            <span>Inference fleet</span>
          </div>
          <div className="topbar-tools">
            {previewEnabled && (
              <span
                className="badge preview-label"
                title="UI preview · sample data"
              >
                <span className="preview-label-desktop">
                  UI preview · sample data
                </span>
                <span className="preview-label-mobile">Sample data</span>
              </span>
            )}
            <span className="region-context small muted">
              {fleet?.regions.length
                ? `${fleet.regions.length} region${fleet.regions.length === 1 ? "" : "s"}`
                : ""}
            </span>
            <span className="tenant-role small muted">
              {key?.role === "admin" ? "Administrator" : ""}
            </span>
            <span className="tenant-avatar" aria-label="Tenant">
              {key?.tenant?.slice(0, 1).toUpperCase() ?? "N"}
            </span>
          </div>
        </header>
        <main id="main-content" className="content" tabIndex={-1}>
          {crumbs.length > 1 && (
            <nav className="crumbs" aria-label="Breadcrumb">
              <a href={href("/endpoints")}>Serverless</a>
              {crumbs.map((c, i) => (
                <span key={i} className="crumbs" style={{ margin: 0 }}>
                  <span>/</span>
                  {c.to ? (
                    <a href={href(c.to)}>{c.label}</a>
                  ) : (
                    <span className="current">{c.label}</span>
                  )}
                </span>
              ))}
            </nav>
          )}
          {children}
        </main>
      </div>
    </div>
  );
}
