# GUI design specification

Reference: `gui-concept.png` (1536 × 1024), generated with the built-in Image Gen tool.
Brief: full Japanese desktop post-management screen, left scrollable account/filter
rail, right table/detail workspace, bottom persistent action bar. Preserve existing
profile, collection, filtering, selection, stop and delete workflows; no new navigation.

## Design system

- Graphite background #11151c; settings/workspace #191f29; inputs #151b24;
  borders #303a49; text #e3e9f3; secondary #a2afc2; primary #8296ff; danger #b85b70;
  quiet danger outline #6a3845 / text #eba3b2.
- Flat native controls, no image overlays or decorative assets; 8px corner radius on
  panels, inputs, buttons and the table (progress bar and scrollbars keep smaller radii).
- Noto Sans CJK JP / Yu Gothic / sans-serif. Body and controls 13px,
  section titles 16px semibold, product title 22px semibold, auxiliary 12px.
- 12px spacing between control groups, 6px between a label and its field,
  16px panel insets, 20px outer gutter, 350px settings rail.
- Families: primary/secondary/danger/quiet-danger buttons, labelled input fields, native checkboxes,
  sortable table with 44px rows, detail editor, collapsible log, progress/status footer.
- Controls retain native checkbox/spinner/disclosure symbols for platform accessibility.
- Main workflow: profile/login → filter/collect → inspect/select → confirm/delete.
- Smaller desktop windows scroll the settings independently, keeping the footer accessible.
  This is a desktop Qt application, not a mobile web application.

## Copy and intentional deviations

Use all existing input and action labels. Add only the concept title/subtitle,
`0 件 / 0 件選択`, and empty-state copy for three moments:

| Moment | Title | Hint |
| --- | --- | --- |
| Before collecting | `まだポストがありません` | `ログイン後、条件を指定してポストを収集してください。` |
| Collection found nothing | `条件に一致するポストはありませんでした` | `期間やフィルタを広げて、もう一度収集してください。` |
| Every row was processed | `選択した項目をすべて処理しました` | `続けて整理する場合は、条件を指定してもう一度収集してください。` |

The solid danger button is reserved for the footer delete action. Profile deletion uses
the quiet-danger outline so it stays distinct from add/rename without competing with it.
The post text column keeps at least 240px; the URL column starts at 180px (full URL in
the tooltip and preview) and narrower windows scroll the table horizontally.
Keep existing combined `すべて選択/解除` and `ブラウザ停止 / 処理中止` controls
instead of the image model's invented separate actions. Use real saved/default dates.
Native window decorations are controlled by the operating system. Flat surfaces replace
image rendering texture; the concept is a layout/style reference, never a UI bitmap.

## Verification ledger (2026-09-28)

Reference and final screenshots were directly inspected with `view_image` in the
same QA pass. Screenshots use the real Qt widgets (`QWidget.grab()`), the production
Fusion palette, isolated settings, and the offscreen Qt platform. Browser/IAB and
Playwright do not render this native QWidget application, so browser/mobile QA is
not applicable; no HTML stand-in was used.

| Comparison | Reference / implementation evidence | Resolution |
| --- | --- | --- |
| Layout | Left settings, right table/details, fixed footer in concept and `gui-desktop.png` | Replaced stacked full-width settings with a 350px scrollable rail. |
| Typography | Hierarchical title, section headings, quieter helper copy | Explicit 22/18/16/13/12px sizes; compact profile actions use 11px. Native font rendering differs from generated pixels. |
| Palette | Graphite surfaces, blue-violet primary, muted red destructive action | Locked flat color tokens and matched the application palette, including dialogs. No generated texture/gradient. |
| Spacing | Large table canvas, small detail pane, compact log | Fixed original 988px minimum-height overflow; 980×640 now works. Reduced field gaps so the full rail fits at 1536×1024. |
| Copy | Existing labels plus concept title, subtitle, selection count and empty-state guidance | No unrelated copy added. Combined select/stop actions retained; original `プロファイル選択` label retained, generated `詳細` heading omitted. |
| Controls/icons | Native dropdown, date, spin, checkbox and log controls | Platform-native glyphs intentionally retained, including checkable log instead of generated disclosure icon. |
| Empty/populated state | `gui-desktop.png` / `gui-populated.png` | Empty guidance disappears on collection; count tracks checks, toggles and row removal. 2026-09-28 review: empty copy now distinguishes no-match and all-processed; text column regression (43px) fixed and covered by a test. |
| Compact layout | `gui-compact.png`, 980×640 | Settings scroll vertically and table horizontally; footer stays accessible. Table retains 154px height. |

