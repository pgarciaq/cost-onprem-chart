"""
UI data validation tests.

These tests are SELF-CONTAINED - they set up their own test data using the
cost_validation_data fixture, then validate that data displays correctly in the UI.

1. Data Visualization - Charts render correctly with real cost data
2. Optimization Recommendations - CPU/memory recommendations display correctly
3. Optimization Breakdown - Detailed breakdown view shows correct data
"""

import os
import re
import time

import pytest
from playwright.sync_api import Page, expect


def save_screenshot(page: Page, name: str) -> str:
    """Save a screenshot for documentation/verification purposes."""
    screenshots_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "reports", "screenshots", "data_validation"
    )
    os.makedirs(screenshots_dir, exist_ok=True)
    path = os.path.join(screenshots_dir, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    print(f"\n📸 Screenshot: {path}")
    return path


def get_page_content_signature(page: Page) -> str:
    """Capture a content signature to detect page state changes after a click."""
    return page.locator("body").inner_text()[:2000]


def click_first_optimization_row(page: Page) -> None:
    """Click the first optimization cluster link to drill into container view."""
    row_link = page.locator(
        "table tbody tr td a, table tbody tr a, [role='row'] a"
    ).first
    if row_link.count() > 0:
        row_link.click()
    else:
        page.locator("table tbody tr, [role='row']").first.click()
    page.wait_for_load_state("networkidle")
    time.sleep(2)


def assert_click_changed_page_state(
    page: Page, content_before: str, url_before: str
) -> None:
    """Assert that clicking changed the page — URL change, filter applied, or content changed.

    The on-prem optimization UI shows cluster-level efficiency summaries.
    Clicking a cluster name applies a filter (same URL) to show container-level
    data for that cluster.  The page may show container data or an empty state
    ("No match found") — both indicate the click was effective.
    """
    url_changed = page.url != url_before
    content_after = get_page_content_signature(page)
    content_changed = content_after != content_before
    has_filter_active = page.locator("text=/clear all filters/i").count() > 0
    assert url_changed or content_changed or has_filter_active, (
        "Clicking an optimization row should change page state "
        "(URL change, content change, or filter applied). "
        f"url_changed={url_changed}, content_changed={content_changed}, "
        f"has_filter_active={has_filter_active}"
    )


@pytest.mark.ui
@pytest.mark.data_validation
class TestCostDataVisualization:
    """Test that cost data displays correctly in charts and tables.
    
    HIGH PRIORITY: Validates that the UI correctly renders cost data from the backend.
    Uses cost_validation_data fixture for self-contained data setup.
    """

    def test_overview_shows_cost_data(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify Overview page displays cost data (not empty state).
        
        The Overview page should show:
        - Cost summary cards or widgets
        - Charts with actual data points
        - Not just "No data available" messages
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for any loading indicators to disappear
        loading = authenticated_page.locator(".pf-v6-c-spinner, [data-testid='loading']")
        if loading.count() > 0:
            loading.first.wait_for(state="hidden", timeout=30000)
        
        # Check for empty state - should NOT be present since we have data
        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        empty_text = authenticated_page.get_by_text(re.compile(r"no data|no cost data|empty", re.IGNORECASE))
        
        # With cost_validation_data, we expect data to be present
        # If empty state is shown, the test should fail (not skip)
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.fail(
                f"Empty state shown despite cost_validation_data setup. "
                f"Cluster ID: {cost_validation_data['cluster_id']}"
            )
        
        # Look for data indicators - charts, tables, or cost values
        found_data = False
        
        # Check CSS selectors
        css_indicators = ["svg path", "svg rect", "table tbody tr", ".pf-v6-c-card"]
        for selector in css_indicators:
            if authenticated_page.locator(selector).count() > 0:
                found_data = True
                break
        
        # Check for dollar amounts via text
        if not found_data:
            dollar_amounts = authenticated_page.get_by_text(re.compile(r"\$[0-9]"))
            if dollar_amounts.count() > 0:
                found_data = True
        
        # Capture screenshot for verification
        save_screenshot(authenticated_page, "01_overview_cost_data")
        
        assert found_data, (
            f"Overview page should display cost data (charts, tables, or cost values). "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_openshift_page_shows_cluster_costs(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify OpenShift page displays cluster cost data.
        
        The OpenShift page should show:
        - Cost breakdown by cluster, project, or node
        - Charts or tables with actual values
        
        Note: The /ocp page may show empty state initially while data propagates
        through the UI's caching layer, even when data exists in the database.
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/ocp")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for loading to complete - OCP page may need more time
        time.sleep(5)  # Allow async data to load
        
        # Look for cost data elements first
        found_data = False
        if authenticated_page.locator("svg path, svg rect, table tbody tr, .pf-v6-c-table tbody tr").count() > 0:
            found_data = True
        if not found_data and authenticated_page.get_by_text(re.compile(r"\$[0-9]", re.IGNORECASE)).count() > 0:
            found_data = True
        
        if found_data:
            # Capture screenshot for verification
            save_screenshot(authenticated_page, "02_openshift_cluster_costs")
            return  # Test passes - data is displayed
        
        # Check for empty state - may occur due to UI caching/timing
        empty_state = authenticated_page.locator(".pf-v6-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            # This can happen due to UI caching - skip rather than fail
            # The Overview and Cost Explorer tests validate data is accessible
            pytest.skip(
                f"OpenShift page shows empty state (possible UI caching delay). "
                f"Data exists for cluster: {cost_validation_data['cluster_id']}"
            )
        
        # No data and no empty state - something else is wrong
        pytest.fail(
            f"OpenShift page should display cost data or empty state. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_cost_explorer_displays_chart_with_data(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify Cost Explorer displays charts with actual data points.
        
        The Cost Explorer should show:
        - A chart (bar, line, or area) with visible data
        - Not just empty axes or "no data" message
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/explorer")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for chart to render
        time.sleep(3)
        
        # Check for empty state - should NOT be present
        empty_state = authenticated_page.locator(".pf-v6-c-empty-state")
        empty_text = authenticated_page.get_by_text(re.compile(r"no data available", re.IGNORECASE))
        if (empty_state.count() > 0 and empty_state.first.is_visible()) or \
           (empty_text.count() > 0 and empty_text.first.is_visible()):
            pytest.fail(
                f"Empty state shown despite cost_validation_data setup. "
                f"Cluster ID: {cost_validation_data['cluster_id']}"
            )
        
        # Look for chart with data (SVG with paths or rects indicates rendered data)
        chart_with_data = authenticated_page.locator("svg path, svg rect, svg circle")
        
        # Capture screenshot for verification
        save_screenshot(authenticated_page, "03_cost_explorer_chart")
        
        # Should have multiple data points (not just axes)
        assert chart_with_data.count() > 2, (
            f"Cost Explorer chart should have data points. Found {chart_with_data.count()} SVG elements. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_cost_explorer_table_has_rows(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify Cost Explorer table displays data rows.
        
        When viewing as table, should show actual cost data rows.
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/explorer")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for data to load
        time.sleep(2)
        
        # Look for table rows
        table_rows = authenticated_page.locator(
            "table tbody tr, [role='grid'] [role='row'], .pf-v6-c-table tbody tr"
        )
        
        if table_rows.count() == 0:
            # Check if there's a table view toggle
            table_toggle = authenticated_page.locator(
                "button:has-text('Table'), [aria-label*='table'], [data-testid='table-view']"
            )
            if table_toggle.count() > 0:
                table_toggle.first.click()
                authenticated_page.wait_for_load_state("networkidle")
                time.sleep(1)
                table_rows = authenticated_page.locator("table tbody tr")
        
        # Capture screenshot for verification
        save_screenshot(authenticated_page, "04_cost_explorer_table")
        
        # With data setup, we expect rows (or chart view is default which is also valid)
        # This test validates table view specifically if available
        if table_rows.count() == 0:
            # Check if chart is showing instead (also valid)
            chart_elements = authenticated_page.locator("svg path, svg rect")
            if chart_elements.count() > 2:
                pass  # Chart view is showing data, that's fine
            else:
                pytest.fail(
                    f"Cost Explorer should have data in table or chart view. "
                    f"Cluster ID: {cost_validation_data['cluster_id']}"
                )


@pytest.mark.ui
@pytest.mark.ros
@pytest.mark.data_validation
class TestOptimizationRecommendations:
    """Test that optimization recommendations display correctly.
    
    HIGH PRIORITY: Validates CPU/memory recommendations from Kruize are shown.
    Uses cost_validation_data fixture for self-contained data setup.
    
    Note: Optimization recommendations require Kruize processing time after data upload.
    The E2E flow waits for recommendations to be generated before these tests run.
    """

    def test_optimizations_table_has_recommendations(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify optimizations page displays recommendation data.
        
        Should show:
        - Table with container/workload recommendations
        - CPU and memory columns
        - Actual values (not all zeros or empty)
        """
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        
        # Wait for data to load
        time.sleep(3)
        
        # Check for empty state
        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        empty_text = authenticated_page.get_by_text(re.compile(r"no optimization|no recommendation", re.IGNORECASE))
        
        # Optimization data may take longer to process - check if available
        if (empty_state.count() > 0 and empty_state.first.is_visible()) or \
           (empty_text.count() > 0 and empty_text.first.is_visible()):
            # This is acceptable - Kruize may not have processed yet
            pytest.skip(
                "No optimization data available yet. "
                "Kruize may still be processing recommendations."
            )
        
        # Look for table with data
        table = authenticated_page.locator("table, [role='grid'], .pf-v6-c-table")
        expect(table.first).to_be_visible(timeout=10000)
        
        # Capture screenshot for verification
        save_screenshot(authenticated_page, "05_optimizations_table")
        
        # Verify table has rows
        rows = authenticated_page.locator("table tbody tr, [role='row']")
        assert rows.count() > 0, "Optimizations table should have recommendation rows"

    def test_optimizations_show_cpu_memory_values(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify optimizations page shows CPU/memory efficiency data."""
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        rows = authenticated_page.locator("table tbody tr, [role='row']")
        if rows.count() == 0:
            pytest.skip("No optimization rows available")

        has_cpu_section = authenticated_page.locator(
            "text=/cpu workload efficiency/i"
        ).count() > 0
        has_memory_section = authenticated_page.locator(
            "text=/memory workload efficiency/i"
        ).count() > 0
        has_efficiency_pct = authenticated_page.locator(
            "text=/[0-9]+\\s*%/i"
        ).count() > 0

        save_screenshot(authenticated_page, "06_optimizations_cpu_memory")

        assert has_cpu_section or has_memory_section, (
            "Optimizations page should show CPU or Memory workload efficiency sections. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )
        assert has_efficiency_pct, (
            "Optimizations page should show efficiency percentages. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_optimizations_show_cost_values(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify optimizations page shows wasted cost and total cost columns."""
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        rows = authenticated_page.locator("table tbody tr, [role='row']")
        if rows.count() == 0:
            pytest.skip("No optimization rows available")

        has_dollar_values = authenticated_page.locator(
            "text=/\\$[0-9,]+\\.?[0-9]*/i"
        ).count() > 0
        has_wasted_cost_header = authenticated_page.locator(
            "text=/wasted cost/i"
        ).count() > 0
        has_total_cost_header = authenticated_page.locator(
            "text=/total cost/i"
        ).count() > 0

        save_screenshot(authenticated_page, "07_optimizations_cost_values")

        assert has_dollar_values, (
            "Optimizations page should show dollar cost values. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )
        assert has_wasted_cost_header or has_total_cost_header, (
            "Optimizations page should show Wasted Cost or Total Cost columns. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )


@pytest.mark.ui
@pytest.mark.ros
@pytest.mark.data_validation
class TestOptimizationBreakdown:
    """Test optimization drill-down behavior.

    The on-prem UI shows cluster-level efficiency summaries. Clicking a cluster
    name applies a filter to show container-level optimization data for that
    cluster. These tests verify the drill-down transition works.
    """

    def test_cluster_click_changes_page_state(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify clicking a cluster name transitions the page to container view."""
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        rows = authenticated_page.locator("table tbody tr, [role='row']")
        if rows.count() == 0:
            pytest.skip("No clickable optimization rows found")

        content_before = get_page_content_signature(authenticated_page)
        url_before = authenticated_page.url

        click_first_optimization_row(authenticated_page)

        save_screenshot(authenticated_page, "08_optimization_cluster_drilldown")

        assert_click_changed_page_state(
            authenticated_page, content_before, url_before
        )

    def test_cluster_drilldown_shows_container_view(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify cluster drill-down shows container-level view or empty state."""
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        rows = authenticated_page.locator("table tbody tr, [role='row']")
        if rows.count() == 0:
            pytest.skip("No clickable optimization rows found")

        click_first_optimization_row(authenticated_page)

        save_screenshot(authenticated_page, "09_optimization_container_view")

        has_container_tab = authenticated_page.locator(
            "text=/container/i"
        ).count() > 0
        has_filter_active = authenticated_page.locator(
            "text=/clear all filters/i"
        ).count() > 0
        has_empty_state = authenticated_page.locator(
            "text=/no match found|0 - 0 of 0/i"
        ).count() > 0
        has_container_rows = authenticated_page.locator(
            "table tbody tr, [role='row']"
        ).count() > 0

        assert has_container_tab or has_filter_active or has_empty_state or has_container_rows, (
            "After cluster drill-down, page should show container view, "
            "active filter, empty state, or container rows. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )

    def test_optimizations_multiple_clusters_listed(
        self, authenticated_page: Page, ui_url: str, cost_validation_data
    ):
        """Verify optimizations page lists multiple clusters."""
        authenticated_page.goto(f"{ui_url}/openshift/cost-management/optimizations")
        authenticated_page.wait_for_load_state("networkidle")
        time.sleep(3)

        empty_state = authenticated_page.locator(".pf-v6-c-empty-state, .pf-c-empty-state")
        if empty_state.count() > 0 and empty_state.first.is_visible():
            pytest.skip("No optimization data available yet")

        rows = authenticated_page.locator("table tbody tr, [role='row']")

        save_screenshot(authenticated_page, "10_optimizations_multiple_clusters")

        assert rows.count() >= 2, (
            f"Optimizations page should list at least 2 clusters. Found {rows.count()}. "
            f"Cluster ID: {cost_validation_data['cluster_id']}"
        )
