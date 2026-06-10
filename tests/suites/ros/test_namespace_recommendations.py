"""E2E tests for ROS namespace recommendation list and detail APIs."""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import pytest
import requests

from conftest import obtain_jwt_token
from e2e_helpers import (
    enable_ocp_tags,
    ensure_nise_available,
    generate_nise_data,
    get_koku_api_url,
    register_source,
    upload_with_retry,
    wait_for_provider,
    wait_for_summary_tables,
)
from suites.ros.test_container_detail import (
    _assert_container_list_engine_filter,
    _assert_paginated_envelope,
)
from suites.ros.test_recommendations import get_fresh_token
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    get_secret_value,
    wait_for_condition,
)

_STALE_DATA_NOTIFICATION_CODE = 2
_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_ros_0.yml"
_MIN_NAMESPACE_PROJECTS = 2
_INGEST_TIMEOUT = 720


def _wait_for_namespace_digest_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = _INGEST_TIMEOUT,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM daily_namespace_digests
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="daily_namespace_digests population",
    )


def _wait_for_tag_values(
    cluster_config,
    db_pod: str,
    org_id: str,
    tag_key: str,
    tag_value: str,
    timeout: int = 300,
) -> bool:
    schema = f"org{org_id}"

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "postgres",
            "postgres",
            f"""
            SELECT COUNT(*) FROM {schema}.reporting_ocptags_values
            WHERE key = '{tag_key}' AND value = '{tag_value}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=15,
        description=f"tag {tag_key}={tag_value} in reporting_ocptags_values",
    )


def _wait_for_joinable_namespace_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    min_projects: int,
    timeout: int = _INGEST_TIMEOUT,
) -> bool:
    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(DISTINCT ns.namespace_name)
            FROM namespace_recommendation_sets ns
            JOIN clusters c ON c.cluster_uuid = ns.cluster_uuid
            JOIN rh_accounts r ON r.id = c.tenant_id
            WHERE ns.org_id = '{org_id}'
              AND r.org_id = '{org_id}'
              AND ns.cluster_uuid = '{cluster_id}'
              AND ns.term IS NOT NULL
              AND ns.schedule_type = 'all_hours'
            """,
        )
        return result is not None and int(result[0][0]) >= min_projects

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="joinable namespace_recommendation_sets",
    )


def _namespaces_url(ros_api_url: str) -> str:
    return (
        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
        "recommendations/openshift/namespaces"
    )


def _namespace_detail_url(ros_api_url: str, recommendation_id: str) -> str:
    return f"{_namespaces_url(ros_api_url)}/{recommendation_id}"


def _namespace_history_url(ros_api_url: str, recommendation_id: str) -> str:
    return f"{_namespace_detail_url(ros_api_url, recommendation_id)}/history"


