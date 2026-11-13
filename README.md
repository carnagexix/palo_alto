# Panorama CLI Object Browser

Interactive command-line browser for Palo Alto **Panorama** device groups and
managed objects (address, address groups, services, service groups,
applications, tags). Built and tested against **PAN-OS 12.1** (works on older
releases via fallbacks).

Only Python 3.8+ and the `requests` library are required.

```
pip install requests
```

## Quick start

```
python panorama_cli.py --host <FQDN or IP of Panorama>
```

First run prompts for username/password, generates an API key, and caches it
in `.panorama_api_key` (chmod 600). Later runs reuse the cached key
automatically.

Interactive flow:

```
Mode            -> [1] explore device-group objects   [2] find where an object is used
                   [3] quit
Mode 1 (explore):
Device groups   -> pick one (number), or "(shared)" for shared objects
Action          -> [1] list objects   [2] export CSV/JSON   [3] import CSV/JSON
                   [4] copy/move object(s) to shared   (only for device groups)
                   [last] back to mode menu            (number varies)
Object types    -> pick one (address, service, ...)
```

Both menus repeat after each action/pass, so you can browse, export, import,
convert and run rule searches as often as you want per session before
quitting. `--output json/csv` is a one-shot export.

Running with **no arguments** prints this help menu.

## Command-line options

| Flag | Description |
|---|---|
| `--host` | Panorama IP/hostname (env: `PANORAMA_HOST`) |
| `--user` / `--password` | admin credentials (env: `PANORAMA_USER` / `PANORAMA_PASSWORD`); password is prompted hidden if omitted |
| `--output {text,json,csv}` | skip the Action menu and export directly; json/csv are saved to `<device_group>_<obj_type>.<ext>` in the current directory (spaces/special chars become underscores) |
| `--renew-key` | delete the cached API key and generate a fresh one |
| `--debug` | print every raw API request/response to stderr |
| `--diagnose` | run an API probe battery (op/config/REST) and dump raw responses - use this first when API access looks wrong |

## Export

- `--output csv` -> e.g. `FW_Device_Group_address.csv`
- `--output json` -> e.g. `FW_Device_Group_address.json`
- From the interactive menu: choose *export to CSV/JSON*, then pick format.

The export files double as import templates (see below). Nested values
(members, tags) appear as JSON blobs inside CSV cells.

## Import

Choose **import from CSV/JSON file** in the Action menu, then enter a file
path. Imports **create or overwrite** objects in the selected device group.

Flow:

1. The script snapshots the current state of every object the file touches.
2. Preview of what will be sent; nothing is sent until you type `yes`.
3. Each object is applied with `action=set`.
4. A **diff** (committed state -> intended post-import state, per object,
   marked `NEW`/`MODIFIED`/`FAILED`; unchanged objects are omitted) is
   printed and saved to `diff_<device_group>_<timestamp>.txt` in the current
   directory.
5. Post-import menu:
   - **[1] commit changes** - optionally enter a commit description, then
     confirm. Note: a Panorama commit pushes **all** pending changes (not just
     this import) to managed devices. Returns a job id to monitor
     (`show jobs id <id>`).
   - **[2] revert this import** - deletes objects the import created and
     restores modified ones to their pre-import state (leaves other admins'
     pending changes untouched).
   - **[3] leave pending** - changes stay in the candidate config; commit from
     the Panorama UI/CLI later.

Note: some Panorama deployments reject XML API config writes with `code 16
unauthorized`. The script automatically falls back to the modern REST API
(`/restapi/v11.0|v12.0/Objects/...`, `X-PAN-KEY` auth) for create/update/
delete in that case; both are tried and combined errors are shown if both fail.
Device-group REST writes are auto-probed across request formats (with/without
the `panorama=LocalDevice` query parameter and different `@`-attribute
styles), because some 12.1 builds answer them with `400 Invalid Object`.
REST validation reasons from the server (`Invalid Object: ...` details) are
shown in the FAILED lines. Imports apply types in dependency order
(tag -> application/service -> address -> groups) so group/tag references
resolve; `tag` strings and lowercase `protocol` values are normalized for
REST, and if the box rejects an optional field's enum value (e.g. a tag
color), the field is dropped from that write with a loud WARNING.
Existing objects are detected via the REST API and overwritten automatically
(POST -> PUT -> delete+create cascade), so re-importing an updated file updates
objects in place.
The post-import commit is tried the same way (classic XML `type=commit`
command first, REST `Commit` endpoint as fallback).

## Copy/move objects to shared

Choose **copy/move object(s) to shared** in the Action menu (only offered when
a real device group is selected), then pick a mode:

- **copy** - creates the shared object and **keeps** the device-group object.
  Use this when rules/local firewalls still reference the local object. The
  device-group object shadows the shared one inside that device group (this
  is valid PAN-OS scoping).
- **move** - creates the shared object, then deletes the device-group object.

Pick object type, then select entries by number (`a` = all) or give a list
file: `@names.txt` - one object name per line (blank lines and `#` comments
ignored, a line may contain several comma-separated names; unmatched names are
reported as `MISSING`). Each object is recreated in **(shared)** with the same
name and all properties. Names that already exist as shared objects are
**skipped, never overwritten**.

