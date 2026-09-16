# Lab room maps

Each file in this folder is one **room layout** (a top-down seat map) that the
launcher draws when you pick a lab. A lab in `lab_info.json` points at a map by
name:

```json
"labs": {
  "small": { "name": "Small lab", "host": "…", "seats": ["1","2","…"], "map": "example_small" }
}
```

`"map": "example_small"` resolves to `maps/example_small.json`. Several labs can
share one map file, and you can add your own room by dropping a new
`maps/<name>.json` here and referencing it. (A lab may also inline a full map
object instead of naming a file, but a file is the normal, reusable way.)

There is **no in-app editor for maps** — you author them by hand-editing JSON
(an LLM is good at this if you paste it this README plus the room shape). The two
example files are complete, working templates: copy one and change it.

## The map is geometry only — seat *names* come from the lab

A map does **not** contain seat names. Every `"kind": "seat"` cell is filled, in
the order the seat cells appear in `cells`, from that lab's own `seats` list in
`lab_info.json`. So the map defines *where* seats sit; the lab defines *what they
are called*. That is why one map file can be reused by different labs, and why
the order of the seat cells matters: the 1st seat cell gets the lab's 1st seat,
and so on. Make sure a map has exactly as many seat cells as the lab has seats.

## Schema

Top level:

| field        | type   | meaning |
|--------------|--------|---------|
| `description`| string | Free text; ignored by the app. Describe the room. |
| `front`      | string | Which edge is the front of the room. Convention: `"bottom"` — grid **row 1 is the BACK**, the highest row number is the FRONT (drawn at the bottom). Informational. |
| `rows`       | int    | Number of grid rows. |
| `cols`       | int    | Number of grid columns. |
| `aisle_cols` | array  | Column numbers left empty as an aisle (no cells placed there). Informational — an aisle is simply a column you put no cells in. |
| `cells`      | array  | The cells of the room (see below). |

Each entry in `cells`:

| field     | type   | meaning |
|-----------|--------|---------|
| `r`       | int    | Grid row, 1-based (row 1 = back of the room). |
| `c`       | int    | Grid column, 1-based (column 1 = left). |
| `kind`    | string | `"seat"`, `"exp"` (experimenter desk), or `"wall"` (a blocked, non-interactive cell). |
| `colspan` | number | *(optional)* How many columns the cell spans (e.g. a wide wall). Default 1. |
| `rowspan` | number | *(optional)* How many rows the cell spans. Default 1. |

Only `"seat"` cells consume a name from the lab's `seats` list; `"exp"` and
`"wall"` cells are drawn but take no seat.

### Annotated snippet

```jsonc
{
  "description": "My room: 3 wide, front at the bottom.",
  "front": "bottom",
  "rows": 2,
  "cols": 3,
  "aisle_cols": [],
  "cells": [
    { "r": 2, "c": 1, "kind": "exp" },    // front-left: experimenter desk (no seat)
    { "r": 2, "c": 2, "kind": "seat" },   // front row  -> gets the lab's 1st seat
    { "r": 2, "c": 3, "kind": "seat" },   //            -> the lab's 2nd seat
    { "r": 1, "c": 1, "kind": "seat" },   // back row   -> the lab's 3rd seat
    { "r": 1, "c": 2, "kind": "wall" },   // a wall in the back row (no seat)
    { "r": 1, "c": 3, "kind": "seat" }    //            -> the lab's 4th seat
  ]
}
```

This map has **4 seat cells**, so a lab that uses it should list **4 seats**;
they fill in the order the seat cells appear above.

## The two examples

- **`example_small.json`** — a 5×5 room with the experimenter desk front-left and
  a wall down the right of two middle rows. No aisle. 22 seats. A good starting
  point for a plain rectangular room with a couple of blocked cells.
- **`example_large.json`** — a 9-column room with a **central aisle** (column 5),
  the experimenter desk beside the aisle, short end rows at the back, and a wall
  spanning the back of the aisle. 31 seats. Shows aisles, spanning walls, and
  partially-filled rows.
