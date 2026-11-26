# Source-only GitHub publication

The GitHub snapshot contains application source, tests, documentation, icons,
frontend assets and placeholder configuration. It intentionally has no parent
commit: the original local history tracked credentials and private runtime data.
That history must not be pushed with `--all`, `--mirror`, or by merging it into
the published branch. Existing local history and the working index are retained.

The following stay local:

- `.env` files and backups, OAuth credentials/tokens, private keys and key stores.
- The entire `vault`, including memories, health data, contacts, faces, projects,
  browser profiles, alarms, downloads and generated reports.
- Screenshots, camera frames, voice recordings, logs and execution reports.
- Installed dependencies, model weights, virtual environments and build output.
- Personal bootstrap interview/profile exports and local deployment settings.

`.env.example` is a template, not a usable credential file. Set up fresh local
credentials and provider authorizations on any new installation. Download model
weights separately, respecting the respective model licenses. Git ignores do not
remove secrets already present in old commits. Rotate any credentials if that
old history has previously been shared or uploaded.
