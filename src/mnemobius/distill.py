"""Distill: raw session messages -> typed atoms. Pure functions; no database, no network.

Atom kinds
  ask         a human (or cron) request, redacted and shortened
  correction  a human message that corrects the agent
  approval    a short human acceptance
  call        a tool call (key = tool, sig = coarse argument digest)
  ok / fail   the tool result (key = tool, sig = failure signature hash for 'fail')
  entity      a named thing touched (skill:<name>, site:<host>)
  lesson      a line from a notes file that states a lesson
"""
from __future__ import annotations

import json
import re

from .redact import is_sensitive, redact
from .textkit import APPROVAL_RX, CORRECTION_RX, h, normalize_error, signature, tokens

_SUBCMD = {"git", "gh", "hermes", "systemctl", "docker", "pip", "pip3", "npm", "pnpm", "yarn", "cargo", "tailscale",
           "ssh", "scp", "rsync", "curl", "sqlite3", "ollama", "apt", "brew", "journalctl", "crontab"}
# Well-known programs may appear by name in a digest. Any other first word (a script, a file, a one-off tool) is hashed:
# a filename can say what was being worked on, and a digest only ever needs to be compared for equality.
_KNOWN_CMDS = frozenset("""git gh hermes systemctl docker pip pip3 npm npx pnpm yarn cargo go make tailscale ssh scp rsync curl wget sqlite3 ollama apt brew
journalctl crontab python python3 node bash sh zsh ls cat head tail grep rg sed awk find sort uniq wc cut tr tee xargs diff cp mv rm mkdir rmdir touch chmod chown
ln stat file du df ps top kill pkill sleep echo printf test true false date env which whoami id uname hostname ping dig nslookup nc tar zip unzip gzip jq yq
ffmpeg ffprobe convert pytest ruff mypy uv uvx pyright tsc vitest jest eslint prettier vercel convex stripe""".split())
_SKIP_LEAD = {"cd", "export", "set", "source", ".", "sudo", "env", "time", "nice", "timeout", "exec", "unset"}
_AREAS = (("/.hermes/skills", "skills"), ("/.hermes/scripts", "scripts"), ("/.hermes/memories", "memories"),
          ("config.yaml", "config"), ("/projects/", "projects"), ("/tmp", "tmp"))
_USER_SKIP = ("[IMPORTANT:", "[System:", "[System note", "[INTERNAL NOTIFICATION", "[CONTEXT COMPACTION", "[SYSTEM:")
_STAMP = re.compile(r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d\d-\d\d [\d:]+ [A-Z]{2,4}\]\s*")


# ---- users -------------------------------------------------------------------------------------
def clean_user(text: str | None) -> str | None:
    t = _STAMP.sub("", (text or "").strip())
    if not t:
        return None
    if "CONTEXT COMPACTION" in t[:160] or "REFERENCE ONLY" in t[:200]:
        return None
    if t.startswith("[OUT-OF-BAND USER MESSAGE"):
        t = re.sub(r"^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*", "", t)
        t = t.split("[/OUT-OF-BAND USER MESSAGE]")[0].strip()
    if t.startswith(_USER_SKIP):
        return None
    t = t.split("--- Attached Context ---")[0]
    t = re.sub(r"\[Image attached[^\]]*\]|@(?:file|image|session):\S+", " ", t)
    # A quoted reply is the *assistant's* text echoed back; it is never the human's request and often longer than it.
    t = re.sub(r'^\[Replying to: ".*?"\]\s*', "", t, flags=re.S)
    if t.startswith("[Replying to:"):
        t = re.sub(r"^\[Replying to:.*?\]\s*", "", t, count=1, flags=re.S)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


# ---- calls -------------------------------------------------------------------------------------
def _cmd_digest(cmd: str) -> str:
    segs = [s.strip() for s in re.split(r"&&|;|\|\||\|", cmd or "") if s.strip()]
    pick = next((s for s in segs if s.split()[0].split("=")[0] not in _SKIP_LEAD and "=" not in s.split()[0]), segs[0] if segs else "")
    parts = pick.split()
    if not parts:
        return ""
    first = parts[0].rsplit("/", 1)[-1]
    if first not in _KNOWN_CMDS:
        return ("cmd #" + h(first, 6) + ("+amp" if re.search(r"(?:^|\s)&\s*$|\bnohup\b|\bsetsid\b|\bdisown\b", cmd or "") else ""))[:48]
    out = first
    if first in _SUBCMD and len(parts) > 1 and not parts[1].startswith("-"):
        out = f"{first} {parts[1]}"
    elif first.startswith("python") and len(parts) > 1 and not parts[1].startswith("-"):
        out = "python #" + h(parts[1].rsplit("/", 1)[-1], 6)  # script names say what was being worked on; equality is all we need
    flags = ""
    if re.search(r"(?:^|\s)&\s*$|\bnohup\b|\bsetsid\b|\bdisown\b", cmd or ""):
        flags += "+amp"
    return (out + flags)[:48]


