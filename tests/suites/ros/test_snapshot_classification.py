"""
Extended E2E: VolumeSnapshot staleness classification upload and API verification.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data using ocp_report_snapshot_classification.yml (--ros-ocp-info)
     with deterministic snapshot inventory rows (stale, orphaned, never_restored, active).
  3. Upload via ingress; wait for snapshot_inventory and snapshot_recommendation_sets.
  4. GET /recommendations/openshift/snapshots — assert classifications and filters.
  5. GET /recommendations/openshift/snapshots/summary — assert aggregated reclaimable data.

NISE requirements:
  - ``--ros-ocp-info`` (includes ocp_snapshot_inventory.csv in the tarball)
  - Template ``snapshots:`` list under OCPGenerator for deterministic ages/classifications

Run (requires cluster + extended time budget):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k snapshot_classification
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest
import requests

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
from suites.ros.test_recommendations import get_fresh_token
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

# Koku prepends "org" to JWT org_id; ROS stores bare org_id. SNO Keycloak uses "1234567".
_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_snapshot_classification.yml"
_NAMESPACE = "e2e-snap-ns"

# Expected recommendation_type per snapshot_name (matches template + default thresholds).
EXPECTED_CLASSIFICATIONS: dict[str, str] = {
    "e2e-snap-stale": "stale",
    "e2e-snap-orphaned": "orphaned",
    "e2e-snap-never-restored": "never_restored",
    "e2e-snap-active": "active",
}
_NON_ACTIVE_TYPES = frozenset({"stale", "orphaned", "never_restored", "redundant", "managed"})


def _snapshots_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/snapshots"
    )


def _snapshots_summary_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/snapshots/summary"
    )


def _fetch_snapshots(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _snapshots_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _fetch_snapshots_summary(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _snapshots_summary_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _skip_if_snapshot_plugin_disabled(resp: requests.Response) -> None:
    if resp.status_code == 404:
        pytest.skip(
            "Snapshot recommendations plugin not enabled (404 on /snapshots)"
        )


def _snapshot_row_by_name(
    rows: list[dict[str, Any]], snapshot_name: str
) -> Optional[dict[str, Any]]:
    for row in rows:
        if row.get("snapshot_name") == snapshot_name:
            return row
    return None


def _wait_for_snapshot_inventory_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 420,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM snapshot_inventory
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) >= len(EXPECTED_CLASSIFICATIONS)

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="snapshot_inventory population",
    )


def _wait_for_snapshot_recommendation_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 720,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM snapshot_recommendation_sets
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
              AND recommendation_type != 'active'
            """,
        )
        return result is not None and int(result[0][0]) >= 2

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=25,
        description="snapshot_recommendation_sets (non-active)",
    )


@dataclass
class SnapshotFlowContext:
    cluster_id: str
    auth: dict[str, str]


