#!/usr/bin/env python3
"""Dispatch independent benchmark.yml runs with gh and the Python standard library."""

import argparse
import datetime as dt
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from urllib.parse import quote

WORKFLOW = "benchmark.yml"
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


class SuiteError(Exception):
    pass


class SuiteTimeout(SuiteError):
    pass


def write_json(path, value):
    """Atomic replacement keeps cleanup input readable after interruption."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Clock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)
    time = staticmethod(time.time)


class Gh:
    def __init__(self, clock=None, runner=subprocess.run):
        self.clock = clock or Clock()
        self.runner = runner
        self.deadline = float("inf")

    def api(self, endpoint, method="GET", payload=None, binary=False, timeout=45):
        remaining = self.deadline - self.clock.monotonic()
        if remaining <= 0:
            raise SuiteTimeout("total suite budget exhausted")
        command = ["gh", "api", endpoint, "--method", method]
        data = None
        if payload is not None:
            command += ["--input", "-"]
            data = json.dumps(payload).encode()
        try:
            result = self.runner(command, input=data, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=min(timeout, remaining),
                                 check=False)
        except subprocess.TimeoutExpired as error:
            raise SuiteTimeout(f"gh request timed out: {endpoint}") from error
        except OSError as error:
            raise SuiteError(f"cannot execute gh: {error}") from error
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise SuiteError(f"gh {method} {endpoint}: {message}")
        if binary:
            return result.stdout
        if not result.stdout.strip():
            return None
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("response must be an object")
            return value
        except (ValueError, UnicodeError) as error:
            raise SuiteError(f"invalid gh JSON for {endpoint}") from error


def validate_seed(receipt, repository, entry, commit):
    if not isinstance(receipt, dict):
        raise SuiteError("seed receipt must be an object")
    expected = {
        "schema_version": 1, "repository": repository,
        "provider": entry["provider"], "workload": entry["workload"],
        "series": entry["series"], "seed_id": entry["seed_id"],
        "run_id": entry["run_id"], "run_attempt": entry["run_attempt"], "commit": commit,
    }
    for key, value in expected.items():
        if receipt.get(key) != value or type(receipt.get(key)) is not type(value):
            raise SuiteError(f"seed receipt mismatch: {key}")
    if not SLUG.fullmatch(receipt["seed_id"]):
        raise SuiteError("unsafe seed_id")
    if entry["provider"] == "archil":
        template = receipt.get("template_id")
        try:
            if str(uuid.UUID(template)) != template:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as error:
            raise SuiteError("seed receipt requires a canonical template UUID") from error
    else:
        key = (f"ci-bench-v1-Linux-X64-{entry['workload']}-{entry['seed_id']}-"
               f"{entry['run_id']}-{entry['run_attempt']}")
        if receipt.get("cache_key") != key:
            raise SuiteError("seed receipt cache_key does not match exact source run")
    return receipt


def unpack_archive(data, destination):
    """Reject archive traversal, symlinks, duplicate paths, and zip bombs."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            total, names = 0, set()
            for item in archive.infolist():
                path = PurePosixPath(item.filename)
                total += item.file_size
                normalized = str(path)
                if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                        or not path.parts or ":" in item.filename
                        or (item.external_attr >> 16) & 0o170000 == 0o120000
                        or normalized in names or total > 128 * 1024 * 1024):
                    raise SuiteError("unsafe artifact archive")
                names.add(normalized)
            archive.extractall(destination)
    except (zipfile.BadZipFile, OSError, RuntimeError) as error:
        raise SuiteError(f"cannot extract artifact: {error}") from error


