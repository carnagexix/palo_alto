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
import re
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
# Order in which object types are applied so references resolve (groups
# reference addresses/services, objects reference tags).
BUILD = "2026-11-14-fix13 (snapshots: cache unsupported state, concise reason, auto-offer local dated snapshot fallback)"
APPLY_ORDER = ["tag", "application", "service", "service-group", "address",
               "address-group"]
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
# Optional object fields we are allowed to drop (with a warning) if the REST
# build's enum rejects their value; key -> message aliases to look for.
DROPPABLE_FIELDS = {
    "color": ("color",),
    "tag": ("tag",),
    "comments": ("comments",),
    "description": ("description",),
    "source-port": ("source-port", "source_port"),
    "source_port": ("source-port", "source_port"),
}


def _dropped_field_hint(error_text: str, obj: dict) -> str | None:
    """Name of a droppable field the server complained about, if any."""
    low = error_text.lower()
    for key in obj:
        aliases = DROPPABLE_FIELDS.get(key.lower())
        if aliases and any(a in low for a in aliases):
            return key
    return None


# --------------------------------------------------------------------------- #
# Config / helpers
# --------------------------------------------------------------------------- #
def env_or_default(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val else default


def key_file_path() -> Path:
    return Path(__file__).resolve().parent / ".panorama_api_key"


def _load_keyring() -> dict:
    """API keys cached per Panorama host: {host: {host, key, saved}}.
    Migrates the legacy single-host file {host, key} automatically."""
    path = key_file_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict) and "key" in data and "host" in data:
        data = {data["host"]: {"host": data["host"], "key": data["key"]}}
    return data if isinstance(data, dict) else {}


def _save_keyring(ring: dict) -> None:
    path = key_file_path()
    try:
        path.write_text(json.dumps(ring, indent=2) + "\n")
        path.chmod(0o600)
    except OSError as exc:
        print(f"warning: could not save API keys to {path}: {exc}",
              file=sys.stderr)


def save_api_key(host: str, key: str) -> None:
    ring = _load_keyring()
    ring[host] = {"host": host, "key": key,
                  "saved": datetime.now().isoformat(timespec="seconds")}
    _save_keyring(ring)


def forget_api_key(host: str) -> None:
    ring = _load_keyring()
    if ring.pop(host, None) is not None:
        _save_keyring(ring)
    os.environ.pop("PANORAMA_API_KEY", None)


def list_api_keys() -> list[tuple[str, str]]:
    """(host, saved-date) for every cached API key, sorted by host."""
    return sorted((host, entry.get("saved", "?"))
                  for host, entry in _load_keyring().items()
                  if isinstance(entry, dict) and entry.get("key"))


