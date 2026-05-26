---
name: sliderule-region-picker
description: >
  Present the user with a link to the SlideRule helper map so they can
  interactively draw a polygon or bounding box to define a geographic
  region for analysis. Use this skill whenever the user says they want to
  "draw" a region, "pick a region on a map", "select an area on a map",
  "draw a box/polygon", or otherwise indicates they want an interactive
  map to define their analysis area rather than specifying coordinates
  directly. Also trigger when the user says "show me the map", "open the
  region picker", "let me draw my region", "I want to draw the request",
  "let me select a region", or when the user needs to provide a region
  and hasn't specified coordinates. This skill can also be triggered when
  the agent asks the user if they'd like to draw their region and they
  say yes.
---

# SlideRule Region Picker — Helper Map Workflow

## Requirements

No dependencies. Requires only the ability to present a URL to the user.

See `CHANGELOG.md` for version history.

## Overview

The SlideRule web client hosts a helper map page built with OpenLayers
and real map tiles. The agent directs the user to open this page in a
new browser tab, draw their region, and paste the result back into the
conversation.

Do NOT attempt to render tile-based maps (Leaflet, OpenLayers, etc.)
inline — external tile servers are typically blocked by sandboxed
execution environments. Use the hosted helper map instead.

## Helper Map URL

```
https://client.slideruleearth.io/helpermap
```

## The Flow

### Step 1: Direct the user to the helper map

When the user wants to draw a region, respond with something like:

> To select your region interactively:
>
> 1. **Open the helper map:** [https://client.slideruleearth.io/helpermap](https://client.slideruleearth.io/helpermap)
> 2. **Draw your region** using the rectangle or polygon tool
> 3. **Click "Copy to Clipboard"**
> 4. **Come back here and paste** the result into this conversation
>
> I'll then run the analysis on your selected region.

Keep it concise. Don't over-explain — the helper map UI is
self-explanatory.

### Step 2: Parse the pasted region

The user will paste JSON in one of two formats depending on whether
the **rasterize** checkbox was enabled on the helper map.

#### Without rasterize (default)

When rasterize is off, the helper map copies the simplified convex hull
of the drawn shape:

```json
{
  "poly": [
    {"lon": -123.52, "lat": 47.75},
    {"lon": -123.45, "lat": 47.74},
    {"lon": -123.38, "lat": 47.77},
    {"lon": -123.32, "lat": 47.83},
    {"lon": -123.52, "lat": 47.75}
  ]
}
```

Extract the `poly` array and use it directly as the `"poly"` parameter
in a SlideRule API request.

#### With rasterize enabled

When rasterize is on, the helper map copies the exact drawn polygon as
`poly` along with a `region_mask` containing the polygon as rasterized
GeoJSON with a cell size. This tells the SlideRule server to rasterize
the complex polygon into a grid mask for more precise spatial filtering
instead of using the simplified convex hull boundary.

```json
{
  "poly": [ ... ],
  "raster": {
    "data": "<GeoJSON>",
    "length": <n>,
    "cellsize": <degrees>
  }
}
```

When this format is present, include both `"poly"` and `"raster"` in
the `parms` of the SlideRule API request. Pass the `raster` object as
the `"region_mask"` parameter:

```python
parms = {
    "poly": parsed["poly"],
    "region_mask": parsed["raster"],
    ...
}
```

### Step 3: Proceed with the analysis

Once you have the polygon, continue with the `sliderule-api` skill to
make the actual processing request. The user may have already specified
what kind of analysis they want (canopy, water level, terrain, etc.)
before or after drawing the region.

## When to Offer the Helper Map

- **User explicitly wants to draw:** They say "let me draw", "I want to
  select on a map", "show me the picker", etc. → Provide the link.

- **User needs a region but hasn't specified one:** If the user asks for
  an analysis (e.g., "run a canopy study of Mount Rainier") and provides
  a place name but no coordinates, the agent should use its knowledge to
  construct a reasonable polygon. Only offer the helper map if the user
  wants to be more precise or if the location is ambiguous.

- **Agent asks and user says yes:** If the agent asks "Would you like to
  specify coordinates or draw a region on a map?" and the user picks the
  map option, provide the link.

## Important Notes

- The helper map is an OpenLayers-based page hosted on the SlideRule web
  client domain with full tile access — it is NOT rendered inline.
- Do NOT attempt to render tile-based maps inside sandboxed environments
  — external tile servers are typically blocked by content security policies.
- The rasterize checkbox on the helper map controls which polygon
  representation is copied: convex hull only (off) vs. exact polygon
  with rasterized grid mask (on). For complex or concave polygons where
  precise boundaries matter, suggest the user enable rasterize.
