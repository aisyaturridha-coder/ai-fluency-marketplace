---
name: run-ai-fluency
description: Run the AI fluency analysis — score your own AI collaboration skill against Anthropic's 4D framework from local Claude Code transcripts. Use for "run the fluency analysis", "score my AI skill", "how good am I at using AI", "what should I improve", "how often should I re-analyse", "analyse my sessions", "/run-ai-fluency". Scores locally with no API call or billing.
---

# Run the AI fluency analysis

Scores how well the operator collaborates with AI agents, against Anthropic's
**4D framework** (Delegation, Description, Discernment, Diligence), using
evidence distilled from their own Claude Code transcripts in
`~/.claude/projects`.

Two halves. **The driver is the agent path** — it runs the same rubric locally,
so you get scores, ranked practices, and a re-analysis cadence with no network
call, no agent to create, and nothing billed. The second half (`assess.sh`)
posts the pack to a Managed Agent for a written narrative assessment; that
costs money and is documented last.

All paths below are relative to the unit root — the directory containing
`extract-evidence.py`. The driver resolves the root from its own location, so
it works from any working directory.

## Install (sharing this with someone else)

The skill is self-contained when `extract-evidence.py` sits **beside**
`driver.py`; the driver checks there first, then falls back to the unit root.
So the whole thing travels as one directory.

**For a team — publish it as a plugin.** Push a marketplace repo (see
`SKILLS/ai-fluency-marketplace/` for a working one) and everybody runs two
lines once:

```bash
/plugin marketplace add <org>/<repo>
/plugin install ai-fluency@aisya-skills
```

The skill then loads in every session automatically, and `/plugin update`
ships later versions. When it runs from a plugin checkout the driver writes
results to `~/.claude/ai-fluency/` instead of beside the code, so a
`/plugin update` git pull never collides with anyone's data. Override with
`AI_FLUENCY_HOME` if you want them elsewhere.

**Per person, works in every project** — drop it in the personal skills dir:

```bash
cp -R run-ai-fluency ~/.claude/skills/
~/.claude/skills/run-ai-fluency/driver.py check
```

**Per repo, everyone who clones gets it** — commit it into the project:

```bash
cp -R run-ai-fluency <repo>/.claude/skills/
```

> **Never share the folder this was developed in by copying it wholesale.**
> That directory also holds `.env` (an API key), `evidence.json`, and
> `reports/` — the author's own behavioural data and scores. Ship only the
> `run-ai-fluency/` directory.

Each person scores their **own** transcripts. Nothing about anyone else is
readable from this tool, and no scores are baked into it. The bundled
`.gitignore` keeps each person's `evidence.json` and `reports/` out of any
repo it lands in.

## Prerequisites

Nothing to install. Python 3.9+ (stdlib only) and the transcripts themselves.

```bash
./.claude/skills/run-ai-fluency/driver.py check
```

Verified output on a working setup:

```
  PASS  python3                     3.14.4
  PASS  extract-evidence.py         extract-evidence.py
  PASS  transcripts                 132 .jsonl under ~/.claude/projects
  INFO  skill-agent.json            absent — only needed for the paid API path
  INFO  agent created               no — local scoring still works
  INFO  evidence.json               not yet extracted
```

Only the three `PASS` rows gate anything. The `INFO` rows describe the optional
paid API path — a share-bundle ships without it deliberately, and everything in
this skill works regardless.

## Run (agent path)

One command does the whole local pipeline — extract, score, practices, cadence:

```bash
./.claude/skills/run-ai-fluency/driver.py all
```

Or a step at a time:

```bash
./.claude/skills/run-ai-fluency/driver.py extract
./.claude/skills/run-ai-fluency/driver.py cert
./.claude/skills/run-ai-fluency/driver.py score
./.claude/skills/run-ai-fluency/driver.py practices
./.claude/skills/run-ai-fluency/driver.py cadence
```

Useful flags:

```bash
./.claude/skills/run-ai-fluency/driver.py extract --days 30
./.claude/skills/run-ai-fluency/driver.py extract --no-samples
./.claude/skills/run-ai-fluency/driver.py score --json
./.claude/skills/run-ai-fluency/driver.py cadence --pack /tmp/w30.json
```

`--no-samples` strips every prompt excerpt, leaving metrics only. `--json`
emits scores, practices, and cadence as one object for programmatic use.
Set `NO_COLOR=1` when capturing output to a file.

### What `score` gives you

Each dimension is 1–5, averaged from named sub-signals, with the value that
drove each one. Output shape (figures are per-operator — yours will differ):

```
  ██···  Delegation   2/5  (2.5 raw)
      ~ runway median (tool calls/instruction): 6 → 3/5
      ! spend on top model tiers %: 94.0 → 1/5
  ████·  Discernment  4/5  (4.5 raw)
        steering latency (agent msgs, median): 1 → 5/5
```

`!` marks a sub-signal at 2 or below, `~` marks 3. The driver also detects the
**asymmetry** that the framework says matters most — weak Description with
strong Discernment is the safe one (costs time); the inverse ships polished
mistakes faster than they can be caught.

### What `practices` gives you

Ranked by weakest sub-signal, each naming the metric it should move so the
next run can check whether it worked:

```
  1. Route by task weight, not by habit
     Delegation · currently 94.0 → 1/5
     Target: spend on top tiers → target under 70%
```

### What `cert` gives you

A self-issued record with **three headline facts instead of one fake average** —
the framework forbids averaging the four, because an average hides an operator
strong on one dimension and dangerous on another:

```
  Profile (D·D·D·D)   2 · 2 · 4 · 2
  Floor               2/5  binding constraint: Description
  Safety posture      favourable
  Digest              <64 hex chars, unique to this pack>
```

