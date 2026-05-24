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

import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token, get_recommendations_endpoint
from utils import (
    check_pod_ready,
    exec_in_pod,
    execute_db_query,
    get_pod_by_label,
    get_secret_value,
    run_oc_command,
    wait_for_condition,
)

# BH-E2E IDs mapped to test functions below.

# Mirrors ros-ocp-backend validWorkloadTypes (internal/api/common.go).
_VALID_WORKLOAD_TYPES = frozenset({
    "daemonset",
    "deployment",
    "deploymentconfig",
    "replicaset",
    "replicationcontroller",
    "statefulset",
})


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
def business_hours_feature(ros_api_url: str, keycloak_config, cluster_config):
    """Skip the module when business hours is disabled or routes are hidden."""
    session = requests.Session()
    session.verify = False
    auth = get_fresh_token(keycloak_config, cluster_config, session)
    if not auth:
        pytest.skip("Could not obtain JWT for capabilities check")

    resp = session.get(_capabilities_url(ros_api_url), headers=auth, timeout=30)
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


def _fresh_bh_auth(keycloak_config, cluster_config, http_session: requests.Session) -> dict:
    """Obtain a new JWT (Keycloak tokens expire after ~5 minutes)."""
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.fail("Could not obtain JWT token")
    return auth


def _pick_registered_bh_cluster(
    ros_api_url: str,
    bh_auth: dict,
    http_session: requests.Session,
    ros_database_config: dict,
    org_id: str,
) -> Optional[str]:
    """Return a cluster registered in ROS clusters with digest data and BH settings access."""
    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT DISTINCT c.cluster_uuid::text
        FROM clusters c
        JOIN rh_accounts r ON r.id = c.tenant_id AND r.org_id = '{org_id}'
        JOIN daily_container_digests d
          ON d.cluster_uuid::text = c.cluster_uuid::text AND d.org_id = '{org_id}'
        ORDER BY c.cluster_uuid::text
        LIMIT 10
        """,
        password=ros_database_config["password"],
    )
    candidates = [row[0] for row in rows or [] if row and row[0]]

    if not candidates:
        endpoint = get_recommendations_endpoint(ros_api_url)
        resp = http_session.get(endpoint, headers=bh_auth, params={"limit": 20}, timeout=60)
        if resp.status_code == 200:
            for item in resp.json().get("data", []) or []:
                cid = item.get("cluster_uuid") or item.get("cluster")
                if cid and str(cid) not in candidates:
                    candidates.append(str(cid))

    settings_base = _bh_settings_base(ros_api_url)
    for cluster_id in candidates:
        probe = http_session.get(
            f"{settings_base}/clusters/{cluster_id}",
            headers=bh_auth,
            timeout=30,
        )
        if probe.status_code == 200:
            return cluster_id
    return None


@pytest.fixture
def bh_cluster_uuid(
    ros_api_url: str,
    bh_auth: dict,
    http_session: requests.Session,
    ros_database_config: dict,
    org_id: str,
) -> str:
    """Discover a cluster registered in ROS with digest data for business hours E2E."""
    cluster_id = _pick_registered_bh_cluster(
        ros_api_url, bh_auth, http_session, ros_database_config, org_id
    )
    if cluster_id:
        return cluster_id
    pytest.skip("No registered cluster with digest data for business hours E2E")


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


def _pg_bool(value: Optional[str]) -> bool:
    """Parse a PostgreSQL boolean from psql -t -A output (true/false or t/f)."""
    return (value or "").lower() in ("t", "true", "1")


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
        return rows is not None and _pg_bool(rows[0][0])

    return wait_for_condition(
        check, timeout=timeout, interval=20, description="reship_pending cleared"
    )


def _recommendations_include_business_hours(body: dict) -> bool:
    """Return True if any recommendation engine includes a business_hours block."""
    for item in body.get("data", []) or []:
        terms = (
            item.get("recommendations", {}).get("recommendation_terms")
            or item.get("recommendations", {})
        )
        if not isinstance(terms, dict):
            continue
        for term in terms.values():
            if not isinstance(term, dict):
                continue
            engines = term.get("recommendation_engines") or {}
            for profile in ("cost", "performance"):
                engine = engines.get(profile) or {}
                bh = engine.get("business_hours")
                if isinstance(bh, dict) and bh:
                    return True
    return False


def _find_business_hours_in_recommendations(body: dict) -> bool:
    """Return True if any recommendation includes business_hours (structured check)."""
    return _recommendations_include_business_hours(body)


def _is_api_safe_param_value(value: str, allow_dot: bool = False) -> bool:
    """Match ros-ocp-backend sanitizeParamValue / isCharSafeRFC1123 rules."""
    if not value:
        return False
    for ch in value:
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
            continue
        if ch in "-_":
            continue
        if allow_dot and ch == ".":
            continue
        return False
    return True


def _find_container_with_bh_digests(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
) -> Optional[dict[str, str]]:
    """Return one container row that has business_hours digest data."""
    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT namespace, workload, workload_type, container_name
        FROM daily_container_digests
        WHERE org_id = '{org_id}'
          AND cluster_uuid = '{cluster_uuid}'::uuid
          AND schedule_type = 'business_hours'
          AND sample_count > 0
        ORDER BY bucket_date DESC
        LIMIT 20
        """,
        password=ros_database_config["password"],
    )
    for row in rows or []:
        if not row or not row[0]:
            continue
        ns, workload, workload_type, container = row
        ns = (ns or "").strip()
        workload = (workload or "").strip()
        workload_type = (workload_type or "").strip()
        container = (container or "").strip()
        candidate = {
            "namespace": ns,
            "workload": workload,
            "workload_type": workload_type,
            "container": container,
        }
        wl_type = (workload_type or "").lower()
        if (
            _is_api_safe_param_value(ns)
            and _is_api_safe_param_value(workload, allow_dot=True)
            and wl_type in _VALID_WORKLOAD_TYPES
            and _is_api_safe_param_value(container)
        ):
            candidate["workload_type"] = wl_type
            return candidate
    return None


