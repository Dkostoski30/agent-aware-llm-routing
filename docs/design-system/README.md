Tenax is an agent that works through a task in a chain of LLM calls, shown together with the cluster that serves those calls. The name is Latin for "holding fast": a run holds on to the pod that has its context, and holds on through a pod's death. A person picks a scenario and types a task; the agent plans, calls deterministic tools and answers. Every call is a visible step that says which vLLM pod served it and whether that pod already had the task's context cached. Beside the conversation, always on screen, is the cluster: pods, queues, the remaining work that drives autoscaling, and the two demo buttons.

The look takes its cues from Perplexity: crisp and dense, the task set as a heading, the agent's steps shown as numbered evidence cards, an answer that cites them. Dark is the default theme; light is there for projectors.

## Screens

Build exactly these. Each is a card in the Screens group, at 1440 × 900.

| Screen | Card | What it shows | Plan |
| --- | --- | --- | --- |
| New task | `NewTaskScreen` | Horse, composer, the three scenarios as starters; cluster idle | P1.4, P2.1 |
| Run | `RunScreen` | Steps arriving over SSE, a parallel pair running, the answer slot waiting | P1.4, P2.6, D6 |
| Answer | `AnswerScreen` | Evidence cards and the answer citing them | P1.4, P2.6 |
| Steps tab | `StepsTabScreen` | The run as a table in the trace fields; must match the recorder's log | P2.6, D12 |
| Step detail | `StepDetailScreen` | One step: call, result, routing, timing, headers | P2.6, D1, D10 |
| Burst | `BurstScreen` | Start 20 tasks: parallel spill, scale-up, cold start, priority order | P4.6, P3.6, P3.9, P4.1, P4.2 |
| Pod failure | `PodFailureScreen` | Kill pod mid-run: the step is retried on the other pod from saved history | P4.4, P3.4, D7 |
| Drain | `DrainScreen` | Scale-down: a draining pod finishes its run; warm-up routing on the newest pod | P4.3, P4.2 |

## Layout

- Split view on every screen: the conversation on the left, the cluster panel on the right at `panel-width`, under a top bar of `topbar-height`. The panel never collapses, so the audience sees a step land on a pod, and a pod scale or die, in one glance.
- Conversation content is centred at `content-max`, padded `space-10` at the sides and `space-8` at the top. The composer docks at the bottom.
- The cluster panel scrolls; the demo controls stay pinned to its bottom.
- The step-detail sheet opens at `sheet-width` over the right edge of the conversation column, on a `scrim`, never over the panel.
- Run blocks are separated by `space-6`; panel sections by a `line` rule with `space-3` / `space-4` padding.

## Content

- The name is Tenax: one word, capital T, no article ("Tenax is working", never "the Tenax"). In the top bar it is set in `label` at weight 700 beside the horse; there is no separate wordmark.
- Plain sentences in sentence case. No exclamation marks, no emoji, no "Oops".
- The agent speaks only in its answer. Everything else is the system speaking, terse and impersonal: "Step 3 · 5 expected", "Killed", "Retried on vllm-1".
- A notice is one bold sentence saying what happened and one plain sentence saying what the system did: "**vllm-0 was killed during step 4.** The call was retried on vllm-1 with the run's history from Redis."
- Name things exactly as the system emits them and set them in mono: tools (`extract_clauses`), arguments (`contract_b`), pods (`vllm-0`, `sim-2`), runs (`run-7f3a9c`), headers (`x-expected-steps`), profiles (`real`), config keys (`overload_guard`). Never prettify or capitalise them.
- `expected` is an estimate: write "Step 3 · 5 expected", never "Step 3 of 5".
- Times: under a second in ms (`290 ms`), above in seconds with one decimal (`1.6 s`); durations on a clock as `1:03`. Percentages without a space (`92%`). Thousands with a comma (`1,834`).
- A state is always a word, never only a colour: Ready, Starting, Draining, Killed; hit, miss; retry 1; spilled.

## Visual foundations

**Colour.** Neutral surfaces, one teal accent, four status hues.

