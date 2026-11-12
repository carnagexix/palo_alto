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
Device groups   -> pick one (number), or "(shared)" for shared objects
Action          -> [1] list objects   [2] export CSV/JSON   [3] import CSV/JSON
                   [4] copy/move object(s) to shared   (only for device groups)
Object types    -> pick one (address, service, ...)
```

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

Pick object type, then select entries by number (`a` = all). Each object is
recreated in **(shared)** with the same name and all properties. Names that
already exist as shared objects are **skipped, never overwritten**.

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

Note: source objects are read from the committed (running) config, so
uncommitted changes made in the Panorama UI are not included in a move.

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
