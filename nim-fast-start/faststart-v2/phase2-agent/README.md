# Generic one-shot Dynamo restore worker

This directory contains the complete source overlay, patch series, and
provenance needed to reproduce the one-shot worker used by the OpenFold2 native
restore path. Materialization and source verification are offline and always
start from the immutable Dynamo Git object
`f7f37be174d252590c4b56e25ff4262dd82466fd`.

The final worker is model-independent. Model, Pod, container, checkpoint,
artifact, and tool-bundle identities are supplied through its explicit
argument contract rather than compiled OpenFold2 constants.

## Source composition

`materialize_overlay.sh` archives the pinned Git object, applies these patches
in order, and then installs `overlay/`:

| Input | SHA-256 |
|---|---|
| `public-aio-toolchain.patch` | `de10a0609115589efb137186544c9d90a0fcc03616ef959376139c65ae9615bc` |
| `core-hardening.patch` | `260c1d9a7f192b8c0b25c924ab26b43a95ad599d38d3f367383e3e984aecfd11` |
| `compliance-closure.patch` | `32493e5da929a993976148124699f8abf99240c4ba486e6a01fda453c682ae68` |
| `source-archive-fix.patch` | `5dd45d97596bbdf068f33f0532fc71da1754c9eeb7d91f0abe547ac29f30bf0e` |
| `portable-glibc35-toolchain.patch` | `62d584ea83770c62d090bbbc15f265a6fadda064ec8c6e6a4fd0abe8328780b9` |
| `buffered-criu-io.patch` | `6a59dcb524abd0d9596697efd28afb3bd9d4e543b193f687178494452cecac15` |
| `jammy-compliance-tomli.patch` | `6fa58bcdf97c54f8ecd75e2a685150bd1ae5cede71283eb8fc9aa88bacf87156` |
| normalized overlay tree | `5363391ab0faed88612308fa1d7a1a9664d464fc9ba440973e89e8bc4ca2a0ed` |
| `Dockerfile.restore-worker` | `f0fb42c68ad7bf8dd39e27a5e070a1613953e7ec2cac0f19027cbea63a509570` |

The patch series pins the compiler, CUDA helper, CRIU builder, runtime, CRIU
revision, cuda-checkpoint revision, Go dependency graph, SBOM inputs, and source
archive behavior. The portable-toolchain patch builds CRIU and the injected
shared-library closure on pinned Ubuntu 22.04 while retaining the already
validated Ubuntu 24.04 CUDA helper build. Its helper SHA-256 remains pinned to
`0e44e9067d71411c775cd3b21fa0df806613504c8c699bd1944e3e7c10989298`.
The overlay adds the one-shot worker, focused tests, namespace-FD pinning, and
the all-ELF compatibility verifier.
The buffered-I/O patch preserves direct and writeback behavior and adds an
explicit `buffered` mode that leaves CRIU's image-I/O protobuf field unset.
This is the retained-page-cache path selected by the ProteinMPNN benchmark.
The Jammy compliance patch supplies the `tomllib` compatibility module needed
by the policy gate under Python 3.10; it is build-stage-only.

The exact component and image metadata is in `provenance.json`.

## Worker contract

The worker accepts one `restore` operation and exits after emitting one JSON
receipt. Before restore, it binds and revalidates:

- target namespace, Pod name and UID;
- container name, full containerd ID, immutable image ID, cgroup, Pod IP, and
  node;
- canonical API-defaulted PodSpec SHA-256 and run ID;
- checkpoint ID, artifact version and artifact-manifest SHA-256; and
- the SHA-256 of the baked `/snapshot-binaries.manifest`.

It injects the baked CRIU/CUDA tool bundle into the unmodified target mount
namespace. The OpenFold2 target therefore remains the exact NVIDIA image
digest; no derivative model image is required.

Before either runnable agent target can be built, a dedicated stage scans
every regular ELF in `/snapshot-binaries` with `readelf` and rejects any
required GLIBC version newer than 2.35. The resulting deterministic receipt is
copied to `/snapshot-binaries.glibc-compatibility`, making the gate a build
dependency rather than an external post-build observation. Rootfs extraction
uses the target image's `/bin/tar` with `LD_LIBRARY_PATH` removed only for that
child process, so the target's tar cannot accidentally load injected libraries.

## Reproduce the source tree

```console
./materialize_overlay.sh \
  /path/to/dynamo-at-f7f37be \
  /tmp/dynamo-one-shot-worker
```

