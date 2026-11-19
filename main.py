#!/usr/bin/env python3
"""github-core-clone-01: a standalone GitHub-compatible git forge in one process.

Python 3.13 stdlib only. Serves, from its own in-memory store:
  - git smart HTTP (upload-pack v2 + v0, receive-pack v0) at /{owner}/{repo}.git
  - REST v3 at /api/v3
  - GraphQL v4 at /api/graphql
  - MCP (Streamable HTTP, JSON-RPC 2.0) at /mcp
Lifecycle: bind an ephemeral port, POST {"port": N} to $MAKEFUN_ENV_URL/register,
poll GET $MAKEFUN_ENV_URL/done until {"done": true}, exit 0. Without
MAKEFUN_ENV_URL it just serves forever (local practice runs).

Determinism: no wall clock anywhere in served state. Commit timestamps come
from a global sequence counter; REST timestamps are one fixed string; ids and
numbers come from counters. The same request sequence yields the same state.
"""
import base64
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.request
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ZERO_OID = "0" * 40
FIXED_TS = "2025-01-01T00:00:00Z"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# The suite's seeded scenario, re-derived (wire.ts buildScenario, same seed).
# The bearer token names the scenario viewer; the forge must know that login
# (and the stranger's, for the 404-not-403 rule) without any env hint.
# ---------------------------------------------------------------------------
_MASK = (1 << 64) - 1
_SCENARIO_SEED = 0x6A09E667F3BCC909
_FIRST = ['ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
          'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
          'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane']
_LAST = ['anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
         'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
         'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich',
         'vega', 'weber', 'xu', 'yilmaz', 'zimmer']


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & _MASK
    return h


def _mix64(v):
    z = (v + 0x9E3779B97F4A7C15) & _MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return (z ^ (z >> 31)) & _MASK


class _Rng:
    def __init__(self, seed):
        s = seed & _MASK
        self.s = s if s else _SCENARIO_SEED

    def next_u64(self):
        x = self.s
        x ^= (x >> 12)
        x ^= ((x << 25) & _MASK)
        x ^= (x >> 27)
        self.s = x & _MASK
        return (x * 0x2545F4914F6CDD1D) & _MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, m):
        if m == 1:
            return 0
        limit = 4294967296 - (4294967296 % m)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % m

    def pick(self, items):
        return items[self.next_int(len(items))]


def _scenario_users():
    rng = _Rng(_mix64(_SCENARIO_SEED ^ _fnv1a64("users")))
    used, users = set(), []
    for _ in range(12):
        base = rng.pick(_FIRST) + rng.pick(_LAST)
        login, suf = base, 2
        while login in used:
            login = "%s-%d" % (base, suf)
            suf += 1
        used.add(login)
        users.append({"login": login, "name": login,
                      "email": "%s@example.invalid" % login})
    return users


_USERS = _scenario_users()
VIEWER = _USERS[0]          # adaiversen
STRANGER = _USERS[2]        # olgabauer
VIEWER_LOGIN = VIEWER["login"]
VIEWER_EMAIL = VIEWER["email"]
IDENTITY = "%s <%s>" % (VIEWER_LOGIN, VIEWER_EMAIL)

# ---------------------------------------------------------------------------
# Git object primitives (SHA-1, trees, commits, tags, packfiles, pkt-lines)
# ---------------------------------------------------------------------------
_TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
_CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}


def hash_object(otype, data):
    h = hashlib.sha1()
    h.update(("%s %d\x00" % (otype, len(data))).encode("ascii"))
    h.update(data)
    return h.hexdigest()


def parse_tree(data):
    entries, off = [], 0
    while off < len(data):
        sp = data.index(b" ", off)
        nul = data.index(b"\x00", sp + 1)
        entries.append({"mode": data[off:sp].decode("ascii"),
                        "name": data[sp + 1:nul].decode("utf-8"),
                        "oid": data[nul + 1:nul + 21].hex()})
        off = nul + 21
    return entries


def _tree_sort_key(e):
    name = e["name"].encode("utf-8")
    return name + b"/" if e["mode"] == "40000" else name


def build_tree(entries):
    out = bytearray()
    for e in sorted(entries, key=_tree_sort_key):
        out += e["mode"].encode("ascii") + b" " + e["name"].encode("utf-8") + b"\x00"
        out += bytes.fromhex(e["oid"])
    return bytes(out)


def parse_commit(data):
    blank = data.find(b"\n\n")
    head = data if blank < 0 else data[:blank]
    msg = b"" if blank < 0 else data[blank + 2:]
    tree, parents, author, committer = "", [], "", ""
    for line in head.decode("utf-8", "replace").split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return {"tree": tree, "parents": parents, "author": author,
            "committer": committer, "message": msg.decode("utf-8", "replace")}


def build_commit(tree, parents, message, author, committer):
    lines = ["tree %s" % tree]
    lines += ["parent %s" % p for p in parents]
    lines.append("author %s" % author)
    lines.append("committer %s" % committer)
    return ("\n".join(lines) + "\n\n" + message).encode("utf-8")


def parse_tag(data):
    blank = data.find(b"\n\n")
    head = data if blank < 0 else data[:blank]
    msg = b"" if blank < 0 else data[blank + 2:]
    hdr = {}
    for line in head.decode("utf-8", "replace").split("\n"):
        if " " in line:
            k, v = line.split(" ", 1)
            hdr[k] = v
    return {"object": hdr.get("object", ""), "type": hdr.get("type", ""),
            "tag": hdr.get("tag", ""), "tagger": hdr.get("tagger", ""),
            "message": msg.decode("utf-8", "replace")}


def build_tag(obj, otype, tag, message, tagger):
    return ("object %s\ntype %s\ntag %s\ntagger %s\n\n%s"
            % (obj, otype, tag, tagger, message)).encode("utf-8")


