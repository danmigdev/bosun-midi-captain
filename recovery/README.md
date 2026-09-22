# Local recovery files

This directory keeps private recovery material separate from current builds.
Its contents are ignored by Git, except for this guide.

- `hardware-recovery-and-evidence.zip`: flash backup, installation journals,
  manufacturer recovery files and hardware validation records from earlier work.
  `SHA256.json` inside the archive records each file's checksum.
- `uncommitted-worktree-2026-09-22.zip`: eight uncommitted source edits recovered
  from the retired 0.6.6 worktree. It includes the base commit, a binary-safe
  Git patch and copies of the modified files. These edits are not applied to
  the current project.
- `worktree-changes.patch`: the same patch, accessible without extracting the archive.
- `incomplete-linux-download.part`: an interrupted optional Linux release
  download, isolated here because automatic approval review blocked deletion.
  It is not a usable release package or a recovery backup.

Keep these archives until the associated recovery and unfinished work are no
longer needed. They are not release packages and must not be uploaded publicly.
Current release downloads are in `dist/release-0.7.1/`.
