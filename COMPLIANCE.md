# Compliance: how Iris relates to Anthropic's terms

Read this before you deploy. It is written plainly and it does not pretend the
situation is cleaner than it is.

## The short version

- Iris drives the **official `claude` binary** that you already installed and
  signed into. It never reads, copies, or reuses your subscription's OAuth
  token, and it never disguises requests as something they are not.
- That matters, because the clear, enforced Terms-of-Service violation in tools
  like this is **extracting the subscription token and spoofing the Claude Code
  client**. Iris does not do that. Anthropic has sent legal requests to projects
  that did (OpenCode, Crush). Iris is in a different category: a wrapper that
  runs the real client.
- Two real concerns remain, and they are yours to manage:
  1. **Automated access.** Anthropic's Consumer Terms restrict bot/script access
     to the service unless you use an API key. Driving `claude` from a bot is
     automated access on a subscription. Personal, individual automation of your
     own Claude Code is the most defensible reading, but it is not a guaranteed
     zero.
  2. **Serving other people is a clear violation.** Do not point Iris at a
     community and answer other members from your personal plan.
- If you need an always-on or multi-user agent with **no gray area at all**, use
  an **Anthropic API key** (pay per token). That is the fully sanctioned path,
  and Iris works with it too.

## What the terms actually say

Quotes are from Anthropic's live legal pages (current as of mid-2026).

**Consumer Terms of Service** (anthropic.com/legal/consumer-terms, effective
Oct 8 2025), prohibited uses:

> Except when you are accessing our Services via an Anthropic API Key or where
> we otherwise explicitly permit it, to access the Services through automated or
> non-human means, whether through a bot, script, or otherwise.

Same page, on accounts:

> You may not share your Account login information, Anthropic API key, or Account
> credentials with anyone else. You also may not make your Account available to
> anyone else.

**Claude Code legal page** (code.claude.com/docs/en/legal-and-compliance):

> OAuth authentication is intended exclusively for purchasers of Claude Free,
> Pro, Max, Team, and Enterprise subscription plans and is designed to support
> ordinary use of Claude Code and other native Anthropic applications.

> Anthropic does not permit third-party developers to offer Claude.ai login or
> to route requests through Free, Pro, or Max plan credentials on behalf of
> their users.

> Advertised usage limits for Pro and Max plans assume ordinary, individual
> usage.

## What this means for how you run Iris

| Use of Iris | Standing |
| --- | --- |
| You, alone, in your own Discord, low volume, on your subscription | Most defensible free use. Bot/script clause still applies, so not a guaranteed zero, but there is no impersonation and no account sharing. |
| Answering other people from your subscription | Clear violation. Do not do this. |
| Always-on, high volume, on your subscription | Pushes against "ordinary, individual usage" and is more likely to draw automated review. |
| Any of the above, but pointed at an **Anthropic API key** | Fully within terms. This is the clean path. |

`.env.example` ships with `IRIS_ALLOWED_USER_IDS` so you can lock the bot to
yourself in one line. Use it.

## Enforcement, realistically

Through 2026 the trend has been **silent server-side breakage** of
token-spoofing tools rather than mass account bans, with the harshest outcomes
(personal account suspension) clustered around plan upgrades and volume spikes
that trip automated review. Because Iris uses the official client rather than a
spoofed token, it is not the thing those technical countermeasures target. The
residual risk is the policy question above, not a header that Anthropic can
rotate to break you.

None of this is legal advice. It is a careful reading of public terms. You are
responsible for how you deploy this.
