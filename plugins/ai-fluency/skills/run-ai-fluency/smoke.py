#!/usr/bin/env python3
"""
Smoke test for the AI fluency tool.

Everything here is free and offline by default — it builds synthetic evidence
packs rather than reading anyone's transcripts, so it is safe to run anywhere
and gives the same answer on every machine.

  ./smoke.py                 local checks only (no network, no cost)
  ./smoke.py --online        also verify the published update endpoint
  ./smoke.py --agent         also validate the Managed Agent config (no API call)
  ./smoke.py --agent-live    create a real session — COSTS MONEY, opt-in only

Exit code is 0 only when every selected check passes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
UNIT = HERE.parents[2]
PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str):
    def deco(fn):
        def run():
            try:
                detail = fn() or ""
                results.append((PASS, name, detail))
            except AssertionError as e:
                results.append((FAIL, name, str(e)))
            except Exception as e:
                results.append((FAIL, name, f"{type(e).__name__}: {e}"))
        run.__name__ = fn.__name__
        return run
    return deco


def load_driver():
    spec = importlib.util.spec_from_file_location("drv", HERE / "driver.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --- synthetic evidence ----------------------------------------------------
# A pack shaped exactly like the real schema, with values chosen so each
# dimension lands on a known score. If the rubric drifts, these fail loudly.

def make_pack(**over) -> dict:
    pack = {
        "schema": "claude-code-skill-evidence/3",
        "window": {"first_session": "2026-01-01T00:00:00+00:00",
                   "last_session": "2026-02-01T00:00:00+00:00",
                   "calendar_days": 31},
        "volume": {"sessions": 100, "distinct_projects": 5, "human_turns": 1000,
                   "assistant_turns": 5000, "sidechain_turns": 0,
                   "tool_calls": 10000, "output_tokens": 1_000_000,
                   "cache_read_tokens": 10_000_000},
        "four_d": {
            "delegation": {
                "runway_tool_calls_per_instruction": {"median": 10, "p75": 20, "p90": 40, "max": 100},
                "babysat_instructions_pct": 15.0,
                "long_leash_instructions_pct": 20.0,
                "subagent_calls": 100,
                "purpose_built_vs_generic_subagents": {"purpose_built": 50, "generic": 50},
                "model_mix": {"claude-opus-5": 100},
            },
            "description": {
                "typed_prompt_chars_median": 200,
                "short_prompts_under_40_chars_pct": 10.0,
                "prompts_with_explicit_constraints_pct": 55.0,
                "recon_calls_before_first_edit": {"median": 3, "p75": 5, "p90": 8,
                                                  "sessions_measured": 90},
                "raw_material_supplied_pastes": 10,
            },
            "discernment": {
                "steering_latency_agent_msgs": {"median": 1, "p75": 2, "p90": 4, "events": 200},
                "correction_turns_pct": 5.0,
                "interrupt_events": 20,
                "tool_errors": 100,
                "human_took_over_after_error": 10,
                "agent_self_healed_after_error": 90,
                "takeover_share_pct": 10.0,
                "sessions_verifying_agent_work_pct": 95.0,
            },
            "diligence": {
                "permission_modes_turn_weighted": {"default": 800, "auto": 200},
                "plan_mode_sessions_pct": 50.0,
                "verified_and_committed_pct": 80.0,
                "deployed_without_commit_sessions": 0,
                "gate_skill_invocations": 40,
            },
        },
        "cost": {"total_usd": 100.0, "heaviest_tier_usd": 0.0,
                 "by_model_usd": {"claude-sonnet-5": 60.0, "claude-opus-5": 40.0},
                 "cache": {}},
        "efficiency_ratios": {},
        "monthly": {"2026-01": {"sessions": 100}},
        "projects": [],
        "surface": {},
    }
    for k, v in over.items():
        pack[k] = v
    return pack


def write_pack(pack: dict) -> pathlib.Path:
    f = pathlib.Path(tempfile.mkdtemp()) / "evidence.json"
    f.write_text(json.dumps(pack), encoding="utf-8")
    return f


# --- local checks ----------------------------------------------------------

@check("driver imports cleanly")
def t_import():
    m = load_driver()
    assert hasattr(m, "score_pack"), "score_pack missing"
    return f"version {m.installed_version()}"


@check("extractor compiles")
def t_extractor_compiles():
    ex = load_driver().find_extractor()
    assert ex is not None, "extract-evidence.py not found beside skill or at unit root"
    subprocess.run([sys.executable, "-m", "py_compile", str(ex)], check=True,
                   capture_output=True)
    return str(ex.name)


@check("a strong pack scores near the top")
def t_high():
    m = load_driver()
    s = m.score_pack(make_pack())
    for dim, data in s.items():
        assert data["score"] >= 4, f"{dim} scored {data['score']}, expected >=4"
    return " ".join(f"{k[:4]}{v['score']}" for k, v in s.items())


@check("a weak pack scores near the bottom")
def t_low():
    m = load_driver()
    p = make_pack()
    d = p["four_d"]
    d["delegation"]["runway_tool_calls_per_instruction"]["median"] = 1
    d["delegation"]["babysat_instructions_pct"] = 60.0
    d["delegation"]["purpose_built_vs_generic_subagents"] = {"purpose_built": 1, "generic": 99}
    d["description"]["recon_calls_before_first_edit"]["median"] = 30
    d["description"]["prompts_with_explicit_constraints_pct"] = 5.0
    d["description"]["short_prompts_under_40_chars_pct"] = 70.0
    d["discernment"]["steering_latency_agent_msgs"]["median"] = 40
    d["discernment"]["sessions_verifying_agent_work_pct"] = 10.0
    d["diligence"]["deployed_without_commit_sessions"] = 25
    d["diligence"]["plan_mode_sessions_pct"] = 0.0
    d["diligence"]["permission_modes_turn_weighted"] = {"auto": 1000}
    d["diligence"]["gate_skill_invocations"] = 0
    p["cost"] = {"total_usd": 100.0, "heaviest_tier_usd": 100.0,
                 "by_model_usd": {"claude-fable-5": 100.0}}
    s = m.score_pack(p)
    for dim, data in s.items():
        assert data["score"] <= 2, f"{dim} scored {data['score']}, expected <=2"
    return " ".join(f"{k[:4]}{v['score']}" for k, v in s.items())


@check("scores stay within 1..5")
def t_bounds():
    m = load_driver()
    for mult in (0, 1, 1000, -5):
        p = make_pack()
        d = p["four_d"]
        d["delegation"]["runway_tool_calls_per_instruction"]["median"] = mult
        d["description"]["recon_calls_before_first_edit"]["median"] = mult
        d["discernment"]["steering_latency_agent_msgs"]["median"] = mult
        for dim, data in m.score_pack(p).items():
            assert 1 <= data["score"] <= 5, f"{dim}={data['score']} at input {mult}"
    return "0, 1, 1000, -5 all bounded"


@check("degenerate pack does not crash")
def t_degenerate():
    m = load_driver()
    p = make_pack()
    p["volume"]["sessions"] = 0
    p["four_d"]["delegation"]["subagent_calls"] = 0
    p["cost"]["total_usd"] = 0
    m.score_pack(p)
    m.cadence(p)
    return "zero sessions, zero cost, zero subagents"


@check("old schema is rejected, not silently scored")
def t_schema_guard():
    m = load_driver()
    p = make_pack(); p["schema"] = "claude-code-skill-evidence/1"
    f = write_pack(p)
    try:
        m.load_pack(f)
    except SystemExit:
        return "v1 pack refused"
    raise AssertionError("a v1 pack was accepted")


@check("cert digest is deterministic and data-bound")
def t_digest():
    m = load_driver()
    p = make_pack()
    a = m.certificate(p, m.score_pack(p))["digest"]
    b = m.certificate(p, m.score_pack(p))["digest"]
    assert a == b, "same pack produced two different digests"
    p2 = make_pack(); p2["volume"]["sessions"] = 101
    c = m.certificate(p2, m.score_pack(p2))["digest"]
    assert a != c, "changing the data did not change the digest"
    return a[:16]


@check("cert reports the set, never a single average")
def t_no_average():
    m = load_driver()
    p = make_pack()
    cert = m.certificate(p, m.score_pack(p))
    assert "overall" not in cert and "average" not in cert, "an overall score leaked in"
    assert cert["profile"].count("·") == 3, f"profile shape wrong: {cert['profile']}"
    return cert["profile"]


@check("cadence scales with session rate")
def t_cadence():
    m = load_driver()
    slow = make_pack(); slow["volume"]["sessions"] = 30
    fast = make_pack(); fast["volume"]["sessions"] = 300
    ds, df = m.cadence(slow), m.cadence(fast)
    assert ds["recommended_window_days"] > df["recommended_window_days"], \
        "a busier operator should be told to re-run sooner"
    assert not ds["trend_ready"], "one month should not be trend-ready"
    return f"{ds['recommended_window_days']}d slow vs {df['recommended_window_days']}d busy"


@check("every CLI command runs end to end")
def t_cli():
    f = write_pack(make_pack())
    env = {**os.environ, "NO_COLOR": "1"}
    for cmd in ("score", "practices", "cadence", "cert"):
        r = subprocess.run([sys.executable, str(HERE / "driver.py"), cmd, "--pack", str(f)],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"{cmd} exited {r.returncode}: {r.stderr[:200]}"
        assert r.stdout.strip(), f"{cmd} printed nothing"
    return "score, practices, cadence, cert"


@check("--json output is machine-readable")
def t_json():
    f = write_pack(make_pack())
    r = subprocess.run([sys.executable, str(HERE / "driver.py"), "score",
                        "--pack", str(f), "--json"],
                       capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"})
    d = json.loads(r.stdout)
    for key in ("scores", "certificate", "practices", "cadence"):
        assert key in d, f"missing {key}"
    return f"{len(d['scores'])} dimensions"


@check("legacy console falls back to ASCII instead of crashing")
def t_ascii():
    code = (
        "import io,sys,importlib.util as u\n"
        "class L(io.TextIOBase):\n"
        "    encoding='cp1252'\n"
        "    def write(self,s):\n"
        "        s.encode('cp1252'); return len(s)\n"
        "sys.stdout=L()\n"
        f"s=u.spec_from_file_location('d',r'{HERE / 'driver.py'}')\n"
        "m=u.module_from_spec(s); s.loader.exec_module(m)\n"
        "assert not m.UNICODE_OK\n"
        "m.render_scores(m.score_pack(__import__('json').load(open(P))))\n"
    ).replace("P", repr(str(write_pack(make_pack()))))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"crashed on a cp1252 console: {r.stderr[-300:]}"
    return "no UnicodeEncodeError"


@check("transcripts are read as UTF-8, not the local code page")
def t_utf8():
    ex = load_driver().find_extractor()
    src = ex.read_text(encoding="utf-8")
    assert 'encoding="utf-8"' in src, "extractor opens transcripts without an encoding"
    return "explicit encoding present"


# --- online (opt-in) -------------------------------------------------------

@check("published VERSION is reachable")
def t_online_version():
    m = load_driver()
    import time
    v = m._fetch("VERSION", bust=str(int(time.time()))).decode().strip()
    assert v, "VERSION came back empty"
    return v


@check("published files match the published VERSION (no stale CDN cache)")
def t_online_consistency():
    """The failure this guards against is subtle: raw.githubusercontent expires
    files independently, so a fresh VERSION can arrive with stale code — an
    install that reports the new number while running the old logic."""
    m = load_driver()
    import time
    remote = m._fetch("VERSION", bust=str(int(time.time()))).decode().strip()
    drv = m._fetch("driver.py", bust=remote).decode("utf-8")
    assert f'VERSION = "{remote}"' in drv, (
        f"published driver.py does not declare VERSION {remote} — "
        "the CDN served a mismatched set")
    return f"driver.py agrees it is {remote}"


# --- agent config (opt-in, still free) -------------------------------------

@check("Managed Agent config is well-formed")
def t_agent_config():
    cfg = UNIT / "skill-agent.json"
    assert cfg.exists(), f"{cfg} not found (only present in the source repo)"
    d = json.loads(cfg.read_text(encoding="utf-8"))
    for field in ("name", "description", "model", "system", "tools"):
        assert field in d, f"missing required field: {field}"
    assert d["model"]["id"].startswith("claude-"), f"odd model id {d['model']['id']}"
    assert len(d["system"]) > 2000, "system prompt suspiciously short"
    return f"{d['name']} on {d['model']['id']}"


@check("agent rubric is anchored to the published framework")
def t_agent_rubric():
    d = json.loads((UNIT / "skill-agent.json").read_text(encoding="utf-8"))
    s = d["system"]
    for term in ("Delegation", "Description", "Discernment", "Diligence"):
        assert term in s, f"system prompt never mentions {term}"
    assert "not averag" in s.lower() or "do not average" in s.lower(), \
        "system prompt does not forbid averaging the four scores"
    assert "terrain" in s.lower(), "system prompt does not separate terrain from skill"
    return "4D + no-average + terrain rules present"


@check("agent is told never to write")
def t_agent_readonly():
    s = json.loads((UNIT / "skill-agent.json").read_text(encoding="utf-8"))["system"]
    assert "never write" in s.lower() or "assess and report" in s.lower(), \
        "system prompt does not forbid writing"
    return "read-only instruction present"


# --- live agent (opt-in, COSTS MONEY) --------------------------------------

@check("live Managed Agent session responds")
def t_agent_live():
    ids = UNIT / ".skill-agent-ids"
    assert ids.exists(), "no agent created yet — run AGENT=skill ./setup.sh first"
    assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY not set"
    r = subprocess.run(["bash", str(UNIT / "run.sh"),
                        "Reply with exactly: SMOKE OK"],
                       capture_output=True, text=True, cwd=UNIT,
                       env={**os.environ, "AGENT": "skill"}, timeout=300)
    assert r.returncode == 0, f"run.sh exited {r.returncode}: {r.stderr[-300:]}"
    assert "SMOKE" in r.stdout.upper(), f"unexpected reply: {r.stdout[-200:]}"
    return "session completed"


LOCAL = [t_import, t_extractor_compiles, t_high, t_low, t_bounds, t_degenerate,
         t_schema_guard, t_digest, t_no_average, t_cadence, t_cli, t_json,
         t_ascii, t_utf8]
ONLINE = [t_online_version, t_online_consistency]
AGENT = [t_agent_config, t_agent_rubric, t_agent_readonly]
LIVE = [t_agent_live]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--online", action="store_true", help="check the update endpoint")
    ap.add_argument("--agent", action="store_true", help="validate the agent config (free)")
    ap.add_argument("--agent-live", action="store_true",
                    help="run a real agent session — COSTS MONEY")
    args = ap.parse_args()

    suite = list(LOCAL)
    if args.online:
        suite += ONLINE
    if args.agent or args.agent_live:
        suite += AGENT
    if args.agent_live:
        print("--agent-live will create a billable session.\n")
        suite += LIVE

    for t in suite:
        t()

    width = max(len(n) for _, n, _ in results)
    print()
    for status, name, detail in results:
        mark = "PASS" if status == PASS else "FAIL"
        print(f"  {mark}  {name.ljust(width)}  {detail}")
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n  {len(results) - failed}/{len(results)} passed"
          + (f", {failed} FAILED" if failed else "") + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
