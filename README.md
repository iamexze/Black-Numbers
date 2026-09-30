# Black Number

A voice-activated, self-improving personal assistant for your Mac — a Jarvis you
actually own. It listens, reasons, and controls the machine through a growing set
of skills, with a hard safety boundary between what it does freely and what it
asks you about first.

It is built to run **before you configure anything**: clone it, run it, and it
works with typed input, an offline command brain, and a real set of skills. Add a
key and a microphone and the same assistant becomes conversational and hands-free
— nothing is rewritten, capabilities just light up.

```bash
git clone <this> && cd black-number
python3 -m black_number                 # starts talking immediately, typed input
python3 -m black_number "what's the time"   # one-shot mode
python3 tests/test_core.py              # 13 checks, including the safety invariants
```

## What works right now, with zero setup

- **Voice out** through macOS `say` (no install).
- **Offline brain**: direct commands map to skills with no API key — "volume up",
  "open Safari", "run a security audit", "what's the time", "search X".
- **Skills, live and real** — verified against this machine:
  - System: volume, brightness, open/quit apps, battery, clock, screenshot, focus.
  - Files & shell: list, read, find, and run commands **through the safety gate**.
  - Web: keyless search and page fetch.
  - Security: audit this Mac's own hardening; authorized-only port checks.
  - Self: capabilities, durable memory, read back its own transcript, status.

## The four upgrade switches

Everything is behind an interface, so each of these is a one-line `.env` change,
not a rewrite. Copy `.env.example` to `.env`:

| Switch | Default | Upgrade to |
|---|---|---|
| `BN_BRAIN` | `offline` (no key) | `anthropic` (add `ANTHROPIC_API_KEY`) or `ollama` (local) |
| `BN_STT` | `text` | `macos` (on-device mic) — `pip install -e '.[macspeech]'` |
| `BN_TTS` | `macos_say` | `none` for silent |
| `BN_CONFIRM` | `trusted` | `always` (confirm everything) or `never` (allowlist only) |

With a key the assistant holds real conversations and chains tools; without one it
still runs every skill from direct phrasing. It never fails to start because
something is missing — each layer degrades to the one below it.

## Architecture

```
black_number/
├── core/        config (env → .env → defaults) and the JSONL transcript logger
├── brain/       reasoning behind one interface:
│                  anthropic · ollama · offline (deterministic) + a fallback router
├── speech/
│   ├── stt/     text · macOS on-device speech (SFSpeechRecognizer)
│   └── tts/     macOS `say` · silent
├── skills/      the unit of capability — one contract, five families so far
│                  system_mac · files_shell · web_research · security · meta
├── safety/      policy (what needs confirming) + gate (the one checkpoint)
├── agent/       the perceive→think→act loop, and two-tier memory
└── ui/          the CLI runtime that assembles it all
```

**One idea runs through all of it:** a capability is a `Skill` with a declared
risk level, and the registry routes every call through a single safety `Gate`
before anything that changes state runs. There is no code path that runs a
mutating action unattended under the default policy — a property the test suite
enforces and I verified by mutation-testing it.

## Safety, concretely

- Every skill declares a **risk**: read-only, reversible, mutating, destructive.
- Under the default `trusted` policy, read-only runs freely; anything that changes
  state is confirmed with the exact action shown ("run shell: `rm x`").
- The shell skill classifies each command against a **read-only allowlist** — an
  unknown command is treated as state-changing and confirmed, never the reverse.
- File skills are rooted at your home directory.

### The security skill

Scoped, in code, to **defensive and authorized use**:

- The default action audits **this Mac's own posture** — firewall, FileVault,
  screen lock, updates, exposed sharing services — and tells you what's weak. It
  changes nothing. (On this machine it flagged pending updates and an exposed SSH
  service — real findings.)
- Any check against a **named target** requires you to affirm, in the moment, that
  you are authorized to test it. This authorization prompt is **never**
  auto-allowed, even under a permissive confirmation policy — enforced and tested.
- Public-internet targets are refused outright. Mass scanning, exploitation of
  third-party systems, credential attacks and detection-evasion are out of scope
  by design and the module will not grow to include them.

## Self-improving, as a real mechanism not a slogan

Every turn — each utterance, tool call, confirmation and outcome — is appended to
a JSONL transcript. The `review_self` skill already reads it back to summarise
what ran and what failed. That transcript is the substrate the next steps build
on: a maintenance loop that reads its own failures and proposes fixes, and a
skill-authoring skill that scaffolds new skills into `skills/` following the same
contract. The architecture is arranged so those are additions, not surgery.

## Roadmap

1. **Now** — working base: skills, safety gate, offline + Anthropic brains, memory,
   voice out, tests. ✅
2. Wake-word listening and barge-in with on-device speech (`BN_STT=macos`).
3. LLM-driven multi-step tasks with the full tool loop (needs a key).
4. A skill that writes skills, gated and reviewed before load.
5. A self-critique loop that reads the transcript and files its own improvements.

## Requirements

macOS, Python 3.11+. No dependencies for the base. Optional extras:
`pip install -e '.[anthropic,macspeech,web,dev]'`.
