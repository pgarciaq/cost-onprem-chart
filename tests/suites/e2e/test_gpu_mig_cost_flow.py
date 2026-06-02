"""
Extended E2E: MIG GPU cost data upload through Koku and GPU report API verification.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data (ocp_report_gpu_mig.yml) with ocp_gpu_usage.csv (mig_instance_id).
  3. Upload via ingress; wait for pod and GPU summary tables in Koku.
  4. GET /reports/openshift/gpu/ — assert MIG instance data for the cluster.
  5. GET /reports/openshift/gpu/mig_profiles/ — assert profile breakdown.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k gpu_mig_cost_flow
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
import requests

from conftest import obtain_jwt_token
from e2e_helpers import (
    ensure_nise_available,
    generate_nise_data,
    get_koku_api_url,
    register_source,
    upload_with_retry,
    wait_for_gpu_summary_tables,
    wait_for_provider,
    wait_for_summary_tables,
)
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    get_pod_by_label,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_gpu_mig.yml"
_EXPECTED_MIG_NODE = "e2e-mig-node-h100"
_EXPECTED_MIG_MODEL = "NVIDIA H100 80GB HBM3"


def _gpu_report_url(koku_api_url: str, path_suffix: str = "") -> str:
    base = f"{koku_api_url.rstrip('/')}/reports/openshift/gpu/"
    if path_suffix:
        return f"{base}{path_suffix.lstrip('/')}"
    return base


def _time_scope_params() -> dict[str, str]:
    return {
        "filter[time_scope_value]": "-1",
        "filter[time_scope_units]": "month",
    }


def _walk_values(obj: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj[key])
        for value in obj.values():
            found.extend(_walk_values(value, key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_values(item, key))
    return found


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestGpuMigCostExtendedFlow:
    """Upload MIG GPU cost CSVs and verify Koku GPU and mig_profiles report APIs."""

    @pytest.fixture(scope="class")
    def gpu_mig_e2e_cluster_id(self) -> str:
        return f"e2e-gpu-mig-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def gpu_mig_e2e_context(
        self,
        cluster_config,
        keycloak_config,
        gpu_mig_e2e_cluster_id: str,
        ingress_url: str,
    ) -> dict[str, Any]:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for GPU MIG cost E2E")

        ingress_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=ingress"
        )
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not ingress_pod or not db_pod:
            pytest.skip("Ingress or database pod not found")

        admin_identity = create_rh_identity_header(_UPLOAD_ORG_ID)
        koku_url = get_koku_api_url(
            cluster_config.helm_release_name, cluster_config.namespace
        )

        reg = register_source(
            namespace=cluster_config.namespace,
            pod=ingress_pod,
            api_url=koku_url,
            rh_identity_header=admin_identity,
            cluster_id=gpu_mig_e2e_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-gpu-mig-{gpu_mig_e2e_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, gpu_mig_e2e_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {gpu_mig_e2e_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-gpu-mig-")

        files = generate_nise_data(
            cluster_id=gpu_mig_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=False,
            iqe_template=_NISE_TEMPLATE,
        )
        if not files.get("pod_usage_files"):
            pytest.fail("NISE did not generate pod_usage files for GPU MIG E2E")
        if not files.get("gpu_usage_files"):
            pytest.fail(
                "NISE did not generate ocp_gpu_usage files; "
                "ensure nise supports MIG (mig_instances) in static YAML"
            )

        package_path = create_upload_package_from_files(
            pod_usage_files=files["pod_usage_files"],
            ros_usage_files=files["pod_usage_files"],
            cluster_id=gpu_mig_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            node_label_files=files.get("node_label_files") or None,
            namespace_label_files=files.get("namespace_label_files") or None,
            gpu_usage_files=files["gpu_usage_files"],
        )

        upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
        upload_session = requests.Session()
        upload_session.verify = False
        token = obtain_jwt_token(keycloak_config)
        response = upload_with_retry(
            upload_session, upload_url, package_path, token.authorization_header
        )
        assert response.status_code in (200, 201, 202), response.text

        schema = wait_for_summary_tables(
            cluster_config.namespace,
            db_pod,
            gpu_mig_e2e_cluster_id,
            timeout=420,
        )
        if not schema:
            pytest.fail("Summary tables not populated after GPU MIG E2E upload")

        if not wait_for_gpu_summary_tables(
            cluster_config.namespace,
            db_pod,
            gpu_mig_e2e_cluster_id,
            schema,
            timeout=600,
        ):
            pytest.fail(
                "reporting_ocp_gpu_summary_p has no MIG rows after upload; "
                "check listener GPU processing and migration 0344 (mig_instance_id)"
            )

        return {
            "cluster_id": gpu_mig_e2e_cluster_id,
            "schema": schema,
            "node": _EXPECTED_MIG_NODE,
            "gpu_model": _EXPECTED_MIG_MODEL,
        }

    def test_gpu_report_returns_mig_instances(
        self,
        e2e_pod_session: requests.Session,
        koku_api_url: str,
        gpu_mig_e2e_context: dict[str, Any],
    ):
        params = {
            **_time_scope_params(),
            "filter[cluster]": gpu_mig_e2e_context["cluster_id"],
            "group_by[node]": "*",
            "group_by[project]": "*",
        }
        url = _gpu_report_url(koku_api_url) + "?" + urlencode(params, doseq=True)
        resp = e2e_pod_session.get(url, timeout=120)
        if resp.status_code == 403:
            pytest.skip("GPU report API disabled (Unleash flag off for tenant)")
        assert resp.status_code == 200, resp.text

        mig_ids = _walk_values(resp.json(), "mig_instance_id")
        non_empty = [mid for mid in mig_ids if mid]
        assert non_empty, (
            "Expected mig_instance_id in GPU report response; "
            f"got keys sample: {list(resp.json().keys())}"
        )

        nodes = _walk_values(resp.json(), "node")
        assert _EXPECTED_MIG_NODE in nodes, (
            f"Expected node {_EXPECTED_MIG_NODE} in GPU report; nodes={nodes[:5]}"
        )

    def test_mig_profiles_report_returns_profile_breakdown(
        self,
        e2e_pod_session: requests.Session,
        koku_api_url: str,
        gpu_mig_e2e_context: dict[str, Any],
    ):
        params = {
            **_time_scope_params(),
            "filter[cluster]": gpu_mig_e2e_context["cluster_id"],
            "filter[gpu_vendor]": "nvidia",
            "filter[gpu_model]": gpu_mig_e2e_context["gpu_model"],
            "filter[node]": gpu_mig_e2e_context["node"],
            "group_by[mig_profile]": "*",
        }
        url = (
            _gpu_report_url(koku_api_url, "mig_profiles/")
            + "?"
            + urlencode(params, doseq=True)
        )
        resp = e2e_pod_session.get(url, timeout=120)
        if resp.status_code == 403:
            pytest.skip("MIG profiles API disabled (Unleash flag off for tenant)")
        assert resp.status_code == 200, resp.text

        body = resp.json()
        assert "data" in body, body
        profiles = _walk_values(body, "mig_profile")
        non_empty = [p for p in profiles if p and p not in ("Other", "Others")]
        assert non_empty, (
            "Expected mig_profile values in mig_profiles response; "
            f"data sample: {body.get('data', [])[:2]}"
        )

    def test_mig_profiles_filter_limit_without_other_bucket(
        self,
        e2e_pod_session: requests.Session,
        koku_api_url: str,
        gpu_mig_e2e_context: dict[str, Any],
    ):
        params = {
            **_time_scope_params(),
            "filter[cluster]": gpu_mig_e2e_context["cluster_id"],
            "filter[gpu_vendor]": "nvidia",
            "filter[gpu_model]": gpu_mig_e2e_context["gpu_model"],
            "filter[node]": gpu_mig_e2e_context["node"],
            "filter[limit]": "5",
        }
        url = (
            _gpu_report_url(koku_api_url, "mig_profiles/")
            + "?"
            + urlencode(params, doseq=True)
        )
        resp = e2e_pod_session.get(url, timeout=120)
        if resp.status_code == 403:
            pytest.skip("MIG profiles API disabled (Unleash flag off for tenant)")
        assert resp.status_code == 200, resp.text

        profiles = _walk_values(resp.json(), "mig_profile")
        assert "Other" not in profiles and "Others" not in profiles
