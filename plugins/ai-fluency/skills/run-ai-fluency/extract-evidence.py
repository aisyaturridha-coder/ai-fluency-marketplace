#!/usr/bin/env python3
"""
Distil local Claude Code transcripts into a compact evidence pack.

Reads ~/.claude/projects/**/*.jsonl and emits aggregated behavioural metrics —
never raw conversation. Prompt text appears only as short, redacted samples,
capped and truncated, so the pack can be inspected before it is sent anywhere.

  ./extract-evidence.py                    # -> evidence.json
  ./extract-evidence.py --days 90          # recent window only
  ./extract-evidence.py --no-samples       # metrics only, zero prompt text
  ./extract-evidence.py -o /tmp/pack.json
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import re
import sys

# --- redaction ---------------------------------------------------------------
# Applied to every sampled string before it can reach the output file. Ordered
# most-specific first so a key is never partially matched by a looser pattern.
SECRET_PATTERNS = [
    # Whole private-key blocks first: once the header matches, take the lot.
    (re.compile(r"-----BEGIN[^-]{0,40}PRIVATE KEY-----[\s\S]*?-----END[^-]{0,40}PRIVATE KEY-----"),
     "[PRIVATE-KEY]"),
    (re.compile(r"-----BEGIN[^-]{0,40}PRIVATE KEY-----"), "[PRIVATE-KEY]"),
    # Cloud and payment credentials. These are high-signal prefixes, so matching
    # them is safe; a false positive only costs a mangled sample.
    (re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|APKA)[A-Z0-9]{12,}"), "[AWS-KEY-ID]"),
    (re.compile(r"(?i)\b(aws_secret_access_key|aws_session_token)\b\s*[=:]\s*\S+"),
     r"[AWS-SECRET]"),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"), "[STRIPE-KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[SLACK-TOKEN]"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"), "[GOOGLE-KEY]"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{16,}"), "[API-KEY]"),
    # A credential assigned to an obviously-named variable, whatever its shape.
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|"
                r"auth[_-]?token|private[_-]?key)\b\s*[=:]\s*[\"']?[^\s\"',;]{6,}"),
     r"[REDACTED-ASSIGNMENT]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[API-KEY]"),
    (re.compile(r"(?:github_pat|ghp|gho|ghs|ghu)_[A-Za-z0-9_]{16,}"), "[GH-TOKEN]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"), "[JWT]"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:sbp|sb|supabase)_[A-Za-z0-9_]{16,}"), "[SB-TOKEN]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[HEX]"),
    (re.compile(r"\+?\d[\d\s\-]{8,}\d"), "[PHONE]"),
]


def redact(s: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


# --- classification vocabularies ---------------------------------------------
# Bash verbs are bucketed into practice areas. A session's technical surface is
# inferred from which buckets it touches, not from what the user claims to know.
VERB_DOMAIN = {
    "git": "version-control", "gh": "version-control",
    "npm": "js-build", "pnpm": "js-build", "yarn": "js-build", "bun": "js-build",
    "npx": "js-build", "node": "js-build", "tsc": "js-build", "vite": "js-build",
    "next": "js-build", "eslint": "js-build", "prettier": "js-build",
    "python3": "python", "python": "python", "pip": "python", "pip3": "python",
    "uv": "python", "pytest": "testing", "jest": "testing", "vitest": "testing",
    "playwright": "testing", "cypress": "testing",
    "psql": "database", "sqlite3": "database", "supabase": "database",
    "prisma": "database", "mysql": "database", "pg_dump": "database",
    "docker": "infra", "kubectl": "infra", "terraform": "infra",
    "colima": "infra", "systemctl": "infra", "launchctl": "infra",
    "wrangler": "edge-deploy", "vercel": "edge-deploy", "netlify": "edge-deploy",
    "fly": "edge-deploy", "cloudflared": "edge-deploy",
    "curl": "http-api", "wget": "http-api", "http": "http-api",
    "xcodebuild": "mobile", "pod": "mobile", "swift": "mobile",
    "expo": "mobile", "eas": "mobile", "adb": "mobile",
    "ffmpeg": "media", "convert": "media", "magick": "media",
    "jq": "data-wrangling", "awk": "data-wrangling", "sed": "data-wrangling",
    "sort": "data-wrangling", "uniq": "data-wrangling", "csvlook": "data-wrangling",
}

EXT_DOMAIN = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".py": "python", ".rb": "ruby", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".swift": "swift", ".m": "objc",
    ".sql": "sql", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".css": "styling", ".scss": "styling", ".html": "markup",
    ".md": "docs", ".mdx": "docs", ".txt": "docs",
    ".json": "config", ".yaml": "config", ".yml": "config", ".toml": "config",
    ".env": "config", ".lock": "config",
}

# Commands whose presence means the session actually checked its own work.
VERIFY_RE = re.compile(
    r"\b(test|tests|pytest|jest|vitest|playwright|lint|eslint|typecheck|tsc|"
    r"build|gate|check|verify|audit|coverage)\b"
)
COMMIT_RE = re.compile(r"\bgit\s+(commit|push|tag)\b|\bgh\s+pr\s+create\b")
DEPLOY_RE = re.compile(r"\b(wrangler\s+deploy|vercel\s+(deploy|--prod)|"
                       r"eas\s+(build|submit)|fly\s+deploy|supabase\s+db\s+push)\b")

# Malay stopwords that rarely appear in English prose — enough to tag a prompt's
# language without pulling in a detection library.
BM_MARKERS = re.compile(
    r"\b(yang|untuk|dengan|saya|awak|boleh|tak|nak|dah|kena|macam|dalam|"
    r"jangan|kalau|sudah|belum|buat|jadi|bagi|ni|tu|je|pun)\b", re.I)

# Signals the user pushed back, redirected, or caught a mistake. High rates mean
# active steering; near-zero means either flawless prompting or passive acceptance.
CORRECTION_RE = re.compile(
    r"\b(bukan|salah|tak betul|tak jalan|silap|jangan|patah balik|"
    r"no,|nope|wrong|that'?s not|actually|revert|undo|redo|"
    r"why did you|you missed|still broken|doesn'?t work)\b", re.I)

# Signals the user specified constraints up front rather than discovering them.
SPEC_RE = re.compile(
    r"\b(jangan|mesti|kena|pastikan|guna|ikut|constraint|must|should not|"
    r"do not|make sure|only|instead of|requirement|acceptance|criteria)\b", re.I)

INTERRUPT_RE = re.compile(r"\[Request interrupted by user", re.I)

# Tools that gather context versus tools that change the world. The ratio of
# gathering-before-changing is a proxy for how well the task was briefed.
RECON_TOOLS = {"Read", "Grep", "Glob", "Bash", "ToolSearch", "WebFetch",
               "WebSearch", "NotebookRead"}
MUTATION_TOOLS = {"Edit", "Write", "NotebookEdit"}

# Agent messages after a failed tool call within which a human turn still counts
# as intervening. Beyond it, the agent recovered on its own.
ERR_TAKEOVER_WINDOW = 3

# USD per million tokens, (input, output), as published 2026-07-27. Cache reads
# bill at 0.1x input and 5-minute cache writes at 1.25x input.
#
# These produce an API-EQUIVALENT cost. Claude Code usage on a subscription is
# not billed this way — the figure answers "what would this have cost through
# the API", which is what makes it comparable across sessions and models.
PRICING = {
    "claude-fable-5":    (10.0, 50.0),
    "claude-mythos-5":   (10.0, 50.0),
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-opus-4-5":   (5.0, 25.0),
    "claude-sonnet-5":   (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def price_of(model: str, usage: dict) -> float:
    """API-equivalent USD for one model's token usage. Unknown models cost 0."""
    rate = PRICING.get(model)
    if not rate:
        return 0.0
    inp, out = rate
    return (
        usage["in"] * inp
        + usage["out"] * out
        + usage["cache_read"] * inp * CACHE_READ_MULT
        + usage["cache_write"] * inp * CACHE_WRITE_MULT
    ) / 1_000_000

