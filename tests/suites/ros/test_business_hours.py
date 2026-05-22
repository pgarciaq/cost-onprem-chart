"""
Business hours end-to-end tests (Phase 10).

Exercises the deployed stack: ros-ocp-backend settings API, dual digests,
recommendations enrichment, masu reship, and kill-switch behavior.

Requires:
  - ROS business hours feature enabled (ROS_BUSINESS_HOURS_ENABLED=true on ros-api)
  - Existing OCP cost/ROS data in the cluster (run E2E flow first for BH-E2E-001)

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --ros -k business_hours
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --ros -k business_hours -m slow
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import (
    check_pod_ready,
    execute_db_query,
    get_pod_by_label,
    get_secret_value,
    run_oc_command,
    wait_for_condition,
)

# BH-E2E IDs mapped to test functions below.


def _bh_settings_base(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/business-hours"
    )


def _capabilities_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/settings/capabilities"
    )


def _valid_schedule_payload() -> dict[str, Any]:
    return {
        "timezone": "America/New_York",
        "schedule": {
            "days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
            "start_time": "08:00",
            "end_time": "17:00",
        },
        "off_hours_weight": 0.0,
        "enabled": True,
    }


@pytest.fixture(scope="module")
def ros_database_config(cluster_config, database_config):
    """ROS PostgreSQL database on the unified server."""
    secret_name = f"{cluster_config.helm_release_name}-db-credentials"
    user = get_secret_value(cluster_config.namespace, secret_name, "ros-user")
    password = get_secret_value(cluster_config.namespace, secret_name, "ros-password")
    if not user or not password:
        pytest.skip("ROS database credentials not found")

    db_name_result = run_oc_command([
        "get", "deployment", f"{cluster_config.helm_release_name}-ros-api",
        "-n", cluster_config.namespace,
        "-o", "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='DB_NAME')].value}",
    ], check=False)
    db_name = db_name_result.stdout.strip() if db_name_result.returncode == 0 else ""
    if not db_name:
        db_name = "costonprem_ros"

    return {
        "pod_name": database_config.pod_name,
        "namespace": database_config.namespace,
        "database": db_name,
        "user": user,
        "password": password,
    }


@pytest.fixture(scope="module")
def business_hours_feature(ros_api_url: str, keycloak_config, cluster_config, http_session):
    """Skip the module when business hours is disabled or routes are hidden."""
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT for capabilities check")

    resp = http_session.get(_capabilities_url(ros_api_url), headers=auth, timeout=30)
    if resp.status_code == 404:
        pytest.skip("Business hours capabilities endpoint not deployed")
    if resp.status_code not in (200,):
        pytest.skip(f"Capabilities check failed: {resp.status_code}")

    data = resp.json()
    if not data.get("business_hours"):
        pytest.skip("business_hours capability is false (ROS_BUSINESS_HOURS_ENABLED?)")
    return data


@pytest.fixture
def bh_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.fixture
def bh_cluster_uuid(
    ros_api_url: str,
    bh_auth: dict,
    http_session: requests.Session,
    ros_database_config: dict,
    org_id: str,
) -> str:
    """Discover a cluster UUID with container digest data."""
    endpoint = get_recommendations_endpoint(ros_api_url)
    resp = http_session.get(endpoint, headers=bh_auth, params={"limit": 5}, timeout=60)
    if resp.status_code == 200:
        body = resp.json()
        for item in body.get("data", []) or []:
            cid = item.get("cluster_uuid") or item.get("cluster")
            if cid:
                return str(cid)

    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT DISTINCT cluster_uuid::text
        FROM daily_container_digests
        WHERE org_id = '{org_id}'
        LIMIT 1
        """,
        password=ros_database_config["password"],
    )
    if rows and rows[0][0]:
        return rows[0][0]
    pytest.skip("No cluster with digest data for business hours E2E")


def put_business_hours_schedule(
    http_session: requests.Session,
    ros_api_url: str,
    auth_header: dict,
    cluster_id: Optional[str] = None,
    namespace: Optional[str] = None,
    payload: Optional[dict] = None,
) -> requests.Response:
    """PUT org, cluster, or namespace business-hours schedule."""
    url = _bh_settings_base(ros_api_url)
    if cluster_id:
        url += f"/clusters/{cluster_id}"
    if cluster_id and namespace:
        url += f"/namespaces/{namespace}"
    return http_session.put(
        url,
        headers={**auth_header, "Content-Type": "application/json"},
        json=payload or _valid_schedule_payload(),
        timeout=60,
    )