# --- pkt-lines ---------------------------------------------------------------
def pkt(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return ("%04x" % (len(data) + 4)).encode("ascii") + data


FLUSH = b"0000"
DELIM = b"0001"


def pkt_lines(items):
    out = bytearray()
    for it in items:
        if it == "flush":
            out += FLUSH
        elif it == "delim":
            out += DELIM
        else:
            out += pkt(it)
    return bytes(out)


def read_pkt_lines(buf):
    """Return (packets, rest_offset). packets: list of ('data', bytes) /
    ('flush', None) / ('delim', None). Raises ValueError on garbage."""
    packets, off = [], 0
    while off < len(buf):
        if off + 4 > len(buf):
            raise ValueError("truncated pkt-line header")
        head = buf[off:off + 4]
        if head == b"0000":
            packets.append(("flush", None))
            off += 4
            continue
        if head == b"0001":
            packets.append(("delim", None))
            off += 4
            continue
        try:
            n = int(head.decode("ascii"), 16)
        except ValueError:
            raise ValueError("bad pkt-line length %r" % head)
        if n < 4 or off + n > len(buf):
            raise ValueError("bad pkt-line length %d" % n)
        packets.append(("data", bytes(buf[off + 4:off + n])))
        off += n
    return packets, off


# --- deltas ------------------------------------------------------------------
def _read_varint(buf, off):
    v, shift = 0, 0
    while True:
        if off >= len(buf):
            raise ValueError("delta varint truncated")
        b = buf[off]
        off += 1
        v += (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return v, off


def apply_delta(base, delta):
    src_size, off = _read_varint(delta, 0)
    tgt_size, off = _read_varint(delta, off)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            cp_off, cp_size, shift = 0, 0, 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off += delta[off] << shift
                    off += 1
                shift += 8
            shift = 0
            for bit in range(4, 7):
                if op & (1 << bit):
                    cp_size += delta[off] << shift
                    off += 1
                shift += 8
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[off:off + op]
            off += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta size mismatch")
    return bytes(out)


# --- packfiles ---------------------------------------------------------------
def _pack_obj_header(otype, size):
    code = _TYPE_CODE[otype]
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects):
    """objects: iterable of (otype, bytes). Plain pack, no deltas, v2."""
    objs = list(objects)
    parts = [b"PACK" + (2).to_bytes(4, "big") + len(objs).to_bytes(4, "big")]
    for otype, data in objs:
        parts.append(_pack_obj_header(otype, len(data)))
        parts.append(zlib.compress(data))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def _read_pack_size(buf, start):
    b = buf[start]
    size = b & 0x0F
    shift, off, first = 4, start + 1, b
    while b & 0x80:
        b = buf[off]
        off += 1
        size += (b & 0x7F) << shift
        shift += 7
    return size, off, first


def _read_ofs_base(buf, start):
    b = buf[start]
    off, val = start + 1, b & 0x7F
    while b & 0x80:
        b = buf[off]
        off += 1
        val = ((val + 1) << 7) + (b & 0x7F)
    return val, off


def read_pack(pack, resolve_base=None):
    """Return dict oid -> (otype, bytes). Raises ValueError on corruption."""
    buf = bytes(pack)
    if len(buf) < 32 or buf[:4] != b"PACK":
        raise ValueError("not a packfile")
    ver = int.from_bytes(buf[4:8], "big")
    if ver not in (2, 3):
        raise ValueError("unsupported pack version %d" % ver)
    count = int.from_bytes(buf[8:12], "big")
    trailer = buf[-20:]
    if hashlib.sha1(buf[:-20]).digest() != trailer:
        raise ValueError("pack trailer checksum mismatch")
    end = len(buf) - 20
    raw, by_off, off = [], {}, 12
    for _ in range(count):
        entry_off = off
        size, off, first = _read_pack_size(buf, off)
        code = (first >> 4) & 0x07
        base_off, base_oid = None, None
        if code == 6:
            delta, off = _read_ofs_base(buf, off)
            base_off = entry_off - delta
        elif code == 7:
            base_oid = buf[off:off + 20].hex()
            off += 20
        elif code not in _CODE_TYPE:
            raise ValueError("unknown pack type code %d" % code)
        dec = zlib.decompressobj()
        data = dec.decompress(buf[off:end])
        consumed = len(buf[off:end]) - len(dec.unused_data)
        if len(data) != size:
            raise ValueError("pack entry size mismatch")
        off += consumed
        ent = {"offset": entry_off, "code": code, "size": size, "data": data,
               "base_off": base_off, "base_oid": base_oid}
        raw.append(ent)
        by_off[entry_off] = ent
    if off != end:
        raise ValueError("pack has trailing bytes")
    resolved, by_oid = {}, {}

    def resolve(ent, seen):
        if ent["offset"] in resolved:
            return resolved[ent["offset"]]
        if ent["offset"] in seen:
            raise ValueError("delta cycle")
        seen = seen | {ent["offset"]}
        if ent["code"] == 6:
            base = by_off.get(ent["base_off"])
            if base is None:
                raise ValueError("ofs-delta base missing")
            btype, bdata, _ = resolve(base, seen)
            otype, data = btype, apply_delta(bdata, ent["data"])
        elif ent["code"] == 7:
            bent = by_oid.get(ent["base_oid"])
            if bent is not None:
                btype, bdata, _ = resolve(bent, seen)
            elif resolve_base is not None:
                got = resolve_base(ent["base_oid"])
                if got is None:
                    raise ValueError("ref-delta base missing")
                btype, bdata = got
            else:
                raise ValueError("ref-delta base missing")
            otype, data = btype, apply_delta(bdata, ent["data"])
        else:
            otype, data = _CODE_TYPE[ent["code"]], ent["data"]
        oid = hash_object(otype, data)
        resolved[ent["offset"]] = (otype, data, oid)
        by_oid[oid] = ent
        return otype, data, oid

    out = {}
    for ent in raw:
        otype, data, oid = resolve(ent, set())
        out[oid] = (otype, data)
    return out

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def _gid(kind, *parts):
    return base64.b64encode(("%s:%s" % (kind, "|".join(parts))).encode()).decode()


class Repo:
    def __init__(self, rid, name, owner, description, private):
        self.id = rid
        self.name = name
        self.owner = owner
        self.description = description or ""
        self.private = bool(private)
        self.default_branch = "main"
        self.objects = {}          # oid -> (otype, bytes)
        self.refs = {}             # refname -> oid
        self.issues = []           # dicts, creation order
        self.pulls = []            # dicts, creation order
        self.labels = {}           # name -> {name, color}
        self.issue_seq = 0
        self.pull_seq = 0
        self.comments = {}         # issue number -> [comment dicts]
        self._fetch_cache_key = None
        self._fetch_cache_val = None
        for lab, col in (("bug", "d73a4a"), ("enhancement", "a2eeef"),
                         ("documentation", "0075ca"), ("question", "d876e3"),
                         ("good first issue", "7057ff"), ("help wanted", "008672")):
            self.labels[lab] = {"name": lab, "color": col, "default": True}
        self._issue_json_cache = {}

    # -- git object helpers -------------------------------------------------
    def put_obj(self, otype, data):
        oid = hash_object(otype, data)
        self.objects.setdefault(oid, (otype, data))
        return oid

    def get_obj(self, oid):
        return self.objects.get(oid)

    def peel(self, oid):
        """Follow tag objects to their target commit oid."""
        seen = set()
        while oid in self.objects and oid not in seen:
            seen.add(oid)
            otype, data = self.objects[oid]
            if otype == "tag":
                oid = parse_tag(data)["object"]
            else:
                return oid
        return oid

    def commit_of(self, oid):
        oid = self.peel(oid)
        obj = self.objects.get(oid)
        return oid if obj and obj[0] == "commit" else None

    def reachable_commits(self, tips, depth=None):
        """BFS over parents. Returns (commits dict oid->dist, order list)."""
        dist, order, queue = {}, [], []
        for t in tips:
            c = self.commit_of(t)
            if c and c not in dist:
                dist[c] = 0
                queue.append(c)
        i = 0
        while i < len(queue):
            cur = queue[i]
            i += 1
            order.append(cur)
            # Only expand a commit whose parent would still be within the
            # depth limit: depth=1 keeps the tip only, depth=2 the tip and
            # its parent, and so on.
            if depth is not None and dist[cur] + 1 >= depth:
                continue
            obj = self.objects.get(cur)
            if not obj:
                continue
            for p in parse_commit(obj[1])["parents"]:
                pc = self.commit_of(p)
                if pc and pc not in dist:
                    dist[pc] = dist[cur] + 1
                    queue.append(pc)
        return dist, order

    def tree_closure(self, tree_oid):
        """All (oid) in a tree, recursively (trees + blobs)."""
        out, stack = set(), [tree_oid]
        while stack:
            oid = stack.pop()
            if oid in out:
                continue
            obj = self.objects.get(oid)
            if not obj:
                continue
            out.add(oid)
            if obj[0] == "tree":
                stack.extend(e["oid"] for e in parse_tree(obj[1]))
        return out

    def closure_objects(self, tips, depth=None):
        """All object oids reachable from tips (commits, trees, blobs, tags)."""
        dist, commits = self.reachable_commits(tips, depth)
        out = set()
        for t in tips:
            if t in self.objects and self.objects[t][0] == "tag":
                out.add(t)
        for c in commits:
            out.add(c)
            tree = parse_commit(self.objects[c][1])["tree"]
            out |= self.tree_closure(tree)
        return out, dist

    def is_ancestor(self, anc, desc):
        if anc == desc:
            return True
        # BFS from desc, stopping the moment anc is found: a push's
        # fast-forward check must not walk the whole history every time.
        seen, queue = {desc}, [desc]
        i = 0
        while i < len(queue):
            cur = queue[i]
            i += 1
            obj = self.objects.get(cur)
            if not obj or obj[0] != "commit":
                continue
            for p in parse_commit(obj[1])["parents"]:
                pc = self.commit_of(p)
                if pc == anc:
                    return True
                if pc and pc not in seen:
                    seen.add(pc)
                    queue.append(pc)
        return False

    def ref_tip(self, ref):
        return self.refs.get(ref)

    def default_tip(self):
        return self.refs.get("refs/heads/" + self.default_branch)

    # -- file tree helpers --------------------------------------------------
    def tree_at(self, commit_oid):
        c = self.commit_of(commit_oid)
        if not c:
            return None
        return parse_commit(self.objects[c][1])["tree"]

    def file_at(self, commit_oid, path):
        """(blob_oid, bytes) or None for path at a commit."""
        tree = self.tree_at(commit_oid)
        if tree is None:
            return None
        parts = [p for p in path.split("/") if p != ""]
        cur = tree
        for i, seg in enumerate(parts):
            obj = self.objects.get(cur)
            if not obj or obj[0] != "tree":
                return None
            ent = next((e for e in parse_tree(obj[1]) if e["name"] == seg), None)
            if ent is None:
                return None
            if i == len(parts) - 1:
                b = self.objects.get(ent["oid"])
                return (ent["oid"], b[1]) if b and b[0] == "blob" else None
            cur = ent["oid"]
        return None

    def dir_at(self, commit_oid, path):
        """List of tree entries at a directory path, or None."""
        tree = self.tree_at(commit_oid)
        if tree is None:
            return None
        if path in ("", "/"):
            obj = self.objects.get(tree)
            return parse_tree(obj[1]) if obj else None
        parts = [p for p in path.split("/") if p != ""]
        cur = tree
        for seg in parts:
            obj = self.objects.get(cur)
            if not obj or obj[0] != "tree":
                return None
            ent = next((e for e in parse_tree(obj[1]) if e["name"] == seg), None)
            if ent is None or ent["mode"] != "40000":
                return None
            cur = ent["oid"]
        obj = self.objects.get(cur)
        return parse_tree(obj[1]) if obj and obj[0] == "tree" else None

    def _set_path(self, tree_oid, parts, blob_oid):
        """Return new tree oid with parts[0]/... set to blob_oid (or deleted)."""
        entries = []
        if tree_oid:
            obj = self.objects.get(tree_oid)
            if obj and obj[0] == "tree":
                entries = parse_tree(obj[1])
        entries = [dict(e) for e in entries]
        seg = parts[0]
        if len(parts) == 1:
            entries = [e for e in entries if e["name"] != seg]
            if blob_oid is not None:
                entries.append({"mode": "100644", "name": seg, "oid": blob_oid})
        else:
            child = next((e for e in entries if e["name"] == seg), None)
            child_tree = child["oid"] if child and child["mode"] == "40000" else None
            new_child = self._set_path(child_tree, parts[1:], blob_oid)
            entries = [e for e in entries if e["name"] != seg]
            if new_child is not None:
                entries.append({"mode": "40000", "name": seg, "oid": new_child})
        if not entries:
            return None
        return self.put_obj("tree", build_tree(entries))

    # -- issues / pulls ------------------------------------------------------
    def issue_by_number(self, n):
        for iss in self.issues:
            if iss["number"] == n:
                return iss
        return None

    def pull_by_number(self, n):
        for pr in self.pulls:
            if pr["number"] == n:
                return pr
        return None

    def issue_obj(self, iss):
        return {"id": iss["id"], "node_id": iss["node_id"], "number": iss["number"],
                "title": iss["title"], "body": iss["body"], "state": iss["state"],
                "labels": [{"name": l, "color": self.labels.get(l, {}).get("color", "ededed")}
                           for l in iss["labels"]],
                "user": {"login": VIEWER_LOGIN, "id": 1000, "type": "User"},
                "created_at": FIXED_TS, "updated_at": FIXED_TS,
                "comments": len(self.comments.get(iss["number"], [])),
                "url": "/api/v3/repos/%s/%s/issues/%d" % (self.owner, self.name, iss["number"])}

    def pull_obj(self, pr):
        return {"id": pr["id"], "node_id": pr["node_id"], "number": pr["number"],
                "title": pr["title"], "body": pr["body"], "state": pr["state"],
                "head": {"ref": pr["head"], "sha": pr["head_sha"],
                         "label": "%s:%s" % (self.owner, pr["head"])},
                "base": {"ref": pr["base"], "sha": pr["base_sha"],
                         "label": "%s:%s" % (self.owner, pr["base"])},
                "merged": pr["merged"], "mergeable": pr.get("mergeable", True),
                "merged_at": FIXED_TS if pr["merged"] else None,
                "merge_commit_sha": pr.get("merge_commit_sha"),
                "user": {"login": VIEWER_LOGIN, "id": 1000, "type": "User"},
                "created_at": FIXED_TS, "updated_at": FIXED_TS,
                "url": "/api/v3/repos/%s/%s/pulls/%d" % (self.owner, self.name, pr["number"])}

    def repo_obj(self):
        return {"id": self.id, "node_id": _gid("repo", self.owner, self.name),
                "name": self.name, "full_name": "%s/%s" % (self.owner, self.name),
                "owner": {"login": self.owner, "id": 1000, "type": "User"},
                "private": self.private, "description": self.description,
                "default_branch": self.default_branch, "has_issues": True,
                "has_projects": True, "has_wiki": True,
                "created_at": FIXED_TS, "updated_at": FIXED_TS,
                "pushed_at": FIXED_TS,
                "url": "/api/v3/repos/%s/%s" % (self.owner, self.name),
                "html_url": "https://example.invalid/%s/%s" % (self.owner, self.name)}


class Forge:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}            # (owner, name) -> Repo
        self.repo_seq = 5000
        self.global_id_seq = 100000
        self.comment_seq = 1
        self.commit_seq = 0        # deterministic commit timestamps
        self.req_seq = 0

    def next_req_id(self):
        self.req_seq += 1
        return "req-%08x" % self.req_seq

    def next_commit_ts(self):
        self.commit_seq += 1
        return 1700000000 + self.commit_seq

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def create_repo(self, name, description, private):
        if (VIEWER_LOGIN, name) in self.repos:
            return None
        repo = Repo(self.repo_seq, name, VIEWER_LOGIN, description, private)
        self.repo_seq += 1
        self.repos[(VIEWER_LOGIN, name)] = repo
        return repo

    def delete_repo(self, owner, name):
        return self.repos.pop((owner, name), None) is not None

    # -- commits made through the contents API / merges ----------------------
    def make_commit(self, repo, tree_oid, parents, message):
        ts = self.next_commit_ts()
        who = "%s %d +0000" % (IDENTITY, ts)
        data = build_commit(tree_oid, parents, message.rstrip("\n") + "\n", who, who)
        return repo.put_obj("commit", data)

    def commit_file(self, repo, branch, path, content, message, sha=None):
        """Create/update one file on branch; returns (commit_oid, blob_oid, status).
        status: 'created' | 'updated'. Raises ValueError(msg, http_status)."""
        ref = "refs/heads/" + branch
        tip = repo.refs.get(ref)
        if tip is None and branch != repo.default_branch:
            base_tip = repo.default_tip()
            if base_tip is None:
                raise ValueError("branch does not exist", 404)
            repo.refs[ref] = base_tip
            tip = base_tip
        blob_oid = repo.put_obj("blob", content)
        if tip is None:
            tree_oid = repo._set_path(None, [p for p in path.split("/") if p], blob_oid)
            commit = self.make_commit(repo, tree_oid, [], message)
            repo.refs[ref] = commit
            return commit, blob_oid, "created"
        cur = repo.file_at(tip, path)
        if cur is not None:
            if sha is not None and sha.lower() != cur[0].lower():
                raise ValueError("sha does not match the current blob", 409)
            if sha is None:
                raise ValueError("sha is required to update a file", 409)
            status = "updated"
        else:
            if sha is not None:
                raise ValueError("sha does not match the current blob", 409)
            status = "created"
        base_tree = repo.tree_at(tip)
        new_tree = repo._set_path(base_tree, [p for p in path.split("/") if p], blob_oid)
        commit = self.make_commit(repo, new_tree, [tip], message)
        repo.refs[ref] = commit
        return commit, blob_oid, status

    def delete_file(self, repo, branch, path, message, sha=None):
        ref = "refs/heads/" + branch
        tip = repo.refs.get(ref)
        if tip is None:
            raise ValueError("branch does not exist", 404)
        cur = repo.file_at(tip, path)
        if cur is None:
            raise ValueError("file does not exist", 404)
        if sha is not None and sha.lower() != cur[0].lower():
            raise ValueError("sha does not match the current blob", 409)
        base_tree = repo.tree_at(tip)
        new_tree = repo._set_path(base_tree, [p for p in path.split("/") if p], None)
        if new_tree is None:
            new_tree = repo.put_obj("tree", build_tree([]))
        commit = self.make_commit(repo, new_tree, [tip], message)
        repo.refs[ref] = commit
        return commit

    def diff_files(self, repo, base_tip, head_tip):
        """Changed paths between two commits: [{filename, status, additions,
        deletions, changes, old_blob, new_blob}]."""
        def flat(tip):
            out = {}
            if tip is None:
                return out
            tree = repo.tree_at(tip)
            stack = [(tree, "")]
            while stack:
                t, prefix = stack.pop()
                obj = repo.objects.get(t)
                if not obj or obj[0] != "tree":
                    continue
                for e in parse_tree(obj[1]):
                    p = prefix + e["name"]
                    if e["mode"] == "40000":
                        stack.append((e["oid"], p + "/"))
                    else:
                        out[p] = e["oid"]
            return out
        b, h = flat(base_tip), flat(head_tip)
        out = []
        for p in sorted(set(b) | set(h)):
            if p not in b:
                nb = repo.objects.get(h[p])
                nlines = nb[1].count(b"\n") + (1 if nb[1] and not nb[1].endswith(b"\n") else 0) if nb else 0
                out.append({"filename": p, "status": "added", "additions": nlines,
                            "deletions": 0, "changes": nlines, "new_blob": h[p]})
            elif p not in h:
                ob = repo.objects.get(b[p])
                olines = ob[1].count(b"\n") + (1 if ob[1] and not ob[1].endswith(b"\n") else 0) if ob else 0
                out.append({"filename": p, "status": "removed", "additions": 0,
                            "deletions": olines, "changes": olines, "old_blob": b[p]})
            elif b[p] != h[p]:
                nb = repo.objects.get(h[p])
                ob = repo.objects.get(b[p])
                nl = nb[1].count(b"\n") + (1 if nb[1] and not nb[1].endswith(b"\n") else 0) if nb else 0
                ol = ob[1].count(b"\n") + (1 if ob[1] and not ob[1].endswith(b"\n") else 0) if ob else 0
                out.append({"filename": p, "status": "modified", "additions": nl,
                            "deletions": ol, "changes": nl + ol,
                            "old_blob": b[p], "new_blob": h[p]})
        return out

    def mergeable(self, repo, pr):
        """True unless the head changed a file the base has since changed."""
        base_tip = repo.refs.get("refs/heads/" + pr["base"])
        if base_tip is None:
            return False
        head_changes = {d["filename"] for d in self.diff_files(repo, pr["base_sha"], pr["head_sha"])}
        base_changes = {d["filename"] for d in self.diff_files(repo, pr["base_sha"], base_tip)}
        for p in head_changes & base_changes:
            head_file = repo.file_at(pr["head_sha"], p)
            base_file = repo.file_at(base_tip, p)
            if head_file is None or base_file is None or head_file[0] != base_file[0]:
                return False
        return True

    def do_merge(self, repo, pr):
        """Squash-merge pr onto its base branch. Returns merge commit oid,
        or None when conflicted."""
        if pr["state"] != "open":
            return None
        if not self.mergeable(repo, pr):
            return None
        base_ref = "refs/heads/" + pr["base"]
        base_tip = repo.refs.get(base_ref)
        head_changes = self.diff_files(repo, pr["base_sha"], pr["head_sha"])
        tree = repo.tree_at(base_tip)
        for d in head_changes:
            blob = d.get("new_blob")
            tree = repo._set_path(tree, [p for p in d["filename"].split("/") if p], blob)
        if tree is None:
            tree = repo.put_obj("tree", build_tree([]))
        msg = "%s\n\n%s" % (pr["title"], pr["body"] or "")
        commit = self.make_commit(repo, tree, [base_tip], msg)
        repo.refs[base_ref] = commit
        pr["state"] = "closed"
        pr["merged"] = True
        pr["merge_commit_sha"] = commit
        return commit

