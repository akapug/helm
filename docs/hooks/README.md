# docs/hooks/ — hook estate fragments

Each file here (`<hook>.md`) holds one markdown table row of the hook estate
table in `docs/HOOKS.md`, for the hook named `<hook>` in `helm.hooks.SPECS`.

A lane modifying or adding a hook edits or adds its fragment here, never in
`docs/HOOKS.md`. `docs/HOOKS.md` holds a pointer line that is assembled at
read time, preventing auto-land train conflicts across lanes that touch hooks.