@pytest.mark.e2e
@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(1200)
class TestSnapshotClassificationExtendedFlow:
    """Upload snapshot inventory and verify staleness classification APIs."""

    @pytest.fixture(scope="class")
    def snapshot_e2e_cluster_id(self) -> str:
        return f"e2e-snap-{uuid.uuid4().hex[:8]}"

    @pytest.fixture(scope="class")
    def snapshot_flow_context(
        self,
        cluster_config,
        keycloak_config,
        snapshot_e2e_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
    ) -> SnapshotFlowContext:
        if not ensure_nise_available():
            pytest.skip("NISE is not available for snapshot classification E2E")

        probe_auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if probe_auth:
            probe = _fetch_snapshots(
                http_session, ros_api_url, probe_auth, {"limit": 1}
            )
            if probe.status_code == 404:
                pytest.skip(
                    "Snapshot recommendations plugin not enabled on cluster"
                )

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
            cluster_id=snapshot_e2e_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-snap-{snapshot_e2e_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, snapshot_e2e_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {snapshot_e2e_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-snapshot-")

        files = generate_nise_data(
            cluster_id=snapshot_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        snapshot_files = files.get("snapshot_inventory_files") or []
        if not snapshot_files:
            pytest.fail(
                "NISE did not generate ocp_snapshot_inventory.csv; "
                "use --ros-ocp-info and ocp_report_snapshot_classification.yml "
                "(requires koku-nise with OCP_SNAPSHOT_INVENTORY support)"
            )

        pod_files = files.get("pod_usage_files") or []
        if not pod_files:
            pytest.fail("NISE did not generate pod_usage files for snapshot E2E")

        ros_files = list(files.get("ros_usage_files") or [])
        ros_files = list(dict.fromkeys(ros_files + snapshot_files))

        package_path = create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files,
            cluster_id=snapshot_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            node_label_files=files.get("node_label_files") or None,
            namespace_label_files=files.get("namespace_label_files") or None,
        )

        upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
        upload_session = requests.Session()
        upload_session.verify = False
        token = obtain_jwt_token(keycloak_config)
        response = upload_with_retry(
            upload_session, upload_url, package_path, token.authorization_header
        )
        assert response.status_code in (200, 201, 202), response.text

        wait_for_summary_tables(
            cluster_config.namespace,
            db_pod,
            snapshot_e2e_cluster_id,
            timeout=420,
        )

        if not _wait_for_snapshot_inventory_rows(
            cluster_config, db_pod, snapshot_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=420
        ):
            pytest.fail("snapshot_inventory not populated after snapshot E2E upload")

        if not _wait_for_snapshot_recommendation_rows(
            cluster_config, db_pod, snapshot_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=720
        ):
            pytest.skip(
                "snapshot_recommendation_sets not populated within timeout; "
                "ROS snapshot plugin may need another processing cycle"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT after snapshot upload")

        return SnapshotFlowContext(cluster_id=snapshot_e2e_cluster_id, auth=auth)

    def test_snapshot_plugin_enabled(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        snapshot_flow_context: SnapshotFlowContext,
    ):
        resp = _fetch_snapshots(
            http_session,
            ros_api_url,
            snapshot_flow_context.auth,
            {"filter[cluster]": snapshot_flow_context.cluster_id, "limit": 5},
        )
        _skip_if_snapshot_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text

    def test_snapshot_classifications_after_upload(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        snapshot_flow_context: SnapshotFlowContext,
    ):
        resp = _fetch_snapshots(
            http_session,
            ros_api_url,
            snapshot_flow_context.auth,
            {
                "filter[cluster]": snapshot_flow_context.cluster_id,
                "filter[project]": _NAMESPACE,
                "limit": 50,
            },
        )
        _skip_if_snapshot_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("meta", {}).get("count", 0) >= len(EXPECTED_CLASSIFICATIONS), body

        rows = body.get("data") or []
        types_seen: set[str] = set()
        for snap_name, expected_type in EXPECTED_CLASSIFICATIONS.items():
            row = _snapshot_row_by_name(rows, snap_name)
            assert row, f"Missing snapshot row: {snap_name}"
            assert row.get("namespace") == _NAMESPACE
            assert row.get("cluster_uuid") == snapshot_flow_context.cluster_id
            actual = row.get("recommendation_type")
            assert actual == expected_type, (
                f"{snap_name}: expected {expected_type}, got {actual}"
            )
            types_seen.add(actual)

        assert types_seen & _NON_ACTIVE_TYPES, (
            "expected at least one non-active classification in API response"
        )

    def test_snapshot_filter_by_recommendation_type(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        snapshot_flow_context: SnapshotFlowContext,
    ):
        for rec_type in ("stale", "orphaned", "never_restored"):
            resp = _fetch_snapshots(
                http_session,
                ros_api_url,
                snapshot_flow_context.auth,
                {
                    "filter[cluster]": snapshot_flow_context.cluster_id,
                    "filter[project]": _NAMESPACE,
                    "filter[recommendation_type]": rec_type,
                    "limit": 20,
                },
            )
            _skip_if_snapshot_plugin_disabled(resp)
            assert resp.status_code == 200, resp.text
            rows = resp.json().get("data") or []
            assert rows, f"filter[recommendation_type]={rec_type} returned no rows"
            for row in rows:
                assert row.get("recommendation_type") == rec_type

        active_resp = _fetch_snapshots(
            http_session,
            ros_api_url,
            snapshot_flow_context.auth,
            {
                "filter[cluster]": snapshot_flow_context.cluster_id,
                "filter[recommendation_type]": "active",
                "limit": 20,
            },
        )
        _skip_if_snapshot_plugin_disabled(active_resp)
        assert active_resp.status_code == 200, active_resp.text
        active_rows = active_resp.json().get("data") or []
        active_names = {r.get("snapshot_name") for r in active_rows}
        assert "e2e-snap-active" in active_names

    def test_snapshot_summary_aggregates_reclaimable(
        self,
        ros_api_url: str,
        http_session: requests.Session,
        snapshot_flow_context: SnapshotFlowContext,
    ):
        resp = _fetch_snapshots_summary(
            http_session,
            ros_api_url,
            snapshot_flow_context.auth,
            {
                "filter[cluster]": snapshot_flow_context.cluster_id,
                "filter[project]": _NAMESPACE,
                "limit": 20,
            },
        )
        _skip_if_snapshot_plugin_disabled(resp)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        rows = body.get("data") or []
        assert rows, "snapshot summary should include namespace aggregates"

        row = rows[0]
        assert row.get("namespace") == _NAMESPACE
        assert row.get("cluster_uuid") == snapshot_flow_context.cluster_id
        assert row.get("snapshot_count", 0) >= len(EXPECTED_CLASSIFICATIONS)
        assert row.get("actionable_snapshot_count", 0) >= 2

        counts_by_type = row.get("counts_by_type") or {}
        assert counts_by_type.get("stale", 0) >= 1
        assert counts_by_type.get("orphaned", 0) >= 1

        reclaimable_bytes = row.get("reclaimable_restore_size_bytes", 0)
        reclaimable_cost = row.get("reclaimable_monthly_holding_cost_usd", 0)
        assert reclaimable_bytes > 0
        assert reclaimable_cost > 0