- Grounds: `bg` for the page, `surface` for the panel, composer, cards and sheet, `surface-2` for step cards, pod cards, tags and code, `surface-3` for hover, selection and meter tracks.
- Text: `ink` for content, `ink-muted` for metadata. Both hold at least 4.5:1 on every ground in both themes.
- `line` is decoration only (panel edge, dividers, the tab rule). Any border that marks a control uses `line-strong` (3:1).
- `accent` marks the one thing to do or watch: the primary button, the active tab, the running step, the remaining-work line, the horse. Text on it is `on-accent`, which is dark in the dark theme because the teal is lightened there. `accent-soft` with `accent-ink` is the quiet version: cache hits, citations, your own run, the selected card.
- Pod states each have a dot, a soft ground and an ink: `ready` (green), `starting` (blue), `draining` (amber), `failed` (coral). Starting is blue so it never depends on telling green from amber. The same palettes mark step events: `retry` uses failed, `spilled` uses draining.
- A cache miss is an outline, never red: misses are expected on the first step and after a move.

**Type.** Hanken Grotesk for the interface, JetBrains Mono for everything the machine says.

- `display` once per screen (New task). `title` for the task heading of a run. `heading` for section heads. `body` for the answer and composer. `body-sm` for notices and notes. `label` for buttons, tabs and chips. `caption` for metadata. `overline`, set in capitals, for panel section labels.
- `data` for tool calls, results, headers and ids; `data-sm` for tags; `metric` for the one big number in a panel section. Numbers in tables are tabular.

**Shape.** Flat surfaces separated by `line` and by ground changes, not by shadows.

- `radius-xs` for step numbers and citations, `radius-sm` for tags, chips and selects, `radius-md` for cards and buttons, `radius-lg` for the composer and the sheet, `radius-full` for switches and dots.
- `shadow-raise` on the composer and the segmented control's active segment only; `shadow-pop` on the sheet only.
- Dashed borders mean "not yet" or "on its way out": pending steps, starting pods (`starting`), draining pods (`draining`).

**Focus.** A 2px solid `focus-ring` with a 2px offset on every interactive element; at least 4.6:1 on every ground in both themes.

**Motion.** Only where something is in progress, and all of it stops under `prefers-reduced-motion`.

- The horse trots while a run is in progress (two frames, 0.36 s) and blinks when idle (every 4.2 s).
- Running progress segments pulse (1.2 s); the Live dot pulses (2 s).
- Nothing slides or bounces except the sheet, which slides in from the right in 200 ms, ease-out.

## The avatar

An 8-bit horse, 14 × 12 art pixels, one colour (`avatar`, which follows `accent`), its eye a hole that shows the ground. It appears in the top bar, the run header, the Answer heading (all `horse-2x`) and the New task screen (`horse-4x`). Use only whole multiples of 14px so the pixels stay square. `data-state="working"` while a run is in progress, `idle` otherwise. Never outline it, put it in a circle, add a second colour or flip it. The Avatar card has the frames and the rules.

In the app, always render the horse with `AgentUI.horse()` so it animates and follows the theme. Outside the app (favicon, slides, the thesis document) use the still SVGs in the Logos asset group: `tenax-horse-on-dark.svg` on dark grounds, `tenax-horse-on-light.svg` on light ones.

## Iconography

A small set of 16px line icons with a 1.5 stroke and round caps, drawn for this system and served by `AgentUI.icon(name)`: `send`, `plus`, `moon`, `chevron`, `close`, `table`, `doc`, `chat`, `play`, `power`, `info`, `warn`, `retry`, `pods`, `clock`, `spill`. They inherit `currentColor`. Icons sit beside a word; the only icon-only buttons are send, close and the theme switch, and each has an `aria-label`. No emoji anywhere.

## Using the components

- `components/bundle.css` holds every component as plain CSS classes prefixed `ag-`, built only on the tokens. Load it after `tokens.css`. It also loads the two fonts from Google Fonts.
- `components/bundle.js` defines `window.AgentUI`: one function per component that takes plain data and returns its markup (`AgentUI.stepCard({n, tool, args, pod, cache, ms, par, state})`, `AgentUI.podCard({...})`, `AgentUI.app({top, main, panel, sheet})`). It has no dependencies and no network calls. The Angular app ports each function to a component with the same inputs and the same classes; the previews are the reference.
- Every component card says which data it needs. Step data follows `schemas/trace.v1.json` and `schemas/headers.v1.json`; pod data comes from vLLM's metrics; remaining work and run ranks come from the run tracker; routing flags come from the active `config/` profile and are read-only in the UI.
