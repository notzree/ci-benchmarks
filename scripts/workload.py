#!/usr/bin/env python3
"""Provider-independent real upstream workloads; standard-library-only runner."""

import time

ENTRY_TIME = time.monotonic()

import argparse
import base64
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "benchmarks.json"
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
JSON_LIMIT = 128 * 1024


def manifest():
    return json.loads(MANIFEST.read_text())


def fingerprint():
    digest = hashlib.sha256()
    for path in [MANIFEST, REPO / "scripts/bootstrap.sh",
                 *sorted((REPO / "scripts").glob("workload*.py"))]:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def linux_x64():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("Only Linux x64 is supported")
    return "x86_64"


def no_symlink(path):
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise ValueError(f"Symlink path is not allowed: {component}")


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def cache_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("Cache root must be an absolute, non-destructive external directory")
    no_symlink(path)
    path = path.resolve()
    consumers = {REPO}
    if os.environ.get("GITHUB_WORKSPACE"):
        consumers.add(Path(os.environ["GITHUB_WORKSPACE"]).resolve())
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if path == home or path in home.parents or any(overlaps(path, p) for p in consumers):
        raise ValueError("Cache root overlaps home/consumer checkout or is a dangerous ancestor")
    if path.exists() and not path.is_dir():
        raise ValueError("Cache root is not a directory")
    return path


def output_path(value, root=None, seed=False):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("Output must be an absolute directory")
    no_symlink(path)
    path = path.resolve()
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if path == home or path in home.parents or path == REPO or path in REPO.parents:
        raise ValueError("Output must not be home or a consumer checkout ancestor")
    if root is not None and overlaps(path, root) and not (seed and path == root / "seed-output"):
        raise ValueError("Output must not overlap the cache root")
    path.mkdir(parents=True, exist_ok=True)
    for name in ("result.json", "result.csv", "error.log", "setup.log", "checkout.log",
                 "install.log", "build.log", "test.log", "probe.log"):
        no_symlink(path / name)
    return path


