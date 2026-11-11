#!/usr/bin/env python3
"""
Palo Alto Panorama interactive CLI (tested on PAN-OS 12.1).

Flow:
  1. Grab API key from Panorama server (login with username/password, or reuse
     a previously saved key from .panorama_api_key / PANORAMA_API_KEY).
  2. List all device-group names; select one interactively.
  3. List object types (address, address-group, ...); select one and list all
     objects of that type in the selected device group with their fields.

PAN-OS 12.x quirks discovered on 12.1.10-h3:
  * `type=op` with plain CLI-style `cmd` ("show system info") is rejected with
    "Invalid request: malformed op command"; the XML form
    (<show><system><info/></system></show>) must be used instead.
  * `type=config action=get` returns an empty candidate store
    (status="success" code="7" <result/>); `type=config action=show` returns
    the full committed running config, so we fetch it once and parse the tree.

Only stdlib + `requests` are used.

Usage:
  python panorama_cli.py --host 10.1.2.3 [--user admin --password 'xxx']

Credentials can also come from environment variables:
  PANORAMA_HOST, PANORAMA_USER, PANORAMA_PASSWORD, PANORAMA_API_KEY
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests
import urllib3

urllib3.disable_warnings()  # Panorama firewalls commonly use self-signed certs.

API_VERSION = "1"
OBJECT_TYPES = [
    "address",
    "address-group",
    "service",
    "service-group",
    "application",
    "tag",
]
DG_GROUPS = ("DevGroup", "DynGroup")  # static and dynamic device groups
SHARED_LABEL = "(shared)"  # pseudo group for shared objects
# Modern REST API (/restapi/<ver>/...); 12.1 was verified to serve v11.0.
REST_VERSIONS = ("v11.0", "v12.0", "v10.0")
REST_RESOURCES = {
    "address": "Objects/Addresses",
    "address-group": "Objects/AddressGroups",
    "service": "Objects/Services",
    "service-group": "Objects/ServiceGroups",
    "application": "Objects/Applications",
    "tag": "Objects/Tags",
}


# --------------------------------------------------------------------------- #
# Config / helpers
# --------------------------------------------------------------------------- #
def env_or_default(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val else default


def key_file_path() -> Path:
    return Path(__file__).resolve().parent / ".panorama_api_key"


def save_api_key(host: str, key: str) -> None:
    path = key_file_path()
    try:
        path.write_text(json.dumps({"host": host, "key": key}))
        path.chmod(0o600)
    except OSError as exc:
        print(f"warning: could not save API key to {path}: {exc}", file=sys.stderr)


def load_api_key(host: str) -> str | None:
    # Environment variable wins, then cached file if it matches the host.
    env_key = env_or_default("PANORAMA_API_KEY")
    if env_key:
        return env_key
    path = key_file_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get("host") == host:
                return data.get("key")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _merge(container: dict, key: str, value) -> None:
    if key in container:
        cur = container[key]
        if not isinstance(cur, list):
            container[key] = [cur]
        container[key].append(value)
    else:
        container[key] = value


def xml_to_obj(elem: ET.Element):
    """Convert a Panorama XML fragment into plain dicts/lists (entry -> dict)."""
    if elem.tag == "entry":
        obj: dict = {"name": elem.get("name")}
        for child in list(elem):
            _merge(obj, child.tag, xml_to_obj(child))
        return obj
    children = list(elem)
    if not children:
        return (elem.text or "").strip() or None
    if all(c.tag == "entry" for c in children):
        return [xml_to_obj(c) for c in children]
    out: dict = {}
    for child in children:
        _merge(out, child.tag, xml_to_obj(child))
    return out


def as_entry_list(result) -> list[dict]:
    """Normalize a node into a list of entry dicts at that level only."""
    if isinstance(result, list):
        return [e for e in result if isinstance(e, dict) and e.get("name")]
    if isinstance(result, dict):
        entries = result.get("entry")
        if entries is not None:
            return as_entry_list(entries)
    return []


def dig(obj, *keys):
    """Walk nested dicts/lists; bare names match entry@name, e.g.

    dig(config, "device-group", "DevGroup", "MY-DG", "address").
    """
    for key in keys:
        if isinstance(obj, dict):
            if key in obj:
                obj = obj[key]
            elif isinstance(obj.get("entry"), list):
                matches = [e for e in obj["entry"]
                           if isinstance(e, dict) and e.get("name") == key]
                obj = matches[0] if matches else None
            else:
                return None
        elif isinstance(obj, list):
            matches = [e for e in obj
                       if isinstance(e, dict) and e.get("name") == key]
            obj = matches[0] if matches else None
        else:
            return None
    return obj


# --------------------------------------------------------------------------- #
# Panorama client (Config API)
# --------------------------------------------------------------------------- #
class PanoramaClient:
    def __init__(self, host: str, api_key: str, timeout: int = 60,
                 debug: bool = False):
        # Accept bare host or a full https:// URL.
        host = host.strip()
        if host.startswith("https://"):
            host = host[len("https://"):]
        elif host.startswith("http://"):
            host = host[len("http://"):]
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.debug = debug
        self.session = requests.Session()
        self._running_config: dict | None = None

    def _handle_response(self, resp, url: str, query: dict):
        if self.debug:
            safe = {k: ("<redacted>" if k == "key" else v)
                    for k, v in query.items()}
            print(f"REQUEST {url}", file=sys.stderr)
            print(f"params: {safe}", file=sys.stderr)
            body = resp.text
            if len(body) > 2000:
                body = body[:2000] + f"\n[...truncated, {len(resp.text)} bytes]"
            print(body, file=sys.stderr)
        resp.raise_for_status()
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            raise RuntimeError(
                f"Panorama returned non-XML response: {resp.text[:200]!r}")
        if root.get("status") != "success":
            code = root.get("code") or root.findtext("code") or "?"
            msg = root.findtext("msg") or resp.text[:200]
            hint = ""
            if code == "4":
                hint = (" (API key may be invalid/expired - delete "
                        ".panorama_api_key and re-run with credentials)")
            raise RuntimeError(f"Panorama API error (code {code}): {msg}{hint}")
        result = root.find("result")
        return xml_to_obj(result) if result is not None else None

    def _request(self, params: dict):
        query = {"key": self.api_key, "api-version": API_VERSION}
        query.update(params)
        if "path" in query:
            # PAN-OS config APIs historically read 'xpath'; send both names.
            query["xpath"] = query["path"]
        url = f"https://{self.host}/api/{API_VERSION}"
        resp = self.session.get(url, params=query, timeout=self.timeout,
                                verify=False)
        return self._handle_response(resp, url, query)

    def config_write(self, action: str, path: str, element: str | None = None):
        """Send type=config action=set|delete.

        Config writes read the path from 'xpath' (not 'path'); send both.
        Try POST form body first (payload can be large), fall back to GET
        query form if the platform answers 'code 16 unauthorized'.
        """
        data = {"key": self.api_key, "api-version": API_VERSION,
                "type": "config", "action": action, "path": path,
                "xpath": path}
        if element is not None:
            data["element"] = element
        url = f"https://{self.host}/api/{API_VERSION}"
        resp = self.session.post(url, data=data, timeout=self.timeout,
                                 verify=False)
        try:
            return self._handle_response(resp, url, data)
        except RuntimeError as exc:
            if "code 16" not in str(exc):
                raise
            if element is not None and len(element) > 4000:
                raise
            return self._handle_response(
                self.session.get(url, params=data, timeout=self.timeout,
                                 verify=False),
                url, data)

    def config_set(self, path: str, element: str):
        return self.config_write("set", path, element)

    def config_delete(self, path: str):
        return self.config_write("delete", path)

    # ---------------- Modern REST API (panui_restapi) ------------------- #
    def _rest(self, method: str, path: str, params: dict | None = None,
              body: dict | None = None) -> dict:
        query = dict(params or {})
        url = f"https://{self.host}{path}"
        headers = {"X-PAN-KEY": self.api_key,
                   "Content-Type": "application/json"}
        payload = json.dumps(body) if body is not None else None
        resp = self.session.request(method, url, params=query, data=payload,
                                    timeout=self.timeout, verify=False,
                                    headers=headers)
        if self.debug:
            print(f"REST {method} {url}", file=sys.stderr)
            print(f"params: {query}", file=sys.stderr)
            print(f"HTTP {resp.status_code}\n{resp.text[:2000]}",
                  file=sys.stderr)
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"REST {method} {path} (HTTP {resp.status_code}): "
                f"{resp.text[:200]}")
        ok = resp.status_code < 400 and \
            str(data.get("@status", "success")).lower() == "success"
        if not ok:
            msg = data.get("message") or data.get("msg") or resp.text[:200]
            raise RuntimeError(
                f"REST {method} {path} (HTTP {resp.status_code} "
                f"code {data.get('code')}): {msg}")
        return data

    def rest_base(self) -> str:
        """Discover the supported /restapi version (cached)."""
        if getattr(self, "_rest_base", None):
            return self._rest_base
        for ver in REST_VERSIONS:
            try:
                self._rest("GET", f"/restapi/{ver}/Objects/Addresses",
                           {"location": "shared", "limit": 1})
            except RuntimeError as exc:
                if "Version Not Supported" in str(exc):
                    continue
                raise
            self._rest_base = f"/restapi/{ver}"
            return self._rest_base
        raise RuntimeError(
            f"no supported REST API version among {REST_VERSIONS}")

    @staticmethod
    def _rest_location(device_group: str) -> dict:
        if device_group == SHARED_LABEL:
            return {"location": "shared"}
        return {"location": "device-group", "device-group": device_group}

    @staticmethod
    def _rest_body(name: str, device_group: str, obj: dict) -> dict:
        entry = {"@location": ("shared" if device_group == SHARED_LABEL
                               else "device-group"),
                 "@name": name}
        if device_group != SHARED_LABEL:
            entry["@device-group"] = device_group
        for key, value in obj.items():
            if key == "name":
                continue
            if key.endswith("member") or key in ("member",):
                value = value if isinstance(value, list) else [value]
            elif isinstance(value, dict) and "member" in value:
                member = value["member"]
                value = {**value,
                         "member": member if isinstance(member, list)
                         else [member]}
            entry[key] = value
        return {"entry": [entry]}

    def rest_write(self, obj_type: str, device_group: str, name: str,
                   obj: dict, create: bool) -> dict:
        resource = REST_RESOURCES[obj_type]
        method = "POST" if create else "PUT"
        params = {**self._rest_location(device_group), "name": name}
        return self._rest(method, f"{self.rest_base()}/{resource}",
                          params, self._rest_body(name, device_group, obj))

    def rest_delete(self, obj_type: str, device_group: str,
                    name: str) -> dict:
        resource = REST_RESOURCES[obj_type]
        params = {**self._rest_location(device_group), "name": name}
        return self._rest("DELETE", f"{self.rest_base()}/{resource}", params)

    def rest_commit(self, description: str) -> dict:
        base = self.rest_base()
        body = {"type": "commit"}
        if description:
            body["description"] = description
        try:
            return self._rest("POST", f"{base}/Commit", body=body)
        except RuntimeError as exc:
            first = exc
        text = str(first).lower()
        if any(w in text for w in ("panorama", "missing", "required",
                                   "parameter")):
            try:
                return self._rest("POST", f"{base}/Commit",
                                  body={**body, "panorama": "LocalDevice"})
            except RuntimeError as exc:
                raise RuntimeError(f"{first}; retry with panorama param: "
                                   f"{exc}")
        raise first

    def running_config(self) -> dict:
        """Fetch the full committed running config once and cache it.

        PAN-OS 12.x returns an empty candidate for action=get, so use
        action=show instead.
        """
        if self._running_config is None:
            result = self._request({"type": "config", "action": "show"})
            config = None
            if isinstance(result, dict):
                config = result.get("config", result)
            if not isinstance(config, dict):
                raise RuntimeError("unexpected running-config response shape")
            self._running_config = config
        return self._running_config

    def raw_get(self, params: dict, path: str = "") -> str:
        """Unparsed response body for diagnostics."""
        query = ({} if path.startswith("/restapi")
                 else {"key": self.api_key, "api-version": API_VERSION})
        query.update(params)
        url = f"https://{self.host}{path or '/api/' + API_VERSION}"
        resp = self.session.get(
            url, params=query, timeout=self.timeout, verify=False,
            headers={"X-PAN-KEY": self.api_key})
        return f"HTTP {resp.status_code}\n{resp.text[:3000]}"

    def rest_v13(self, resource: str, params: dict | None = None):
        """Modern JSON REST API (PAN-OS 10.1+): /api/rest/v13/<resource>."""
        query = {"key": self.api_key}
        query.update(params or {})
        url = f"https://{self.host}/api/rest/v13/{resource}"
        resp = self.session.get(url, params=query, timeout=self.timeout,
                                verify=False, headers={"Accept": "application/json"})
        if self.debug:
            safe = {k: ("<redacted>" if k == "key" else v) for k, v in query.items()}
            print(f"GET {url}", file=sys.stderr)
            print(f"params: {safe}", file=sys.stderr)
            print(resp.text[:2000], file=sys.stderr)
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"REST v13 {resource} (HTTP {resp.status_code}): {resp.text[:200]}")
        if resp.status_code >= 400 or data.get("status") == "error":
            msg = (data.get("msg") or (data.get("result") or {}).get("msg")
                   or resp.text[:200])
            raise RuntimeError(f"REST v13 {resource} (HTTP {resp.status_code}): {msg}")
        return data


# --------------------------------------------------------------------------- #
# API key retrieval
# --------------------------------------------------------------------------- #
def fetch_api_key(host: str, user: str, password: str) -> str:
    params = {
        "type": "keygen",
        "user": user,
        "password": password,
        "api-version": API_VERSION,
    }
    url = f"https://{host}/api/{API_VERSION}"
    resp = requests.get(url, params=params, timeout=30, verify=False)
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        try:
            key = json.loads(resp.text).get("result", {}).get("key")
        except (json.JSONDecodeError, AttributeError):
            key = None
        if key:
            return key
        raise RuntimeError(f"API key generation failed: {resp.text[:200]!r}")
    key = root.findtext("./result/key")
    if not key:
        msg = root.findtext("./result/msg") or "no key returned"
        raise RuntimeError(f"API key generation failed: {msg}")
    return key


# --------------------------------------------------------------------------- #
# Interactive selection
# --------------------------------------------------------------------------- #
def choose(items: list[str], prompt: str) -> str:
    if not items:
        raise SystemExit(f"No items to select for: {prompt}")
    print(f"\n{prompt}:")
    for i, item in enumerate(items, start=1):
        print(f"  [{i}] {item}")
    while True:
        raw = input("Select number (or 'q' to quit): ").strip()
        if raw.lower() in ("q", "quit"):
            raise SystemExit("bye")
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print(f"Pick a number between 1 and {len(items)}.")


# --------------------------------------------------------------------------- #
# Device groups / objects (parsed from the running config)
# --------------------------------------------------------------------------- #
def dg_containers(config) -> list[dict]:
    """Recursively find every <device-group> container in the config tree.

    Handles top-level, /devices/<panorama-id>/, and folder-based layouts
    without hardcoding paths.
    """
    seen: list[dict] = []
    ids: set[int] = set()

    def walk(parent_key: str, node):
        if isinstance(node, dict):
            if parent_key == "device-group" and id(node) not in ids:
                ids.add(id(node))
                seen.append(node)
            for key, value in node.items():
                if (key == "device-group" and isinstance(value, list)
                        and id(value) not in ids):
                    # PAN-OS 12.x: direct entry list under
                    # config/devices/device-group, no DevGroup wrapper.
                    ids.add(id(value))
                    seen.append({"entry": value})
                walk(key, value)
        elif isinstance(node, list):
            for item in node:
                walk("", item)

    walk("", config)
    return seen


def print_dg_container_shapes(config) -> None:
    """Dump every device-group-shaped container with its config path."""
    def walk(path: str, node):
        if isinstance(node, dict):
            if "DevGroup" in node or "DynGroup" in node:
                print(f"  {path} -> keys: {sorted(node)}", file=sys.stderr)
            for key, value in node.items():
                walk(f"{path}/{key}", value)
        elif isinstance(node, list):
            for item in node:
                name = item.get("name") if isinstance(item, dict) else None
                walk(f"{path}{'[' + str(name) + ']' if name else ''}", item)

    print("container shapes (DevGroup/DynGroup) with paths:", file=sys.stderr)
    walk("config", config)
    # And show where 'device-group' keys live even if shape unexpected.
    def walk2(path: str, node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "device-group":
                    if isinstance(value, list):
                        names = [e.get("name") for e in value
                                 if isinstance(e, dict)]
                        print(f"  {path}/{key} -> list of "
                              f"{len(names)} entries", file=sys.stderr)
                    elif isinstance(value, dict):
                        print(f"  {path}/{key} -> {sorted(value)}",
                              file=sys.stderr)
                walk2(f"{path}/{key}", value)
        elif isinstance(node, list):
            for item in node:
                walk2(path, item)

    walk2("config", config)


def collect_named(obj) -> list[dict]:
    """Find all dicts with a 'name' key anywhere in a JSON structure."""
    found: list[dict] = []
    seen: set[int] = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") and id(node) not in seen:
                seen.add(id(node))
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found


def rest_paged(client: PanoramaClient, resource: str, params: dict) -> list[dict]:
    """Page through a v13 list endpoint via limit/offset."""
    items: list[dict] = []
    offset = 0
    limit = 500
    for _ in range(40):  # cap at 20k items
        data = client.rest_v13(resource, {**params, "limit": limit,
                                          "offset": offset})
        page = collect_named(data)
        items.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return items


def list_device_groups(client: PanoramaClient) -> list[str]:
    # PAN-OS 12.x keeps device groups in the XML config tree under
    # /devices/device-group as a direct entry list.
    names: list[str] = []
    config = client.running_config()
    for container in dg_containers(config):
        for group in DG_GROUPS:
            node = container.get(group)
            if node is not None:
                names += [e["name"] for e in as_entry_list(node)
                          if e.get("name")]
        if not any(container.get(g) for g in DG_GROUPS):
            # Older schema or PAN-OS 12.x: entries directly in container.
            names += [e["name"] for e in as_entry_list(container)
                      if e.get("name")]
    if names:
        return sorted(set(names))

    # Fall back to the modern REST API if the XML tree came up empty.
    v13_errors: list[str] = []
    for kind in ("static", "dynamic"):
        try:
            for entry in rest_paged(client, "device-group",
                                    {"panorama": "LocalDevice", "type": kind}):
                if entry.get("name"):
                    names.append(entry["name"])
        except RuntimeError as exc:
            v13_errors.append(str(exc))
    if names:
        return sorted(set(names))

    top = sorted(config.keys()) if isinstance(config, dict) else str(config)
    print(f"No device-group container found.\n"
          f"Config top-level sections: {top}", file=sys.stderr)
    for err in v13_errors:
        print(f"  REST v13 device-group probe: {err}", file=sys.stderr)
    print_dg_container_shapes(config)
    raise SystemExit(
        "No device groups found. Paste the probe output above and I will "
        "target the real path.")


def get_device_group_objects(client: PanoramaClient, device_group: str,
                             obj_type: str) -> list[dict]:
    dg = find_device_group(client, device_group)
    entries = as_entry_list(dig(dg, obj_type)) if dg is not None else None
    if entries:
        return entries
    if device_group == SHARED_LABEL:
        return []
    # Fall back to the modern REST API (older Panorama schemas).
    try:
        return rest_paged(client, obj_type,
                          {"panorama": "LocalDevice", "device-group": device_group})
    except RuntimeError:
        return entries or []


def find_device_group(client: PanoramaClient, device_group: str) -> dict | None:
    config = client.running_config()
    if device_group == SHARED_LABEL:
        shared = config.get("shared")
        return shared if isinstance(shared, dict) else None
    for container in dg_containers(config):
        for group in DG_GROUPS:
            entry = dig(container, group, device_group)
            if entry is not None:
                return entry
        entry = dig(container, device_group)
        if entry is not None:
            return entry
    return None


def fmt_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Import objects (create/overwrite via type=config action=set)
# --------------------------------------------------------------------------- #
def xpath_quote(value: str) -> str:
    return value.replace("'", "''")


def obj_to_xml(obj: dict) -> str:
    """Render an exported object dict back into PAN-OS element XML."""
    def esc(value: str, attr: bool = False) -> str:
        entities = {'"': "&quot;"} if attr else {}
        return escape(str(value), entities)

    def build(tag: str, value) -> str:
        if tag == "name":
            return ""
        if isinstance(value, dict):
            inner = "".join(build(k, v) for k, v in value.items())
            return f"<{tag}>{inner}</{tag}>"
        if isinstance(value, list):
            return "".join(build(tag, v) for v in value)
        if value is None or value == "":
            return f"<{tag}/>"
        return f"<{tag}>{esc(value)}</{tag}>"

    inner = "".join(build(k, v) for k, v in obj.items() if k != "name")
    return f'<entry name="{esc(str(obj.get("name", "")), attr=True)}">{inner}</entry>'


def dg_set_paths(device_group: str, obj_type: str, name: str) -> list[str]:
    sel = f"entry[@name='{xpath_quote(name)}']"
    if device_group == SHARED_LABEL:
        return [f"/devices/LocalDevice/shared/{obj_type}/{sel}",
                f"/shared/{obj_type}/{sel}"]
    dg_sel = f"entry[@name='{xpath_quote(device_group)}']"
    return [f"/devices/LocalDevice/device-group/DevGroup/{dg_sel}/{obj_type}/{sel}",
            f"/dev/group/{dg_sel}/{obj_type}/{sel}"]


def read_import_file(path: str) -> dict[str, list[dict]]:
    """Group entries in a CSV/JSON file by object type.

    - JSON list:  [ {name...}, ... ]              -> key "" (type asked later)
    - JSON dict:  { "address": [...], "tag": [...] } -> keyed by type
    - CSV with a "type" column                    -> grouped by type
    - CSV without "type" column                   -> key "" (type asked later)
    Key "" means the object type is not specified in the file.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    groups: dict[str, list[dict]] = {}

    def normalize_row(row: dict) -> dict:
        entry: dict = {}
        for key, value in row.items():
            if not key or value in (None, ""):
                continue
            if key == "name":
                entry["name"] = value
                continue
            if value[:1] in "[{":
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            entry[key] = value
        return entry

    if path.lower().endswith(".json"):
        data = json.loads(text)
        if isinstance(data, list):
            groups[""] = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list):
                    groups[key] = [v for v in value if isinstance(v, dict)]
    else:
        reader = csv.DictReader(io.StringIO(text))
        has_type = bool(reader.fieldnames) and "type" in reader.fieldnames
        for row in reader:
            entry = normalize_row(row)
            obj_type = entry.pop("type", "") if has_type else ""
            if entry.get("name"):
                groups.setdefault(obj_type, []).append(entry)

    return {k: v for k, v in groups.items() if v}


