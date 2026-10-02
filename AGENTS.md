# CI benchmark development

- This is a public consumer repository. Use only public upstream projects; never
  add internal source, credentials, or environment dumps.
- Workload commands and pinned versions are provider-independent. Provider setup
  belongs in workflows, not the measured build/test commands.
- A warm trial must restore a successful immutable seed onto a fresh runner.
  Missing or incompatible caches are failures, not cold trials labeled warm.
- Keep seeding, provisioning, cache restore, workload, and end-to-end timings
  distinct. Preserve failed trials and actual hardware metadata in reports.
- Do not publish a performance winner from a single sample or silently pool
  different workload/toolchain/resource fingerprints.
- Test production scripts with deterministic fake commands:
  `python3 -m unittest discover -s tests -v`.
- Validate workflows with `actionlint` and shell scripts with `shellcheck`.
- Keep cloud credentials on hosted lifecycle controllers, never in cached
  templates or workload processes. Delete only resources created by this suite.
