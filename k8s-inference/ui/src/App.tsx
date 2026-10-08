import { useState } from "react";
import { settings } from "./api/client";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/ui";
import { Endpoints, EndpointDetail } from "./pages/Endpoints";
import { Jobs, JobDetail } from "./pages/Jobs";
import { Keys } from "./pages/Keys";
import { Login } from "./pages/Login";
import { Models } from "./pages/Models";
import { NewJob } from "./pages/NewJob";
import { Settings } from "./pages/Settings";
import { navigate, useRoute } from "./router";

export default function App() {
  const route = useRoute();
  const [, bump] = useState(0);
  const rerender = () => bump((n) => n + 1);
  const signedIn = Boolean(settings.key);
  if (!signedIn) return <Login onDone={() => { rerender(); if (!location.hash || location.hash === "#/") navigate("/models"); }} />;

  const [root, id] = route.parts;
  let page: React.ReactNode; let crumbs: { label: string; to?: string }[] = [];
  switch (root) {
    case "jobs":
      if (id === "new") { page = <NewJob initialModel={route.query.get("model") ?? undefined} />; crumbs = [{ label: "Jobs", to: "/jobs" }, { label: "New job" }]; }
      else if (id) { page = <JobDetail id={id} />; crumbs = [{ label: "Jobs", to: "/jobs" }, { label: id }]; }
      else { page = <Jobs />; crumbs = [{ label: "Jobs" }]; }
      break;
    case "endpoints":
      if (id) { page = <EndpointDetail id={id} />; crumbs = [{ label: "Endpoints", to: "/endpoints" }, { label: id }]; }
      else { page = <Endpoints />; crumbs = [{ label: "Endpoints" }]; }
      break;
    case "keys": page = <Keys />; crumbs = [{ label: "API keys" }]; break;
    case "settings": page = <Settings onChange={rerender} />; crumbs = [{ label: "Settings" }]; break;
    default: page = <Models />; crumbs = [{ label: "Models" }];
  }
  const path = root ? "/" + root : "/models";
  return (
    <ToastProvider>
      <Layout path={path} crumbs={crumbs} onLogout={() => { settings.key = ""; rerender(); navigate("/"); }}>{page}</Layout>
    </ToastProvider>
  );
}