def _fields_of(entry: dict | None) -> dict:
    return {k: fmt_value(v) for k, v in (entry or {}).items() if k != "name"}


def diff_entry_lines(before: dict | None, after_effective: dict) -> list[str]:
    """Text diff between the pre-import state (None = did not exist) and
    the effective post-set state (PAN-OS set merges into existing entries)."""
    b, a = _fields_of(before), _fields_of(after_effective)
    lines = []
    for key in sorted(set(b) | set(a)):
        bv, av = b.get(key), a.get(key)
        if bv == av:
            continue
        if bv is not None:
            lines.append(f"  - {key}: {bv}")
        if av is not None:
            lines.append(f"  + {key}: {av}")
    return lines


def build_diff_text(device_group: str, source: str, changes: list[tuple]) -> str:
    header = (f"Panorama import diff\n"
              f"Device group : {device_group}\n"
              f"Source file  : {source}\n"
              f"Generated    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"(before = committed config read via API; after = intended "
              f"effective state after set)\n"
              f"{'=' * 60}\n\n")
    body: list[str] = []
    for obj_type, name, status, lines, err in changes:
        if status == "UNCHANGED":
            continue
        body.append(f"[{status}] {obj_type}/{name}")
        if err:
            body.append(f"  ! {err}")
        body.extend(lines or (["  (no effective field changes)"]
                              if status == "UNCHANGED" else []))
        body.append("")
    return header + "\n".join(body)


def write_diff_file(device_group: str, source: str, changes: list[tuple]) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = safe_filename(device_group, f"diff_{ts}", "txt")
    text = build_diff_text(device_group, source, changes)
    Path(path).write_text(text, encoding="utf-8")
    return path


def _job_summary(data) -> str:
    job = None
    if isinstance(data, dict):
        job = data.get("job")
        if not isinstance(job, dict) and isinstance(data.get("result"), dict):
            job = data["result"].get("job")
    if isinstance(job, dict):
        bits = [f"{k.lstrip('@')}={v}" for k, v in job.items()
                if isinstance(v, str)]
        return "Commit job: " + ", ".join(bits) if bits else \
            f"Commit job: {json.dumps(job)[:200]}"
    return f"Commit result: {json.dumps(data, default=str)[:200]}"


def commit_changes(client: PanoramaClient) -> None:
    description = input("Commit description (blank for none): ").strip()
    print("WARNING: this commits ALL pending Panorama changes (not just this "
          "import) and pushes to managed devices.")
    if input("Type 'yes' to commit: ").strip().lower() != "yes":
        print("Commit skipped; changes stay in the candidate config.")
        return
    errors = []
    try:
        data = client.rest_commit(description)
        client._running_config = None
        print(_job_summary(data))
        print("Monitor progress in Panorama: show jobs id <id> (UI or CLI).")
        return
    except RuntimeError as exc:
        errors.append(f"REST API: {exc}")
    desc_xml = f"<description>{escape(description)}</description>" \
        if description else ""
    try:
        result = client._request(
            {"type": "commit", "cmd": f"<commit>{desc_xml}</commit>"})
        client._running_config = None
        if isinstance(result, dict) and result:
            print(f"Commit job started: {result}")
        else:
            print("Commit command accepted (job queued on Panorama).")
        print("Monitor progress in Panorama: show jobs id <id> (UI or CLI).")
    except RuntimeError as exc:
        errors.append(f"XML API: {exc}")
    if errors:
        for err in errors:
            print(f"commit failed - {err}", file=sys.stderr)
        print("You can still commit from the Panorama UI (Commit button) or "
              "CLI ('commit').", file=sys.stderr)


def revert_import(client: PanoramaClient, device_group: str,
                  applied: list[tuple]) -> None:
    if not applied:
        print("Nothing to revert.")
        return
    print(f"Reverting {len(applied)} change(s) (new objects deleted, modified "
          f"objects restored to their pre-import state):")
    for obj_type, name, existed, _before in applied[:10]:
        print(f"  - {obj_type}/{name} ({'delete' if not existed else 'restore'})")
    if input("Type 'yes' to revert: ").strip().lower() != "yes":
        print("Revert skipped.")
        return
    ok, failed = 0, []
    for obj_type, name, existed, before in applied:
        if existed:
            good, err = write_object(client, device_group, obj_type, name,
                                     before, existed=True)
        else:
            good, err = delete_object(client, device_group, obj_type, name)
        if good:
            ok += 1
        else:
            failed.append((f"{obj_type}/{name}", err))
    client._running_config = None
    print(f"Reverted {ok}/{len(applied)} change(s).")
    for name, err in failed[:5]:
        print(f"  FAILED {name}: {err}", file=sys.stderr)


def write_object(client: PanoramaClient, device_group: str, obj_type: str,
                 name: str, obj: dict, existed: bool) -> tuple[bool, str]:
    """Create/update one object: XML API first, modern REST API on refusal."""
    last = ""
    element = obj_to_xml({**obj, "name": name})
    for set_path in dg_set_paths(device_group, obj_type, name):
        try:
            client.config_set(set_path, element)
            return True, ""
        except RuntimeError as exc:
            last = str(exc)
    if obj_type in REST_RESOURCES:
        try:
            client.rest_write(obj_type, device_group, name, obj,
                              create=not existed)
            return True, ""
        except RuntimeError as exc:
            last = f"XML API: {last}; REST API: {exc}"
    return False, last


def delete_object(client: PanoramaClient, device_group: str, obj_type: str,
                  name: str) -> tuple[bool, str]:
    last = ""
    for set_path in dg_set_paths(device_group, obj_type, name):
        try:
            client.config_delete(set_path)
            return True, ""
        except RuntimeError as exc:
            last = str(exc)
    if obj_type in REST_RESOURCES:
        try:
            client.rest_delete(obj_type, device_group, name)
            return True, ""
        except RuntimeError as exc:
            last = f"XML API: {last}; REST API: {exc}"
    return False, last


def import_objects(client: PanoramaClient, device_group: str) -> None:
    path = input("File to import (.json or .csv): ").strip().strip('"')
    try:
        groups = read_import_file(path)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON in {path}: {exc}\n"
              f"(check for missing brackets/commas; keys and strings need "
              f"double quotes)", file=sys.stderr)
        return
    except OSError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return
    if not groups:
        print("No named entries found in file.")
        return

    # Files without type info: ask which object type the entries are.
    for key in list(groups):
        if key == "":
            obj_type = choose(OBJECT_TYPES, "Object type for these entries")
            groups[obj_type] = groups.get(obj_type, []) + groups.pop("")

    for obj_type in groups:
        if obj_type not in OBJECT_TYPES:
            print(f"warning: '{obj_type}' is not in the known object list, "
                  f"importing anyway.", file=sys.stderr)

    # Snapshot the current state of every object we are about to touch.
    print("\nSnapshotting current state of target objects ...")
    existing: dict[str, dict] = {}
    for obj_type, entries in groups.items():
        names_map = {e["name"]: e for e in
                     get_device_group_objects(client, device_group, obj_type)
                     if e.get("name")}
        existing[obj_type] = names_map

    total = sum(len(v) for v in groups.values())
    print(f"\nWill CREATE/OVERWRITE {total} object(s) in '{device_group}':")
    for obj_type, entries in groups.items():
        print(f"  {obj_type}: {len(entries)}")
        for entry in entries[:5]:
            print(f"    - {entry['name']}")
        if len(entries) > 5:
            print(f"    ... and {len(entries) - 5} more")
    if input("Type 'yes' to send these to Panorama: ").strip().lower() != "yes":
        print("Import cancelled.")
        return

    ok, failed, changes, applied = 0, [], [], []
    for obj_type, entries in groups.items():
        for entry in entries:
            name = entry["name"]
            before = existing[obj_type].get(name)
            payload = {k: fmt_value(v) for k, v in entry.items()
                       if k != "name"}
            applied_entry, err = write_object(
                client, device_group, obj_type, name, entry,
                existed=before is not None)
            if not applied_entry:
                failed.append((f"{obj_type}/{name}", err))
                changes.append((obj_type, name, "FAILED",
                                [], err.split(";")[-1].strip()))
                continue
            ok += 1
            # PAN-OS set merges: fields not in the payload stay as they were.
            eff = {**_fields_of(before), **payload}
            lines = diff_entry_lines(before, eff)
            status = ("NEW" if before is None
                      else "MODIFIED" if lines else "UNCHANGED")
            changes.append((obj_type, name, status, lines, ""))
            applied.append((obj_type, name, before is not None, before))

    print(f"\nApplied {ok}/{total} object(s) into '{device_group}'.")
    for name, err in failed[:5]:
        print(f"  FAILED {name}: {err}", file=sys.stderr)
    if len(failed) > 5:
        print(f"  ... and {len(failed) - 5} more failures", file=sys.stderr)

    if changes:
        diff_path = write_diff_file(device_group, path, changes)
        text = build_diff_text(device_group, path, changes)
        lines = text.splitlines()
        print(f"\n--- diff (full copy: {diff_path}) ---")
        for line in lines[:60]:
            print(line)
        if len(lines) > 60:
            print(f"[... {len(lines) - 60} more lines in {diff_path}]")
        print("--- end diff ---\n")

    if not applied:
        if ok == 0 and failed:
            print("Nothing was applied; both the XML API and the modern REST "
                  "API refused the writes. Full errors above "
                  "(run with --debug for each request).", file=sys.stderr)
        return

    actions = ["commit changes", "revert this import", "leave pending"]
    action = choose(actions, "Post-import")
    if action == actions[0]:
        commit_changes(client)
    elif action == actions[1]:
        revert_import(client, device_group, applied)
    else:
        print("Changes left in the candidate config - commit from the "
              "Panorama UI or CLI when ready.")