**Scores in these samples are illustrative.** Each operator scores their own
transcripts; nobody else's numbers are readable from this tool, and none are
baked into it.

The digest is a SHA-256 over the window, session count, and every scored value.
**Reproducibility is the entire claim** — nothing accredits this, and the command
says so in its own output.

Verification means re-running `cert` against **the same pack**, which is exactly
deterministic. Use `--freeze` to archive that pack so the record stays checkable:

```bash
./.claude/skills/run-ai-fluency/driver.py cert --freeze
./.claude/skills/run-ai-fluency/driver.py cert --pack reports/cert-<digest8>.json
```

Re-running `extract` gives a *different* digest, and that is correct rather than
broken — see Gotchas.

### What `cadence` gives you

How often re-analysis is worth doing, **derived from the operator's own
session rate** rather than asserted. For a proportion metric compared across
two windows, the detectable difference at 95% confidence is
`1.96 × sqrt(2p(1−p)/n)`; solving for `n` gives sessions per window:

```
  Observed: <N> sessions over <D> days = <rate>/day

  To detect     sessions needed   days at your rate
  5pp move      450               199
  15pp move     50                23
  20pp move     29                13

  Recommended: re-run every ~23 days (50 sessions)
```

The sessions-needed column is fixed by the statistics; the days column and the
recommendation scale to whatever rate the operator actually works at, so a
heavier user re-runs sooner and a lighter one later.

The recommendation is the smallest window that resolves a 15-point move.
Running more often surfaces movement indistinguishable from chance. It also
reports whether there are 3+ monthly points yet — below that, trajectory
claims are not supportable.

## Direct invocation

To reuse the scorer without the CLI:

```bash
python3 -c "
import sys; sys.path.insert(0, '.claude/skills/run-ai-fluency')
import importlib.util as u
s = u.spec_from_file_location('drv', '.claude/skills/run-ai-fluency/driver.py')
m = u.module_from_spec(s); s.loader.exec_module(m)
pack = m.load_pack(m.DEFAULT_PACK)
print({k: v['score'] for k, v in m.score_pack(pack).items()})
print('cadence days:', m.cadence(pack)['recommended_window_days'])
"
```

Verified output: `{'Delegation': 2, 'Description': 2, 'Discernment': 4, 'Diligence': 2}`
and `cadence days: 23`.

## Run (API path — costs money, NOT exercised in this session)

`./assess.sh` posts the pack to a Managed Agent for a written narrative and
saves a timestamped report to `reports/`. It needs `AGENT=skill ./setup.sh`
first (creates a billable agent) and an `ANTHROPIC_API_KEY`. Everything in
this skill's verified sections works without it — reach for `assess.sh` only
when the narrative is wanted on top of the numbers.

## Gotchas

- **The driver is stricter than a human read of the same pack.** It scored
  Diligence 2 where an earlier hand assessment said 3, because it weights
  `deployed_without_commit_sessions: 11` and `plan_mode: 0.8%` at 1/5 each.
  Mechanical scoring is reproducible, not wiser — treat a gap between the two
  as a prompt to look at the sub-signals, not as one being wrong.
- **The cert digest is not stable across re-extraction, by design.** The
  operator keeps working, and the assessment includes the session running the
  assessment — so every `extract` picks up sessions that did not exist before.
  Two extractions a second apart matched; extractions minutes apart did not.
  Determinism holds against a *fixed* pack (verified: three identical runs), which
  is why `--freeze` exists. **A changed digest is not evidence that anything
  improved** — check the scores, not the hash.
- **A stale `evidence.json` silently predates the rubric.** Packs below schema
  v3 lack the cost block the Delegation score needs. The driver refuses them
  (`pack is schema v1; this driver needs v3+`) rather than scoring around the
  hole — re-run `extract`.
- **`extract` overwrites `evidence.json` in the unit root by default.** Pass
  `--pack` when you want a windowed pack side by side with the full one;
  otherwise a `--days 30` run replaces the full-history pack.
- **`spend on top model tiers %` reads ~100% for most Claude Code users.**
  Everything except Sonnet and Haiku counts as a top tier, so a default Opus
  session scores 1/5 here. That is the intended reading — it means nothing is
  being routed by task weight — but do not mistake it for a misconfiguration.
- **Two monthly points is not a trend.** The cadence command says so
  explicitly. Do not let an assessment claim trajectory before three.
- **zsh aborts on a non-matching glob.** The skills-discovery probe
  `grep ... "$d"/.claude/skills/*/SKILL.md` exits 1 under zsh when a directory
  has no skills, killing the whole loop. Use `find "$d/.claude/skills"
  -maxdepth 2 -name SKILL.md` instead.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pack is schema v1; this driver needs v3+` | `evidence.json` from an older extractor | `driver.py extract` |
| `no evidence pack at …` | never extracted, or wrong `--pack` | `driver.py extract` |
| `wrote … — 0 sessions, 0 projects` | extractor swallowed a per-file exception on every file (a `__slots__` mismatch does this) | the extractor now exits 1 on an empty pack; if it recurs, run `python3 -m py_compile extract-evidence.py` and read the skipped-file warnings on stderr |
| `no matches found: …/SKILL.md` | zsh glob with no match | use `find`, see Gotchas |
| Colour codes in a captured file | ANSI escapes | prefix `NO_COLOR=1` |
| `A[@]: unbound variable` from `assess.sh` | macOS bash 3.2 expands an empty array under `set -u` | already guarded with `${arr[@]+"${arr[@]}"}`; keep that idiom |