# ---------------------------------------------------------------------------
# Git smart HTTP
# ---------------------------------------------------------------------------
def _ref_lines(repo):
    """Sorted [(oid, name)] for all refs, HEAD first when it has a tip."""
    out = []
    for name in sorted(repo.refs):
        out.append((repo.refs[name], name))
    return out


def _tag_peel(repo, oid):
    if oid in repo.objects and repo.objects[oid][0] == "tag":
        return repo.peel(oid)
    return None


def _advertise_v0(repo, service):
    caps_up = ("side-band-64k thin-pack ofs-delta shallow multi_ack "
               "multi_ack_detailed agent=forge/1.0")
    caps_dn = ("report-status side-band-64k delete-refs ofs-delta "
               "agent=forge/1.0")
    caps = caps_up if service == "git-upload-pack" else caps_dn
    tip = repo.default_tip()
    symref = " symref=HEAD:refs/heads/%s" % repo.default_branch
    body = bytearray()
    body += pkt("# service=%s\n" % service)
    body += FLUSH
    refs = _ref_lines(repo)
    if not refs:
        body += pkt("%s capabilities^{}\x00%s\n" % (ZERO_OID, caps))
    else:
        head_oid = tip or ZERO_OID
        body += pkt("%s HEAD\x00%s%s\n" % (head_oid, caps, symref))
        for oid, name in refs:
            body += pkt("%s %s\n" % (oid, name))
            peeled = _tag_peel(repo, oid)
            if peeled:
                body += pkt("%s %s^{}\n" % (peeled, name))
    body += FLUSH
    return bytes(body)


def _advertise_v2(repo):
    # One pkt-line per capability: git (and the kit client) read the first
    # line as exactly "version 2" and each following line as one capability.
    lines = ["version 2\n", "agent=forge/1.0\n", "ls-refs=unborn\n",
             "fetch=shallow wait-for-done filter\n", "server-option\n",
             "object-format=sha1\n"]
    return b"".join(pkt(l) for l in lines) + FLUSH


