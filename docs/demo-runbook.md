# Demo runbook (P5.4)

The two demos as the presenter runs them, and the rehearsal that does the same steps in a headless Chromium
(`make demo-rehearsal`, `experiments/demo-rehearsal.yaml`). The two demos run on different profiles (D8), so a
`make down` and a `make up` sit between them.

## Before either demo

- Windows: plugged in, AC standby off (sleep kills a long run), and the ASUS lighting service stopped, as admin:
  `sc.exe stop LightingService` (P4.13d; `make demo-rehearsal` refuses to start while it runs).
- Once per machine, for the rehearsal only: `make demo-browser` (Chromium and its libraries, sudo).
- `make up` asks for sudo; run `sudo -v` first so it does not stop at the prompt.

## Practical demo: chat on the demo profile (1.7B, one pod)

Terminal 1:

```bash
cd ~/thesis
make down
make up PROFILE=demo
```

Then, each in its own terminal, after `make up` ends:

```bash
cd ~/thesis
make app-forward
```

```bash
cd ~/thesis
make ui PROFILE=demo
```

In the browser, `http://localhost:4200`:

1. Register a new user. The chat list and the cluster panel sit beside the chat.
2. **csv:** attach `experiments/data/sales.csv` and ask "What is the total revenue of the North region in sales.csv?".
   Show the steps: step 1 picks `csv`, `group_by` computes the number. Then, in the same chat and with no file, ask
   "And what is the total revenue of the South region?": the chat's history and its CSV answer it.
3. **contracts:** New chat, attach `agent/tests/fixtures/contract-2025.txt` and `contract-2026.txt`, ask "What changed
   in the termination clause between contract-2025.txt and contract-2026.txt?".
4. **support:** New chat, attach `agent/tests/fixtures/orders.csv` and `policies.txt`, ask "What is the status of order
   1043?".
5. **Out of scope:** New chat, no file, ask "Write a short poem about the sea.": the fixed reply, no tool call.

Rehearsal of this part, with the three terminals above running:

```bash
cd ~/thesis
make demo-rehearsal PART=practical
```

## Infrastructure demo: Start and Kill on the real profile (0.6B, two pods)

Stop `make ui` and `make app-forward` (Ctrl+C), then terminal 1:

```bash
cd ~/thesis
make down
make up
```

Then, each in its own terminal, after `make up` ends (the runner needs the token this `make up` made):

```bash
cd ~/thesis
make app-forward
```

```bash
cd ~/thesis
make ui PROFILE=real
```

```bash
cd ~/thesis
make demo-runner
```

In the browser, `http://localhost:4200`:

1. Register or log in. The cluster panel shows two pods Ready, remaining work and cache pressure at zero.
2. Pick `steady` and press **Start 20 tasks**: the replay sends the recorded demo runs through the gateway, and the
   panel's remaining work and the pods' running calls rise.
3. About a minute in, press **Kill pod** on the pod offered first. Its runs go on on the other pod; a call the kill cut
   is sent again. The Deployment starts a replacement, Ready in ~2-3 min.
4. The caption ends at `Replay done: 20 of 20 finished` once the last run ends.

Rehearsal of this part, with the four terminals above running:

```bash
cd ~/thesis
make demo-rehearsal PART=infrastructure
```

## What the rehearsal checks

Each part runs twice in a row, each round a new browser session and a new user, and is green when both rounds pass. A
round fails at the first of: a wrong answer or step 1 pick, an answer the page does not show, a replay that does not
finish every run or loses a call, a kill not taken, a replacement pod not Ready in time, a raw error or failure
caption on the page, or a working horse (the spinner) still trotting 15 s after its step ended. Each round's
screenshots, video and `round.json` go to `experiments/results/demo-rehearsal-<hash>-<part>-<time>/`; `HEADED=1`
shows the window.
