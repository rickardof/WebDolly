# Security model

WebDolly drives a real browser on pages you do not control. Assume that any
page the agent opens may be hostile, and that the machine is yours alone.

## What is in place

- **Everything listens on loopback only.** The panel (8766), the model server
  (8090), the browser's debugging port (9333) and VNC (5901) are published on
  `127.0.0.1`, never on the network.
- **The panel only answers its own page.** A request must be addressed to a
  loopback name and, if it carries an `Origin`, that origin must be the panel
  itself. Websites open in your browser, or in the agent's, cannot start or stop
  runs, type, or read the screen through it. Scripts and `curl` send no
  `Origin` and are unaffected. See `tests/panel_guard_test.py`.
- **The containers are not privileged.** The browser runs as an ordinary user
  with no Linux capabilities, `no-new-privileges` and a process limit. The model
  container cannot write to `models/`.
- **Secrets stay on the host.** The Telegram token lives in
  `agent/telegram_config.py` and the browser's cookies in `var/profiles/`; both
  are git-ignored and never mounted into the model container.
- **The agent never types a password.** When the account is signed out the run
  stops with a message and you sign in yourself.

## Limits to know

- **Chromium runs without its own sandbox** (`--no-sandbox`), which is what lets
  it run unprivileged in a container. A browser exploit served by a hostile page
  would run as the container's user, with network access and the cookies of the
  logged-in profile. It would not reach your PC without a second, container
  escape bug. Keep Docker and the image up to date (rebuild now and then), and
  prefer accounts you could afford to lose.
- **The browser container can reach the host and the model server.** The model
  server has no API key and answers any origin; it exposes inference and
  counters, not prompts. The practical exposure is a page using your GPU.
- **The debugging port and VNC have no password.** Any program running as a user
  on this PC can drive the browser and its signed-in sessions. That is fine on a
  single-user PC and not on a shared one.
- **A hosted model sees your screen.** With `FARA_API_KEY` set, screenshots and
  the task text are sent to that provider.
- **On a server,** keep the ports on loopback and reach the panel through an SSH
  tunnel. Never publish 8766, 9333 or 5901.

## Applying the container settings

The flags above are set when `setup.ps1` / `setup.sh` create the containers.
Containers that already exist keep the settings they were created with: run the
setup again to get them. The browser profile in `var/profiles/` is kept, but you
may have to sign in again.
