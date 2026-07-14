"""Checkpoint 3 runtime UI verification — drives the real app in headless Chromium.

Run: .venv/bin/python scripts/ui_verify.py
Exercises spec 8.2 click flows end to end: login, select, connect (line drawn),
conflict dialog + force replace, paired-click disconnect, label edit, refresh, resize.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
PORT_COUNT = 8
USER, PASSWORD = "admin", "uiverifypw"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pf-ui-"))
    (tmp / "port_map.json").write_text(json.dumps({str(u): 100 + u for u in range(1, 9)}))
    port = free_port()
    env = dict(
        os.environ,
        PORT_COUNT=str(PORT_COUNT),
        MAPPINGS_FILE=str(tmp / "m.json"),
        PORT_MAP_FILE=str(tmp / "port_map.json"),
        AUTH_FILE=str(tmp / "auth.json"),
        SESSION_SECRET="ui-verify",
        BOOTSTRAP_USERNAME=USER,
        BOOTSTRAP_PASSWORD=PASSWORD,
        TOFINO_BACKEND="fake",
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    time.sleep(2)
    assert server.poll() is None, server.stdout.read().decode()

    failures = []
    js_errors = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True)
            page = browser.new_page()
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)

            page.goto(base + "/ui")
            check("unauthenticated /ui lands on login", "/ui/login" in page.url)
            page.fill('input[name="username"]', USER)
            page.fill('input[name="password"]', PASSWORD)
            page.click('button[type="submit"]')
            page.wait_for_url("**/ui")
            check("login redirects to panel", page.url.endswith("/ui"))
            check("8 ingress + 8 egress rendered",
                  page.locator('[data-side="ingress"]').count() == 8
                  and page.locator('[data-side="egress"]').count() == 8)

            ing = lambda n: page.locator(f'[data-side="ingress"][data-port="{n}"]')
            egr = lambda n: page.locator(f'[data-side="egress"][data-port="{n}"]')
            lines = lambda: page.locator("#lines line").count()

            # select is client-side: no network, class applied
            ing(1).locator(".port-number").click()
            check("ingress click selects (client-side)", "selected" in (ing(1).get_attribute("class") or ""))

            # connect 1 -> 2: swap + line drawn
            egr(2).locator(".port-number").click()
            page.wait_for_function('document.querySelector(\'[data-side="ingress"][data-port="1"]\').dataset.mappedEgress === "2"')
            check("connect 1->2 updates mapping attr", True)
            check("connect draws 1 SVG line", lines() == 1)

            # second mapping 7 -> 5
            ing(7).locator(".port-number").click(); egr(5).locator(".port-number").click()
            page.wait_for_function('document.querySelector(\'[data-side="ingress"][data-port="7"]\').dataset.mappedEgress === "5"')
            check("two lines after second connect", lines() == 2)

            # conflict: 1 -> 5
            ing(1).locator(".port-number").click(); egr(5).locator(".port-number").click()
            page.wait_for_selector("#dialog .conflict-confirm")
            dialog_text = page.locator("#dialog").inner_text()
            check("conflict dialog names both removals", "1" in dialog_text and "2" in dialog_text and "7" in dialog_text and "5" in dialog_text)

            page.click('#dialog button[type="submit"]')  # Replace (force=true)
            page.wait_for_function('document.querySelector(\'[data-side="ingress"][data-port="1"]\').dataset.mappedEgress === "5"')
            check("force replace applies 1->5", True)
            check("dialog cleared after replace", page.locator("#dialog .conflict-confirm").count() == 0)
            check("exactly 1 line after replace", lines() == 1)
            check("ingress 7 unmapped after replace", (ing(7).get_attribute("data-mapped-egress") or "") == "")

            # paired-click disconnect: select ingress 1 then click its egress 5
            ing(1).locator(".port-number").click(); egr(5).locator(".port-number").click()
            page.wait_for_function('document.querySelector(\'[data-side="ingress"][data-port="1"]\').dataset.mappedEgress === ""')
            check("paired click disconnects", lines() == 0)

            # label edit is a deliberate action: edit button reveals the input
            check("label input hidden until edit clicked", not ing(3).locator("input").is_visible())
            ing(3).locator(".label-edit").click()
            check("edit button reveals input", ing(3).locator("input").is_visible())
            ing(3).locator("input").fill("Camera A")
            ing(3).locator("input").blur()
            page.wait_for_function('document.querySelector(\'[data-side="ingress"][data-port="3"]\').textContent.includes("Camera A")')
            page.reload()
            check("label persists across reload", "Camera A" in (ing(3).text_content() or ""))
            check("input hidden again after save", not ing(3).locator("input").is_visible())

            # refresh button re-renders panel
            ing(4).locator(".port-number").click(); egr(6).locator(".port-number").click()
            page.wait_for_function('document.querySelector(\'[data-side="ingress"][data-port="4"]\').dataset.mappedEgress === "6"')
            page.click('button:has-text("Refresh")')
            page.wait_for_timeout(500)
            check("refresh keeps mapping and line", lines() == 1
                  and (ing(4).get_attribute("data-mapped-egress") or "") == "6")

            # resize redraw doesn't error, line survives
            page.set_viewport_size({"width": 700, "height": 900})
            page.wait_for_timeout(300)
            check("line survives resize", lines() == 1)

            check("no JS errors on page", not js_errors)
            if js_errors:
                print("JS errors:", js_errors)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    print(f"\n{'OK' if not failures else 'FAILED'}: {len(failures)} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