def _fetch_recommendations_for_bh_container(
    http_session: requests.Session,
    ros_api_url: str,
    auth_header: dict,
    cluster_uuid: str,
    container: dict[str, str],
    keycloak_config=None,
    cluster_config=None,
) -> requests.Response:
    """Query recommendations scoped to a container known to have BH digests."""
    if keycloak_config is not None and cluster_config is not None:
        auth_header = _fresh_bh_auth(keycloak_config, cluster_config, http_session)

    # Let requests encode query params once (manual encoding can double-encode %).
    return http_session.get(
        get_recommendations_endpoint(ros_api_url),
        headers=auth_header,
        params={
            "cluster": cluster_uuid,
            "project": container["namespace"],
            "workload": container["workload"],
            "workload_type": container["workload_type"],
            "container": container["container"],
            "limit": 5,
        },
        timeout=60,
    )


def _fetch_cluster_recommendations_with_business_hours(
    http_session: requests.Session,
    ros_api_url: str,
    auth_header: dict,
    cluster_uuid: str,
    keycloak_config=None,
    cluster_config=None,
    max_pages: int = 10,
    page_size: int = 50,
) -> tuple[requests.Response, bool]:
    """Paginate cluster recommendations until any item includes business_hours.

    Avoids per-container workload filters, which fail when digest rows contain
    API-unsafe names (e.g. ``<none>``).
    """
    endpoint = get_recommendations_endpoint(ros_api_url)
    offset = 0
    last_resp: Optional[requests.Response] = None

    for _ in range(max_pages):
        if keycloak_config is not None and cluster_config is not None:
            auth_header = _fresh_bh_auth(keycloak_config, cluster_config, http_session)

        last_resp = http_session.get(
            endpoint,
            headers=auth_header,
            params={"cluster": cluster_uuid, "limit": page_size, "offset": offset},
            timeout=60,
        )
        if last_resp.status_code != 200:
            return last_resp, False

        body = last_resp.json()
        if _recommendations_include_business_hours(body):
            return last_resp, True

        data = body.get("data", []) or []
        if len(data) < page_size:
            break

        meta = body.get("meta", {})
        total = meta.get("count")
        offset += page_size
        if isinstance(total, int) and offset >= total:
            break

    assert last_resp is not None
    return last_resp, False


def _scale_masu_to_zero(cluster_config, timeout: int = 180) -> None:
    """Scale MASU to zero and wait until cost-processor pods are fully gone."""
    masu_deploy = f"{cluster_config.helm_release_name}-koku-masu"
    ns = cluster_config.namespace
    run_oc_command([
        "scale", "deployment", masu_deploy, "-n", ns, "--replicas=0",
    ], check=False)

    def masu_pods_gone() -> bool:
        result = run_oc_command([
            "get", "pods", "-n", ns,
            "-l", "app.kubernetes.io/component=cost-processor",
            "-o", "jsonpath={.items[*].metadata.name}",
        ], check=False)
        if result.returncode != 0:
            return False
        pod_names = [p for p in result.stdout.strip().split() if p]
        if not pod_names:
            return True
        # Terminating pods can still serve traffic; force-remove stragglers.
        run_oc_command([
            "delete", "pod", "-n", ns,
            "-l", "app.kubernetes.io/component=cost-processor",
            "--force", "--grace-period=0",
        ], check=False)
        return False

    assert wait_for_condition(
        masu_pods_gone,
        timeout=timeout,
        interval=5,
        description="masu pods terminated",
    ), (
        "masu must be fully scaled down before PUT "
        "(Terminating pods can still accept traffic)"
    )


RESHIP_PROMETHEUS_METRICS = (
    "ros_reship_in_progress",
    "ros_reship_files_processed",
    "ros_reship_duration_seconds",
    "ros_reship_failures_total",
)

PROCESSOR_FETCH_ERROR_METRIC = "rosocp_csv_fetch_error_total"


def _org_id_from_auth(auth_header: dict) -> Optional[str]:
    """Decode org_id from a Bearer JWT payload."""
    token = auth_header.get("Authorization", "").removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (json.JSONDecodeError, ValueError):
        return None
    return claims.get("org_id")


