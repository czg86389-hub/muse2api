# Installed version and explicit updates

Version checks report installed `version.json` and local commit separately from the configured remote. They never download code, trigger a restart, mark an unverified install as current, or silently replace the local SHA with remote HEAD. Failed checks return an unknown state.

`MUSE2API_REPOSITORY` defaults to `czg86389-hub/muse2api`; `MUSE2API_REPO_REF` defaults to `main`. Fork deployments can override both. Build Docker images with `--build-arg BUILD_SHA=$(git rev-parse HEAD)` to embed immutable provenance, which takes precedence over an old persistent `.installed_sha` file. Set `MUSE2API_MANAGED_DEPLOYMENT=1` for image-managed deployments; in-container upgrades are then rejected and the UI directs updates through image builds.

Explicit source-install upgrades pin a verified full commit SHA, check Git command results, preserve `.env` and `data/`, and record the installed SHA only after success. They are rejected while tasks are active or queued. The legacy silent-update behavior is removed from both the status API and browser; a status refresh is read-only.
