# Security model

WebDolly drives a real browser on pages you do not control. Assume that any
page the agent opens may be hostile, and that the machine is yours alone.

## What is in place

- **Everything listens on loopback only.** The panel (8766), the model server
  (8090), the browser's debugging port (9333) and VNC (5901) are published on
  `127.0.0.1`, never on the network.
- **Chromium runs with its own sandbox.** A hostile page that finds a browser bug
  gets a process with no filesystem, no network namespace and a syscall filter,
  not a shell in the container. The container allows this with a seccomp profile
  ([docker/seccomp-chromium.json](../docker/seccomp-chromium.json)): everything
  Docker's default profile allows, plus the user namespaces the sandbox needs.
  `chrome://sandbox` in the agent's browser reports "You are adequately
  sandboxed."
- **The containers are not privileged.** The browser runs as an ordinary user
  with no Linux capabilities, `no-new-privileges` and a process limit. The model
  container cannot write to `models/`.
- **The panel only answers its own page.** A request must be addressed to a
  loopback name and, if it carries an `Origin`, that origin must be the panel
  itself. Scripts and `curl` send no `Origin` and are unaffected. Current Chrome
  already refuses to let a public website send requests to a local address (it
  was tested from inside the agent's browser: nothing arrived), so this is a
  second layer, for other browsers and other programs. See
  `tests/panel_guard_test.py`.
- **Secrets stay on the host.** The Telegram token lives in
  `agent/telegram_config.py` and the browser's cookies in `var/profiles/`; both
  are git-ignored and never mounted into the model container.
- **The agent never types a password.** When the account is signed out the run
  stops with a message and you sign in yourself.

## Limits to know

- **Code running inside the browser container can reach your PC's local
  services.** The container reaches the host as `host.docker.internal`, and the
  panel, the model server and VNC answer there. A web page cannot do it (see
  above), but a program could: that is what the sandbox is for. If the sandbox
  were broken, the next barrier would be the container itself, which is
  unprivileged and has no capabilities.
- **The model server has no API key and answers any origin.** It exposes
  inference and counters, not prompts. The practical exposure is use of your GPU
  by something that can reach it.
- **The debugging port and VNC have no password.** Any program running as a user
  on this PC can drive the browser and its signed-in sessions. That is fine on a
  single-user PC and not on a shared one.
- **A hosted model sees your screen.** See below.
- **On a server,** keep the ports on loopback and reach the panel through an SSH
  tunnel. Never publish 8766, 9333 or 5901.

## The model and your screenshots

At each step the agent sends the model one request: the task, the conversation
so far and a full-resolution screenshot of the browser. By default the address
is the model container on your own machine (`http://127.0.0.1:8090`), so the
screenshots never leave it.

If you set `FARA_URL` and `FARA_API_KEY` to use a hosted model, the same request
goes over the internet to that provider. Then everything visible in the browser
(signed-in pages, names, balances) and your task text and context leave your PC
at every step. Use a provider you trust with that, and a dedicated account.

## Applying the container settings

The flags above are set when `setup.ps1` / `setup.sh` create the containers, and
the sandbox change is in the browser image, which setup rebuilds. Containers that
already exist keep the settings they were created with: run the setup again to
get them. The browser profile in `var/profiles/` is kept, but you may have to
sign in again.

If Chromium does not start after the change, the host kernel probably has no
user namespaces. Set `FARA_NO_SANDBOX=1` before running the setup to start it
without the sandbox (weaker: a browser bug would then run as the container's
user, with network access and the profile's cookies).