def _fetch_namespaces(
    session: requests.Session,
    ros_api_url: str,
    auth: dict[str, str],
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return session.get(
        _namespaces_url(ros_api_url),
        headers=auth,
        params=params or {},
        timeout=60,
    )


def _item_has_stale_data_notification(item: dict[str, Any]) -> bool:
    """True when STALE_DATA (code 2) appears on namespace list recommendations."""
    recs = item.get("recommendations") or {}
    top_notifs = recs.get("notifications") or {}
    for key, entry in top_notifs.items():
        if str(key) == str(_STALE_DATA_NOTIFICATION_CODE):
            return True
        if isinstance(entry, dict) and entry.get("code") == _STALE_DATA_NOTIFICATION_CODE:
            return True

    terms = recs.get("recommendation_terms") or {}
    for term in terms.values():
        if not isinstance(term, dict):
            continue
        term_notifs = term.get("notifications") or {}
        for key, entry in term_notifs.items():
            if str(key) == str(_STALE_DATA_NOTIFICATION_CODE):
                return True
            if isinstance(entry, dict) and entry.get("code") == _STALE_DATA_NOTIFICATION_CODE:
                return True
        engines = term.get("recommendation_engines") or {}
        for engine_key in ("cost", "performance"):
            eng = engines.get(engine_key) or {}
            codes = eng.get("notification_codes") or []
            if _STALE_DATA_NOTIFICATION_CODE in codes:
                return True
            for key, entry in (eng.get("notifications") or {}).items():
                if str(key) == str(_STALE_DATA_NOTIFICATION_CODE):
                    return True
                if isinstance(entry, dict) and entry.get("code") == _STALE_DATA_NOTIFICATION_CODE:
                    return True
    return False


def _item_is_stale(item: dict[str, Any]) -> bool:
    """Resolve staleness from item.stale when present, else STALE_DATA notifications."""
    if "stale" in item:
        stale_val = item.get("stale")
        if stale_val is None:
            return False
        return bool(stale_val)
    return _item_has_stale_data_notification(item)


def _item_is_fresh(item: dict[str, Any]) -> bool:
    """Non-stale: explicit stale=false/null or no STALE_DATA notification."""
    if "stale" in item:
        stale_val = item.get("stale")
        return stale_val is False or stale_val is None
    return not _item_has_stale_data_notification(item)


def _assert_namespace_items_cluster(
    items: list[dict[str, Any]], cluster_uuid: str
) -> None:
    for item in items:
        assert item.get("cluster_uuid") == cluster_uuid, (
            f"Expected cluster_uuid={cluster_uuid!r}, got {item.get('cluster_uuid')!r}"
        )


def _assert_namespace_items_fresh(items: list[dict[str, Any]]) -> None:
    for item in items:
        assert _item_is_fresh(item), (
            f"filter[stale]=false should exclude stale rows; got stale indicators on {item.get('id')!r}"
        )


def _assert_namespace_items_stale(items: list[dict[str, Any]]) -> None:
    for item in items:
        assert _item_is_stale(item), (
            f"filter[stale]=only should return only stale rows; item {item.get('id')!r} is not stale"
        )


def _namespace_project_name(item: dict[str, Any]) -> str:
    return item.get("project") or item.get("namespace") or ""


def _parse_last_reported(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _assert_sorted(
    items: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], Any],
    *,
    descending: bool,
) -> None:
    keys = [key_fn(item) for item in items]
    if len(keys) < 2:
        return
    for i in range(len(keys) - 1):
        left, right = keys[i], keys[i + 1]
        if descending:
            assert left >= right, f"Expected descending order; {left!r} before {right!r}"
        else:
            assert left <= right, f"Expected ascending order; {left!r} before {right!r}"


def _history_rows_match_engine(rows: list[dict[str, Any]], engine: str) -> None:
    for row in rows:
        assert row.get("recommendation_type") == engine, (
            f"filter[engine]={engine} must omit other engines; got {row.get('recommendation_type')!r}"
        )


def _history_rows_match_term(rows: list[dict[str, Any]], term: str) -> None:
    for row in rows:
        assert row.get("term") == term, (
            f"filter[term]={term} must omit other terms; got {row.get('term')!r}"
        )


@pytest.fixture(scope="module")
def ros_database_config(cluster_config, database_config):
    """ROS PostgreSQL database on the unified server."""
    secret_name = f"{cluster_config.helm_release_name}-db-credentials"
    user = get_secret_value(cluster_config.namespace, secret_name, "ros-user")
    password = get_secret_value(cluster_config.namespace, secret_name, "ros-password")
    if not user or not password:
        pytest.skip("ROS database credentials not found")

    return {
        "pod_name": database_config.pod_name,
        "namespace": database_config.namespace,
        "database": "costonprem_ros",
        "user": user,
        "password": password,
    }


def _seed_stale_namespace_cluster(
    ros_database_config: dict,
    org_id: str,
    cluster_uuid: str,
) -> None:
    """Mark a cluster as stale (>48h since last_reported_at) for filter[stale]=only tests."""
    execute_db_query(
        ros_database_config["namespace"],
        ros_database_config["pod_name"],
        ros_database_config["database"],
        ros_database_config["user"],
        f"""
        UPDATE clusters c
        SET last_reported_at = NOW() - interval '72 hours'
        FROM rh_accounts r
        WHERE r.id = c.tenant_id
          AND r.org_id = '{org_id}'
          AND c.cluster_uuid = '{cluster_uuid}'::uuid
        """,
        password=ros_database_config["password"],
    )


