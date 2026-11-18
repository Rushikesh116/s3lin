# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-18

First release. The checker comes from the study at
https://github.com/Rushikesh116/supabase-storage-s3lin.

### Added

- `check()`: linearizability check of a history, one key at a time, returning a
  `CheckResult` whose per-key results are `Linearizable`, `Violation` (with the blocking
  operation, the longest linearizable prefix and the state after it) or `Unknown` (a time
  or state limit was hit).
- `check_invariants()`: the create-once, cas-unique, no-phantom and no-ghost invariants,
  also attached to every `Violation` and `Unknown` result.
- `Op`, a frozen record of one operation that validates its fields, and `OpRecord`, the
  `TypedDict` of one history-file line.
- `Recorder`, a thread-safe recorder with `invoke()` and `complete()`, and
  `outcome_from_status()`, which classifies HTTP statuses the way S3 defines them.
- `read_history()`, `write_history()` and `parse_history()` for JSON Lines files,
  optionally gzip-compressed, in the format the study recorded.
- The `s3lin check` command (also `python -m s3lin`) with text and `--json` output.

### Changed (compared with the study's code)

- An unconditional `put` answered with a failure is now a violation; the study's checker
  treated it as a successful write. The study's workloads never issued such puts.
- `outcome_from_status()` maps a 404 on `put`, `put_inm` and `mpu_inm` to `noeffect`
  (a missing bucket or upload) instead of `fail`, and any non-2xx, non-404 answer to a
  `get` to `noeffect` or `info`.
- The no-ghost invariant also covers writes answered with `noeffect`.

[Unreleased]: https://github.com/Rushikesh116/s3lin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Rushikesh116/s3lin/releases/tag/v0.1.0
