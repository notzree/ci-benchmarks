#!/usr/bin/env python3
"""Validate dispatch inputs and publish successful immutable seed receipts."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")


def positive(value, name):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError(f"{name} must be a positive integer")
    return str(value)


def slug(value, name):
    if not SLUG.fullmatch(value):
        raise ValueError(f"{name} must be a 1–120 character safe slug")
    return value


def inputs(env):
    values = {
        name: env.get(f"CI_BENCH_{name.upper()}", "")
        for name in (
            "provider", "workload", "scenario", "series", "trial",
            "seed_id", "template_id", "cache_key",
        )
    }
    if values["provider"] not in {"github", "archil"}:
        raise ValueError("provider must be github or archil")
    if values["workload"] not in {"vue", "hugo"}:
        raise ValueError("workload must be vue or hugo")
    if values["scenario"] not in {"cold", "seed", "warm"}:
        raise ValueError("scenario must be cold, seed, or warm")
    slug(values["series"], "series")
    positive(values["trial"], "trial")
    run_id = positive(env["GITHUB_RUN_ID"], "run ID")
    attempt = positive(env["GITHUB_RUN_ATTEMPT"], "run attempt")
    if values["scenario"] == "cold":
        if any(values[key] for key in ("seed_id", "template_id", "cache_key")):
            raise ValueError("cold trials must not supply a seed or cache")
    else:
        if values["scenario"] == "seed" and not values["seed_id"]:
            values["seed_id"] = f"{run_id}-{attempt}-{values['workload']}-{values['provider']}"
        slug(values["seed_id"], "seed ID")
        prefix = f"ci-bench-v1-Linux-X64-{values['workload']}-{values['seed_id']}-"
        if values["scenario"] == "seed":
            if values["template_id"] or values["cache_key"]:
                raise ValueError("seeds must not reuse another seed")
            if values["provider"] == "github":
                values["cache_key"] = f"{prefix}{run_id}-{attempt}"
        elif values["provider"] == "github":
            if values["template_id"] or not re.fullmatch(
                re.escape(prefix) + r"[1-9][0-9]*-[1-9][0-9]*", values["cache_key"]
            ):
                raise ValueError("GitHub warm trials need the exact seed cache key")
        elif not UUID.fullmatch(values["template_id"]) or values["cache_key"]:
            raise ValueError("Archil warm trials need a template UUID and no archive cache")
    return values


def write_receipt(env, run=subprocess.run):
    provider = env["CI_BENCH_PROVIDER"]
    workload = env["CI_BENCH_WORKLOAD"]
    seed_id = slug(env["CI_BENCH_SEED_ID"], "seed ID")
    run_id = positive(env["GITHUB_RUN_ID"], "run ID")
    attempt = positive(env["GITHUB_RUN_ATTEMPT"], "run attempt")
    commit = env["GITHUB_SHA"]
    if not SHA.fullmatch(commit):
        raise ValueError("consumer commit must be a full SHA")
    directory = Path(env["BENCH_OUTPUT"])
    result_path = directory / "result.json"
    if result_path.stat().st_size > 1024 * 1024:
        raise ValueError("seed result is too large")
    result = json.loads(result_path.read_text())
    expected = {
        "provider": provider, "workload": workload, "scenario": "seed",
        "series": env["CI_BENCH_SERIES"], "seed_id": seed_id, "commit": commit,
        "run_id": run_id, "run_attempt": attempt,
    }
    if result.get("schema_version") != 1 or result.get("outcome") != "success" or any(
        str(result.get(key)) != value for key, value in expected.items()
    ):
        raise ValueError("seed result does not match this successful seed run")
    receipt = {
        "schema_version": 1, "repository": env["GITHUB_REPOSITORY"],
        **expected, "run_id": int(run_id), "run_attempt": int(attempt),
        "cache_key": "", "template_id": "",
    }
    if provider == "github":
        key = env["CI_BENCH_CACHE_KEY"]
        expected_key = f"ci-bench-v1-Linux-X64-{workload}-{seed_id}-{run_id}-{attempt}"
        if key != expected_key:
            raise ValueError("seed cache key does not match this run")
        response = run(
            ["gh", "api", "--method", "GET",
             f"repos/{receipt['repository']}/actions/caches",
             "-f", f"key={key}", "-f", "per_page=100"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        caches = json.loads(response.stdout).get("actions_caches", [])
        if len([cache for cache in caches if cache.get("key") == key]) != 1:
            raise ValueError("successful immutable archive cache was not published")
        receipt["cache_key"] = key
    elif provider == "archil":
        template_id = env["CI_BENCH_TEMPLATE_ID"]
        if not UUID.fullmatch(template_id):
            raise ValueError("seed template ID must be a UUID")
        receipt["template_id"] = template_id
        receipt["region"] = env.get("CI_BENCH_REGION", "aws-us-east-1")
    else:
        raise ValueError("unknown seed provider")
    temporary = directory / "seed.json.tmp"
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(directory / "seed.json")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["validate", "receipt"])
    args = parser.parse_args()
    try:
        if args.operation == "receipt":
            write_receipt(os.environ)
        else:
            values = inputs(os.environ)
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(f"seed_id={values['seed_id']}\ncache_key={values['cache_key']}\n")
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Trial validation failed: {error}\n")


if __name__ == "__main__":
    main()