@pytest.fixture
def namespace_auth(keycloak_config, cluster_config, http_session):
    """Get a JWT token using the same client-credentials flow as container tests."""
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.fixture(scope="module", autouse=True)
def namespace_recommendation_seed_data(
    cluster_config,
    keycloak_config,
    ingress_url,
):
    """Upload ocp_report_ros_0 NISE data with namespace ROS CSVs before namespace API tests."""
    if not ensure_nise_available():
        pytest.skip("NISE is not available for namespace recommendation E2E")

    ingress_pod = get_pod_by_label(
        cluster_config.namespace, "app.kubernetes.io/component=ingress"
    )
    db_pod = get_pod_by_label(
        cluster_config.namespace, "app.kubernetes.io/component=database"
    )
    if not ingress_pod or not db_pod:
        pytest.skip("Ingress or database pod not found")

    cluster_id = str(uuid.uuid4())
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
        source_name=f"ns-rec-{cluster_id[-8:]}",
        container="ingress",
    )
    if not reg.source_id:
        pytest.fail(f"Failed to register source for namespace E2E cluster {cluster_id}")

    if not wait_for_provider(
        cluster_config.namespace, db_pod, cluster_id, timeout=300
    ):
        pytest.fail(f"Provider not created for namespace E2E cluster {cluster_id}")

    if not enable_ocp_tags(cluster_config, org_id=_UPLOAD_ORG_ID):
        pytest.fail("Failed to enable OCP tags via masu enabled_tags API")

    end_date = datetime.utcnow() - timedelta(days=1)
    start_date = end_date - timedelta(days=14)
    temp_dir = tempfile.mkdtemp(prefix="ns-rec-ingest-")

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
        pytest.fail("NISE did not generate pod_usage files for namespace E2E")
    if not ros_files:
        pytest.fail("NISE did not generate ros_usage files for namespace E2E")
    if not files.get("namespace_usage_files"):
        pytest.fail(
            "NISE did not generate ocp_ros_namespace_usage files; "
            "namespace tests require --ros-ocp-info namespace CSVs"
        )

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
    response = upload_with_retry(
        upload_session, upload_url, package_path, token.authorization_header
    )
    assert response.status_code in (200, 201, 202), response.text

    schema = wait_for_summary_tables(
        cluster_config.namespace,
        db_pod,
        cluster_id,
        timeout=420,
    )
    if not schema:
        pytest.fail("Summary tables not populated after namespace recommendation upload")

    if not _wait_for_namespace_digest_rows(
        cluster_config, db_pod, cluster_id, _UPLOAD_ORG_ID, timeout=_INGEST_TIMEOUT
    ):
        pytest.fail(
            "daily_namespace_digests not populated; namespace ROS CSV ingest failed"
        )

    if not _wait_for_joinable_namespace_rows(
        cluster_config,
        db_pod,
        cluster_id,
        _UPLOAD_ORG_ID,
        _MIN_NAMESPACE_PROJECTS,
        timeout=_INGEST_TIMEOUT,
    ):
        pytest.fail(
            f"Expected at least {_MIN_NAMESPACE_PROJECTS} joinable namespace "
            f"recommendations for cluster {cluster_id}"
        )

    if not _wait_for_tag_values(
        cluster_config,
        db_pod,
        _UPLOAD_ORG_ID,
        "environment",
        "production",
        timeout=300,
    ):
        pytest.fail(
            "reporting_ocptags_values missing environment=production after namespace ingest; "
            "check namespace_label CSV processing and enabled_tags"
        )