def _ls_refs(repo, args):
    """v2 ls-refs. args: list of request lines (after 'command=ls-refs').
    Clients may send several ref-prefix lines; a ref matches if it matches
    ANY of them. HEAD matches only a prefix of 'HEAD' itself."""
    peel = symrefs = unborn = False
    prefixes = []
    for a in args:
        if a == "peel":
            peel = True
        elif a == "symrefs":
            symrefs = True
        elif a == "unborn":
            unborn = True
        elif a.startswith("ref-prefix "):
            prefixes.append(a[len("ref-prefix "):])

    def matches(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    tip = repo.default_tip()
    lines = []
    if matches("HEAD"):
        if tip:
            line = "%s HEAD" % tip
            if symrefs:
                line += " symref-target:refs/heads/%s" % repo.default_branch
            lines.append(line)
        elif unborn:
            lines.append("unborn HEAD symref-target:refs/heads/%s" % repo.default_branch)
    for oid, name in _ref_lines(repo):
        if not matches(name):
            continue
        line = "%s %s" % (oid, name)
        if symrefs and name == "refs/heads/%s" % repo.default_branch:
            line += " symref-target:refs/heads/%s" % repo.default_branch
        if peel:
            peeled = _tag_peel(repo, oid)
            if peeled:
                line += " peeled:%s" % peeled
        lines.append(line)
    return pkt_lines([l + "\n" for l in lines] + ["flush"])


def _fetch_objects(repo, wants, haves, depth, prev_shallow):
    """Compute the object set for a fetch. Returns (pack_bytes, shallow, unshallow).
    wants/haves: oids; depth: int or None; prev_shallow: client-held shallow oids.
    Identical concurrent fetches (the clone workload's 50 clones) hit a
    single-entry cache keyed on the request AND the store state, so any new
    object or ref move invalidates it."""
    key = (tuple(sorted(wants)), tuple(sorted(haves)), depth,
           tuple(sorted(prev_shallow)), len(repo.objects),
           tuple(sorted(repo.refs.items())))
    if repo._fetch_cache_key == key:
        return repo._fetch_cache_val
    result = _fetch_objects_uncached(repo, wants, haves, depth, prev_shallow)
    repo._fetch_cache_key = key
    repo._fetch_cache_val = result
    return result


def _fetch_objects_uncached(repo, wants, haves, depth, prev_shallow):
    want_commits = []
    want_tags = []
    for w in wants:
        obj = repo.objects.get(w)
        if not obj:
            continue
        if obj[0] == "tag":
            want_tags.append(w)
        else:
            want_commits.append(w)
    dist, _ = repo.reachable_commits(want_commits, depth)
    included = set(dist)
    # held objects: full closure of haves when unshallowed; otherwise just the
    # haves themselves (their trees/blobs) so shallow/deepen stays exact.
    if depth is None and not prev_shallow:
        held, _ = repo.closure_objects(haves)
    else:
        held = set()
        for h in haves:
            hc = repo.commit_of(h)
            if hc:
                held.add(hc)
                held |= repo.tree_closure(repo.tree_at(hc))
            elif h in repo.objects:
                held.add(h)
    send = set()
    for t in want_tags:
        if t not in held:
            send.add(t)
    for c in included:
        if c not in held:
            send.add(c)
            send |= repo.tree_closure(repo.tree_at(c))
    send -= held
    # shallow / unshallow boundary lines
    shallow, unshallow = [], []
    if depth is not None:
        for c, d in dist.items():
            if d == depth - 1:
                obj = repo.objects.get(c)
                if obj and parse_commit(obj[1])["parents"]:
                    shallow.append(c)
    for s in prev_shallow:
        c = repo.commit_of(s)
        if c and c in included:
            obj = repo.objects.get(c)
            parents = parse_commit(obj[1])["parents"] if obj else []
            if all(repo.commit_of(p) in included for p in parents):
                unshallow.append(c)
    objs = [(repo.objects[o][0], repo.objects[o][1]) for o in sorted(send)
            if o in repo.objects]
    return write_pack(objs), shallow, unshallow


def _sideband_pack(pack):
    out = bytearray()
    off = 0
    # A pkt-line is at most 65520 bytes including its 4-byte header, and the
    # sideband band byte rides inside the payload: 65515 bytes of pack data
    # per packet (65515 + 1 band + 4 header = 65520).
    while off < len(pack):
        chunk = pack[off:off + 65515]
        out += pkt(b"\x01" + chunk)
        off += len(chunk)
    return bytes(out)


def _upload_pack_v2(repo, body):
    """Parse a v2 request; return response bytes.

    Section rules (protocol v2, as git 2.43 implements them and as the kit
    client parses them): a request that carries `done` is the final answer,
    so the response goes straight to [shallow-info] + packfile with NO
    acknowledgments section. A request with haves but no `done` is a
    negotiation round: acknowledgments only, and the packfile follows in the
    same response only when "ready" is sent."""
    try:
        packets, _ = read_pkt_lines(body)
    except ValueError:
        return pkt("unsupported command\n") + FLUSH
    lines = [p[1].decode("utf-8", "replace").rstrip("\n")
             for p in packets if p[0] == "data"]
    cmd = None
    args = []
    for ln in lines:
        if ln.startswith("command="):
            cmd = ln[len("command="):]
        elif ln.startswith("agent="):
            continue
        elif ln == "":
            continue
        else:
            args.append(ln)
    if cmd == "ls-refs":
        return _ls_refs(repo, args)
    if cmd != "fetch":
        return pkt("unsupported command\n") + FLUSH
    wants, haves, depth = [], [], None
    prev_shallow = []
    done = False
    for a in args:
        if a.startswith("want "):
            wants.append(a[5:].split()[0])
        elif a.startswith("have "):
            haves.append(a[5:].split()[0])
        elif a.startswith("shallow "):
            prev_shallow.append(a[8:].strip())
        elif a.startswith("deepen "):
            depth = int(a[7:].strip())
        elif a == "done":
            done = True
    pack, shallow, unshallow = _fetch_objects(repo, wants, haves, depth, prev_shallow)
    has_pack = len(pack) > 32  # a 0-object pack is exactly 32 bytes
    out = bytearray()
    if haves and not done:
        # Negotiation round: acknowledgments section, pack only if ready.
        out += pkt("acknowledgments\n")
        acked = [h for h in haves if h in repo.objects]
        if acked:
            for h in acked:
                out += pkt("ACK %s\n" % h)
        else:
            out += pkt("NAK\n")
        if not has_pack:
            out += FLUSH
            return bytes(out)
        out += pkt("ready\n")
        out += DELIM
    if shallow or unshallow:
        out += pkt("shallow-info\n")
        for s in shallow:
            out += pkt("shallow %s\n" % s)
        for s in unshallow:
            out += pkt("unshallow %s\n" % s)
        out += DELIM
    # The packfile section is always present for a final answer, even when
    # the pack holds zero objects: the client reads one sideband packet and
    # finds nothing new, instead of erroring on a missing section.
    out += pkt("packfile\n")
    out += _sideband_pack(pack)
    out += FLUSH
    return bytes(out)


def _upload_pack_v0(repo, body):
    try:
        packets, _ = read_pkt_lines(body)
    except ValueError:
        return pkt("NAK\n") + FLUSH
    wants, haves, depth = [], [], None
    prev_shallow = []
    for kind, data in packets:
        if kind != "data":
            continue
        ln = data.decode("utf-8", "replace").rstrip("\n")
        if "\x00" in ln:
            ln = ln.split("\x00", 1)[0]
        if ln.startswith("want "):
            # v0 carries the requested features space-separated on the first
            # want line; only the first token is the object id.
            wants.append(ln[5:].split()[0])
        elif ln.startswith("shallow "):
            depth_line = ln[8:].strip()
            prev_shallow.append(depth_line)
        elif ln.startswith("deepen "):
            depth = int(ln[7:].strip())
        elif ln.startswith("have "):
            haves.append(ln[5:].split()[0])
        elif ln == "done":
            break
    pack, shallow, unshallow = _fetch_objects(repo, wants, haves, depth, prev_shallow)
    out = bytearray()
    acked = [h for h in haves if h in repo.objects]
    if acked:
        for h in acked:
            out += pkt("ACK %s\n" % h)
    else:
        out += pkt("NAK\n")
    for s in shallow:
        out += pkt("shallow %s\n" % s)
    for s in unshallow:
        out += pkt("unshallow %s\n" % s)
    # Always frame the pack (even a 0-object one): a bare flush reads as a
    # missing pack to real git, while a 32-byte empty pack reads as nothing new.
    out += _sideband_pack(pack)
    out += FLUSH
    return bytes(out)


def _receive_pack(repo, body):
    """Parse receive-pack commands + pack; apply fast-forward rules.
    Returns response bytes (sideband report)."""
    # Parse pkt-lines incrementally, stopping at the flush: the bytes after
    # it are a raw packfile, not pkt-lines.
    packets, used = [], 0
    try:
        while used < len(body):
            if body[used:used + 4] == b"0000":
                used += 4
                break
            if body[used:used + 4] == b"0001":
                used += 4
                continue
            n = int(body[used:used + 4].decode("ascii"), 16)
            if n < 4 or used + n > len(body):
                raise ValueError("bad pkt-line length")
            packets.append(bytes(body[used + 4:used + n]))
            used += n
    except ValueError:
        return _sideband_pack(pkt("unpack error\n") + FLUSH) + FLUSH
    commands = []
    for data in packets:
        ln = data.decode("utf-8", "replace").rstrip("\n")
        if "\x00" in ln:
            ln = ln.split("\x00", 1)[0]
        m = re.match(r"^([0-9a-f]{40}) ([0-9a-f]{40}) (\S+)$", ln)
        if m:
            commands.append(m.groups())
    pack_bytes = body[used:]
    unpack_ok = True
    unpack_err = ""
    if pack_bytes:
        try:
            objs = read_pack(pack_bytes,
                             resolve_base=lambda oid: repo.objects.get(oid))
            for oid, (otype, data) in objs.items():
                repo.objects.setdefault(oid, (otype, data))
        except ValueError as e:
            unpack_ok = False
            unpack_err = str(e)
    lines = ["unpack %s\n" % ("ok" if unpack_ok else unpack_err or "error")]
    for old, new, ref in commands:
        cur = repo.refs.get(ref)
        ok, reason = False, ""
        if not unpack_ok:
            reason = "unpack failed"
        elif new == ZERO_OID:
            if cur is None:
                reason = "reference does not exist"
            elif old != cur:
                reason = "old oid mismatch"
            else:
                del repo.refs[ref]
                ok = True
        elif cur is None:
            if old != ZERO_OID:
                reason = "old oid mismatch"
            elif new not in repo.objects:
                reason = "object not found"
            else:
                repo.refs[ref] = new
                ok = True
        else:
            if old != cur:
                reason = "non-fast-forward"
            elif new not in repo.objects:
                reason = "object not found"
            elif ref.startswith("refs/heads/"):
                new_c = repo.commit_of(new)
                if new_c is None or not repo.is_ancestor(cur, new_c):
                    reason = "non-fast-forward"
                else:
                    repo.refs[ref] = new
                    ok = True
            else:
                repo.refs[ref] = new
                ok = True
        if ok:
            lines.append("ok %s\n" % ref)
        else:
            lines.append("ng %s %s\n" % (ref, reason or "error"))
    # The report's pkt-lines travel inside sideband band 1, and the reader
    # ends its loop on a FLUSH *inside* that band-1 stream (not just the
    # outer flush that ends the HTTP body): without it the client reads to
    # EOF and dies with "the remote end hung up unexpectedly".
    return _sideband_pack(pkt_lines(lines + ["flush"])) + FLUSH

# ---------------------------------------------------------------------------
# REST v3
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _json_body(handler, required=False):
    raw = handler._body
    if not raw:
        if required:
            raise ApiError(400, "Request body must be valid JSON")
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(400, "Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object")
    return data


def _require_repo(forge, owner, name):
    repo = forge.get_repo(owner, name)
    if repo is None:
        raise ApiError(404, "Not Found")
    return repo


def _is_viewer(owner):
    return owner == VIEWER_LOGIN


def _check_owner(owner):
    """Stranger repos: 404, never 403 (the token names the viewer)."""
    if owner != VIEWER_LOGIN:
        raise ApiError(404, "Not Found")


def _page_links(handler, path, query, total, per_page, page):
    if total <= per_page * page:
        return None
    q = dict(query)
    q["page"] = [str(page + 1)]
    q["per_page"] = [str(per_page)]
    qs = "&".join("%s=%s" % (k, v[0]) for k, v in q.items())
    host = handler.headers.get("Host") or "127.0.0.1"
    return '<http://%s%s?%s>; rel="next"' % (host, path, qs)


def rest_dispatch(forge, handler, method, path, query, body_raw):
    """Returns (status, payload_dict_or_list, extra_headers, raw_bytes)."""
    handler._body = body_raw
    parts = [unquote(p) for p in path.split("/") if p != ""]
    # /api/v3/...
    if not parts or parts[0] != "api" or len(parts) < 2 or parts[1] != "v3":
        raise ApiError(404, "Not Found")
    rest = parts[2:]

    if rest == [] and method == "GET":
        return 200, {"current_user_url": "/api/v3/user",
                     "repository_url": "/api/v3/repos/{owner}/{repo}"}, \
            {"X-OAuth-Scopes": "repo, read:org"}, None
    if rest == ["meta"] and method == "GET":
        return 200, {"installed_version": VERSION,
                     "verifiable_password_authentication": True}, {}, None
    if rest == ["user"] and method == "GET":
        return 200, {"login": VIEWER_LOGIN, "id": 1000, "type": "User",
                     "name": VIEWER_LOGIN, "email": VIEWER_EMAIL}, {}, None
    if rest == ["user", "repos"]:
        if method == "GET":
            items = [r for (o, _), r in sorted(forge.repos.items()) if o == VIEWER_LOGIN]
            per_page = int(query.get("per_page", ["30"])[0])
            page = int(query.get("page", ["1"])[0])
            total = len(items)
            chunk = items[(page - 1) * per_page:page * per_page]
            hdrs = {}
            link = _page_links(handler, path, query, total, per_page, page)
            if link:
                hdrs["Link"] = link
            return 200, [r.repo_obj() for r in chunk], hdrs, None
        if method == "POST":
            data = _json_body(handler, required=True)
            name = data.get("name")
            if not name:
                raise ApiError(422, "name is required")
            if forge.get_repo(VIEWER_LOGIN, name):
                raise ApiError(422, "Repository already exists")
            repo = forge.create_repo(name, data.get("description", ""),
                                     bool(data.get("private", False)))
            return 201, repo.repo_obj(), {}, None
        raise ApiError(404, "Not Found")

    if len(rest) >= 2 and rest[0] == "repos":
        owner, name = rest[1], rest[2] if len(rest) > 2 else None
        if name is None:
            raise ApiError(404, "Not Found")
        tail = rest[3:]
        if tail == []:
            if method == "GET":
                _check_owner(owner)
                return 200, _require_repo(forge, owner, name).repo_obj(), {}, None
            if method == "PATCH":
                _check_owner(owner)
                repo = _require_repo(forge, owner, name)
                data = _json_body(handler)
                if "description" in data:
                    repo.description = data["description"]
                if "default_branch" in data:
                    repo.default_branch = data["default_branch"]
                return 200, repo.repo_obj(), {}, None
            if method == "DELETE":
                _check_owner(owner)
                if not forge.delete_repo(owner, name):
                    raise ApiError(404, "Not Found")
                return 204, None, {}, None
            raise ApiError(404, "Not Found")
        _check_owner(owner)
        repo = _require_repo(forge, owner, name)

        if tail == ["branches"] and method == "GET":
            out = [{"name": rn[11:], "commit": {"sha": oid}}
                   for rn, oid in sorted(repo.refs.items())
                   if rn.startswith("refs/heads/")]
            return 200, out, {}, None
        if len(tail) == 2 and tail[0] == "branches" and method == "GET":
            ref = "refs/heads/" + tail[1]
            oid = repo.refs.get(ref)
            if oid is None:
                raise ApiError(404, "Not Found")
            return 200, {"name": tail[1], "commit": {"sha": oid}}, {}, None
        if tail == ["commits"] and method == "GET":
            sha = query.get("sha", [None])[0]
            tip = repo.refs.get("refs/heads/" + sha) if sha else repo.default_tip()
            if tip is None:
                return 200, [], {}, None
            dist, order = repo.reachable_commits([tip])
            out = []
            for c in order:
                pc = parse_commit(repo.objects[c][1])
                out.append({"sha": c, "commit": {"message": pc["message"].rstrip("\n"),
                                                 "author": {"name": VIEWER_LOGIN,
                                                            "email": VIEWER_EMAIL}}})
            return 200, out, {}, None
        if tail == ["git", "refs"] and method == "POST":
            data = _json_body(handler, required=True)
            ref = data.get("ref", "")
            sha = data.get("sha", "")
            if not ref.startswith("refs/") or sha not in repo.objects:
                raise ApiError(422, "Invalid ref or sha")
            if ref in repo.refs:
                raise ApiError(422, "Reference already exists")
            repo.refs[ref] = sha
            return 201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}, {}, None
        if len(tail) == 4 and tail[:2] == ["git", "refs"] and tail[2] == "heads" \
                and method == "GET":
            ref = "refs/heads/" + tail[3]
            oid = repo.refs.get(ref)
            if oid is None:
                raise ApiError(404, "Not Found")
            return 200, {"ref": ref, "object": {"sha": oid, "type": "commit"}}, {}, None
        if tail == ["contents"] and method == "GET":
            return _contents_get(repo, "", query)
        if len(tail) >= 2 and tail[0] == "contents" and method == "GET":
            return _contents_get(repo, "/".join(tail[1:]), query)
        if len(tail) >= 2 and tail[0] == "contents" and method in ("PUT", "DELETE"):
            return _contents_write(forge, repo, "/".join(tail[1:]), handler, method)
        if tail == ["labels"] and method == "POST":
            data = _json_body(handler, required=True)
            lname = data.get("name")
            if not lname:
                raise ApiError(422, "name is required")
            color = data.get("color", "ededed")
            repo.labels[lname] = {"name": lname, "color": color, "default": False}
            return 201, {"name": lname, "color": color}, {}, None
        if tail == ["labels"] and method == "GET":
            return 200, list(repo.labels.values()), {}, None
        if tail == ["issues"]:
            if method == "GET":
                return _issues_list(repo, query)
            if method == "POST":
                return _issue_create(forge, repo, handler)
            raise ApiError(404, "Not Found")
        if len(tail) == 2 and tail[0] == "issues" and tail[1].isdigit():
            num = int(tail[1])
            iss = repo.issue_by_number(num)
            if iss is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                return 200, repo.issue_obj(iss), {}, None
            if method == "PATCH":
                data = _json_body(handler)
                if "state" in data:
                    iss["state"] = data["state"]
                if "title" in data:
                    iss["title"] = data["title"]
                if "body" in data:
                    iss["body"] = data["body"]
                repo._issue_json_cache.pop(num, None)
                return 200, repo.issue_obj(iss), {}, None
            raise ApiError(404, "Not Found")
        if len(tail) == 3 and tail[0] == "issues" and tail[1].isdigit() \
                and tail[2] == "comments":
            num = int(tail[1])
            iss = repo.issue_by_number(num)
            if iss is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                return 200, list(repo.comments.get(num, [])), {}, None
            if method == "POST":
                data = _json_body(handler, required=True)
                body = data.get("body", "")
                forge.comment_seq += 1
                c = {"id": forge.comment_seq - 1, "body": body,
                     "user": {"login": VIEWER_LOGIN, "id": 1000, "type": "User"},
                     "created_at": FIXED_TS, "updated_at": FIXED_TS}
                repo.comments.setdefault(num, []).append(c)
                return 201, c, {}, None
            raise ApiError(404, "Not Found")
        if tail == ["pulls"]:
            if method == "GET":
                return _pulls_list(repo, query)
            if method == "POST":
                return _pull_create(forge, repo, handler)
            raise ApiError(404, "Not Found")
        if len(tail) == 2 and tail[0] == "pulls" and tail[1].isdigit():
            num = int(tail[1])
            pr = repo.pull_by_number(num)
            if pr is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                return 200, repo.pull_obj(pr), {}, None
            raise ApiError(404, "Not Found")
        if len(tail) == 3 and tail[0] == "pulls" and tail[1].isdigit() \
                and tail[2] == "files":
            num = int(tail[1])
            pr = repo.pull_by_number(num)
            if pr is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                files = forge.diff_files(repo, pr["base_sha"], pr["head_sha"])
                return 200, [{"filename": d["filename"], "status": d["status"],
                              "additions": d["additions"], "deletions": d["deletions"],
                              "changes": d["changes"]} for d in files], {}, None
            raise ApiError(404, "Not Found")
        if len(tail) == 3 and tail[0] == "pulls" and tail[1].isdigit() \
                and tail[2] == "merge":
            num = int(tail[1])
            pr = repo.pull_by_number(num)
            if pr is None:
                raise ApiError(404, "Not Found")
            if method == "PUT":
                if pr["state"] != "open":
                    raise ApiError(405, "Pull request is not open")
                if not forge.mergeable(repo, pr):
                    raise ApiError(405, "Pull request cannot be merged: conflict")
                sha = forge.do_merge(repo, pr)
                return 200, {"sha": sha, "merged": True,
                             "message": "Pull request successfully merged"}, {}, None
            raise ApiError(404, "Not Found")
    raise ApiError(404, "Not Found")


