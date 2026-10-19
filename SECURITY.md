# Security policy

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** for this repository
(Security tab → "Report a vulnerability"). Do not open a public issue for a
suspected vulnerability. We aim to acknowledge reports within 7 days.

## Scope

In scope:

- `safeforge.zipcheck`: any archive that `validate_zip` accepts although it
  should be rejected (path escape, collision, special file, resource
  exhaustion), and any exception other than `ZipRejected` escaping
  `validate_zip`.
- `safeforge.store`: any sequence of crashes or concurrent calls that leaves a
  `stored` row without an intact file, makes `ingest` report success for data
  that is not stored, or makes `reconcile` delete a file it did not create.
- `safeforge.events`: any interleaving in which one event id runs its effect
  twice.

Out of scope, documented as limits in README.md:

- power-loss durability on Windows (no directory fsync available)
- content of archives (malicious JavaScript in a valid archive)
- network filesystems (SQLite WAL needs a single host)
- side effects outside the database transaction in `process_event`

## Supported versions

Only the latest release receives fixes. This is a research prototype (0.x).
