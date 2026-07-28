#!/usr/bin/env python3
"""
Drive the AI fluency analysis without spending API credit.

`assess.sh` sends the evidence pack to a Managed Agent, which costs money and
needs a created agent. This driver runs the same 4D rubric locally against
evidence.json, so an agent (or a human) can get scores, practices, and a
re-analysis cadence with no network call and nothing to set up.

  ./driver.py check            preflight — deps, transcripts, configs
  ./driver.py extract [--days N] [--no-samples]
  ./driver.py score            4D scores from evidence.json
  ./driver.py practices        ranked actions, each tied to the score it moves
  ./driver.py cadence          how often to re-run, derived from your own rate
  ./driver.py cert             self-issued record + reproducibility digest
  ./driver.py report [-o F]    self-contained HTML: cert + transcript + advice
  ./driver.py update           pull the latest published version
  ./driver.py all              cert + score + practices + cadence

Run from the unit root (where extract-evidence.py lives), or pass --pack.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]

# Bumped whenever the shipped files change. `update` compares this against the
# published VERSION file; nothing else in the tool touches the network.
VERSION = "1.2.0"
UPDATE_REPO = "aisyaturridha-coder/ai-fluency-marketplace"
UPDATE_PATH = "plugins/ai-fluency/skills/run-ai-fluency"
UPDATE_FILES = ("VERSION", "SKILL.md", "driver.py", "extract-evidence.py")


def _raw_base(ref: str) -> str:
    return f"https://raw.githubusercontent.com/{UPDATE_REPO}/{ref}/{UPDATE_PATH}"


# Kept so existing callers and tests still resolve; commit-pinned fetches are
# what `update` actually uses.
UPDATE_BASE = _raw_base("main")


def find_extractor() -> pathlib.Path | None:
    """extract-evidence.py, whether the skill is repo-scoped or personally installed.

    Repo-scoped, it sits at the unit root beside the other scripts. Installed to
    ~/.claude/skills/, there is no unit root — so a copy shipped inside the skill
    directory wins. Checking beside-me first is what makes the skill portable.
    """
    for cand in (HERE / "extract-evidence.py", ROOT / "extract-evidence.py"):
        if cand.exists():
            return cand
    return None


_EX = find_extractor()


def _output_base() -> pathlib.Path:
    """Where this operator's own results are written.

    Beside the code, normally. But when the skill is installed as a *plugin*,
    the code sits in a git-managed marketplace checkout that `/plugin update`
    pulls over — user data does not belong in there. In that case results go to
    a stable per-user directory instead. `AI_FLUENCY_HOME` overrides either.
    """
    override = os.environ.get("AI_FLUENCY_HOME")
    if override:
        return pathlib.Path(override).expanduser()
    if "plugins" in HERE.parts:
        return pathlib.Path.home() / ".claude" / "ai-fluency"
    return _EX.parent if _EX else ROOT


BASE = _output_base()
DEFAULT_PACK = BASE / "evidence.json"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
RED, YEL, GRN, CYA = "\033[31m", "\033[33m", "\033[32m", "\033[36m"

# A legacy Windows console runs a non-UTF-8 code page, so printing the block and
# arrow glyphs raises UnicodeEncodeError and kills the run. Ask for UTF-8 first;
# if the console still cannot represent them, fall back to ASCII rather than crash.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _printable(sample: str) -> bool:
    try:
        sample.encode(sys.stdout.encoding or "ascii")
        return True
    except Exception:
        return False


UNICODE_OK = _printable("█·→—")
FILLED, EMPTY = ("█", "·") if UNICODE_OK else ("#", ".")
ARROW = "→" if UNICODE_OK else "->"


def c(s: str, colour: str) -> str:
    return s if os.environ.get("NO_COLOR") else f"{colour}{s}{RESET}"


def band(value: float, edges: list[float], reverse: bool = False) -> int:
    """Map a value to a 1-5 score. `edges` ascends; reverse=True means lower is better."""
    score = 1
    for i, e in enumerate(edges):
        if value >= e:
            score = i + 2
    if reverse:
        score = 6 - score
    return max(1, min(5, score))


def window_band(value: float, lo: float, hi: float, tol: float) -> int:
    """Score a metric where a middle band is best (appropriate reliance)."""
    if lo <= value <= hi:
        return 5
    dist = (lo - value) if value < lo else (value - hi)
    return max(1, 5 - int(dist / tol) - 1)


# --- the rubric ------------------------------------------------------------
# Each entry: (label, extractor fn, edges, reverse, why-it-matters)
# Sub-scores are averaged, then rounded, to produce the dimension score.

def sub_delegation(p: dict) -> list[tuple]:
    d = p["four_d"]["delegation"]
    cost = p.get("cost", {})
    total = cost.get("total_usd") or 0
    heavy = (cost.get("heaviest_tier_usd") or 0)
    by_model = cost.get("by_model_usd") or {}
    # Opus-and-above share of spend: the routing signal.
    top_tier = heavy + sum(v for m, v in by_model.items() if "opus" in m)
    top_share = 100.0 * top_tier / total if total else 0.0
    return [
        ("runway median (tool calls/instruction)",
         d["runway_tool_calls_per_instruction"]["median"], [2, 4, 8, 15], False,
         "how much rope each instruction gives the agent"),
        ("babysat instructions %",
         d["babysat_instructions_pct"], [10, 20, 30, 45], True,
         "work taken back that the agent could have carried"),
        ("purpose-built subagent share %",
         100.0 * d["purpose_built_vs_generic_subagents"]["purpose_built"]
         / max(1, d["subagent_calls"]), [10, 25, 45, 65], False,
         "whether built tooling is reachable at the moment of need"),
        ("spend on top model tiers %",
         top_share, [55, 70, 85, 93], True,
         "whether model choice is matched to task difficulty"),
    ]


def sub_description(p: dict) -> list[tuple]:
    d = p["four_d"]["description"]
    return [
        ("recon calls before first edit (median)",
         d["recon_calls_before_first_edit"]["median"], [4, 8, 14, 20], True,
         "context the agent rebuilt because it was not briefed"),
        ("prompts stating constraints %",
         d["prompts_with_explicit_constraints_pct"], [12, 22, 35, 50], False,
         "whether the task is scoped before work starts"),
        ("prompts under 40 chars %",
         d["short_prompts_under_40_chars_pct"], [12, 22, 35, 48], True,
         "how much the agent has to infer"),
    ]


def sub_discernment(p: dict) -> list[tuple]:
    d = p["four_d"]["discernment"]
    return [
        ("steering latency (agent msgs, median)",
         d["steering_latency_agent_msgs"]["median"], [2, 5, 10, 18], True,
         "how fast a wrong trajectory is caught"),
        ("sessions verifying agent work %",
         d["sessions_verifying_agent_work_pct"], [35, 55, 72, 85], False,
         "whether output is checked before it is trusted"),
    ]


def sub_diligence(p: dict) -> list[tuple]:
    d = p["four_d"]["diligence"]
    modes = d["permission_modes_turn_weighted"]
    tot = sum(modes.values()) or 1
    auto = 100.0 * modes.get("auto", 0) / tot
    sessions = max(1, p["volume"]["sessions"])
    # Self-imposed gates (preflight, review skills) are creation diligence and
    # must be credited — without this the score reads only the failures.
    gates_per_100 = 100.0 * d.get("gate_skill_invocations", 0) / sessions
    return [
        ("self-imposed gate runs per 100 sessions",
         gates_per_100, [3, 10, 20, 35], False,
         "voluntary checks before building — creation diligence"),
        ("deploys without a commit (sessions)",
         d["deployed_without_commit_sessions"], [1, 3, 6, 10], True,
         "shipping from a state that exists in no repository"),
        ("plan-mode sessions %",
         d["plan_mode_sessions_pct"], [2, 8, 20, 40], False,
         "thinking before touching, on risky work"),
        ("auto-approved turns %",
         auto, [25, 40, 55, 70], True,
         "permission posture against blast radius"),
    ]


DIMENSIONS = [
    ("Delegation", sub_delegation, "deciding what to hand over and what to keep"),
    ("Description", sub_description, "turning intent into instructions the agent can act on"),
    ("Discernment", sub_discernment, "judging what comes back — the most predictive of the four"),
    ("Diligence", sub_diligence, "owning what you ship with the agent's help"),
]


def score_pack(pack: dict) -> dict:
    out = {}
    for name, fn, gloss in DIMENSIONS:
        subs = []
        for label, value, edges, reverse, why in fn(pack):
            s = band(value, edges, reverse)
            subs.append({"label": label, "value": round(value, 2), "score": s, "why": why})
        avg = sum(x["score"] for x in subs) / len(subs)
        out[name] = {"score": int(round(avg)), "raw": round(avg, 2),
                     "gloss": gloss, "subs": subs}
    return out


# --- practices -------------------------------------------------------------
# Keyed by the sub-signal that is weakest. Each names the metric it moves and
# the number to aim at, so the advice is checkable on the next run.

PRACTICES = {
    "recon calls before first edit (median)": (
        "Open with the context the agent will otherwise hunt for",
        "Name the file, the constraint, and what done looks like in the first "
        "message. A 48-character instruction becomes ~200 and removes most of "
        "the exploratory calls.",
        "recon_calls_before_first_edit.median → target under 8"),
    "prompts stating constraints %": (
        "State the constraint before the work, not after the result",
        "One clause is enough: what must not change, what the output shape is, "
        "which approach is out of bounds. This is the cheapest lever in the "
        "whole rubric.",
        "prompts_with_explicit_constraints_pct → target above 35"),
    "prompts under 40 chars %": (
        "Reserve one-liners for genuinely trivial asks",
        "Short prompts are fine for 'run the tests'. On anything the agent has "
        "to scope, they trade thirty seconds of your time for several minutes "
        "of its searching.",
        "short_prompts_under_40_chars_pct → target under 25"),
    "spend on top model tiers %": (
        "Route by task weight, not by habit",
        "Mechanical work — renames, formatting, log reading, first-pass "
        "summaries — belongs on a cheaper tier. Choosing the tool for the job "
        "is the skill being scored; the cost is only the evidence.",
        "spend on top tiers → target under 70%"),
    "purpose-built subagent share %": (
        "Make the built machinery the default path",
        "Generic subagents winning over purpose-built ones means the specific "
        "tooling is not reachable when it is needed. Either route to it "
        "deliberately or retire the parts that are not paying.",
        "purpose_built share → target above 45%"),
    "babysat instructions %": (
        "Let the agent finish a thought before stepping in",
        "Instructions that end after one or two tool calls are work you took "
        "back. Give it the whole subtask and check the result, rather than "
        "steering every step.",
        "babysat_instructions_pct → target under 20"),
    "runway median (tool calls/instruction)": (
        "Hand over whole subtasks, not single steps",
        "A median runway under 4 means the agent is being driven rather than "
        "delegated to. Batch the instruction up to a unit of work with a "
        "checkable result.",
        "runway median → target 8 or above"),
    "steering latency (agent msgs, median)": (
        "Read the first action, not the final answer",
        "Most wrong trajectories are visible in the agent's first tool call. "
        "Catching it there costs one message; catching it at the end costs the "
        "whole run.",
        "steering_latency median → target under 5"),
    "sessions verifying agent work %": (
        "Verify the claim, not just the build",
        "A green build says it compiled. Run the behaviour that the change was "
        "supposed to alter, and prefer that over asking the agent whether it "
        "worked.",
        "sessions_verifying_agent_work_pct → target above 85"),
    "deploys without a commit (sessions)": (
        "Refuse to deploy from a dirty tree",
        "Make it structural, not remembered: a pre-deploy check that exits "
        "non-zero when the tree is dirty. This is what moves Diligence toward "
        "the top anchor, where the competence lives in the setup.",
        "deployed_without_commit_sessions → target 0"),
    "plan-mode sessions %": (
        "Plan first on anything touching production or a shared schema",
        "Plan mode costs a minute and converts a class of irreversible "
        "mistakes into a reviewable proposal.",
        "plan_mode_sessions_pct → target above 20"),
    "auto-approved turns %": (
        "Match permission posture to blast radius",
        "Auto-approval is right for a scratch repo and wrong for anything that "
        "deploys. Vary it by project rather than leaving it wide open "
        "everywhere.",
        "auto-approved turns → target under 55%"),
}


def practices_for(scores: dict, limit: int = 6) -> list[dict]:
    weak = []
    for dim, data in scores.items():
        for s in data["subs"]:
            if s["score"] <= 3 and s["label"] in PRACTICES:
                title, body, target = PRACTICES[s["label"]]
                weak.append({"dimension": dim, "sub_score": s["score"],
                             "value": s["value"], "title": title,
                             "body": body, "target": target})
    weak.sort(key=lambda x: (x["sub_score"], x["dimension"]))
    return weak[:limit]


# --- cadence ---------------------------------------------------------------

def cadence(pack: dict) -> dict:
    """How often re-analysis is worth doing, derived from the operator's rate.

    A re-run is only informative if a real change would clear sampling noise.
    For a proportion metric compared across two independent windows, the
    detectable difference at 95% confidence is 1.96 * sqrt(2p(1-p)/n). Solving
    for n at a chosen effect size gives the sessions each window needs.
    """
    vol = pack["volume"]
    win = pack["window"]
    days = max(1, win.get("calendar_days") or 1)
    sessions = vol["sessions"]
    rate = sessions / days

    # Use the verification rate as the reference proportion — it is the
    # best-populated proportion signal in the pack.
    p = (pack["four_d"]["discernment"]["sessions_verifying_agent_work_pct"] or 50) / 100
    p = min(max(p, 0.05), 0.95)

    rows = []
    for effect in (0.05, 0.10, 0.15, 0.20):
        n = 2 * p * (1 - p) * (1.96 / effect) ** 2
        rows.append({
            "detectable_change_pp": int(effect * 100),
            "sessions_per_window": int(math.ceil(n)),
            "days_at_current_rate": int(math.ceil(n / rate)) if rate else None,
        })

    # The recommended window is the smallest one that resolves a 15pp move —
    # coarse enough to be reachable, fine enough to catch a real regression.
    rec = next(r for r in rows if r["detectable_change_pp"] == 15)
    months = len(pack.get("monthly") or {})

    return {
        "sessions_observed": sessions,
        "calendar_days": days,
        "sessions_per_day": round(rate, 2),
        "reference_proportion": round(p, 3),
        "power_table": rows,
        "recommended_window_days": rec["days_at_current_rate"],
        "recommended_sessions": rec["sessions_per_window"],
        "monthly_points_available": months,
        "trend_ready": months >= 3,
    }


# --- rendering -------------------------------------------------------------

def bar(score: int) -> str:
    filled = FILLED * score + EMPTY * (5 - score)
    colour = RED if score <= 2 else (YEL if score == 3 else GRN)
    return c(filled, colour)


def render_scores(scores: dict) -> None:
    print(c("\n4D AI FLUENCY — local scoring", BOLD))
    print(c("Anthropic 4D framework. Behavioural proxies, not direct measures.\n", DIM))
    for dim, data in scores.items():
        print(f"  {bar(data['score'])}  {c(dim.ljust(12), BOLD)} {data['score']}/5"
              f"  {c('(' + str(data['raw']) + ' raw)', DIM)}")
        print(f"      {c(data['gloss'], DIM)}")
        for s in data["subs"]:
            mark = c("!", RED) if s["score"] <= 2 else (c("~", YEL) if s["score"] == 3 else " ")
            print(f"      {mark} {s['label']}: {c(str(s['value']), CYA)}"
                  f" {c(ARROW + ' ' + str(s['score']) + '/5', DIM)}")
        print()

    weakest = min(scores.items(), key=lambda kv: kv[1]["raw"])
    strongest = max(scores.items(), key=lambda kv: kv[1]["raw"])
    print(f"  Strongest: {c(strongest[0], GRN)}   Weakest: {c(weakest[0], RED)}")
    if strongest[0] == "Discernment" and weakest[0] == "Description":
        print(c("  Asymmetry: weak Description + strong Discernment — the SAFE one.\n"
                "  Costs time, not correctness. The dangerous inverse is strong\n"
                "  Description with weak Discernment (polished, confident, wrong).", GRN))
    elif strongest[0] == "Description" and weakest[0] == "Discernment":
        print(c("  Asymmetry: strong Description + weak Discernment — the EXPENSIVE one.\n"
                "  This profile ships polished mistakes faster than they can be caught.", RED))
    print()


def render_practices(items: list[dict]) -> None:
    print(c("BEST PRACTICES — ranked by weakest signal", BOLD))
    if not items:
        print(c("  Nothing scored 3 or below. Re-run after the next window.\n", DIM))
        return
    print(c("Each names the metric it should move, so the next run can check it.\n", DIM))
    for i, x in enumerate(items, 1):
        print(f"  {c(str(i) + '. ' + x['title'], BOLD)}")
        print(f"     {c(x['dimension'] + ' - currently ' + str(x['value']) + ' ' + ARROW + ' ' + str(x['sub_score']) + '/5', DIM)}")
        for line in _wrap(x["body"], 72):
            print(f"     {line}")
        print(f"     {c('Target: ' + x['target'], CYA)}\n")


def render_cadence(cad: dict) -> None:
    print(c("RE-ANALYSIS CADENCE", BOLD))
    print(c("Derived from your own session rate, not a rule of thumb.\n", DIM))
    print(f"  Observed: {cad['sessions_observed']} sessions over {cad['calendar_days']} days"
          f" = {c(str(cad['sessions_per_day']) + '/day', CYA)}\n")
    print(f"  {'To detect':<14}{'sessions needed':<18}{'days at your rate'}")
    print(f"  {'-' * 50}")
    for r in cad["power_table"]:
        days = r["days_at_current_rate"]
        print(f"  {str(r['detectable_change_pp']) + 'pp move':<14}"
              f"{r['sessions_per_window']:<18}{days if days else '—'}")
    print()
    print(f"  {c('Recommended: re-run every ~' + str(cad['recommended_window_days']) + ' days', BOLD)}"
          f" ({cad['recommended_sessions']} sessions)")
    for line in _wrap(
            "That is the smallest window where a 15-point move clears sampling "
            "noise. Running more often produces movement you cannot distinguish "
            "from chance; running much less lets a regression sit undetected.", 72):
        print(f"  {c(line, DIM)}")
    print()
    if cad["trend_ready"]:
        print(c(f"  Trend: {cad['monthly_points_available']} monthly points — trajectory is readable.", GRN))
    else:
        print(c(f"  Trend: only {cad['monthly_points_available']} monthly point(s). "
                f"Need 3+ before trajectory means anything.", YEL))
    print()


def certificate(pack: dict, scores: dict) -> dict:
    """A self-issued record whose only claim to weight is reproducibility.

    The digest covers the assessment window and every scored value. Against a
    FIXED pack it is perfectly deterministic — the same evidence.json always
    yields the same string.

    It is NOT stable across re-extraction, and that is not a bug: the operator
    keeps working, so `extract` legitimately picks up new sessions (including
    the one running the assessment). Verification therefore means re-running
    `cert` against the archived pack, which is what `--freeze` exists to
    preserve. There is no authority behind any of this and none is implied.
    """
    import hashlib

    win = pack["window"]
    material = {
        "schema": pack["schema"],
        "first": win.get("first_session"),
        "last": win.get("last_session"),
        "sessions": pack["volume"]["sessions"],
        "scores": {k: v["score"] for k, v in scores.items()},
        "subs": {k: {x["label"]: x["value"] for x in v["subs"]}
                 for k, v in scores.items()},
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode()).hexdigest()

    order = ["Delegation", "Description", "Discernment", "Diligence"]
    floor_dim = min(scores.items(), key=lambda kv: kv[1]["raw"])
    top_dim = max(scores.items(), key=lambda kv: kv[1]["raw"])

    if top_dim[0] == "Discernment" and floor_dim[0] == "Description":
        posture, posture_note = "favourable", (
            "weak Description with strong Discernment — costs time, not correctness")
    elif top_dim[0] == "Description" and floor_dim[0] == "Discernment":
        posture, posture_note = "adverse", (
            "strong Description with weak Discernment — ships polished mistakes "
            "faster than they can be caught")
    else:
        posture, posture_note = "neutral", "no named asymmetry in this profile"

    return {
        "profile": " · ".join(str(scores[d]["score"]) for d in order if d in scores),
        "profile_order": order,
        "floor": floor_dim[1]["score"],
        "binding_constraint": floor_dim[0],
        "strongest": top_dim[0],
        "safety_posture": posture,
        "posture_note": posture_note,
        "digest": digest,
        "digest_short": digest[:16],
        "sessions": pack["volume"]["sessions"],
        "window": f"{(win.get('first_session') or '')[:10]} → {(win.get('last_session') or '')[:10]}",
        "no_single_average": (
            "The 4D framework treats fluency as the whole set. The four scores are "
            "not averaged into one figure, because an average hides an operator who "
            "is excellent at one dimension and dangerous at another."
        ),
    }


def render_cert(cert: dict) -> None:
    print(c("\nCERTIFICATE — self-issued, reproducible\n", BOLD))
    print(f"  Profile (D·D·D·D)   {c(cert['profile'], BOLD)}")
    print(f"  Floor               {c(str(cert['floor']) + '/5', RED if cert['floor'] <= 2 else YEL)}"
          f"  {c('binding constraint: ' + cert['binding_constraint'], DIM)}")
    colour = GRN if cert["safety_posture"] == "favourable" else (
        RED if cert["safety_posture"] == "adverse" else YEL)
    print(f"  Safety posture      {c(cert['safety_posture'], colour)}")
    print(f"                      {c(cert['posture_note'], DIM)}")
    print(f"\n  Window              {cert['window']}  ({cert['sessions']} sessions)")
    print(f"  Digest              {c(cert['digest_short'], CYA)}"
          f"{c(cert['digest'][16:], DIM)}")
    print()
    for line in _wrap("Verify by re-running `cert` against the SAME pack — that is "
                      "deterministic. Re-extracting gives a different digest because "
                      "new sessions have genuinely landed since. Use --freeze to "
                      "archive the pack so the record stays checkable. Nothing "
                      "accredits this; reproducibility is the entire claim.", 72):
        print(f"  {c(line, DIM)}")
    if cert.get("frozen_to"):
        print(f"\n  {c('Pack archived: ' + cert['frozen_to'], CYA)}")
    print()
    for line in _wrap(cert["no_single_average"], 72):
        print(f"  {c(line, DIM)}")
    print()


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# --- commands --------------------------------------------------------------

def load_pack(path: pathlib.Path) -> dict:
    if not path.exists():
        sys.exit(f"no evidence pack at {path}\nRun:  ./driver.py extract")
    pack = json.loads(path.read_text(encoding="utf-8"))
    schema = pack.get("schema", "")
    if not schema.startswith("claude-code-skill-evidence/"):
        sys.exit(f"{path} is not an evidence pack (schema={schema!r})")
    ver = int(schema.rsplit("/", 1)[1])
    if ver < 3:
        sys.exit(f"pack is schema v{ver}; this driver needs v3+. Re-run extract.")
    return pack


# --- HTML report -----------------------------------------------------------
# Self-contained: one file, no assets, no network, safe to email or attach.

ANCHOR_WORDS = {5: "Structural", 4: "Deliberate", 3: "Habitual",
                2: "Unreliable", 1: "Occasional"}


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_report(pack: dict, scores: dict, subject: str) -> str:
    cert = certificate(pack, scores)
    cad = cadence(pack)
    prac = practices_for(scores, limit=6)
    win = pack["window"]
    order = [d for d in cert["profile_order"] if d in scores]

    rows = []
    for dim in order:
        data = scores[dim]
        rows.append(
            f'<tr class="dim"><td colspan="5">{_esc(dim)}'
            f'<span class="roll">{data["score"]}/5 · mean {data["raw"]:.2f}</span></td></tr>')
        for s in data["subs"]:
            rows.append(
                f'<tr><td class="sig">{_esc(s["label"])}</td>'
                f'<td class="n">{_esc(s["value"])}</td>'
                f'<td><span class="g g{s["score"]}">{s["score"]}/5</span></td>'
                f'<td class="n">{ANCHOR_WORDS[s["score"]]}</td>'
                f'<td class="why">{_esc(s["why"])}</td></tr>')

    prac_html = "".join(
        f'<div class="rec"><h3>{i}. {_esc(p["title"])}</h3>'
        f'<p class="meta">{_esc(p["dimension"])} · currently '
        f'{_esc(p["value"])} → {p["sub_score"]}/5</p>'
        f'<p>{_esc(p["body"])}</p>'
        f'<p class="target">Target: {_esc(p["target"])}</p></div>'
        for i, p in enumerate(prac, 1)) or \
        '<p class="plain">Nothing scored 3 or below — re-check after the next window.</p>'

    cad_rows = "".join(
        f'<tr><td>{r["detectable_change_pp"]} point move</td>'
        f'<td class="n">{r["sessions_per_window"]}</td>'
        f'<td class="n">{r["days_at_current_rate"] or "—"}</td></tr>'
        for r in cad["power_table"])

    trend = ("Trajectory is readable." if cad["trend_ready"] else
             f'Only {cad["monthly_points_available"]} monthly point(s) so far — '
             "three are needed before any trend claim holds.")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Fluency Record — {_esc(subject)}</title><style>
:root{{--paper:#F4F5F7;--card:#fff;--ink:#131A2B;--soft:#4A5464;--faint:#7C8593;
--rule:#D5DAE1;--soft-rule:#E7EAEE;--seal:#1F6B63;--seal-bg:#E6F0EE;
--flag:#C2571E;--flag-bg:#FBEDE4;--g1:#B03A2E;--g2:#C2571E;--g3:#B8860B;--g4:#3E7C59;--g5:#1F6B63}}
@media(prefers-color-scheme:dark){{:root{{--paper:#0D1119;--card:#151B26;--ink:#E8EBEC;
--soft:#A3ACBA;--faint:#737D8C;--rule:#29313D;--soft-rule:#1E2530;--seal:#5FBFAE;
--seal-bg:#10241F;--flag:#E2814A;--flag-bg:#2A1B12;--g1:#E0705F;--g2:#E2814A;
--g3:#D9AE64;--g4:#6FBF95;--g5:#5FBFAE}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);
padding:2.5rem 1.25rem 5rem;font:400 16px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:88ch;margin:0 auto}}.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-variant-numeric:tabular-nums}}
.cert{{background:var(--card);border:1px solid var(--ink);padding:clamp(1.5rem,5vw,3rem);position:relative}}
.cert::after{{content:"";position:absolute;inset:7px;border:1px solid var(--rule);pointer-events:none}}
.eyebrow{{font:500 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.2em;
text-transform:uppercase;color:var(--seal);text-align:center}}
h1{{font:400 clamp(1.6rem,4.5vw,2.3rem)/1.15 ui-serif,Georgia,serif;text-align:center;margin:.8rem 0 .4rem}}
.subject{{text-align:center;font:400 1.05rem/1.4 ui-monospace,Menlo,monospace;color:var(--soft)}}
.win{{text-align:center;font:400 12px/1.5 ui-monospace,Menlo,monospace;color:var(--faint);
padding-bottom:1.6rem;margin-bottom:1.6rem;border-bottom:1px solid var(--rule)}}
.facts{{display:grid;grid-template-columns:1fr;gap:1.4rem}}
@media(min-width:700px){{.facts{{grid-template-columns:repeat(3,1fr);gap:0}}
.facts>div+div{{border-left:1px solid var(--soft-rule)}}}}
.fact{{text-align:center;padding:0 1rem}}
.fact dt{{font:500 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.15em;
text-transform:uppercase;color:var(--faint);margin-bottom:.6rem}}
.big{{font:400 clamp(1.8rem,5vw,2.4rem)/1 ui-serif,Georgia,serif;display:block;margin-bottom:.35rem}}
.note{{font-size:.82rem;line-height:1.45;color:var(--soft)}}
.floor .big{{color:var(--flag)}}.posture .big{{color:var(--seal);font-size:clamp(1.1rem,3vw,1.4rem)}}
.noavg{{margin-top:1.7rem;padding-top:1.3rem;border-top:1px solid var(--rule);font-size:.85rem;
color:var(--soft);text-align:center;max-width:58ch;margin-left:auto;margin-right:auto}}
.seal{{margin-top:1.7rem;background:var(--seal-bg);border:1px solid var(--seal);padding:1rem}}
.seal dt{{font:500 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.15em;
text-transform:uppercase;color:var(--seal);margin-bottom:.5rem}}
.digest{{font-family:ui-monospace,Menlo,monospace;font-size:clamp(.66rem,2vw,.8rem);
line-height:1.7;word-break:break-all}}
.seal p{{margin:.7rem 0 0;font-size:.83rem;color:var(--soft)}}
.disc{{margin-top:1.4rem;border:1px solid var(--flag);background:var(--flag-bg);
padding:.9rem 1.1rem;font-size:.86rem;line-height:1.55}}.disc strong{{color:var(--flag)}}
section{{margin-top:3.2rem}}h2{{font:400 1.45rem/1.2 ui-serif,Georgia,serif;margin:0 0 .3rem}}
.sn{{color:var(--faint);font-size:.86rem;margin:0 0 1.3rem;padding-bottom:.8rem;border-bottom:2px solid var(--ink)}}
h3{{font:600 .93rem/1.35 ui-sans-serif,-apple-system,sans-serif;margin:1.6rem 0 .4rem}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:40rem;font-size:.87rem}}
th,td{{text-align:left;padding:.55rem 1rem .55rem 0;border-bottom:1px solid var(--soft-rule);vertical-align:baseline}}
th{{font:500 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.13em;text-transform:uppercase;
color:var(--faint);border-bottom:1px solid var(--rule);white-space:nowrap}}
td.n{{font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;white-space:nowrap}}
tr.dim td{{padding-top:1.4rem;border-bottom:1px solid var(--rule);font:600 .96rem/1.3 ui-sans-serif,sans-serif}}
.roll{{font-family:ui-monospace,Menlo,monospace;font-weight:400;color:var(--faint);
font-size:.84rem;margin-left:.6rem}}
td.sig{{padding-left:1.3rem}}td.why{{color:var(--faint);font-size:.82rem}}
.g{{display:inline-block;min-width:2.3rem;text-align:center;font-family:ui-monospace,Menlo,monospace;
font-size:.77rem;font-weight:500;padding:.1em .45em;border:1px solid currentColor;border-radius:2px}}
.g1{{color:var(--g1)}}.g2{{color:var(--g2)}}.g3{{color:var(--g3)}}.g4{{color:var(--g4)}}.g5{{color:var(--g5)}}
.rec{{background:var(--card);border-left:3px solid var(--seal);padding:1rem 1.2rem;margin-bottom:1rem}}
.rec h3{{margin:0 0 .3rem}}.rec p{{margin:0 0 .6rem;font-size:.9rem;color:var(--soft)}}
.rec .meta{{font-family:ui-monospace,Menlo,monospace;font-size:.78rem;color:var(--faint)}}
.rec .target{{color:var(--seal);font-family:ui-monospace,Menlo,monospace;font-size:.8rem;margin:0}}
.plain{{color:var(--soft);font-size:.92rem}}ul{{padding-left:1.15rem}}li{{margin-bottom:.5rem;color:var(--soft);font-size:.9rem}}
footer{{margin-top:3.5rem;padding-top:1.3rem;border-top:2px solid var(--ink);
font-size:.83rem;color:var(--faint);line-height:1.6}}
</style></head><body><div class="wrap">

<article class="cert">
<div class="eyebrow">AI Fluency Record · Self-issued</div>
<h1>Certificate of AI Fluency Assessment</h1>
<p class="subject">Subject: {_esc(subject)}</p>
<p class="win">Anthropic 4D framework · {_esc((win.get("first_session") or "")[:10])} →
{_esc((win.get("last_session") or "")[:10])} · {pack["volume"]["sessions"]} sessions</p>
<dl class="facts">
<div class="fact"><dt>Profile</dt><span class="big mono">{_esc(cert["profile"])}</span>
<span class="note">Delegation, Description, Discernment, Diligence — reported as a set.</span></div>
<div class="fact floor"><dt>Floor</dt><span class="big mono">{cert["floor"]}/5</span>
<span class="note">Binding constraint: <strong>{_esc(cert["binding_constraint"])}</strong>.
The set is limited by its weakest dimension.</span></div>
<div class="fact posture"><dt>Safety posture</dt>
<span class="big">{_esc(cert["safety_posture"].title())}</span>
<span class="note">{_esc(cert["posture_note"])}</span></div>
</dl>
<p class="noavg">{_esc(cert["no_single_average"])}</p>
<dl class="seal"><dt>Verification digest — SHA-256</dt>
<dd class="digest">{_esc(cert["digest"])}</dd>
<p>Computed over the window, session count and every scored value. Re-running
<code>driver.py cert</code> against the same evidence file reproduces it exactly.</p></dl>
<p class="disc"><strong>What this certifies, and what it does not.</strong>
Generated by its own subject from their own local session transcripts.
<strong>No organisation has accredited it.</strong> Its only claim to weight is that
every figure is reproducible and the scoring rubric is published. It measures
collaboration behaviour, never code quality.</p>
</article>

<section><h2>Transcript of record</h2>
<p class="sn">Every sub-signal behind each dimension score, with the measured value and the
anchor it maps to. Dimension scores are the rounded mean of their rows.</p>
<div class="scroll"><table><thead><tr>
<th style="width:32%">Signal</th><th style="width:11%">Measured</th><th style="width:9%">Grade</th>
<th style="width:14%">Anchor</th><th>What it reads</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<h3>Grade anchors</h3>
<p class="plain">Assigned by matching a measured value to a fixed band — never by judgement.
5 Structural · 4 Deliberate · 3 Habitual · 2 Unreliable · 1 Occasional.</p>
</section>

<section><h2>How to raise these scores</h2>
<p class="sn">Ranked by weakest signal. Each names the metric it should move, so the next
run can check whether it worked.</p>
{prac_html}</section>

<section><h2>How often to re-evaluate</h2>
<p class="sn">Derived from your own session rate, not a rule of thumb.</p>
<p class="plain">Observed {cad["sessions_observed"]} sessions over {cad["calendar_days"]} days
= <span class="mono">{cad["sessions_per_day"]}</span> per day. A re-run only tells you
something if a real change would clear sampling noise.</p>
<div class="scroll"><table><thead><tr><th>To detect</th><th>Sessions needed</th>
<th>Days at your rate</th></tr></thead><tbody>{cad_rows}</tbody></table></div>
<p class="plain"><strong>Recommended: re-run about every
{cad["recommended_window_days"]} days</strong> ({cad["recommended_sessions"]} sessions) —
the smallest window in which a 15-point move is distinguishable from chance. {trend}</p>
</section>

<section><h2>Method and limits</h2><p class="sn">Read before quoting any number above.</p>
<ul>
<li><strong>Framework.</strong> Anthropic's 4D AI Fluency framework, developed with
Prof. Joseph Feller (University College Cork) and Prof. Rick Dakan (Ringling College).</li>
<li><strong>Proxies, not measures.</strong> Transcripts record behaviour, not intent or
correctness. Whether the agent was actually wrong is not visible.</li>
<li><strong>Thresholds are uncalibrated.</strong> The framework is published; the numeric
bands are a judgement call with no population data behind them.</li>
<li><strong>Scores move with the window.</strong> A short window can shift a dimension by a
full point. Treat any single run as approximate.</li>
<li><strong>No peer benchmark.</strong> No population to compare against — no percentiles,
no ranking.</li>
</ul></section>

<footer>Self-issued · Anthropic 4D framework · skill v{_esc(installed_version())} ·
digest {_esc(cert["digest_short"])}. Not an accredited credential.</footer>
</div></body></html>"""