The implementation was compared against the adopted concept for layout and design
system fidelity. It deliberately adapts the generated mockup to native widgets;
it is not a pixel-identical reproduction. No known content-overflow blocker remains
at the checked desktop sizes. OS window frames and other operating systems' font
rendering were not validated.

Scoped validation: 63 tests passed (`test_x_deleter_gui.py`, `test_main.py`,
`test_integration.py`), Ruff passed, project Pyright passed with zero errors/warnings.
Regression checks include compact sizing/scroll reachability, empty/selected count
updates and actual log folding, plus existing profile/login, collection, selection,
confirmation and action-result paths. Screenshots contain local synthetic posts;
no real X account was opened and no real post was deleted. Live X and full browser
DOM checks are outside this GUI assignment, not counted as passes.

Final review artifacts: [desktop](gui-desktop.png), [populated](gui-populated.png),
[compact](gui-compact.png), [concept](gui-concept.png).

## Main-window workflow refinement (2026-11-05)

The collection button now stays below the scrolling settings rail. Login and
filters precede a separate following group; diagnostics, current-post actions and
autosave timing are under a collapsed settings toggle. Existing signals, login
checks, confirmations and initial checked targets remain unchanged.

Selection/save/wait controls and interval settings occupy separate toolbar rows.
The header is a single row so the compact layout retains a 153px table at
980×640. Counts and action labels distinguish checked processing targets from
row selection for text preview. Help now explains profiles, the collection flow,
initial checked targets, stop results and keyboard shortcuts in Japanese/English.

Current widgets were rendered offscreen with isolated XDG directories and synthetic
posts at 1440×960 and 980×640 in both languages. Toolbar buttons meet their Qt
size hints; collection remains visible at both scroll extremes. Scoped GUI and
translation checks: 98 passed. Ruff passed for changed Python files; Pyright:
zero errors/warnings. Real X operations, other OS rendering and screen-reader
behavior were not checked. Older screenshots above document the prior layout.

### Full-width settings overview (2026-11-05, supersedes the rail layout)

Settings and posts now have separate native Qt tabs. Settings use three visible
columns: account/following, collection filters, and action pacing/diagnostics.
Retry settings and diagnostics are no longer collapsed. The collection button
stays below the settings scroll area. Collected or imported results switch to
Posts; the wait/retry button returns to Settings and focuses the interval field.
Existing widgets are retained across tab changes, preserving settings and rows.
The batch-delete footer button is shown on Posts only; its confirmation and
keyboard shortcut retain the existing behavior.

Offscreen native renders with isolated XDG directories verified Japanese/English
at 1440×960 and 980×640. At 1440×960 all settings fit without scrolling. At 980×640
settings scroll vertically, with no horizontal scrollbar and collection still
visible. The posts table retains 162px height at the compact size. Regression
checks cover tab navigation, automatic result switching, retained input values,
full-size settings visibility and compact action widths. GUI/i18n: 99 passed;
Ruff and Pyright passed. Live X, other OS rendering and screen readers remain
outside this verification.

### GUI recipe loading (2026-11-05)

The header now offers Load TOML settings while the browser and jobs are stopped.
It uses the CLI recipe parser, applies the selected profile and filters, and
preserves all RetryPolicy fields in GUI profile settings. Collection-only recipes
disable deletion; confirmations remain manual regardless of the recipe's confirm
flag. Archive and output paths resolve beside the recipe; saving lists is manual.
Relative age filters become visible absolute dates at load time. The source label
explains these GUI semantics. Loading never changes the source TOML or starts work.

Additional wait/traffic controls and file paths remain visible in the three-column
settings overview. Traffic/isolation modes are locked while the browser is running
because they must reach the core before browser startup. Values that cannot be
represented by the GUI are rejected before changing profile, filters or rows.
The GUI supports 32-bit positive collection counts and up to 12 decimal places for
imported timing values; profile reload retains the imported decimal precision.

Japanese/English offscreen renders were inspected at 1440×960 and 980×640 using
synthetic recipes and isolated XDG directories. Both widths avoid horizontal
settings scrolling; the loaded-recipe notice adds a small vertical scroll at the
larger size. Collection remains fixed below the scrolling overview. The scoped
GUI, translation, settings, recipe, worker and integration suite passed 231 tests;
Ruff and Pyright passed. Real X execution was not performed.