def safe_filename(device_group: str, obj_type: str, ext: str) -> str:
    name = f"{device_group}_{obj_type}.{ext}"
    for ch in ' /\\:*?"<>|()':
        name = name.replace(ch, "_")
    return name


def print_objects(client: PanoramaClient, device_group: str, obj_type: str,
                  out_fmt: str = "text") -> None:
    entries = get_device_group_objects(client, device_group, obj_type)
    if not entries:
        print(f"No {obj_type} objects found in device group '{device_group}'.")
        return

    if out_fmt in ("json", "csv"):
        path = safe_filename(device_group, obj_type, out_fmt)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            if out_fmt == "json":
                json.dump(entries, fh, indent=2, ensure_ascii=False)
            else:
                keys: list[str] = []
                for entry in entries:
                    for key in entry:
                        if key not in keys:
                            keys.append(key)
                writer = csv.writer(fh)
                writer.writerow(keys)
                for entry in entries:
                    writer.writerow([fmt_value(entry.get(key))
                                     for key in keys])
        print(f"Wrote {len(entries)} {obj_type} object(s) to {path}")
        return

    print(f"\n{len(entries)} {obj_type} object(s) in device group '{device_group}':\n")
    for entry in entries:
        name = entry.get("name", "(unnamed)")
        print(f"- {name}")
        for key, value in entry.items():
            if key == "name":
                continue
            print(f"    {key}: {fmt_value(value)}")
        print()


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def print_diagnostics(client: PanoramaClient) -> None:
    print("\ndiagnostics:", file=sys.stderr)
    try:
        info = client._request(
            {"type": "op", "cmd": "<show><system><info/></system></show>"})
        sysinfo = info.get("system", info) if isinstance(info, dict) else {}
        for field in ("hostname", "model", "sw-version"):
            value = sysinfo.get(field)
            if value:
                print(f"  {field}: {value}", file=sys.stderr)
    except RuntimeError as exc:
        print(f"  op show system info failed: {exc}", file=sys.stderr)
    try:
        config = client.running_config()
        users = as_entry_list(dig(config, "mgt-config", "users"))
        print(f"  running-config fetched, "
              f"{len(users)} local admin account(s)", file=sys.stderr)
    except RuntimeError as exc:
        print(f"  running-config fetch failed: {exc}", file=sys.stderr)


