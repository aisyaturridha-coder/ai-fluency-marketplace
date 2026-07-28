# aisya-skills

Claude Code plugin marketplace. One plugin so far: **ai-fluency**.

## Install

    /plugin marketplace add <your-org>/<this-repo>
    /plugin install ai-fluency@aisya-skills

Then `/run-ai-fluency` in any session, or ask "score my AI skill".

`/plugin update` pulls newer versions later.

## What ai-fluency does

Scores how you work with AI agents against Anthropic's 4D AI Fluency framework
— Delegation, Description, Discernment, Diligence — from your own Claude Code
transcripts.

Everything runs locally. No API key, no billing, no network call. Each person
scores their own sessions; results are written to `~/.claude/ai-fluency/` and
go nowhere else. Nothing is collected centrally.
