# RDOS production deployment

Production URL: https://yjmt.cn/baite-rdos/
Production branch: `main`. The old application is archived on `archive/pre-central-agent-20261011`.

## Current state (2026-10-11)

The previous application has been taken offline at the owner's request. Its database is retained only in `/var/lib/baite-rdos/retired-20261011T131658Z/`; the active database path is empty. The public RDOS routes return HTTP 503 until the replacement application is ready. YXXZ is separate and unchanged.

The GitHub Actions connection-only check is verified. This is not a successful application deployment. No staging website or approval gate is used.

## Normal operation

- A push to `main` deploys the exact commit. Changes limited to `.github/`, `deploy/`, `docs/`, or Markdown do not trigger a rollout. Such changes can be followed by a manual run when appropriate.
- Actions → RDOS production → Run workflow: `check` only verifies the dedicated SSH connection and reports service state. `deploy` publishes the selected main SHA; the server rejects a commit that is no longer the latest main.
- Preserve `.github/workflows/production.yml` and the deployment documentation when bringing in the replacement local application. Do not force-push over remote configuration commits.
- Deployments are serialized both by GitHub concurrency and a server lock.
- Dependency installation, `pip check`, unittest discovery, and the E2E demo must pass in an unprivileged build sandbox before live service changes.
- Versioned releases live under `/opt/baite-rdos/releases/<SHA>`. The runtime configuration stays in `/etc/baite-rdos/rdos.env`; the database stays at `/var/lib/baite-rdos/baite.db`. No deployment overwrites these with repository fixtures.
- Before an update, the stopped SQLite database is backed up with the SQLite backup API and checked for integrity. The release symlink is changed atomically. Both local and public health must report the exact SHA.
- A successful deployment restores only RDOS's two saved reverse-proxy snippets, enables `baite-rdos` and its backup timer, and leaves YXXZ untouched.
- On failure, the previous code is restored only if the database's complete logical contents are unchanged, and its health is checked. If the database changed or recovery health fails, RDOS stays offline with the database preserved for deliberate recovery. The script never automatically restores an older database over newer records.

## Replacement application's first deployment

The repaired application must be pushed to main only when ready. With the active database absent, the application initializes a clean database using the configured administrator username and a password of at least 10 characters. Ensure those initialization values have been supplied through a secure interface; no default password is created. After initialization, normal deployments retain the database and do not recreate accounts.

Health alone does not establish that the new central agent works. Verify the Linux service user's `lark-cli` executable and authorization, model API configuration, and the new version's intended scheduling behavior before declaring the replacement fully usable. The old `BAITE_DISABLE_EXTERNAL_SYNC` flag does not necessarily disable the new central scheduler. Never run a shadow copy with live integration credentials for a deployment check.

## Access and maintenance

The repository secret is `RDOS_DEPLOY_SSH_KEY`. It authenticates only `baite-rdos-deploy` on the production host. The public key has a forced command with forwarding and PTY disabled. That gateway accepts only `check` or `deploy <40-character-SHA>`; it does not allow an arbitrary shell.

The root-owned installed entry points are `/usr/local/sbin/rdos-ssh-gateway` and `/usr/local/sbin/rdos-production-deploy`. Their reviewed source is stored here. Editing a repository deployment script does not automatically replace the privileged server copy: review and install privileged script changes deliberately. No YXXZ credentials or deployment permissions are shared.

If a failed release is still the current SHA and health cannot be restored, inspect its logs and supply a corrected commit; do not delete or reset the database. Retired databases and backup files are not automatically destroyed.
