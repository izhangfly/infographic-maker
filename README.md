# IZhang Aviation — Aircraft Infographic Maker (V1)

Upload an aircraft side-profile PNG + paste planespotters.net text → DeepSeek structures & translates
the data → two downloadable, print-ready infographics built on canvas (not AI-generated imagery):

4 styles at once — Classic, Dithered, Retro, Editorial — each in two formats:

- **2.35:1 poster** (3840×1634) — minimal: aircraft, registration, title, logo
- **9:16 data sheet** (3240×5760) — full bilingual sheet: facts grid, complete condensed
 operator history timeline (auto-collapses lessor ping-pong eras), remarks.
 A "Limit data sheet to 9:16" toggle can be turned off so the sheet height follows content
 (comfortable rows, no compression, no empty space) for very long or very short histories.

The aircraft image accepts drag&drop, file browse, or clipboard paste (button or Cmd+V).
Palette swatches (primary/secondary) are click-editable after upload — native colour picker
(saturation/brightness square + RGB/hex) or the ⌖ screen eyedropper.

Colours are auto-extracted from the livery; background patterns shift slightly on every build.
A "Virtual livery" toggle produces posters only, with a THIS IS A VIRTUAL LIVERY badge.

**Rockets** (button above section 1): a parallel pipeline for launch vehicles — upload a
rocket render on a pure #FF00FF background (chroma-keyed with a border-sampled key colour,
so slightly-off AI magentas work too), paste Wikipedia-style specs, synthesize via
/api/synthesize_rocket, and build 4 styles × (diagonal 2.35:1 poster + content-height data
sheet with the rocket filling the right half and a ghost name behind it). Colours come from
a curated random palette (shuffle button) since rockets are mostly white. Styles: Classic,
Dithered, Retro-Tech (old sci-fi: starfield/orbits/reticles), Editorial.

## Run locally
 pip3 install flask requests
 python3 app.py # http://localhost:8640

## Server
Deployed at http://YOUR_SERVER_IP:8640 — folder /root/infographic-maker,
systemd unit `infographic-maker.service` (systemctl restart infographic-maker).

## Files
- app.py Flask: serves static/ + /api/synthesize (DeepSeek proxy)
- deepseek.key API key (chmod 600, not for sharing)
- static/index.html the whole frontend (analysis + renderers)
- static/logo.png I'Zhang Aviation logo (white on transparent)
- static/demo/ demo assets; open /?demo=1 to test the pipeline offline

## Notes
- Test with: /?demo=1 or /?demo=1&img=/static/demo/sf757.png&data=/static/demo/apboi.json
- The synthesized JSON in step 3 is editable before rendering — fix anything DeepSeek got wrong.