The destination must not exist. The script reads only the pinned Git object,
never untracked or dirty files from the source checkout.

The source object and base images are immutable inputs. Ubuntu package
repositories are not date-snapshotted in the upstream Dockerfile, so a clean
release build must still record and review its resulting image, bundle, and
compatibility-receipt digests. The pinned helper hash and GLIBC gate fail
closed if dependency drift changes those compatibility-critical outputs.

## Build and test

From the materialized tree:

```console
cd /tmp/dynamo-one-shot-worker/deploy/snapshot

docker build --progress=plain --platform linux/amd64 \
  --target tester \
  --build-context operator=../operator \
  --build-context compliance=../../container/compliance .

docker build --progress=plain --platform linux/amd64 \
  --target agent \
  --build-context operator=../operator \
  --build-context compliance=../../container/compliance \
  --build-arg ENABLE_SOURCE_ARCHIVAL=true \
  --tag archvteams-2407-snapshot-agent:f7f37be-criu91d552 .
```

`Dockerfile.restore-worker` is an optional binary-only export target. The
runnable worker image is the patched upstream `agent` target because that
image also carries the matched CRIU, CUDA helper, plugins, libraries, legal
inventory, and `/snapshot-binaries.manifest`.

The image used for the successful OpenFold2 runs was:

```text
registry.example.invalid/faststart/snapshot-agent@sha256:31e1dacd18b99aec1ab7e8ec8c933f260c9dcec687938b40c44c61274f930d86
```

Its local OCI image ID was
`sha256:dc7806d8440c967fcf1efd713ec0a9bcb6496a7741f97fe65e28a66d66a91314`;
the worker binary SHA-256 was
`8873c760a1760c0dc3b3e331c7aedf879e7737ad11aed7f028513f057bbbee6e`;
and its tool-bundle manifest SHA-256 was
`04214d6f4d72fa59d5daa6168d8c63024b6ebe34c4876394e47e80319ef00c39`.

The pre-integration Jammy validation image was:

```text
registry.example.invalid/faststart/snapshot-agent@sha256:b3801ee9272b9032da7e46c4cea8f6a20ac1bf2f3c7fa25c604b4ad15cd761a3
```

It demonstrated the compatibility strategy against the exact ProteinMPNN
image: all 34 bundled ELFs were at or below GLIBC 2.35 and the bundle manifest
SHA-256 was
`fbee62d23781a1298fe7b1173ce39e50edd1ea451476ffb147319d92fa88e44c`.
That image was built from the task-local `task-public-agent` target, rebuilt
the CUDA helper on Jammy, and used an external ELF audit. It is validation
evidence, not byte provenance for the integrated patch above. A new immutable
`agent` image must be built before release.

The exact eight-patch materialization, including buffered I/O, the in-build
GLIBC gate, and the Jammy-built host-side `ns-bind-mount`, produced this newer
performance-validation image:

```text
registry.example.invalid/faststart/snapshot-agent@sha256:d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28
```

Its restore-worker SHA-256 is
`941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651`,
its tool-manifest SHA-256 is
`c0d638100c03fa35973e82859d15b9c8dd1bcbf0fe9cb185b58cc21fae7ead1e`,
and its GLIBC receipt SHA-256 is
`f7af5b214cb963c4cf64910dfafe16987f0c5ec886af5d0e5d7aab5b634f6950`.
The receipt covers 34 ELFs/files and caps required GLIBC at 2.35. This image
also carries host-side `ns-bind-mount` SHA-256
`4fa66737c2963cb0fac4305a058fbd39e95819d10b8e1b14776c3a212087b2d5`,
built and executed on GLIBC 2.35.
This image
uses `task-public-agent`; the full compliance `agent` build remains gated on a
captured baseline SBOM for the exact Jammy CUDA runtime base.

## Offline verification

```console
./verify_offline.sh /path/to/dynamo-at-f7f37be
```

The verifier checks the source object, eight patch hashes, materialized files,
dependency graph, Jammy and retained-helper pins, the GLIBC gate's comparison
self-test, provenance JSON, and normalized overlay hash. It makes no network,
registry, cloud, or Kubernetes request. The exact integrated materialization
passed the full upstream Go suite offline through the cached tester image, and
both runnable Docker targets passed static build checks. Building the integrated
`agent` image and compliance target remains a release gate.
