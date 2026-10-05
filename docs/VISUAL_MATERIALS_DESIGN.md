# Materials and infographic design

## Scope

The visual phase uses the supplied burgundy/rose palette throughout deterministic
infographics, material-quality cards, and DOCX/XLSX presentation. Orange is not
part of the theme. It does not change what counts as evidence or authorization.

The palette has separate semantic roles: pale background, very dark body text,
burgundy headings, muted-rose secondary text, rose data fills, and neutral
borders. Small text is never coloured with low-contrast pink/red on the pale
background. Saved style dictionaries remain readable; style-only changes do not
change the factual hash. Native requests do not inherit project style sources.

## Presentation contracts

- All nine declared infographic formats have dedicated composition rules.
- Cards are a document overview; comparison/table present aligned values and
  explicit missingness; statistics use a shared quantitative scale; timeline and
  roadmap show ordered steps; process, arguments and teaching expose their
  structure without promoting relationships to proven causal claims.
- Values, units, intervals, status words and source references are retained.
  Negative values stay negative. Subpixel values retain their exact labels.
  Missing data is not converted to zero. A proposal quoted from a source remains
  a quoted proposal, not an accepted decision.
- PNG, SVG and PDF are exports of one measured scene. Long Russian text wraps or
  continues on another page instead of being silently dropped or shrunk.
- DOCX contains editable text, status/value tables, source details and the visual
  result. XLSX retains the original data coordinates and exact normalized text,
  with styled headers, source details, readable widths and frozen rows. Exported
  formula-looking text must remain text on every worksheet.
- Telegram completion uses one preview, a short caption and the existing source,
  format and revision actions. Processing/status cards remain text. Actual file
  outputs are delivered as a file or one ZIP package when multiple files exist.
  The durable delivery slot remains authoritative: ambiguous sends do not retry.
- Material summary and diagnostic cards describe extraction/dataset coverage and
  retained excerpts. They do not claim to establish the quality of a live model.

## Reproducible validation

Run only with explicit disposable PostgreSQL and the offline environment guard:

```
python -m tools.generate_visual_samples --output temp/visual-design-samples
python -m tools.verify_visual_design --suite focused
python -m tools.verify_visual_design --suite all
```

The sample tool creates owned synthetic DOCX/XLSX sources, ingests and extracts
those files, validates every factual quote/number against persisted sources,
then exports three review examples and a first-page raster for all nine formats.
It also creates PDF-raster and phone-size previews for separate pixel inspection.
`manifest.json` identifies the source checks and byte hashes. Generated files live
under ignored `temp/`, not as real user fixtures in the repository.

The validation runner freezes a source fingerprint before testing, verifies it
again afterward and records exact test counts/failures/skips. Focused validation
is not a full-suite claim. A later code or test edit requires rerunning affected
checks and the final aggregate.

## Boundaries

No live provider, Telegram session, production database or real conversation is
required for these checks. A successful offline result is not a live rollout or
visual user study. Native Telegram controls cannot use arbitrary HEX colours;
there is no new Mini App. JSON/CSV remain machine-readable formats.

Evidence verification in `artifacts/validation.py` is unchanged: a verified text
claim must equal its source quote. Styling does not turn interpretations into
verified text or import unselected materials. Source deletion, permission
changes, task/artifact revision and cancellation still fence delivery.

## Telegram entry points

- Reply to a document with `/material_summary` for extraction coverage, explicit
  limitations and at most three literal retained fragments. This is deliberately
  an extraction overview, not a model-authored semantic summary.
- `/dataset` and `/calc` retain their existing copyable result text and add one
  optional compact diagnostic preview. A renderer failure falls back to text;
  an ambiguous photo send is not retried as a different result.
- Existing artifact completion, format and source buttons are reused. A requested
  file export takes precedence over its intermediate illustration/artifact.
  Independent generated files are packaged together without losing their names.

Office validation uses real LibreOffice conversion plus raster inspection.
Microsoft Word/Excel native-app rendering is not separately established. Very
long Excel cells retain their full value but carry a comment when Excel's maximum
print row height can hide part of the printed display. Values exceeding Excel's
32,767-character cell limit are rejected instead of silently truncated.

SVG embeds a per-page Unicode font subset, keeping multi-page exports within the
existing tool byte budget. Font glyph masks/widths and SVG geometry are compared
to the original bundled font and PNG/PDF scene. A Chromium raster check was
attempted but this execution environment denied Chromium's local singleton
socket, so browser-specific SVG font acceptance is not claimed.

The installed independent Inkscape renderer also produced the three final SVG
preview rasters; those were inspected for correct Cyrillic, values, geometry and
absence of clipping. It warns that `font-kerning` is unsupported, so typography can
vary slightly by renderer. This does not establish Chromium/browser-specific
font acceptance. The three checked primary PNGs are included under
`docs/evaluation/visual-samples/` (about 272 KiB total); full generated PDFs and
editable office files remain reproducible outputs rather than repository blobs.

## Frozen review checkpoint

The focused visual/native/acceptance suite passed 127/127 checks without skips.
An independent safety/source reviewer passed a separate 113-test scoped suite,
also without failures, errors or skips, on unchanged source fingerprint
`701d5534b5688439ed53e230957fb6ed1d064ce589926ca92cdd877bff602352`.
The full aggregate result is recorded separately in
`docs/evaluation/visual_design_validation.json`; neither focused result is used
as a substitute for that aggregate.

Final full aggregate: **1418/1418 passed**, zero failures/errors/skips, on the same
unchanged fingerprint. The runner finished at 2026-10-05 20:17:44 UTC in 791.904 s
(test execution 786.120 s). No code or test changes followed that aggregate.