class Coordinator:
    def __init__(self, args, gh=None, clock=None, *, discovery_seconds=180,
                 queue_seconds=1200, runtime_seconds=5400, total_seconds=19800,
                 poll_seconds=10):
        self.args = args
        self.clock = clock or Clock()
        self.gh = gh or Gh(self.clock)
        self.root = Path(args.output)
        self.base = f"repos/{args.repository}"
        self.discovery_seconds = discovery_seconds
        self.queue_seconds = queue_seconds
        self.runtime_seconds = runtime_seconds
        self.poll_seconds = poll_seconds
        self.deadline = self.clock.monotonic() + total_seconds
        self.gh.deadline = self.deadline
        self.active = None
        self.pending = None
        self.suite = {
            "schema_version": 1, "repository": args.repository, "ref": args.ref,
            "series": args.series, "status": "running", "trials": self.plan(),
        }
        self.seeds = {
            "schema_version": 1, "repository": args.repository,
            "series": args.series, "seeds": [],
        }

    def plan(self):
        entries = []
        for scenario in ("cold", "seed", "warm"):
            for trial in range(1, (1 if scenario == "seed" else self.args.trials) + 1):
                providers = self.args.providers if trial % 2 else self.args.providers[::-1]
                for workload in self.args.workloads:
                    for provider in providers:
                        seed_id = f"{self.args.series}-{workload}-{provider}"
                        if not SLUG.fullmatch(seed_id):
                            raise SuiteError("series is too long for seed IDs")
                        entries.append({
                            "provider": provider, "workload": workload,
                            "scenario": scenario, "series": self.args.series, "trial": trial,
                            "seed_id": "" if scenario == "cold" else seed_id,
                            "template_id": "", "cache_key": "", "state": "planned",
                        })
        return entries

    def persist(self):
        for entry in self.suite["trials"]:
            entry["status"] = entry["state"]
        write_json(self.root / "suite.json", self.suite)
        write_json(self.root / "seeds.json", self.seeds)

    def check_budget(self):
        if self.clock.monotonic() >= self.deadline:
            raise SuiteTimeout("total suite budget exhausted")

    def pause(self, deadline=None):
        self.check_budget()
        remaining = min(self.deadline, deadline or self.deadline) - self.clock.monotonic()
        if remaining > 0:
            self.clock.sleep(min(self.poll_seconds, remaining))

    def setup(self):
        resolved = self.gh.api(f"{self.base}/commits/{quote(self.args.ref, safe='')}")
        commit = resolved.get("sha", "")
        if not SHA.fullmatch(commit):
            raise SuiteError("ref did not resolve to a full commit SHA")
        workflow = self.gh.api(f"{self.base}/actions/workflows/{WORKFLOW}")
        if workflow.get("path") != f".github/workflows/{WORKFLOW}":
            raise SuiteError("unexpected benchmark workflow path")
        self.workflow_id = workflow["id"]
        self.suite["commit"] = commit
        self.persist()

    def discover(self, entry, since):
        deadline = min(self.deadline, self.clock.monotonic() + self.discovery_seconds)
        name = "ci-bench/{provider}/{workload}/{scenario}/{series}/{trial}".format(**entry)
        while self.clock.monotonic() < deadline:
            # Walk creation-filtered pages rather than a latest-runs window, which
            # loses dispatches on busy repositories.
            matches, page = [], 1
            while True:
                self.check_budget()
                if self.clock.monotonic() >= deadline:
                    raise SuiteTimeout("dispatch discovery timed out")
                endpoint = (f"{self.base}/actions/workflows/{self.workflow_id}/runs"
                            f"?event=workflow_dispatch&created=%3E%3D{since}"
                            f"&per_page=100&page={page}")
                runs = self.gh.api(endpoint)["workflow_runs"]
                for run in runs:
                    if (run.get("display_title") == name
                            and run.get("workflow_id") == self.workflow_id
                            and run.get("event") == "workflow_dispatch"
                            and run.get("head_branch") == self.args.ref
                            and run.get("created_at", "") >= since):
                        matches.append(run)
                if len(runs) < 100:
                    break
                page += 1
            if len(matches) > 1:
                entry["discovery_matches"] = [run["id"] for run in matches]
                self.persist()
                raise SuiteError("multiple exact dispatch matches; refusing to choose")
            if matches:
                run = matches[0]
                if (type(run.get("id")) is not int or run["id"] <= 0
                        or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0):
                    raise SuiteError("invalid run identity")
                entry.update(run_id=run["id"], run_attempt=run.get("run_attempt", 1))
                entry["unresolved_dispatch"] = False
                self.active = entry
                self.persist()
                self.metadata(entry, run)
                self.verify_run(run, entry)
                return run
            self.pause(deadline)
        raise SuiteTimeout("dispatch discovery timed out")

    def verify_run(self, run, entry):
        if run.get("id") != entry["run_id"]:
            raise SuiteError("run identity changed")
        if run.get("head_sha") != self.suite["commit"]:
            raise SuiteError("child head_sha differs from resolved commit; ref may have moved")
        if run.get("run_attempt") != 1 or entry["run_attempt"] != 1:
            raise SuiteError("unexpected rerun attempt; refusing to use it")

    def run_dir(self, entry):
        return self.root / "runs" / f"{entry['run_id']}-{entry['run_attempt']}"

    def metadata(self, entry, run, jobs=None, error=None):
        path = self.run_dir(entry) / "metadata.json"
        existing = json.loads(path.read_text()) if path.exists() else {}
        existing.update({key: entry[key] for key in
                         ("provider", "workload", "scenario", "series", "trial",
                          "run_id", "run_attempt")})
        for key in ("head_sha", "run_started_at", "updated_at", "status", "conclusion", "html_url"):
            if key in run:
                existing[key] = run[key]
            else:
                existing.setdefault(key, None)
        existing.setdefault("jobs", [])
        if jobs is not None:
            existing["jobs"] = jobs
        if error:
            existing["collection_error"] = str(error)
        write_json(path, existing)

    def wait(self, entry, initial):
        attempt = f"{self.base}/actions/runs/{entry['run_id']}/attempts/{entry['run_attempt']}"
        queued_at, started_at, run = self.clock.monotonic(), None, initial
        while True:
            self.verify_run(run, entry)
            self.metadata(entry, run)
            if run["status"] == "completed":
                return run
            now = self.clock.monotonic()
            if run["status"] == "in_progress" and started_at is None:
                started_at = now
            if started_at is None and now - queued_at >= self.queue_seconds:
                raise SuiteTimeout("child queue/start timeout")
            if started_at is not None and now - started_at >= self.runtime_seconds:
                raise SuiteTimeout("child runtime exceeded")
            self.pause()
            run = self.gh.api(attempt)

    def collect(self, entry, run):
        errors, jobs = [], []
        try:
            page = 1
            while True:
                result = self.gh.api(
                    f"{self.base}/actions/runs/{entry['run_id']}/attempts/"
                    f"{entry['run_attempt']}/jobs?per_page=100&page={page}")
                jobs.extend(result["jobs"])
                if len(result["jobs"]) < 100:
                    break
                page += 1
        except SuiteTimeout:
            raise
        except SuiteError as error:
            errors.append(str(error))
        self.metadata(entry, run, jobs=jobs)
        artifacts = []
        try:
            page = 1
            while True:
                result = self.gh.api(f"{self.base}/actions/runs/{entry['run_id']}/artifacts"
                                     f"?per_page=100&page={page}")
                artifacts.extend(result["artifacts"])
                if len(result["artifacts"]) < 100:
                    break
                page += 1
            kinds = ("bench", "provision") if entry["provider"] == "archil" else ("bench",)
            for kind in kinds:
                name = f"{kind}-{entry['run_id']}-{entry['run_attempt']}"
                found = [a for a in artifacts if a["name"] == name and not a.get("expired")]
                if len(found) != 1:
                    errors.append(f"missing or duplicate artifact {name}")
                    continue
                try:
                    data = self.gh.api(f"{self.base}/actions/artifacts/{found[0]['id']}/zip",
                                       binary=True)
                    unpack_archive(data, self.run_dir(entry) / kind)
                    if kind == "bench" and entry["scenario"] == "seed" and run.get("conclusion") == "success":
                        self.remember_seed(entry)
                except SuiteTimeout:
                    raise
                except SuiteError as error:
                    errors.append(str(error))
        except SuiteTimeout:
            raise
        except SuiteError as error:
            errors.append(str(error))
        try:
            result = json.loads((self.run_dir(entry) / "bench" / "result.json").read_text())
            if not isinstance(result, dict):
                raise ValueError("result must be an object")
        except (OSError, ValueError) as error:
            errors.append(f"missing or invalid result.json: {error}")
        if errors:
            self.metadata(entry, run, error="; ".join(errors))
            raise SuiteError("; ".join(errors))

    def remember_seed(self, entry):
        try:
            receipt = json.loads((self.run_dir(entry) / "bench" / "seed.json").read_text())
        except (OSError, ValueError) as error:
            raise SuiteError(f"missing or invalid seed receipt: {error}") from error
        receipt = validate_seed(receipt, self.args.repository, entry, self.suite["commit"])
        if not any(seed["seed_id"] == receipt["seed_id"] for seed in self.seeds["seeds"]):
            self.seeds["seeds"].append(receipt)
            self.persist()

    def collect_partial(self, entry):
        """Recover uploaded artifacts after a polling failure without waiting."""
        if not self.active or self.clock.monotonic() >= self.deadline:
            return
        metadata = json.loads((self.run_dir(entry) / "metadata.json").read_text())
        # Never consume artifacts from a child whose commit/attempt was rejected.
        if metadata.get("head_sha") != self.suite["commit"] or entry["run_attempt"] != 1:
            return
        old_deadline = self.gh.deadline
        self.gh.deadline = min(self.deadline, self.clock.monotonic() + 20)
        try:
            self.collect(entry, metadata)
        except SuiteTimeout:
            raise
        except SuiteError as error:
            entry["partial_collection_error"] = str(error)
        finally:
            self.gh.deadline = old_deadline

    def cancel(self):
        if not self.active:
            return
        entry = self.active
        # Cancellation is run-scoped. Check latest first to avoid cancelling a
        # human rerun. The service provides no atomic attempt-scoped cancellation.
        old_deadline = self.gh.deadline
        self.gh.deadline = self.clock.monotonic() + 20
        try:
            latest = self.gh.api(f"{self.base}/actions/runs/{entry['run_id']}", timeout=10)
            if (entry["run_attempt"] == 1 and latest.get("run_attempt") == 1
                    and latest.get("status") != "completed"):
                self.gh.api(f"{self.base}/actions/runs/{entry['run_id']}/cancel",
                            method="POST", timeout=10)
                entry["cancellation_requested"] = True
        except (SuiteError, KeyboardInterrupt) as error:
            entry["cancellation_error"] = str(error) or "interrupted"
        finally:
            self.gh.deadline = old_deadline
            self.persist()

    def recover_dispatch(self):
        """A dispatch interrupted before discovery can still have created a run."""
        entry = self.pending
        if not entry or not entry.get("unresolved_dispatch"):
            return
        old_deadline, old_gh_deadline = self.deadline, self.gh.deadline
        old_discovery = self.discovery_seconds
        self.deadline = self.gh.deadline = self.clock.monotonic() + 10
        self.discovery_seconds = 10
        try:
            self.discover(entry, entry["dispatched_at"])
        except (SuiteError, KeyboardInterrupt) as error:
            entry["recovery_error"] = str(error) or "interrupted"
        finally:
            self.deadline, self.gh.deadline = old_deadline, old_gh_deadline
            self.discovery_seconds = old_discovery
            self.persist()

    def execute(self, entry):
        entry["state"] = "dispatching"
        entry["dispatched_at"] = dt.datetime.fromtimestamp(
            math.floor(self.clock.time()), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.persist()
        inputs = {key.replace("_", "-"): str(entry[key]) for key in
                  ("provider", "workload", "scenario", "series", "trial", "seed_id",
                   "template_id", "cache_key")}
        # A timed-out dispatch may still have reached GitHub. Cleanup must treat
        # it as live until discovery identifies the exact attempt.
        entry["unresolved_dispatch"] = True
        self.pending = entry
        self.persist()
        self.gh.api(f"{self.base}/actions/workflows/{self.workflow_id}/dispatches",
                    method="POST", payload={"ref": self.args.ref, "inputs": inputs})
        run = self.discover(entry, entry["dispatched_at"])
        entry["state"] = "running"
        self.persist()
        run = self.wait(entry, run)
        entry["conclusion"] = run["conclusion"]
        collection_error = None
        try:
            self.collect(entry, run)
        except SuiteTimeout:
            raise
        except SuiteError as error:
            collection_error = error
        # Preserve cleanup receipts even if another artifact/jobs request failed.
        if entry["scenario"] == "seed" and run["conclusion"] == "success":
            self.remember_seed(entry)
        if collection_error:
            raise collection_error
        if run["conclusion"] != "success":
            raise SuiteError(f"child conclusion: {run['conclusion']}")
        entry["state"] = "completed"
        self.active = None
        self.persist()

    def run(self):
        if (self.root / "suite.json").exists() or (self.root / "seeds.json").exists():
            raise SuiteError("output already contains a suite; use a fresh directory")
        self.persist()
        interrupted = False
        try:
            self.setup()
            for entry in self.suite["trials"]:
                self.check_budget()
                if entry["scenario"] == "warm":
                    source = next((seed for seed in self.seeds["seeds"]
                                   if seed["seed_id"] == entry["seed_id"]), None)
                    if source is None:
                        entry.update(state="skipped", error="no successful validated seed")
                        self.persist()
                        continue
                    if entry["provider"] == "archil":
                        entry["template_id"] = source["template_id"]
                    else:
                        entry["cache_key"] = source["cache_key"]
                try:
                    self.execute(entry)
                except SuiteTimeout as error:
                    entry.update(state="failed", error=str(error))
                    if self.active:
                        self.metadata(entry, {}, error=error)
                    # Do not dispatch another child after a timeout.
                    raise
                except SuiteError as error:
                    entry.update(state="failed", error=str(error))
                    if self.active:
                        self.metadata(entry, {}, error=error)
                        self.cancel()
                        self.collect_partial(entry)
                        self.metadata(entry, {}, error=error)
                    self.active = None
                    self.persist()
        except (KeyboardInterrupt, SuiteTimeout) as error:
            interrupted = True
            self.suite["error"] = str(error) or "interrupted"
            if self.pending and self.pending.get("unresolved_dispatch"):
                self.pending.update(state="failed", error=self.suite["error"])
            self.recover_dispatch()
            if self.active:
                self.active.update(state="failed", error=self.suite["error"])
                self.metadata(self.active, {}, error=self.suite["error"])
            self.cancel()
        except SuiteError as error:
            self.suite["error"] = str(error)
        finally:
            for entry in self.suite["trials"]:
                if entry["state"] in ("planned", "dispatching", "running"):
                    entry.update(state="skipped", error=self.suite.get("error", "suite stopped"))
            failed = any(e["state"] != "completed" for e in self.suite["trials"])
            self.suite["status"] = "interrupted" if interrupted else "failed" if failed else "completed"
            self.persist()
        return 130 if interrupted else 1 if failed else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--workloads", default="vue,hugo")
    parser.add_argument("--providers", default="github,archil")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--series")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("repository must be OWNER/REPO")
    if not args.ref or args.ref.startswith("-") or any(c.isspace() for c in args.ref):
        parser.error("ref must be a nonempty branch or tag")
    if not 1 <= args.trials <= 10:
        parser.error("trials must be 1..10")
    if not args.output.is_absolute():
        parser.error("output must be absolute")
    for name, choices in (("workloads", {"vue", "hugo"}), ("providers", {"github", "archil"})):
        values = getattr(args, name).split(",")
        if not values or len(set(values)) != len(values) or not set(values) <= choices:
            parser.error(f"invalid or duplicate {name}")
        setattr(args, name, values)
    if args.series is None:
        run = os.environ.get("GITHUB_RUN_ID", "local")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        args.series = f"s-{run}-{attempt}-{uuid.uuid4().hex[:12]}"
    if not SLUG.fullmatch(args.series) or len(args.series) > 100:
        parser.error("series must be a safe slug of at most 100 characters")
    return args


def interrupt(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        coordinator = Coordinator(parse_args(argv))
        result = coordinator.run()
        print(f"Suite {coordinator.suite['status']}: {coordinator.root / 'suite.json'}")
        return result
    except (SuiteError, OSError) as error:
        print(f"suite: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
