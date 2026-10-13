"""The Nebius CLI from inside the dispatcher image, with the ops image's credential convention.

The platform stage mounts one service-account key per GPU region at /etc/nebius/<region>.json (Secret
kueue-system/price-feed-nebius-sa, stack/platform/secrets.tf: the `ops_credentials` of every worker project).
`login()` turns every file into a CLI profile `sa-<region>` exactly as `ops-login.sh` does in the ops image
(the key file is re-read on every call, tokens are minted per call, nothing expires), so a caller names the
region whose project it acts in and never handles tokens. HOME must be writable (the manifests set /tmp).
"""

import glob
import logging
import os
import subprocess

log = logging.getLogger("nebius-cli")
KEYS_DIR = os.environ.get("NEBIUS_KEYS_DIR", "/etc/nebius")
ENDPOINT = os.environ.get("NEBIUS_ENDPOINT", "api.nebius.cloud")
_profiles: dict[str, str] = {}  # region name -> profile name, filled by login()


def login(keys_dir: str = KEYS_DIR) -> dict[str, str]:
    """Create (or re-create) the `sa-<region>` profile for every key file; returns {region: profile}. A file whose
    profile cannot be created is logged and skipped: the callers then act without that region's credentials."""
    _profiles.clear()
    for f in sorted(glob.glob(os.path.join(keys_dir, "*.json"))):
        region = os.path.basename(f)[: -len(".json")]
        profile = f"sa-{region}"
        try:
            subprocess.run(
                ["nebius", "profile", "create", profile, "--endpoint", ENDPOINT, "--service-account-file-path", f],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            _profiles[region] = profile
        except Exception as e:  # noqa: BLE001 - a bad key must not stop the dispatcher
            log.warning("profile %s from %s failed: %s", profile, f, getattr(e, "stderr", None) or e)
    if _profiles:
        log.info("nebius profiles: %s", sorted(_profiles))
    return dict(_profiles)


def profile(region: str | None = None) -> str | None:
    """The profile of a region (None when no key is mounted for it), or any profile when no region is given
    (prices are tenant-wide: any project's identity may ask for them)."""
    if region:
        return _profiles.get(region)
    return next(iter(_profiles.values()), None)


def run(args: list[str], region: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """`nebius <args>` as the region's profile (`--profile`, when one exists); raises on a non-zero exit."""
    p = profile(region)
    cmd = ["nebius"] + (["--profile", p] if p else []) + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