def load_api_key(host: str) -> str | None:
    # Environment variable wins, then the cached key for this exact host.
    env_key = env_or_default("PANORAMA_API_KEY")
    if env_key:
        return env_key
    entry = _load_keyring().get(host)
    return entry.get("key") if isinstance(entry, dict) else None


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

    dig(config, "device-group", "MY-DG", "address").
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
            if query.get("type") == "config" and query.get("action") in ("show", "get"):
                body = f"[configuration XML response hidden: {len(resp.content)} bytes]"
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
                hint = (" (API key may be invalid/expired - re-run with "
                        "--renew-key to regenerate it)")
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

    def op_partial_config_load(self, subtree_xml: str | list[str]):
        """Write a config subtree through the op API's load/partial command.

        type=op works even when type=config set is refused (code 16), and
        partial-load is the classic route for Panorama rule bulk changes.
        Accepts one subtree or a list of candidate roots to try in order."""
        if isinstance(subtree_xml, str):
            subtree_xml = [subtree_xml]
        data = {"key": self.api_key, "api-version": API_VERSION,
                "type": "op"}
        url = f"https://{self.host}/api/{API_VERSION}"
        errs: list[str] = []
        for sub in subtree_xml:
            data["cmd"] = (f"<load><config><partial>{sub}"
                           f"</partial></config></load>")
            try:
                self._handle_response(
                    self.session.post(url, data=data, timeout=self.timeout,
                                      verify=False), url, data)
                return
            except RuntimeError as exc:
                if "code 16" not in str(exc):
                    errs.append(str(exc))
                    continue
                try:
                    self._handle_response(
                        self.session.get(url, params=data,
                                         timeout=self.timeout, verify=False),
                        url, data)
                    return
                except RuntimeError as exc2:
                    errs.append(str(exc2))
        raise RuntimeError("; ".join(errs))

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
            if payload is not None:
                print(f"body: {payload}", file=sys.stderr)
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
            # this build nests causes under details[0], not details
            details = data.get("details")
            if details:
                blocks = details if isinstance(details, list) else [details]
                causes = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    for c in block.get("causes") or []:
                        if isinstance(c, dict) and c.get("description"):
                            causes.append(c["description"])
                if causes:
                    msg = f"{msg} ({'; '.join(causes)})"
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
    def _rest_body(name: str, device_group: str, obj: dict,
                   style: str = "full", obj_type: str = "") -> dict:
        fields: dict = {}
        for key, value in obj.items():
            if key == "name":
                continue
            lkey = key.lower()
            if lkey == "protocol" and isinstance(value, str):
                # REST enum for service protocol is lower-case (xml uses TCP)
                value = value.lower()
            elif lkey == "tag" and isinstance(value, (str, list)):
                # REST wants tag as {"member": [...]}, XML tolerated a string
                value = {"member": value if isinstance(value, list)
                         else [value]}
            elif lkey == "static" and isinstance(value, str):
                # accept "a, b, c" as member list
                value = {"member": [s.strip() for s in value.split(",")
                                    if s.strip()]}
            elif key.endswith("member") or key in ("member",):
                value = value if isinstance(value, list) else [value]
            elif isinstance(value, dict) and "member" in value:
                member = value["member"]
                value = {**value,
                         "member": member if isinstance(member, list)
                         else [member]}
            fields[key] = value
        # this 12.1 build models custom services as a nested object keyed by
        # the (lowercase) protocol name: "protocol": {"tcp": {"port": ...}}
        if (obj_type == "service" and isinstance(fields.get("protocol"), str)
                and "port" in fields):
            proto = fields.pop("protocol")
            spec: dict = {"port": fields.pop("port")}
            sport = fields.pop("source-port", None)
            if sport is not None and str(sport).lower() != "any":
                spec["source-port"] = sport
            fields["protocol"] = {proto: spec}
        # service groups key their members as "members" (not "static" like
        # address groups); custom applications carry protocol inside the port
        # members under "default": {"port": {"member": ["tcp/8001"]}}
        if obj_type == "service-group":
            static_val = fields.pop("static", None)
            if static_val is None:
                static_val = fields.pop("members", None)
            if isinstance(static_val, dict):
                fields["members"] = static_val
            elif isinstance(static_val, list):
                fields["members"] = {"member": static_val}
            elif static_val is not None:
                fields["members"] = {"member": [str(static_val)]}
        if obj_type == "application":
            pval = fields.get("protocol")
            protos: list[str] = []
            if isinstance(pval, str):
                protos = [pval.lower()]
            elif isinstance(pval, list):
                protos = [str(p).lower() for p in pval]
            elif isinstance(pval, dict) and isinstance(pval.get("member"),
                                                       (str, list)):
                mem = pval["member"]
                protos = [str(p).lower() for p in
                          (mem if isinstance(mem, list) else [mem])]
            praw = fields.get("port", "")
            ports = [str(p).strip() for p in
                     (praw if isinstance(praw, list)
                      else str(praw).split(",")) if str(p).strip()]
            if protos and ports:
                fields.pop("protocol", None)
                fields.pop("port", None)
                spec = {"port": {"member": [f"{p}/{q}"
                                            for p in protos for q in ports]}}
                sport = fields.pop("source-port", None)
                if sport is not None and str(sport).lower() != "any":
                    spec["source-port"] = {
                        "member": sport if isinstance(sport, list)
                        else [str(sport)]}
                fields["default"] = spec
        entry: dict = {}
        if style == "plain":
            entry["name"] = name
        else:
            entry["@location"] = ("shared" if device_group == SHARED_LABEL
                                  else "device-group")
            entry["@name"] = name
            if style == "full" and device_group != SHARED_LABEL:
                entry["@device-group"] = device_group
        entry.update(fields)
        return {"entry": [entry]}

    @staticmethod
    def _rest_absent(exc) -> bool:
        return any(w in str(exc).lower() for w in
                   ("not present", "not found", "not exist", "does not",
                    "no object", "http 404"))

    def _rest_location_attempts(self, device_group: str,
                                base_params: dict) -> list[dict]:
        """Param sets to try: device-group writes on some 12.1 REST builds
        only work when the 'panorama' query parameter is added."""
        if device_group == SHARED_LABEL:
            return [base_params]
        plain = dict(base_params)
        pano = {**base_params, "panorama": "LocalDevice"}
        cached = getattr(self, "_dg_style", None)
        if cached and cached[1]:
            return [pano, plain]
        return [plain, pano]

    def rest_get_entry(self, obj_type: str, device_group: str,
                       name: str) -> dict | None:
        """Return the REST view of one object (None if absent/not readable)."""
        resource = REST_RESOURCES[obj_type]
        params = {**self._rest_location(device_group), "name": name}
        last: RuntimeError | None = None
        data = None
        for p in self._rest_location_attempts(device_group, params):
            try:
                data = self._rest("GET", f"{self.rest_base()}/{resource}", p)
                break
            except RuntimeError as exc:
                if self._rest_absent(exc):
                    return None
                last = exc
        if data is None:
            raise last  # type: ignore[misc]
        result = data.get("result") if isinstance(data.get("result"), dict) \
            else data
        entries = result.get("entry") if isinstance(result, dict) else None
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("@name") == name:
                return {k: v for k, v in entry.items() if not k.startswith("@")}
        return None

    def rest_write(self, obj_type: str, device_group: str, name: str,
                   obj: dict, create: bool) -> dict:
        """Create/update via REST, probing request formats the box accepts.

        Some 12.1 REST builds answer device-group writes with
        'Invalid Object' unless 'panorama=LocalDevice' is present and/or the
        entry omits the @device-group/@location attributes. We try the
        combinations in order and remember the winning one. If the server
        then still rejects a *named optional field* (e.g. an enum mismatch),
        we retry once without that field and warn loudly."""
        resource = REST_RESOURCES[obj_type]
        base = self.rest_base()
        params = {**self._rest_location(device_group), "name": name}
        first = "POST" if create else "PUT"
        other = "PUT" if create else "POST"
        errors: list[str] = []
        conflict = False

        if device_group == SHARED_LABEL:
            styles = [("full", False)]
        else:
            styles = [("full", True), ("full", False), ("minimal", True),
                      ("minimal", False), ("plain", True), ("plain", False)]
            cached = getattr(self, "_dg_style", None)
            if cached and cached in styles:
                styles.remove(cached)
                styles.insert(0, cached)

        def try_body(method: str, body: dict, note: str):
            nonlocal conflict
            for style, pano in styles:
                p = dict(params)
                if pano:
                    p["panorama"] = "LocalDevice"
                tag = f"{method} {style}{' +panorama' if pano else ''}{note}"
                try:
                    data = self._rest(method, f"{base}/{resource}", p, body)
                except RuntimeError as exc:
                    errors.append(f"{tag}: {exc}")
                    if any(w in str(exc).lower() for w in
                           ("unique", "exist", "already", "not found")):
                        conflict = True
                        break
                    continue
                if device_group != SHARED_LABEL:
                    self._dg_style = (style, pano)
                return data
            return None

        full_body = self._rest_body(name, device_group, obj,
                                     obj_type=obj_type)
        data = try_body(first, full_body, "")
        if data is None and conflict:
            data = try_body(other, full_body, "")
        # the default service body is {"tcp": {"port": ...}}; if still
        # refused, try the richer shapes seen on existing service entries
        if (data is None and obj_type == "service"
                and isinstance(obj.get("protocol"), str)
                and any("protocol" in e.lower() for e in errors[-3:])):
            pv = obj["protocol"].strip().lower()
            spec: dict = {}
            if obj.get("port") not in (None, ""):
                spec["port"] = str(obj["port"])
            alt_proto = []
            if pv in ("tcp", "udp") and spec:
                alt_proto = [
                    ({pv: {**spec, "source-port":
                           str(obj.get("source-port", "any") or "any")}},
                     " [proto sport]"),
                    ({pv: {**spec, "override": {"no": {}}}},
                     " [proto override]"),
                    ({pv.upper(): dict(spec)}, " [proto upper key]"),
                ]
            for pval, note in alt_proto:
                alt = json.loads(json.dumps(full_body))
                for entry in alt.get("entry") or []:
                    entry["protocol"] = pval
                data = try_body(first, alt, note)
                if data is None and conflict:
                    data = try_body(other, alt, note)
                if data is not None:
                    break
        if data is None:
            dropped = _dropped_field_hint(" ".join(errors[-3:]), obj)
            if dropped:
                trimmed = {k: v for k, v in obj.items() if k != dropped}
                print(f"WARNING REST: '{name}' rejected on optional field "
                      f"'{dropped}' ({errors[-1]}); retrying WITHOUT it "
                      f"- add it manually if needed", file=sys.stderr)
                trim_body = self._rest_body(name, device_group, trimmed,
                                            obj_type=obj_type)
                data = try_body(first, trim_body, f" [no {dropped}]")
                if data is None and conflict:
                    data = try_body(other, trim_body, f" [no {dropped}]")
        if data is not None:
            return data
        # last resort: replace (delete then create)
        try:
            self.rest_delete(obj_type, device_group, name)
        except RuntimeError as exc:
            errors.append(f"replace: {exc}")
        else:
            data = try_body("POST", full_body, "")
            if data is not None:
                return data
        if self.debug and obj_type in REST_RESOURCES:
            # dump how the box itself serialises this object type so the
            # expected request schema can be read off an existing entry
            for probe in ({**self._rest_location(device_group),
                           "limit": 5}, {"location": "shared", "limit": 3}):
                try:
                    self._rest("GET", f"{base}/{resource}", probe)
                except RuntimeError:
                    pass
        raise RuntimeError("; ".join(errors[-2:]))

    def rest_delete(self, obj_type: str, device_group: str,
                    name: str) -> dict:
        resource = REST_RESOURCES[obj_type]
        params = {**self._rest_location(device_group), "name": name}
        last: RuntimeError | None = None
        for p in self._rest_location_attempts(device_group, params):
            try:
                return self._rest("DELETE", f"{self.rest_base()}/{resource}", p)
            except RuntimeError as exc:
                if self._rest_absent(exc):
                    return {}
                last = exc
        raise last  # type: ignore[misc]

    @staticmethod
    def _policy_resource(rulebase: str) -> str:
        """Panorama policy endpoints differ from firewall /Policies/SecurityRules.

        Example: post-rulebase/security -> /Policies/SecurityPostRules.
        Never guess endpoints with POST: POST would create policies.
        """
        parts = rulebase.split("/")
        if len(parts) != 2 or parts[0] not in ("pre-rulebase", "post-rulebase"):
            raise RuntimeError(f"unsupported Panorama rulebase: {rulebase!r}")
        policy_types = {
            "security": "Security", "nat": "NAT", "qos": "QoS",
            "pbr": "PolicyBasedForwarding", "dos": "DoS",
            "decryption": "Decryption",
            "application-override": "ApplicationOverride",
            "authentication": "Authentication",
        }
        prefix = policy_types.get(parts[1])
        if not prefix:
            raise RuntimeError(f"unsupported policy type: {parts[1]!r}")
        phase = "Pre" if parts[0] == "pre-rulebase" else "Post"
        return f"/Policies/{prefix}{phase}Rules"

    def _rule_rest_params(self, device_group: str, rule_name: str) -> dict:
        return {**self._rest_location(device_group), "name": rule_name}

    def rest_rule_get_entry(self, device_group: str, rulebase: str,
                            rule_name: str) -> dict:
        """Read a rule from the candidate policy API before modifying it.

        Preserve the fields/metadata supplied by Panorama rather than PUTting
        a potentially stale copy extracted from committed running config.
        """
        endpoint = f"{self.rest_base()}{self._policy_resource(rulebase)}"
        data = self._rest("GET", endpoint,
                          self._rule_rest_params(device_group, rule_name))
        result = data.get("result") if isinstance(data.get("result"), dict) else data
        entries = result.get("entry") if isinstance(result, dict) else None
        if isinstance(entries, dict):
            entries = [entries]
        for entry in (entries if isinstance(entries, list) else []):
            if isinstance(entry, dict) and entry.get("@name", entry.get("name")) == rule_name:
                return entry
        raise RuntimeError(f"REST policy GET returned no exact match for {rule_name!r}; refusing blind update")

    def rest_rule_update(self, device_group: str, rulebase: str,
                         rule_name: str, rule: dict) -> dict:
        """Update an existing candidate rule with documented Panorama PUT."""
        endpoint = f"{self.rest_base()}{self._policy_resource(rulebase)}"
        entry = {k: v for k, v in rule.items() if k != "name"}
        entry["@name"] = rule_name
        entry["@location"] = "shared" if device_group == SHARED_LABEL else "device-group"
        if device_group != SHARED_LABEL:
            entry["@device-group"] = device_group
        entry.pop("@rulebase", None)  # the URI selects pre or post rules
        return self._rest("PUT", endpoint,
                          self._rule_rest_params(device_group, rule_name),
                          {"entry": [entry]})

    def config_rule_field_read(self, path: str, field: str) -> list[str] | None:
        """Read a specific candidate XML field; None means unavailable."""
        result = self._request({"type": "config", "action": "get", "xpath": path})
        if isinstance(result, dict) and field in result:
            return member_values(result[field])
        return None

    def config_rule_field_edit(self, path: str, field: str,
                               members: list[str]):
        """Standard XML API edit at a known, existing field XPath."""
        url = f"https://{self.host}/api/"
        data = {"type": "config", "action": "edit", "key": self.api_key,
                "xpath": path, "element": xml_value(field, {"member": members})}
        resp = self.session.post(url, data=data, timeout=self.timeout,
                                 verify=False)
        return self._handle_response(resp, url, data)

    def config_rule_desc_edit(self, rule_xpath: str, text: str):
        """Replace a rule's description through the XML config API."""
        url = f"https://{self.host}/api/"
        data = {"type": "config", "action": "edit", "key": self.api_key,
                "xpath": f"{rule_xpath}/description",
                "element": f"<description>{escape(text)}</description>"}
        resp = self.session.post(url, data=data, timeout=self.timeout,
                                 verify=False)
        return self._handle_response(resp, url, data)

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

    def config_show_raw(self) -> str:
        """Return the committed configuration as raw XML text."""
        url = f"https://{self.host}/api/{API_VERSION}"
        params = {"key": self.api_key, "api-version": API_VERSION,
                  "type": "config", "action": "show"}
        resp = self.session.get(url, params=params, timeout=self.timeout,
                                 verify=False)
        if self.debug:
            print(f"REQUEST {url}\nparams: key=<redacted> type=config "
                  f"action=show\n[raw XML, {len(resp.content)} bytes]",
                  file=sys.stderr)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        if root.get("status") != "success":
            code = root.get("code") or "?"
            msg = root.findtext("msg") or resp.text[:200]
            raise RuntimeError(f"Panorama API error (code {code}): {msg}")
        result = root.find("result")
        if result is None:
            return resp.text
        return ET.tostring(result, encoding="unicode")

    def _snapshot_api_unsupported(self) -> bool:
        return getattr(self, "_snap_unsupported", False)

    def _mark_snapshot_api_unsupported(self, reason: str) -> None:
        self._snap_unsupported = True
        self._snap_reason = reason

    def create_snapshot(self, name: str, description: str = "") -> str:
        """Take a Panorama configuration snapshot; try REST commands first,
        then the XML op equivalents (op cmd trees have no <commands>
        wrapper). Returns the method that worked."""
        if self._snapshot_api_unsupported():
            raise RuntimeError(getattr(self, "_snap_reason",
                                       "snapshots not exposed via API"))
        entry = {"@name": name}
        if description:
            entry["description"] = description
        errs: list[str] = []
        for path in (f"{self.rest_base()}/Commands/Snapshot/Config",
                     f"{self.rest_base()}/Commands/SnapshotConfig",
                     f"{self.rest_base()}/Commands/Snapshot"):
            try:
                self._rest("POST", path, {"panorama": "LocalDevice"},
                           {"entry": [entry]})
                return f"REST {path}"
            except RuntimeError as exc:
                errs.append(f"REST {path}: {exc}")
        desc = (f"<description>{escape(description)}</description>"
                if description else "")
        for cmd in (
                f"<snapshot><config>"
                f"<entry name=\"{escape(name)}\">{desc}</entry>"
                f"</config></snapshot>",
                f"<export><config>"
                f"<snapshot name=\"{escape(name)}\"/>"
                f"</config></export>"):
            try:
                self._request({"type": "op", "cmd": cmd})
                return "XML op " + cmd[:40] + "..."
            except RuntimeError as exc:
                errs.append(f"XML op <{cmd.split('>', 1)[0][1:]}>: {exc}")
        reason = ("this Panorama does not expose configuration snapshots "
                  "through the API (REST snapshot routes are not served and "
                  "the op export command is refused for API clients). "
                  "Use 'export configuration locally' instead, or take "
                  "snapshots in the Panorama UI.")
        if all(("501" in e or "code 17" in e) for e in errs):
            self._mark_snapshot_api_unsupported(reason)
        raise RuntimeError(reason + "  Details: " + "; ".join(errs))

    def list_snapshots(self) -> list[dict]:
        """List saved configuration snapshots (best-effort probe)."""
        if self._snapshot_api_unsupported():
            raise RuntimeError(getattr(self, "_snap_reason",
                                       "snapshots not exposed via API"))
        try:
            data = self._rest("GET",
                              f"{self.rest_base()}/Commands/Snapshot/Config",
                              {"panorama": "LocalDevice"})
            result = (data.get("result")
                      if isinstance(data.get("result"), dict) else data)
            entries = result.get("entry") if isinstance(result, dict) else None
            if isinstance(entries, dict):
                entries = [entries]
            if isinstance(entries, list):
                return [e for e in entries if isinstance(e, dict)]
        except RuntimeError as exc:
            rest_err = str(exc)
        else:
            return []
        last: RuntimeError | None = None
        op_errs: list[str] = []
        for cmd in ("<show><snapshot/></show>",
                    "<show><snapshot><config/></snapshot></show>",
                    "<show><checkpoints/></show>"):
            try:
                result = self._request({"type": "op", "cmd": cmd})
            except RuntimeError as exc:
                last = exc
                op_errs.append(str(exc))
                continue
            node = result
            if isinstance(node, dict):
                node = node.get("snapshot", node)
                if isinstance(node, dict):
                    node = node.get("config", node)
                    entries = (node.get("entry")
                               if isinstance(node, dict) else None)
                    if isinstance(entries, dict):
                        entries = [entries]
                    if isinstance(entries, list):
                        return [e for e in entries if isinstance(e, dict)]
        if ("501" in rest_err and op_errs
                and all("code 17" in e for e in op_errs)):
            self._mark_snapshot_api_unsupported(
                "this Panorama does not expose configuration snapshots "
                "through the API; saved snapshots cannot be listed or "
                "downloaded via API on this build")
        raise RuntimeError(f"REST: {rest_err}"
                           + (f"; XML op: {last}" if last else ""))

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
                    # config/devices/device-group, entries listed directly.
                    ids.add(id(value))
                    seen.append({"entry": value})
                walk(key, value)
        elif isinstance(node, list):
            for item in node:
                walk("", item)

    walk("", config)
    return seen