def cmd_report(pack: dict, scores: dict, out: pathlib.Path, subject: str) -> int:
    html = build_report(pack, scores, subject)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(c("\nREPORT\n", BOLD))
    print(f"  {c('written', GRN)}  {out}")
    print(f"  {c(str(len(html) // 1024) + ' KB · certificate, transcript, '
                'recommendations, cadence', DIM)}")
    print(f"\n  {c('Open it in a browser. It is one self-contained file — no '
                  'assets, no network.', DIM)}\n")
    return 0


def installed_version() -> str:
    f = HERE / "VERSION"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return VERSION


def _resolve_head() -> str:
    """The commit SHA that `main` currently points at.

    raw.githubusercontent caches by path and ignores query strings, and expires
    files independently — so branch URLs can serve a new VERSION beside stale
    code. Commit URLs are immutable, so pinning one SHA makes the downloaded set
    consistent by construction and never stale.
    """
    import json as _json
    data = _fetch_url(f"https://api.github.com/repos/{UPDATE_REPO}/commits/main")
    return _json.loads(data.decode())["sha"]


def _fetch(name: str, bust: str = "") -> bytes:
    """Fetch one published file, tolerating a broken Python CA bundle.

    `bust` appends a cache-busting query. raw.githubusercontent.com caches for
    minutes, and it expires files independently — so a fresh VERSION can arrive
    alongside stale code, leaving a mixed install that reports the new number
    while running the old logic. Keying every URL to the version prevents that.

    A Python installed from python.org ships without CA certificates until
    `Install Certificates.command` is run, so urllib raises
    CERTIFICATE_VERIFY_FAILED on a perfectly good connection. curl carries the
    system trust store and is present on macOS and Windows 10+, so it is the
    fallback rather than disabling verification — which would defeat the point.
    """
    ref = bust or "main"
    return _fetch_url(f"{_raw_base(ref)}/{name}")


