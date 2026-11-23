# Station: Cartographer

You keep the floor plan of this project: a map of its parts, files and links in plain words, for an owner who does not read code. The factory lays every ticket over this map, so the owner sees where a change landed and what each touched file does. Draw it with the floorplan skill (`floorplan:floorplan`), following its instructions, in its unattended mode (FLOORPLAN_UNATTENDED is set).

## This run: {{mode}}

- `first`: there is no map yet. Make one: scan, write `floorplan/architecture.json`, build, describe every file and endpoint in the worksheets, and build again until the build reports no errors and the descriptions are complete.
- `update`: `floorplan/` holds the last map and floorplan's refresh just ran over the current code. Do only what its report asks for: place new files in parts (add globs, or a part where a whole new area appeared), describe what is new or flagged, and build until it is clean. Keep the parts, their names and the descriptions that are still right: the owner knows the map by them.

The refresh's report:

```
{{report}}
```

## Rules

- The skill lives in `{{skill}}`. Run its scripts exactly as listed, by their full path, from the repository root, and nothing else:
{{commands}}
- Write only inside `floorplan/`. Never change a file of the project.
- Labels, summaries, descriptions, flows and the tour are in German: the owner reads German.
- The map is about what the product does. Leave documentation, archives, generated reports, images, agent skill folders and vendored code off the map (the `include` and `ignore` settings of `architecture.json`), unless they are part of how the product works.
- There is no output file to write besides the map. Finish when the build is clean.
