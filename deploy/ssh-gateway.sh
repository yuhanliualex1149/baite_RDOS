#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
case "${SSH_ORIGINAL_COMMAND:-}" in
  check)
    systemctl is-active baite-rdos || true
    if systemctl is-active --quiet baite-rdos; then curl -fsS --max-time 5 http://127.0.0.1:8010/api/health; else echo 'RDOS is offline; no deployment performed.'; fi
    printf '\nRDOS restricted deployment connection verified.\n'
    ;;
  *)
    if [[ ${SSH_ORIGINAL_COMMAND:-} =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
      exec sudo -n /usr/local/sbin/rdos-production-deploy "${BASH_REMATCH[1]}"
    fi
    echo 'Only check and deploy <40-character-main-SHA> are allowed.' >&2
    exit 64
    ;;
esac