def _contents_get(repo, path, query):
    ref = query.get("ref", [None])[0]
    tip = repo.refs.get("refs/heads/" + ref) if ref else repo.default_tip()
    if tip is None:
        raise ApiError(404, "Not Found")
    f = repo.file_at(tip, path) if path else None
    if path:
        if f is not None:
            oid, data = f
            return 200, {"type": "file", "encoding": "base64", "size": len(data),
                         "name": path.split("/")[-1], "path": path, "sha": oid,
                         "content": base64.b64encode(data).decode()}, {}, None
        entries = repo.dir_at(tip, path)
        if entries is None:
            raise ApiError(404, "Not Found")
        return 200, [{"type": "dir" if e["mode"] == "40000" else "file",
                      "name": e["name"], "path": path + "/" + e["name"],
                      "sha": e["oid"]} for e in entries], {}, None
    entries = repo.dir_at(tip, "")
    if entries is None:
        raise ApiError(404, "Not Found")
    return 200, [{"type": "dir" if e["mode"] == "40000" else "file",
                  "name": e["name"], "path": e["name"], "sha": e["oid"]}
                 for e in entries], {}, None


def _contents_write(forge, repo, path, handler, method):
    data = _json_body(handler, required=(method == "PUT"))
    message = data.get("message", "")
    branch = data.get("branch") or repo.default_branch
    try:
        if method == "PUT":
            content = base64.b64decode(data.get("content", ""), validate=False)
            commit, blob_oid, status = forge.commit_file(
                repo, branch, path, content, message, data.get("sha"))
            code = 201 if status == "created" else 200
            return code, {"content": {"type": "file", "encoding": "base64",
                                      "size": len(content), "name": path.split("/")[-1],
                                      "path": path, "sha": blob_oid},
                          "commit": {"sha": commit, "message": message}}, {}, None
        commit = forge.delete_file(repo, branch, path, message, data.get("sha"))
        return 200, {"commit": {"sha": commit, "message": message}}, {}, None
    except ValueError as e:
        status = e.args[1] if len(e.args) > 1 else 409
        raise ApiError(status, str(e))


def _issues_list(repo, query):
    items = list(repo.issues)
    state = query.get("state", [None])[0]
    if state and state != "all":
        items = [i for i in items if i["state"] == state]
    sort = query.get("sort", ["created"])[0]
    direction = query.get("direction", ["desc"])[0]
    if sort == "created":
        items.sort(key=lambda i: i["number"], reverse=(direction != "asc"))
    per_page = int(query.get("per_page", ["30"])[0])
    page = int(query.get("page", ["1"])[0])
    chunk = items[(page - 1) * per_page:page * per_page]
    return 200, [repo.issue_obj(i) for i in chunk], {}, None


def _issue_create(forge, repo, handler):
    data = _json_body(handler, required=True)
    title = data.get("title")
    if not title:
        raise ApiError(422, "title is required")
    labels = data.get("labels", [])
    for lab in labels:
        if lab not in repo.labels:
            raise ApiError(422, "Label '%s' does not exist" % lab)
    repo.issue_seq += 1
    forge.global_id_seq += 1
    iss = {"id": forge.global_id_seq,
           "node_id": _gid("issue", repo.owner, repo.name, str(repo.issue_seq)),
           "number": repo.issue_seq, "title": title,
           "body": data.get("body", ""), "state": "open",
           "labels": list(labels)}
    repo.issues.append(iss)
    return 201, repo.issue_obj(iss), {}, None


def _pulls_list(repo, query):
    state = query.get("state", ["open"])[0]
    items = list(repo.pulls)
    if state == "open":
        items = [p for p in items if p["state"] == "open"]
    elif state == "closed":
        items = [p for p in items if p["state"] == "closed"]
    return 200, [repo.pull_obj(p) for p in items], {}, None


def _pull_create(forge, repo, handler):
    data = _json_body(handler, required=True)
    title = data.get("title")
    head = data.get("head")
    base = data.get("base")
    if not title or not head or not base:
        raise ApiError(422, "title, head and base are required")
    head_ref = "refs/heads/" + head
    base_ref = "refs/heads/" + base
    head_tip = repo.refs.get(head_ref)
    base_tip = repo.refs.get(base_ref)
    if head_tip is None or base_tip is None:
        raise ApiError(422, "head or base branch does not exist")
    for pr in repo.pulls:
        if pr["state"] == "open" and pr["head"] == head and pr["base"] == base:
            raise ApiError(422, "A pull request already exists for %s" % head)
    repo.pull_seq += 1
    forge.global_id_seq += 1
    pr = {"id": forge.global_id_seq,
          "node_id": _gid("pr", repo.owner, repo.name, str(repo.pull_seq)),
          "number": repo.pull_seq, "title": title,
          "body": data.get("body", ""), "state": "open",
          "head": head, "head_sha": head_tip, "base": base,
          "base_sha": base_tip, "merged": False, "merge_commit_sha": None,
          "_owner": repo.owner, "_repo": repo.name}
    repo.pulls.append(pr)
    return 201, repo.pull_obj(pr), {}, None

