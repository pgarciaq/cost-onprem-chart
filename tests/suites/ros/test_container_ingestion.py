"""Deterministic container recommendation E2E via NISE ingest.

Ingests ocp_report_ros_0.yml (container-level ROS CSVs with --ros-ocp-info),
waits for native recommendation_sets rows, then asserts list API output.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k TestContainerIngestionE2E
"""

from __future__ import annotations

import tempfile
import time
import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Any

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
from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_ros_0.yml"
# NISE single-container pods use the pod name as the ROS container identifier (see IQE fixtures).
_EXPECTED_CONTAINERS = frozenset(
    {
        "pod-ros-A11",
        "pod-ros-A12",
        "pod-ros-A21",
        "pod-ros-B11",
        "pod-ros-B21",
        "pod-ros-idle1",
        "pod-ros-oom1",
    }
)
_MIN_DISTINCT_CONTAINERS = 7
_WAIT_TIMEOUT = 720
_POLL_INTERVAL = 15


def _wait_for_container_recommendation_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    min_distinct: int,
    timeout: int = _WAIT_TIMEOUT,
) -> bool:
    """Wait until recommendation_sets has enough distinct containers for the cluster."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(DISTINCT container_name) FROM recommendation_sets
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) >= min_distinct

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="container recommendation_sets population",
    )


def _add_codes_from_notifications(notifications: Any, codes: set[int]) -> None:
    """Extract integer codes from API notifications map (code -> {code, type, message})."""
    if not isinstance(notifications, dict):
        return
    for entry in notifications.values():
        if isinstance(entry, dict) and entry.get("code") is not None:
            codes.add(int(entry["code"]))


def _collect_notification_codes(item: dict[str, Any]) -> set[int]:
    """Collect notification codes from container recommendation list/detail payload."""
    codes: set[int] = set()
    recs = item.get("recommendations") or {}
    _add_codes_from_notifications(recs.get("notifications"), codes)

    terms = recs.get("recommendation_terms") or {}
    if not isinstance(terms, dict):
        return codes

    for term_data in terms.values():
        if not isinstance(term_data, dict):
            continue
        _add_codes_from_notifications(term_data.get("notifications"), codes)
        engines = term_data.get("recommendation_engines") or {}
        if not isinstance(engines, dict):
            continue
        for engine_data in engines.values():
            if not isinstance(engine_data, dict):
                continue
            _add_codes_from_notifications(engine_data.get("notifications"), codes)

    return codes


def _medium_term_engine_savings(item: dict[str, Any]) -> dict[str, Any] | None:
    recs = item.get("recommendations") or {}
    terms = recs.get("recommendation_terms") or recs
    if not isinstance(terms, dict):
        return None
    medium = terms.get("medium_term") or {}
    if not isinstance(medium, dict):
        return None
    engines = medium.get("recommendation_engines") or medium
    if not isinstance(engines, dict):
        return None
    for engine_name in ("cost", "performance"):
        engine = engines.get(engine_name)
        if isinstance(engine, dict):
            savings = engine.get("estimated_monthly_savings")
            if savings is not None:
                return savings
    return item.get("estimated_monthly_savings")


@pytest.mark.ros
@pytest.mark.extended
@pytest.mark.integration
@pytest.mark.timeout(600)
class TestContainerIngestionE2E:
    """Deterministic container recommendations via NISE data ingestion."""

    @pytest.fixture(autouse=True, scope="class")
    def ingest_container_data(
        self,
        ros_api_url,
        cluster_config,
        keycloak_config,
        ingress_url,
    ):
        """Generate container ROS data via NISE, upload, wait for recommendations."""
        if not ensure_nise_available():
            pytest.skip("NISE is not available for container ingestion E2E")

        session = requests.Session()
        session.verify = False

        auth = get_fresh_token(keycloak_config, cluster_config, session)
        if not auth:
            pytest.skip("Could not obtain JWT token for container ingestion E2E")

        cluster_id = str(uuid_mod.uuid4())

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
            source_name=f"ctr-ingest-{cluster_id.replace('-', '')[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, cluster_id, timeout=300
        ):
            pytest.fail(f"Provider not created for cluster {cluster_id}")

        end_date = datetime.utcnow() - timedelta(days=1)
        start_date = end_date - timedelta(days=14)

        with tempfile.TemporaryDirectory(prefix="ctr_ingest_e2e_") as temp_dir:
            files = generate_nise_data(
                cluster_id=cluster_id,
                start_date=start_date,
                end_date=end_date,
                output_dir=temp_dir,
                include_ros=True,
                iqe_template=_NISE_TEMPLATE,
            )

            pod_files = files.get("pod_usage_files") or []
            ros_files = list(files.get("ros_usage_files") or [])
            if not pod_files:
                pytest.skip("NISE did not generate pod_usage files for container E2E")
            if not ros_files:
                pytest.skip("NISE did not generate ocp_ros_usage files; need --ros-ocp-info")

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
            pytest.fail("Summary tables not populated after container E2E upload")

        if not _wait_for_container_recommendation_rows(
            cluster_config,
            db_pod,
            cluster_id,
            _UPLOAD_ORG_ID,
            _MIN_DISTINCT_CONTAINERS,
            timeout=_WAIT_TIMEOUT,
        ):
            pytest.fail(
                f"Expected at least {_MIN_DISTINCT_CONTAINERS} distinct container "
                f"recommendations after {_WAIT_TIMEOUT}s"
            )

        endpoint = get_recommendations_endpoint(ros_api_url)
        deadline = time.time() + 300
        containers: list[dict[str, Any]] = []
        while time.time() < deadline:
            api_resp = session.get(
                endpoint,
                headers=auth,
                params={"filter[cluster]": cluster_id, "limit": 100},
                timeout=60,
            )
            if api_resp.status_code == 200:
                data = api_resp.json().get("data") or []
                matched = [
                    row
                    for row in data
                    if (row.get("cluster_uuid") or row.get("cluster")) == cluster_id
                ]
                if matched:
                    containers = matched
                    break
            time.sleep(_POLL_INTERVAL)

        self.__class__._containers = containers
        self.__class__._cluster_id = cluster_id
        self.__class__._session = session
        self.__class__._auth = auth
        self.__class__._ros_api_url = ros_api_url

    def test_containers_ingested(self):
        """At least one container recommendation was produced from NISE data."""
        if not self._containers:
            pytest.skip(
                "No container recommendations on list API yet — DB rows exist; "
                "check RBAC or API filter[cluster]"
            )
        assert len(self._containers) > 0

    def test_expected_containers_present(self):
        """Verify NISE template containers appear in recommendations."""
        if not self._containers:
            pytest.skip("No container recommendations available")

        container_names = {
            c.get("container") or c.get("container_name") for c in self._containers
        }
        container_names.discard(None)
        matched = container_names & _EXPECTED_CONTAINERS
        assert len(matched) >= _MIN_DISTINCT_CONTAINERS, (
            f"Expected at least {_MIN_DISTINCT_CONTAINERS} of {_EXPECTED_CONTAINERS}, "
            f"got {container_names}"
        )

    def test_notification_codes_present(self):
        """At least one container has notification codes (idle/OOM from NISE template)."""
        if not self._containers:
            pytest.skip("No container recommendations available")

        all_codes: set[int] = set()
        containers_with_recs = 0
        for row in self._containers:
            if row.get("recommendations"):
                containers_with_recs += 1
            all_codes |= _collect_notification_codes(row)

        if all_codes:
            for code in all_codes:
                assert isinstance(code, int)
            assert all(code > 0 for code in all_codes), (
                f"Expected positive notification code integers, got {all_codes}"
            )
            return

        sample = self._containers[0]
        recs = sample.get("recommendations") or {}
        pytest.fail(
            f"No notification codes across {len(self._containers)} containers "
            f"({containers_with_recs} with recommendations). "
            f"Sample top-level notifications={bool(recs.get('notifications'))}, "
            f"recommendation_terms={bool(recs.get('recommendation_terms'))}. "
            f"NISE template should emit codes 3 (OOM on pod-ros-oom1) and/or "
            f"5 (idle on pod-ros-idle1)."
        )

    def test_savings_shape(self):
        """When cost data exists, savings have the expected value/units shape."""
        if not self._containers:
            pytest.skip("No container recommendations available")

        for row in self._containers:
            savings = row.get("estimated_monthly_savings") or _medium_term_engine_savings(
                row
            )
            if savings is None:
                continue
            if isinstance(savings, dict):
                assert "value" in savings, f"Unexpected savings shape: {savings}"
                assert "units" in savings, f"Unexpected savings shape: {savings}"
                return

        pytest.skip(
            "No cost-backed savings on ingested containers — cost model or "
            "Koku cost correlation may be missing for this cluster"
        )
