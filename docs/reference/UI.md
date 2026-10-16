# Title conventions

Use `Heading` from `frontend/src/components/Heading.tsx` for page, section,
subsection and dialog titles. It forwards heading attributes and refs so focus
management, IDs and accessible names work without special wrappers.

Choose the semantic `level` for the document hierarchy, then the visual
`variant` for the title's role. Do not select a heading level just to change its
font size. The default is a level-two section heading.

| Role | Variant | Mobile / desktop size |
| --- | --- | --- |
| Page or sign-in title | page | 24px / 24px |
| Introductory heading | intro | 18px / 20px |
| Card, section or dialog title | section | 16px / 16px |
| Subsection or list-item heading | subsection | 14px / 14px |
| Metric label with heading semantics | label | 12px / 12px |

Weights, line heights and tracking live in the shared CSS classes. Use sentence
case; uppercase is reserved for short metric labels. Prefer concise, descriptive
titles (usually two to five words), but never truncate account, merchant or
security names to meet a length target. Titles wrap and have no fixed height.
Inline counts, currency labels and supporting descriptions may use smaller,
muted text; they should not compete with the title.

Keep per-page classes for layout, spacing, responsive visibility and meaningful
status colours. Avoid per-page font-size, font-weight or tracking overrides.
Charts keep their existing plots and interactions; only surrounding title
typography follows this hierarchy.

The implementation uses component composition and encapsulation rather than
parallel page-specific title implementations. Dialogs likewise share
`trapDialogFocus`, including visibility checks for collapsed disclosures and
disabled controls.

## Shared visual and interaction rules

Body text and control labels use 14px; supporting captions use 12px. Keep
meaningful status, empty-state and placeholder text opaque so it retains
contrast while loading. Values use tabular numerals and a separate metric scale.
Standard cards have 16px padding; table and chart containers may opt out.
Phone buttons and disclosure rows have at least 44px touch height; native text
controls use 16px on coarse pointers. Desktop pointer controls may use 36px.
Inline table evidence may use a compact 24px row on fine desktop pointers.
Conditional filter actions must preserve the surrounding row height.

Use `Disclosure` for boxed optional sections. Its centered header owns collapsed
spacing, title typography and chevron; only its content owns expanded padding.
Forward native `open`, `onToggle`, details refs and `summaryRef` for focus/recovery.
Keep grids containing independent disclosures aligned at their start so a closed
neighbor does not stretch to the height of expanded content. Settings retains
visited content to preserve drafts when a section closes.

`Modal` keeps long content scrollable between its header and optional footer.
Use `DialogActions` and the form's ID for footer submission so required validity
and Enter submission still work. Busy actions remain disabled. `DialogSession`
owns scroll locking and opener-focus restoration for custom and native dialogs;
`trapDialogFocus` handles the visible keyboard loop. Fieldsets must shrink inside
narrow dialogs; document overflow alone cannot detect clipped descendants.

Primary workspace tabs use the same active style and scroll horizontally when
enlarged phone text needs space. Secondary explanations may collapse, while
missing evidence, partial totals and the action to inspect them remain visible.
Focused workspace tabs scroll into view. Phone scroll insets follow the measured
sticky header and bottom navigation heights, including enlarged text; dialog
focus remains inside its own scrollable body. Loading wrappers and placeholder
widths shrink with their container, and portfolio placeholders reserve the same
coverage-action and value geometry as loaded content.

For synthetic touch screenshots, measure controls before capture. On this host,
Chromium full-page capture can reset coarse-pointer emulation; viewport capture
preserves it. Root-font enlargement is a useful stress probe, distinct from
physical-device zoom or assistive-technology validation.