def _path_digest(path: str) -> str:
    p = path or ""
    ext = ""
    m = re.search(r"(\.[A-Za-z0-9]{1,6})$", p)
    if m:
        ext = m.group(1).lower()
    area = next((a for frag, a in _AREAS if frag in p), "")
    return (ext + ("@" + area if area else ""))[:32]


def arg_digest(name: str, args: dict | None, raw: str = "") -> str:
    a = args or {}
    try:
        if name == "terminal":
            cmd = str(a.get("command") or "")
            if not cmd and raw:
                m = re.search(r'"command"\s*:\s*"((?:[^"\\]|\\.){0,200})', raw)
                cmd = m.group(1).replace("\\n", "; ") if m else ""
            d = _cmd_digest(cmd)
            if a.get("background") or re.search(r'"background"\s*:\s*true', raw or ""):
                d += "+bgflag"
            return d
        if name in ("read_file", "write_file", "patch", "search_files"):
            path = str(a.get("path") or a.get("file_path") or "")
            if not path and raw:
                m = re.search(r'"(?:path|file_path)"\s*:\s*"([^"]+)"', raw)
                path = m.group(1) if m else ""
            return _path_digest(path)
        if name == "skill_manage":
            act = str(a.get("action") or "")
            if not act and raw:
                m = re.search(r'"action"\s*:\s*"([a-z_]+)"', raw)
                act = m.group(1) if m else ""
            return act
        if name == "skill_view":
            return "#" + h(str(a.get("name") or ""), 6)  # which skill was consulted can reveal a topic; keep only equality
        if name == "execute_code":
            return "py"
    except Exception:
        return ""
    return ""


