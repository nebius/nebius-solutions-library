import { useState } from "react";
import { api, settings } from "./api/client";
import { Layout } from "./components/Layout";
import { ErrorBox, Loading, ToastProvider, useAsync } from "./components/ui";
import { Button } from "./components/controls";
import { SessionProvider } from "./components/Session";
import { Endpoints, EndpointDetail } from "./pages/Endpoints";
import { Jobs, JobDetail } from "./pages/Jobs";
import { Keys } from "./pages/Keys";
import { Login } from "./pages/Login";
import { ModelEditor, Models } from "./pages/Models";
import { NewJob } from "./pages/NewJob";
import { Settings } from "./pages/Settings";
import { navigate, useRoute } from "./router";

export default function App() {
  const route = useRoute();
  const [, bump] = useState(0);
  const rerender = () => bump((n) => n + 1);
  const signedIn = Boolean(settings.key);
  const session = useAsync(
    () => (signedIn ? api().keyInfo() : Promise.resolve(null)),
    [settings.key, settings.apiBase],
  );
  const fleet = useAsync(
    () => (signedIn ? api().fleetInfo() : Promise.resolve(null)),
    [settings.key, settings.apiBase],
  );
  const logout = () => {
    settings.key = "";
    rerender();
    navigate("/");
  };
  if (!signedIn)
    return (
      <Login
        onDone={() => {
          rerender();
          if (!location.hash || location.hash === "#/") navigate("/endpoints");
        }}
      />
    );
  if (!session.data)
    return (
      <div className="session-loading">
        {session.error ? (
          <>
            <ErrorBox msg={session.error} retry={session.reload} />
            <Button onClick={logout}>Use another key</Button>
          </>
        ) : (
          <Loading />
        )}
      </div>
    );

  const [root, id] = route.parts;
  let page: React.ReactNode;
  let crumbs: { label: string; to?: string }[] = [];
  switch (root) {
    case "jobs":
      if (id === "new") {
        page = (
          <NewJob
            key={route.query.get("model") ?? "new"}
            initialModel={route.query.get("model") ?? undefined}
          />
        );
        crumbs = [{ label: "Jobs", to: "/jobs" }, { label: "Create job" }];
      } else if (id) {
        page = <JobDetail key={id} id={id} />;
        crumbs = [{ label: "Jobs", to: "/jobs" }, { label: id }];
      } else {
        page = <Jobs />;
        crumbs = [{ label: "Jobs" }];
      }
      break;
    case "endpoints":
      if (id === "new") {
        page = <ModelEditor kind="endpoint" />;
        crumbs = [
          { label: "Endpoints", to: "/endpoints" },
          { label: "Create endpoint" },
        ];
      } else if (id) {
        page = (
          <EndpointDetail
            key={`${id}:${route.query.get("region")}`}
            id={id}
            region={route.query.get("region") ?? undefined}
          />
        );
        crumbs = [{ label: "Endpoints", to: "/endpoints" }, { label: id }];
      } else {
        page = <Endpoints />;
        crumbs = [{ label: "Endpoints" }];
      }
      break;
    case "models":
      if (id === "new") {
        page = <ModelEditor kind="job" />;
        crumbs = [
          { label: "Models", to: "/models" },
          { label: "Create model" },
        ];
      } else if (id && route.parts[2] === "edit") {
        page = <ModelEditor key={id} id={id} />;
        crumbs = [
          { label: "Models", to: "/models" },
          { label: id },
          { label: "Edit" },
        ];
      } else {
        page = <Models />;
        crumbs = [{ label: "Models" }];
      }
      break;
    case "keys":
      page = <Keys />;
      crumbs = [{ label: "API keys" }];
      break;
    case "settings":
      page = <Settings onChange={rerender} />;
      crumbs = [{ label: "Settings" }];
      break;
    default:
      page = <Endpoints />;
      crumbs = [{ label: "Endpoints" }];
  }
  const path = root ? "/" + root : "/endpoints";
  return (
    <ToastProvider>
      <SessionProvider value={{ key: session.data, fleet: fleet.data }}>
        <Layout path={path} crumbs={crumbs} onLogout={logout}>
          {page}
        </Layout>
      </SessionProvider>
    </ToastProvider>
  );
}
