# Compliance and cost

Read this before you deploy. It is written plainly and kept current.

## The short version

- As of **June 15, 2026**, Anthropic officially supports running the Agent SDK,
  the `claude -p` command, and **third-party apps built on them** against your
  Claude plan. Iris is exactly that: a third-party app that drives the official
  `claude` binary on your own subscription. This is a **supported path**, not a
  workaround.
- It is **not unlimited and not strictly free**. Programmatic use draws from a
  separate **monthly agent credit** ($20 Pro / $100 Max 5x / $200 Max 20x),
  metered at full API rates, with no rollover. You claim it once in your Claude
  account. When it runs out, requests either stop or bill at API rates,
  depending on whether you enabled usage credits.
- Iris never extracts or replays your OAuth token and never disguises its
  requests. It runs the real client. That keeps it clear of the thing Anthropic
  actually enforces against (token spoofing, which got tools like OpenCode and
  Crush legal requests in early 2026).
- One hard rule remains: **single-user only.** Run Iris against your own
  subscription, for yourself. Answering other people from your personal plan is
  still a violation.

## Why this is the supported path now

Anthropic's Help Center article "Use the Claude Agent SDK with your Claude plan"
spells it out: the monthly agent credit "covers Claude Agent SDK usage, the
`claude -p` command, and third-party apps built on the Agent SDK." That is a
direct, current statement that the thing Iris does is allowed and billed, not
prohibited.

This is a real shift from the pre-June-2026 reading, where the Consumer Terms
restricted bot/script access unless you used an API key. The agent credit is
Anthropic's answer to that: a sanctioned, metered lane for programmatic use of
your plan.

## What still applies

The single-tenant rule has not changed. From Anthropic's Claude Code legal page:

> Anthropic does not permit third-party developers to offer Claude.ai login or
> to route requests through Free, Pro, or Max plan credentials on behalf of
> their users.

So: each person runs Iris against their **own** plan via their own `claude`
login. Do not build a hosted, multi-tenant service on top of it, and do not
point your personal bot at a community and answer other members. `.env.example`
ships with `IRIS_ALLOWED_USER_IDS` so you can lock the bot to yourself in one
line. Use it.

## Cost, concretely

Position this as "included in your plan's monthly agent credit, then API rates,"
not "free."

- The credit is generous for personal use. On Max 20x ($200/month of credit) at
  Sonnet rates ($3 / $15 per million input/output tokens), that is on the order
  of tens of millions of tokens a month before you pay anything extra. A
  personal assistant rarely gets close.
- Iris is built to stay inside it. It only calls the model when a message
  actually arrives (one `claude -p` per message), so it burns **zero idle
  inference**. A naive always-listening process that re-runs a turn on every
  poll timeout would quietly drain the credit; Iris does not work that way.
- The one deliberate exception is **scheduled jobs** (`IRIS_SCHEDULED_JOBS`,
  off by default): the clock may launch a job whose instructions you recorded
  verbatim with `iris schedule` — never a conversation, never anything the
  system composed on its own. Every firing is a normal background job: grants
  re-clamped, parked when the credit guard runs hot, capped per rule per
  month, and skipped while the previous firing is still running. The usage
  ledger records every firing, and `iris usage` projects your month-end pace.
- To stretch the credit further: use `IRIS_MODEL=claude-haiku-4-5-...` for a
  cheaper brain, keep personas and context lean, and avoid wiring tools that
  balloon the prompt. The `browser` job grant is the heaviest tool Iris can
  wire (page snapshots bill like big pastes), and its profile holds only
  logins to your own accounts — browsing as someone else is out of scope.

Claim your monthly agent credit once in your Claude account, or programmatic
requests will not draw from it.

## Bottom line

For one person running Iris on their own subscription, this is a supported,
metered, low-cost setup with no token spoofing and no gray area. The moment you
serve other people or build a hosted service, you are outside the lane. None of
this is legal advice; it is a careful reading of Anthropic's current public
terms and help docs, and you are responsible for how you deploy.
