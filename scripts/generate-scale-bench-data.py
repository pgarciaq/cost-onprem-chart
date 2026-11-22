#!/usr/bin/env python3
"""Generate NISE scale-bench tarball on the hypervisor (no VPN upload of CSVs).

Uses performance profiles from tests/suites/performance/profiles.py.

Example (on dell-r640-082 hypervisor):
  python3 scripts/generate-scale-bench-data.py --profile medium --days 15
  python3 scripts/generate-scale-bench-data.py --profile small --days 15 --cluster-id <uuid>
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml  # PyYAML: preferred — parse the exact config nise reads
except ImportError:  # pragma: no cover - hypervisor fallback
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from suites.performance.profiles import PROFILES, get_profile_nise_yaml  # noqa: E402


def expected_container_ids(profile_name: str) -> int:
    profile = PROFILES[profile_name]
    return (
        profile["nodes_per_cluster"]
        * profile["namespaces_per_cluster"]
        * profile["pods_per_namespace"]
    )


def _inner(wrap: dict, key: str) -> dict:
    """Unwrap ``- key: {...}`` list items, accepting both YAML styles.

    Nested style: ``- node:\\n    node_name: …`` parses as ``{'node': {…}}``.
    Flat style (what the perf profiles emit): ``- node:\\n  node_name: …``
    parses as ``{'node': None, 'node_name': …}`` — the key exists with a
    null value, so ``.get(key, wrap)`` returns None, not the fallback.
    """
    value = wrap.get(key)
    return value if isinstance(value, dict) else wrap


def interval_rows_from_yaml(cfg: dict) -> tuple[int, int]:
    """(pod-variant rows, namespace rows) emitted per quarter-hour interval.

    nise emits interval-major — ``for quarter_hour: for pod: yield`` — so
    every interval carries one row per pod (GPU pods yield one row per
    distinct gpu_uuid) and the namespace report carries one aggregated row
    per namespace (#773).
    """
    pod_rows = ns_rows = 0
    for generator in cfg.get("generators", []):
        for name, block in generator.items():
            if name != "OCPGenerator":
                continue  # VM generators emit other, non-hour-blocked types
            nodes = block.get("nodes", []) or []
            for node_wrap in nodes:
                node = _inner(node_wrap, "node")
                namespaces = node.get("namespaces", {}) or {}
                ns_rows += len(namespaces)
                for ns_body in namespaces.values():
                    for pod_wrap in ns_body.get("pods", []) or []:
                        pod = _inner(pod_wrap, "pod")
                        gpus = pod.get("gpus") or []
                        pod_rows += max(1, len(gpus))
    return pod_rows, ns_rows


def interval_rows_from_profile(profile: dict) -> tuple[int, int]:
    """Profile-derived fallback when PyYAML is unavailable.

    Accurate for the tracked perf profiles (uniform, GPU-less). The
    post-generation scan is the guard when this approximation is off.
    """
    pods = (
        profile["nodes_per_cluster"]
        * profile["namespaces_per_cluster"]
        * profile["pods_per_namespace"]
    )
    ns = profile["nodes_per_cluster"] * profile["namespaces_per_cluster"]
    return pods, ns


def alignment_hour_block(pod_rows: int, ns_rows: int) -> int:
    """Rows in one complete cluster-hour — the unit file splits must not cut.

    nise flushes a report file when its row accumulator hits the limit
    (``nise/report.py``), purely by row count. Because emission is
    interval-major, the alignment unit is the cluster's whole hour:
    4 quarter-hours x the per-interval row count. The per-interval count
    must simultaneously serve the container file (pod rows) and the
    namespace file (one row per namespace), hence the lcm.

    NOTE: the alignment unit is NOT the node-day from the original issue
    proposal (k x pods_per_node x 4 x 24) — that arithmetic assumed
    node-major emission; 7 x 13,248 is not a multiple of bench-v3's
    4,968-row cluster-hour and would keep splitting mid-hour (#894).
    """
    per_interval = math.lcm(max(pod_rows, 1), max(ns_rows, 1))
    return 4 * per_interval


def aligned_row_limit(hour_block: int, target: int = 100_000) -> int:
    """Largest k x hour_block not exceeding ~target rows per file."""
    k = max(1, target // max(hour_block, 1))
    return k * hour_block


def nise_command(nise_bin: str, yaml_path: Path, cluster_id: str, row_limit: int) -> list[str]:
    """The nise invocation, carrying the aligned --file-row-limit (#894)."""
    return [
        nise_bin,
        "report",
        "ocp",
        "--static-report-file",
        str(yaml_path),
        "--ocp-cluster-id",
        cluster_id,
        "-w",
        "--ros-ocp-info",
        "--file-row-limit",
        str(row_limit),
    ]


def _parse_interval_start(value: str):
    """Parse nise's interval_start, which is Go-formatted, not ISO.

    Real output: ``2026-11-01 06:15:01 +0000 UTC`` (the unit tests' ISO
    spelling is accepted too, but the Go format is what nise writes —
    the first scan implementation parsed only ISO and silently skipped
    every row, making itself vacuous on real output).
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z UTC", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def scan_hour_completeness(csv_paths, group_column: str) -> list[str]:
    """Report (group, date, hour) cells holding fewer than 4 intervals.

    nise emits quarter-hour rows; a complete hour carries 4 distinct
    interval_start values. A file split landing inside an hour leaves the
    boundary cell with a subset in the last file that wrote it — the
    processor upserts (org, cluster, group, report_date, hour) with
    overwrite semantics, so the earlier, larger subset is lost (#894).
    """
    violations = []
    for path in csv_paths:
        name = path.name if hasattr(path, "name") else str(path)
        seen: dict[tuple, set] = {}
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                ts = _parse_interval_start(row.get("interval_start", ""))
                if ts is None:
                    continue
                key = (name, row.get(group_column, ""), ts.date(), ts.hour)
                seen.setdefault(key, set()).add(row.get("interval_start", ""))
        for key in sorted(seen, key=str):
            if len(seen[key]) < 4:
                violations.append(f"{key[0]} {key[1]} {key[2]}T{key[3]:02d}: {len(seen[key])}/4 intervals")
    return violations


def find_nise_bin(nise_root: Path | None) -> str:
    candidates = [
        Path("/var/tmp/native-scale-bench-venv/bin/nise"),
        Path("/var/tmp/native-scale-bench/nise"),
    ]
    if nise_root:
        candidates.append(nise_root / ".venv" / "bin" / "nise")
    candidates.extend(
        [
            Path("/root/nise/.venv/bin/nise"),
            Path.home() / "dev/koku/nise/.venv/bin/nise",
        ]
    )
    for path in candidates:
        if path.is_file():
            return str(path)
    nise = subprocess.run(["which", "nise"], capture_output=True, text=True)
    if nise.returncode == 0 and nise.stdout.strip():
        return nise.stdout.strip()
    raise SystemExit(
        "nise not found. Install koku-nise on the hypervisor or set --nise-root to a nise checkout with .venv"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate ROS scale-bench NISE tarball")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES.keys()),
        default="medium",
        help="NISE performance profile (default: medium)",
    )
    parser.add_argument("--days", type=int, default=15, help="Report window in days (default: 15)")
    parser.add_argument(
        "--cluster-id",
        help="Cluster UUID (default: random). Must be a valid UUID for ROS kafka.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/var/tmp/native-scale-bench"),
        help="Output root on hypervisor (default: /var/tmp/native-scale-bench)",
    )
    parser.add_argument(
        "--nise-root",
        type=Path,
        default=Path("/root/nise"),
        help="Path to nise repo with .venv (default: /root/nise)",
    )
    parser.add_argument(
        "--tag",
        default="",
        help="Tarball filename tag (default: profile-days-timestamp)",
    )
    args = parser.parse_args()

    cluster_id = args.cluster_id or str(uuid.uuid4())
    try:
        uuid.UUID(cluster_id)
    except ValueError as exc:
        raise SystemExit(f"--cluster-id must be a valid UUID: {cluster_id}") from exc

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    start = end - timedelta(days=args.days)

    run_dir = args.work_dir / f"gen-{args.profile}-{args.days}d"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cluster_id.txt").write_text(cluster_id)

    yaml_path = run_dir / "static_report.yml"
    yaml_path.write_text(get_profile_nise_yaml(args.profile, start, end, cluster_id, 0))

    # #894: compute the file row limit so nise's splits land on cluster-hour
    # boundaries. The YAML is the preferred source (what nise actually
    # reads — works for custom configs too); the profile is the fallback
    # when PyYAML is unavailable on the generator host.
    if yaml is not None:
        with open(yaml_path) as fh:
            pod_rows, ns_rows = interval_rows_from_yaml(yaml.safe_load(fh) or {})
    else:
        pod_rows, ns_rows = interval_rows_from_profile(PROFILES[args.profile])
    hour_block = alignment_hour_block(pod_rows, ns_rows)
    row_limit = aligned_row_limit(hour_block)
    print(f"alignment: pod_rows/interval={pod_rows} ns_rows/interval={ns_rows} "
          f"hour_block={hour_block} file_row_limit={row_limit} ({row_limit // hour_block} hours/file)")

    nise_out = run_dir / "nise_output"
    nise_out.mkdir(exist_ok=True)
    nise_bin = find_nise_bin(args.nise_root if args.nise_root.exists() else None)

    print(f"profile={args.profile} expected_container_ids≈{expected_container_ids(args.profile)}")
    print(f"cluster_id={cluster_id}")
    print(f"date_range={start.date()}..{end.date()}")
    print(f"running: {nise_bin} report ocp ...")

    result = subprocess.run(
        nise_command(nise_bin, yaml_path, cluster_id, row_limit),
        cwd=nise_out,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        print(result.stdout[-3000:], file=sys.stderr)
        print(result.stderr[-3000:], file=sys.stderr)
        return result.returncode

    files = sorted(p for p in nise_out.iterdir() if p.is_file() and p.name != "manifest.json")
    if not files:
        raise SystemExit(f"No CSV files under {nise_out}")

    # #894: enforce the invariant, don't assume it — every node-hour in the
    # container CSVs and every namespace-hour in the namespace CSVs must
    # carry all 4 quarter-hour intervals. A violation means the row limit
    # lost alignment (config drift, GPU variants, heterogeneous blocks):
    # fail the generation loudly instead of shipping a fixture whose
    # boundary hours under-count.
    usage_files = [p for p in files if "ocp_ros_usage" in p.name]
    ns_usage_files = [p for p in files if "ocp_ros_namespace_usage" in p.name]
    violations = scan_hour_completeness(usage_files, "node")
    violations += scan_hour_completeness(ns_usage_files, "namespace")
    if violations:
        for v in violations[:20]:
            print(f"incomplete hour: {v}", file=sys.stderr)
        raise SystemExit(
            f"#894 alignment check failed: {len(violations)} incomplete hours across "
            f"{len(usage_files) + len(ns_usage_files)} files — the row limit "
            f"({row_limit}) lost cluster-hour alignment for this config"
        )
    print(f"#894 alignment check passed: 0 incomplete hours across "
          f"{len(usage_files)} usage + {len(ns_usage_files)} namespace files")

    cost_files = sorted(
        p.name
        for p in files
        if any(
            token in p.name
            for token in ("pod_usage", "node_label", "namespace_label", "storage", "gpu", "vm_usage", "snapshot")
        )
    )
    ros_files = sorted(
        p.name for p in files if "ros" in p.name.lower() or p.suffix == ".json"
    )
    manifest = {
        "uuid": str(uuid.uuid4()),
        "cluster_id": cluster_id,
        "date": end.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "files": cost_files,
        "resource_optimization_files": ros_files,
        "certified": True,
        "operator_version": "1.0.0",
        "daily_reports": False,
    }
    manifest_path = nise_out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    tag = args.tag or f"{args.profile}-{args.days}d"
    tarball = args.work_dir / f"scale-{tag}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=path.name)
        tar.add(manifest_path, arcname="manifest.json")

    meta = {
        "profile": args.profile,
        "days": args.days,
        "cluster_id": cluster_id,
        "tarball": str(tarball),
        "tarball_size_mb": round(tarball.stat().st_size / 1024 / 1024, 2),
        "csv_files": len(files),
        "expected_container_ids": expected_container_ids(args.profile),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "file_row_limit": row_limit,
        "alignment_hour_block": hour_block,
        "alignment_intervals": {"pod_rows": pod_rows, "ns_rows": ns_rows},
    }
    meta_path = args.work_dir / f"scale-{tag}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"tarball={tarball}")
    print(f"size_mb={meta['tarball_size_mb']}")
    print(f"meta={meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
