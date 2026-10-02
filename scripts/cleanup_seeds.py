#!/usr/bin/env python3
"""Evict only this suite's terminal, successful seed resources."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from trial import SHA, UUID, positive, slug


def load(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"oversized cleanup input: {path.name}")
    return json.loads(path.read_text())


def api(run, path, *args):
    response = run(
        ["gh", "api", path, *args], check=True, capture_output=True,
        text=True, timeout=30,
    )
    return json.loads(response.stdout) if response.stdout.strip() else None


def validate_receipt(receipt, repository, series):
    if receipt.get("schema_version") != 1 or receipt.get("repository") != repository:
        raise ValueError("seed receipt belongs to a different repository/schema")
    if receipt.get("series") != series:
        raise ValueError("seed receipt belongs to a different suite")
    slug(series, "series")
    provider, workload = receipt.get("provider"), receipt.get("workload")
    if provider not in {"github", "archil"} or workload not in {"vue", "hugo"}:
        raise ValueError("unknown seed provider/workload")
    seed_id = slug(receipt.get("seed_id", ""), "seed ID")
    run_id = positive(receipt.get("run_id"), "seed run ID")
    attempt = positive(receipt.get("run_attempt"), "seed attempt")
    if not SHA.fullmatch(receipt.get("commit", "")):
        raise ValueError("seed receipt has no exact commit")
    if provider == "github":
        expected = f"ci-bench-v1-Linux-X64-{workload}-{seed_id}-{run_id}-{attempt}"
        if receipt.get("cache_key") != expected or receipt.get("template_id"):
            raise ValueError("archive cache does not belong to this seed attempt")
    elif not UUID.fullmatch(receipt.get("template_id", "")) or receipt.get("cache_key"):
        raise ValueError("invalid Archil seed template")
    if provider == "archil" and receipt.get("region", "aws-us-east-1") not in {
        "aws-us-east-1", "aws-us-west-2", "aws-eu-west-1",
    }:
        raise ValueError("unsupported seed region")
    return run_id, attempt


def terminal_attempt(run, repository, run_id, attempt):
    path = f"repos/{repository}/actions/runs/{run_id}"
    latest = api(run, path)
    if not isinstance(latest, dict) or str(latest.get("id")) != run_id or str(latest.get("run_attempt")) != attempt:
        raise ValueError("a child was rerun or its identity changed; keep immutable seeds")
    recorded = api(run, f"{path}/attempts/{attempt}")
    if not isinstance(recorded, dict) or str(recorded.get("id")) != run_id or str(recorded.get("run_attempt")) != attempt:
        raise ValueError("child attempt API returned an unexpected identity")
    if latest.get("status") != "completed" or recorded.get("status") != "completed":
        raise ValueError("a benchmark child is still active; keep immutable seeds")
    return recorded


def cleanup(directory, action_root=None, *, env=None, run=subprocess.run):
    env = dict(os.environ if env is None else env)
    receipt_path = directory / "seeds.json"
    if not receipt_path.exists():
        print("No successful seed receipts were recorded.")
        return []
    data = load(receipt_path)
    repository = env["GITHUB_REPOSITORY"]
    if data.get("schema_version") != 1 or data.get("repository") != repository:
        raise ValueError("suite receipts do not match this repository")
    seeds = data.get("seeds")
    if not isinstance(seeds, list) or len(seeds) > 4:
        raise ValueError("expected at most one seed per provider/workload")
    series = data.get("series", "")
    identities = set()
    for seed in seeds:
        identity = validate_receipt(seed, repository, series)
        if identity in identities:
            raise ValueError("duplicate seed receipt")
        identities.add(identity)
    suite = load(directory / "suite.json")
    if suite.get("repository") != repository or suite.get("series") != series:
        raise ValueError("suite manifest does not match seed receipts")
    trials = suite.get("trials", [])
    if not isinstance(trials, list):
        raise ValueError("malformed suite trials")
    if any(item.get("unresolved_dispatch") for item in trials):
        raise ValueError("a dispatch was not identified; keep seeds until children are reconciled")
    for item in trials:
        if item.get("run_id"):
            run_id = positive(item["run_id"], "trial run ID")
            attempt = positive(item["run_attempt"], "trial attempt")
            terminal_attempt(run, repository, run_id, attempt)
    results = []
    errors = []
    for seed in seeds:
        try:
            run_id, attempt = validate_receipt(seed, repository, series)
            creator = terminal_attempt(run, repository, run_id, attempt)
            if creator.get("conclusion") != "success":
                raise ValueError("seed creator did not complete successfully")
            if creator.get("head_sha") != seed["commit"] or creator.get("path") != ".github/workflows/benchmark.yml":
                raise ValueError("seed creator is not the expected benchmark workflow/commit")
            if seed["provider"] == "github":
                api(
                    run, f"repos/{repository}/actions/caches", "--method", "DELETE",
                    "-f", f"key={seed['cache_key']}",
                )
            else:
                if action_root is None:
                    raise ValueError("Archil eviction needs the pinned action checkout")
                cli = action_root / "src" / "cli.mjs"
                if not cli.is_file() or not env.get("ARCHIL_API_KEY") or not env.get("GH_RUNNER_ADMIN_TOKEN"):
                    raise ValueError("Archil cleanup CLI/credentials are missing")
                cleanup_env = {
                    **env, "GITHUB_RUN_ID": run_id, "GITHUB_RUN_ATTEMPT": attempt,
                    "GITHUB_SHA": seed["commit"], "GITHUB_OUTPUT": "",
                    "GITHUB_TOKEN": env.get("GH_TOKEN", ""),
                    "ARCHIL_CI_REGION": seed.get("region", "aws-us-east-1"),
                    "ARCHIL_CI_TEMPLATE_ID": "", "ARCHIL_CI_RETAIN_TEMPLATE": "false",
                    "ARCHIL_CI_GUEST_ENV": "{}",
                    "ARCHIL_CI_OUTPUT": str(directory / "cleanup" / seed["seed_id"]),
                }
                run(
                    ["node", str(cli), "cleanup"], env=cleanup_env,
                    check=True, timeout=300,
                )
            results.append({"seed_id": seed["seed_id"], "provider": seed["provider"], "outcome": "deleted"})
            print(f"Evicted {seed['provider']} seed {seed['seed_id']}.")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            errors.append(str(error))
            results.append({"seed_id": seed["seed_id"], "provider": seed["provider"], "outcome": "failure", "error": str(error)})
    (directory / "cleanup.json").write_text(json.dumps(results, indent=2) + "\n")
    if errors:
        raise ValueError("Seed cleanup incomplete: " + "; ".join(errors))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--action-root", type=Path)
    args = parser.parse_args()
    try:
        cleanup(args.directory, args.action_root)
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Seed cleanup refused or failed: {error}\n")


if __name__ == "__main__":
    main()