def _fetch_url(url: str) -> bytes:
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "ai-fluency-update"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception as first:
        try:
            out = subprocess.run(["curl", "-fsSL", "--max-time", "20", url],
                                 capture_output=True, check=True)
            return out.stdout
        except subprocess.CalledProcessError as second:
            # curl reached the network but the server said no — a 404 here means
            # the published copy is missing that file, not that we are offline.
            raise RuntimeError(
                f"{url} could not be fetched (curl exit {second.returncode}); "
                f"direct fetch also failed: {first}") from None
        except Exception:
            raise first


def cmd_update(dry_run: bool) -> int:
    """Replace the installed files with the published ones.

    Downloads are staged and validated before anything is overwritten — a
    truncated or corrupted fetch must never leave a half-broken install behind,
    because the thing being replaced is the tool doing the replacing.
    """
    local = installed_version()
    print(c("\nUPDATE\n", BOLD))
    print(f"  installed  {c(local, CYA)}")

    try:
        sha = _resolve_head()
        remote = _fetch("VERSION", bust=sha).decode().strip()
    except Exception as e:
        print(f"  {c('could not reach the update server', RED)}")
        print(f"  {c(str(e), DIM)}")
        print(f"\n  {c('Nothing was changed. Check your connection and retry.', DIM)}")
        return 1

    print(f"  published  {c(remote, CYA)}  {c('@ ' + sha[:10], DIM)}\n")
    if remote == local:
        print(f"  {c('Already up to date.', GRN)}\n")
        return 0

    staged: dict[str, bytes] = {}
    for name in UPDATE_FILES:
        try:
            staged[name] = _fetch(name, bust=sha)
        except Exception as e:
            print(f"  {c('failed to download ' + name, RED)} {c(str(e), DIM)}")
            print(f"\n  {c('Nothing was changed.', DIM)}")
            return 1

    # Validate before touching anything on disk.
    for name, data in staged.items():
        if not data.strip():
            print(f"  {c(name + ' came back empty — aborting', RED)}\n")
            return 1
        if name.endswith(".py"):
            try:
                compile(data.decode("utf-8"), name, "exec")
            except SyntaxError as e:
                print(f"  {c(name + ' failed to parse — aborting', RED)} {c(str(e), DIM)}\n")
                return 1

    if dry_run:
        print(f"  {c('--dry-run: ' + str(len(staged)) + ' files would be replaced', YEL)}\n")
        return 0

    for name, data in staged.items():
        target = HERE / name
        tmp = target.with_suffix(target.suffix + ".new")
        tmp.write_bytes(data)
        tmp.replace(target)          # atomic swap; the running file keeps its inode
        print(f"  {c('updated', GRN)}  {name}")

    print(f"\n  {c(local + ' ' + ARROW + ' ' + remote, BOLD)}")
    print(f"  {c('Restart Claude Code so the new version loads.', DIM)}\n")
    return 0


