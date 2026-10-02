#!/usr/bin/env python3
"""Normalize benchmark evidence; only complete runner successes enter statistics."""

import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys


IDENTITY = ("provider", "workload", "scenario", "series", "trial", "run_id", "run_attempt")
PROVENANCE = ("provider", "workload", "scenario", "series", "commit", "upstream_sha",
              "fingerprint", "toolchains", "seed_id")
HARDWARE = ("architecture", "cpu_model", "logical_cpus", "memory_limit_bytes", "cpu_quota")
PHASE_METRICS = ("wall_seconds", "user_seconds", "system_seconds", "max_rss_kib")


def load_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected numeric metric")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}: metric must be finite and nonnegative")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def statistics_for(values):
    values = sorted(number(value, "statistics") for value in values)
    if not values:
        return dict(n=0, median=None, min=None, max=None, p95=None, population_stddev=None)
    return {"n": len(values), "median": statistics.median(values), "min": values[0],
            "max": values[-1], "p95": values[math.ceil(0.95 * len(values)) - 1],
            "population_stddev": statistics.pstdev(values)}


def elapsed(start, end, label):
    if start is None or end is None:
        return None
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if first.tzinfo is None or last.tzinfo is None:
            raise ValueError("timezone required")
        return number((last - first).total_seconds(), label)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{label}: invalid timestamp interval: {error}") from error


def workflow_metrics(metadata):
    metrics = {}
    wall = elapsed(metadata.get("run_started_at"), metadata.get("updated_at"), "workflow")
    if wall is not None:
        metrics["workflow.wall_seconds"] = wall
    expected = {"github": "GitHub-hosted benchmark", "archil": "Archil native benchmark"}
    jobs = metadata.get("jobs", [])
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError("metadata.jobs: expected list of objects")
    jobs = [job for job in jobs if job.get("name") == expected.get(metadata.get("provider"))]
    if len(jobs) > 1:
        raise ValueError("metadata: duplicate benchmark jobs")
    for job in jobs:
        wall = elapsed(job.get("started_at"), job.get("completed_at"), "benchmark job")
        if wall is not None:
            metrics["benchmark_job.wall_seconds"] = wall
        for name, metric in (("Restore immutable seed", "cache_restore.wall_seconds"),
                             ("Save immutable seed", "cache_save.wall_seconds")):
            steps = [step for step in job.get("steps", []) if step.get("name") == name]
            if len(steps) > 1:
                raise ValueError(f"metadata: duplicate step {name}")
            for step in steps:
                wall = elapsed(step.get("started_at"), step.get("completed_at"), name)
                if wall is not None:
                    metrics[metric] = wall
    return metrics


def validate_result(result, source):
    if result.get("schema_version") != 1:
        raise ValueError(f"{source}: unsupported schema_version")
    for key in (*IDENTITY, "commit", "upstream_sha", "fingerprint", "seed_id"):
        if key not in result:
            raise ValueError(f"{source}: missing {key}")
    for key, allowed in (("provider", {"github", "archil"}), ("workload", {"vue", "hugo"}),
                         ("scenario", {"cold", "seed", "warm"}),
                         ("outcome", {"success", "failure"})):
        if result.get(key) not in allowed:
            raise ValueError(f"{source}: invalid {key}")
    for key in ("toolchains", "system", "cache", "phases"):
        if not isinstance(result.get(key), dict):
            raise ValueError(f"{source}: {key} must be an object")
    for key in ("logical_cpus", "memory_limit_bytes", "cpu_quota"):
        if result["system"].get(key) is not None:
            number(result["system"][key], f"{source}: system.{key}")
    metrics, reasons = {}, []
    phases = result["phases"]
    required = {"install", "build", "probe", "total"}
    if result["workload"] == "vue":
        required.add("test")
    for name, phase in phases.items():
        if not isinstance(phase, dict):
            raise ValueError(f"{source}: phases.{name} must be an object")
        code = phase.get("exit_code")
        if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
            raise ValueError(f"{source}: phases.{name}.exit_code must be an integer")
        if code != 0:
            reasons.append(f"phase {name} exit_code={code}")
        for metric in PHASE_METRICS:
            if metric in phase:
                metrics[f"{name}.{metric}"] = number(phase[metric], f"{source}: {name}.{metric}")
    for name in sorted(required):
        if name not in phases or "wall_seconds" not in phases[name]:
            reasons.append(f"missing required phase timing: {name}")
    if result["outcome"] != "success":
        reasons.append("workload outcome failure")
    return metrics, reasons


def check_identity(result, metadata, source):
    for key in IDENTITY:
        if key not in metadata or str(result[key]) != str(metadata[key]):
            raise ValueError(f"{source}: result/metadata identity mismatch: {key}")
    if result["commit"] != metadata.get("head_sha"):
        raise ValueError(f"{source}: result/metadata identity mismatch: commit/head_sha")


