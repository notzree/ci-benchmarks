# Real-world CI runner benchmarks

Compare GitHub-hosted Actions with native Archil Actions runners using pinned
public projects. Depot and Blacksmith can be added as execution adapters later;
they must use the same workload commands and successful-seed rules.

There is **no performance winner assumed here**. Faster CPUs may win cold
compilation; filesystem cache reuse may win cache restoration or incremental
work. Measure both workload time and the time users wait for CI to finish.

## Workloads

| Workload | Upstream release | Measured work |
| --- | --- | --- |
| Vue | 3.5.13 | Frozen pnpm install, full build, unit tests, built-library correctness probe |
| Hugo | 0.139.3 | Read-only Go module download, executable build, version/site-generation correctness probe |

`benchmarks.json` pins upstream commit SHAs, toolchains, tool checksums and
commands. Scripts fetch only public source. No Archil monorepo access is required.
Vue uses Node 22.11.0/pnpm 9.12.3; Hugo uses Go 1.23.3. These are fixed benchmark
toolchains, not recommendations for production deployments.

Both lanes use Linux x64/Ubuntu 24.04. Archil requests 4 vCPU/16 GiB to approximate
the public GitHub-hosted runner tier. Actual CPU model, CPU count, cgroup quota,
architecture and memory limits are recorded; equal nominal size is **not** equal
CPU generation. Vue tests and Go compilation have the same four-worker budget.
Docker and browser/end-to-end tests are not included in these initial workloads.

## Cold, seed, warm

1. **Cold:** fresh runner, empty upstream checkout/dependency/build state. The
   pinned toolchain may already be installed. Installation/build/test/probe
   commands and their timings are recorded.
2. **Seed:** a separate successful execution prepares one immutable baseline per
   provider/workload. Its preparation/cache-save time is reported, not relabeled
   as a fast warm trial.
3. **Warm:** a **new runner for every sample**, restored from that same seed.
   GitHub restores an exact Actions cache archive; Archil forks a stopped,
   unregistered filesystem template. The seed includes tools, the public upstream
   checkout, dependency state and compiler/build state.

There are no fallback restore keys and no seed promotion after benchmarks. A
miss, corrupt marker, changed script/toolchain/upstream, wrong architecture or
missing build state is a failed warm trial. Warm commands still execute the
real build/tests/probe: a cache is not permission to skip correctness.

This compares the complete native cache mechanisms, not two identical storage
systems. Keep archive restore and filesystem fork/provision timings visible.
Vue's full build does not become an incremental compiler workload merely because
dependencies are warm; Hugo can reuse its Go compiler cache. No claim about
arbitrary shared multiwriter build directories is made.

## Run a comparison

GitHub-only needs no cloud secrets. To include Archil, add these **repository
Actions secrets in this consumer repo**, not the reusable action repo:

- `ARCHIL_API_KEY`: personal/regional Archil sandbox key.
- `GH_RUNNER_ADMIN_TOKEN`: token authorized for this repository's self-hosted
  runners, with Repository Administration read/write.

The API/admin keys are used only by GitHub-hosted lifecycle controllers. They
are not included in templates, workload environments, caches, or artifacts.
Normal Actions job credentials remain job-scoped. The action is pinned to
`notzree/archil-ci@9a5ae8ba1d491202de15025842c91921d246d95f`.

Optional repository variable `ARCHIL_CI_REGION` defaults to `aws-us-east-1`;
supported alternatives are `aws-us-west-2` and `aws-eu-west-1`. Use a key for the
selected region and sufficient sandbox/fork/egress entitlement.

Start with one smoke sample:

```bash
gh workflow run suite.yml --repo notzree/ci-benchmarks \
  --ref notzree/ci-benchmarks \
  -f workload=vue -f providers=both -f trials=1 -f retain-seeds=false
```

Then use `workload=all` and `trials=5` for repeated comparisons. Use
`providers=github` for a credential-free baseline. Every trial is a separate
`benchmark.yml` dispatch, with alternating provider order and a common resolved
consumer commit. The suite refuses to mix commits if the branch moves.
Workflows are manual; a push only runs fast deterministic harness tests.

The suite creates billed Archil compute/storage and uses GitHub cache quota.
By default it evicts its exact immutable seeds after all known children finish.
`retain-seeds=true` intentionally keeps them for later experiments; Archil
storage remains billed. Seed receipts identify exact source IDs/cache keys.

## Results and interpretation

Download the suite artifact:

```bash
gh run download RUN_ID --repo notzree/ci-benchmarks \
  --pattern 'ci-benchmark-suite-*' --dir results
python3 scripts/report.py results --output results/report
```

The artifact preserves raw phase logs, JSON/CSV measurements, seed receipts,
workflow/job timestamps, provisioning reports, failures and skipped trials.
`report/summary.md` is also shown in the suite's Actions summary.

Report workload phases, job wall time, seed cost, cache restore, provisioning
and end-to-end workflow time separately. Parallel watchdog time must never be
added to native job time. Archil toolchain installation occurs in the template;
GitHub may install it during the benchmark job, so guest workload totals alone
are not an end-to-end comparison.

Statistics include sample count, median, nearest-rank p95, range and population
standard deviation. Groups separate workload/upstream/consumer/toolchain/cache/
hardware fingerprints. One smoke sample only proves the path works; it does not
establish a speedup. Per-phase RSS is the kernel-reported largest child-process
peak, not simultaneous whole-process-tree memory.

## Failure recovery and cleanup

Do not force-cancel the suite or manually rerun its children while it is active.
GitHub has no atomic attempt-scoped cancellation/seed-deletion protocol; latest
attempt checks reduce that race but cannot lock out a concurrent human rerun.
Normal interruption
requests cancellation of its identified child; each native child has its own
hosted watchdog and exact-run cleanup. Force cancellation, runner/controller
loss, API outages, or an unidentified dispatch can still leave resources.

Cleanup refuses to delete seeds while known children are active or a dispatch
is unresolved. Inspect `suite.json`, `seeds.json` and Actions run URLs first.
After confirming all children are terminal, retry cleanup on a trusted hosted
controller using `scripts/cleanup_seeds.py` with the pinned action checkout and
credentials. It reuses the production action cleanup against the seed creator's
repository/run/attempt namespace; it never deletes arbitrary account resources.
If a dispatch remains unresolved, reconcile it manually before clearing its
marker. Archil paused/stopped sandboxes still consume storage.

Standalone `benchmark.yml` seed runs intentionally retain their source; the
suite is responsible for eviction. Warm runs never delete their reused parent.
No scheduled retained-seed eviction is deployed.

## Development

```bash
python3 -m unittest discover -s tests -v
actionlint
shellcheck scripts/bootstrap.sh
```

Tests exercise production scripts with fake commands and temporary caches; they
do not spend cloud credits. Real workload validation happens through the manual
smoke suite. The bootstrap/run scripts explicitly require Linux x64.

Workload choices were informed by
[BuildPulse's public runner comparison](https://github.com/buildpulse/runner-benchmarks)
and the measurement principles in
[GitHub's Java cache benchmarks](https://github.com/actions/setup-java-benchmarks).
This harness is independently implemented; vendor-published speedups are not
results from this repository.
