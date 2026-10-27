"""Initialize service imports and fixture configuration before collecting any API test module."""

import os
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
ROOT = API_DIR.parents[1]
sys.path.insert(0, str(API_DIR))
os.environ.update(
    CATALOG_DIRS=f"{ROOT}/catalog/models:{API_DIR}/tests/catalog",
    PUBLIC_API_URL="https://api.example",
    REGION="eu-north1",
    ENDPOINT_DOMAIN="203.0.113.10.sslip.io",
)