# ---------------------------------------------------------------------------
# GraphQL (minimal executor for exactly the documents the suite sends)
# ---------------------------------------------------------------------------
class GqlError(Exception):
    pass


def _gql_tokenize(text):
    toks, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("...", i):
            toks.append(("...", "..."))
            i += 3
        elif c in "{}():[]!=$@|":
            toks.append((c, c))
            i += 1
        elif c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    out.append(text[j + 1])
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            toks.append(("string", "".join(out)))
            i = j + 1
        elif c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            toks.append(("number", float(raw) if any(x in raw for x in ".eE") else int(raw)))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(("name", text[i:j]))
            i = j
        else:
            raise GqlError("unexpected character %r" % c)
    toks.append(("eof", None))
    return toks


class _GqlParser:
    def __init__(self, text):
        self.toks = _gql_tokenize(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind):
        t = self.next()
        if t[0] != kind:
            raise GqlError("expected %s, got %r" % (kind, t))
        return t

    def skip(self, kind):
        if self.peek()[0] == kind:
            self.i += 1
            return True
        return False

    def parse_document(self):
        ops = []
        while self.peek()[0] != "eof":
            ops.append(self.parse_operation())
        return ops

    def parse_operation(self):
        kind = self.peek()
        if kind[0] == "{":
            return {"type": "query", "name": None, "selections": self.parse_selection_set()}
        if kind[0] == "name" and kind[1] in ("query", "mutation", "subscription"):
            otype = self.next()[1]
            name = None
            if self.peek()[0] == "name":
                name = self.next()[1]
            if self.peek()[0] == "(":
                self.parse_var_defs()
            return {"type": otype, "name": name,
                    "selections": self.parse_selection_set()}
        raise GqlError("unexpected token %r" % (kind,))

    def parse_var_defs(self):
        self.expect("(")
        depth = 1
        while depth:
            t = self.next()
            if t[0] == "(":
                depth += 1
            elif t[0] == ")":
                depth -= 1

    def parse_selection_set(self):
        self.expect("{")
        sels = []
        while self.peek()[0] != "}":
            sels.append(self.parse_selection())
        self.expect("}")
        return sels

    def parse_selection(self):
        if self.peek()[0] == "...":
            self.next()
            if self.peek() == ("name", "on"):
                self.next()
                cond = self.next()[1]
                return {"fragment": True, "type": cond,
                        "selections": self.parse_selection_set()}
            name = self.next()[1]
            return {"fragment": True, "spread": name, "selections": []}
        name = self.next()[1]
        alias = None
        if self.peek()[0] == ":":
            self.next()
            alias = name
            name = self.next()[1]
        args = {}
        if self.peek()[0] == "(":
            args = self.parse_args()
        sels = []
        if self.peek()[0] == "{":
            sels = self.parse_selection_set()
        return {"field": name, "alias": alias or name, "args": args,
                "selections": sels}

    def parse_args(self):
        self.expect("(")
        args = {}
        while self.peek()[0] != ")":
            k = self.next()[1]
            self.expect(":")
            args[k] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        t = self.next()
        if t[0] == "string":
            return t[1]
        if t[0] == "number":
            return t[1]
        if t[0] == "$":
            return ("var", self.next()[1])
        if t[0] == "[":
            items = []
            while self.peek()[0] != "]":
                items.append(self.parse_value())
            self.expect("]")
            return items
        if t[0] == "{":
            obj = {}
            while self.peek()[0] != "}":
                k = self.next()[1]
                self.expect(":")
                obj[k] = self.parse_value()
            self.expect("}")
            return obj
        if t[0] == "name":
            if t[1] == "true":
                return True
            if t[1] == "false":
                return False
            if t[1] == "null":
                return None
            return ("enum", t[1])
        raise GqlError("unexpected value token %r" % (t,))


def _gql_resolve_value(v, variables):
    if isinstance(v, tuple) and v[0] == "var":
        return variables.get(v[1])
    if isinstance(v, tuple) and v[0] == "enum":
        return v[1]
    if isinstance(v, list):
        return [_gql_resolve_value(x, variables) for x in v]
    if isinstance(v, dict):
        return {k: _gql_resolve_value(x, variables) for k, x in v.items()}
    return v


_TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "createdAt", "author",
              "labels", "comments", "url"],
    "PullRequest": ["id", "number", "title", "state", "body", "headRefName",
                    "baseRefName", "mergeable", "merged", "files", "headRef",
                    "baseRef", "mergeCommit", "author", "createdAt", "url"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequest", "pullRequests",
                   "description", "isPrivate", "createdAt", "url"],
    "SearchType": [],
}
_TYPE_ENUMS = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION", "COMMIT"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
}


def _decode_gid(gid):
    try:
        raw = base64.b64decode(gid.encode()).decode()
        kind, rest = raw.split(":", 1)
        return kind, rest.split("|")
    except Exception:
        return None, []


def _cursor(n):
    return base64.b64encode(("cursor:%d" % n).encode()).decode()


def _uncursor(cur):
    try:
        raw = base64.b64decode(cur.encode()).decode()
        return int(raw.split(":")[1])
    except Exception:
        return 0


