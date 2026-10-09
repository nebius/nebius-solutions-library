import { useState } from "react";
import { previewEnabled, settings } from "./api/client";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/ui";
import { SessionProvider } from "./components/Session";
import { DefinitionForm } from "./pages/DefinitionForm";
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
  const signedIn = previewEnabled || Boolean(settings.key);
  if (!signedIn)
    return (
      <Login
        onDone={() => {
          rerender();
          if (!location.hash || location.hash === "#/") navigate("/endpoints");
        }}
      />
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
        page = <DefinitionForm kind="endpoint" />;
        crumbs = [
          { label: "Endpoints", to: "/endpoints" },
          { label: "Create endpoint" },
        ];
      } else if (id) {
        page = (
          <EndpointDetail
            key={`${id}-${route.query.get("region")}`}
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
    case "keys":
      page = <Keys />;
      crumbs = [{ label: "API keys" }];
      break;
    case "settings":
      page = <Settings onChange={rerender} />;
      crumbs = [{ label: "Settings" }];
      break;
    case "models":
      page =
        id === "new" ? (
          <DefinitionForm kind="job" />
        ) : id ? (
          <DefinitionForm key={id} modelId={id} />
        ) : (
          <Models />
        );
      crumbs = [
        { label: "Saved definitions", to: "/models" },
        ...(id
          ? [{ label: id === "new" ? "Create definition" : "Edit definition" }]
          : []),
      ];
      break;
    default:
      page = <Endpoints />;
      crumbs = [{ label: "Endpoints" }];
  }
  const path = root ? "/" + root : "/endpoints";
  return (
    <ToastProvider>
      <SessionProvider key={settings.apiBase}>
        <Layout
          path={path}
          crumbs={crumbs}
          onLogout={() => {
            settings.key = "";
            rerender();
            navigate("/");
          }}
        >
          {page}
        </Layout>
      </SessionProvider>
    </ToastProvider>
  );
}