def provision_metrics(value):
    metrics = {}
    if "totalProvisionSeconds" in value:
        metrics["provision.total_seconds"] = number(value["totalProvisionSeconds"],
                                                   "totalProvisionSeconds")
    timings = value.get("timings", {})
    if not isinstance(timings, dict):
        raise ValueError("native-provision.timings must be an object")
    for name, timing in timings.items():
        metrics[f"provision.{name}.wall_seconds"] = number(timing, f"provision.timings.{name}")
    return metrics


def record_key(record):
    return tuple(str(record.get(key)) for key in IDENTITY)


def collect(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"{directory}: results directory does not exist")
    metadata = {path.parent: (path, load_json(path))
                for path in sorted(directory.rglob("metadata.json"))}
    records, seen, represented = [], {}, set()
    for path in sorted(directory.rglob("result.json")):
        result = load_json(path)
        metrics, reasons = validate_result(result, path)
        ancestor = next((parent for parent in path.parents if parent in metadata), None)
        meta_path, meta = metadata[ancestor] if ancestor else (None, None)
        if meta is not None:
            check_identity(result, meta, path)
            represented.add(ancestor)
        provision = None
        for provision_path in sorted((ancestor or path.parent).rglob("native-provision.json")):
            candidate = load_json(provision_path)
            if provision is not None and canonical(candidate) != canonical(provision):
                raise ValueError(f"{path}: conflicting native-provision records")
            provision = candidate
        evidence = (path.read_bytes(), meta_path.read_bytes() if meta_path else None,
                    canonical(provision) if provision is not None else None)
        key = record_key(result)
        if key in seen:
            if evidence != seen[key]:
                raise ValueError(f"{path}: conflicting duplicate trial {key}")
            continue
        seen[key] = evidence
        workload_success = not reasons
        if meta is None:
            reasons.append("missing workflow metadata")
        else:
            metrics.update(workflow_metrics(meta))
            if meta.get("status") != "completed" or meta.get("conclusion") != "success":
                reasons.append("workflow not completed successfully")
            if meta.get("collection_error"):
                reasons.append(f"collection error: {meta['collection_error']}")
        if provision is not None:
            metrics.update(provision_metrics(provision))
        records.append({**{key: result[key] for key in PROVENANCE + ("trial", "run_id", "run_attempt")},
                        "hardware": {key: result["system"].get(key) for key in HARDWARE},
                        "cache": result["cache"], "workload_outcome": result["outcome"],
                        "workload_success": workload_success, "trial_success": not reasons,
                        "workflow_status": meta.get("status") if meta else None,
                        "workflow_conclusion": meta.get("conclusion") if meta else None,
                        "reasons": reasons, "metrics": metrics,
                        "source": str(path.relative_to(directory)),
                        "metadata_source": str(meta_path.relative_to(directory)) if meta_path else None,
                        "html_url": meta.get("html_url") if meta else None})
    for ancestor, (path, meta) in metadata.items():
        if ancestor in represented:
            continue
        record = {key: meta.get(key) for key in IDENTITY}
        record.update(commit=meta.get("head_sha"), workload_outcome=None,
                      workload_success=False, trial_success=False,
                      workflow_status=meta.get("status"), workflow_conclusion=meta.get("conclusion"),
                      reasons=["missing workload result"], metrics=workflow_metrics(meta),
                      source=str(path.relative_to(directory)), html_url=meta.get("html_url"))
        if meta.get("collection_error"):
            record["reasons"].append(f"collection error: {meta['collection_error']}")
        key, evidence = record_key(record), path.read_bytes()
        if key in seen:
            if seen[key] != evidence:
                raise ValueError(f"{path}: conflicting duplicate trial {key}")
            continue
        seen[key] = evidence
        records.append(record)
    suite_links, keys = [], {record_key(record) for record in records}
    for path in sorted(directory.rglob("suite.json")):
        suite = load_json(path)
        suite_links.append(str(path.relative_to(directory)))
        for trial in suite.get("trials", []):
            if not isinstance(trial, dict):
                raise ValueError(f"{path}: suite trials must be objects")
            if record_key(trial) in keys:
                continue
            record = {key: trial.get(key) for key in IDENTITY}
            record.update(commit=trial.get("commit", suite.get("commit")),
                          workload_success=False, trial_success=False, workload_outcome=None,
                          suite_status=trial.get("status"), workflow_status=None,
                          workflow_conclusion=None, metrics={},
                          reasons=[f"suite trial {trial.get('status', 'unknown')}: no collected result"],
                          source=str(path.relative_to(directory)),
                          unresolved_dispatch=trial.get("unresolved_dispatch"))
            records.append(record)
            keys.add(record_key(record))
    return records, suite_links


