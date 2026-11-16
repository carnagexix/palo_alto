# Panorama CLI Object Browser

Interactive command-line browser for Palo Alto **Panorama** device groups and
managed objects (address, address groups, services, service groups,
applications, tags). Built and tested against **PAN-OS 12.1** (works on older
releases via fallbacks).

Only Python 3.8+ and the `requests` library are required.

> **Use at your own risk.** This tool reads and writes live Panorama
> configuration (objects, address groups, and security rules) through the
> Panorama API. Always review the before/after diffs, prefer leaving changes
> pending for inspection, and test on a noncritical device group or rule
> first. The authors accept no liability for damage or downtime.

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
                   [3] convert object files (CSV <-> JSON)   [4] config snapshots   [5] quit
Mode 1 (explore):
Device groups   -> pick one (number), or "(shared)" for shared objects
Action          -> [1] list objects   [2] export CSV/JSON   [3] import CSV/JSON
                   [4] copy/move object(s) to shared   (only for device groups)
                   [last] back to mode menu            (number varies)
Object types    -> pick one (address, service, ...)
Mode 3 (convert):
Conversion      -> [1] convert CSV to JSON   [2] convert JSON to CSV
                   then: input file, output file (Enter = same name, other
                   extension), overwrite confirmation. Files use the same
                   format as exports/imports: JSON is `{ "<type>": [...] }`
                   (or a plain list when no type column exists), CSV has a
                   leading `type` column; nested values (tags, members) are
                   embedded as JSON. Round-trip CSV -> JSON -> CSV is lossless.
Mode 4 (snapshots):
Snapshots       -> [1] save configuration snapshot on Panorama
                   [2] export configuration locally (XML + JSON)
                   [3] list snapshots   [4] back to main menu
                   Saved snapshots get the entered base name plus a
                   `YYYYMMDD_HHMMSS` date stamp. The local export downloads
                   the committed configuration through the API and writes
                   `snapshot_<name>_<date>.xml` and `.json` locally.
```

Both menus repeat after each action/pass, so you can browse, export, import,
convert and run rule searches as often as you want per session before
quitting. `--output json/csv` is a one-shot export.

Running with **no arguments** prints this help menu.

## Panorama access requirements

The script talks to Panorama **only** over HTTPS (TCP 443) on the management
interface, using the standard APIs below. Make sure all of these are
reachable/permitted before reporting "unauthorized" issues:

- **Network / service**: HTTPS 443 allowed from the client to Panorama
  (management interface ACL on Panorama + intervening firewalls), TLS 1.2+.
  The script does **not** verify TLS certificates (`verify=False`); pin a
  CA yourself or accept that risk.
- **Admin account and role**: an admin that can log in to the Panorama web
  UI. Read-only browsing/searching works with read access; **imports, rule
  and group edits, commits and snapshots require a write-capable role**
  (superuser, or Panorama Administrator / Device Management with config and
  commit rights). If `type=config action=set|edit` comes back
  `unauth code 16`, the API key - not the account - lacks write rights:
  regenerate it from a full-rights session (`request api-key`) or the UI.
- **API key**: generated on first run via `type=op <request-api-key>`; it
  inherits the role of the generating session. API keys and admin users can
  additionally be restricted by an **API authentication profile** (MFA /
  authentication factor) - if your org enforces one for API access, script
  writes will be rejected and must be exempted or performed in the UI.
- **XML API (`/api/`)** must be enabled (it is by default): used for config
  `show`/`get`/`set`/`edit`, and `op` commands (`commit`, snapshot save,
  system info).
- **Modern REST API (`/restapi/`)** must be enabled (default). The script
  auto-probes the version and needs these resources depending on what you
  do:
  - objects/groups: `Objects/Addresses`, `Objects/AddressGroups`,
    `Objects/Services`, `Objects/ServiceGroups`, `Objects/Applications`,
    `Objects/Tags`
  - security rules: `Policies/SecurityPreRules` / `Policies/SecurityPostRules`
    (rule updates are REST `PUT`; XML `action=edit` is the fallback)
  - commits: `Commit`
  - config snapshots: `Commands/Snapshot/Config` (XML `op
    <snapshot><config>` fallback)
  If a route answers `501 Route Not Found`, that Panorama build does not
  serve that resource - the script falls back to the XML API where possible.

## Command-line options

| Flag | Description |
|---|---|
| `--host` | Panorama IP/hostname (env: `PANORAMA_HOST`) |
| `--user` / `--password` | admin credentials (env: `PANORAMA_USER` / `PANORAMA_PASSWORD`); password is prompted hidden if omitted |
| `--output {text,json,csv}` | skip the Action menu and export directly; json/csv are saved to `<device_group>_<obj_type>.<ext>` in the current directory (spaces/special chars become underscores) |
| `--renew-key` | delete the cached API key and generate a fresh one |
| `--debug` | print diagnostic API details to stderr (large configuration bodies are suppressed) |
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
- lists every group (address-group, service-group, application-group) that
  **uses the object** (directly or via a nested group), with the members it
  contains;
- scans every security, NAT and PBR rulebase (pre-/post-rulebase included)
  in each device group;
- always lists every matched **object name** with its type and location
  (plus IP/member summary - handy when searching by IP address);
- output per rule: location, rulebase, rule name, which fields referenced
  the object (directly or via a group), and the rule's from/to/source/dest/
  service/application/action.

After each search, enter another query to keep searching (`q` returns to the
Mode menu). At the `Export results` prompt: `c` writes
`where-used_<query>_<ts>_objects.csv` (matched objects),
`where-used_<query>_<ts>_rules.csv` (rule references) and
`where-used_<query>_<ts>_groups.csv` (groups that use the object); `j` writes
a single `where-used_<query>_<ts>.json` containing all three. Enter skips
export (results are always printed to the screen).

After export, an **action menu** appears when there is anything to edit:

```
Next action:
  [1] add/remove objects in the matched address group(s)   (only when the
      searched object is an address group)
  [2] add address/address-group objects to rules DIRECTLY containing
      the matched object(s) (group-mediated rule hits are excluded)