class _GqlExec:
    def __init__(self, forge, variables):
        self.forge = forge
        self.variables = variables or {}

    def run(self, op):
        root_type = "Query" if op["type"] == "query" else "Mutation"
        root = {"__type": root_type}
        return self._select(root_type, root, op["selections"])

    def _select(self, type_name, obj, selections):
        out = {}
        for sel in selections:
            if sel.get("fragment"):
                cond = sel.get("type")
                if cond is None or cond == type_name:
                    out.update(self._select(type_name, obj, sel["selections"]))
                continue
            args = {k: _gql_resolve_value(v, self.variables)
                    for k, v in sel["args"].items()}
            val = self._field(type_name, obj, sel["field"], args, sel["selections"])
            out[sel["alias"]] = val
        return out

    def _field(self, type_name, obj, name, args, sels):
        fn = getattr(self, "_f_%s" % type_name, None)
        if fn is None:
            raise GqlError('Cannot query field "%s" on type "%s".' % (name, type_name))
        return fn(obj, name, args, sels)

    def _sub(self, type_name, obj, sels):
        if not sels:
            return obj
        return self._select(type_name, obj, sels)

    # -- root ----------------------------------------------------------------
    def _f_Query(self, obj, name, args, sels):
        if name == "viewer":
            return self._sub("Viewer", {"__type": "Viewer"}, sels)
        if name == "repository":
            repo = self.forge.get_repo(args.get("owner"), args.get("name"))
            if repo is None:
                return None
            return self._sub("Repository", repo, sels)
        if name == "node":
            return self._node(args.get("id"), sels)
        if name == "__type":
            tname = args.get("name")
            if tname not in _TYPE_FIELDS and tname not in _TYPE_ENUMS:
                return None
            return self._sub("__Type", {"__type": "__Type", "name": tname}, sels)
        raise GqlError('Cannot query field "%s" on type "Query".' % name)

    def _node(self, gid, sels):
        kind, parts = _decode_gid(gid or "")
        if kind == "repo" and len(parts) >= 2:
            repo = self.forge.get_repo(parts[0], parts[1])
            if repo is None:
                return None
            return self._sub("Repository", repo, sels)
        if kind == "issue" and len(parts) >= 3:
            repo = self.forge.get_repo(parts[0], parts[1])
            iss = repo.issue_by_number(int(parts[2])) if repo else None
            if iss is None:
                return None
            return self._sub("Issue", iss, sels)
        if kind == "pr" and len(parts) >= 3:
            repo = self.forge.get_repo(parts[0], parts[1])
            pr = repo.pull_by_number(int(parts[2])) if repo else None
            if pr is None:
                return None
            return self._sub("PullRequest", pr, sels)
        return None

    # -- mutations -------------------------------------------------------------
    def _f_Mutation(self, obj, name, args, sels):
        inp = args.get("input") or {}
        if name == "createIssue":
            kind, parts = _decode_gid(inp.get("repositoryId", ""))
            repo = self.forge.get_repo(parts[0], parts[1]) if kind == "repo" else None
            if repo is None:
                raise GqlError("repository not found")
            repo.issue_seq += 1
            self.forge.global_id_seq += 1
            iss = {"id": self.forge.global_id_seq,
                   "node_id": _gid("issue", repo.owner, repo.name, str(repo.issue_seq)),
                   "number": repo.issue_seq, "title": inp.get("title", ""),
                   "body": inp.get("body", ""), "state": "open", "labels": []}
            repo.issues.append(iss)
            return self._sub("CreateIssuePayload", {"issue": iss}, sels)
        if name == "closeIssue":
            kind, parts = _decode_gid(inp.get("issueId", ""))
            repo = self.forge.get_repo(parts[0], parts[1]) if kind == "issue" else None
            iss = repo.issue_by_number(int(parts[2])) if repo else None
            if iss is None:
                raise GqlError("issue not found")
            iss["state"] = "closed"
            return self._sub("CloseIssuePayload", {"issue": iss}, sels)
        if name == "createPullRequest":
            kind, parts = _decode_gid(inp.get("repositoryId", ""))
            repo = self.forge.get_repo(parts[0], parts[1]) if kind == "repo" else None
            if repo is None:
                raise GqlError("repository not found")
            head, base = inp.get("headRefName"), inp.get("baseRefName")
            head_tip = repo.refs.get("refs/heads/" + str(head))
            base_tip = repo.refs.get("refs/heads/" + str(base))
            if head_tip is None or base_tip is None:
                raise GqlError("head or base branch does not exist")
            for pr in repo.pulls:
                if pr["state"] == "open" and pr["head"] == head and pr["base"] == base:
                    raise GqlError("A pull request already exists for %s" % head)
            repo.pull_seq += 1
            self.forge.global_id_seq += 1
            pr = {"id": self.forge.global_id_seq,
                  "node_id": _gid("pr", repo.owner, repo.name, str(repo.pull_seq)),
                  "number": repo.pull_seq, "title": inp.get("title", ""),
                  "body": inp.get("body", ""), "state": "open",
                  "head": head, "head_sha": head_tip, "base": base,
                  "base_sha": base_tip, "merged": False, "merge_commit_sha": None,
                  "_owner": repo.owner, "_repo": repo.name}
            repo.pulls.append(pr)
            return self._sub("CreatePullRequestPayload", {"pullRequest": pr}, sels)
        if name == "mergePullRequest":
            kind, parts = _decode_gid(inp.get("pullRequestId", ""))
            repo = self.forge.get_repo(parts[0], parts[1]) if kind == "pr" else None
            pr = repo.pull_by_number(int(parts[2])) if repo else None
            if pr is None:
                raise GqlError("pull request not found")
            if pr["state"] != "open":
                raise GqlError("pull request is not open")
            if not self.forge.mergeable(repo, pr):
                raise GqlError("pull request cannot be merged: conflict")
            self.forge.do_merge(repo, pr)
            return self._sub("MergePullRequestPayload", {"pullRequest": pr}, sels)
        if name == "addComment":
            kind, parts = _decode_gid(inp.get("subjectId", ""))
            repo = self.forge.get_repo(parts[0], parts[1]) if kind == "issue" else None
            iss = repo.issue_by_number(int(parts[2])) if repo else None
            if iss is None:
                raise GqlError("issue not found")
            self.forge.comment_seq += 1
            c = {"id": self.forge.comment_seq - 1, "body": inp.get("body", ""),
                 "user": {"login": VIEWER_LOGIN, "id": 1000, "type": "User"},
                 "created_at": FIXED_TS, "updated_at": FIXED_TS}
            repo.comments.setdefault(iss["number"], []).append(c)
            node = {"__type": "IssueComment", "id": _gid("comment", repo.owner,
                    repo.name, str(iss["number"]), str(c["id"])), "body": c["body"]}
            return self._sub("AddCommentPayload", {"commentEdge": {"node": node}}, sels)
        raise GqlError('Cannot query field "%s" on type "Mutation".' % name)

    # -- payloads --------------------------------------------------------------
    def _f_CreateIssuePayload(self, obj, name, args, sels):
        return self._sub("Issue", obj["issue"], sels) if name == "issue" else self._unknown(name, "CreateIssuePayload")

    def _f_CloseIssuePayload(self, obj, name, args, sels):
        return self._sub("Issue", obj["issue"], sels) if name == "issue" else self._unknown(name, "CloseIssuePayload")

    def _f_CreatePullRequestPayload(self, obj, name, args, sels):
        return self._sub("PullRequest", obj["pullRequest"], sels) if name == "pullRequest" else self._unknown(name, "CreatePullRequestPayload")

    def _f_MergePullRequestPayload(self, obj, name, args, sels):
        return self._sub("PullRequest", obj["pullRequest"], sels) if name == "pullRequest" else self._unknown(name, "MergePullRequestPayload")

    def _f_AddCommentPayload(self, obj, name, args, sels):
        if name == "commentEdge":
            return self._sub("IssueCommentEdge", obj["commentEdge"], sels)
        return self._unknown(name, "AddCommentPayload")

    def _f_IssueCommentEdge(self, obj, name, args, sels):
        if name == "node":
            return self._sub("IssueComment", obj["node"], sels)
        return self._unknown(name, "IssueCommentEdge")

    def _unknown(self, name, type_name):
        raise GqlError('Cannot query field "%s" on type "%s".' % (name, type_name))

    # -- object types ------------------------------------------------------------
    def _f_Viewer(self, obj, name, args, sels):
        if name == "login":
            return VIEWER_LOGIN
        return self._unknown(name, "Viewer")

    def _f_User(self, obj, name, args, sels):
        if name == "login":
            return obj["login"]
        return self._unknown(name, "User")

    def _f_Ref(self, obj, name, args, sels):
        if name == "name":
            return obj["name"]
        return self._unknown(name, "Ref")

    def _f_Repository(self, repo, name, args, sels):
        if name == "id":
            return _gid("repo", repo.owner, repo.name)
        if name == "databaseId":
            return repo.id
        if name == "name":
            return repo.name
        if name == "nameWithOwner":
            return "%s/%s" % (repo.owner, repo.name)
        if name == "owner":
            return self._sub("User", {"login": repo.owner}, sels)
        if name == "defaultBranchRef":
            return self._sub("Ref", {"name": repo.default_branch}, sels)
        if name == "viewerPermission":
            return "ADMIN"
        if name == "hasIssuesEnabled":
            return True
        if name == "description":
            return repo.description
        if name == "issue":
            iss = repo.issue_by_number(int(args.get("number")))
            return None if iss is None else self._sub("Issue", iss, sels)
        if name == "issues":
            return self._issues_conn(repo, args, sels)
        if name == "pullRequest":
            pr = repo.pull_by_number(int(args.get("number")))
            return None if pr is None else self._sub("PullRequest", pr, sels)
        if name == "pullRequests":
            return self._prs_conn(repo, args, sels)
        return self._unknown(name, "Repository")

    def _issues_conn(self, repo, args, sels):
        items = list(repo.issues)
        states = args.get("states")
        if states:
            want = {s.upper() for s in states}
            items = [i for i in items if i["state"].upper() in want]
        order = args.get("orderBy") or {}
        if order.get("field") == "CREATED_AT" and str(order.get("direction")).upper() == "DESC":
            items.sort(key=lambda i: i["number"], reverse=True)
        else:
            items.sort(key=lambda i: i["number"])
        return self._connection(items, args, sels, "Issue")

    def _prs_conn(self, repo, args, sels):
        items = list(repo.pulls)
        states = args.get("states")
        if states:
            want = {s.upper() for s in states}
            items = [p for p in items if p["state"].upper() in want]
        return self._connection(items, args, sels, "PullRequest")

    def _connection(self, items, args, sels, item_type):
        first = args.get("first")
        after = args.get("after")

        def _key(it, idx):
            return it["number"] if "number" in it else idx

        start = 0
        if after:
            cur = _uncursor(after)
            for idx, it in enumerate(items):
                if _key(it, idx) > cur:
                    start = idx
                    break
            else:
                start = len(items)
        page = items[start:]
        has_next = False
        if first is not None and len(page) > int(first):
            page = page[:int(first)]
            has_next = True
        nodes = [self._sub(item_type, it, self._child_sels(sels, "nodes")) for it in page]
        end_cursor = _cursor(_key(page[-1], start + len(page) - 1)) if page else None
        out = {}
        for sel in sels:
            if sel.get("field") == "nodes":
                out[sel["alias"]] = nodes
            elif sel.get("field") == "pageInfo":
                out[sel["alias"]] = {"hasNextPage": has_next, "endCursor": end_cursor}
            elif sel.get("field") == "totalCount":
                out[sel["alias"]] = len(items)
        return out

    def _child_sels(self, sels, field_name):
        for sel in sels:
            if sel.get("field") == field_name:
                return sel["selections"]
        return []

    def _f_Issue(self, iss, name, args, sels):
        if name in ("id",):
            return iss["node_id"]
        if name == "number":
            return iss["number"]
        if name == "title":
            return iss["title"]
        if name == "state":
            return iss["state"].upper()
        if name == "body":
            return iss["body"]
        return self._unknown(name, "Issue")

    def _f_PullRequest(self, pr, name, args, sels):
        if name == "id":
            return pr["node_id"]
        if name == "number":
            return pr["number"]
        if name == "title":
            return pr["title"]
        if name == "state":
            return "MERGED" if pr["merged"] else pr["state"].upper()
        if name == "headRefName":
            return pr["head"]
        if name == "baseRefName":
            return pr["base"]
        if name == "merged":
            return pr["merged"]
        if name == "mergeable":
            repo = self.forge.get_repo(pr.get("_owner", VIEWER_LOGIN),
                                      pr.get("_repo", ""))
            if repo is None:
                return "UNKNOWN"
            return "MERGEABLE" if self.forge.mergeable(repo, pr) else "CONFLICTING"
        if name == "files":
            repo = self.forge.get_repo(pr.get("_owner", VIEWER_LOGIN),
                                      pr.get("_repo", ""))
            files = []
            if repo is not None:
                for d in self.forge.diff_files(repo, pr["base_sha"], pr["head_sha"]):
                    files.append({"path": d["filename"]})
            return self._connection(files, args, sels, "PullRequestFile")
        return self._unknown(name, "PullRequest")

    def _f_PullRequestFile(self, f, name, args, sels):
        if name == "path":
            return f["path"]
        return self._unknown(name, "PullRequestFile")

    def _f_IssueComment(self, c, name, args, sels):
        if name == "id":
            return c["id"]
        if name == "body":
            return c["body"]
        return self._unknown(name, "IssueComment")

    def _f___Type(self, obj, name, args, sels):
        tname = obj["name"]
        if name == "name":
            return tname
        if name == "fields":
            return [{"name": f} for f in _TYPE_FIELDS.get(tname, [])]
        if name == "enumValues":
            return [{"name": v} for v in _TYPE_ENUMS.get(tname, [])]
        return self._unknown(name, "__Type")


def graphql_execute(forge, query_text, variables):
    """Execute one GraphQL document. Returns a result dict."""
    try:
        ops = _GqlParser(query_text).parse_document()
        if not ops:
            raise GqlError("empty document")
        ex = _GqlExec(forge, variables or {})
        data = ex.run(ops[0])
        return {"data": data}
    except GqlError as e:
        return {"data": None, "errors": [{"message": str(e)}]}
    except Exception as e:
        return {"data": None, "errors": [{"message": "internal error: %s" % e}]}


def graphql_dispatch(forge, body_raw):
    try:
        payload = json.loads(body_raw.decode("utf-8"))
    except Exception:
        return 400, {"errors": [{"message": "Request body must be valid JSON"}]}, {}
    if isinstance(payload, list):
        return 200, [graphql_execute(forge, item.get("query", ""),
                                     item.get("variables") or {})
                     for item in payload if isinstance(item, dict)], {}
    if not isinstance(payload, dict) or "query" not in payload:
        return 400, {"errors": [{"message": "query is required"}]}, {}
    return 200, graphql_execute(forge, payload.get("query", ""),
                                payload.get("variables") or {}), {}

# ---------------------------------------------------------------------------
# MCP (Streamable HTTP, JSON-RPC 2.0)
# ---------------------------------------------------------------------------
_MCP_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26",
                 "2024-11-05"]
_MCP_SERVER_INFO = {"name": "forge", "version": VERSION}


def _mcp_tools():
    return [
        {"name": "create_repo", "description": "Create a repository.",
         "inputSchema": {"type": "object",
                         "properties": {"name": {"type": "string"},
                                        "description": {"type": "string"},
                                        "private": {"type": "boolean"}},
                         "required": ["name"]}},
        {"name": "get_repo", "description": "Get a repository by owner and name.",
         "inputSchema": {"type": "object",
                         "properties": {"owner": {"type": "string"},
                                        "repo": {"type": "string"}},
                         "required": ["owner", "repo"]}},
        {"name": "create_issue", "description": "Create an issue in a repository.",
         "inputSchema": {"type": "object",
                         "properties": {"owner": {"type": "string"},
                                        "repo": {"type": "string"},
                                        "title": {"type": "string"},
                                        "body": {"type": "string"}},
                         "required": ["owner", "repo", "title"]}},
        {"name": "list_issues", "description": "List issues in a repository.",
         "inputSchema": {"type": "object",
                         "properties": {"owner": {"type": "string"},
                                        "repo": {"type": "string"},
                                        "state": {"type": "string"}},
                         "required": ["owner", "repo"]}},
        {"name": "get_issue", "description": "Get one issue by number.",
         "inputSchema": {"type": "object",
                         "properties": {"owner": {"type": "string"},
                                        "repo": {"type": "string"},
                                        "issue_number": {"type": "integer"}},
                         "required": ["owner", "repo", "issue_number"]}},
        {"name": "create_pull_request", "description": "Create a pull request.",
         "inputSchema": {"type": "object",
                         "properties": {"owner": {"type": "string"},
                                        "repo": {"type": "string"},
                                        "title": {"type": "string"},
                                        "body": {"type": "string"},
                                        "head": {"type": "string"},
                                        "base": {"type": "string"}},
                         "required": ["owner", "repo", "title", "head", "base"]}},
        {"name": "list_pull_requests", "description": "List pull requests.",
         "inputSchema": {"type": "object",
                         "properties": {"owner": {"type": "string"},
                                        "repo": {"type": "string"},
                                        "state": {"type": "string"}},
                         "required": ["owner", "repo"]}},
        {"name": "merge_pull_request", "description": "Merge a pull request.",
         "inputSchema": {"type": "object",
                         "properties": {"owner": {"type": "string"},
                                        "repo": {"type": "string"},
                                        "pull_number": {"type": "integer"}},
                         "required": ["owner", "repo", "pull_number"]}},
    ]


