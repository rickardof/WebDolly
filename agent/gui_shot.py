import sys, time, json
from pathlib import Path
from playwright.sync_api import sync_playwright
out = sys.argv[1] if len(sys.argv) > 1 else str(
    Path(__file__).resolve().parent.parent / "var" / "gui_shots")
Path(out).mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1500, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append("console:" + m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8766/"); time.sleep(2)
    pg.screenshot(path=f"{out}/1_idle.png")
    pg.click("text=Compito e opzioni"); time.sleep(0.4)
    pg.screenshot(path=f"{out}/2_settings.png")
    pg.click("text=Attività")
    # start with empty task -> must be refused with a visible message, no alert
    pg.fill("#taskBox", ""); pg.click("#runBtn"); time.sleep(0.5)
    print("empty-task validation: error shown =", pg.is_visible("#taskErr"), "| switched to settings =", pg.is_visible("#pane-settings"))
    print("JS errors:", errs)
    b.close()
