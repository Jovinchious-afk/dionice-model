"""
Screenshots for visual verification (used by the "verify" skill in .claude/skills).

  --html PATH   render a newsletter/report HTML file (e.g. a --dry-run output) to PNG
  --app         start the Streamlit app locally, screenshot every page, stop it

Pages are only viewed — nothing is clicked except the sidebar navigation, so no
form is ever submitted. Requires the dev dependencies (requirements-dev.txt) and
once: python -m playwright install chromium

Usage: python scripts/verify_screens.py [--html PATH] [--app] [--out DIR] [--pages "Portfolio,Učenje"]
"""

import argparse
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALL_PAGES = ["Portfolio", "Log Trade", "Watchlist", "Decisions", "Newsletteri", "Učenje"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_idle(page, timeout_s: int = 90) -> None:
    """Waits until Streamlit has finished running the script (no 'Running…' status widget)."""
    time.sleep(2)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.locator("[data-testid='stStatusWidget']").count() == 0:
            break
        time.sleep(1)
    time.sleep(1.5)


def shoot_html(path: Path, out: Path) -> Path:
    from playwright.sync_api import sync_playwright
    target = out / f"{path.stem}.png"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 760, "height": 1000})
        page.goto(path.resolve().as_uri())
        page.wait_for_load_state("load")
        page.screenshot(path=str(target), full_page=True)
        browser.close()
    return target


def shoot_app(out: Path, pages: list[str]) -> list[Path]:
    from playwright.sync_api import sync_playwright
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app/portfolio_app.py", "--server.headless", "true",
         "--server.port", str(port), "--browser.gatherUsageStats", "false"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    shots = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            for _ in range(60):
                try:
                    page.goto(f"http://localhost:{port}", timeout=5000)
                    break
                except Exception:
                    time.sleep(1)
            wait_idle(page)
            for name in pages:
                if name != "Portfolio":
                    page.get_by_text(name, exact=True).first.click()
                wait_idle(page)
                target = out / f"app_{name.replace(' ', '_')}.png"
                page.screenshot(path=str(target), full_page=True)
                shots.append(target)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return shots


def main():
    parser = argparse.ArgumentParser(description="Screenshots for visual verification")
    parser.add_argument("--html", type=Path, help="HTML file to render")
    parser.add_argument("--app", action="store_true", help="screenshot the Streamlit app")
    parser.add_argument("--pages", default=",".join(ALL_PAGES), help="comma-separated page names")
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "dionice_screens")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    if args.html:
        print(f"[verify] {shoot_html(args.html, args.out)}")
    if args.app:
        for shot in shoot_app(args.out, [p.strip() for p in args.pages.split(",") if p.strip()]):
            print(f"[verify] {shot}")
    if not (args.html or args.app):
        parser.print_help()


if __name__ == "__main__":
    main()
