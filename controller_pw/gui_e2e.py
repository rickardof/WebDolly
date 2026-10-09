import sys, time, json
from playwright.sync_api import sync_playwright
OUT = "/home/riccardo/web-agent/gui_shots"
res = []
def check(name, ok, detail=""):
    res.append(ok); print(f"{'PASS' if ok else 'FAIL'}  {name:52s} {detail}")
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1500, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("dialog", lambda d: (errs.append("DIALOG:" + d.message), d.dismiss()))
    pg.goto("http://127.0.0.1:8766/"); time.sleep(1.5)

    # 1) empty task is refused inline (no alert), user is taken to the settings tab
    pg.click("#runBtn"); time.sleep(0.4)
    check("empty task -> inline error + settings tab", pg.is_visible("#taskErr") and pg.is_visible("#pane-settings"))

    # 2) run a task that makes the agent ASK a question mid-run
    pg.fill("#contextBox", "Persona: a software engineer from Milan.")
    pg.fill("#taskBox", "Use ask_user_question to ask the user which button to press (RED, GREEN or BLUE), "
                        "then click exactly that button on the page, then terminate.")
    pg.fill("#urlBox", "file:///data/test.html")
    if not pg.is_checked("#navigateOnStart"): pg.click("label.switch:has(#navigateOnStart) .slider")
    check("switch toggles by clicking it", pg.is_checked("#navigateOnStart"))
    pg.click("#runBtn"); time.sleep(1.5)
    check("run started: button shows Ferma, pill running", "Ferma" in pg.inner_text("#runBtn") and "esecuzione" in pg.inner_text("#pillText"))
    check("activity tab auto-selected", pg.is_visible("#feed"))
    t0 = time.time()
    while time.time() - t0 < 60 and not pg.is_visible("#answerBox"): time.sleep(0.5)
    check("agent question appears as pinned card", pg.is_visible("#answerBox"), pg.inner_text(".ask .q")[:70] if pg.is_visible(".ask .q") else "")
    check("header shows 'in attesa di te'", "attesa" in pg.inner_text("#pillText"))
    check("page title flags the pending question", pg.title().startswith("(!)"))
    pg.screenshot(path=f"{OUT}/3_question.png")
    pg.fill("#answerBox", "GREEN"); pg.click("#answerBtn")
    t0 = time.time()
    while time.time() - t0 < 90 and "Ferma" in pg.inner_text("#runBtn"): time.sleep(0.5)
    time.sleep(1.2)
    rows = pg.locator("#feed .row").count()
    check("run finished, button back to Avvia", "Avvia" in pg.inner_text("#runBtn"), f"{rows} feed rows")
    check("feed keeps the agent's question and my answer", pg.locator("#feed .row.ask-row").count() == 1 and "GREEN" in pg.inner_text("#feed"))
    check("feed shows steps + a final 'Completato'", pg.locator("#feed .row.step").count() >= 1 and pg.locator("#feed .row.done").count() == 1)
    pg.screenshot(path=f"{OUT}/4_finished.png")
    # expand a step
    first = pg.locator("#feed .row.step").first; first.click(); time.sleep(0.2)
    check("clicking a step expands its detail", "open" in (first.get_attribute("class") or ""))

    # 3) Stop button through the real UI
    pg.click("text=Compito e opzioni"); pg.fill("#taskBox", "Use the wait action with time 30 seconds. Then terminate.")
    pg.click("#runBtn"); time.sleep(4)
    t = time.time(); pg.click("#runBtn")
    seen_stopping = False
    while time.time() - t < 10:
        txt = pg.inner_text("#runBtn")
        seen_stopping = seen_stopping or "Fermando" in txt
        if "Avvia" in txt: break
        time.sleep(0.02)
    dt = time.time() - t
    check("Stop via UI: back to Avvia fast", "Avvia" in pg.inner_text("#runBtn") and dt < 3.0, f"{dt:.2f}s (saw 'Fermando…': {seen_stopping})")
    check("stopped run is labelled 'Fermato'", pg.locator("#feed .row.done.stopped").count() >= 1)
    pg.screenshot(path=f"{OUT}/5_stopped.png")
    check("no JS errors / alert dialogs", not errs, str(errs)[:200])
    b.close()
print(f"\n{sum(res)}/{len(res)} passed")