def summarize(records, suite_links):
    groups = {}
    for record in records:
        identity = {key: record.get(key) for key in PROVENANCE}
        identity["hardware"] = record.get("hardware")
        group = groups.setdefault(canonical(identity),
                                  {"identity": identity, "n_success": 0, "n_failure": 0,
                                   "metrics": {}, "trials": []})
        group["n_success" if record["trial_success"] else "n_failure"] += 1
        group["trials"].append(record["source"])
        if record["trial_success"]:
            for metric, value in record["metrics"].items():
                group["metrics"].setdefault(metric, []).append(value)
    for group in groups.values():
        group["metrics"] = {name: statistics_for(values)
                            for name, values in sorted(group["metrics"].items())}
    return {"schema_version": 1, "p95_method": "nearest-rank (ceil(0.95*n), 1-based)",
            "stddev_method": "population", "suite_sources": suite_links,
            "n_trials": len(records), "n_success": sum(r["trial_success"] for r in records),
            "n_failure": sum(not r["trial_success"] for r in records),
            "groups": list(groups.values()), "trials": records}


def csv_value(value):
    if isinstance(value, (dict, list)):
        value = canonical(value)
    if isinstance(value, str) and (
            value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fields})


def markdown_value(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def write_report(directory, output):
    records, links = collect(directory)
    summary = summarize(records, links)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n",
                                       encoding="utf-8")
    rows = [{**{key: value for key, value in record.items() if key != "metrics"},
             **record["metrics"]} for record in records]
    fields = sorted({key for row in rows for key in row}) or list(IDENTITY) + ["trial_success", "reasons"]
    write_csv(output / "trials.csv", rows, fields)
    stat_rows = []
    lines = ["# CI benchmark report", "",
             f"Trials: {summary['n_trials']}; successful: {summary['n_success']}; "
             f"failed/incomplete/skipped: {summary['n_failure']}.", "",
             "Only complete workload **and** workflow successes enter timing statistics. "
             "Collection errors exclude a trial even if GitHub reports success. "
             "Seed scenarios are separate preparation costs, never cold/warm samples.", "",
             "Groups preserve provenance, toolchains, seed identity and reported hardware; "
             "missing hardware is unknown, not evidence of resource equivalence. "
             "Matched workload commands do not imply matched CPU generation or cache mechanism. "
             "No winner or speedup is inferred, especially from a single trial.", "",
             "Workload `total.wall_seconds`, full `workflow.wall_seconds`, benchmark job, "
             "cache restore/save and native provisioning costs are distinct, potentially "
             "overlapping intervals; do not add them. Parallel watchdog jobs are not summed. "
             "Raw observed timings are retained in `trials.csv`, including failed trials, "
             "but failed timings do not enter successful medians.", "",
             "p95: nearest-rank, ceil(0.95 × n), 1-based. Standard deviation: population. "
             "Each metric has its own n; absent CPU/RSS aggregates are not zero.", ""]
    for index, group in enumerate(summary["groups"], 1):
        identity = group["identity"]
        lines.extend([f"## Group {index}", "", f"`{markdown_value(canonical(identity))}`", "",
                      f"n_success={group['n_success']}; n_failure={group['n_failure']}", "",
                      "| Metric | n | Median | Min | Max | p95 | Population stddev |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for metric, stats in group["metrics"].items():
            stat_rows.append({"group": index, **identity, "metric": metric, **stats,
                              "n_success": group["n_success"], "n_failure": group["n_failure"]})
            lines.append("| " + " | ".join(markdown_value(value) for value in
                                          (metric, stats["n"], stats["median"], stats["min"],
                                           stats["max"], stats["p95"], stats["population_stddev"])) + " |")
        if not group["metrics"]:
            lines.append("| No successful timing samples | 0 | — | — | — | — | — |")
            stat_rows.append({"group": index, **identity, "metric": None, **statistics_for([]),
                              "n_success": group["n_success"], "n_failure": group["n_failure"]})
        lines.append("")
    lines.extend(["## Failed, incomplete and uncollected trials", "",
                  "| Provider / workload / scenario | Run / attempt / trial | Status and reason | Evidence |",
                  "| --- | --- | --- | --- |"])
    for record in records:
        if not record["trial_success"]:
            lines.append("| " + " | ".join(markdown_value(value) for value in (
                "/".join(str(record.get(key)) for key in ("provider", "workload", "scenario")),
                "/".join(str(record.get(key)) for key in ("run_id", "run_attempt", "trial")),
                f"workload={record.get('workload_outcome')}; workflow={record.get('workflow_conclusion')}; "
                + "; ".join(record["reasons"]), record["source"])) + " |")
    if links:
        lines.extend(["", "Suite manifests: " + ", ".join(f"`{markdown_value(link)}`" for link in links)])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_csv(output / "summary.csv", stat_rows,
              ["group", *PROVENANCE, "hardware", "n_success", "n_failure", "metric",
               "n", "median", "min", "max", "p95", "population_stddev"])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        write_report(args.results_directory, args.output)
    except (ValueError, OSError) as error:
        parser.exit(2, f"report: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