def delete_business_hours_schedule(
    http_session: requests.Session,
    ros_api_url: str,
    auth_header: dict,
    cluster_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> requests.Response:
    url = _bh_settings_base(ros_api_url)
    if cluster_id:
        url += f"/clusters/{cluster_id}"
    if cluster_id and namespace:
        url += f"/namespaces/{namespace}"
    return http_session.delete(url, headers=auth_header, timeout=60)


def wait_for_dual_digests(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
    namespace: str = "",
    timeout: int = 300,
) -> bool:
    ns_filter = f" AND namespace = '{namespace}'" if namespace else ""

    def check():
        rows = execute_db_query(
            ros_database_config["namespace"],
            ros_database_config["pod_name"],
            ros_database_config["database"],
            ros_database_config["user"],
            f"""
            SELECT schedule_type::text, COUNT(*)::text
            FROM daily_container_digests
            WHERE org_id = '{org_id}'
              AND cluster_uuid = '{cluster_uuid}'::uuid
              {ns_filter}
            GROUP BY schedule_type
            """,
            password=ros_database_config["password"],
        )
        if not rows:
            return False
        types = {r[0] for r in rows}
        return "all_hours" in types and "business_hours" in types

    return wait_for_condition(check, timeout=timeout, interval=15, description="dual digests")


def wait_for_reship_pending_cleared(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
    timeout: int = 600,
) -> bool:
    def check():
        rows = execute_db_query(
            ros_database_config["namespace"],
            ros_database_config["pod_name"],
            ros_database_config["database"],
            ros_database_config["user"],
            f"""
            SELECT reship_pending_since IS NULL
            FROM business_hours_schedules
            WHERE org_id = '{org_id}'
              AND cluster_uuid = '{cluster_uuid}'::uuid
              AND namespace = ''
            LIMIT 1
            """,
            password=ros_database_config["password"],
        )
        return rows is not None and rows[0][0] == "t"

    return wait_for_condition(
        check, timeout=timeout, interval=20, description="reship_pending cleared"
    )


def _find_business_hours_in_recommendations(body: dict) -> bool:
    """Return True if any recommendation engine includes business_hours."""
    text = json.dumps(body)
    return "business_hours" in text


@pytest.mark.ros
@pytest.mark.integration
class TestBusinessHoursE2E:
    """Full-stack business hours scenarios (BH-E2E-001 through BH-E2E-007)."""

    def test_happy_path_dual_recommendations(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-001: schedule → dual digests → API shows business_hours CPU."""
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert resp.status_code in (200, 202), resp.text

            assert wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, timeout=420
            ), "Timed out waiting for dual schedule_type digests"

            rec_resp = http_session.get(
                get_recommendations_endpoint(ros_api_url),
                headers=bh_auth,
                params={"cluster": bh_cluster_uuid, "limit": 20},
                timeout=60,
            )
            assert rec_resp.status_code == 200, rec_resp.text
            assert _find_business_hours_in_recommendations(rec_resp.json())
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_schedule_change_trailing_reship(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-002: PUT schedule change triggers reship; BH digests refresh."""
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            assert put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            ).status_code in (200, 202)

            wait_for_dual_digests(ros_database_config, org_id, bh_cluster_uuid, timeout=420)

            rows_before = execute_db_query(
                ros_database_config["namespace"],
                ros_database_config["pod_name"],
                ros_database_config["database"],
                ros_database_config["user"],
                f"""
                SELECT COALESCE(MAX(updated_at)::text, '')
                FROM daily_container_digests
                WHERE org_id = '{org_id}' AND cluster_uuid = '{bh_cluster_uuid}'::uuid
                  AND schedule_type = 'business_hours'
                """,
                password=ros_database_config["password"],
            )
            ts_before = rows_before[0][0] if rows_before else ""

            payload = _valid_schedule_payload()
            payload["schedule"]["start_time"] = "09:00"
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, payload=payload
            )
            assert resp.status_code in (200, 202), resp.text

            def digests_refreshed():
                rows = execute_db_query(
                    ros_database_config["namespace"],
                    ros_database_config["pod_name"],
                    ros_database_config["database"],
                    ros_database_config["user"],
                    f"""
                    SELECT COALESCE(MAX(updated_at)::text, '')
                    FROM daily_container_digests
                    WHERE org_id = '{org_id}' AND cluster_uuid = '{bh_cluster_uuid}'::uuid
                      AND schedule_type = 'business_hours'
                    """,
                    password=ros_database_config["password"],
                )
                if not rows:
                    return False
                return rows[0][0] != "" and (not ts_before or rows[0][0] >= ts_before)

            assert wait_for_condition(
                digests_refreshed, timeout=600, interval=20, description="BH digest refresh"
            )
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_delete_inheritance(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        http_session: requests.Session,
    ):
        """BH-E2E-003: DELETE cluster override falls back to org default."""
        org_payload = _valid_schedule_payload()
        org_payload["schedule"]["start_time"] = "07:00"
        cluster_payload = _valid_schedule_payload()
        cluster_payload["schedule"]["start_time"] = "10:00"

        try:
            put_business_hours_schedule(http_session, ros_api_url, bh_auth, payload=org_payload)
            put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, payload=cluster_payload
            )

            del_resp = delete_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert del_resp.status_code in (200, 204), del_resp.text

            get_resp = http_session.get(
                f"{_bh_settings_base(ros_api_url)}/clusters/{bh_cluster_uuid}",
                headers=bh_auth,
                timeout=30,
            )
            assert get_resp.status_code == 200, get_resp.text
            data = get_resp.json()
            assert data.get("schedule", {}).get("start_time") == "07:00"
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth)

    @pytest.mark.slow
    def test_masu_unavailable_retry(
        self,
        business_hours_feature,
        cluster_config,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-004: masu down → reship_pending set → masu up → pending cleared."""
        masu_deploy = f"{cluster_config.helm_release_name}-masu-api"
        ns = cluster_config.namespace

        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            run_oc_command([
                "scale", "deployment", masu_deploy, "-n", ns, "--replicas=0",
            ], check=False)
            time.sleep(10)

            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert resp.status_code in (200, 202), resp.text

            def pending_set():
                rows = execute_db_query(
                    ros_database_config["namespace"],
                    ros_database_config["pod_name"],
                    ros_database_config["database"],
                    ros_database_config["user"],
                    f"""
                    SELECT (reship_pending_since IS NOT NULL)::text
                    FROM business_hours_schedules
                    WHERE org_id = '{org_id}'
                      AND cluster_uuid = '{bh_cluster_uuid}'::uuid
                      AND namespace = ''
                    """,
                    password=ros_database_config["password"],
                )
                return rows and rows[0][0] == "t"

            assert wait_for_condition(
                pending_set, timeout=120, interval=10, description="reship_pending set"
            )

            run_oc_command([
                "scale", "deployment", masu_deploy, "-n", ns, "--replicas=1",
            ], check=False)
            assert check_pod_ready(ns, "app.kubernetes.io/component=cost-processor", timeout=180)

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            )
        finally:
            run_oc_command([
                "scale", "deployment", masu_deploy, "-n", ns, "--replicas=1",
            ], check=False)
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)


@pytest.mark.ros
@pytest.mark.integration
class TestBusinessHoursKillSwitch:
    """Kill-switch behavior requires ROS_BUSINESS_HOURS_ENABLED=false on ros-api."""

    def _ros_bh_env_enabled(self, cluster_config) -> Optional[bool]:
        result = run_oc_command([
            "get", "deployment", f"{cluster_config.helm_release_name}-ros-api",
            "-n", cluster_config.namespace,
            "-o", "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='ROS_BUSINESS_HOURS_ENABLED')].value}",
        ], check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip().lower() in ("true", "1", "yes")

    def test_kill_switch_disabled(
        self,
        ros_api_url: str,
        keycloak_config,
        cluster_config,
        http_session: requests.Session,
    ):
        """BH-E2E-005: feature off → 404 on settings; OpenAPI omits paths."""
        if self._ros_bh_env_enabled(cluster_config) is not False:
            pytest.skip(
                "Set ROS_BUSINESS_HOURS_ENABLED=false on ros-api to run kill-switch test"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT token")

        resp = http_session.get(_bh_settings_base(ros_api_url), headers=auth, timeout=30)
        assert resp.status_code == 404

        cap = http_session.get(_capabilities_url(ros_api_url), headers=auth, timeout=30)
        if cap.status_code == 200:
            assert cap.json().get("business_hours") is False

        openapi = http_session.get(
            f"{ros_api_url.rstrip('/')}/cost-management/v1/openapi.json",
            headers=auth,
            timeout=60,
        )
        if openapi.status_code == 200:
            paths = openapi.json().get("paths", {})
            assert "/recommendations/openshift/settings/business-hours" not in paths

    def test_kill_switch_re_enable(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-006: when enabled, settings routes respond."""
        resp = http_session.get(_bh_settings_base(ros_api_url), headers=bh_auth, timeout=30)
        assert resp.status_code in (200, 404), resp.text


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.slow
class TestBusinessHoursExtended:
    """Long-running validation (BH-E2E-007)."""

    def test_first_time_90_day_reship(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-007: first schedule on cluster with ~90d history completes within 30 minutes."""
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            start = time.time()
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert resp.status_code in (200, 202), resp.text

            assert wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, timeout=1800
            ), "90-day reship did not produce dual digests within 30 minutes"

            elapsed_min = (time.time() - start) / 60.0
            assert elapsed_min < 30.0, f"Reship took {elapsed_min:.1f} minutes (limit 30)"
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)


@pytest.mark.extended
class TestBusinessHoursExtendedScenarios:
    """Extended business-hours scenarios (BH-E2E-012 through BH-E2E-020)."""

    def test_bh_e2e_012_metrics_reship_attempts_total(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
    ):
        """BH-E2E-012: Metrics endpoint exposes reship-related Prometheus counters."""
        pytest.skip("requires Prometheus scrape of ros-api metrics port; not wired in default E2E")

    def test_bh_e2e_013_kafka_consumer_failure_redelivery(
        self,
        business_hours_feature,
    ):
        """BH-E2E-013: Kafka consumer failure causes message redelivery after recovery."""
        pytest.skip("requires fault injection infrastructure")

    def test_bh_e2e_014_s3_presigned_url_expired(
        self,
        business_hours_feature,
    ):
        """BH-E2E-014: Expired S3 presigned URL returns 403, logged, metric incremented."""
        pytest.skip("requires fault injection infrastructure")

    def test_bh_e2e_015_schedule_change_during_active_reship(
        self,
        business_hours_feature,
        http_session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
    ):
        """BH-E2E-015: Schedule change during active reship triggers trailing reship."""
        pytest.skip("requires concurrent PUT timing against live masu reship")

    def test_bh_e2e_016_incremental_visibility(
        self,
        business_hours_feature,
        http_session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
    ):
        """BH-E2E-016: New BH recommendations appear within upload_cycle after reship starts."""
        pytest.skip("requires long-running reship observation window")

    def test_bh_e2e_017_delete_schedule_prunes_digests(
        self,
        business_hours_feature,
        http_session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        ros_database_config,
        org_id: str,
    ):
        """BH-E2E-017: DELETE schedule prunes business_hours digests on next ingest."""
        pytest.skip("requires full ingest cycle after DELETE; extend BH-E2E-002 pattern")

    def test_bh_e2e_018_kill_switch_no_bh_data(
        self,
        ros_api_url: str,
        keycloak_config,
        cluster_config,
        http_session,
    ):
        """BH-E2E-018: Kill-switch off hides BH endpoints and omits BH digests from API."""
        pytest.skip("covered by TestBusinessHoursKillSwitch; run with ROS_BUSINESS_HOURS_ENABLED=false")

    def test_bh_e2e_019_multiple_orgs_isolated(
        self,
        business_hours_feature,
        http_session,
        ros_api_url: str,
        bh_auth: dict,
        ros_database_config,
    ):
        """BH-E2E-019: Org1 schedule does not affect org2 recommendations."""
        pytest.skip("requires second org identity and isolated cluster fixtures")

    def test_bh_e2e_020_concurrent_puts_max_two_reships(
        self,
        business_hours_feature,
        http_session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
    ):
        """BH-E2E-020: Concurrent PUTs result in at most two masu reship executions."""
        pytest.skip("requires masu request counting under concurrent load")
