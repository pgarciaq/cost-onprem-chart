#!/usr/bin/env python3
"""Generate PVC growth-pattern NISE data and upload to cost-onprem ingress."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import KeycloakConfig, JWTToken  # noqa: E402
from e2e_helpers import generate_nise_data, upload_with_retry  # noqa: E402
from utils import create_upload_package_from_files, get_route_url, get_secret_value  # noqa: E402

DEFAULT_CLUSTER_ID = "f7e97509-d48f-4b12-ba82-55550345c568"
PVC_TEMPLATE = "ocp_report_pvc_rightsizing.yml"


def _keycloak_config(namespace: str, keycloak_namespace: str, realm: str) -> KeycloakConfig:
    keycloak_url = get_route_url(keycloak_namespace, "keycloak")
    if not keycloak_url:
        raise RuntimeError(f"Keycloak route not found in namespace {keycloak_namespace}")

    client_id = "cost-management-operator"
    secret_patterns = [
        "keycloak-client-secret-cost-management-operator",
        "keycloak-client-secret-cost-management-service-account",
        f"credential-{client_id}",
        f"keycloak-client-{client_id}",
        f"{client_id}-secret",
    ]
    client_secret = None
    for secret_name in secret_patterns:
        client_secret = get_secret_value(keycloak_namespace, secret_name, "CLIENT_SECRET")
        if client_secret:
            break
    if not client_secret:
        raise RuntimeError(f"Keycloak client secret not found in {keycloak_namespace}")

    return KeycloakConfig(
        url=keycloak_url,
        client_id=client_id,
        client_secret=client_secret,
        realm=realm,
    )


def _ingress_upload_url(namespace: str, helm_release: str) -> str:
    gateway = get_route_url(namespace, f"{helm_release}-api")
    if not gateway:
        raise RuntimeError(f"API gateway route not found in namespace {namespace}")
    return f"{gateway.rstrip('/')}/api/ingress/v1/upload"


def main() -> int:
    nise_bin = os.environ.get("NISE_BIN_DIR", "/root/dev/nise/.venv/bin")
    if os.path.isdir(nise_bin):
        os.environ["PATH"] = f"{nise_bin}:{os.environ.get('PATH', '')}"

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-id", default=os.environ.get("CLUSTER_ID", DEFAULT_CLUSTER_ID))
    parser.add_argument("--namespace", default=os.environ.get("NAMESPACE", "cost-onprem"))
    parser.add_argument("--helm-release", default=os.environ.get("HELM_RELEASE_NAME", "cost-onprem"))
    parser.add_argument("--days", type=int, default=30, help="Report window length in days")
    parser.add_argument(
        "--keycloak-realm",
        default=os.environ.get("KEYCLOAK_REALM", "cost-management"),
    )
    parser.add_argument(
        "--keycloak-namespace",
        default=os.environ.get("KEYCLOAK_NAMESPACE", "keycloak"),
    )
    args = parser.parse_args()

    keycloak_config = _keycloak_config(args.namespace, args.keycloak_namespace, args.keycloak_realm)
    token_response = requests.post(
        keycloak_config.token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": keycloak_config.client_id,
            "client_secret": keycloak_config.client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        verify=False,
        timeout=30,
    )
    if token_response.status_code != 200:
        print(f"Failed to obtain JWT token: {token_response.status_code} {token_response.text}")
        return 1
    token_data = token_response.json()
    token = JWTToken(
        access_token=token_data["access_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=token_data.get("expires_in", 300)),
    )

    end_date = datetime.now(timezone.utc) - timedelta(days=1)
    start_date = end_date - timedelta(days=args.days - 1)

    temp_dir = tempfile.mkdtemp(prefix="pvc-growth-upload-")
    print(f"Generating NISE data for {args.cluster_id} ({start_date.date()} → {end_date.date()})")
    files = generate_nise_data(
        cluster_id=args.cluster_id,
        start_date=start_date,
        end_date=end_date,
        output_dir=temp_dir,
        include_ros=True,
        iqe_template=PVC_TEMPLATE,
    )

    storage_files = files.get("storage_usage_files") or []
    pod_files = files.get("pod_usage_files") or []
    ros_files = files.get("ros_usage_files") or []
    if not storage_files:
        raise RuntimeError("NISE did not generate ocp_storage_usage CSV files")

    package_path = create_upload_package_from_files(
        pod_usage_files=pod_files,
        ros_usage_files=ros_files,
        storage_usage_files=storage_files,
        cluster_id=args.cluster_id,
        start_date=start_date,
        end_date=end_date,
        node_label_files=files.get("node_label_files"),
        namespace_label_files=files.get("namespace_label_files"),
    )

    upload_url = _ingress_upload_url(args.namespace, args.helm_release)
    session = requests.Session()
    session.verify = False

    print(f"Uploading package to {upload_url}")
    response = upload_with_retry(
        session,
        upload_url,
        package_path,
        token.authorization_header,
        timeout=300,
    )
    if response.status_code not in (200, 201, 202):
        print(f"Upload failed: {response.status_code} {response.text}")
        return 1

    print("Upload accepted; waiting 120s for ROS PVC digest processing")
    time.sleep(120)
    print("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