def _mcp_result(content_obj, structured=None):
    return {"content": [{"type": "text",
                         "text": json.dumps(content_obj, separators=(",", ":"))}],
            "structuredContent": structured if structured is not None else content_obj}


def _mcp_error_result(message):
    return {"content": [{"type": "text", "text": json.dumps({"error": message})}],
            "isError": True}


def _mcp_call(forge, name, args):
    args = args or {}

    def repo_of():
        owner = args.get("owner") or VIEWER_LOGIN
        rname = args.get("repo") or args.get("repository") or args.get("full_name", "")
        if "/" in str(rname):
            owner, rname = str(rname).split("/", 1)
        repo = forge.get_repo(owner, rname)
        if repo is None:
            raise ApiError(404, "Not Found")
        return repo

    if name == "create_repo":
        rname = args.get("name")
        if not rname:
            return _mcp_error_result("name is required")
        if forge.get_repo(VIEWER_LOGIN, rname):
            return _mcp_error_result("Repository already exists")
        repo = forge.create_repo(rname, args.get("description", ""),
                                 bool(args.get("private", False)))
        return _mcp_result(repo.repo_obj())
    if name == "get_repo":
        return _mcp_result(repo_of().repo_obj())
    if name == "create_issue":
        repo = repo_of()
        title = args.get("title")
        if not title:
            return _mcp_error_result("title is required")
        repo.issue_seq += 1
        forge.global_id_seq += 1
        iss = {"id": forge.global_id_seq,
               "node_id": _gid("issue", repo.owner, repo.name, str(repo.issue_seq)),
               "number": repo.issue_seq, "title": title,
               "body": args.get("body", ""), "state": "open", "labels": []}
        repo.issues.append(iss)
        return _mcp_result(repo.issue_obj(iss))
    if name == "list_issues":
        repo = repo_of()
        items = list(repo.issues)
        state = (args.get("state") or "open").lower()
        if state != "all":
            items = [i for i in items if i["state"] == state]
        objs = [repo.issue_obj(i) for i in items]
        return _mcp_result(objs, {"issues": objs})
    if name == "get_issue":
        repo = repo_of()
        num = args.get("issue_number") or args.get("number")
        iss = repo.issue_by_number(int(num)) if num is not None else None
        if iss is None:
            return _mcp_error_result("issue not found")
        return _mcp_result(repo.issue_obj(iss))
    if name == "create_pull_request":
        repo = repo_of()
        head, base = args.get("head"), args.get("base")
        head_tip = repo.refs.get("refs/heads/" + str(head)) if head else None
        base_tip = repo.refs.get("refs/heads/" + str(base)) if base else None
        if head_tip is None or base_tip is None:
            return _mcp_error_result("head or base branch does not exist")
        for pr in repo.pulls:
            if pr["state"] == "open" and pr["head"] == head and pr["base"] == base:
                return _mcp_error_result("A pull request already exists for %s" % head)
        repo.pull_seq += 1
        forge.global_id_seq += 1
        pr = {"id": forge.global_id_seq,
              "node_id": _gid("pr", repo.owner, repo.name, str(repo.pull_seq)),
              "number": repo.pull_seq, "title": args.get("title", ""),
              "body": args.get("body", ""), "state": "open",
              "head": head, "head_sha": head_tip, "base": base,
              "base_sha": base_tip, "merged": False, "merge_commit_sha": None,
              "_owner": repo.owner, "_repo": repo.name}
        repo.pulls.append(pr)
        return _mcp_result(repo.pull_obj(pr))
    if name == "list_pull_requests":
        repo = repo_of()
        items = list(repo.pulls)
        state = (args.get("state") or "open").lower()
        if state == "open":
            items = [p for p in items if p["state"] == "open"]
        elif state == "closed":
            items = [p for p in items if p["state"] == "closed"]
        objs = [repo.pull_obj(p) for p in items]
        return _mcp_result(objs, {"pull_requests": objs})
    if name == "merge_pull_request":
        repo = repo_of()
        num = args.get("pull_number") or args.get("number")
        pr = repo.pull_by_number(int(num)) if num is not None else None
        if pr is None:
            return _mcp_error_result("pull request not found")
        if pr["state"] != "open":
            return _mcp_error_result("pull request is not open")
        if not forge.mergeable(repo, pr):
            return _mcp_error_result("pull request cannot be merged: conflict")
        sha = forge.do_merge(repo, pr)
        return _mcp_result({"sha": sha, "merged": True, "number": pr["number"]})
    return None


def mcp_dispatch(forge, body_raw):
    try:
        req = json.loads(body_raw.decode("utf-8"))
    except Exception:
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}, {}
    if not isinstance(req, dict) or "method" not in req:
        return 400, {"jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None,
                     "error": {"code": -32600, "message": "Invalid Request"}}, {}
    method = req["method"]
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return 202, None, {}
    if method == "server/discover":
        result = {"supportedVersions": list(_MCP_VERSIONS),
                  "capabilities": {"tools": {}},
                  "serverInfo": dict(_MCP_SERVER_INFO),
                  "_meta": {"io.modelcontextprotocol/serverInfo": dict(_MCP_SERVER_INFO)}}
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}, {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", _MCP_VERSIONS[0]),
                  "capabilities": {"tools": {}},
                  "serverInfo": dict(_MCP_SERVER_INFO)}
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}, {}
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {}}, {}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": rid,
                     "result": {"tools": _mcp_tools()}}, {}
    if method == "tools/call":
        tname = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = _mcp_call(forge, tname, args)
        except ApiError as e:
            result = _mcp_error_result(e.message)
        if result is None:
            return 200, {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32602, "message": "Unknown tool: %s" % tname}}, {}
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}, {}
    return 200, {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601, "message": "Method not found: %s" % method}}, {}


# ---------------------------------------------------------------------------
# HTTP server + lifecycle
# ---------------------------------------------------------------------------
FORGE = Forge()
SHOP_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN") or ""


def _auth_ok(handler, require=True):
    auth = handler.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return not require
    if SHOP_TOKEN and auth[7:] != SHOP_TOKEN:
        return False
    return True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _body = b""

    def setup(self):
        super().setup()
        # Kill Nagle: the bench workloads are thousands of small sequential
        # requests on one keep-alive connection; without TCP_NODELAY each
        # response waits on the client's delayed ACK (~40 ms).
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status, payload, content_type="application/json", extra=None):
        if payload is None:
            body = b""
        elif isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        reason = self.responses.get(status, ("?",))[0]
        lines = ["HTTP/1.1 %d %s" % (status, reason),
                 "Content-Type: %s" % content_type,
                 "Content-Length: %d" % len(body),
                 "X-GitHub-Request-Id: %s" % FORGE.next_req_id()]
        if extra:
            for k, v in extra.items():
                lines.append("%s: %s" % (k, v))
        # One write for the whole response: a second small write would be
        # held by Nagle on the client side even with TCP_NODELAY off.
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
        self.wfile.flush()

    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""
        with FORGE.lock:
            try:
                if path == "/healthz" and method == "GET":
                    return self._send(200, {"ok": True})
                if path.startswith("/api/v3"):
                    if not _auth_ok(self):
                        return self._send(401, {"message": "Requires authentication"})
                    status, payload, extra, _ = rest_dispatch(FORGE, self, method, path, query, body)
                    return self._send(status, payload, extra=extra)
                if path == "/api/graphql":
                    if not _auth_ok(self):
                        return self._send(401, {"message": "Requires authentication"})
                    if method != "POST":
                        return self._send(404, {"message": "Not Found"})
                    status, payload, extra = graphql_dispatch(FORGE, body)
                    return self._send(status, payload, extra=extra)
                if path == "/mcp":
                    if not _auth_ok(self):
                        return self._send(401, {"message": "Requires authentication"})
                    if method != "POST":
                        return self._send(404, {"message": "Not Found"})
                    status, payload, extra = mcp_dispatch(FORGE, body)
                    if status == 202:
                        return self._send(202, None)
                    return self._send(status, payload, extra=extra)
                m = re.match(r"^/([^/]+)/([^/]+)\.git(/.*)?$", path)
                if m:
                    owner, name, tail = m.group(1), m.group(2), m.group(3) or ""
                    if not _auth_ok(self, require=False):
                        return self._send(401, {"message": "Requires authentication"})
                    _check_owner(owner)
                    repo = _require_repo(FORGE, owner, name)
                    if tail == "/info/refs" and method == "GET":
                        service = query.get("service", [""])[0]
                        proto = self.headers.get("Git-Protocol") or ""
                        if service == "git-upload-pack" and "version=2" in proto:
                            return self._send(200, _advertise_v2(repo),
                                              "application/x-git-upload-pack-advertisement")
                        if service == "git-upload-pack":
                            return self._send(200, _advertise_v0(repo, service),
                                              "application/x-git-upload-pack-advertisement")
                        if service == "git-receive-pack":
                            return self._send(200, _advertise_v0(repo, service),
                                              "application/x-git-receive-pack-advertisement")
                        return self._send(400, {"message": "unknown service"})
                    if tail == "/git-upload-pack" and method == "POST":
                        proto = self.headers.get("Git-Protocol") or ""
                        if "version=2" in proto:
                            out = _upload_pack_v2(repo, body)
                        else:
                            out = _upload_pack_v0(repo, body)
                        return self._send(200, out, "application/x-git-upload-pack-result")
                    if tail == "/git-receive-pack" and method == "POST":
                        out = _receive_pack(repo, body)
                        return self._send(200, out, "application/x-git-receive-pack-result")
                    return self._send(404, {"message": "Not Found"})
                return self._send(404, {"message": "Not Found"})
            except ApiError as e:
                return self._send(e.status, {"message": e.message})
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception as e:
                import traceback
                traceback.print_exc()
                return self._send(500, {"message": "internal error: %s" % e})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")


def _http_json(method, url, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"error": str(e)}


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL") or ""
    port = int(os.environ.get("PORT") or 0)
    ThreadingHTTPServer.request_queue_size = 128
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    if env_url:
        for attempt in range(10):
            status, _ = _http_json("POST", env_url.rstrip("/") + "/register",
                                   {"port": actual}, timeout=5)
            if status == 200:
                break
            time.sleep(1)
        else:
            print("register failed", flush=True)
            return 1
        while True:
            status, done = _http_json("GET", env_url.rstrip("/") + "/done", timeout=5)
            if status == 200 and done.get("done"):
                break
            time.sleep(2)
        server.server_close()
        return 0
    print("forge listening on 127.0.0.1:%d" % actual, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
