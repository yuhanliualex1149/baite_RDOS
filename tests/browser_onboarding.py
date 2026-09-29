"""Headless real-browser acceptance against disposable local Panel; no production calls."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from tests.e2e_demo import free_port, stop, wait_until
import subprocess
import urllib.request


def main():
    with tempfile.TemporaryDirectory(prefix="rdos-browser-test-") as tmp:
        base = Path(tmp)
        url = f"http://127.0.0.1:{free_port()}"
        environment = {**os.environ, "BAITE_DB_PATH": str(base / "test.db"), "BAITE_PUBLIC_URL": url,
                       "BAITE_ADMIN_USERNAME": "admin", "BAITE_ADMIN_PASSWORD": "IsolatedBrowser123!",
                       "BAITE_SESSION_SECRET": "isolated-browser-not-production-secret", "BAITE_APP_RELEASE": "a" * 40,
                       "BAITE_DISABLE_EXTERNAL_SYNC": "true", "BAITE_COOKIE_SECURE": "false"}
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1",
                                   "--port", url.rsplit(":", 1)[1], "--log-level", "error"], env=environment,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_until(lambda: urllib.request.urlopen(url + "/api/health").status == 200, "Panel startup")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chromium")
                context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda msg: errors.append(msg.text) if msg.type in {"error", "warning"}
                        and "401 (Unauthorized)" not in msg.text else None)
                page.goto(url)
                page.locator("#login-username").fill("admin")
                page.locator("#login-password").fill("IsolatedBrowser123!")
                page.locator('#login-form button[type="submit"]').click()
                page.locator('[data-page="runners"]').first.click()
                page.locator("#show-runner-form").click()
                page.locator("#runner-name").fill("user1")
                page.locator('#runner-form button[type="submit"]').click()
                expect(page.locator("#config-dialog")).to_be_visible()
                configuration = json.loads(page.locator("#config-json").inner_text())
                assert configuration["workspace"] == ""
                with page.expect_download() as download:
                    page.locator("#download-config").click()
                downloaded = download.value.path()
                assert json.loads(Path(downloaded).read_text(encoding="utf-8")) == configuration
                page.locator("#onboarding-platform").select_option("windows")
                expect(page.locator("#copy-onboarding")).to_be_enabled()
                expect(page.locator("#onboarding-text")).to_have_value(re.compile("Windows 10", re.S))
                instructions = page.locator("#onboarding-text").input_value()
                assert configuration["runner_token"] not in instructions
                page.locator("#copy-onboarding").click()
                expect(page.locator("#toast")).to_have_text("接入指令已复制，不含 Token")
                copied = page.evaluate("navigator.clipboard.readText()")
                # The Windows clipboard can expose CRLF; compare every character after newline normalization.
                assert copied.replace("\r\n", "\n") == instructions, "Clipboard content differs from onboarding text"
                page.locator('#config-dialog button[value="close"]').click()
                expect(page.locator("#config-json")).to_have_text("")
                page.locator('[data-action="runner-onboarding"]').click()
                expect(page.locator("#config-secret")).to_be_hidden()
                expect(page.locator("#config-unavailable")).to_be_visible()
                expect(page.locator("#copy-onboarding")).to_be_enabled()
                page.locator("#onboarding-platform").select_option("macos")
                expect(page.locator("#onboarding-text")).to_have_value(re.compile("平台：macOS", re.S))
                page.locator('#config-dialog button[value="close"]').click()
                page.locator('[data-action="runner-rotate"]').click()
                expect(page.locator("#config-secret")).to_be_visible()
                rotated = json.loads(page.locator("#config-json").inner_text())
                assert rotated["runner_id"] == configuration["runner_id"]
                assert rotated["runner_token"] != configuration["runner_token"]
                with page.expect_download() as download:
                    page.locator("#download-config").click()
                assert json.loads(Path(download.value.path()).read_text(encoding="utf-8")) == rotated
                expect(page.locator("#copy-onboarding")).to_be_enabled()
                assert rotated["runner_token"] not in page.locator("#onboarding-text").input_value()
                page.locator('#config-dialog button[value="close"]').click()
                assert page.locator(".runner-card").count() == 1
                assert not errors, errors
                browser.close()
            print("BROWSER PASS: create/download, Windows+Mac instructions, clipboard, one-time secret cleared, existing node, rotate/download; no unexpected console errors/warnings")
        finally:
            stop(server)


if __name__ == "__main__":
    main()
