"""HCP recommendations E2E via NISE ingest + SQL-seeded snapshots.

Fixture: ocp_report_ros_hcp.yml (HCP-named namespaces) uploaded through the
proven ingress path; HCP evidence seeded as fresh manifest_hcp_snapshots rows
BEFORE upload so the in-pipeline association pass stamps hosted_cluster_id.
clusters-hc-a associates to hc-e2e-1; clusters-hc-b is evidenced with an empty
ID (incomplete:true); app-ns has no evidence (never on the HCP surface).

Seed helpers live here for #637 UI reuse; snapshot rows are manifest-scoped
synthetic IDs, self-contained per run.

Tracks ros-ocp-backend #641.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k TestHCPRecommendationsE2E
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Any

import pytest
import requests

import e2e_helpers
from conftest import obtain_jwt_token
from e2e_helpers import (
    ensure_nise_available,
    generate_nise_data,
    get_koku_api_url,
    register_source,
    upload_with_retry,
    wait_for_provider,
    wait_for_summary_tables,
)
from suites.ros.test_recommendations import get_recommendations_endpoint
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_ros_hcp.yml"
_HCP_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nise_templates")
_HCP_NAMESPACE_ASSOCIATED = "clusters-hc-a"
_HCP_NAMESPACE_INCOMPLETE = "clusters-hc-b"
_APP_NAMESPACE = "app-ns"
_HCP_HOSTED_ID = "hc-e2e-1"
_WAIT_TIMEOUT = 720
_POLL_INTERVAL = 15


def _hcp_endpoint(ros_api_url: str) -> str:
    return f"{get_recommendations_endpoint(ros_api_url)}/hcp"


def seed_hcp_snapshots(
    namespace: str,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    manifest_id: str,
    hosted_cluster_id: str,
) -> None:
    """Insert a fresh complete snapshot row evidencing one HCP namespace.

    Shared seed helper for HCP E2E (#641) and UI (#637) needs: report-scoped
    synthetic manifest IDs keep runs self-contained. An empty hosted_cluster_id
    evidences the namespace without associating rows (incomplete:true).
    """
    result = execute_db_query(
        namespace,
        db_pod,
        "costonprem_ros",
        "postgres",
        f"""
        INSERT INTO manifest_hcp_snapshots
            (manifest_id, org_id, cluster_uuid, hcp_namespace,
             hosted_cluster_id, hc_uid, observed_at, complete)
        VALUES ('{manifest_id}', '{org_id}', '{cluster_id}',
                '{_HCP_NAMESPACE_ASSOCIATED if hosted_cluster_id else _HCP_NAMESPACE_INCOMPLETE}',
                '{hosted_cluster_id}', 'uid-{hosted_cluster_id or "unassociated"}',
                now(), true)
        ON CONFLICT (manifest_id, hcp_namespace) DO UPDATE SET
            hosted_cluster_id = EXCLUDED.hosted_cluster_id,
            observed_at = now(),
            complete = true
        """,
    )
    assert result is not None, "snapshot seed query failed"


def _seed_hcp_evidence(
    namespace: str, db_pod: str, cluster_id: str, manifest_id: str
) -> None:
    seed_hcp_snapshots(
        namespace, db_pod, cluster_id, _UPLOAD_ORG_ID, manifest_id, _HCP_HOSTED_ID
    )
    seed_hcp_snapshots(namespace, db_pod, cluster_id, _UPLOAD_ORG_ID, manifest_id, "")


def _wait_for_hcp_namespace_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    timeout: int = _WAIT_TIMEOUT,
) -> bool:
    """Wait until the associated HCP namespace has recommendation_sets rows."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(DISTINCT container_name) FROM recommendation_sets
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{_UPLOAD_ORG_ID}'
              AND namespace = '{_HCP_NAMESPACE_ASSOCIATED}'
            """,
        )
        return result is not None and int(result[0][0]) >= 2

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="HCP namespace recommendation_sets population",
    )


@pytest.mark.ros
@pytest.mark.extended
@pytest.mark.integration
@pytest.mark.timeout(900)
class TestHCPRecommendationsE2E:
    """HCP control-plane recommendations: scope, filter, group-by, detail, CSV."""

    _items: list[dict[str, Any]] = []
    _cluster_id = ""
    _session: requests.Session | None = None
    _auth: dict[str, str] = {}
    _ros_api_url = ""

    @pytest.fixture(autouse=True, scope="class")
    def ingest_hcp_data(
        self,
        ros_api_url,
        cluster_config,
        keycloak_config,
        ingress_url,
    ):
        """Seed HCP evidence, upload NISE HCP fixture, wait for association."""
        if not ensure_nise_available():
            pytest.skip("NISE is not available for HCP E2E")

        session = requests.Session()
        session.verify = False

        auth = get_fresh_token(keycloak_config, cluster_config, session)
        if not auth:
            pytest.skip("Could not obtain JWT token for HCP E2E")

        cluster_id = str(uuid_mod.uuid4())
        manifest_id = f"e2e-hcp-{cluster_id.replace('-', '')[:12]}"

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
            cluster_id=cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"hcp-ingest-{cluster_id.replace('-', '')[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, cluster_id, timeout=300
        ):
            pytest.fail(f"Provider not created for cluster {cluster_id}")

        # Evidence BEFORE upload: the in-pipeline association pass stamps
        # hosted_cluster_id only from evidence present during the run.
        _seed_hcp_evidence(
            cluster_config.namespace, db_pod, cluster_id, manifest_id
        )

        end_date = datetime.utcnow() - timedelta(days=1)
        start_date = end_date - timedelta(days=14)

        prev_templates_dir = e2e_helpers.NISE_TEMPLATES_DIR
        e2e_helpers.NISE_TEMPLATES_DIR = _HCP_TEMPLATE_DIR
        try:
            with tempfile.TemporaryDirectory(prefix="hcp_ingest_e2e_") as temp_dir:
                files = generate_nise_data(
                    cluster_id=cluster_id,
                    start_date=start_date,
                    end_date=end_date,
                    output_dir=temp_dir,
                    include_ros=True,
                    iqe_template=_NISE_TEMPLATE,
                )
        finally:
            e2e_helpers.NISE_TEMPLATES_DIR = prev_templates_dir

        pod_files = files.get("pod_usage_files") or []
        ros_files = list(files.get("ros_usage_files") or [])
        if not pod_files:
            pytest.skip("NISE did not generate pod_usage files for HCP E2E")
        if not ros_files:
            pytest.skip("NISE did not generate ocp_ros_usage files; need --ros-ocp-info")

        with tempfile.TemporaryDirectory(prefix="hcp_pkg_e2e_") as pkg_dir:
            package_path = create_upload_package_from_files(
                pod_usage_files=pod_files,
                ros_usage_files=ros_files,
                cluster_id=cluster_id,
                start_date=start_date,
                end_date=end_date,
                node_label_files=files.get("node_label_files") or None,
                namespace_label_files=files.get("namespace_label_files") or None,
            )

            upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
            upload_session = requests.Session()
            upload_session.verify = False
            token = obtain_jwt_token(keycloak_config)
            resp = upload_with_retry(
                upload_session,
                upload_url,
                package_path,
                token.authorization_header,
            )
            assert resp.status_code in (200, 201, 202), (
                f"Upload failed: {resp.status_code} {resp.text}"
            )

        schema = wait_for_summary_tables(
            cluster_config.namespace,
            db_pod,
            cluster_id,
            timeout=420,
        )
        if not schema:
            pytest.fail("Summary tables not populated after HCP E2E upload")

        if not _wait_for_hcp_namespace_rows(
            cluster_config, db_pod, cluster_id, timeout=_WAIT_TIMEOUT
        ):
            pytest.fail(
                f"No rows for {_HCP_NAMESPACE_ASSOCIATED} after {_WAIT_TIMEOUT}s"
            )

        endpoint = _hcp_endpoint(ros_api_url)
        deadline = time.time() + 300
        items: list[dict[str, Any]] = []
        while time.time() < deadline:
            api_resp = session.get(
                endpoint,
                headers=auth,
                params={"filter[cluster]": cluster_id, "limit": 100},
                timeout=60,
            )
            if api_resp.status_code == 200:
                data = api_resp.json().get("data") or []
                if data:
                    items = data
                    break
            time.sleep(_POLL_INTERVAL)

        self.__class__._items = items
        self.__class__._cluster_id = cluster_id
        self.__class__._session = session
        self.__class__._auth = auth
        self.__class__._ros_api_url = ros_api_url

    def test_hcp_list_scope(self):
        """HCP list shows associated + incomplete rows only; app rows excluded."""
        if not self._items:
            pytest.skip("No HCP recommendations available")
        namespaces = {item.get("namespace") or item.get("project") for item in self._items}
        assert _APP_NAMESPACE not in namespaces, "app-namespace rows must not appear"
        assert _HCP_NAMESPACE_ASSOCIATED in namespaces
        for item in self._items:
            if (item.get("namespace") or item.get("project")) == _HCP_NAMESPACE_INCOMPLETE:
                assert item.get("incomplete") is True, "unassociated HCP rows show incomplete:true"
            else:
                assert item.get("hosted_cluster_id") == _HCP_HOSTED_ID

    def test_hcp_filter_hosted_cluster_id(self):
        """filter[hosted_cluster_id] narrows; unknown IDs return empty 200."""
        if not self._items:
            pytest.skip("No HCP recommendations available")
        resp = self._session.get(
            _hcp_endpoint(self._ros_api_url),
            headers=self._auth,
            params={
                "filter[cluster]": self._cluster_id,
                "filter[hosted_cluster_id]": _HCP_HOSTED_ID,
                "limit": 100,
            },
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        for item in resp.json().get("data") or []:
            assert item.get("hosted_cluster_id") == _HCP_HOSTED_ID

        resp = self._session.get(
            _hcp_endpoint(self._ros_api_url),
            headers=self._auth,
            params={
                "filter[cluster]": self._cluster_id,
                "filter[hosted_cluster_id]": "no-such-hc",
                "limit": 100,
            },
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        assert (resp.json().get("data") or []) == []

    def test_hcp_group_by_hosted_cluster_id(self):
        """group_by aggregates associated HCs with counts (no unassociated)."""
        if not self._items:
            pytest.skip("No HCP recommendations available")
        resp = self._session.get(
            _hcp_endpoint(self._ros_api_url),
            headers=self._auth,
            params={
                "filter[cluster]": self._cluster_id,
                "group_by[hosted_cluster_id]": "*",
            },
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json().get("data") or []
        by_hc = {row.get("hosted_cluster_id"): row for row in rows}
        assert _HCP_HOSTED_ID in by_hc, f"associated HC missing: {rows}"
        assert by_hc[_HCP_HOSTED_ID].get("count", 0) >= 2

    def test_hcp_detail_gate(self):
        """HCP detail serves associated IDs; app-namespace IDs 404."""
        if not self._items:
            pytest.skip("No HCP recommendations available")
        rec_id = self._items[0].get("id")
        assert rec_id, "list item must include id"
        resp = self._session.get(
            f"{_hcp_endpoint(self._ros_api_url)}/{rec_id}",
            headers=self._auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("hosted_cluster_id") == _HCP_HOSTED_ID

    def test_hcp_detail_app_namespace_404(self):
        """App-namespace recommendation IDs 404 on the HCP surface."""
        if not self._items:
            pytest.skip("No HCP recommendations available")
        list_resp = self._session.get(
            get_recommendations_endpoint(self._ros_api_url),
            headers=self._auth,
            params={"filter[cluster]": self._cluster_id, "limit": 100},
            timeout=60,
        )
        assert list_resp.status_code == 200, list_resp.text
        app_ids = [
            item.get("id")
            for item in (list_resp.json().get("data") or [])
            if (item.get("namespace") or item.get("project")) == _APP_NAMESPACE
        ]
        if not app_ids:
            pytest.skip("No app-namespace rows to gate-check")
        resp = self._session.get(
            f"{_hcp_endpoint(self._ros_api_url)}/{app_ids[0]}",
            headers=self._auth,
            timeout=60,
        )
        assert resp.status_code == 404, resp.text

    def test_hcp_csv_export_smoke(self):
        """format=csv returns text/csv for the HCP surface."""
        if not self._items:
            pytest.skip("No HCP recommendations available")
        resp = self._session.get(
            _hcp_endpoint(self._ros_api_url),
            headers=self._auth,
            params={"filter[cluster]": self._cluster_id, "format": "csv", "limit": 10},
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        assert "text/csv" in resp.headers.get("Content-Type", ""), (
            f"Expected text/csv, got {resp.headers.get('Content-Type')!r}"
        )
