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
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
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


@check("HTML report matches the circulated certificate and carries the scores")
def t_report():
    m = load_driver()
    pack = make_pack()
    sc = m.score_pack(pack)
    html = m.build_report(pack, sc, "tester")
    # The record deliberately mirrors the certificate already circulated to the
    # team: cert, transcript, validity window, method. Ranked advice is a
    # separate deliverable — `driver.py practices`, not this document.
    for section in ("Certificate of AI Fluency", "Transcript of record",
                    "Validity window", "Method and limits"):
        assert section in html, f"report is missing the {section!r} section"
    cert = m.certificate(pack, sc)
    assert cert["digest"] in html, "digest not embedded"
    assert cert["profile"] in html, "profile not embedded"
    for dim, data in sc.items():
        for sub in data["subs"]:
            assert sub["label"] in html, f"sub-signal missing: {sub['label']}"
    assert html.count("<div") == html.count("</div>"), "unbalanced divs"
    # A teammate holding the file must be able to install the skill and score
    # their own — that link is the one and only outbound reference allowed.
    assert m.SKILL_PACKAGE_URL in html, "skill-package link missing"
    for load in ('src="http', '@import', '<link rel="stylesheet"'):
        assert load not in html, f"report loads a remote asset: {load}"
    assert html.count('href="http') == 1, "unexpected outbound link"
    return f"{len(html) // 1024} KB, renders offline"


@check("every surface formats measured values identically")
def t_format_consistency():
    """The record one person shares must be formatted like everyone else's.

    Terminal, HTML and PDF each render the same sub-signals; if any of them
    formats a number its own way, two operators' certificates stop being
    comparable. Every path must go through fmt_value.
    """
    m = load_driver()
    pack = make_pack()
    sc = m.score_pack(pack)
    html = m.build_report(pack, sc, "tester")
    pdf = m.build_print_html(pack, sc, "tester")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.render_scores(sc)
        m.render_practices(m.practices_for(sc, limit=6))
    term = buf.getvalue()

    for dim, data in sc.items():
        for sub in data["subs"]:
            shown = m.fmt_value(sub["value"])
            for surface, doc in (("terminal", term), ("html", html), ("pdf", pdf)):
                assert shown in doc, (
                    f"{surface} does not render {sub['label']!r} as {shown!r} "
                    "— a surface is formatting values on its own")

    # Whole floats must not render as "6.0" for one operator and "6" for another.
    assert m.fmt_value(6.0) == m.fmt_value(6) == "6", "whole floats drift"
    assert m.fmt_value(27.4) == "27.4", "current format changed"
    assert m.fmt_value(99.71) == "99.71", "current format changed"
    assert m.fmt_value(0.8) == "0.8", "current format changed"
    return "terminal = html = pdf"


@check("print edition paginates correctly and keeps both disclaimers")
def t_print_edition():
    m = load_driver()
    pack = make_pack()
    sc = m.score_pack(pack)
    doc = pdf = m.build_print_html(pack, sc, "tester")
    html = m.build_report(pack, sc, "tester")
    # Four pages when the pack carries cost, three without it. Any other count
    # means a section landed on a page the layout never accounted for.
    n = doc.count('class="page')
    assert n == 4, f"expected 4 pages with a cost block, got {n}"
    assert "@page" in doc and "210mm" in doc, "not sized for A4"
    for section in ("Certificate of AI Fluency", "Transcript of record",
                    "What this costs", "Validity window", "Method and limits",
                    "How to raise these scores"):
        assert section in doc, f"print edition is missing {section!r}"
    assert "Page 2 of 4" in doc and "Page 4 of 4" in doc, "footers did not renumber"

    # A pack with no cost block drops the page and renumbers rather than
    # rendering an empty one.
    nocost = make_pack()
    nocost["cost"] = {}
    d3 = m.build_print_html(nocost, m.score_pack(nocost), "tester")
    n3 = d3.count('class="page')
    assert n3 == 3, f"expected 3 pages without cost, got {n3}"
    assert "What this costs" not in d3, "empty cost section rendered"
    assert "Page 2 of 3" in d3 and "Page 3 of 3" in d3, "footers did not renumber down"
    # The scroll layout reads as a credential, so the denial must survive.
    assert "No organisation has accredited it" in doc, "disclaimer dropped"
    assert "confers no qualification" in doc, "disclaimer weakened"
    cert = m.certificate(pack, sc)
    assert cert["digest"] in doc, "digest not embedded"
    for dim, data in sc.items():
        for sub in data["subs"]:
            assert sub["label"] in doc, f"sub-signal missing: {sub['label']}"
    # The priced total is large and reads as money unless the caveat travels
    # with it. Same class of claim as the accreditation denial.
    for doc, where in ((html, "html"), (pdf, "pdf")):
        assert "API-equivalent" in doc, f"{where}: cost caveat missing"
        assert "not money spent" in doc, f"{where}: 'not money spent' dropped"
    return "4 pages w/ cost, 3 without; disclaimers intact"