def _dg_entries(container) -> list[dict]:
    """All device-group entries in a container, including nested
    group-of-groups entries, at any depth."""
    out: list[dict] = []
    for entry in as_entry_list(container):
        out.append(entry)
        if isinstance(entry.get("entry"), (dict, list)):
            out += _dg_entries(entry)
    return out


def print_dg_container_shapes(config) -> None:
    """Dump every device-group container with its config path."""
    # Show where 'device-group' keys live even if shape unexpected.
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
    # /devices/device-group as a direct entry list; older builds may nest
    # groups inside groups. Collect every entry name at any depth.
    names: list[str] = []
    config = client.running_config()
    for container in dg_containers(config):
        names += [e["name"] for e in _dg_entries(container)
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
        for entry in _dg_entries(container):
            if entry.get("name") == device_group:
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


def xml_value(tag: str, value) -> str:
    """Render a (possibly nested) dict/list value as XML under one tag."""
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

    return build(tag, value)


def obj_to_xml(obj: dict) -> str:
    """Render an exported object dict back into PAN-OS element XML."""
    def esc(value: str, attr: bool = False) -> str:
        entities = {'"': "&quot;"} if attr else {}
        return escape(str(value), entities)

    inner = "".join(xml_value(k, v) for k, v in obj.items() if k != "name")
    return f'<entry name="{esc(str(obj.get("name", "")), attr=True)}">{inner}</entry>'


def dg_set_paths(device_group: str, obj_type: str, name: str) -> list[str]:
    sel = f"entry[@name='{xpath_quote(name)}']"
    if device_group == SHARED_LABEL:
        return [f"/config/shared/{obj_type}/{sel}",
                f"/devices/LocalDevice/shared/{obj_type}/{sel}"]
    dg_sel = f"entry[@name='{xpath_quote(device_group)}']"
    return [f"/config/devices/entry[@name='localhost.localdomain']"
            f"/device-group/{dg_sel}/{obj_type}/{sel}",
            f"/devices/LocalDevice/device-group/{dg_sel}/{obj_type}/{sel}",
            f"/dev/group/{dg_sel}/{obj_type}/{sel}"]


def rule_field_paths(device_group: str, rulebase: str, rule_name: str,
                     field: str) -> list[str]:
    """Valid Panorama XML rule-field XPaths (candidate config hierarchy)."""
    if field not in ("source", "destination"):
        raise ValueError(f"unsupported member field {field!r}")
    if not (rulebase.startswith("pre-rulebase/") or
            rulebase.startswith("post-rulebase/")):
        raise ValueError(f"unexpected rulebase {rulebase!r}")
    q = xpath_quote(rule_name)
    suffix = f"/{rulebase}/rules/entry[@name='{q}']/{field}"
    if device_group == SHARED_LABEL:
        return [f"/config/shared{suffix}"]
    dg = xpath_quote(device_group)
    # Documented Panorama XML configuration hierarchy.
    return [
        "/config/devices/entry[@name='localhost.localdomain']"
        f"/device-group/entry[@name='{dg}']{suffix}",
    ]

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
    desc_xml = f"<description>{escape(description)}</description>" \
        if description else ""
    # Classic XML commit (type=commit) first; some 12.1 builds lack a REST
    # Commit route.
    try:
        result = client._request(
            {"type": "commit", "cmd": f"<commit>{desc_xml}</commit>"})
        client._running_config = None
        printed = False
        if isinstance(result, dict):
            msg = result.get("msg")
            if isinstance(msg, dict) and msg.get("line"):
                print(msg["line"])
                printed = True
            if result.get("job"):
                print(f"Commit job id: {result['job']}")
                printed = True
        if not printed:
            print("Commit command accepted (job queued on Panorama).")
        print("Monitor progress in Panorama: show jobs id <id> (UI or CLI).")
        return
    except RuntimeError as exc:
        errors.append(f"XML API: {exc}")
    try:
        data = client.rest_commit(description)
        client._running_config = None
        print(_job_summary(data))
        print("Monitor progress in Panorama: show jobs id <id> (UI or CLI).")
        return
    except RuntimeError as exc:
        errors.append(f"REST API: {exc}")
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
    # delete dependents first (group -> address -> tag), then restore
    applied = sorted(applied,
                     key=lambda c: APPLY_ORDER.index(c[0])
                     if c[0] in APPLY_ORDER else len(APPLY_ORDER))
    ordered = (list(reversed([c for c in applied if not c[2]]))
               + [c for c in applied if c[2]])
    for obj_type, name, existed, before in ordered:
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
        # The XML snapshot cannot see candidate-config objects on some
        # builds; ask the REST API what actually exists before deciding
        # create vs update.
        create = not existed
        try:
            create = client.rest_get_entry(obj_type, device_group,
                                           name) is None
        except RuntimeError:
            pass
        try:
            client.rest_write(obj_type, device_group, name, obj,
                              create=create)
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
    while True:
        path = input("File to import (.json or .csv, 'q' to quit): ") \
            .strip().strip('"')
        if path.lower() in ("q", "quit"):
            raise SystemExit("bye")
        if not path:
            continue
        try:
            groups = read_import_file(path)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON in {path}: {exc}\n"
                  f"(check for missing brackets/commas; keys and strings need "
                  f"double quotes)", file=sys.stderr)
            continue
        except OSError as exc:
            print(f"could not read {path}: {exc}", file=sys.stderr)
            continue
        if not groups:
            print(f"No named entries found in {path}.")
            continue
        break

    # Files without type info: ask which object type the entries are.
    for key in list(groups):
        if key == "":
            obj_type = choose(OBJECT_TYPES, "Object type for these entries")
            groups[obj_type] = groups.get(obj_type, []) + groups.pop("")

    for obj_type in groups:
        if obj_type not in OBJECT_TYPES:
            print(f"warning: '{obj_type}' is not in the known object list, "
                  f"importing anyway.", file=sys.stderr)
    # Apply in dependency order so groups/tag references exist when used.
    groups = dict(sorted(
        groups.items(),
        key=lambda kv: APPLY_ORDER.index(kv[0]) if kv[0] in APPLY_ORDER
        else len(APPLY_ORDER)))

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
        print("Nothing new to apply (objects already match), but there may "
              "still be pending candidate changes to commit below.")

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
# Where-used: find rules (security/NAT/PBR) referencing an object or IP
# --------------------------------------------------------------------------- #
RULE_DETAIL_KEYS = ("from", "to", "source", "source-hip", "destination",
                    "destination-hip", "service", "application", "action",
                    "negated", "disabled", "description")
IP_FIELDS = ("ip-netmask", "ip-range", "ip-wildcard")


