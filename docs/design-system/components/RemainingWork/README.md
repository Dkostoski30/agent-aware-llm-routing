The signal KEDA scales on (D2, P3.8): Σ(expected − step) over active runs, now and over the last five minutes.

- The big number is the current value, in steps. The teal area is its history; the dashed line is the pod count on its own scale (legend says which).
- The scale line under it says what KEDA is doing now in words: target, min and max, or `2 → 4` while scaling.
- Must match the Grafana panel for the same metric (P3.11).