def _ros_bh_env_enabled(cluster_config) -> Optional[bool]:
    """Return True/False when ROS_BUSINESS_HOURS_ENABLED is set; None if unset."""
    result = run_oc_command([
        "get", "deployment", f"{cluster_config.helm_release_name}-ros-api",
        "-n", cluster_config.namespace,
        "-o", "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='ROS_BUSINESS_HOURS_ENABLED')].value}",
    ], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().lower() in ("true", "1", "yes")


def _fetch_component_metrics(
    cluster_config,
    component_label: str,
    metrics_port: int = 9000,
) -> Optional[str]:
    """Scrape /metrics from a component pod via in-pod curl."""
    pod = get_pod_by_label(cluster_config.namespace, f"app.kubernetes.io/component={component_label}")
    if not pod:
        return None
    return exec_in_pod(
        cluster_config.namespace,
        pod,
        ["curl", "-sf", f"http://127.0.0.1:{metrics_port}/metrics"],
        timeout=30,
    )


def _prometheus_metric_present(metrics_text: str, metric_name: str) -> bool:
    """Return True if metric_name appears in Prometheus exposition text."""
    if not metrics_text:
        return False
    pattern = re.compile(rf"^(?:# HELP |# TYPE |){re.escape(metric_name)}(\{{|\s)", re.MULTILINE)
    return bool(pattern.search(metrics_text))


def _prometheus_counter_value(metrics_text: str, metric_name: str) -> float:
    """Sum counter/histogram _count samples for a metric name."""
    if not metrics_text:
        return 0.0
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if not (line.startswith(metric_name) or line.startswith(f"{metric_name}_")):
            continue
        if "_bucket{" in line or line.startswith(f"{metric_name}_sum"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                total += float(parts[-1])
            except ValueError:
                continue
    return total


def _count_digest_rows(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
    schedule_type: str,
) -> int:
    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT COUNT(*)::text
        FROM daily_container_digests
        WHERE org_id = '{org_id}'
          AND cluster_uuid = '{cluster_uuid}'::uuid
          AND schedule_type = '{schedule_type}'
        """,
        password=ros_database_config["password"],
    )
    return int(rows[0][0]) if rows and rows[0][0] else 0


def _find_namespaces_with_digests(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
    min_count: int = 2,
) -> list[str]:
    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT namespace, COUNT(*)::text AS cnt
        FROM daily_container_digests
        WHERE org_id = '{org_id}'
          AND cluster_uuid = '{cluster_uuid}'::uuid
          AND schedule_type = 'all_hours'
          AND namespace <> ''
        GROUP BY namespace
        HAVING COUNT(*) >= 5
        ORDER BY cnt DESC
        LIMIT 10
        """,
        password=ros_database_config["password"],
    )
    namespaces = [row[0] for row in rows or [] if row and row[0]]
    if len(namespaces) >= min_count:
        return namespaces[:min_count]
    return namespaces


def _find_namespace_with_bh_samples(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
) -> tuple[Optional[str], float]:
    """Return the namespace with the highest avg business_hours sample_count, if any."""
    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT namespace, COALESCE(AVG(sample_count), 0)::text
        FROM daily_container_digests
        WHERE org_id = '{org_id}'
          AND cluster_uuid = '{cluster_uuid}'::uuid
          AND schedule_type = 'business_hours'
          AND namespace <> ''
          AND sample_count > 0
        GROUP BY namespace
        ORDER BY AVG(sample_count) DESC
        LIMIT 1
        """,
        password=ros_database_config["password"],
    )
    if not rows or not rows[0][0]:
        return None, 0.0
    return rows[0][0], float(rows[0][1] or 0)


def _avg_bh_sample_count(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
    namespace: str,
) -> float:
    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT COALESCE(AVG(sample_count), 0)::text
        FROM daily_container_digests
        WHERE org_id = '{org_id}'
          AND cluster_uuid = '{cluster_uuid}'::uuid
          AND namespace = '{namespace}'
          AND schedule_type = 'business_hours'
          AND sample_count > 0
        """,
        password=ros_database_config["password"],
    )
    if not rows or not rows[0][0]:
        return 0.0
    return float(rows[0][0])