def cmd_check() -> int:
    ok = True
    print(c("PREFLIGHT\n", BOLD))

    def row(label: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and good
        print(f"  {c('PASS', GRN) if good else c('FAIL', RED)}  {label:<28}{c(detail, DIM)}")

    print(f"  {c('INFO', CYA)}  {'skill version':<28}{c(installed_version(), DIM)}")
    row("python3", sys.version_info >= (3, 9), sys.version.split()[0])
    ex = find_extractor()
    row("extract-evidence.py", ex is not None,
        ("beside the skill" if ex and ex.parent == HERE else "at the unit root")
        if ex else "missing — ship it with the skill or put it at the unit root")
    tr = pathlib.Path.home() / ".claude" / "projects"
    files = list(tr.glob("*/*.jsonl")) if tr.is_dir() else []
    row("transcripts", bool(files), f"{len(files)} .jsonl under ~/.claude/projects")
    # Everything below is the optional paid API path. It must never fail the
    # preflight: a share-bundle ships without it on purpose, and a FAIL here
    # would read as "the tool is broken" to someone who only wants local scores.
    cfg = ROOT / "skill-agent.json"
    print(f"  {c('INFO', CYA)}  {'skill-agent.json':<28}"
          f"{c('present' if cfg.exists() else 'absent — only needed for the paid API path', DIM)}")
    ids = ROOT / ".skill-agent-ids"
    print(f"  {c('INFO', CYA)}  {'agent created':<28}"
          f"{c('yes' if ids.exists() else 'no — local scoring still works', DIM)}")
    pack = DEFAULT_PACK
    print(f"  {c('INFO', CYA)}  {'evidence.json':<28}"
          f"{c(f'{pack.stat().st_size // 1024} KB' if pack.exists() else 'not yet extracted', DIM)}")
    print()
    return 0 if ok else 1


def cmd_extract(args) -> int:
    ex = find_extractor()
    if ex is None:
        sys.exit("extract-evidence.py not found beside the skill or at the unit root")
    args.pack.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ex), "-o", str(args.pack)]
    if args.days:
        cmd += ["--days", str(args.days)]
    if args.no_samples:
        cmd.append("--no-samples")
    print(c(f"$ {' '.join(cmd[1:])}\n", DIM))
    return subprocess.call(cmd, cwd=ex.parent)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["check", "extract", "score", "practices", "cadence",
                             "cert", "report", "update", "all"])
    ap.add_argument("--pack", type=pathlib.Path, default=DEFAULT_PACK)
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--no-samples", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("-o", "--out", type=pathlib.Path,
                    help="report: where to write the HTML (default reports/)")
    ap.add_argument("--subject", default=os.environ.get("USER", "me"),
                    help="report: name to print on the certificate")
    ap.add_argument("--dry-run", action="store_true",
                    help="update: report what would change without writing")
    ap.add_argument("--freeze", action="store_true",
                    help="archive the pack under reports/ so the cert stays verifiable")
    args = ap.parse_args()

    if args.command == "update":
        return cmd_update(args.dry_run)
    if args.command == "check":
        return cmd_check()
    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "all":
        rc = cmd_extract(args)
        if rc:
            return rc

    pack = load_pack(args.pack)
    scores = score_pack(pack)
    cert = certificate(pack, scores)

    if args.freeze:
        dest = BASE / "reports" / f"cert-{cert['digest_short']}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(args.pack.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            dest.chmod(0o600)
            dest.parent.chmod(0o700)
        except OSError:
            pass
        cert["frozen_to"] = str(dest.relative_to(BASE))

    if args.json:
        print(json.dumps({"scores": scores,
                          "certificate": cert,
                          "practices": practices_for(scores),
                          "cadence": cadence(pack)}, indent=2))
        return 0

    if args.command in ("report", "all"):
        out = args.out or (BASE / "reports" /
                           f"ai-fluency-{cert['digest_short']}.html")
        cmd_report(pack, scores, out, args.subject)
    if args.command in ("cert", "all"):
        render_cert(cert)
    if args.command in ("score", "all"):
        render_scores(scores)
    if args.command in ("practices", "all"):
        render_practices(practices_for(scores))
    if args.command in ("cadence", "all"):
        render_cadence(cadence(pack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
