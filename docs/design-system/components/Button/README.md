Buttons in four kinds and two sizes. Pass a label, an optional icon name, and a kind.

- **Primary** (`ag-btn--primary`, `accent` with `on-accent` text): one per area. Send, Start 20 tasks.
- **Secondary** (default): everything else that acts.
- **Ghost**: top-bar actions and close buttons.
- **Danger** (`ag-btn--danger`, `failed` outline and text): Kill pod only. It acts at once with no confirmation, because the demo depends on timing; the pod select beside it names the target.
- Disabled buttons keep their label and say why nearby (Start is disabled while a replay runs, with the replay's progress in the caption).
- Icon-only buttons need an `aria` label.