Select number (Enter=skip):
```

Option **[2] is always offered**. Option **[1] appears only when at least one
matched object is itself an address group** (for plain addresses only [2] is
offered).

Option 1 opens a submenu: **[1] add objects** to the matched address group(s)
or **[2] remove objects** from them. Names may be entered comma-separated, as
a single name, or imported via `@file.txt` (one name per line); unknown or
non-address objects are warned about and skipped, and remove requests for
names that are not members are skipped. A before/after preview is shown per
group; after applying, a `where-used_<query>_group-edit_<ts>.txt` diff
records `- before` / `+ after` / `+ added` (or `- removed`) per group.
**Group writes work through the REST objects API**, so these edits actually
take effect. The post-update menu offers **commit (optional description)**,
**revert these group changes** (`..._group-revert_<ts>.txt` diff, restoring
the exact original member list) or **leave pending**.

Option 2 offers **adding address / address-group objects only to rules that
DIRECTLY contain the searched address/address-group object**, rather than
rules that refer to it only indirectly through an address group. Group-based
(indirect) rule matches are still listed in the where-used search output, but
they are excluded from the edit plan. If one rule field matches directly and
another only indirectly, only the directly matching field is edited.

Enter names comma-separated, or `@file.txt` with one name per line. Names
that do not exist (or are not address/address-group objects) are warned about
and skipped. Members are appended to the **same directly matched field**
(`source` or `destination`); fields containing `any` are skipped and
existing members are not duplicated. Optionally enter **text to append to
the rules' descriptions** (Enter=skip): the current description of every
rule in the plan is shown (`desc  : ...`) together with the resulting text
(`desc+ : ...`); the append is idempotent (already-present text is not
duplicated). Review the per-field before/after preview, then type `yes` to
apply. The diff (`where-used_<query>_rule-edit_<ts>.txt`) records
`UPDATED`/`FAILED`, before/after, added members, and `- desc` / `+ desc`
changes. The post-update menu provides **commit**, **revert these rule
changes** (restores members **and removes the appended description text**),
or **leave pending**. If the API write fails, the CLI fallback script
includes the `set ... description "<new text>"` line as well.

**How rule writes work on Panorama (verified on 12.1.10-h3).**
The `/restapi/v11.0/Rules/...`, `/Rules/Security` and similar routes
all return `501 Route Not Found` - those are wrong. The actual Panorama REST
policy resources are **`/restapi/v11.0/Policies/SecurityPreRules` and
`.../Policies/SecurityPostRules`** (`NatPreRules`/`NatPostRules`, etc.),
addressed with `location` + `device-group` + `name` params and updated with
`PUT` (never `POST` - POST would create policies). The script first GETs the
live candidate rule, merges only the target field's members, and PUTs the
whole current entry, so concurrent edits are never clobbered and a stale
read aborts the revert. The XML-API fallback uses the real Panorama config
tree device name **`localhost.localdomain`** (not `LocalDevice`):
`/config/devices/entry[@name='localhost.localdomain']/device-group/entry[...]/post-rulebase/security/rules/entry[...]/source`,
reading via `action=get` then replacing the field with `action=edit`.
If both fail, an **append-only CLI fallback**
(`where-used_<query>_rule-edit-cli_<ts>.txt`) is written with
`set ... <field> member "<name>"` lines (no `clear` commands, so existing
members are preserved); paste into `configure`, review the candidate diff,
then commit. **Test on a noncritical rule first.**

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
- Device groups live under `/devices/device-group` as a plain entry list (and
  may be nested as groups-of-groups); the script locates them by scanning the
  config tree recursively.
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

## Authors & Credits

- **qwen3.8-flash-next-unsloth-ud-iq2_xs** - project owner; ran, tested, and tuned everything against
  production Panorama.
- **opencode** - AI coding assistant; built the object browser, where-used
  search, group/rule editing flows, exports, and diagnostics, and maintains
  this repo.
- **ChatGPT** (OpenAI) - AI assistant; identified the correct Panorama REST
  policy endpoints (`/Policies/{Type}{Pre,Post}Rules`) and the
  `localhost.localdomain` XML config path, wrote the offline
  `test_panorama_rule_fix.py` suite, and contributed the direct-only
  rule-match safety model.
