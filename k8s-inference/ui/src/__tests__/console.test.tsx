import { useState, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@gravity-ui/uikit";
import { api, settings } from "../api/client";
import type { ApiKey, Endpoint, Model, ModelSpec } from "../api/types";
import { SessionProvider } from "../components/Session";
import { Modal, ToastProvider, Field } from "../components/ui";
import { Input, Button } from "../components/controls";
import { Models, ModelEditor, prepareSpec } from "../pages/Models";
import {
  Endpoints,
  EndpointDetail,
  sampleBody,
  curlFor,
} from "../pages/Endpoints";
import { NewJob, buildJobRequest, defaultsFor } from "../pages/NewJob";
import { Keys } from "../pages/Keys";
import App from "../App";

const principal: ApiKey = {
  id: "test",
  alias: "test",
  key_preview: "sk-…test",
  tenant: "test",
  role: "admin",
  spend: 0,
  budget: null,
  models: [],
  created_at: "2026-01-01",
  status: "active",
};
const container: Model = {
  id: "container-run",
  name: "Container run",
  gpu: "Configurable",
  price: "metered",
  default_mode: "run",
  modes: ["run"],
  regions: [{ region: "test-west", status: "ready" }],
  parameters: [
    { name: "image", type: "string", required: true },
    { name: "command", type: "string", required: true },
    { name: "cpu", type: "string", default: "8" },
    { name: "gpus", type: "number", default: 1 },
  ],
};
const endpoint: Endpoint = {
  id: "chat",
  name: "Chat model",
  model: "chat",
  region: "test-west",
  url: "https://api.example/v1/models/chat:invoke",
  status: "scaled-to-zero",
  replicas_ready: 0,
  min_replicas: 0,
  max_replicas: 2,
  scale_to_zero_after_s: 120,
  target_concurrency: 4,
  managed_by: "api",
  protocol: "openai",
};
function show(children: ReactNode, role: "user" | "admin" = "admin") {
  return render(
    <ThemeProvider theme="light">
      <SessionProvider
        value={{
          key: { ...principal, role },
          fleet: {
            ok: true,
            region: "test-west",
            regions: ["test-west", "test-east"],
            fleet_manager: true,
            gpu_classes: ["h100"],
          },
        }}
      >
        <ToastProvider>{children}</ToastProvider>
      </SessionProvider>
    </ThemeProvider>,
  );
}
beforeEach(() => {
  localStorage.clear();
  location.hash = "";
});

describe("model configuration", () => {
  it("preserves existing advanced fields, argument boundaries and environment values during edits", () => {
    const spec: ModelSpec = {
      id: "my-model",
      kind: "endpoint",
      image: "image:tag",
      command: ["python", "server.py"],
      port: 8000,
      gpu: { count: 1 },
      scaling: { min: 0, max: 2, target: 4 },
      weights: { path: "s3://bucket/weights", mount_path: "/weights" },
      resources: { cpu: "8", memory: "32Gi" },
      path: "/generate",
    };
    const payload = prepareSpec(
      spec,
      "--message\nhello world",
      "URL=https://example.test/?a=b=c\nPAD= spaced ",
      "h100",
    );
    expect(payload).toMatchObject({
      command: ["python", "server.py"],
      args: ["--message", "hello world"],
      env: { URL: "https://example.test/?a=b=c", PAD: " spaced " },
      weights: spec.weights,
      resources: spec.resources,
      path: "/generate",
    });
    expect(() =>
      prepareSpec({ ...spec, scaling: { min: 2, max: 1 } }, "", "", ""),
    ).toThrow("Scaling");
    expect(() => prepareSpec(spec, "", "INVALID ENV=value", "")).toThrow(
      "NAME=value",
    );
  });
  it("loads an existing sparse spec with its CPU-only defaults and saves through the update API", async () => {
    const spec: ModelSpec = {
      id: "cpu-echo",
      kind: "endpoint",
      image: "echo:1",
      weights: { path: "s3://bucket/weights" },
    };
    vi.spyOn(api(), "getModel").mockResolvedValue({
      ...container,
      id: spec.id,
      managed_by: "api",
      spec,
    });
    const update = vi
      .spyOn(api(), "updateModel")
      .mockResolvedValue({ id: spec.id, model: container });
    show(<ModelEditor id={spec.id} />);
    await screen.findByLabelText("Container image");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1]).toMatchObject({
      id: spec.id,
      gpu: { count: 0 },
      port: 8080,
      protocol: "http",
      weights: spec.weights,
    });
  });
  it("withholds model creation and editing from member keys", async () => {
    vi.spyOn(api(), "listModels").mockResolvedValue([
      {
        ...container,
        managed_by: "api",
        spec: { id: container.id, kind: "job", image: "image:tag" },
      },
    ]);
    show(<Models />, "user");
    await screen.findByText("Container run");
    expect(
      screen.queryByRole("link", { name: "Create model" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Edit" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete" }),
    ).not.toBeInTheDocument();
  });
});
describe("job submission", () => {
  it("uses container-run and only its declared inputs for a custom image", async () => {
    vi.spyOn(api(), "listModels").mockResolvedValue([container]);
    const invoke = vi
      .spyOn(api(), "invoke")
      .mockResolvedValue({
        id: "job-test",
        model: container.id,
        mode: "run",
        region: "test-west",
        status: "QUEUED",
        created_at: "2026-01-01",
      });
    show(<NewJob initialModel="container-run" />);
    await userEvent.type(await screen.findByLabelText("Job name"), "my-job");
    await userEvent.type(
      await screen.findByLabelText("image *"),
      "registry.example/container:tag",
    );
    await userEvent.type(
      screen.getByLabelText("command *"),
      "python train.py --name 'hello world'",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(invoke).toHaveBeenCalled());
    expect(invoke.mock.calls[0]).toEqual([
      "container-run",
      {
        name: "my-job",
        mode: "run",
        input: {
          image: "registry.example/container:tag",
          command: "python train.py --name 'hello world'",
          cpu: "8",
          gpus: 1,
        },
        region: undefined,
        priority: "normal",
        timeout_s: 7200,
      },
    ]);
    expect(location.hash).toBe("#/jobs/job-test");
  });
  it("requires file inputs and validates placement and timeout before submitting", () => {
    const fileModel: Model = {
      ...container,
      parameters: [
        { name: "dataset", label: "Dataset", type: "file", required: true },
      ],
    };
    const options = {
      name: "job",
      region: "test-west",
      priority: "normal",
      timeoutH: 1,
      mode: "run" as const,
    };
    expect(() =>
      buildJobRequest(fileModel, defaultsFor(fileModel), options),
    ).toThrow("Dataset is required");
    expect(
      buildJobRequest(fileModel, {}, options, { dataset: "s3://test/dataset" })
        .input,
    ).toEqual({ dataset: "s3://test/dataset" });
    expect(() =>
      buildJobRequest(container, {}, { ...options, timeoutH: 0 }),
    ).toThrow("Timeout");
    expect(() =>
      buildJobRequest(container, {}, { ...options, region: "unknown" }),
    ).toThrow("region");
  });
  it("shows a recoverable catalog error", async () => {
    vi.spyOn(api(), "listModels").mockRejectedValue(
      new Error("API is unavailable"),
    );
    show(<NewJob />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "API is unavailable",
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
  it("uploads a required file before invocation and sends its URI", async () => {
    const model: Model = {
      ...container,
      id: "file-job",
      parameters: [
        { name: "dataset", label: "Dataset", type: "file", required: true },
      ],
    };
    vi.spyOn(api(), "listModels").mockResolvedValue([model]);
    const upload = vi
      .spyOn(api(), "uploadArtifact")
      .mockResolvedValue("s3://test/dataset");
    const invoke = vi
      .spyOn(api(), "invoke")
      .mockResolvedValue({
        id: "file-result",
        model: model.id,
        mode: "run",
        region: "test-west",
        status: "QUEUED",
        created_at: "2026-01-01",
      });
    show(<NewJob initialModel={model.id} />);
    await userEvent.type(await screen.findByLabelText("Job name"), "file-job");
    await userEvent.click(screen.getByRole("button", { name: "Create job" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Dataset is required",
    );
    expect(invoke).not.toHaveBeenCalled();
    const file = new File(["synthetic data"], "data.txt", {
      type: "text/plain",
    });
    await userEvent.upload(screen.getByLabelText("Dataset *"), file);
    await userEvent.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(invoke).toHaveBeenCalled());
    expect(upload).toHaveBeenCalledWith(file, undefined);
    expect(invoke.mock.calls[0][1].input).toEqual({
      dataset: "s3://test/dataset",
    });
  });
});
describe("regional endpoints", () => {
  it("keeps same-name endpoints distinct and searchable", async () => {
    vi.spyOn(api(), "listEndpoints").mockResolvedValue([
      endpoint,
      { ...endpoint, region: "test-east", name: "East model" },
    ]);
    show(<Endpoints />);
    const west = await screen.findByRole("link", { name: "Chat model" });
    expect(west).toHaveAttribute("href", "#/endpoints/chat?region=test-west");
    expect(screen.getByRole("link", { name: "East model" })).toHaveAttribute(
      "href",
      "#/endpoints/chat?region=test-east",
    );
    await userEvent.type(
      screen.getByRole("searchbox", { name: "Search endpoints" }),
      "east",
    );
    expect(
      screen.queryByRole("link", { name: "Chat model" }),
    ).not.toBeInTheDocument();
  });
  it("passes the region on detail reads and does not invent telemetry", async () => {
    const get = vi.spyOn(api(), "getEndpoint").mockResolvedValue(endpoint);
    show(<EndpointDetail id="chat" region="test-west" />);
    await screen.findByText("Endpoint information");
    expect(get).toHaveBeenCalledWith("chat", "test-west");
    expect(screen.queryByText("In-flight requests")).not.toBeInTheDocument();
    expect(screen.queryByText("Last cold start")).not.toBeInTheDocument();
    expect(
      screen.getByText("Monitoring is not configured for this console."),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Try it" }));
    expect(screen.getByLabelText("Request body (JSON)")).toHaveValue(
      JSON.stringify(sampleBody(endpoint), null, 2),
    );
  });
  it("generates a regional, shell-quoted request example", () => {
    const curl = curlFor(endpoint, '{"text":"it\'s ready"}');
    expect(curl).toContain('"region":"test-west"');
    expect(curl).toContain("it'\\''s ready");
    expect(curl).toContain("$API_KEY");
  });
});
it("members see their own key without making an administrator-only list request", async () => {
  const list = vi.spyOn(api(), "listKeys");
  vi.spyOn(api(), "keyInfo").mockResolvedValue({ ...principal, role: "user" });
  vi.spyOn(api(), "listModels").mockResolvedValue([]);
  show(<Keys />, "user");
  await screen.findByText(/Unlimited/);
  expect(list).not.toHaveBeenCalled();
  expect(
    screen.queryByRole("button", { name: "Create API key" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Revoke" }),
  ).not.toBeInTheDocument();
});
it("validates the saved session, starts on Endpoints and clears the key on sign out", async () => {
  settings.key = "synthetic-test-key";
  vi.spyOn(api(), "keyInfo").mockResolvedValue(principal);
  vi.spyOn(api(), "fleetInfo").mockResolvedValue({
    ok: true,
    region: "test-west",
    regions: ["test-west"],
    fleet_manager: true,
  });
  vi.spyOn(api(), "listEndpoints").mockResolvedValue([]);
  render(
    <ThemeProvider theme="light">
      <App />
    </ThemeProvider>,
  );
  await screen.findByRole("heading", { name: "Endpoints" });
  await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
  await screen.findByRole("heading", { name: "Sign in" });
  expect(settings.key).toBe("");
});
it("the skip link focuses content without changing the hash route", async () => {
  settings.key = "synthetic-test-key";
  location.hash = "/jobs";
  vi.spyOn(api(), "keyInfo").mockResolvedValue(principal);
  vi.spyOn(api(), "fleetInfo").mockResolvedValue({
    ok: true,
    region: "test-west",
    regions: ["test-west"],
    fleet_manager: true,
  });
  vi.spyOn(api(), "listOperations").mockResolvedValue([]);
  render(
    <ThemeProvider theme="light">
      <App />
    </ThemeProvider>,
  );
  await screen.findByRole("heading", { name: "Jobs" });
  await userEvent.click(screen.getByRole("link", { name: "Skip to content" }));
  expect(location.hash).toBe("#/jobs");
  expect(screen.getByRole("main")).toHaveFocus();
});
it("labels controls, traps dialog focus, closes on Escape and restores focus", async () => {
  // jsdom has no layout. Give visible controls rectangles so Floating UI can detect tabbable nodes.
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockImplementation(
    function (this: HTMLElement) {
      return (this.hidden || getComputedStyle(this).display === "none"
        ? []
        : [new DOMRect(0, 0, 100, 20)]) as unknown as DOMRectList;
    },
  );
  function Example() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <Button onClick={() => setOpen(true)}>Open dialog</Button>
        {open && (
          <Modal title="Edit settings" onClose={() => setOpen(false)}>
            <Field label="Example" help="Help text">
              <Input value="" onChange={() => {}} />
            </Field>
            <Button onClick={() => setOpen(false)}>Save</Button>
          </Modal>
        )}
      </>
    );
  }
  show(<Example />);
  const trigger = screen.getByRole("button", { name: "Open dialog" });
  await userEvent.click(trigger);
  const dialog = await screen.findByRole("dialog", { name: "Edit settings" });
  await waitFor(() =>
    expect(dialog).toContainElement(document.activeElement as HTMLElement),
  );
  const input = screen.getByLabelText("Example");
  expect(input).toHaveAccessibleDescription("Help text");
  input.focus();
  await userEvent.tab();
  await userEvent.tab();
  // Focus guards are siblings of the dialog content inside Gravity's modal overlay.
  expect(dialog.closest(".g-modal")).toContainElement(
    document.activeElement as HTMLElement,
  );
  expect(trigger).not.toHaveFocus();
  await userEvent.keyboard("{Escape}");
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  await waitFor(() => expect(trigger).toHaveFocus());
});