def atomic_json(path, value):
    no_symlink(path)
    payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        temp = Path(tmp.name)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def bounded_json(path):
    no_symlink(path)
    if not path.is_file():
        raise ValueError(f"Seed JSON is missing or not a regular file: {path.name}")
    with path.open("rb") as stream:
        payload = stream.read(JSON_LIMIT + 1)
    if len(payload) > JSON_LIMIT:
        raise ValueError(f"Oversized seed JSON: {path.name}")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate seed JSON key")
            result[key] = value
        return result
    value = json.loads(payload, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("Seed JSON must be an object")
    return value


def remove_tree(path):
    no_symlink(path)
    # Go makes module-cache directories read-only; cold must still clear them as runner.
    for directory, children, _ in os.walk(path, followlinks=False):
        os.chmod(directory, 0o700)
        children[:] = [name for name in children if not (Path(directory) / name).is_symlink()]
    shutil.rmtree(path)


def tools_for(workload):
    config = manifest()
    return {name: config["toolchains"][name]["version"]
            for name in config["workloads"][workload]["tools"]}


def identity(workload, root, seed_id):
    return {
        "schema_version": 1, "workload": workload, "seed_id": seed_id,
        "upstream_sha": manifest()["workloads"][workload]["upstream_sha"],
        "fingerprint": fingerprint(), "toolchains": tools_for(workload),
        "architecture": linux_x64(), "cache_root": str(root),
    }


def validate_seed(workload, root, seed_id):
    expected = identity(workload, root, seed_id)
    marker = bounded_json(root / "seed-marker.json")
    if set(marker) != set(expected) | {"outcome", "seed_result_sha256"}:
        raise ValueError("Invalid seed marker fields")
    for key, value in expected.items():
        if type(marker.get(key)) is not type(value) or marker.get(key) != value:
            raise ValueError(f"Seed marker mismatch: {key}")
    if marker["outcome"] != "success":
        raise ValueError("Seed was not successful")
    result_path = root / "seed-result.json"
    result = bounded_json(result_path)
    if hashlib.sha256(result_path.read_bytes()).hexdigest() != marker["seed_result_sha256"]:
        raise ValueError("Seed result checksum mismatch")
    for key in ("schema_version", "workload", "seed_id", "upstream_sha", "fingerprint", "toolchains"):
        if type(result.get(key)) is not type(expected[key]) or result.get(key) != expected[key]:
            raise ValueError(f"Seed result mismatch: {key}")
    if result.get("outcome") != "success" or result.get("scenario") != "seed":
        raise ValueError("Seed result is not a successful seed")
    if result.get("cache", {}).get("root") != str(root):
        raise ValueError("Seed result cache root mismatch")
    if result.get("system", {}).get("architecture") not in ("x86_64", "amd64"):
        raise ValueError("Seed result architecture mismatch")
    if (result.get("provider") not in ("github", "archil")
            or not isinstance(result.get("series"), str) or not SLUG.fullmatch(result["series"])
            or type(result.get("trial")) is not int or result["trial"] < 1
            or not isinstance(result.get("commit"), str) or not result["commit"]):
        raise ValueError("Seed result metadata is invalid")
    required_phases = {"setup", "checkout", "install", "build", "probe", "total"}
    if workload == "vue":
        required_phases.add("test")
    phases = result.get("phases")
    if not isinstance(phases, dict) or set(phases) != required_phases:
        raise ValueError("Seed result phases are invalid")
    for name, metrics in phases.items():
        if not isinstance(metrics, dict) or type(metrics.get("exit_code")) is not int or metrics["exit_code"] != 0:
            raise ValueError(f"Seed phase was not successful: {name}")
        fields = ("wall_seconds",) if name == "total" else (
            "wall_seconds", "user_seconds", "system_seconds", "max_rss_kib")
        for field in fields:
            if type(metrics.get(field)) not in (int, float) or metrics[field] < 0:
                raise ValueError(f"Seed phase metric is invalid: {name}.{field}")
    for name in ("state", "state/source", "state/source/.git", "tools"):
        no_symlink(root / name)
        if not (root / name).is_dir():
            raise ValueError(f"Seed state missing: {name}")
    head = root / "state/source/.git/HEAD"
    no_symlink(head)
    if head.read_text().strip() != expected["upstream_sha"]:
        raise ValueError("Seed public checkout is not detached at pinned upstream SHA")
    required_state = ("state/source/node_modules", "state/pnpm-store") if workload == "vue" else (
        "state/go-build", "state/go-mod", "state/bin/hugo")
    for name in required_state:
        no_symlink(root / name)
        if not (root / name).exists():
            raise ValueError(f"Seed dependency/build state missing: {name}")
    for name in tools_for(workload):
        tool = manifest()["toolchains"][name]
        dest = root / "tools" / name
        no_symlink(dest)
        no_symlink(dest / tool["binary"])
        if bounded_json(dest / ".receipt.json") != tool or not (dest / tool["binary"]).is_file():
            raise ValueError(f"Seed installed tool state mismatch: {name}")
    return result


def system_info(proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")):
    info = {"architecture": platform.machine(), "logical_cpus": os.cpu_count(),
            "cpu_model": platform.processor() or None, "memory_limit_bytes": None,
            "cpu_quota": None, "cpuset": None}
    try:
        for line in (proc_root / "cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu_model"] = line.split(":", 1)[1].strip()
                break
        info["cpu_affinity"] = sorted(os.sched_getaffinity(0))
        info["host_memory_bytes"] = next(
            int(line.split()[1]) * 1024 for line in (proc_root / "meminfo").read_text().splitlines()
            if line.startswith("MemTotal:"))
        # Read the process's actual cgroup, including parent limits, not host RAM.
        entries = [line.split(":", 2) for line in (proc_root / "self/cgroup").read_text().splitlines()]
        for _, controllers, relative in entries:
            bases = [cgroup_root] if not controllers else [
                cgroup_root / c for c in controllers.split(",")]
            for base in bases:
                group = base / relative.lstrip("/")
                if not group.is_dir():
                    group = base
                info["cgroup_version"] = 2 if not controllers else 1
                info["cgroup_path"] = relative
                for directory in [group, *group.parents]:
                    if directory != base and base not in directory.parents:
                        continue
                    for name in ("memory.max", "memory.limit_in_bytes"):
                        p = directory / name
                        if p.exists():
                            text = p.read_text().strip()
                            if text != "max" and 0 < int(text) < 2**60:
                                value = int(text)
                                old = info["memory_limit_bytes"]
                                info["memory_limit_bytes"] = value if old is None else min(old, value)
                    p = directory / "cpu.max"
                    if p.exists():
                        quota, period = p.read_text().split()
                        value = None if quota == "max" else int(quota) / int(period)
                    elif (directory / "cpu.cfs_quota_us").exists():
                        quota = int((directory / "cpu.cfs_quota_us").read_text())
                        period = int((directory / "cpu.cfs_period_us").read_text())
                        value = quota / period if quota > 0 else None
                    else:
                        value = None
                    if value is not None:
                        old = info["cpu_quota"]
                        info["cpu_quota"] = value if old is None else min(old, value)
                    for name in ("cpuset.cpus.effective", "cpuset.cpus"):
                        p = directory / name
                        if info["cpuset"] is None and p.exists() and p.read_text().strip():
                            info["cpuset"] = p.read_text().strip()
    except (OSError, ValueError, StopIteration):
        pass
    return info


def environment(root, workload):
    state = root / "state"
    for name in ("home", "tmp", "bin", "go-build", "go-mod", "pnpm-store"):
        no_symlink(state / name)
        (state / name).mkdir(parents=True, exist_ok=True)
    # Deliberately whitelist: no inherited GitHub/cloud credentials or npm config.
    env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TZ") if key in os.environ}
    bins = [str(root / "tools" / name / "bin") for name in tools_for(workload)]
    env.update({
        "PATH": ":".join(bins + ["/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
        "XDG_CACHE_HOME": str(state / "home/.cache"), "CI": "true",
        "PUPPETEER_SKIP_DOWNLOAD": "true", "GOMAXPROCS": "4",
        "GOCACHE": str(state / "go-build"), "GOMODCACHE": str(state / "go-mod"),
        "GOPATH": str(state / "go-path"), "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=readonly", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "CI_BENCH_CACHE_ROOT": str(root),
        "CI_BENCH_WORKLOAD": workload,
    })
    return env


class PhaseFailure(RuntimeError):
    pass


def measure(command, cwd, env, log, timeout):
    """wait4 returns this subprocess tree's usage, not RUSAGE_CHILDREN's running peak."""
    started = time.monotonic()
    no_symlink(log)
    with log.open("w") as stream:
        stream.write("$ " + json.dumps(command) + "\n")
        stream.flush()
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            stream.write(str(error) + "\n")
            return {"wall_seconds": time.monotonic() - started,
                    "user_seconds": 0, "system_seconds": 0, "max_rss_kib": 0,
                    "exit_code": 127, "command": command, "timed_out": False}
        timed_out = False
        cancelled = False
        try:
            while True:
                pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                if pid:
                    break
                if time.monotonic() - started > timeout:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    _, status, usage = os.wait4(process.pid, 0)
                    break
                time.sleep(0.02)
        except KeyboardInterrupt:
            cancelled = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
        process.returncode = os.waitstatus_to_exitcode(status)
        code = 130 if cancelled else (124 if timed_out else process.returncode)
    return {"wall_seconds": time.monotonic() - started,
            "user_seconds": usage.ru_utime, "system_seconds": usage.ru_stime,
            "max_rss_kib": usage.ru_maxrss / 1024 if platform.system() == "Darwin" else usage.ru_maxrss,
            "exit_code": code, "command": command, "timed_out": timed_out,
            "cancelled": cancelled, "rss_semantics": "wait4_largest_process_peak"}


def phase(result, name, command, cwd, env, output):
    metrics = measure(command, cwd, env, output / f"{name}.log",
                      manifest()["phase_timeout_seconds"])
    result["phases"][name] = metrics
    if metrics["exit_code"] != 0:
        raise PhaseFailure(f"{name} exited {metrics['exit_code']}; see {name}.log")


def checked(command, **kwargs):
    print("$ " + json.dumps(command), flush=True)
    subprocess.run(command, check=True, timeout=manifest()["phase_timeout_seconds"], **kwargs)


def bootstrap(root, workload):
    linux_x64()
    required = ("git", "curl", "python3", "cc", "c++", "make")
    missing = [name for name in required if shutil.which(name) is None]
    if not Path("/etc/ssl/certs/ca-certificates.crt").is_file():
        missing.append("ca-certificates")
    if missing:
        if os.geteuid() != 0:
            raise ValueError("Missing prerequisites (no sudo attempted): " + ", ".join(missing))
        checked(["apt-get", "update"])
        checked(["apt-get", "install", "-y", "--no-install-recommends",
                 "build-essential", "git", "curl", "python3", "ca-certificates"],
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
    no_symlink(root / "tools")
    (root / "tools").mkdir(parents=True, exist_ok=True)
    config = manifest()
    for name in config["workloads"][workload]["tools"]:
        tool = config["toolchains"][name]
        dest = root / "tools" / name
        no_symlink(dest)
        receipt = dest / ".receipt.json"
        no_symlink(dest / tool["binary"])
        if receipt.exists() and bounded_json(receipt) == tool and (dest / tool["binary"]).is_file():
            continue
        if dest.exists():
            remove_tree(dest)
        with tempfile.TemporaryDirectory(dir=root / "tools", prefix="download-") as tmp:
            tmp = Path(tmp)
            archive = tmp / "tool.tar.gz"
            checked(["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "30",
                     "--max-time", "600", "--output", str(archive), tool["url"]])
            algorithm = "sha256" if "sha256" in tool else "sha512"
            digest = hashlib.new(algorithm)
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest() if algorithm == "sha256" else base64.b64encode(digest.digest()).decode()
            expected = tool.get("sha256", tool.get("sha512_base64"))
            if actual != expected:
                raise ValueError(f"Official {name} archive checksum mismatch")
            extracted = tmp / "extracted"
            extracted.mkdir()
            with tarfile.open(archive) as tar:
                # Python 3.12 Ubuntu and 3.11.8+ support the data filter.
                tar.extractall(extracted, filter="data")
            shutil.move(str(extracted / tool["prefix"]), dest)
            if name == "pnpm":
                launcher = dest / "bin/pnpm"
                launcher.write_text('#!/bin/sh\nexec node "$(dirname "$0")/pnpm.cjs" "$@"\n')
                launcher.chmod(0o755)
            atomic_json(receipt, tool)
    env = environment(root, workload)
    for name, version in tools_for(workload).items():
        output = subprocess.check_output([name, "version" if name == "go" else "--version"],
                                         env=env, text=True, timeout=30).strip()
        expected = f"go version go{version} linux/amd64" if name == "go" else (
            "v" + version if name == "node" else version)
        if output != expected:
            raise ValueError(f"Installed {name} version mismatch: {output}")


def checkout(root, workload, warm):
    config = manifest()["workloads"][workload]
    source = root / "state/source"
    env = environment(root, workload)
    no_symlink(source)
    if not warm:
        source.mkdir()
        checked(["git", "init", "-q", str(source)], env=env)
        checked(["git", "-C", str(source), "remote", "add", "origin", config["repository"]], env=env)
        checked(["git", "-C", str(source), "fetch", "--depth=1", "origin", config["upstream_sha"]], env=env)
        checked(["git", "-C", str(source), "checkout", "--detach", config["upstream_sha"]], env=env)
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                  env=env, text=True, timeout=30).strip()
    if sha != config["upstream_sha"]:
        raise ValueError("Public checkout SHA does not match pinned workload")
    if warm:
        # Do not clean/reset: install, compiler and output state are what warm measures.
        checked(["git", "-C", str(source), "diff", "--exit-code", "HEAD", "--"], env=env)


def hugo_download(root):
    source = root / "state/source"
    paths = [source / name for name in ("go.mod", "go.sum")]
    before = [p.read_bytes() for p in paths]
    checked(["go", "mod", "download"], cwd=source, env=environment(root, "hugo"))
    if before != [p.read_bytes() for p in paths]:
        raise ValueError("go mod download changed readonly go.mod/go.sum")


def write_result(output, result):
    atomic_json(output / "result.json", result)
    no_symlink(output / "result.csv")
    with (output / "result.csv").open("w", newline="") as stream:
        fields = ["schema_version", "provider", "workload", "scenario", "series", "trial",
                  "seed_id", "outcome", "phase", "wall_seconds", "user_seconds",
                  "system_seconds", "max_rss_kib", "exit_code"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for name, metrics in result["phases"].items():
            writer.writerow({**{key: result[key] for key in fields[:8]},
                             "phase": name, **{k: metrics.get(k, "") for k in fields[9:]}})


def consumer_commit():
    return os.environ.get("GITHUB_SHA") or os.environ.get("CI_BENCH_COMMIT") or subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True, timeout=10).strip()


def result_record(provider, workload, scenario, series, trial, seed_id, root):
    return {
        "schema_version": 1, "provider": provider, "workload": workload,
        "scenario": scenario, "series": series, "trial": trial,
        "seed_id": seed_id, "run_id": os.environ.get("GITHUB_RUN_ID", os.environ.get("CI_BENCH_RUN_ID", "")),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", os.environ.get("CI_BENCH_RUN_ATTEMPT", "1")),
        "commit": "", "upstream_sha": manifest()["workloads"][workload]["upstream_sha"],
        "fingerprint": fingerprint(), "toolchains": tools_for(workload),
        "outcome": "failure", "cache": {"validation": "not_checked", "root": str(root)},
        "system": system_info(), "phases": {},
    }


def run(args):
    supplied_root = Path(args.cache_root).absolute()
    output = output_path(args.output, supplied_root, seed=args.scenario == "seed")
    result = result_record(args.provider, args.workload, args.scenario, args.series,
                           args.trial, args.seed_id, args.cache_root)
    lock = None
    try:
        result["commit"] = consumer_commit()
        root = cache_path(args.cache_root)
        output_path(args.output, root, seed=args.scenario == "seed")
        linux_x64()
        root.mkdir(parents=True, exist_ok=True)
        no_symlink(root / ".workload.lock")
        lock = (root / ".workload.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.scenario == "warm":
            validate_seed(args.workload, root, args.seed_id)
            result["cache"]["validation"] = "hit"
        else:
            for name in ("seed-marker.json", "seed-result.json"):
                no_symlink(root / name)
                (root / name).unlink(missing_ok=True)
            state = root / "state"
            no_symlink(state)
            if state.exists():
                remove_tree(state)
            result["cache"]["validation"] = "empty"
        env = environment(root, args.workload)
        phase(result, "setup", ["bash", str(REPO / "scripts/bootstrap.sh")], REPO, env, output)
        command = [sys.executable, str(Path(__file__).resolve()), "_checkout",
                   "--cache-root", str(root), "--workload", args.workload]
        if args.scenario == "warm":
            command.append("--warm")
        phase(result, "checkout", command, REPO, env, output)
        state = root / "state"
        source = state / "source"
        config = manifest()["workloads"][args.workload]
        for name, command in config["commands"].items():
            command = [arg.format(state=state) for arg in command]
            measured_command = command
            if args.workload == "hugo" and name == "install":
                command = [sys.executable, str(Path(__file__).resolve()), "_hugo-download",
                           "--cache-root", str(root), "--workload", "hugo"]
            phase(result, name, command, source, env, output)
            result["phases"][name]["measured_commands"] = [measured_command]
        phase(result, "probe", [sys.executable, str(REPO / "scripts/workload_probe.py"),
                               args.workload, str(state), config["version"]], source, env, output)
        result["outcome"] = "success"
    except (Exception, KeyboardInterrupt) as error:
        if result["cache"]["validation"] == "not_checked" and args.scenario == "warm":
            result["cache"]["validation"] = "miss"
        result["error"] = str(error) or type(error).__name__
        (output / "error.log").write_text(result["error"] + "\n")
    finally:
        result["phases"]["total"] = {"wall_seconds": time.monotonic() - ENTRY_TIME,
                                     "exit_code": 0 if result["outcome"] == "success" else 1}
        try:
            if result["outcome"] == "success" and args.scenario == "seed":
                try:
                    atomic_json(root / "seed-result.json", result)
                    marker = {**identity(args.workload, root, args.seed_id), "outcome": "success",
                              "seed_result_sha256": hashlib.sha256((root / "seed-result.json").read_bytes()).hexdigest()}
                    atomic_json(root / "seed-marker.json", marker)
                except Exception as error:
                    result["outcome"] = "failure"
                    result["error"] = f"Seed publication failed: {error}"
                    result["phases"]["total"]["exit_code"] = 1
                    (output / "error.log").write_text(result["error"] + "\n")
                    for name in ("seed-marker.json", "seed-result.json"):
                        (root / name).unlink(missing_ok=True)
            write_result(output, result)
        finally:
            if lock:
                lock.close()
    return 0 if result["outcome"] == "success" else 1


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("trial must be positive")
    return value


def slug(value):
    if not SLUG.fullmatch(value):
        raise argparse.ArgumentTypeError("Expected a safe 1-80 character slug")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    for action in ("run", "verify-seed", "bootstrap", "_checkout", "_hugo-download"):
        sub = subs.add_parser(action)
        sub.add_argument("--workload", choices=["vue", "hugo"], required=True)
        sub.add_argument("--cache-root", required=True)
        if action in ("run", "verify-seed"):
            sub.add_argument("--output", required=True)
            sub.add_argument("--seed-id", required=True)
        if action == "run":
            sub.add_argument("--provider", choices=["github", "archil"], required=True)
            sub.add_argument("--scenario", choices=["cold", "seed", "warm"], required=True)
            sub.add_argument("--series", type=slug, required=True)
            sub.add_argument("--trial", type=positive, required=True)
        if action == "_checkout":
            sub.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    if args.action in ("run", "verify-seed"):
        if args.seed_id:
            try:
                slug(args.seed_id)
            except argparse.ArgumentTypeError as error:
                parser.error(str(error))
        elif args.action != "run" or args.scenario != "cold":
            parser.error("seed-id may be empty only for cold")
    if args.action == "run":
        # SIGTERM follows the same subprocess-group cancellation path as Ctrl-C.
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        return run(args)
    root = cache_path(args.cache_root)
    if args.action == "verify-seed":
        output = output_path(args.output, root)
        try:
            no_symlink(root / ".workload.lock")
            with (root / ".workload.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                result = validate_seed(args.workload, root, args.seed_id)
                write_result(output, result)
            return 0
        except Exception as error:
            (output / "error.log").write_text(str(error) + "\n")
            series = os.environ.get("CI_BENCH_SERIES", "verify-seed")
            if not SLUG.fullmatch(series):
                series = "verify-seed"
            trial = os.environ.get("CI_BENCH_TRIAL", "1")
            trial = int(trial) if trial.isdigit() and int(trial) > 0 else 1
            result = result_record("archil", args.workload, "seed", series, trial, args.seed_id, root)
            result["commit"] = consumer_commit()
            result["cache"]["validation"] = "miss"
            result["error"] = str(error)
            result["phases"]["total"] = {"wall_seconds": time.monotonic() - ENTRY_TIME, "exit_code": 1}
            write_result(output, result)
            print(str(error), file=sys.stderr)
            return 1
    if args.action == "bootstrap":
        bootstrap(root, args.workload)
    elif args.action == "_checkout":
        checkout(root, args.workload, args.warm)
    elif args.action == "_hugo-download":
        hugo_download(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
