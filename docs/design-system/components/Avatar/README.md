The agent's avatar: an 8-bit horse, 14 by 12 art pixels, one colour, its eye a hole that shows the ground.

It appears in three places only: the top bar (2x), the run header and the Answer heading (2x), and the New task screen (4x).

- **States.** `data-state="idle"` stands still and blinks every 4.2 s. `data-state="working"` trots, two frames at 0.36 s, while a run is in progress; the run header and the pending Answer heading use it. Under `prefers-reduced-motion` the working horse stands still.
- **Size.** Only whole multiples of 14px wide (`horse-1x` to `horse-8x`), so every art pixel stays square. Never scale it to an odd size.
- **Colour.** Fill is `avatar`, which follows `accent`: teal in both themes. The eye is a hole, so it always shows the ground.
- **Markup.** `AgentUI.horse({state, size})` returns the SVG: one path for the fixed head and back, three for the leg frames (`stand`, `trot-a`, `trot-b`) and a one-pixel `lid` for the blink. The CSS in `bundle.css` does the animation; no script runs.
- Don't add outlines, shadows, gradients or a second colour; don't put it inside a circle; don't flip it to face left.