def parse_calls(raw: str | None) -> list[tuple[str, dict | None, str]]:
    """Return [(tool, args_or_None, raw_args)] from an assistant message's tool_calls JSON (tolerates truncation)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        out = []
        for c in data if isinstance(data, list) else []:
            fn = c.get("function") or c
            name = fn.get("name") or c.get("name")
            a = fn.get("arguments", c.get("arguments"))
            args, rawa = None, ""
            if isinstance(a, str):
                rawa = a
                try:
                    args = json.loads(a)
                except ValueError:
                    args = None
            elif isinstance(a, dict):
                args = a
            if name:
                out.append((name, args if isinstance(args, dict) else None, rawa))
        return out
    except ValueError:
        pass
    out = []
    for m in re.finditer(r'"function"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"(.{0,600})', raw, re.S):
        out.append((m.group(1), None, m.group(2)))
    return out


# ---- results -----------------------------------------------------------------------------------
_EXIT_RX = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
_ERR_FIELD = re.compile(r'"error"\s*:\s*"((?:[^"\\]|\\.){2,400})"')
_OUT_FIELD = re.compile(r'"output"\s*:\s*"((?:[^"\\]|\\.){0,1500})')
_TEXT_FAIL = re.compile(r"^\s*(?:error\b|failed\b|refusing\b|traceback|fatal\b|exception\b|\[command timed out|command timed out)", re.I)
_TB = re.compile(r"Traceback \(most recent call last\)")


def _unescape(s: str) -> str:
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s.replace("\\n", "\n").replace('\\"', '"')


def classify_result(tool: str, content: str | None) -> tuple[bool, str]:
    """Return (ok, message). Message is the best one-line error text when not ok."""
    c = content or ""
    head = c[:400]
    if '"bytes_written"' in c[:400] or '"verified": true' in c[:600] or '"verified":true' in c[:600]:
        return True, ""  # a landed write is a success even when it reports lint notes
    em = _ERR_FIELD.search(c)
    if em:
        return False, _unescape(em.group(1))
    xm = _EXIT_RX.search(c)
    if xm and int(xm.group(1)) != 0:
        om = _OUT_FIELD.search(c)
        text = _unescape(om.group(1)) if om else ""
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        tail = c[-600:]
        if _TB.search(c):
            tl = [x.strip() for x in tail.replace("\\n", "\n").splitlines() if x.strip()]
            return False, (tl[-1] if tl else "traceback")
        return False, (lines[-1] if lines else f"exit {xm.group(1)}")
    if re.search(r'"success"\s*:\s*false', c[:1500]) or re.search(r'"status"\s*:\s*"(?:error|failed)"', c[:1500]):
        mm = re.search(r'"(?:message|detail|reason|error_message|hint)"\s*:\s*"((?:[^"\\]|\\.){4,300})"', c)
        return False, (_unescape(mm.group(1)) if mm else "reported failure")
    if tool != "terminal" and _TEXT_FAIL.search(head):
        return False, head.strip().splitlines()[0]
    if tool == "terminal" and not xm and _TEXT_FAIL.search(head):
        return False, head.strip().splitlines()[0]
    if "Foreground command uses '&' backgrounding" in c:
        return False, "foreground command uses & backgrounding"
    return True, ""


# ---- session -> atoms --------------------------------------------------------------------------
HUMAN_SOURCES = frozenset({"telegram", "desktop", "cli", "tui", "buzz", "grokbot", "discord", "slack", "whatsapp", "signal", "email"})


def distill_session(messages: list[dict], blocklist=(), human: bool = True) -> list[dict]:
    """human=False (cron, subagent, delegated task): the "user" turns are instructions, so they can never be corrections."""
    atoms: list[dict] = []
    id2name: dict[str, str] = {}
    ents_seen: set[str] = set()
    for m in messages:
        ts = m.get("timestamp") or 0.0
        role = m.get("role")
        if role == "user":
            txt = clean_user(m.get("content"))
            if not txt:
                continue
            if is_sensitive(txt, blocklist):
                # keep that a request happened, never what it said
                atoms.append(dict(kind="ask", key="sensitive", sig="", text="", ts=ts))
                continue
            red = redact(txt)
            # Never store a readable sentence. Clustering only needs words, so keep an unordered bag of stemmed
            # topic words: enough to recognise "that kind of task again", not enough to read what was said.
            bag = " ".join(sorted(set(tokens(red)[:40])))
            if human and len(txt) <= 300 and CORRECTION_RX.search(txt):
                atoms.append(dict(kind="correction", key="", sig="", text=bag, ts=ts))
            elif human and len(txt) < 80 and APPROVAL_RX.search(txt):
                atoms.append(dict(kind="approval", key="", sig="", text="", ts=ts))
            atoms.append(dict(kind="ask", key="", sig="", text=bag, ts=ts))
        elif role == "assistant" and m.get("tool_calls"):
            for name, args, raw in parse_calls(m["tool_calls"]):
                atoms.append(dict(kind="call", key=name, sig=arg_digest(name, args, raw), text="", ts=ts))
                if name in ("skill_view", "skill_manage"):
                    sk = (args or {}).get("name") or ""
                    if sk and sk not in ents_seen and not is_sensitive(sk, blocklist):
                        ents_seen.add(sk)
                        atoms.append(dict(kind="entity", key=f"skill:{sk[:40]}", sig="", text="", ts=ts))
                # Websites are deliberately NOT recorded: where someone browsed is private and nothing here needs it.
            tcs = parse_calls(m["tool_calls"])
            try:
                for c in json.loads(m["tool_calls"]):
                    if isinstance(c, dict) and c.get("id"):
                        id2name[c["id"]] = (c.get("function") or c).get("name", "")
            except ValueError:
                pass
            _ = tcs
        elif role == "tool":
            tool = m.get("tool_name") or id2name.get(m.get("tool_call_id") or "", "") or "tool"
            ok, msg = classify_result(tool, m.get("content"))
            if ok:
                atoms.append(dict(kind="ok", key=tool, sig="", text="", ts=ts))
            else:
                norm = normalize_error(redact(msg))
                if is_sensitive(norm, blocklist, credentials=False):  # tool errors sometimes echo the content they choked on
                    norm = ""
                atoms.append(dict(kind="fail", key=tool, sig=signature(redact(msg)), text=norm, ts=ts))
    return atoms