def iter_rules(container):
    """Yield (rule entry, rulebase path) for every rule under container.

    Handles rulebase / pre-rulebase / post-rulebase and security/nat/pbr
    sections without hardcoding any single layout."""
    stack = [(container, [])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            for key, value in node.items():
                if key == "rules" and isinstance(value, (dict, list)):
                    rb = "/".join(path) if path else "?"
                    for rule in as_entry_list(value):
                        yield rule, rb
                elif isinstance(value, (dict, list)):
                    stack.append((value, path + [key]))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append((value, path))


def member_values(value) -> list[str]:
    """Extract member names from a rule field (plain, <member> form or list)."""
    out: list[str] = []
    if isinstance(value, dict):
        mem = value.get("member")
        if isinstance(mem, list):
            out.extend(str(m) for m in mem if isinstance(m, (str, int, float)))
        elif isinstance(mem, (str, int, float)):
            out.append(str(mem))
    elif isinstance(value, list):
        out.extend(str(v) for v in value if isinstance(v, (str, int, float)))
    return out


def build_object_index(client: PanoramaClient, locations: list[str]):
    """Index every object across all locations.

    Returns (objects, groups): lower-name -> object dict, and lower-name ->
    set of lower-case member names for group objects."""
    objects: dict[str, dict] = {}
    groups: dict[str, set] = {}
    for loc in locations:
        container = find_device_group(client, loc)
        if not isinstance(container, dict):
            continue
        for obj_type in OBJECT_TYPES + ["application-group"]:
            for entry in as_entry_list(dig(container, obj_type)):
                name = entry.get("name")
                if not name:
                    continue
                key = str(name).lower()
                objects.setdefault(key, {"name": str(name), "type": obj_type,
                                         "location": loc, "fields": entry})
                if obj_type.endswith("group"):
                    # XML API nests address-group members under <static>,
                    # service/app groups use a direct <member> list.
                    mem = entry.get("member")
                    if mem is None:
                        stat = entry.get("static")
                        mem = stat.get("member") if isinstance(stat, dict) \
                            else stat
                    groups.setdefault(key, set()).update(
                        m.lower() for m in member_values(mem))
    return objects, groups


def _object_matches(obj: dict, query_lower: str) -> bool:
    if query_lower in str(obj["name"]).lower():
        return True
    for key in IP_FIELDS:
        value = obj["fields"].get(key)
        if isinstance(value, str) and query_lower in value.lower():
            return True
    return False


def expand_group_hits(direct: set, groups: dict) -> tuple[set, dict]:
    """Grow the hit set with groups (transitively) containing a hit."""
    def members_under(gname: str, seen: set) -> set:
        res = set()
        for m in groups.get(gname, ()):
            if m in seen:
                continue
            res.add(m)
            if m in groups:
                res |= members_under(m, seen | {m})
        return res

    via: dict[str, list[str]] = {}
    current = set(direct)
    changed = True
    while changed:
        changed = False
        for gname, members in groups.items():
            if gname in current:
                continue
            if members & current:
                current.add(gname)
                via[gname] = sorted(members_under(gname, {gname}) & direct)
                changed = True
    return current, via


def where_used(client: PanoramaClient) -> None:
    locations = [SHARED_LABEL] + list_device_groups(client)
    objects, group_map = build_object_index(client, locations)
    if not objects:
        print("No objects found across shared/device groups - nothing to "
              "search.")
        return
    while True:
        query = input("Search object by name, IP address or group name "
                      "('q' to quit): ").strip().strip('"')
        if query.lower() in ("q", "quit"):
            return
        if not query:
            continue
        _where_used_once(client, locations, objects, group_map, query)


def _where_used_once(client: PanoramaClient, locations: list[str],
                     objects: dict, group_map: dict, query: str) -> None:
    ql = query.lower()
    direct = {name for name, obj in objects.items() if _object_matches(obj, ql)}
    hits, via = expand_group_hits(direct, group_map)
    if not direct:
        print(f"No address/service/application/tag object matches '{query}'.")
        return

    def group_rows() -> list[dict]:
        rows = []
        for gl in sorted(via):
            obj = objects.get(gl) or {"name": gl, "type": "?",
                                      "location": "?"}
            hit_members = sorted(m for m in group_map.get(gl, ())
                                 if m in direct or m in via)
            rows.append({"name": obj["name"], "type": obj["type"],
                         "location": obj["location"],
                         "contains": ", ".join(
                             objects[m]["name"] if m in objects else m
                             for m in hit_members),
                         "_key": gl})
        return rows

    grows = group_rows()

    records = []
    for loc in locations:
        container = find_device_group(client, loc)
        if not isinstance(container, dict):
            continue
        for rule, rb in iter_rules(container):
            # Keep indirect/group hits for where-used reporting, but track
            # *direct* field matches separately for the rule editor.  A rule
            # whose source contains only an address-group wrapping the sought
            # address must NEVER be selected for a rule update.
            matched: dict[str, list[str]] = {}
            direct_matched: dict[str, list[str]] = {}
            for field, value in rule.items():
                if field == "name":
                    continue
                for member in member_values(value):
                    ml = member.lower()
                    if ml in hits:
                        matched.setdefault(field, []).append(member)
                        if ml in direct:
                            direct_matched.setdefault(field, []).append(member)
            if not matched:
                continue
            rec = {"location": loc, "rulebase": rb,
                   "rule": str(rule.get("name", ""))}
            for field in RULE_DETAIL_KEYS:
                if field in rule:
                    rec[field] = fmt_value(rule[field])
            matched_objs, via_notes = [], []
            seen_members: set[str] = set()
            for field, ms in matched.items():
                for m in ms:
                    ml = m.lower()
                    if ml in seen_members:
                        continue
                    seen_members.add(ml)
                    matched_objs.append(m)
                    if ml in via:
                        via_notes.append(
                            f"{m} (contains {'/'.join(via[ml])})")
            rec["used_in"] = "; ".join(
                f"{f}: {'|'.join(ms)}" for f, ms in matched.items())
            rec["matched_objects"] = ", ".join(dict.fromkeys(matched_objs))
            rec["indirectly_via"] = "; ".join(dict.fromkeys(via_notes))
            rec["_fields"] = sorted(matched)  # includes indirect hits for reporting
            rec["_direct_fields"] = sorted(direct_matched)  # only edit these
            rec["_rule"] = rule
            rec["_rb"] = rb
            records.append(rec)

    print(f"\nQuery '{query}': {len(direct)} matching object(s), "
          f"{len(grows)} group reference(s), {len(records)} rule "
          f"reference(s).")
    print(f"\nMatched object(s) for '{query}':")
    for n in sorted(direct):
        obj = objects[n]
        summary = _obj_summary(obj["fields"])
        print(f"  {obj['name']} ({obj['type']}, {obj['location']})"
              + (f"  [{summary}]" if summary else ""))

    if grows:
        print(f"\nUsed in group(s) for '{query}':")
        for r in grows:
            print(f"  {r['name']} ({r['type']}, {r['location']})"
                  f"  [contains: {r['contains']}]")

    if records:
        print()
        text_lines: list[str] = []
        current_loc = None
        for rec in records:
            if rec["location"] != current_loc:
                current_loc = rec["location"]
                text_lines.append(f"=== {current_loc} ===")
            flag = (" [disabled]"
                    if str(rec.get("disabled", "")).lower() == "yes" else "")
            text_lines.append(f"  [{rec['rulebase']}] {rec['rule']}{flag}")
            text_lines.append(f"      used in: {rec['used_in']}")
            text_lines.append(f"      objects: {rec['matched_objects']}")
            if rec["indirectly_via"]:
                text_lines.append(f"      indirectly: {rec['indirectly_via']}")
            for key in ("from", "to", "source", "destination", "service",
                        "application", "action", "negated", "description"):
                if rec.get(key):
                    text_lines.append(f"      {key}: {rec[key]}")
        print("\n".join(text_lines))
    else:
        print("\nNo rules reference these objects, or any group that "
              "contains them.")

    fmt = input("\nExport results (c=csv, j=json, Enter=skip): ").strip().lower()
    if fmt in ("c", "csv", "j", "json"):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        obj_rows = [{"name": objects[n]["name"], "type": objects[n]["type"],
                     "location": objects[n]["location"],
                     "summary": _obj_summary(objects[n]["fields"])}
                    for n in sorted(direct)]
        export_records = [{k: v for k, v in rec.items()
                           if not k.startswith("_")} for rec in records]
        if fmt in ("c", "csv"):
            obj_path = safe_filename("where-used", f"{query}_objects_{ts}",
                                     "csv")
            with open(obj_path, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["name", "type", "location", "summary"])
                for row in obj_rows:
                    writer.writerow([row["name"], row["type"], row["location"],
                                     row["summary"]])
            print(f"Wrote {len(obj_rows)} object match(es) to {obj_path}")
            rule_path = safe_filename("where-used", f"{query}_rules_{ts}",
                                      "csv")
            keys = (["location", "rulebase", "rule", "used_in",
                     "matched_objects", "indirectly_via"] +
                    [k for k in RULE_DETAIL_KEYS
                     if k not in ("source", "destination", "service",
                                  "application")])
            keys = list(dict.fromkeys(keys))
            with open(rule_path, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(keys)
                for rec in export_records:
                    writer.writerow([rec.get(key, "") for key in keys])
            print(f"Wrote {len(export_records)} rule reference(s) to "
                  f"{rule_path}")
            if grows:
                grp_path = safe_filename("where-used",
                                         f"{query}_groups_{ts}", "csv")
                with open(grp_path, "w", encoding="utf-8",
                          newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["name", "type", "location", "contains"])
                    for row in grows:
                        writer.writerow([row["name"], row["type"],
                                         row["location"], row["contains"]])
                print(f"Wrote {len(grows)} group reference(s) to {grp_path}")
        else:
            path = safe_filename("where-used", f"{query}_{ts}", "json")
            payload = {"query": query,
                       "generated": datetime.now().isoformat(
                           timespec="seconds"),
                       "object_matches": obj_rows,
                       "group_references": [
                           {k: v for k, v in r.items()
                            if not k.startswith("_")} for r in grows],
                       "rule_references": export_records}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            print(f"Wrote {len(obj_rows)} object match(es) and "
                  f"{len(export_records)} rule reference(s) to {path}")

    ag_objs = [objects[k] for k in sorted(direct)
               if k in objects and objects[k].get("type") == "address-group"]
    has_direct_rules = any(any(f in ("source", "destination")
                               for f in rec.get("_direct_fields", ()))
                           for rec in records)
    if ag_objs or records:
        print("\nNext action:")
        if ag_objs:
            print("  [1] add/remove objects in the matched address group(s)")
        print("  [2] add address/address-group objects to rules "
              "DIRECTLY containing the matched object(s) "
              "(exclude matches via groups)" +
              ("" if has_direct_rules else
               "  (no direct source/destination references)"))
        allowed = {"1", "2"} if ag_objs else {"2"}
        sel = input("Select number (Enter=skip): ").strip()
        if sel and sel not in allowed:
            if sel == "1":
                print("  Option [1] is only available when the searched "
                      "object is an address group.")
        elif sel == "1":
            _edit_address_groups(client, objects, ag_objs, query)
        elif sel == "2":
            if records:
                _add_to_rules(client, objects, records, query)
            else:
                print("  Nothing to edit: no rule references.")


def _prompt_add_names(objects: dict, verb: str = "add") -> list[str] | None:
    """Prompt for address/address-group names (comma-separated, @file.txt or
    a .txt/.csv path). Returns the validated list of existing object names,
    or None when there is nothing to do."""
    raw = input(f"Address or address-group object name(s) to {verb} "
                "(comma-separated, or '@file.txt' one per line): ") \
        .strip().strip('"')
    if not raw:
        return None
    if raw.startswith("@") or raw.lower().endswith((".txt", ".csv")):
        try:
            names = _names_from_convert_file(
                raw.lstrip("@").strip().strip('"').strip("'"))
        except OSError as exc:
            print(f"  Could not read file: {exc}")
            return None
    else:
        names = [t for t in raw.replace(",", " ").split() if t]
    adds, missing, seen = [], [], set()
    for name in names:
        obj = objects.get(str(name).lower())
        if not isinstance(obj, dict) or obj["type"] not in (
                "address", "address-group"):
            missing.append(name)
            continue
        if obj["name"].lower() not in seen:
            seen.add(obj["name"].lower())
            adds.append(obj["name"])
    if missing:
        print(f"  WARNING  {len(missing)} name(s) are not existing address/"
              "address-group objects and will be skipped:")
        for name in missing[:20]:
            print(f"    ? {name}")
        if len(missing) > 20:
            print(f"    ... and {len(missing) - 20} more")
    if not adds:
        print(f"  No valid address/address-group objects to {verb}.")
        return None
    return adds


def _group_members(entry: dict) -> list[str]:
    """Member list of an address group entry in either XML or REST shape."""
    return member_values(entry.get("static", entry))


def _edit_address_groups(client: PanoramaClient, objects: dict,
                         ag_objs: list, query: str = "") -> None:
    """Add or remove address/address-group members in the matched address
    groups (group writes work through the REST objects API)."""
    print("\nAddress group edit:")
    print("  [1] add objects to the address group(s)")
    print("  [2] remove objects from the address group(s)")
    mode_sel = input("Select number (Enter=cancel): ").strip()
    if mode_sel not in ("1", "2"):
        print("Group edit cancelled.")
        return
    add_mode = mode_sel == "1"
    verb = "add to" if add_mode else "remove from"
    names = _prompt_add_names(objects, verb)
    if names is None:
        return

    plan = []
    for obj in ag_objs:
        row = {"name": obj["name"], "type": "address-group",
               "location": obj.get("location", SHARED_LABEL)}
        loc = row["location"]
        name = row["name"]
        base = None
        try:
            entry = client.rest_get_entry("address-group", loc, name)
            if isinstance(entry, dict):
                base = dict(entry)
        except RuntimeError as exc:
            print(f"  NOTE  could not read '{name}' via REST: {exc}",
                  file=sys.stderr)
        if base is None:
            base = {k: v for k, v in obj.get("fields", {}).items()
                    if k != "name"}
        members = _group_members(base)
        if not members:
            print(f"  NOTE  '{name}': no static members (dynamic group?); "
                  "skipped")
            continue
        have = {m.lower() for m in members}
        if add_mode:
            changed = [a for a in names if a.lower() not in have]
            new_members = list(members) + changed
        else:
            changed = [a for a in names if a.lower() in have]
            drop = {a.lower() for a in changed}
            new_members = [m for m in members if m.lower() not in drop]
            not_in = [a for a in names if a.lower() not in have]
            if not_in:
                print(f"  NOTE  '{name}': not member(s), cannot remove: "
                      f"{', '.join(not_in[:10])}"
                      + (f" ... (+{len(not_in) - 10} more)"
                         if len(not_in) > 10 else ""))
        if not changed:
            continue
        new_base = {**base, "static": {"member": new_members}}
        orig_base = {**base, "static": {"member": list(members)}}
        plan.append((row, loc, list(members), orig_base, new_base, changed))
    if not plan:
        print(f"  Nothing to {verb}: no member changes needed in these "
              "groups.")
        return

    print(f"\nWill update {len(plan)} address group(s) ({verb}):")
    for row, _loc, members, _o, new_base, changed in plan:
        print(f"  {row['name']} ({row['location']})")
        print(f"    before : members: {' | '.join(members)}")
        print(f"    after  : members: "
              f"{' | '.join(member_values(new_base['static']))}")
        print(f"    {verb}: {', '.join(changed)}")
    if input("Type 'yes' to update these groups: ").strip().lower() != "yes":
        print("Group update cancelled.")
        return

    ok, failed, statuses = 0, [], []
    for row, loc, _m, _o, new_base, _t in plan:
        try:
            client.rest_write("address-group", loc, row["name"], new_base,
                              create=False)
            statuses.append(("UPDATED", ""))
            ok += 1
        except RuntimeError as exc:
            statuses.append(("FAILED", str(exc)))
            failed.append((row["name"], str(exc)))
    print(f"\nUpdated {ok}/{len(plan)} group(s).")
    for name, err in failed[:5]:
        print(f"  FAILED {name}: {err}", file=sys.stderr)

    diff_entries = [(row, loc, members, new_base["static"], changed)
                    for row, loc, members, _o, new_base, changed in plan]
    diff_path, diff_text = _group_edit_diff(query, names, diff_entries,
                                            statuses, "applied",
                                            "group-edit", mode=verb)
    _move_print_diff(diff_text, diff_path)
    if ok:
        actions = ["commit changes (optional description)",
                   "revert these group changes", "leave pending"]
        action = choose(actions, "Post-update")
        if action == actions[0]:
            commit_changes(client)
        elif action == actions[1]:
            _revert_group_changes(client, query, plan, statuses)


def _group_edit_diff(query: str, names: list[str], entries: list[tuple],
                     statuses: list[tuple[str, str]], phase: str,
                     suffix: str, mode: str = "add") -> tuple[str, str]:
    """entries: (group_row, location, before_members, after_static, changed)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = safe_filename("where-used", f"{query}_{suffix}_{ts}", "txt")
    removing = mode.startswith("remove")
    label, verb, sign = ("Removing", "removed", "-") if removing \
        else ("Adding", "added", "+")
    header = (f"Panorama address-group member diff\n"
              f"Query       : {query}\n"
              f"{label.ljust(12)}: {', '.join(names) if names else '-'}\n"
              f"Phase       : {phase}\n"
              f"Generated   : "
              f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"(group static members before -> after)\n"
              + "=" * 60 + "\n\n")
    body = []
    for (row, loc, before, after_static, changed), (status, err) in \
            zip(entries, statuses):
        body.append(f"[{status}] [{row['type']}] {row['name']} ({loc})")
        body.append(f"  - before: members: {' | '.join(before)}")
        body.append(f"  + after : members: "
                    f"{' | '.join(member_values(after_static))}")
        if changed:
            body.append(f"  {sign} {verb} : members: {', '.join(changed)}")
        if err:
            body.append(f"  ! {err}")
        body.append("")
    text = header + "\n".join(body)
    Path(path).write_text(text, encoding="utf-8")
    return path, text


def _revert_group_changes(client: PanoramaClient, query: str, plan: list,
                          statuses: list) -> None:
    """Restore the original static members of the groups that were updated."""
    entries = [(row, loc, member_values(new_base["static"]), orig_base, t)
               for (row, loc, _m, orig_base, new_base, t), (st, _e)
               in zip(plan, statuses) if st == "UPDATED"]
    if not entries:
        print("Nothing to revert (no group changes applied).")
        return
    ok, rstatus = 0, []
    for row, loc, _before, orig_base, _t in entries:
        try:
            client.rest_write("address-group", loc, row["name"], orig_base,
                              create=False)
            rstatus.append(("REVERTED", ""))
            ok += 1
        except RuntimeError as exc:
            rstatus.append(("FAILED", str(exc)))
    print(f"\nReverted {ok}/{len(entries)} group(s) to their original "
          "members.")
    diff_entries = [(row, loc, before, orig["static"], t)
                    for (row, loc, before, orig, t) in entries]
    mode = "add" if any(len(_group_members(orig)) > len(after)
                        for (_r, _l, after, orig, _t) in entries) \
        else "remove"
    names = list(dict.fromkeys(n for (_r, _l, _a, _o, t) in entries
                               for n in t))
    diff_path, diff_text = _group_edit_diff(query, names, diff_entries,
                                            rstatus, "reverted",
                                            "group-revert", mode=mode)
    _move_print_diff(diff_text, diff_path)
    if ok:
        actions = ["commit changes (optional description)", "leave pending"]
        if choose(actions, "Post-revert") == actions[0]:
            commit_changes(client)


def _append_desc(cur: str, suffix: str) -> str:
    """Append suffix to a rule description; idempotent when already present."""
    cur = cur if isinstance(cur, str) else str(cur or "")
    if cur.strip() and cur.rstrip() == suffix.rstrip():
        return cur
    if cur.rstrip().endswith(suffix.rstrip()):
        return cur
    return f"{cur} {suffix}" if cur.rstrip() else suffix


def _rule_desc_live(client: PanoramaClient, rec: dict) -> str:
    """Current description of a rule: prefer the live REST candidate value,
    falling back to the value seen during the where-used scan."""
    try:
        live = client.rest_rule_get_entry(rec["location"], rec["_rb"],
                                          rec["rule"])
        desc = live.get("description", "")
    except RuntimeError:
        desc = rec["_rule"].get("description", "")
    return desc if isinstance(desc, str) else str(desc or "")


def _add_to_rules(client: PanoramaClient, objects: dict, records: list,
                  query: str = "") -> None:
    """Append members only to fields DIRECTLY containing the searched object.

    Indirect where-used results (a rule referencing a group that contains a
    matched object) are visible in the report but must never be modified.
    _direct_fields comes from _where_used_once and is deliberately required;
    falling back to _fields would silently re-enable indirect edits.
    """
    if not any(any(f in ("source", "destination")
                   for f in rec.get("_direct_fields", ()))
               for rec in records):
        print("No rules directly reference the searched object in their "
              "source or destination fields. Indirect matches through "
              "groups are excluded from rule updates.")
        return

    adds = _prompt_add_names(objects)
    if adds is None:
        return

    plan, skipped, seen_plan = [], [], set()
    for rec in records:
        rule, rb = rec["_rule"], rec["_rb"]
        hit_fields = [f for f in rec.get("_direct_fields", ())
                      if f in ("source", "destination")]
        if not hit_fields:
            if any(f in ("source", "destination")
                   for f in rec.get("_fields", ())):
                note = "indirect match through an object group only"
            else:
                note = "direct match not in source/destination"
            skipped.append(f"[{rb}] {rec['location']}/{rec['rule']}: "
                           f"{note}; rule unchanged")
            continue
        for fld in hit_fields:
            key = (rec["location"], rb, rec["rule"], fld)
            if key in seen_plan:
                continue
            seen_plan.add(key)
            cur = rule.get(fld)
            members = member_values(cur)
            if not members:
                skipped.append(f"[{rb}] {rec['location']}/{rec['rule']} "
                               f"({fld}): no member list (e.g. 'any')")
                continue
            have = {m.lower() for m in members}
            to_add = [a for a in adds if a.lower() not in have]
            if not to_add:
                continue
            base = dict(cur) if isinstance(cur, dict) else {}
            base.pop("member", None)
            orig_val = {**base, "member": list(members)}
            plan.append((rec, fld, members, orig_val,
                         {**base, "member": list(members) + to_add}, to_add))
    if skipped:
        print("\n  Skipped:")
        for note in dict.fromkeys(skipped):
            print(f"    {note}")
    if not plan:
        print("  Nothing to change: the members are already present in all "
              "these rules.")
        return

    print(f"\nWill update {len(plan)} rule field(s):")

    rule_keys = dict.fromkeys(
        (rec["location"], rec["_rb"], rec["rule"])
        for rec, _f, _m, _o, _n, _t in plan)
    suffix = input("\nOptional text to append to these rules' descriptions "
                   "(Enter=skip): ").strip()
    desc_map: dict[tuple, tuple[str, str]] = {}
    for key in rule_keys:
        rec = next(r for r, _f, _m, _o, _n, _t in plan
                   if (r["location"], r["_rb"], r["rule"]) == key)
        orig = _rule_desc_live(client, rec)
        desc_map[key] = (orig, _append_desc(orig, suffix) if suffix else orig)

    seen_desc = set()
    for rec, fld, members, _orig, new_val, _to_add in plan:
        print(f"  [{rec['_rb']}] {rec['location']}/{rec['rule']}")
        print(f"    before: {fld}: {' | '.join(members)}")
        print(f"    after : {fld}: {' | '.join(member_values(new_val))}")
        key = (rec["location"], rec["_rb"], rec["rule"])
        if key not in seen_desc:
            seen_desc.add(key)
            orig, new = desc_map[key]
            print(f"    desc  : {orig!r}")
            if new != orig:
                print(f"    desc+ : {new!r}")
    if input("Type 'yes' to update these rules: ").strip().lower() != "yes":
        print("Rule update cancelled.")
        return

    ok, failed, statuses = 0, [], []
    for rec, fld, _members, _orig, new_val, _to_add in plan:
        key = (rec["location"], rec["_rb"], rec["rule"])
        orig_d, new_d = desc_map[key]
        good, err = _write_rule_field(
            client, rec, fld, new_val,
            desc_new=(new_d if new_d != orig_d else None))
        if good:
            statuses.append(("UPDATED", ""))
            ok += 1
        else:
            statuses.append(("FAILED", err))
            failed.append((f"{rec['location']}/{rec['rule']} ({fld})", err))
    print(f"\nUpdated {ok}/{len(plan)} rule field(s).")
    for name, err in failed[:5]:
        print(f"  FAILED {name}: {err}", file=sys.stderr)
    if failed:
        print("NOTE: API rule writes failed; generated reviewable, "
              "append-only Panorama CLI commands.", file=sys.stderr)
        cli_path, cli_text = _rule_cli_script(query, plan, statuses, desc_map)
        Path(cli_path).write_text(cli_text, encoding="utf-8")
        print(f"CLI fallback script saved to {cli_path}")

    diff_entries = [(rec, fld, members, new_val, to_add)
                    for rec, fld, members, _o, new_val, to_add in plan]
    diff_path, diff_text = _rule_edit_diff(query, adds, diff_entries,
                                           statuses, "applied", "rule-edit",
                                           desc_map=desc_map)
    _move_print_diff(diff_text, diff_path)
    if ok:
        actions = ["commit changes (optional description)",
                   "revert these rule changes", "leave pending"]
        action = choose(actions, "Post-update")
        if action == actions[0]:
            commit_changes(client)
        elif action == actions[1]:
            _revert_rule_changes(client, query, plan, statuses, desc_map)


def _rule_cli_script(query: str, plan: list, statuses: list[tuple[str, str]],
                     desc_map: dict | None = None) -> tuple[str, str]:
    """Append-only Panorama CLI fallback for failed API rule edits."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = safe_filename("where-used", f"{query}_rule-edit-cli_{ts}", "txt")
    lines = [f"! Panorama candidate configuration commands for query {query!r}",
             "! Review in Panorama: 'configure', paste set commands,",
             "! inspect the diff, then commit/push separately.",
             "! No 'clear' commands: preserve all existing policy members.", ""]
    done_desc = set()
    for (rec, fld, _m, _o, _new_val, to_add), (st, _e) in zip(plan, statuses):
        if st == "UPDATED":
            continue
        rb = str(rec["_rb"]).replace("/", " ")
        tgt = ("shared" if rec["location"] == SHARED_LABEL
               else f"device-group {json.dumps(rec['location'])}")
        name = json.dumps(rec["rule"])
        for member in to_add:
            lines.append(f"set {tgt} {rb} rules {name} {fld} member "
                         f"{json.dumps(member)}")
        key = (rec["location"], rec["_rb"], rec["rule"])
        if desc_map and key in desc_map and key not in done_desc:
            orig, new = desc_map[key]
            if new != orig:
                done_desc.add(key)
                lines.append(f"set {tgt} {rb} rules {name} description "
                             f"{json.dumps(new)}")
        lines.append("")
    return path, "\n".join(lines)

def _write_rule_field(client: PanoramaClient, rec: dict, fld: str,
                      new_val, expected_current: list[str] | None = None,
                      desc_new: str | None = None
                      ) -> tuple[bool, str]:
    """Append new members without clobbering concurrent candidate edits.

    For a revert, expected_current is the precise member list we wrote.
    If the rule changed since that write, refuse to overwrite other edits.
    desc_new (when given) replaces the rule description in the same update.
    """
    loc, rb, rname = rec["location"], rec["_rb"], rec["rule"]
    baseline = member_values(rec["_rule"].get(fld))
    wanted = member_values(new_val)
    if not wanted or not baseline:
        return False, "rule has no explicit address member list"
    extra = [m for m in wanted if m.lower() not in {b.lower() for b in baseline}]
    errors: list[str] = []

    def merge(current: list[str]) -> list[str]:
        if not current or any(m.lower() == "any" for m in current):
            raise RuntimeError("rule uses 'any' or has no member list; refusing modification")
        if expected_current is not None:
            if current != expected_current:
                raise RuntimeError("candidate rule changed since update; refusing unsafe revert")
            return wanted
        present = {m.lower() for m in current}
        if not all(m.lower() in present for m in baseline):
            raise RuntimeError("candidate rule changed since search; search again before editing")
        return current + [m for m in extra if m.lower() not in present]

    # Preferred: fetch actual candidate rule, update only the target field
    # and description, and PUT the fully populated, current policy.
    try:
        live = client.rest_rule_get_entry(loc, rb, rname)
        current = member_values(live.get(fld))
        updated = merge(current)
        live_desc = live.get("description", "")
        live_desc = live_desc if isinstance(live_desc, str) else str(live_desc or "")
        desc_changed = desc_new is not None and live_desc != desc_new
        if updated == current and not desc_changed:
            return True, ""
        body = dict(live)
        body[fld] = {"member": updated}
        if desc_new is not None:
            body["description"] = desc_new
        client.rest_rule_update(loc, rb, rname, body)
        # Do not try another write route following an accepted PUT.
        return True, ""
    except RuntimeError as exc:
        errors.append(f"REST policy: {exc}")
        # A stale state is a hard stop, not a trigger for another write method.
        if any(marker in str(exc) for marker in (
                "candidate rule changed", "refusing blind update",
                "rule uses 'any'", "no member list")):
            return False, "; ".join(errors)

    # Fallback: read the candidate field via the XML API, then edit *only*
    # the field (and description if requested).
    try:
        paths = rule_field_paths(loc, rb, rname, fld)
    except ValueError as exc:
        errors.append(f"XML: {exc}")
        return False, "; ".join(errors)
    for path in paths:
        try:
            current = client.config_rule_field_read(path, fld)
            if current is None:
                continue
            updated = merge(current)
            field_changed = updated != current
            if field_changed:
                client.config_rule_field_edit(path, fld, updated)
            desc_note = ""
            if desc_new is not None:
                try:
                    client.config_rule_desc_edit(path.rsplit("/", 1)[0],
                                                 desc_new)
                except RuntimeError as exc:
                    desc_note = f" (description edit failed: {exc})"
            if field_changed or not desc_note:
                return True, desc_note
            errors.append(f"XML {path}: field unchanged and description edit failed")
        except RuntimeError as exc:
            errors.append(f"XML {path}: {exc}")
            if "candidate rule changed" in str(exc) or "refusing" in str(exc):
                return False, "; ".join(errors)
    return False, "; ".join(errors) or "Could not read candidate policy rule"

def _rule_edit_diff(query: str, adds: list[str], entries: list[tuple],
                    statuses: list[tuple[str, str]], phase: str,
                    suffix: str, desc_map: dict | None = None
                    ) -> tuple[str, str]:
    """entries: (rec, field, before_members, after_value, added_list)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = safe_filename("where-used", f"{query}_{suffix}_{ts}", "txt")
    header = (f"Panorama rule member diff\n"
              f"Query       : {query}\n"
              f"Adding      : {', '.join(adds) if adds else '-'}\n"
              f"Phase       : {phase}\n"
              f"Generated   : "
              f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"(source/destination member lists before -> after)\n"
              + "=" * 60 + "\n\n")
    body = []
    seen_desc = set()
    for (rec, fld, before, after_val, to_add), (status, err) in \
            zip(entries, statuses):
        body.append(f"[{status}] [{rec['_rb']}] "
                    f"{rec['location']}/{rec['rule']}")
        body.append(f"  - before: {fld}: {' | '.join(before)}")
        body.append(f"  + after : {fld}: "
                    f"{' | '.join(member_values(after_val))}")
        if to_add:
            body.append(f"  + added : {fld}: {', '.join(to_add)}")
        key = (rec["location"], rec["_rb"], rec["rule"])
        if desc_map and key in desc_map and key not in seen_desc:
            seen_desc.add(key)
            orig, new = desc_map[key]
            if new != orig:
                body.append(f"  - desc  : {orig!r}")
                body.append(f"  + desc  : {new!r}")
        if err:
            body.append(f"  ! {err}")
        body.append("")
    text = header + "\n".join(body)
    Path(path).write_text(text, encoding="utf-8")
    return path, text


def _revert_rule_changes(client: PanoramaClient, query: str,
                         plan: list, statuses: list,
                         desc_map: dict | None = None) -> None:
    """Restore original member lists (and descriptions) for the rule fields
    that were updated."""
    entries = [(rec, fld, member_values(new_val), orig_val, [])
               for (rec, fld, _m, orig_val, new_val, _t), (st, _e)
               in zip(plan, statuses) if st == "UPDATED"]
    if not entries:
        print("Nothing to revert (no rule changes applied).")
        return
    revert_map = {k: (n, o) for k, (o, n) in (desc_map or {}).items()
                  if n != o}
    ok, rstatus = 0, []
    for rec, fld, _before, orig_val, _add in entries:
        key = (rec["location"], rec["_rb"], rec["rule"])
        orig_d = revert_map.get(key, (None, None))[1]
        good, err = _write_rule_field(client, rec, fld, orig_val,
                                      expected_current=_before,
                                      desc_new=orig_d)
        rstatus.append(("REVERTED", "") if good else ("FAILED", err))
        ok += 1 if good else 0
    print(f"\nReverted {ok}/{len(entries)} rule field(s) to their original "
          "members.")
    diff_path, diff_text = _rule_edit_diff(query, [], entries, rstatus,
                                           "reverted", "rule-revert",
                                           desc_map=revert_map)
    _move_print_diff(diff_text, diff_path)
    if ok:
        actions = ["commit changes (optional description)", "leave pending"]
        if choose(actions, "Post-revert") == actions[0]:
            commit_changes(client)


def _obj_summary(fields: dict) -> str:
    parts: list[str] = []
    for key in IP_FIELDS:
        value = fields.get(key)
        if isinstance(value, str):
            parts.append(f"{key}: {value}")
    mem = fields.get("member") or fields.get("members") or \
        fields.get("static")
    members = member_values(mem) if mem is not None else []
    if members:
        parts.append("members: " + ", ".join(members))
    return "; ".join(parts)


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
        ("modern REST v11: security rules probe (rule routes?)",
         {"location": "shared", "rulebase": "rulebase", "limit": "1"},
         "/restapi/v11.0/Rules/Security"),
        ("modern REST v11: SecurityRules resource probe",
         {"location": "shared", "limit": "1"},
         "/restapi/v11.0/SecurityRules"),
        ("modern REST v11: resource index (GET /restapi/v11.0)",
         {}, "/restapi/v11.0"),
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
def _shared_exists(client: PanoramaClient, obj_type: str,
                   name: str) -> bool:
    try:
        if client.rest_get_entry(obj_type, SHARED_LABEL, name) is not None:
            return True
    except RuntimeError:
        pass
    shared = find_device_group(client, SHARED_LABEL)
    entries = as_entry_list(dig(shared, obj_type)) if shared else []
    return any(e.get("name") == name for e in entries)


def _move_diff_write(device_group: str, obj_type: str, phase: str,
                     changes: list[tuple], mode: str = "move") -> tuple[str, str]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = safe_filename(device_group, f"diff_{phase}_{ts}", "txt")
    verb = "Copy" if mode == "copy" else "Move"
    after = (f"(before = state in '{device_group}'; after = shared copy, "
             f"source kept)" if mode == "copy" else
             f"(before = state in '{device_group}'; after = state in "
             f"'{SHARED_LABEL}')")
    header = (f"Panorama {verb.lower()} diff\n"
              f"{verb:<13}: {device_group} -> {SHARED_LABEL}\n"
              f"Object type : {obj_type}\n"
              f"Phase       : {phase}\n"
              f"Generated   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"{after}\n" + "=" * 60 + "\n\n")
    body: list[str] = []
    for ct, name, status, lines, err in changes:
        body.append(f"[{status}] {ct}/{name}")
        if err:
            body.append(f"  ! {err}")
        body.extend(lines or [])
        body.append("")
    text = header + "\n".join(body)
    Path(path).write_text(text, encoding="utf-8")
    return path, text


def _move_print_diff(text: str, diff_path: str) -> None:
    lines = text.splitlines()
    print(f"\n--- diff (full copy: {diff_path}) ---")
    for line in lines[:60]:
        print(line)
    if len(lines) > 60:
        print(f"[... {len(lines) - 60} more lines in {diff_path}]")
    print("--- end diff ---\n")


def _readback(client: PanoramaClient, obj_type: str, location: str,
              name: str) -> dict | None:
    try:
        return client.rest_get_entry(obj_type, location, name)
    except RuntimeError:
        return None


def revert_move(client: PanoramaClient, device_group: str, obj_type: str,
                applied: list[tuple], mode: str = "move") -> None:
    """Undo a copy/move: for move, restore objects in the device group and
    delete the shared copies; for copy, only delete the shared copies."""
    if not applied:
        print("Nothing to revert.")
        return
    what = "move(s)" if mode == "move" else "copy(ies)"
    head = (f"Reverting {len(applied)} {what}: " +
            (f"objects restored to '{device_group}', shared copies created "
             f"by the move deleted"
             if mode == "move" else
             f"shared copies created by the copy deleted "
             f"(source '{device_group}' untouched)") + ":")
    print(head)
    for name, _fields, _partial in applied[:10]:
        print(f"  - {name}")
    if input("Type 'yes' to revert: ").strip().lower() != "yes":
        print("Revert skipped.")
        return
    changes = []
    for name, fields, _partial in applied:
        ok1, err1 = True, ""
        if mode == "move":
            ok1, err1 = write_object(client, device_group, obj_type, name,
                                     fields, existed=True)
        ok2, err2 = delete_object(client, SHARED_LABEL, obj_type, name)
        if ok1 and ok2:
            status, err = ("RESTORED" if mode == "move" else
                           "COPY REMOVED"), ""
        elif ok2:
            status, err = "FAILED", (f"shared copy deleted but restore into "
                                     f"'{device_group}' failed: {err1}")
        else:
            status, err = "PARTIAL", (
                f"shared copy still present: {err2}" if mode == "copy" else
                f"restored into '{device_group}' but shared copy still "
                f"present: {err2}")
        if mode == "copy":
            lines = [f"  - copy in '{SHARED_LABEL}' removed (kept in "
                     f"'{device_group}')"]
            after = _readback(client, obj_type, SHARED_LABEL, name)
            if after is None:
                lines.append("  (shared copy gone, source untouched)")
            else:
                lines.append("  ! shared copy still readable in shared")
        else:
            lines = [f"  ~ location: '{SHARED_LABEL}' -> '{device_group}'"]
            after = _readback(client, obj_type, device_group, name)
            if after is None:
                lines.append("  ! device-group copy could not be read back "
                             "for verification")
            elif not diff_entry_lines({"name": name, **fields}, after):
                lines.append("  (all properties identical)")
            else:
                lines.extend(diff_entry_lines({"name": name, **fields}, after))
        changes.append((obj_type, name, status, lines, err))
    client._running_config = None
    good = "RESTORED" if mode == "move" else "COPY REMOVED"
    restored = sum(1 for c in changes if c[2] == good)
    print(f"Reverted {restored}/{len(applied)} object(s).")
    diff_path, text = _move_diff_write(device_group, obj_type,
                                       f"{mode}-revert", changes, mode)
    _move_print_diff(text, diff_path)
    actions = ["commit changes", "leave pending"]
    action = choose(actions, "Post-revert")
    if action == actions[0]:
        commit_changes(client)


def _write_converted(groups: dict[str, list[dict]], out_path: str,
                     to_json: bool) -> int:
    """Write grouped object entries to JSON ({type: [entry,...]}) or to a
    single CSV with a leading type column. Returns the entry count."""
    total = sum(len(entries) for entries in groups.values())
    if to_json:
        if list(groups) == [""]:
            payload = groups[""]
        else:
            payload = {k: groups[k] for k in groups}
        Path(out_path).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        return total
    keys: list[str] = ["type", "name"]
    for entries in groups.values():
        for entry in entries:
            for key in entry:
                if key not in keys:
                    keys.append(key)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(keys)
        for obj_type, entries in groups.items():
            for entry in entries:
                row = [obj_type] + [fmt_value(entry.get(key))
                                    for key in keys[1:]]
                writer.writerow(row)
    return total


def _convert_file(src: str, dst: str) -> None:
    groups = read_import_file(src)
    if not any(groups.values()):
        print("  No object entries found in the file.")
        return
    to_json = dst.lower().endswith(".json")
    total = _write_converted(groups, dst, to_json)
    types = [k for k in groups if k]
    print(f"Converted {total} object(s)"
          + (f" ({', '.join(sorted(types))})" if types else "")
          + f" -> {dst}")


def convert_mode() -> None:
    """Standalone CSV <-> JSON converter for object import/export files."""
    actions = ["convert CSV to JSON", "convert JSON to CSV",
               "back to main menu"]
    action = choose(actions, "Conversion")
    if action == actions[2]:
        return
    want_ext = ".json" if action == actions[0] else ".csv"   # output
    in_ext = ".csv" if action == actions[0] else ".json"     # input
    src = input(f"Input file ({in_ext}): ").strip().strip('"').strip("'")
    if not src:
        return
    if not Path(src).is_file():
        print(f"  File not found: {src}")
        return
    if not src.lower().endswith((".csv", ".json")):
        print("  Input must be a .csv or .json file.")
        return
    if src.lower().endswith(want_ext):
        print(f"  NOTE: input is already {want_ext}; converting anyway "
              "by content.")
    default = str(Path(src).with_suffix(want_ext))
    dst = input(f"Output file (Enter for '{default}'): ").strip() \
        .strip('"').strip("'") or default
    if not dst.lower().endswith((".csv", ".json")):
        dst += want_ext
    if Path(dst).exists() and input(
            f"'{dst}' exists - overwrite? y/Enter=cancel: ").strip().lower() \
            != "y":
        print("Conversion cancelled.")
        return
    try:
        _convert_file(src, dst)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"  Conversion failed: {exc}", file=sys.stderr)


def _snap_name(raw: str) -> str:
    return re.sub(r"[^\w.-]+", "_", raw).strip("_") or "snapshot"


def _export_config_snapshot_locally(client: PanoramaClient, raw: str) -> None:
    """Fetch the committed config and save a dated local snapshot
    (XML + JSON)."""
    name = _snap_name(raw) if raw else "running-config"
    print("Fetching committed configuration ...")
    try:
        xml_text = client.config_show_raw()
    except (RuntimeError, requests.RequestException) as exc:
        print(f"  Export failed: {exc}", file=sys.stderr)
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    xml_path = safe_filename("snapshot", f"{name}_{ts}", "xml")
    json_path = safe_filename("snapshot", f"{name}_{ts}", "json")
    Path(xml_path).write_text(xml_text, encoding="utf-8")
    try:
        parsed = client.running_config()
        Path(json_path).write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"Saved config snapshot '{name}' ({len(xml_text):,} bytes) to:\n"
              f"  {xml_path}\n  {json_path}")
    except (OSError, RuntimeError) as exc:
        print(f"  XML saved ({xml_path}); JSON failed: {exc}",
              file=sys.stderr)


def snapshot_mode(client: PanoramaClient) -> None:
    """Save/list/export Panorama configuration snapshots (dated names)."""
    actions = ["save configuration snapshot on Panorama",
               "export configuration locally (XML + JSON)",
               "list snapshots",
               "back to main menu"]
    action = choose(actions, "Snapshots")
    if action == actions[3]:
        return

    if action == actions[0]:
        raw = input("Snapshot name: ").strip().strip('"').strip("'")
        name = f"{_snap_name(raw)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        comment = f"saved by panorama_cli {datetime.now().isoformat(timespec='seconds')}"
        print(f"Saving snapshot '{name}' on Panorama ...")
        try:
            via = client.create_snapshot(name, comment)
        except RuntimeError as exc:
            print(f"  {exc}", file=sys.stderr)
            if client._snapshot_api_unsupported():
                print("  Panorama-side snapshots are unavailable through "
                      "this API.", file=sys.stderr)
                if input("  Save the snapshot locally instead (dated "
                         "XML + JSON export)? y/Enter=cancel: "
                         ).strip().lower() == "y":
                    _export_config_snapshot_locally(client, raw)
            return
        print(f"Snapshot '{name}' saved via {via}.")
        return

    if action == actions[1]:
        raw = input("Name for the local export (Enter = running-config): ") \
            .strip().strip('"').strip("'")
        _export_config_snapshot_locally(client, raw)
        return

    if action == actions[2]:
        print("Listing configuration snapshots ...")
        try:
            entries = client.list_snapshots()
        except RuntimeError as exc:
            print(f"  {exc}", file=sys.stderr)
            return
        if not entries:
            print("  No snapshots found (or this Panorama does not expose "
                  "them through the API).")
            return
        for entry in entries:
            name = entry.get("@name") or entry.get("name") or "?"
            desc = entry.get("description") or ""
            print(f"  {name}" + (f"  [{desc}]" if desc else ""))
        return


def _names_from_convert_file(path: str) -> list[str]:
    names: list[str] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.extend(t for t in line.replace(",", " ").split() if t)
    return names


def _entries_from_convert_file(path: str, entries: list[dict],
                               obj_type: str,
                               device_group: str) -> list[dict]:
    try:
        names = _names_from_convert_file(path)
    except OSError as exc:
        print(f"  Could not read '{path}': {exc}")
        return []
    print(f"  Loaded {len(names)} object name(s) from '{path}'.")
    by_exact = {e["name"]: e for e in entries}
    by_lower = {e["name"].lower(): e for e in entries}
    selected, missing, seen = [], [], set()
    for name in names:
        entry = by_exact.get(name) or by_lower.get(name.lower())
        if entry is None:
            missing.append(name)
        elif entry["name"] not in seen:
            seen.add(entry["name"])
            selected.append(entry)
    if missing:
        print(f"  MISSING  {len(missing)} name(s) not found as '{obj_type}' "
              f"objects in '{device_group}':")
        for name in missing[:20]:
            print(f"    ? {name}")
        if len(missing) > 20:
            print(f"    ... and {len(missing) - 20} more")
    if not selected:
        print("  No matching objects to convert.")
    return selected


def _copy_move_pass(client: PanoramaClient, device_group: str,
                    obj_type: str) -> None:
    if device_group == SHARED_LABEL:
        print("Nothing to do: these objects are already shared.")
        return
    modes = ["copy (keep object in device group)",
             "move (delete device-group object after copying)"]
    mode = choose(modes, "Mode").split()[0].lower()
    entries = get_device_group_objects(client, device_group, obj_type)
    if not entries:
        print(f"No '{obj_type}' objects found in '{device_group}'.")
        return
    print(f"\n'{obj_type}' objects in '{device_group}':")
    for i, entry in enumerate(entries, start=1):
        print(f"  [{i}] {entry['name']}")
    verb = "Copy" if mode == "copy" else "Move"
    raw = input(f"{verb} numbers to shared (comma-separated, 'a' = all, "
                "or '@file.txt' with one name per line): ").strip()
    if raw.lower() in ("a", "all"):
        selected = list(entries)
    elif raw.startswith("@") or raw.lower().endswith((".txt", ".csv")):
        selected = _entries_from_convert_file(
            raw.lstrip("@").strip().strip('"').strip("'"), entries,
            obj_type, device_group)
    else:
        nums = []
        for part in raw.replace(" ", ",") .split(","):
            if part.isdigit() and 1 <= int(part) <= len(entries):
                nums.append(int(part))
        selected = [entries[i - 1] for i in dict.fromkeys(nums)]
    if not selected:
        print("Nothing selected.")
        return
    print(f"\n{verb.lower()} into '{SHARED_LABEL}' (name and all properties "
          "kept):")
    moves, skips = [], []
    for entry in selected:
        name = entry["name"]
        if _shared_exists(client, obj_type, name):
            skips.append(name)
            print(f"  SKIP  {name} (already exists in shared - will not "
                  "overwrite)")
        else:
            moves.append(entry)
            print(f"  {verb.upper()}  {name}")
    if not moves:
        print(f"Nothing to {verb.lower()}: all selected names already exist "
              "in shared.")
        return

    preview = []
    for entry in moves:
        fields = {k: v for k, v in entry.items() if k != "name"}
        preview.append((obj_type, entry["name"], verb.upper(),
                        _move_diff_lines(device_group, fields, fields, mode),
                        ""))
    for name in skips:
        preview.append((obj_type, name, "SKIP", [],
                        "already exists in shared (not overwritten)"))
    diff_path, text = _move_diff_write(device_group, obj_type,
                                       f"{mode}-preview", preview, mode)
    _move_print_diff(text, diff_path)

    if input(f"Type 'yes' to {verb.lower()} these objects: ") \
            .strip().lower() != "yes":
        print(f"{verb} cancelled.")
        return
    applied = []
    for entry in moves:
        name = entry["name"]
        if _shared_exists(client, obj_type, name):
            skips.append(name)
            continue
        fields = {k: v for k, v in entry.items() if k != "name"}
        ok, err = write_object(client, SHARED_LABEL, obj_type, name, fields,
                               existed=False)
        if not ok:
            applied.append((name, fields, None, "FAILED", err))
            continue
        if mode == "copy":
            applied.append((name, fields, True, "COPIED", ""))
            continue
        ok2, err2 = delete_object(client, device_group, obj_type, name)
        applied.append((name, fields, True if ok2 else False,
                        "MOVED" if ok2 else "PARTIAL",
                        "" if ok2 else
                        f"created in shared but still present in "
                        f"'{device_group}': {err2}"))
    client._running_config = None

    changes = []
    for name, fields, _ok, status, err in applied:
        after = _readback(client, obj_type, SHARED_LABEL, name) \
            if status != "FAILED" else None
        changes.append((obj_type, name, status,
                        _move_diff_lines(device_group, fields, after, mode),
                        err))
    for name in skips:
        changes.append((obj_type, name, "SKIPPED", [],
                        "already exists in shared (not overwritten)"))
    diff_path, text = _move_diff_write(device_group, obj_type, mode,
                                       changes, mode)

    done_status = "COPIED" if mode == "copy" else "MOVED"
    done = sum(1 for c in changes if c[2] == done_status)
    note = (f"Copied {done} object(s) into '{SHARED_LABEL}' (device-group "
            f"objects kept)" if mode == "copy" else
            f"Moved {done} object(s) into '{SHARED_LABEL}'")
    if skips:
        note += f", skipped {len(skips)} shared duplicate(s)"
    print(f"\n{note}.")
    _move_print_diff(text, diff_path)

    undoable = [(n, f) for n, f, _ok, s, _e in applied if s != "FAILED"]
    actions = ["commit changes", f"revert this {mode}", "leave pending"]
    action = choose(actions, f"Post-{mode}")
    if action == actions[0]:
        commit_changes(client)
    elif action == actions[1]:
        revert_move(client, device_group, obj_type,
                    [(n, f, True) for n, f in undoable], mode)


def copy_or_move_to_shared(client: PanoramaClient, device_group: str,
                           obj_type: str) -> None:
    """Copy (or move) objects from a device group into (shared) keeping all
    properties. Move additionally removes them from the device group.
    Names that already exist as shared objects are skipped, never
    overwritten. After each pass the user can run another pass (objects are
    re-listed, so convert as many batches as needed); the selection can also
    be a text file with one object name per line ('@names.txt')."""
    if device_group == SHARED_LABEL:
        print("Nothing to do: these objects are already shared.")
        return
    while True:
        _copy_move_pass(client, device_group, obj_type)
        more = ["convert more objects (re-pick object type)", "finish"]
        if choose(more, "More") != more[0]:
            return
        obj_type = choose(OBJECT_TYPES, "Object types")


def _move_diff_lines(device_group: str, before_fields: dict | None,
                     after_fields: dict | None, mode: str = "move") -> list[str]:
    if mode == "copy":
        lines = [f"  + copy in '{SHARED_LABEL}' (kept in '{device_group}')"]
    else:
        lines = [f"  ~ location: '{device_group}' -> '{SHARED_LABEL}'"]
    if after_fields is None:
        lines.append("  ! shared copy could not be read back for "
                     "verification")
        return lines
    fd = diff_entry_lines({"name": "-", **(before_fields or {})},
                          {"name": "-", **after_fields})
    if not fd:
        lines.append("  (all properties identical)")
    else:
        lines.extend(fd)
    return lines


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
                        help="remove the cached API key for --host and "
                             "generate a new one")
    parser.add_argument("--list-keys", action="store_true",
                        help="list hosts with cached API keys and exit")
    parser.add_argument("--output", choices=("text", "json", "csv"),
                        default="text",
                        help="object listing format; json/csv are saved to "
                             "<device_group>_<obj_type>.<ext> in the CWD")
    args = parser.parse_args()
    print(f"panorama_cli.py build {BUILD}")

    if args.list_keys:
        saved = list_api_keys()
        if not saved:
            print("No cached API keys.")
        else:
            print(f"Cached API keys ({key_file_path()}):")
            for host, saved_at in saved:
                print(f"  {host}  (saved {saved_at})")
        return

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
        forget_api_key(args.host)
        api_key = None
    else:
        api_key = load_api_key(args.host)
    if api_key:
        print(f"Using existing API key for {args.host}")
    else:
        others = sorted(h for h in _load_keyring() if h != args.host)
        if others:
            print("(no cached key for this host; cached keys exist for: "
                  f"{', '.join(others)})")
        if not args.user:
            args.user = input("Username: ").strip()
        if not args.password:
            args.password = getpass.getpass("Password (hidden): ")
        if not args.user or not args.password:
            sys.exit("error: no saved API key; username and password are required")
        print(f"Generating API key on {args.host} ...")
        api_key = fetch_api_key(args.host, args.user, args.password)
        save_api_key(args.host, api_key)
        print(f"API key for {args.host} saved to {key_file_path().name}")

    client = PanoramaClient(args.host, api_key, debug=args.debug)

    if args.diagnose:
        run_diagnostics(client)
        return

    # 2. Fetch running config, then pick a mode; re-offer the menu after each.
    print("Fetching running config ...")
    client.running_config()
    modes = ["explore device-group objects (browse/export/import/copy)",
             "find where an object is used (rule search)",
             "convert object files (CSV <-> JSON)",
             "configuration snapshots (save/export/list)",
             "quit"]
    while True:
        if args.output == "text":
            mode = choose(modes, "Mode")
        else:
            mode = modes[0]
        if mode == "quit":
            print("bye")
            return
        if mode.startswith("find "):
            where_used(client)
        elif mode.startswith("convert "):
            convert_mode()
        elif mode.startswith("configuration "):
            snapshot_mode(client)
        else:
            explore_objects(client, args)
        if args.output != "text":
            return


def explore_objects(client: PanoramaClient, args) -> None:
    """Device-group browser: list/export/import/copy-move object(s)."""
    groups = list_device_groups(client)
    if isinstance(client.running_config().get("shared"), dict):
        groups.insert(0, SHARED_LABEL)
    device_group = choose(groups, "Device groups")
    print(f"Selected device group: {device_group}")

    # Action: list, export, import, or move objects to shared. Re-offer the
    # menu after each action so multiple actions can be run per session.
    actions = ["list objects", "export to CSV/JSON",
               "import from CSV/JSON file"]
    if device_group != SHARED_LABEL:
        actions.append("copy/move object(s) to shared")
    actions.append("back to mode menu")

    while True:
        if args.output == "text":
            action = choose(actions, "Action")
        else:
            action = actions[1]

        if action == actions[-1]:
            return

        if action == actions[2]:
            import_objects(client, device_group)
            if args.output != "text":
                return
            continue

        obj_type = choose(OBJECT_TYPES, "Object types")

        if action.startswith("copy/"):
            copy_or_move_to_shared(client, device_group, obj_type)
        elif action == actions[1]:
            out_fmt = args.output
            if out_fmt == "text":
                out_fmt = choose(["csv", "json"], "Export format")
            print_objects(client, device_group, obj_type, out_fmt=out_fmt)
        else:
            print_objects(client, device_group, obj_type)

        if args.output != "text":
            return


if __name__ == "__main__":
    main()
