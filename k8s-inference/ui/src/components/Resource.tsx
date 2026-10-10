import type { ReactNode } from "react";
import { DropdownMenu } from "@gravity-ui/uikit";
import { EllipsisVertical, Magnifier, Plus } from "@gravity-ui/icons";
import { Button, CopyButton, Icon, fmt } from "./ui";
import { href, navigate, useRoute } from "../router";

export function resourcePath(
  kind: "endpoints" | "jobs",
  id: string,
  region?: string,
  tab?: string,
) {
  const query = new URLSearchParams({
    ...(region ? { region } : {}),
    ...(tab && tab !== "Overview" ? { tab } : {}),
  });
  return `/${kind}/${encodeURIComponent(id)}${query.size ? "?" + query : ""}`;
}
export function useResourceTab(items: string[]) {
  const route = useRoute();
  const active = route.query.get("tab") ?? "Overview";
  return {
    tab: items.includes(active) ? active : "Overview",
    setTab: (tab: string) => {
      const q = new URLSearchParams(route.query);
      if (tab === "Overview") q.delete("tab");
      else q.set("tab", tab);
      navigate(route.path + (q.size ? "?" + q : ""));
    },
  };
}
export function PageHeader({
  title,
  count,
  create,
  children,
}: {
  title: string;
  count?: number;
  create?: { label: string; to: string };
  children?: ReactNode;
}) {
  return (
    <div className="page-head">
      <h1>
        {title}
        {count != null && <span className="resource-count">{count}</span>}
      </h1>
      <div className="actions">
        {children}
        {create && (
          <Button view="action" size="l" href={href(create.to)}>
            <Icon data={Plus} size={16} /> {create.label}
          </Button>
        )}
      </div>
    </div>
  );
}
export function SearchToolbar({
  search,
  setSearch,
  status,
  setStatus,
  statuses,
  region,
  setRegion,
  regions,
  children,
}: {
  search: string;
  setSearch: (s: string) => void;
  status: string;
  setStatus: (s: string) => void;
  statuses: [string, string][];
  region: string;
  setRegion: (s: string) => void;
  regions: string[];
  children?: ReactNode;
}) {
  return (
    <div className="toolbar">
      <input
        className="input search"
        aria-label="Search by name or ID"
        placeholder="Search by name or ID"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <select
        className="select filter"
        aria-label="Filter by status"
        value={status}
        onChange={(e) => setStatus(e.target.value)}
      >
        <option value="">All statuses</option>
        {statuses.map(([s, l]) => (
          <option key={s} value={s}>
            {l}
          </option>
        ))}
      </select>
      <select
        className="select filter"
        aria-label="Filter by region"
        value={region}
        onChange={(e) => setRegion(e.target.value)}
      >
        <option value="">All regions</option>
        {regions.map((r) => (
          <option key={r}>{r}</option>
        ))}
      </select>
      {(search || status || region) && (
        <Button
          view="outlined"
          size="l"
          onClick={() => {
            setSearch("");
            setStatus("");
            setRegion("");
          }}
        >
          Clear filters
        </Button>
      )}
      <span className="spacer" />
      {children}
    </div>
  );
}
export function ResourceName({
  name,
  id,
  to,
}: {
  name: string;
  id: string;
  to: string;
}) {
  return (
    <>
      <a className="strong" href={href(to)}>
        {name}
      </a>
      <div className="subline">
        <span className="truncate" title={id}>
          {id}
        </span>
        <CopyButton value={id} />
      </div>
    </>
  );
}
export function RowMenu({
  actions,
}: {
  actions: { label: string; action: () => void }[];
}) {
  return (
    <DropdownMenu
      switcher={
        <Button view="flat" size="l" aria-label="Resource actions">
          <Icon data={EllipsisVertical} size={16} />
        </Button>
      }
      items={actions.map((a) => ({ text: a.label, action: a.action }))}
    />
  );
}
export function EmptyList({
  resource,
  filtered,
  reset,
  action,
}: {
  resource: string;
  filtered?: boolean;
  reset?: () => void;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-symbol">
        <Icon data={Magnifier} size={34} />
      </div>
      <div>
        <h2>{filtered ? `No ${resource} found` : `No ${resource} yet`}</h2>
        <p>
          {filtered
            ? "Try a different search or clear your filters."
            : `Create your first ${resource === "jobs" ? "job" : "endpoint"} to get started.`}
        </p>
        {filtered ? (
          <Button view="outlined" size="l" onClick={reset}>
            Clear filters
          </Button>
        ) : (
          action
        )}
      </div>
    </div>
  );
}
export function ResourceHeader({
  name,
  id,
  status,
  created,
  region,
  actions,
}: {
  name: string;
  id: string;
  status: ReactNode;
  created?: string;
  region?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="resource-header">
      <h1>{name}</h1>
      <div className="resource-meta">
        {status}
        <span>Created: {fmt.dt(created)}</span>
        <span className="meta-id">
          ID: {id}
          <CopyButton value={id} />
        </span>
        {region && <span>{region}</span>}
      </div>
      <div className="actions">{actions}</div>
    </div>
  );
}
export function DetailCard({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="card">
      <div className="card__head">
        <h2>{title}</h2>
      </div>
      <div className="card__body">{children}</div>
    </section>
  );
}
