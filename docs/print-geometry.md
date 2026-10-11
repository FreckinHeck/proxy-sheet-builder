# Print geometry

This is the part that took the longest to get right, and the part most likely
to be useful to someone else. Everything below was validated with real test
cuts, not calculated and hoped for.

## Card dimensions

A real Magic card trims to **63 × 88 mm**. That is the number to cut to, and
it is what `trimW` / `trimH` default to.

Add **0.6 mm of bleed** on every side, giving a printed tile of 64.2 × 89.2 mm.
The bleed exists because no cutter is perfect: if you print exactly to the trim
line, a 0.3 mm registration error shows as a white sliver on one edge. With
bleed, the same error just eats into art that was always going to be discarded.

0.6 mm is deliberately modest. It matches what proxxied.com uses, and it is
enough to hide cutter error without wasting so much art that borders start
looking thin.

## The two art sources do not agree, and that matters

Scryfall images are already trimmed to the card edge. What you get is exactly
63 × 88 mm of card and nothing more.

MPC Autofill images are **print-ready canvases with bleed baked in**. The MPC
spec canvas is 2.72 × 3.70 inches — **69.088 × 93.98 mm** — with the card's
trim line sitting centred inside it:

```
canvas   69.088 × 93.98 mm
trim     63.5   × 88.9  mm   (MPC's own trim target)
margin    2.794 ×  2.54 mm   per side
```

Drop an MPC image into a slot sized for a Scryfall image and you get a card
with a visibly fat border, because you have scaled 69 mm of canvas down into
63 mm of slot. That is the cause of the "why do some cards have huge borders"
problem.

### The fix

Crop the MPC canvas back to **its own trim line** before placing it, then let
the normal bleed logic take over. As fractions of the canvas:

```
x = (69.088 − 63.5) / 2 / 69.088 = 0.04044   →  4.04 % off each vertical edge
y = (93.98  − 88.9) / 2 / 93.98  = 0.02703   →  2.70 % off each horizontal edge
```

The subtle part: crop to MPC's **trim** line, not to your **cut** line. An
earlier version of this code used 3.17 %, which mapped MPC's canvas onto the
cut line instead. The result was that MPC images kept a sliver of extra border
while Scryfall images lost 0.6 mm of real art — the two sources still did not
match, just less obviously. Cropping to trim makes both sources fill the slot
identically, and the bleed setting then applies equally to both.

This is `mpcCrop()` in `web/index.html`, and it only runs for entries that
actually came from MPC (`viaMpc && mpcId`).

## Silhouette registration marks

These are not arbitrary. The Silhouette software looks for marks at a fixed
offset from the page corner, and if your PDF puts them anywhere else the
machine fails to register. The values below are Silhouette's own defaults:

| Property           | Value              |
| ------------------ | ------------------ |
| Inset from corner  | 0.394 in (10.0 mm) |
| Square mark size   | 5.92 mm            |
| L-bracket arm      | 0.350 in (8.89 mm) |
| Line thickness     | 0.039 in (0.99 mm) |

Top-left is a filled square; the other three corners are L-brackets pointing
inward. Getting this wrong is the single most common reason a sheet will not
register — an early version of this tool drew the arms at 0.62 in, roughly 3×
too large, and the cutter simply refused to see them.

Measure a known-good PDF from whatever tool you currently use if you want to
confirm against your own machine. That is how these numbers were arrived at.

## Page presets

| Preset         | Page                  | Grid | Marks | Notes                        |
| -------------- | --------------------- | ---- | ----- | ---------------------------- |
| Letter         | 279.4 × 215.9 (lands.) | 4×2  | yes   | 8 cards, no gaps             |
| A4             | 297 × 210 (lands.)     | 4×2  | yes   | 8 cards, no gaps             |
| 9-up / manual  | 215.9 × 279.4 (port.)  | 3×3  | no    | 1 mm gaps, rotary cut guides |

The 3×3 layout deliberately omits registration marks — nine cards do not fit
inside the Silhouette's registration area, so that preset is for hand cutting
only. It adds grey cut lines down the middle of each gap instead, which is
what you want when feeding strips through a rotary trimmer or guillotine.

## Contrast

Cards are rendered with **+10 % contrast** by default. Consumer inkjets tend
to lift blacks, and a straight print of a Magic card looks washed out next to
a real one. 10 % is roughly the correction that makes an Epson EcoTank print
look right; adjust to taste for your own printer and paper.

## PDF output

Export is handled by jsPDF in exact millimetres with no scaling anywhere in
the pipeline. This is important: "fit to page" or "shrink oversized pages" in
a print dialog will silently resize everything and ruin registration. Print
at **100 % / Actual Size**, always.

If a test sheet comes out slightly wrong, check the print dialog before
touching any of the numbers above. Scaling is the culprit far more often than
geometry is.
