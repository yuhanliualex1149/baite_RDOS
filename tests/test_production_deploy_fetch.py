"""Exercise the deploy script's fetch retries without running production setup."""

import pathlib
import subprocess
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "deploy/production-deploy.sh"


class ProductionDeployFetchTests(unittest.TestCase):
    def test_fetch_retries_are_bounded(self):
        source = SCRIPT.read_text()
        start = source.index("fetch_main_with_retry() {")
        end = source.index("\n}\n", start) + 2
        fetch_function = source[start:end]
        self.assertEqual(source.count("\nfetch_main_with_retry\n"), 2)

        for succeeds_on, expected in ((1, "0 1 0"), (3, "0 3 2"), (4, "1 3 2")):
            with self.subTest(succeeds_on=succeeds_on):
                harness = f"""set -euo pipefail
repo=/tmp/repository
calls=0
sleeps=0
succeeds_on={succeeds_on}
timeout() {{
  [[ "$*" == '--signal=TERM --kill-after=5s 60s git -C /tmp/repository fetch --no-tags origin main' ]] || return 97
  ((calls+=1))
  (( calls >= succeeds_on ))
}}
sleep() {{
  [[ $1 == 2 ]] || return 98
  ((sleeps+=1))
}}
{fetch_function}
if fetch_main_with_retry; then status=0; else status=$?; fi
printf '%s %s %s\n' "$status" "$calls" "$sleeps"
"""
                result = subprocess.run(
                    ["bash", "-c", harness], capture_output=True, text=True, check=True
                )
                self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