Flow mirrors import:

1. A **preview diff** (`<dg>_diff_<mode>-preview_<timestamp>.txt`) shows
   `COPY`/`MOVE`/`SKIP` per object and the location change, before anything
   is sent.
2. After applying, a **verification diff** (`<dg>_diff_<mode>_<timestamp>.txt`)
   reads each shared object back and compares every property against the
   original device-group entry (`all properties identical` / any `-`/`+`
   lines).
3. Post-op menu: **[1] commit changes**, **[2] revert this copy/move** (for
   move: restores objects in the device group and deletes the shared copies;
   for copy: deletes only the shared copies - the source objects were never
   touched; writes its own revert diff), **[3] leave pending**.
4. **More** menu: **[1] convert more objects** returns to the **Object types**
   selection (so you can convert a different type next), then the object list
   is re-read so freshly moved/copied entries reflect current state;
   **[2] finish** returns to the Action menu.

Note: source objects are read from the committed (running) config, so
uncommitted changes made in the Panorama UI are not included in a move.

## Where is an object used?

Choose **find where an object is used (rule search)** in the Mode menu. Type
an object name, IP address or (address/service) group name; the tool searches
the **committed** running config across **all device groups and shared**:

- matches objects by name (substring, case-insensitive) or by IP value
  (`ip-netmask` / `ip-range` / `ip-wildcard` contains the query);
- resolves address groups and address groups in rules (transitively) so a
  rule using `grp-servers` counts as using a member address object;
- scans every security, NAT and PBR rulebase (pre-/post-rulebase included)
  in each device group;
- always lists every matched **object name** with its type and location
  (plus IP/member summary - handy when searching by IP address);
- output per rule: location, rulebase, rule name, which fields referenced
  the object (directly or via a group), and the rule's from/to/source/dest/
  service/application/action.

After each search, enter another query to keep searching (`q` returns to the
Mode menu). At the `Export results` prompt: `c` writes
`where-used_<query>_<ts>_objects.csv` (matched objects) plus
`where-used_<query>_<ts>_rules.csv` (rule references); `j` writes a single
`where-used_<query>_<ts>.json` containing both. Enter skips export (results
are always printed to the screen).

When rules were found, an extra prompt offers **adding address /
address-group objects to those rules**: enter names comma-separated, or
`@file.txt` with one name per line. Names that do not exist (or are not
address/address-group objects) are **warned about and skipped**. Pick which
field to extend (`source`, `destination` or `both`); rules whose field has
no member list (e.g. `any`) are skipped with a note. Members already present
are not duplicated. A preview of `+member` per rule is shown before anything
is sent, then each rule field is updated via the XML API with the REST
`/Rules` endpoint as fallback; afterwards you can commit or leave the rules
pending.

Note: object resolution is by name across all device groups and shared;
cross-device-group scoping nuances (e.g. a group with the same name in two
device groups) are not modelled.

One file may mix object types if it declares them:

- **CSV**: include a `type` column (`address`, `address-group`, `service`,
  `service-group`, `application`, `tag`). Entries without a `type` column use
  the object type you select from the menu.
- **JSON**: either a list of objects (one type, chosen from the menu), or an
  object keyed by type: `{"address": [...], "service": [...]}`.

Sample files: **`sample_import.csv`** and **`sample_import.json`** - both
contain one or more examples of every supported object type.

Field reference (unusable/empty columns are ignored; nested lists like tags
and members must be JSON, e.g. `{"member": ["office", "prod"]}`):

| type | key fields |
|---|---|
| `address` | `ip-netmask` *or* `ip-range` *or* `ip-wildcard` *or* `country`, `description`, `tag` |
| `address-group` | `static` (JSON member list), `description` |
| `service` | `protocol` (TCP/UDP/SCTP), `port`, `source-port`, `description`, `tag` |
| `service-group` | `protocol` (JSON member list, lowercase), `port` |
| `application` | `protocol` (JSON member list), `port`, `category`, `subcategory`, `description` |
| `tag` | `color`, `comments` |

## Environment variables

`PANORAMA_HOST`, `PANORAMA_USER`, `PANORAMA_PASSWORD`, `PANORAMA_API_KEY`
(env key wins over the cached file).

## PAN-OS 12.x API quirks handled by this script

- `type=op` rejects plain CLI strings on 12.x; commands must be XML, e.g.
  `<show><system><info/></system></show>`.
- `type=config action=get` returns an empty candidate (`code 7`); the script
  reads the committed config with `action=show` instead and parses that.
- Device groups live under `/devices/device-group` as a plain entry list (no
  `DevGroup` wrapper); the script locates them by scanning the config tree.
- Writes use `action=set` POSTed to both the 12.x and legacy `/dev/group`
  paths; if your Panorama rejects both, run with `--debug` to inspect the
  exact error.

## Troubleshooting

- **`malformed op command` everywhere / empty config** - run
  `--diagnose` and compare the probe outputs (a real Panorama answers at
  least one probe).
- **`No device groups found`** - wrong host (check `model: Panorama` in
  diagnostics), stale key (`--renew-key`), or an admin role without Panorama
  read access.
- **API key invalid/expired** (code 4) - `--renew-key`.