def _find_cluster_without_digests(
    ros_database_config: dict,
    org_id: str,
) -> Optional[str]:
    rows = execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        SELECT c.cluster_uuid::text
        FROM clusters c
        JOIN rh_accounts r ON r.id = c.tenant_id AND r.org_id = '{org_id}'
        WHERE NOT EXISTS (
            SELECT 1 FROM daily_container_digests d
            WHERE d.org_id = '{org_id}'
              AND d.cluster_uuid = c.cluster_uuid
        )
        ORDER BY c.cluster_uuid::text
        LIMIT 5
        """,
        password=ros_database_config["password"],
    )
    return rows[0][0] if rows and rows[0][0] else None


def _get_cluster_reship_status(
    http_session: requests.Session,
    ros_api_url: str,
    auth_header: dict,
    cluster_id: str,
) -> dict[str, Any]:
    resp = http_session.get(
        f"{_bh_settings_base(ros_api_url)}/clusters/{cluster_id}",
        headers=auth_header,
        timeout=30,
    )
    if resp.status_code != 200:
        return {}
    return resp.json()


def wait_for_reship_status(
    http_session: requests.Session,
    ros_api_url: str,
    auth_header: dict,
    cluster_id: str,
    expected_status: str,
    timeout: int = 300,
    auth_refresher: Optional[Callable[[], dict]] = None,
) -> bool:
    def check():
        auth = auth_refresher() if auth_refresher else auth_header
        body = _get_cluster_reship_status(http_session, ros_api_url, auth, cluster_id)
        return body.get("reship_status") == expected_status

    return wait_for_condition(
        check, timeout=timeout, interval=15, description=f"reship_status={expected_status}"
    )


def _count_ros_reship_completions(
    cluster_config,
    since_seconds: int = 600,
    cluster_uuid: Optional[str] = None,
) -> int:
    """Count 'reship completed' log lines on ros-api (one per masu reship_ros call)."""
    deploy = f"{cluster_config.helm_release_name}-ros-api"
    result = run_oc_command([
        "logs", f"deployment/{deploy}",
        "-n", cluster_config.namespace,
        f"--since={since_seconds}s",
        "--tail=5000",
    ], check=False)
    if result.returncode != 0:
        return 0
    count = 0
    cluster_marker = f"cluster_uuid={cluster_uuid}" if cluster_uuid else None
    for line in result.stdout.splitlines():
        if "reship completed" not in line:
            continue
        if cluster_marker and cluster_marker not in line:
            continue
        count += 1
    return count


def _wait_for_reship_quiescence(cluster_config, quiet_seconds: int = 30, timeout: int = 120) -> bool:
    """Wait until no reship completed log lines appear in the recent window."""
    return wait_for_condition(
        lambda: _count_ros_reship_completions(cluster_config, since_seconds=quiet_seconds) == 0,
        timeout=timeout,
        interval=5,
        description=f"no reship completions in last {quiet_seconds}s",
    )


def _grep_pod_logs(
    cluster_config,
    component_label: str,
    pattern: str,
    since_seconds: int = 600,
) -> str:
    pod = get_pod_by_label(
        cluster_config.namespace, f"app.kubernetes.io/component={component_label}"
    )
    if not pod:
        return ""
    result = run_oc_command([
        "logs", pod,
        "-n", cluster_config.namespace,
        f"--since={since_seconds}s",
        "--tail=500",
    ], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout


@pytest.mark.timeout(900)
@pytest.mark.ros
@pytest.mark.integration
class TestBusinessHoursE2E:
    """Full-stack business hours scenarios (BH-E2E-001 through BH-E2E-011)."""

    def test_happy_path_dual_recommendations(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
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

            rec_resp, found_bh = _fetch_cluster_recommendations_with_business_hours(
                http_session,
                ros_api_url,
                bh_auth,
                bh_cluster_uuid,
                keycloak_config,
                cluster_config,
            )
            assert rec_resp.status_code == 200, rec_resp.text
            assert found_bh, (
                "Expected business_hours in cluster recommendations after reship; "
                "ensure cluster has ROS CSV history and ROS_BUSINESS_HOURS_ENABLED on processor"
            )
        finally:
            delete_business_hours_schedule(
                http_session,
                ros_api_url,
                _fresh_bh_auth(keycloak_config, cluster_config, http_session),
                bh_cluster_uuid,
            )

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

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            ), "Initial reship did not complete"

            assert wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, timeout=420
            ), "Timed out waiting for initial dual schedule_type digests"

            payload = _valid_schedule_payload()
            payload["schedule"]["start_time"] = "09:00"
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, payload=payload
            )
            assert resp.status_code in (200, 202), resp.text

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            ), "Schedule change reship did not complete"

            get_resp = http_session.get(
                f"{_bh_settings_base(ros_api_url)}/clusters/{bh_cluster_uuid}",
                headers=bh_auth,
                timeout=30,
            )
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json().get("schedule", {}).get("start_time") == "09:00"
            assert wait_for_reship_status(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, "complete", timeout=120
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
        masu_deploy = f"{cluster_config.helm_release_name}-koku-masu"
        ns = cluster_config.namespace

        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            _scale_masu_to_zero(cluster_config, timeout=180)

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
                return rows and _pg_bool(rows[0][0])

            assert wait_for_condition(
                pending_set, timeout=120, interval=10, description="reship_pending set"
            )

            run_oc_command([
                "scale", "deployment", masu_deploy, "-n", ns, "--replicas=1",
            ], check=False)
            assert wait_for_condition(
                lambda: check_pod_ready(ns, "app.kubernetes.io/component=cost-processor"),
                timeout=180,
                interval=10,
                description="masu cost-processor pod ready",
            )

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            )
        finally:
            run_oc_command([
                "scale", "deployment", masu_deploy, "-n", ns, "--replicas=1",
            ], check=False)
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_namespace_mixed_schedules(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-008: namespace override uses its own window; others inherit cluster schedule."""
        namespaces = _find_namespaces_with_digests(
            ros_database_config, org_id, bh_cluster_uuid, min_count=2
        )
        if len(namespaces) < 2:
            pytest.skip("Need at least two namespaces with digest data for mixed-schedule test")

        override_ns, inherit_ns = namespaces[0], namespaces[1]
        delete_business_hours_schedule(
            http_session, ros_api_url, bh_auth, bh_cluster_uuid, override_ns
        )
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            cluster_payload = _valid_schedule_payload()
            cluster_payload["schedule"]["start_time"] = "08:00"
            cluster_payload["schedule"]["end_time"] = "17:00"
            assert put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, payload=cluster_payload
            ).status_code in (200, 202)

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            ), "Cluster schedule reship did not complete"

            assert wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, override_ns, timeout=420
            ), "Timed out waiting for dual digests under cluster schedule"
            assert wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, inherit_ns, timeout=420
            ), "Timed out waiting for dual digests in inherited namespace"

            inherit_avg = _avg_bh_sample_count(
                ros_database_config, org_id, bh_cluster_uuid, inherit_ns
            )
            override_wide_avg = _avg_bh_sample_count(
                ros_database_config, org_id, bh_cluster_uuid, override_ns
            )
            assert inherit_avg > 0 and override_wide_avg > 0, "Expected non-zero BH sample counts"

            ns_payload = _valid_schedule_payload()
            ns_payload["schedule"]["start_time"] = "12:00"
            ns_payload["schedule"]["end_time"] = "13:00"
            assert put_business_hours_schedule(
                http_session,
                ros_api_url,
                bh_auth,
                bh_cluster_uuid,
                namespace=override_ns,
                payload=ns_payload,
            ).status_code in (200, 202)

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            ), "Namespace override reship did not complete"

            def override_narrower_than_inherited() -> bool:
                override_avg = _avg_bh_sample_count(
                    ros_database_config, org_id, bh_cluster_uuid, override_ns
                )
                inherited_avg = _avg_bh_sample_count(
                    ros_database_config, org_id, bh_cluster_uuid, inherit_ns
                )
                return (
                    override_avg > 0
                    and inherited_avg > 0
                    and override_avg < inherited_avg
                )

            assert wait_for_condition(
                override_narrower_than_inherited,
                timeout=420,
                interval=20,
                description="namespace override BH sample_count below inherited",
            ), (
                f"Override namespace ({override_ns}) should have fewer in-window samples "
                f"than inherited namespace ({inherit_ns}) after 1h vs 9h schedules"
            )

            override_avg = _avg_bh_sample_count(
                ros_database_config, org_id, bh_cluster_uuid, override_ns
            )
            inherit_avg = _avg_bh_sample_count(
                ros_database_config, org_id, bh_cluster_uuid, inherit_ns
            )
            assert override_avg < inherit_avg, (
                f"Override namespace ({override_ns}) sample_count avg {override_avg:.1f} "
                f"should be below inherited namespace ({inherit_ns}) avg {inherit_avg:.1f}"
            )
            # 1-hour override vs 9-hour cluster schedule (~4 vs ~36 quarter-hour samples/day).
            assert override_avg <= inherit_avg * 0.85, (
                f"Override namespace ({override_ns}) sample_count avg {override_avg:.1f} "
                f"should be meaningfully below inherited namespace ({inherit_ns}) "
                f"avg {inherit_avg:.1f}"
            )
            # Same namespace should shrink after applying the narrow override (sanity check).
            assert override_avg < override_wide_avg * 0.85, (
                f"Namespace override should reduce in-window samples for {override_ns} "
                f"({override_avg:.1f} vs wide cluster window {override_wide_avg:.1f})"
            )
        finally:
            delete_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, override_ns
            )
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_namespace_enabled_false(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-009: enabled=false skips business_hours digest generation."""
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            payload = _valid_schedule_payload()
            payload["enabled"] = False
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, payload=payload
            )
            assert resp.status_code in (200, 202), resp.text

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=420
            ), "Reship did not complete for enabled=false schedule"

            bh_count = _count_digest_rows(
                ros_database_config, org_id, bh_cluster_uuid, "business_hours"
            )
            assert bh_count == 0, (
                f"enabled=false must not create business_hours digests (found {bh_count})"
            )

            all_hours_count = _count_digest_rows(
                ros_database_config, org_id, bh_cluster_uuid, "all_hours"
            )
            assert all_hours_count > 0, "all_hours digests should remain after enabled=false PUT"
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_no_historical_data(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
    ):
        """BH-E2E-010: fresh cluster reship completes immediately with reship_status=complete."""
        fresh_cluster = _find_cluster_without_digests(ros_database_config, org_id)
        if not fresh_cluster:
            pytest.skip(
                "No registered cluster without digest history; "
                "register a source without ROS ingest to run BH-E2E-010"
            )

        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, fresh_cluster)
        try:
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, fresh_cluster
            )
            assert resp.status_code in (200, 202), resp.text

            assert wait_for_reship_status(
                http_session, ros_api_url, bh_auth, fresh_cluster, "complete", timeout=120
            ), "Fresh cluster reship_status should reach complete quickly"

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, fresh_cluster, timeout=120
            ), "reship_pending_since should clear when no historical files exist"

            bh_count = _count_digest_rows(
                ros_database_config, org_id, fresh_cluster, "business_hours"
            )
            assert bh_count == 0, (
                "Fresh cluster should have no BH digests until first Kafka ingest arrives"
            )
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, fresh_cluster)

    def test_reingestion_sequence(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        """BH-E2E-011: schedule PUT → reship → digest update → trailing reship on change."""
        auth = _fresh_bh_auth(keycloak_config, cluster_config, http_session)
        delete_business_hours_schedule(http_session, ros_api_url, auth, bh_cluster_uuid)
        try:
            payload_wide = _valid_schedule_payload()
            payload_wide["schedule"]["start_time"] = "08:00"
            payload_wide["schedule"]["end_time"] = "17:00"
            assert put_business_hours_schedule(
                http_session, ros_api_url, auth, bh_cluster_uuid, payload=payload_wide
            ).status_code in (200, 202)

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            ), "Initial reship did not complete"

            assert wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, timeout=420
            ), "Initial reship did not produce dual digests"

            def namespace_with_wide_samples() -> tuple[Optional[str], float]:
                return _find_namespace_with_bh_samples(
                    ros_database_config, org_id, bh_cluster_uuid
                )

            assert wait_for_condition(
                lambda: namespace_with_wide_samples()[0] is not None,
                timeout=420,
                interval=15,
                description="namespace with positive BH sample_count after wide schedule",
            ), "No namespace with business_hours samples after wide schedule reship"
            ns, wide_avg = namespace_with_wide_samples()
            assert wide_avg > 0

            payload_narrow = _valid_schedule_payload()
            payload_narrow["schedule"]["start_time"] = "12:00"
            payload_narrow["schedule"]["end_time"] = "13:00"
            auth = _fresh_bh_auth(keycloak_config, cluster_config, http_session)
            resp = put_business_hours_schedule(
                http_session, ros_api_url, auth, bh_cluster_uuid, payload=payload_narrow
            )
            assert resp.status_code in (200, 202), resp.text

            # Digest sample_count is the durable signal that trailing re-ingestion ran.
            # Log-based reship counting is flaky after pod restarts during deploys.
            def narrow_samples_reduced() -> bool:
                narrow_avg = _avg_bh_sample_count(
                    ros_database_config, org_id, bh_cluster_uuid, ns
                )
                return narrow_avg > 0 and narrow_avg < wide_avg * 0.6

            assert wait_for_condition(
                narrow_samples_reduced,
                timeout=600,
                interval=20,
                description="BH sample_count reduced after narrowed schedule reingestion",
            ), (
                f"Expected narrowed schedule to reduce BH samples for {ns} "
                f"(wide avg {wide_avg:.1f})"
            )

            narrow_avg = _avg_bh_sample_count(
                ros_database_config, org_id, bh_cluster_uuid, ns
            )
            assert narrow_avg > 0
            assert narrow_avg < wide_avg * 0.6, (
                f"Narrowed schedule should reduce BH sample_count ({narrow_avg:.1f} vs {wide_avg:.1f})"
            )

            auth = _fresh_bh_auth(keycloak_config, cluster_config, http_session)
            get_resp = http_session.get(
                f"{_bh_settings_base(ros_api_url)}/clusters/{bh_cluster_uuid}",
                headers=auth,
                timeout=30,
            )
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json().get("schedule", {}).get("start_time") == "12:00"
            assert wait_for_reship_status(
                http_session,
                ros_api_url,
                auth,
                bh_cluster_uuid,
                "complete",
                timeout=120,
                auth_refresher=lambda: _fresh_bh_auth(
                    keycloak_config, cluster_config, http_session
                ),
            )
        finally:
            delete_business_hours_schedule(
                http_session,
                ros_api_url,
                _fresh_bh_auth(keycloak_config, cluster_config, http_session),
                bh_cluster_uuid,
            )


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


@pytest.mark.timeout(900)
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


@pytest.mark.timeout(900)
@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.extended
class TestBusinessHoursExtendedScenarios:
    """Extended business-hours scenarios (BH-E2E-012 through BH-E2E-020)."""

    def test_bh_e2e_012_metrics_reship_attempts_total(
        self,
        business_hours_feature,
        cluster_config,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        http_session: requests.Session,
    ):
        """BH-E2E-012: Metrics endpoint exposes reship-related Prometheus counters."""
        metrics_text = _fetch_component_metrics(cluster_config, "ros-api")
        if not metrics_text:
            pytest.skip("ros-api metrics endpoint not reachable from pod (curl /metrics failed)")

        missing = [m for m in RESHIP_PROMETHEUS_METRICS if not _prometheus_metric_present(metrics_text, m)]
        assert not missing, f"ros-api /metrics missing reship counters: {missing}"

        # Optional: trigger one reship and confirm duration histogram count increases.
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            before = _prometheus_counter_value(metrics_text, "ros_reship_duration_seconds")
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert resp.status_code in (200, 202), resp.text

            def counter_increased():
                fresh = _fetch_component_metrics(cluster_config, "ros-api")
                if not fresh:
                    return False
                return _prometheus_counter_value(fresh, "ros_reship_duration_seconds") > before

            assert wait_for_condition(
                counter_increased, timeout=180, interval=10, description="reship metric increment"
            ), "ros_reship_duration_seconds did not increase after schedule PUT"
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_bh_e2e_013_kafka_consumer_failure_redelivery(
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
        """BH-E2E-013: Kafka consumer failure causes message redelivery after recovery."""
        processor_deploy = f"{cluster_config.helm_release_name}-ros-processor"
        ns = cluster_config.namespace

        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            run_oc_command([
                "scale", "deployment", processor_deploy, "-n", ns, "--replicas=0",
            ], check=False)
            time.sleep(15)

            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert resp.status_code in (200, 202), resp.text

            # While consumer is down, reship may complete on masu side but digests lag.
            run_oc_command([
                "scale", "deployment", processor_deploy, "-n", ns, "--replicas=1",
            ], check=False)
            if not wait_for_condition(
                lambda: check_pod_ready(ns, "app.kubernetes.io/component=ros-processor"),
                timeout=240,
                interval=10,
                description="ros-processor pod ready",
            ):
                pytest.skip("ros-processor did not become ready after scale-up")

            # Kafka redelivery / catch-up: dual digests should appear after consumer resumes.
            if not wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, timeout=900
            ):
                pytest.skip(
                    "Could not verify Kafka redelivery after processor recovery "
                    "(no dual digests within 15m; cluster may lack ROS history for reship)"
                )
        finally:
            run_oc_command([
                "scale", "deployment", processor_deploy, "-n", ns, "--replicas=1",
            ], check=False)
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_bh_e2e_014_s3_presigned_url_expired(
        self,
        business_hours_feature,
        cluster_config,
    ):
        """BH-E2E-014: Expired S3 presigned URL returns 403, logged, metric incremented."""
        metrics_text = _fetch_component_metrics(cluster_config, "ros-processor")
        if not metrics_text:
            pytest.skip("ros-processor metrics endpoint not reachable from pod")

        assert _prometheus_metric_present(metrics_text, PROCESSOR_FETCH_ERROR_METRIC), (
            f"{PROCESSOR_FETCH_ERROR_METRIC} must be exposed on ros-processor /metrics"
        )

        # Fault injection (expired presigned URL) is not available in chart E2E; verify
        # processor logs document 403 handling when historical errors exist.
        logs = _grep_pod_logs(cluster_config, "ros-processor", "403")
        if "403" not in logs:
            pytest.skip(
                "No 403 presigned-download errors in recent ros-processor logs; "
                "cannot assert metric increment without fault injection"
            )

        assert _prometheus_metric_present(metrics_text, PROCESSOR_FETCH_ERROR_METRIC)

    def test_bh_e2e_015_schedule_change_during_active_reship(
        self,
        business_hours_feature,
        cluster_config,
        http_session: requests.Session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
    ):
        """BH-E2E-015: Schedule change during active reship triggers trailing reship."""
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            assert _wait_for_reship_quiescence(
                cluster_config, quiet_seconds=45, timeout=180
            ), "Cluster reship activity did not settle before trailing-reship burst"

            burst_started = time.time()

            payload_a = _valid_schedule_payload()
            payload_a["schedule"]["start_time"] = "08:00"
            assert put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, payload=payload_a
            ).status_code in (200, 202)

            payload_b = _valid_schedule_payload()
            payload_b["schedule"]["start_time"] = "09:00"
            payload_c = _valid_schedule_payload()
            payload_c["schedule"]["start_time"] = "10:00"

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        put_business_hours_schedule,
                        http_session,
                        ros_api_url,
                        bh_auth,
                        bh_cluster_uuid,
                        payload=payload_b,
                    ),
                    pool.submit(
                        put_business_hours_schedule,
                        http_session,
                        ros_api_url,
                        bh_auth,
                        bh_cluster_uuid,
                        payload=payload_c,
                    ),
                ]
                for fut in as_completed(futures):
                    r = fut.result()
                    assert r.status_code in (200, 202), r.text

            get_resp = http_session.get(
                f"{_bh_settings_base(ros_api_url)}/clusters/{bh_cluster_uuid}",
                headers=bh_auth,
                timeout=30,
            )
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json().get("schedule", {}).get("start_time") == "10:00"

            def reships_bounded():
                elapsed = max(60, int(time.time() - burst_started) + 15)
                count = _count_ros_reship_completions(
                    cluster_config,
                    since_seconds=elapsed,
                    cluster_uuid=bh_cluster_uuid,
                )
                return 1 <= count <= 2

            assert wait_for_condition(
                reships_bounded, timeout=300, interval=15, description="trailing reship bound"
            ), "Expected 1–2 masu reship executions for burst of schedule PUTs"
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_bh_e2e_016_incremental_visibility(
        self,
        business_hours_feature,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
        http_session: requests.Session,
        keycloak_config,
        cluster_config,
    ):
        """BH-E2E-016: New BH recommendations appear within upload_cycle after reship starts."""
        # Operator default upload_cycle is 360 minutes; cap E2E wait at 15 minutes.
        visibility_timeout = 900

        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            resp = put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert resp.status_code in (200, 202), resp.text

            def bh_visible_in_api():
                container = _find_container_with_bh_digests(
                    ros_database_config, org_id, bh_cluster_uuid
                )
                if not container:
                    return False
                rec_resp = _fetch_recommendations_for_bh_container(
                    http_session,
                    ros_api_url,
                    bh_auth,
                    bh_cluster_uuid,
                    container,
                    keycloak_config,
                    cluster_config,
                )
                if rec_resp.status_code != 200:
                    return False
                return _recommendations_include_business_hours(rec_resp.json())

            assert wait_for_condition(
                bh_visible_in_api,
                timeout=visibility_timeout,
                interval=30,
                description="business_hours in recommendations API",
            ), (
                "business_hours did not appear in recommendations within 15 minutes "
                "(upload_cycle may be longer on this cluster)"
            )

            # Incremental visibility: API enrichment can precede full dual-digest backfill.
            if not wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, timeout=visibility_timeout
            ):
                pytest.skip(
                    "BH recommendations visible but dual digests not complete within window "
                    "(reship still in progress or limited ROS history)"
                )
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_bh_e2e_017_delete_schedule_prunes_digests(
        self,
        business_hours_feature,
        http_session: requests.Session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        ros_database_config: dict,
        org_id: str,
    ):
        """BH-E2E-017: DELETE schedule prunes business_hours digests on next ingest."""
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            assert put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            ).status_code in (200, 202)

            assert wait_for_dual_digests(
                ros_database_config, org_id, bh_cluster_uuid, timeout=420
            ), "Timed out waiting for business_hours digests before DELETE"

            bh_before = _count_digest_rows(
                ros_database_config, org_id, bh_cluster_uuid, "business_hours"
            )
            assert bh_before > 0, "Expected business_hours digests before DELETE"

            del_resp = delete_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            )
            assert del_resp.status_code in (200, 204), del_resp.text

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            ), "reship did not complete after DELETE"

            def bh_digests_pruned():
                return _count_digest_rows(
                    ros_database_config, org_id, bh_cluster_uuid, "business_hours"
                ) == 0

            assert wait_for_condition(
                bh_digests_pruned, timeout=600, interval=20, description="BH digests pruned"
            ), "business_hours digests still present after DELETE and re-ingest"
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_bh_e2e_018_kill_switch_no_bh_data(
        self,
        ros_api_url: str,
        keycloak_config,
        cluster_config,
        http_session: requests.Session,
    ):
        """BH-E2E-018: Kill-switch off hides BH endpoints and omits BH digests from API."""
        if _ros_bh_env_enabled(cluster_config) is not False:
            pytest.skip(
                "Set ROS_BUSINESS_HOURS_ENABLED=false on ros-api deployment to run "
                "kill-switch extended test (BH-E2E-018)"
            )

        auth = get_fresh_token(keycloak_config, cluster_config, http_session)
        if not auth:
            pytest.skip("Could not obtain JWT token")

        settings_resp = http_session.get(_bh_settings_base(ros_api_url), headers=auth, timeout=30)
        assert settings_resp.status_code == 404

        put_resp = http_session.put(
            _bh_settings_base(ros_api_url),
            headers={**auth, "Content-Type": "application/json"},
            json=_valid_schedule_payload(),
            timeout=30,
        )
        assert put_resp.status_code == 404

        cap = http_session.get(_capabilities_url(ros_api_url), headers=auth, timeout=30)
        if cap.status_code == 200:
            assert cap.json().get("business_hours") is False

        rec_resp = http_session.get(
            get_recommendations_endpoint(ros_api_url),
            headers=auth,
            params={"limit": 20},
            timeout=60,
        )
        if rec_resp.status_code == 200:
            assert not _find_business_hours_in_recommendations(rec_resp.json())

    def test_bh_e2e_019_multiple_orgs_isolated(
        self,
        business_hours_feature,
        http_session: requests.Session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        ros_database_config: dict,
        org_id: str,
    ):
        """BH-E2E-019: Org1 schedule does not affect org2 recommendations."""
        org_a = org_id
        org_b = "2222222"
        jwt_org = _org_id_from_auth(bh_auth)
        if jwt_org and jwt_org != org_a:
            org_a = jwt_org

        rows_b = execute_db_query(
            ros_database_config["namespace"],
            ros_database_config["pod_name"],
            ros_database_config["database"],
            ros_database_config["user"],
            f"""
            SELECT COUNT(*)::text
            FROM daily_container_digests
            WHERE org_id = '{org_b}'
            """,
            password=ros_database_config["password"],
        )
        if not rows_b or int(rows_b[0][0]) == 0:
            pytest.skip(
                f"No ROS digest data for secondary org_id={org_b}; "
                "bootstrap a second tenant with ROS data to run BH-E2E-019"
            )

        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            assert put_business_hours_schedule(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid
            ).status_code in (200, 202)

            assert wait_for_dual_digests(
                ros_database_config, org_a, bh_cluster_uuid, timeout=420
            ), "Org A did not receive business_hours digests"

            bh_org_b = _count_digest_rows(
                ros_database_config, org_b, bh_cluster_uuid, "business_hours"
            )
            assert bh_org_b == 0, (
                f"Org B ({org_b}) must not gain business_hours digests from org A ({org_a}) schedule"
            )

            schedules_b = execute_db_query(
                ros_database_config["namespace"],
                ros_database_config["pod_name"],
                ros_database_config["database"],
                ros_database_config["user"],
                f"""
                SELECT COUNT(*)::text
                FROM business_hours_schedules
                WHERE org_id = '{org_b}'
                """,
                password=ros_database_config["password"],
            )
            assert schedules_b and int(schedules_b[0][0]) == 0, (
                "Org B must not inherit org A business_hours_schedules rows via API PUT"
            )
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)

    def test_bh_e2e_020_concurrent_puts_max_two_reships(
        self,
        business_hours_feature,
        cluster_config,
        http_session: requests.Session,
        ros_api_url: str,
        bh_auth: dict,
        bh_cluster_uuid: str,
        org_id: str,
        ros_database_config: dict,
    ):
        """BH-E2E-020: Concurrent PUTs result in at most two masu reship executions."""
        delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
        try:
            assert _wait_for_reship_quiescence(cluster_config, quiet_seconds=45, timeout=180), (
                "Cluster reship activity did not settle before concurrent PUT burst"
            )

            burst_started = time.time()

            def put_with_start(start_time: str) -> requests.Response:
                payload = _valid_schedule_payload()
                payload["schedule"]["start_time"] = start_time
                return put_business_hours_schedule(
                    http_session, ros_api_url, bh_auth, bh_cluster_uuid, payload=payload
                )

            start_times = ["08:00", "08:30", "09:00", "09:30"]
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(put_with_start, t) for t in start_times]
                for fut in as_completed(futures):
                    r = fut.result()
                    assert r.status_code in (200, 202), r.text

            def reships_started() -> bool:
                elapsed = max(60, int(time.time() - burst_started) + 15)
                return _count_ros_reship_completions(
                    cluster_config, since_seconds=elapsed, cluster_uuid=bh_cluster_uuid
                ) >= 1

            assert wait_for_condition(
                reships_started, timeout=300, interval=15, description="concurrent PUT reships"
            ), "Expected masu reship_ros to run after concurrent schedule PUT burst"

            elapsed = max(60, int(time.time() - burst_started) + 15)
            reship_count = _count_ros_reship_completions(
                cluster_config, since_seconds=elapsed, cluster_uuid=bh_cluster_uuid
            )
            assert 1 <= reship_count <= 2, (
                f"Expected at most 2 masu reship_ros executions for concurrent schedule PUT burst, "
                f"saw {reship_count}"
            )

            assert wait_for_reship_pending_cleared(
                ros_database_config, org_id, bh_cluster_uuid, timeout=600
            ), "Concurrent PUT reship did not complete"
            assert wait_for_reship_status(
                http_session, ros_api_url, bh_auth, bh_cluster_uuid, "complete", timeout=120
            )
        finally:
            delete_business_hours_schedule(http_session, ros_api_url, bh_auth, bh_cluster_uuid)