# Quoted strings are stripped before a command is classified, so that
# `git commit -m "fix the test"` is not scored as having run tests.
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# A gap longer than this between two events means the user walked away and came
# back; sessions get resumed across days, so raw first-to-last span is not work time.
IDLE_GAP_MIN = 30.0


def head_verb(cmd: str) -> str:
    """First real executable in a command line, skipping env assignments and cd."""
    for part in re.split(r"\s*(?:\|\||&&|;|\|)\s*", cmd.strip()):
        for tok in part.split():
            if "=" in tok and not tok.startswith("-"):
                continue  # FOO=bar prefix
            if tok in ("sudo", "time", "env", "exec", "nohup"):
                continue
            if tok == "cd":
                break  # look at the next segment instead
            return os.path.basename(tok).lower()
    return ""


class SessionStats:
    __slots__ = ("sid", "project", "cwd", "branch", "start", "end", "user_turns",
                 "assistant_turns", "sidechain_turns", "tools", "bash_verbs",
                 "domains", "exts", "skills", "subagents", "mcp_servers", "models",
                 "modes", "entrypoints", "versions", "thinking_chars", "thinking_blocks",
                 "parallel_batches", "max_batch", "in_tokens", "out_tokens",
                 "cache_read", "prompt_lens", "corrections", "spec_prompts",
                 "bm_prompts", "interrupts", "verified", "committed", "deployed",
                 "errors", "first_prompt", "plan_mode", "active_min", "_last_ts",
                 "_msg_ids", "_batch",
                 # --- collaboration signals: how the operator works *an agent*,
                 # as opposed to how they work a codebase ---
                 "runways", "recon_before_mut", "steer_lat", "tool_errors",
                 "err_took_over", "err_self_healed", "_recon", "_hit_mut",
                 "_tools_since_human", "_msgs_since_human", "_err_age",
                 "model_tokens", "cache_write")

    def __init__(self, sid: str):
        self.sid = sid
        self.project = self.cwd = self.branch = ""
        self.start = self.end = None
        self.active_min = 0.0
        self._last_ts = None
        self._msg_ids: set[str] = set()
        self._batch: tuple[str, int] = ("", 0)  # (message.id, tool_uses so far)
        self.user_turns = self.assistant_turns = self.sidechain_turns = 0
        self.tools = collections.Counter()
        self.bash_verbs = collections.Counter()
        self.domains = collections.Counter()
        self.exts = collections.Counter()
        self.skills = collections.Counter()
        self.subagents = collections.Counter()
        self.mcp_servers = collections.Counter()
        self.models = collections.Counter()
        self.modes = collections.Counter()
        self.entrypoints = collections.Counter()
        self.versions = set()
        self.thinking_chars = self.thinking_blocks = 0
        self.parallel_batches = self.max_batch = 0
        self.in_tokens = self.out_tokens = self.cache_read = self.cache_write = 0
        # model -> {in, out, cache_read, cache_write}; needed because pricing
        # differs per model and the session mixes them.
        self.model_tokens: dict[str, dict[str, int]] = {}
        self.prompt_lens: list[int] = []
        self.corrections = self.spec_prompts = self.bm_prompts = 0
        self.interrupts = self.errors = 0
        self.verified = self.committed = self.deployed = self.plan_mode = False
        self.first_prompt = ""

        # Collaboration signals. `runways` is the core one: how many tool calls
        # the agent gets to make on each instruction before the human speaks
        # again. Short runways mean babysitting; very long ones mean the agent
        # ran unsupervised. Neither extreme is skill.
        self.runways: list[int] = []
        self.recon_before_mut = -1     # -1 = session never reached a mutation
        self.steer_lat: list[int] = []
        self.tool_errors = self.err_took_over = self.err_self_healed = 0
        self._recon = 0
        self._hit_mut = False
        self._tools_since_human = 0
        self._msgs_since_human = 0
        self._err_age = None   # agent messages since the last failed tool call

    def span_days(self) -> float:
        """Calendar reach of the session, including time it sat resumed but idle."""
        if not (self.start and self.end):
            return 0.0
        return round((self.end - self.start).total_seconds() / 86400, 2)

    def note_time(self, ts) -> None:
        """Accumulate hands-on minutes, ignoring gaps where the user was away."""
        if ts is None:
            return
        if self._last_ts is not None and ts >= self._last_ts:
            gap = (ts - self._last_ts).total_seconds() / 60
            if gap <= IDLE_GAP_MIN:
                self.active_min += gap
        self._last_ts = ts

    def close_batch(self) -> None:
        """Record the finished message's tool-call fan-out, then reset."""
        _, count = self._batch
        if count > 1:
            self.parallel_batches += 1
            self.max_batch = max(self.max_batch, count)
        self._batch = ("", 0)


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def text_of(content) -> str:
    """Flatten a message content field to plain text, whatever shape it is in."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(b.get("text") or "")
            elif b.get("type") == "tool_result":
                c = b.get("content")
                out.append(c if isinstance(c, str) else text_of(c))
        return "\n".join(out)
    return ""


def scan_file(path: pathlib.Path, cutoff, sessions: dict) -> None:
    # Transcripts are UTF-8. Without the explicit encoding, Windows falls back
    # to the ANSI code page (cp1252) and silently mangles every non-ASCII prompt.
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue

            typ = d.get("type")
            if typ not in ("user", "assistant"):
                continue

            ts = parse_ts(d.get("timestamp") or "")
            if cutoff and ts and ts < cutoff:
                continue

            sid = d.get("sessionId") or path.stem
            s = sessions.get(sid)
            if s is None:
                s = sessions[sid] = SessionStats(sid)

            if ts:
                s.start = ts if s.start is None or ts < s.start else s.start
                s.end = ts if s.end is None or ts > s.end else s.end
                s.note_time(ts)

            if not s.cwd and d.get("cwd"):
                s.cwd = d["cwd"]
                s.project = os.path.basename(d["cwd"].rstrip("/")) or "root"
            if d.get("gitBranch"):
                s.branch = d["gitBranch"]
            if d.get("version"):
                s.versions.add(d["version"])
            if d.get("entrypoint"):
                s.entrypoints[d["entrypoint"]] += 1
            if d.get("permissionMode"):
                s.modes[d["permissionMode"]] += 1
                if d["permissionMode"] == "plan":
                    s.plan_mode = True

            sidechain = bool(d.get("isSidechain"))
            msg = d.get("message") or {}
            content = msg.get("content")

            if typ == "user":
                s.close_batch()  # a user turn ends whatever the model was doing
                if sidechain:
                    s.sidechain_turns += 1
                    continue

                # A tool_result carrying is_error is the harness reporting a
                # failed call, not the human speaking. What happens on the *next*
                # event decides whether the operator took over or let the agent
                # recover on its own.
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("is_error") for b in content):
                    s.tool_errors += 1
                    s._err_age = 0

                body = text_of(content)
                if INTERRUPT_RE.search(body):
                    s.interrupts += 1
                    s.steer_lat.append(s._msgs_since_human)
                # Real user prompts are strings or text blocks; tool_result-only
                # entries are the harness replying to itself, not the human.
                is_human = isinstance(content, str) or (
                    isinstance(content, list)
                    and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
                )
                if is_human and body.strip():
                    s.user_turns += 1
                    s.prompt_lens.append(len(body))
                    # Read the counters for the instruction that just ended
                    # BEFORE resetting them for the new one.
                    elapsed = s._msgs_since_human
                    if s._tools_since_human:
                        s.runways.append(s._tools_since_human)
                    # Stepping in within a few agent messages of a failure counts
                    # as taking over; letting it grind past that is self-heal.
                    if s._err_age is not None:
                        s.err_took_over += 1
                        s._err_age = None
                    s._tools_since_human = 0
                    s._msgs_since_human = 0
                    if not s.first_prompt:
                        s.first_prompt = body.strip()[:220]
                    if CORRECTION_RE.search(body):
                        s.corrections += 1
                        s.steer_lat.append(elapsed)
                    if SPEC_RE.search(body):
                        s.spec_prompts += 1
                    if len(BM_MARKERS.findall(body)) >= 2:
                        s.bm_prompts += 1
                continue

            # assistant. One API message is written out as several records — one
            # per content block — so turns are counted by distinct message id,
            # and a message's tool_use blocks are tallied across those records.
            mid = msg.get("id") or ""
            if mid and mid != s._batch[0]:
                s.close_batch()
                s._batch = (mid, 0)
            if sidechain:
                s.sidechain_turns += 1
            elif mid:
                if mid not in s._msg_ids:
                    s._msg_ids.add(mid)
                    s.assistant_turns += 1
                    s._msgs_since_human += 1
            else:
                s.assistant_turns += 1
                s._msgs_since_human += 1

            # The agent carried on after a failed call without being told to.
            # Only once it has pushed past a few messages unaided does this
            # count as a genuine self-heal rather than a reflex reply.
            if s._err_age is not None and not sidechain:
                s._err_age += 1
                if s._err_age > ERR_TAKEOVER_WINDOW:
                    s.err_self_healed += 1
                    s._err_age = None

            if msg.get("model"):
                s.models[msg["model"]] += 1
            usage = msg.get("usage") or {}
            u_in = usage.get("input_tokens") or 0
            u_out = usage.get("output_tokens") or 0
            u_cr = usage.get("cache_read_input_tokens") or 0
            u_cw = usage.get("cache_creation_input_tokens") or 0
            s.in_tokens += u_in
            s.out_tokens += u_out
            s.cache_read += u_cr
            s.cache_write += u_cw
            if msg.get("model") and (u_in or u_out or u_cr or u_cw):
                mt = s.model_tokens.setdefault(
                    msg["model"], {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
                mt["in"] += u_in
                mt["out"] += u_out
                mt["cache_read"] += u_cr
                mt["cache_write"] += u_cw

            if not isinstance(content, list):
                continue

            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    s.thinking_blocks += 1
                    s.thinking_chars += len(b.get("thinking") or "")
                elif bt == "tool_use":
                    s._batch = (s._batch[0], s._batch[1] + 1)
                    name = b.get("name") or "?"
                    s.tools[name] += 1
                    s._tools_since_human += 1
                    # How much hunting the agent had to do before it could act.
                    # A high count means it was under-briefed and had to
                    # reconstruct context the operator already had.
                    if not s._hit_mut:
                        if name in MUTATION_TOOLS:
                            s.recon_before_mut = s._recon
                            s._hit_mut = True
                        elif name in RECON_TOOLS:
                            s._recon += 1
                    inp = b.get("input") or {}
                    if not isinstance(inp, dict):
                        continue

                    if name.startswith("mcp__"):
                        parts = name.split("__")
                        if len(parts) > 1:
                            s.mcp_servers[parts[1]] += 1
                    elif name == "Bash":
                        cmd = str(inp.get("command") or "")
                        v = head_verb(cmd)
                        if v:
                            s.bash_verbs[v] += 1
                            s.domains[VERB_DOMAIN.get(v, "shell-misc")] += 1
                        # Classify on the unquoted command only: a commit message
                        # mentioning "test" is not a test run.
                        bare = QUOTED_RE.sub(" ", cmd)
                        if VERIFY_RE.search(bare):
                            s.verified = True
                        if COMMIT_RE.search(bare):
                            s.committed = True
                        if DEPLOY_RE.search(bare):
                            s.deployed = True
                    elif name == "Skill":
                        s.skills[str(inp.get("skill") or "?")] += 1
                    elif name in ("Agent", "Task"):
                        s.subagents[str(inp.get("subagent_type") or "general")] += 1
                    elif name == "Workflow":
                        s.subagents["__workflow__"] += 1

                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(fp, str):
                        ext = os.path.splitext(fp)[1].lower()
                        if ext:
                            s.exts[ext] += 1
                            if ext in EXT_DOMAIN:
                                s.domains[EXT_DOMAIN[ext]] += 1



def pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def build_pack(sessions: dict, days, include_samples: bool, sample_n: int) -> dict:
    for s in sessions.values():
        s.close_batch()  # flush the final message of each transcript
    # Sessions with no human turn are resumed/aborted shells, not real work.
    real = [s for s in sessions.values() if s.user_turns > 0]
    real.sort(key=lambda s: (s.start or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    if not real:
        return {"error": "no sessions found in window"}

    tot_tools = collections.Counter()
    tot_verbs = collections.Counter()
    tot_domains = collections.Counter()
    tot_exts = collections.Counter()
    tot_skills = collections.Counter()
    tot_agents = collections.Counter()
    tot_mcp = collections.Counter()
    tot_models = collections.Counter()
    tot_modes = collections.Counter()
    tot_entry = collections.Counter()
    per_project: dict[str, dict] = {}
    monthly: dict[str, collections.Counter] = {}

    for s in real:
        tot_tools += s.tools
        tot_verbs += s.bash_verbs
        tot_domains += s.domains
        tot_exts += s.exts
        tot_skills += s.skills
        tot_agents += s.subagents
        tot_mcp += s.mcp_servers
        tot_models += s.models
        tot_modes += s.modes
        tot_entry += s.entrypoints

        p = per_project.setdefault(s.project or "unknown", {
            "sessions": 0, "user_turns": 0, "minutes": 0.0, "tool_calls": 0,
            "verified": 0, "committed": 0, "deployed": 0, "corrections": 0,
            "out_tokens": 0, "domains": collections.Counter(),
            "first": None, "last": None, "branches": set(),
        })
        p["sessions"] += 1
        p["user_turns"] += s.user_turns
        p["minutes"] += s.active_min
        p["tool_calls"] += sum(s.tools.values())
        p["verified"] += int(s.verified)
        p["committed"] += int(s.committed)
        p["deployed"] += int(s.deployed)
        p["corrections"] += s.corrections
        p["out_tokens"] += s.out_tokens
        p["domains"] += s.domains
        if s.branch:
            p["branches"].add(s.branch)
        if s.start:
            p["first"] = min(p["first"] or s.start, s.start)
            p["last"] = max(p["last"] or s.start, s.start)
            key = s.start.strftime("%Y-%m")
            m = monthly.setdefault(key, collections.Counter())
            m["sessions"] += 1
            m["user_turns"] += s.user_turns
            m["tool_calls"] += sum(s.tools.values())
            m["subagent_calls"] += sum(s.subagents.values())
            m["skill_calls"] += sum(s.skills.values())
            m["verified_sessions"] += int(s.verified)
            m["planned_sessions"] += int(s.plan_mode)

    n = len(real)
    turns = sorted(s.user_turns for s in real)
    mins = sorted(s.active_min for s in real)
    spans = sorted(s.span_days() for s in real)
    plens = sorted(x for s in real for x in s.prompt_lens)
    # Pasted logs and specs are not "prompts" in any meaningful sense; keeping
    # them in the distribution would drag every percentile upward.
    typed = [x for x in plens if x <= 5000]

    def q(xs, f):
        return xs[min(len(xs) - 1, int(len(xs) * f))] if xs else 0

    span_start = real[0].start
    span_end = max((s.end or s.start) for s in real if s.start)

    projects_out = []
    for name, p in sorted(per_project.items(), key=lambda kv: -kv[1]["sessions"])[:25]:
        projects_out.append({
            # Derived from a directory name, so it can carry PII — a cloud-drive
            # mount named after the account email is the usual offender.
            "project": redact(name),
            "sessions": p["sessions"],
            "user_turns": p["user_turns"],
            "hours": round(p["minutes"] / 60, 1),
            "tool_calls": p["tool_calls"],
            "turns_per_session": round(p["user_turns"] / p["sessions"], 1),
            "verify_rate_pct": pct(p["verified"], p["sessions"]),
            "commit_rate_pct": pct(p["committed"], p["sessions"]),
            "deploy_sessions": p["deployed"],
            "corrections_per_session": round(p["corrections"] / p["sessions"], 2),
            "top_domains": [k for k, _ in p["domains"].most_common(6)],
            "branches": len(p["branches"]),
            "first_seen": p["first"].date().isoformat() if p["first"] else None,
            "last_seen": p["last"].date().isoformat() if p["last"] else None,
            "share_of_all_sessions_pct": pct(p["sessions"], n),
        })

    # --- 4D collaboration signals -------------------------------------------
    # Grouped against Anthropic's AI Fluency framework (Delegation, Description,
    # Discernment, Diligence) rather than invented categories, so the assessment
    # maps to an external rubric instead of a private one.
    runways = sorted(x for s in real for x in s.runways)
    recon = sorted(s.recon_before_mut for s in real if s.recon_before_mut >= 0)
    steer = sorted(x for s in real for x in s.steer_lat)
    tot_err = sum(s.tool_errors for s in real)
    took_over = sum(s.err_took_over for s in real)
    healed = sum(s.err_self_healed for s in real)
    generic = sum(v for k, v in tot_agents.items()
                  if k in ("general-purpose", "general"))
    purpose = sum(tot_agents.values()) - generic

    four_d = {
        "_framework": "Anthropic AI Fluency 4D (Delegation, Description, "
                      "Discernment, Diligence). Signals below are behavioural "
                      "proxies observable in transcripts, not direct measures.",
        "delegation": {
            "runway_tool_calls_per_instruction": {
                "median": q(runways, 0.5), "p75": q(runways, 0.75),
                "p90": q(runways, 0.9), "max": runways[-1] if runways else 0,
                "note": "how far the agent gets on one instruction before the human speaks again",
            },
            "babysat_instructions_pct": pct(sum(1 for x in runways if x <= 2), len(runways)),
            "long_leash_instructions_pct": pct(sum(1 for x in runways if x >= 20), len(runways)),
            "subagent_calls": sum(tot_agents.values()),
            "purpose_built_vs_generic_subagents": {
                "purpose_built": purpose, "generic": generic,
                "note": "generic dominance suggests built tooling is not reachable at the moment of need",
            },
            "model_mix": dict(tot_models.most_common(6)),
        },
        "description": {
            "typed_prompt_chars_median": q(typed, 0.5),
            "short_prompts_under_40_chars_pct": pct(
                sum(1 for x in typed if x < 40), len(typed)),
            "prompts_with_explicit_constraints_pct": pct(
                sum(s.spec_prompts for s in real), len(plens)),
            "recon_calls_before_first_edit": {
                "median": q(recon, 0.5), "p75": q(recon, 0.75), "p90": q(recon, 0.9),
                "sessions_measured": len(recon),
                "note": "context the agent had to reconstruct itself; high = under-briefed",
            },
            "raw_material_supplied_pastes": len(plens) - len(typed),
        },
        "discernment": {
            "steering_latency_agent_msgs": {
                "median": q(steer, 0.5), "p75": q(steer, 0.75), "p90": q(steer, 0.9),
                "events": len(steer),
                "note": "agent messages elapsed before the human corrected or interrupted",
            },
            "correction_turns_pct": pct(sum(s.corrections for s in real), len(plens)),
            "interrupt_events": sum(s.interrupts for s in real),
            "tool_errors": tot_err,
            "human_took_over_after_error": took_over,
            "agent_self_healed_after_error": healed,
            "takeover_share_pct": pct(took_over, took_over + healed),
            "sessions_verifying_agent_work_pct": pct(
                sum(1 for s in real if s.verified), n),
        },
        "diligence": {
            "permission_modes_turn_weighted": dict(tot_modes.most_common()),
            "plan_mode_sessions_pct": pct(sum(1 for s in real if s.plan_mode), n),
            "verified_and_committed_pct": pct(
                sum(1 for s in real if s.verified and s.committed), n),
            "deployed_without_commit_sessions": sum(
                1 for s in real if s.deployed and not s.committed),
            "gate_skill_invocations": sum(
                v for k, v in tot_skills.items() if "preflight" in k or "review" in k),
        },
    }

    # --- cost, and what the spend bought ----------------------------------
    by_model: dict[str, dict[str, int]] = {}
    for s in real:
        for m, u in s.model_tokens.items():
            agg = by_model.setdefault(
                m, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
            for k in agg:
                agg[k] += u[k]

    model_cost = {m: round(price_of(m, u), 2) for m, u in by_model.items()}
    total_cost = round(sum(model_cost.values()), 2)
    known_cost = round(sum(v for m, v in model_cost.items() if m in PRICING), 2)
    priced_sessions = [s for s in real if any(m in PRICING for m in s.model_tokens)]
    landed = sum(1 for s in real if s.committed)

    # Sessions whose spend produced no landed change. Not waste by itself —
    # research and reading sessions should not commit — but it is the pool any
    # efficiency gain would come out of.
    unlanded_cost = round(sum(
        sum(price_of(m, u) for m, u in s.model_tokens.items())
        for s in real if not s.committed), 2)

    heavy = {"claude-fable-5", "claude-mythos-5"}
    heavy_cost = round(sum(v for m, v in model_cost.items() if m in heavy), 2)
    cheap = {"claude-haiku-4-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5"}
    cheap_cost = round(sum(v for m, v in model_cost.items() if m in cheap), 2)

    tot_in = sum(s.in_tokens for s in real)
    tot_cr = sum(s.cache_read for s in real)
    tot_cw = sum(s.cache_write for s in real)

    cost = {
        "_basis": (
            "API-equivalent USD at list prices as of 2026-07-27, computed locally. "
            "Claude Code on a subscription is NOT billed this way — this answers "
            "'what would this usage have cost through the API', which is what makes "
            "sessions and models comparable. Do not report it as money spent."
        ),
        "total_usd": total_cost,
        "priced_share_pct": pct(int(known_cost * 100), int(total_cost * 100) or 1),
        "by_model_usd": dict(sorted(model_cost.items(), key=lambda kv: -kv[1])),
        "heaviest_tier_usd": heavy_cost,
        "cheaper_tier_usd": cheap_cost,
        "cache": {
            "uncached_input_tokens": tot_in,
            "cache_read_tokens": tot_cr,
            "cache_write_tokens": tot_cw,
            "cache_hit_ratio_pct": pct(tot_cr, tot_cr + tot_in),
            "note": "cache reads bill at 0.1x input, writes at 1.25x — a high hit ratio is the single largest cost lever",
        },
    }

    n_priced = len(priced_sessions) or 1
    ratios = {
        "_reading": (
            "Cost per unit of outcome. Lower is better only when the outcome held "
            "constant — read these against the 4D scores, not alone."
        ),
        "usd_per_session": round(total_cost / n_priced, 2),
        "usd_per_landed_session": round(total_cost / landed, 2) if landed else None,
        "usd_per_human_instruction": round(
            total_cost / sum(s.user_turns for s in real), 3),
        "usd_per_tool_call": round(total_cost / max(1, sum(tot_tools.values())), 4),
        "unlanded_session_usd": unlanded_cost,
        "unlanded_share_of_spend_pct": pct(
            int(unlanded_cost * 100), int(total_cost * 100) or 1),
        "briefing_overhead": {
            "median_recon_calls_before_first_edit": q(recon, 0.5),
            "sessions_measured": len(recon),
            "note": (
                "Each of these is context the agent rebuilt because it was not "
                "briefed. Multiply the excess over a well-briefed baseline by "
                "usd_per_tool_call for a floor estimate of what weak Description "
                "costs — a floor, because every recon result also re-enters "
                "context on later turns."
            ),
        },
    }

    pack = {
        "schema": "claude-code-skill-evidence/3",
        "four_d": four_d,
        "cost": cost,
        "efficiency_ratios": ratios,
        "window": {
            "days_requested": days,
            "first_session": span_start.isoformat() if span_start else None,
            "last_session": span_end.isoformat() if span_end else None,
            "calendar_days": (span_end - span_start).days + 1 if span_start and span_end else 0,
        },
        "volume": {
            "sessions": n,
            "distinct_projects": len(per_project),
            "human_turns": sum(s.user_turns for s in real),
            "assistant_turns": sum(s.assistant_turns for s in real),
            "sidechain_turns": sum(s.sidechain_turns for s in real),
            "tool_calls": sum(tot_tools.values()),
            "output_tokens": sum(s.out_tokens for s in real),
            "cache_read_tokens": sum(s.cache_read for s in real),
        },
        "depth_signals": {
            "turns_per_session": {
                "median": q(turns, 0.5), "p75": q(turns, 0.75),
                "p90": q(turns, 0.9), "max": turns[-1] if turns else 0,
            },
            "active_minutes_per_session": {
                "median": round(q(mins, 0.5), 1), "p75": round(q(mins, 0.75), 1),
                "p90": round(q(mins, 0.9), 1), "max": round(mins[-1], 1) if mins else 0,
                "note": f"hands-on time; gaps over {IDLE_GAP_MIN:g} min excluded",
            },
            "session_span_days": {
                "median": q(spans, 0.5), "p90": q(spans, 0.9),
                "max": spans[-1] if spans else 0,
                "note": "calendar reach — high values mean sessions get resumed, not worked continuously",
            },
            "one_shot_sessions_pct": pct(sum(1 for s in real if s.user_turns == 1), n),
            "long_haul_sessions_pct": pct(sum(1 for s in real if s.user_turns >= 15), n),
            "tool_calls_per_session_median": q(
                sorted(sum(s.tools.values()) for s in real), 0.5),
            "thinking_blocks": sum(s.thinking_blocks for s in real),
        },
        "complexity_signals": {
            "distinct_tools_used": len(tot_tools),
            "distinct_bash_verbs": len(tot_verbs),
            "distinct_mcp_servers": len(tot_mcp),
            "distinct_skills_invoked": len(tot_skills),
            "subagent_calls": sum(tot_agents.values()),
            "sessions_using_subagents_pct": pct(sum(1 for s in real if s.subagents), n),
            "sessions_using_mcp_pct": pct(sum(1 for s in real if s.mcp_servers), n),
            "sessions_using_skills_pct": pct(sum(1 for s in real if s.skills), n),
            "parallel_tool_batches": sum(s.parallel_batches for s in real),
            "max_tools_in_one_message": max((s.max_batch for s in real), default=0),
            "multi_domain_sessions_pct": pct(sum(1 for s in real if len(s.domains) >= 4), n),
        },
        "completeness_signals": {
            "sessions_with_verification_pct": pct(sum(1 for s in real if s.verified), n),
            "sessions_with_commit_pct": pct(sum(1 for s in real if s.committed), n),
            "sessions_with_deploy_pct": pct(sum(1 for s in real if s.deployed), n),
            "verified_and_committed_pct": pct(
                sum(1 for s in real if s.verified and s.committed), n),
            "explored_but_never_committed_pct": pct(
                sum(1 for s in real if s.tools and not s.committed), n),
            "plan_mode_sessions_pct": pct(sum(1 for s in real if s.plan_mode), n),
        },
        "prompting_signals": {
            "typed_prompt_chars": {
                "median": q(typed, 0.5), "p75": q(typed, 0.75),
                "p90": q(typed, 0.9), "max": typed[-1] if typed else 0,
                "note": "excludes pastes over 5000 chars",
            },
            "pasted_blobs_over_5k_chars": len(plens) - len(typed),
            "largest_paste_chars": plens[-1] if plens else 0,
            "short_prompts_under_40_chars_pct": pct(
                sum(1 for x in typed if x < 40), len(typed)),
            "long_prompts_over_500_chars_pct": pct(
                sum(1 for x in typed if x > 500), len(typed)),
            "prompts_with_explicit_constraints_pct": pct(
                sum(s.spec_prompts for s in real), len(plens)),
            "correction_turns_pct": pct(sum(s.corrections for s in real), len(plens)),
            "malay_prompts_pct": pct(sum(s.bm_prompts for s in real), len(plens)),
            "interrupt_events": sum(s.interrupts for s in real),
            "permission_modes": dict(tot_modes.most_common()),
        },
        "surface": {
            "top_tools": dict(tot_tools.most_common(30)),
            "top_bash_verbs": dict(tot_verbs.most_common(30)),
            "technical_domains": dict(tot_domains.most_common(25)),
            "file_extensions": dict(tot_exts.most_common(20)),
            "mcp_servers": dict(tot_mcp.most_common(20)),
            "skills_invoked": dict(tot_skills.most_common(25)),
            "subagent_types": dict(tot_agents.most_common(20)),
            "models": dict(tot_models.most_common(10)),
            "entrypoints": dict(tot_entry.most_common(10)),
        },
        "projects": projects_out,
        "monthly": {k: dict(v) for k, v in sorted(monthly.items())},
    }

    if include_samples:
        # Deepest sessions first — they carry the most signal about how the user
        # actually opens a hard problem.
        top = sorted(real, key=lambda s: -(s.user_turns + sum(s.tools.values()) / 10))
        pack["session_samples"] = [{
            "project": redact(s.project),
            "date": s.start.date().isoformat() if s.start else None,
            "human_turns": s.user_turns,
            "active_minutes": round(s.active_min, 1),
            "tool_calls": sum(s.tools.values()),
            "top_tools": [k for k, _ in s.tools.most_common(5)],
            "domains": [k for k, _ in s.domains.most_common(5)],
            "verified": s.verified, "committed": s.committed, "plan_mode": s.plan_mode,
            "corrections": s.corrections,
            "opening_prompt": redact(s.first_prompt),
        } for s in top[:sample_n]]

    return pack


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"),
                    help="transcript root (default: ~/.claude/projects)")
    ap.add_argument("--days", type=int, default=0,
                    help="only include activity in the last N days (0 = all)")
    ap.add_argument("-o", "--out", default="evidence.json")
    ap.add_argument("--no-samples", action="store_true",
                    help="omit every prompt excerpt; metrics only")
    ap.add_argument("--sample-n", type=int, default=25)
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        # The commonest case by far is a brand-new install, not a broken one.
        print(f"No Claude Code history found at {root}\n\n"
              "Nothing to score yet — this is normal on a new machine. Use Claude "
              "Code for a couple of weeks, then run this again.", file=sys.stderr)
        return 1

    cutoff = None
    if args.days > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)

    files = sorted(root.glob("*/*.jsonl"))
    if not files:
        print(f"no .jsonl transcripts under {root}", file=sys.stderr)
        return 1

    sessions: dict[str, SessionStats] = {}
    for i, f in enumerate(files, 1):
        print(f"\r  scanning {i}/{len(files)} …", end="", file=sys.stderr, flush=True)
        try:
            scan_file(f, cutoff, sessions)
        except Exception as e:  # a corrupt transcript must not abort the run
            print(f"\n  skipped {f.name}: {e}", file=sys.stderr)
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    pack = build_pack(sessions, args.days or None,
                      not args.no_samples, args.sample_n)
    if pack.get("error"):
        # Writing an empty pack would send the agent nothing to assess and look
        # like a valid run. Fail loudly instead.
        print(f"no usable sessions found: {pack['error']}", file=sys.stderr)
        return 1
    text = json.dumps(pack, indent=2, ensure_ascii=False)
    out = pathlib.Path(args.out)
    out.write_text(text, encoding="utf-8")
    # Behavioural data about one person. On a shared machine the default 0644
    # would let any other account read it.
    try:
        out.chmod(0o600)
    except OSError:
        pass

    v = pack.get("volume", {})
    print(f"wrote {args.out}  ({len(text) // 1024} KB) — "
          f"{v.get('sessions', 0)} sessions, "
          f"{v.get('distinct_projects', 0)} projects, "
          f"{v.get('tool_calls', 0)} tool calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
