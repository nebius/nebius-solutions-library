"""Server construction and health-gated tool registration."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .artifacts import ArtifactManager, ArtifactStore, LocalArtifactStore, S3ArtifactStore
from .catalog import load_catalog
from .fleet import FleetClient
from .schemas import FleetHealthResult
from .settings import Settings
from .tools import MODEL_TOOL_NAMES, PIPELINE_REQUIREMENTS, ToolHandlers


@dataclass
class Runtime:
    settings: Settings
    fleet: FleetClient
    artifacts: ArtifactManager

    async def close(self) -> None:
        await self.fleet.close()


def create_runtime(settings: Settings) -> Runtime:
    catalog = load_catalog(settings.catalog_file, allow_non_cluster_urls=settings.allow_non_cluster_urls)
    fleet = FleetClient(
        catalog,
        probe_timeout_seconds=settings.startup_probe_timeout_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_response_bytes=settings.max_response_bytes,
    )
    if settings.artifact_backend == "s3":
        assert settings.s3_bucket is not None
        assert settings.s3_endpoint_url is not None
        store: ArtifactStore = S3ArtifactStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            prefix=settings.s3_prefix,
            presign_ttl_seconds=settings.presign_ttl_seconds,
        )
    else:
        store = LocalArtifactStore(settings.artifact_directory)
    return Runtime(settings=settings, fleet=fleet, artifacts=ArtifactManager(store))


async def build_server(runtime: Runtime, *, startup_health: FleetHealthResult | None = None) -> MCPServer:
    health = startup_health or await runtime.fleet.probe_all()
    healthy_keys = {model.catalog_key for model in health.models if model.enabled and model.healthy}
    registered_tools = {key: tool_name for key, tool_name in MODEL_TOOL_NAMES.items() if key in healthy_keys}
    for pipeline, requirements in PIPELINE_REQUIREMENTS.items():
        if requirements <= healthy_keys:
            registered_tools[pipeline] = pipeline

    handlers = ToolHandlers(runtime.fleet, runtime.artifacts, health, registered_tools)
    server = MCPServer(
        "nebius-bionemo-mcp",
        title="Nebius BioNeMo MCP",
        description="Typed access to healthy BioNeMo NIMs in a customer-owned Nebius Kubernetes cluster.",
        instructions=(
            "Use list_models before selecting a model. Scientific artifacts are stored outside the MCP response; "
            "download them from the returned presigned URLs before those URLs expire."
        ),
        version=__version__,
    )

    server.add_tool(handlers.list_models, name="list_models")
    server.add_tool(handlers.fleet_health, name="fleet_health")
    for catalog_key, tool_name in MODEL_TOOL_NAMES.items():
        if catalog_key in healthy_keys:
            server.add_tool(getattr(handlers, tool_name), name=tool_name, meta={"nim_catalog_key": catalog_key})
    for pipeline, requirements in PIPELINE_REQUIREMENTS.items():
        if requirements <= healthy_keys:
            server.add_tool(getattr(handlers, pipeline), name=pipeline, meta={"requires": sorted(requirements)})

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def healthz(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "registered_model_tools": len(healthy_keys & set(MODEL_TOOL_NAMES)),
                "registered_tools": sorted(registered_tools.values()),
            }
        )

    @server.custom_route("/livez", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def livez(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return server