@check("pdf rendering shells out safely and degrades without a browser")
def t_pdf_invocation():
    m = load_driver()
    src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src, "pdf path introduced a shell"
    # The browser is invoked as an argument list; a str command would let a
    # path with spaces (or worse) be re-parsed by a shell.
    i = src.index("def render_pdf")
    body = src[i:i + 700]
    assert "[chrome," in body, "browser not invoked as an argument list"
    assert "--print-to-pdf=" in body, "not actually printing to pdf"
    # find_chrome must simply return None when nothing is installed, so the
    # caller can fall back to "print it by hand" instead of raising.
    real_which, real_exists = shutil.which, pathlib.Path.exists
    try:
        shutil.which = lambda *_a, **_k: None
        pathlib.Path.exists = lambda self: False
        m.shutil.which = shutil.which
        assert m.find_chrome() is None, "claimed a browser that is not there"
    finally:
        shutil.which = real_which
        pathlib.Path.exists = real_exists
        m.shutil.which = real_which
    return "argument list, graceful fallback"


@check("report escapes values instead of injecting them raw")
def t_report_escaping():
    m = load_driver()
    pack = make_pack()
    html = m.build_report(pack, m.score_pack(pack), '<img src=x onerror=alert(1)>')
    assert "<img src=x" not in html, "subject name was injected unescaped"
    assert "&lt;img" in html, "subject name was not escaped"
    return "subject escaped"


@check("redaction covers the credential formats people actually paste")
def t_redaction():
    ex = load_driver().find_extractor()
    spec = importlib.util.spec_from_file_location("ev", ex)
    ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
    # Fixtures are assembled at runtime. Committing literal key-shaped strings
    # trips secret scanners (GitHub push protection blocks it) and teaches the
    # wrong habit, so each prefix is joined to its body here instead.
    j = "".join
    cases = {
        "anthropic":       j(["sk-", "ant-", "api03-AbCdEfGh1234567890xyz"]),
        "openai":          j(["sk-", "proj-", "AbCdEf1234567890GhIjKlMnOp"]),
        "github":          j(["github", "_pat_", "11ABCDEFG0aBcDeFgHiJkL"]),
        "aws key id":      j(["AKIA", "IOSFODNN7EXAMPLE"]),
        "aws secret":      j(["aws_secret", "_access_key", "=wJalrXUtnFEMI/K7MDENG"]),
        "stripe":          j(["sk", "_live_", "51AbCdEfGhIjKlMnOpQrStUv"]),
        "slack":           j(["xox", "b-", "123456789012-1234567890123-AbCdEfGhIj"]),
        "google":          j(["AIza", "SyD-1234567890abcdefghijklmnop"]),
        "private key":     j(["-----BEGIN ", "RSA PRIVATE", " KEY-----"]),
        "jwt":             j(["eyJhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxIn0.", "abc123"]),
        "email":           j(["someone", "@", "company.com"]),
        "password assign": j(['password: "', 'hunter2horse"']),
    }
    missed = [n for n, v in cases.items() if ev.redact(v) == v]
    assert not missed, f"redaction missed: {', '.join(missed)}"
    for prose in ("fix the login bug in auth.ts", "run npm test then commit"):
        assert ev.redact(prose) == prose, f"redaction mangled ordinary prose: {prose}"
    return f"{len(cases)} formats caught, prose untouched"


@check("the pack is written private, not world-readable")
def t_perms():
    import stat
    ex = load_driver().find_extractor()
    out = pathlib.Path(tempfile.mkdtemp()) / "evidence.json"
    subprocess.run([sys.executable, str(ex), "--days", "1", "-o", str(out)],
                   capture_output=True)
    if not out.exists():
        return "skipped — no transcripts on this machine"
    mode = out.stat().st_mode
    assert not (mode & stat.S_IROTH), "pack is world-readable"
    assert not (mode & stat.S_IRGRP), "pack is group-readable"
    return oct(stat.S_IMODE(mode))


@check("update cannot be told to write arbitrary paths")
def t_update_paths():
    m = load_driver()
    for name in m.UPDATE_FILES:
        assert "/" not in name and ".." not in name, f"unsafe entry: {name}"
    src = (HERE / "driver.py").read_text(encoding="utf-8")
    assert "HERE / name" in src, "update writes somewhere other than the skill dir"
    return f"{len(m.UPDATE_FILES)} fixed filenames, no traversal"


@check("no shell=True, eval, or exec of remote data")
def t_no_injection():
    for f in ("driver.py", "extract-evidence.py"):
        path = HERE / f
        if not path.exists():
            path = UNIT / f
        src = path.read_text(encoding="utf-8")
        assert "shell=True" not in src, f"{f} uses shell=True"
        assert "pickle" not in src, f"{f} imports pickle"
    return "clean"


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
    sha = m._resolve_head()
    v = m._fetch("VERSION", bust=sha).decode().strip()
    assert v, "VERSION came back empty"
    return f"{v} @ {sha[:10]}"


@check("published files match the published VERSION (no stale CDN cache)")
def t_online_consistency():
    """The failure this guards against is subtle: raw.githubusercontent expires
    files independently, so a fresh VERSION can arrive with stale code — an
    install that reports the new number while running the old logic."""
    m = load_driver()
    sha = m._resolve_head()          # one immutable commit for the whole set
    remote = m._fetch("VERSION", bust=sha).decode().strip()
    drv = m._fetch("driver.py", bust=sha).decode("utf-8")
    assert f'VERSION = "{remote}"' in drv, (
        f"published driver.py does not declare VERSION {remote} — "
        "the CDN served a mismatched set")
    return f"driver.py agrees it is {remote} @ {sha[:10]}"


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
         t_ascii, t_report, t_format_consistency, t_print_edition, t_pdf_invocation, t_report_escaping,
         t_redaction, t_perms, t_update_paths, t_no_injection, t_utf8]
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
