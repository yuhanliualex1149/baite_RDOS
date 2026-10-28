# RDOS 2.0 routine releases

Production URL: https://yjmt.cn/baite-rdos/. Code releases use the exact current main SHA. The initial migration and activation record remains in cutover/OPERATIONS.md; it is not part of routine publishing.

## One release path

.github/workflows/production.yml asks the restricted SSH gateway for check before any deploy. Only a gateway reporting RDOS2_DEPLOY_PROTOCOL=2 and the exact SHA-256 of this commit's deploy/production-deploy.sh may receive deploy <SHA>. An older installed gateway or script fails this check before the write request. The installed root-owned /usr/local/sbin/rdos-production-deploy must contain the reviewed deploy/production-deploy.sh; the installed gateway must contain the reviewed deploy/ssh-gateway.sh. Updating repository files alone does not update either installed executable.

The root script accepts only the exact current main SHA, serializes on /run/lock/baite-rdos-deploy.lock, and keeps the current service running during staging. It builds an exact Git archive as the unprivileged deployment user with an empty environment and runs python -B -m cutover.check_release plus pip check. It checks the sealed source and virtual environment are readable by baite-rdos before writing .verified atomically. A failed build leaves no verified release.

Before stopping the service, the script runs the read-only formal preflight, rechecks main and the active release, saves the three existing RDOS configuration files and the two RDOS Nginx snippets in a root-only backup directory, and gates both public RDOS paths with HTTP 503. After the service drains, it takes an SQLite backup through the backup API and checks it. It then changes only the RDOS current symlink, starts the existing service, and verifies the exact local SHA, static assets, runtime info, public SHA and public assets. Maintenance is removed only after these checks.

On failure, recovery switches back to the previous code and checks its health while retaining the latest database. It never restores the backup over newer business records. If previous-code health cannot be proved, RDOS stays under maintenance for manual recovery. First-time schema or Feishu migration is never invoked by this script.

## Before enabling a normal push release

1. Review the exact candidate, diff, tests and currently installed root scripts; confirm the current service uses the RDOS 2.0 override. Do not infer installed behavior from repository files.
2. Deliberately install the reviewed gateway and deployment script at their existing root-owned paths, preserving the existing restricted SSH and sudo command scope. This is a separate production change and is not performed by a repository commit.
3. Run the workflow's connection-only check and require the protocol marker. Then use the authorized release procedure for a specific main SHA. Verify backup, local/public version, static page, account login and intended business read/write with appropriate authorization. The script's public read-only checks do not prove a real client's Skill installation or natural scheduled execution.

The workflow also supports manual check and deploy; pushes that change only .github/, deploy/, docs/ or Markdown do not trigger deployment. Repository branch and installed-script changes must be coordinated before the next ordinary code push. YXXZ is a separate site and is outside this release path.