def run_diagnostics(client: PanoramaClient) -> None:
    """Fire a battery of API probes and dump raw responses for triage."""
    probes = [
        ("op, XML-style cmd (system info)",
         {"type": "op", "cmd": "<show><system><info/></system></show>"}, ""),
        ("config SHOW (committed) running config (first 3000 chars)",
         {"type": "config", "action": "show"}, ""),
        ("config GET (candidate) /dev/group - expected empty on 12.x",
         {"type": "config", "action": "get", "path": "/dev/group"}, ""),
        ("new REST shim /api/rest/v13 device-group",
         {"panorama": "LocalDevice", "type": "device-group"},
         "/api/rest/v13/device-group"),
        ("modern REST v12: address objects in shared",
         {"location": "shared", "limit": "3"},
         "/restapi/v12.0/Objects/Addresses"),
        ("modern REST v12: address objects in device-group 'Physical'",
         {"location": "device-group", "device-group": "Physical",
          "limit": "3"},
         "/restapi/v12.0/Objects/Addresses"),
        ("modern REST v12: device groups",
         {"panorama": "LocalDevice", "limit": "20"},
         "/restapi/v12.0/DeviceGroups"),
        ("modern REST v11: address objects in shared",
         {"location": "shared", "limit": "3"},
         "/restapi/v11.0/Objects/Addresses"),
        ("REST API docs index",
         {}, "/restapi-doc/"),
    ]
    for label, params, path in probes:
        print(f"\n===== {label} =====", file=sys.stderr)
        try:
            print(client.raw_get(params, path), file=sys.stderr)
        except requests.RequestException as exc:
            print(f"request failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Panorama interactive object browser")
    parser.add_argument("--host", default=env_or_default("PANORAMA_HOST"),
                        help="Panorama server IP/hostname (or PANORAMA_HOST)")
    parser.add_argument("--user", default=env_or_default("PANORAMA_USER"),
                        help="Admin username (or PANORAMA_USER)")
    parser.add_argument("--password", default=env_or_default("PANORAMA_PASSWORD"),
                        help="Admin password (or PANORAMA_PASSWORD); prompts hidden if omitted")
    parser.add_argument("--debug", action="store_true",
                        help="print raw Panorama API requests/responses to stderr")
    parser.add_argument("--diagnose", action="store_true",
                        help="run API probes and dump raw responses to stderr")
    parser.add_argument("--renew-key", action="store_true",
                        help="ignore/delete cached API key and generate a new one")
    parser.add_argument("--output", choices=("text", "json", "csv"),
                        default="text",
                        help="object listing format; json/csv are saved to "
                             "<device_group>_<obj_type>.<ext> in the CWD")
    args = parser.parse_args()

    if not args.host:
        if len(sys.argv) == 1:
            parser.print_help()
            print("\nExample: python panorama_cli.py --host panorama.example.com "
                  "--output json")
            print("Use --help to see all options.")
            return
        sys.exit("error: --host (or PANORAMA_HOST) is required")

    # 1. API key: reuse cached/env key, otherwise generate a new one.
    if args.renew_key:
        try:
            key_file_path().unlink()
        except FileNotFoundError:
            pass
        os.environ.pop("PANORAMA_API_KEY", None)
        api_key = None
    else:
        api_key = load_api_key(args.host)
    if api_key:
        print(f"Using existing API key for {args.host}")
    else:
        if not args.user:
            args.user = input("Username: ").strip()
        if not args.password:
            args.password = getpass.getpass("Password (hidden): ")
        if not args.user or not args.password:
            sys.exit("error: no saved API key; username and password are required")
        print(f"Generating API key on {args.host} ...")
        api_key = fetch_api_key(args.host, args.user, args.password)
        save_api_key(args.host, api_key)
        print("API key saved to .panorama_api_key")

    client = PanoramaClient(args.host, api_key, debug=args.debug)

    if args.diagnose:
        run_diagnostics(client)
        return

    # 2. Fetch running config -> list device groups -> select one.
    print("Fetching running config ...")
    groups = list_device_groups(client)
    if isinstance(client.running_config().get("shared"), dict):
        groups.insert(0, SHARED_LABEL)
    device_group = choose(groups, "Device groups")
    print(f"Selected device group: {device_group}")

    # 3. Action: list, export or import.
    actions = ["list objects", "export to CSV/JSON", "import from CSV/JSON file"]
    if args.output == "text":
        action = choose(actions, "Action")
    else:
        action = actions[1]

    if action == actions[2]:
        import_objects(client, device_group)
        return

    # 4. Object type.
    obj_type = choose(OBJECT_TYPES, "Object types")

    if action == actions[1]:
        out_fmt = args.output
        if out_fmt == "text":
            out_fmt = choose(["csv", "json"], "Export format")
        print_objects(client, device_group, obj_type, out_fmt=out_fmt)
    else:
        print_objects(client, device_group, obj_type)


if __name__ == "__main__":
    main()