@pytest.mark.ros
@pytest.mark.integration
@pytest.mark.timeout(900)
class TestNamespaceRecommendationsE2E:
    """Namespace recommendation list and detail endpoints."""

    def test_namespace_list_returns_200(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """Smoke test: namespace list endpoint returns a valid paginated envelope (count may be 0)."""
        resp = _fetch_namespaces(http_session, ros_api_url, namespace_auth, {"limit": 10})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "meta" in body
        assert "data" in body
        assert isinstance(body["data"], list)

    def test_namespace_list_data_fields(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_namespaces(http_session, ros_api_url, namespace_auth, {"limit": 5})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        data = body.get("data") or []
        if not data:
            pytest.skip("No namespace recommendation data in cluster")

        item = data[0]
        namespace = item.get("project") or item.get("namespace")
        cluster = item.get("cluster_uuid") or item.get("cluster")
        assert namespace, "namespace list item must include project/namespace"
        assert cluster, "namespace list item must include cluster_uuid/cluster"
        recs = item.get("recommendations") or {}
        assert "recommendation_terms" in recs
        assert isinstance(recs["recommendation_terms"], dict)

    def test_namespace_detail_valid_id(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        list_resp = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 1}
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        if not items:
            pytest.skip("No namespace recommendation data in cluster")

        rec_id = items[0].get("id")
        assert rec_id, "namespace list item must include id"

        detail_resp = http_session.get(
            _namespace_detail_url(ros_api_url, rec_id),
            headers=namespace_auth,
            timeout=60,
        )
        assert detail_resp.status_code == 200, detail_resp.text
        body = detail_resp.json()
        assert body.get("id") == rec_id
        recs = body.get("recommendations") or {}
        assert "recommendation_terms" in recs

    def test_namespace_detail_not_found_returns_404(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        missing_id = str(uuid.uuid4())
        resp = http_session.get(
            _namespace_detail_url(ros_api_url, missing_id),
            headers=namespace_auth,
            timeout=60,
        )
        assert resp.status_code == 404, resp.text

    def test_namespace_filter_by_cluster(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        baseline = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 5}
        )
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        if not items:
            pytest.skip("No namespace recommendation data in cluster")

        cluster_uuid = items[0].get("cluster_uuid")
        if not cluster_uuid:
            pytest.skip("Namespace item missing cluster_uuid for filter test")

        filtered = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[cluster]": cluster_uuid, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        body = filtered.json()
        _assert_paginated_envelope(body)
        filtered_items = body.get("data") or []
        if not filtered_items:
            pytest.skip(f"No namespace recommendations for cluster {cluster_uuid}")

        _assert_namespace_items_cluster(filtered_items, cluster_uuid)

    def test_namespace_pagination(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """Keyset (cursor) pagination: page2 returns new results and respects limit."""
        limit = 3
        first = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": limit}
        )
        assert first.status_code == 200, first.text
        body1 = first.json()
        total = body1.get("meta", {}).get("count", 0)
        if total == 0:
            pytest.skip("No namespace recommendation data in cluster")
        if total <= limit:
            pytest.skip(f"Need more than {limit} namespace recommendations for pagination")

        links = body1.get("links", {})
        next_url = links.get("next")
        if not next_url:
            pytest.skip("No 'next' link in first page — single page result")

        # Build absolute URL from relative next link
        if not next_url.startswith("http"):
            base = _namespaces_url(ros_api_url)
            origin = base.rsplit("/api/", 1)[0]
            next_url = f"{origin}{next_url}"

        second = http_session.get(next_url, headers=namespace_auth, timeout=60)
        assert second.status_code == 200, second.text
        body2 = second.json()
        assert len(body2.get("data", [])) <= limit, "Page 2 exceeds limit"
        assert len(body2.get("data", [])) > 0, "Page 2 is empty despite total > limit"

        page1_ids = {item.get("id") for item in body1["data"]}
        page2_ids = {item.get("id") for item in body2["data"]}
        overlap = page1_ids & page2_ids
        assert not overlap, f"Keyset pagination returned duplicate IDs across pages: {overlap}"

    def test_namespace_filter_by_engine(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """filter[engine]=cost omits performance from recommendation_engines on each row."""
        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[engine]": "cost", "limit": 5},
        )
        assert resp.status_code == 200, resp.text
        _assert_container_list_engine_filter(resp.json(), "cost")

    def test_namespace_filter_engine_omission(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """filter[engine] returns only the selected engine under recommendation_engines."""
        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[engine]": "cost", "limit": 5},
        )
        assert resp.status_code == 200, resp.text
        _assert_container_list_engine_filter(resp.json(), "cost")

    def test_namespace_filter_stale_false(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """filter[stale]=false excludes stale rows (default list behavior)."""
        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[stale]": "false", "limit": 10},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []
        if not items:
            pytest.skip("No namespace recommendations for this cluster")

        _assert_namespace_items_fresh(items)

    def test_namespace_filter_stale_true(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """filter[stale]=true includes stale and fresh rows (count >= fresh-only list)."""
        fresh_resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[stale]": "false", "limit": 10},
        )
        assert fresh_resp.status_code == 200, fresh_resp.text
        fresh_count = fresh_resp.json().get("meta", {}).get("count", 0)

        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[stale]": "true", "limit": 10},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        inclusive_count = body.get("meta", {}).get("count", 0)
        assert inclusive_count >= fresh_count, (
            f"filter[stale]=true count ({inclusive_count}) should be >= "
            f"filter[stale]=false count ({fresh_count})"
        )

    def test_namespace_filter_stale_only(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
        ros_database_config: dict,
        org_id: str,
    ):
        """filter[stale]=only returns rows marked stale in the database."""
        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[stale]": "only", "limit": 10},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_paginated_envelope(body)
        items = body.get("data") or []

        if not items:
            baseline = _fetch_namespaces(
                http_session, ros_api_url, namespace_auth, {"limit": 5}
            )
            assert baseline.status_code == 200, baseline.text
            baseline_items = baseline.json().get("data") or []
            if not baseline_items:
                pytest.skip("No namespace recommendation data in cluster")

            cluster_uuid = (
                baseline_items[0].get("cluster_uuid") or baseline_items[0].get("cluster")
            )
            assert cluster_uuid, "namespace list item must include cluster_uuid"
            _seed_stale_namespace_cluster(ros_database_config, org_id, str(cluster_uuid))

            resp = _fetch_namespaces(
                http_session,
                ros_api_url,
                namespace_auth,
                {"filter[stale]": "only", "limit": 10},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            _assert_paginated_envelope(body)
            items = body.get("data") or []
            if not items:
                pytest.skip(
                    "No stale namespaces after seeding last_reported_at; "
                    "cluster may not be registered in ROS clusters table"
                )

        _assert_namespace_items_stale(items)

    def test_namespace_filter_tag(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """filter[tag:environment]=production matches namespaces with that Koku tag."""
        tag_key = "environment"
        tag_value = "production"
        baseline = _fetch_namespaces(http_session, ros_api_url, namespace_auth, {"limit": 100})
        assert baseline.status_code == 200, baseline.text
        unfiltered_count = baseline.json().get("meta", {}).get("count", 0)
        assert unfiltered_count > 0, "No namespace recommendation data in cluster"

        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {f"filter[tag:{tag_key}]": tag_value, "limit": 100},
        )
        if resp.status_code == 400:
            pytest.fail(f"Tag filtering rejected filter[tag:{tag_key}]={tag_value}: {resp.text}")
        assert resp.status_code == 200, resp.text
        filtered = resp.json()
        _assert_paginated_envelope(filtered)
        baseline_items = baseline.json().get("data") or []
        filtered_items = filtered.get("data") or []
        assert filtered_items, (
            f"No namespaces match filter[tag:{tag_key}]={tag_value}; "
            "enable ROS_TAGS_ENABLED, ROS_TAGS_SOURCE=db, and org1234567.reporting_ocptags_values "
            "in the ROS PostgreSQL database (costonprem_ros)"
        )
        if len(filtered_items) >= len(baseline_items):
            pytest.fail(
                f"Tag filter did not narrow list items ({len(filtered_items)} vs {len(baseline_items)}); "
                "check ROS_TAGS_ENABLED=true and reporting_ocptags_values for org1234567"
            )
        # meta.count should match the filtered page; ros-ocp-backend uses the filtered
        # distinct subquery count when tag (or other) filters are active.
        filtered_count = filtered.get("meta", {}).get("count", 0)
        assert filtered_count == len(filtered_items), (
            f"meta.count ({filtered_count}) should match returned items ({len(filtered_items)})"
        )
        # NISE ocp_report_ros_0.yml tags only project-ros-A1 with environment:production.
        for item in filtered_items:
            assert item.get("project") == "project-ros-A1"

    def test_namespace_history(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """Namespace history returns snapshots for a valid recommendation id."""
        list_resp = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 1}
        )
        assert list_resp.status_code == 200, list_resp.text
        items = list_resp.json().get("data") or []
        assert items, "No namespace recommendation data in cluster"

        rec_id = items[0].get("id")
        assert rec_id

        history_resp = http_session.get(
            _namespace_history_url(ros_api_url, rec_id),
            headers=namespace_auth,
            params={"limit": 30},
            timeout=60,
        )
        assert history_resp.status_code == 200, history_resp.text
        body = history_resp.json()
        rows = body.get("data") or []
        assert rows, "Namespace history must return at least one snapshot row"
        assert body.get("meta", {}).get("count", 0) >= len(rows)
        for row in rows:
            assert row.get("term"), "history row must include term"
            assert row.get("recommendation_type") in ("cost", "performance")
            assert row.get("resource") in ("cpu", "memory")

        cost_resp = http_session.get(
            _namespace_history_url(ros_api_url, rec_id),
            headers=namespace_auth,
            params={"filter[engine]": "cost", "limit": 30},
            timeout=60,
        )
        assert cost_resp.status_code == 200, cost_resp.text
        cost_rows = cost_resp.json().get("data") or []
        assert cost_rows, "Expected cost engine history rows"
        _history_rows_match_engine(cost_rows, "cost")

        term_resp = http_session.get(
            _namespace_history_url(ros_api_url, rec_id),
            headers=namespace_auth,
            params={"filter[term]": "short_term", "limit": 30},
            timeout=60,
        )
        assert term_resp.status_code == 200, term_resp.text
        term_rows = term_resp.json().get("data") or []
        assert term_rows, "Expected short_term history rows"
        _history_rows_match_term(term_rows, "short_term")

    def test_namespace_list_csv_export(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """format=csv returns text/csv with header and data rows."""
        resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"format": "csv", "limit": 10},
        )
        assert resp.status_code == 200, resp.text
        content_type = resp.headers.get("Content-Type", "")
        assert "text/csv" in content_type, f"Expected text/csv, got {content_type!r}"

        lines = [ln for ln in resp.text.strip().splitlines() if ln.strip()]
        assert len(lines) >= 2, "CSV export must include a header row and at least one data row"
        header = lines[0].lower()
        assert "cluster_uuid" in header
        assert "project" in header
        assert "recommendation_term" in header

    def test_namespace_order_by_last_reported(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """order_by=last_reported with order_how sorts by cluster last_reported."""
        for order_how, descending in (("asc", False), ("desc", True)):
            resp = _fetch_namespaces(
                http_session,
                ros_api_url,
                namespace_auth,
                {"order_by": "last_reported", "order_how": order_how, "limit": 20},
            )
            assert resp.status_code == 200, resp.text
            items = resp.json().get("data") or []
            assert len(items) >= 2, "Need multiple namespaces to verify sort order"
            _assert_sorted(
                items,
                lambda item: _parse_last_reported(item.get("last_reported") or ""),
                descending=descending,
            )

    def test_namespace_order_by_project(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """order_by=project with order_how sorts namespace names."""
        for order_how, descending in (("asc", False), ("desc", True)):
            resp = _fetch_namespaces(
                http_session,
                ros_api_url,
                namespace_auth,
                {"order_by": "project", "order_how": order_how, "limit": 20},
            )
            assert resp.status_code == 200, resp.text
            items = resp.json().get("data") or []
            assert len(items) >= 2, "Need multiple namespaces to verify sort order"
            _assert_sorted(
                items,
                lambda item: _namespace_project_name(item).lower(),
                descending=descending,
            )

    def test_namespace_filter_by_project(
        self,
        ros_api_url: str,
        namespace_auth: dict,
        http_session: requests.Session,
    ):
        """filter[project] returns only rows for the selected namespace."""
        baseline = _fetch_namespaces(
            http_session, ros_api_url, namespace_auth, {"limit": 5}
        )
        assert baseline.status_code == 200, baseline.text
        items = baseline.json().get("data") or []
        assert items, "No namespace recommendation data in cluster"

        project = _namespace_project_name(items[0])
        assert project, "namespace list item must include project/namespace"

        filtered = _fetch_namespaces(
            http_session,
            ros_api_url,
            namespace_auth,
            {"filter[project]": project, "limit": 20},
        )
        assert filtered.status_code == 200, filtered.text
        body = filtered.json()
        _assert_paginated_envelope(body)
        filtered_items = body.get("data") or []
        assert filtered_items, f"No namespaces returned for filter[project]={project!r}"
        for item in filtered_items:
            assert _namespace_project_name(item) == project, (
                f"filter[project]={project!r} must match every row; got {_namespace_project_name(item)!r}"
            )
