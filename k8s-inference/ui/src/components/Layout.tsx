import { useRef, useState, type ReactNode } from "react";
import { Icon } from "@gravity-ui/uikit";
import {
  Bars,
  Cube,
  Server,
  Key,
  Gear,
  CirclePlay,
  ArrowRightFromSquare,
  Globe,
  Person,
} from "@gravity-ui/icons";
import { href } from "../router";
import { apiHost } from "../api/client";
import { grafanaUrl } from "../config";
import { Button } from "./controls";
import { useSession } from "./Session";

const NAV = [
  ["/endpoints", "Endpoints", Server],
  ["/jobs", "Jobs", CirclePlay],
  ["/models", "Models", Cube],
  ["/keys", "API keys", Key],
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
  const { key, fleet } = useSession();
  const [expanded, setExpanded] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  return (
    <div className="shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(e) => {
          e.preventDefault();
          mainRef.current?.focus();
        }}
      >
        Skip to content
      </a>
      <aside className={`sidebar ${expanded ? "sidebar--expanded" : ""}`}>
        <a
          className="brand"
          href={href("/endpoints")}
          aria-label="Nebius Serverless AI home"
        >
          <img src="/nebius.svg" width="117" height="18" alt="Nebius" />
          <span>Serverless AI</span>
        </a>
        <div className="sidebar__context">
          <Icon data={Cube} size={18} />
          <div>
            <strong>Inference fleet</strong>
            <span>Standalone console</span>
          </div>
        </div>
        <div className="sidebar__section">Serverless AI</div>
        <nav className="nav" aria-label="Main navigation">
          {NAV.map(([to, label, icon]) => (
            <a
              key={to}
              href={href(to)}
              onClick={() => setExpanded(false)}
              aria-current={path === to ? "page" : undefined}
              className={path === to ? "active" : ""}
            >
              <Icon data={icon} size={18} />
              {label}
            </a>
          ))}
        </nav>
        <div className="sidebar__bottom">
          <nav className="nav" aria-label="Console navigation">
            {grafanaUrl() && (
              <a href={grafanaUrl()} target="_blank" rel="noreferrer">
                <Icon data={Globe} size={18} />
                Observability
              </a>
            )}
            <a
              href={href("/settings")}
              onClick={() => setExpanded(false)}
              className={path === "/settings" ? "active" : ""}
              aria-current={path === "/settings" ? "page" : undefined}
            >
              <Icon data={Gear} size={18} />
              Settings
            </a>
          </nav>
          <div className="sidebar__footer">
            <div className="account">
              <span className="account__avatar">
                <Icon data={Person} size={18} />
              </span>
              <div>
                <strong>{key?.alias || key?.tenant || "API key"}</strong>
                <span>
                  {key?.role === "admin" ? "Administrator" : "Member"}
                </span>
              </div>
            </div>
            <Button view="flat" size="m" onClick={onLogout}>
              <Icon data={ArrowRightFromSquare} size={16} />
              Sign out
            </Button>
          </div>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="row">
            <Button
              className="menu-toggle"
              view="flat"
              aria-label="Toggle navigation"
              aria-expanded={expanded}
              onClick={() => setExpanded(!expanded)}
            >
              <Icon data={Bars} size={20} />
            </Button>
            <Icon data={Cube} size={16} />
            <strong>{key?.tenant || "Inference fleet"}</strong>
            <span className="context-tag">Standalone</span>
          </div>
          <span className="topbar__region">
            <Icon data={Globe} size={16} />
            {fleet
              ? `${fleet.regions.length} region${fleet.regions.length === 1 ? "" : "s"}`
              : "Region information unavailable"}
          </span>
        </header>
        <main id="main-content" ref={mainRef} tabIndex={-1} className="content">
          <nav className="crumbs" aria-label="Breadcrumb">
            <a href={href("/endpoints")}>Serverless AI</a>
            {crumbs.map((c, i) => (
              <span key={i}>
                <span className="crumb-divider">/</span>
                {c.to ? (
                  <a href={href(c.to)}>{c.label}</a>
                ) : (
                  <span aria-current="page">{c.label}</span>
                )}
              </span>
            ))}
          </nav>
          {children}
        </main>
        <footer className="main-footer">
          Serverless AI<span title={apiHost()}>Connected to {apiHost()}</span>
        </footer>
      </div>
    </div>
  );
}
