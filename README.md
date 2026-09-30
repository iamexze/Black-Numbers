# Black Number

A voice-activated, self-improving personal assistant for your Mac — a Jarvis you
actually own. It listens, reasons, and controls the machine through 81 skills,
with a hard safety boundary between what it does freely and what it asks you
about first.

It is built to run **before you configure anything**: clone it, run it, and it
works — typed input, an offline command brain, voice out, and every skill live.
Add a key and a microphone and the same assistant becomes conversational and
hands-free. Nothing is rewritten; capabilities light up.

```bash
git clone https://github.com/iamexze/Black-Numbers.git && cd Black-Numbers
python3 -m black_number                       # starts talking immediately
python3 -m black_number "what's the weather"  # one-shot mode
python3 -m black_number serve                 # web console on http://127.0.0.1:3002
python3 tests/test_core.py                    # 57 checks, including the safety invariants
```

No dependencies. macOS, Python 3.11+.

## What it can do

81 skills in 14 families. Everything below was run against this machine, not
sketched.

| Family | What it covers |
|---|---|
| **system** | volume, brightness, open/quit apps, battery, clock, screenshot, frontmost app |
| **time** | timers, reminders, focus sessions, stopwatch — on a real background scheduler |
| **world** | weather, forecast, headlines, exact arithmetic, unit conversion, definitions |
| **network** | connectivity, local and public IP, ping, DNS, port owners, Wi-Fi power, throughput |
| **machine** | load, memory, disk, busiest processes |
| **repos** | git status and log, code search, project scan, run a test suite, open in an editor |
| **media** | play/pause/skip in Music or Spotify, now playing, dark mode, lock, sleep, keep awake, wallpaper |
| **personal** | Reminders, Calendar, Notes — read and write, through AppleScript |
| **clipboard & notes** | read/write the clipboard, a timestamped Markdown notebook, search across notebooks |
| **files & shell** | list, read, find, and run commands **through the safety gate** |
| **web** | keyless search and page fetch |
| **security** | audit this Mac's own hardening; authorized-only port checks |
| **protocols** | named routines it can be taught and run by name |
| **self** | diagnose its own failures, measure its own usage, learn corrections, run its own tests |

Ask in plain words. With no API key an offline table maps direct phrasings onto
skills — `what's the weather`, `set a timer for ten minutes`, `remind me to call
mom in 20 minutes`, `am I online`, `run the sitrep protocol`, `how's the machine`,
`what's 15% of 2400`, `note down buy coffee`. With a key, it holds a conversation
and chains tools.

## Protocols — teaching it a routine

A protocol is an ordered set of skills under one name. Five ship built in
(`sitrep`, `morning`, `diagnostics`, `workspace`, `wind_down`), and you can teach
more in one sentence:

```
run the sitrep protocol
protocol_define name=coffee steps="clock; set_timer duration='4 min' label=french press"
```

They are data, not code — saved as JSON, readable before you run one, available
immediately, no restart and no model required.

**A protocol is not a way around the gate.** It executes nothing itself; every
step goes back through the registry and is confirmed on its own merits. A
protocol containing a destructive step prompts exactly as that step would if you
had asked for it directly. The test suite pins this down, and I verified it by
mutation-testing: rewire `protocol_run` to call skills directly and the suite
fails.

## The local web console

```bash
python3 -m black_number serve            # http://127.0.0.1:3002
python3 -m black_number serve --port 3010 --no-browser
```

The same assistant in a browser: a transcript, the full skill list by risk level,
your protocols as one-click buttons, live pending timers, and the configuration
it is actually running on. Confirmations appear as a modal — nothing that changes
state runs until you click.

It is a second **front end**, not a second assistant. Both the terminal and the
console build the assistant through one function in `ui/runtime.py`, so they
cannot drift apart on the gate, the registry or the risk policy. A test asserts
that they both go through it.

Four security properties, each mutation-verified:

- **Loopback only.** It binds `127.0.0.1`, never `0.0.0.0`, and that is not a
  setting. An API that can control your Mac does not belong on the LAN.
- **Every `/api` call needs a token** in the `X-BN-Token` header, injected into
  the page server-side so it never appears in a URL. A page on another origin
  cannot set a custom header without a CORS preflight, and this server answers no
  preflight and sends no CORS headers — so the browser refuses before the request
  is made. Without this, any site open in your browser could POST to
  `localhost:3002` and run skills.
- **A foreign `Origin` is rejected outright**, as a second line of defence.
- **Confirmation fails closed.** The gate is synchronous: it publishes the prompt
  and blocks. If no answer arrives in 120 seconds — tab closed, browser crashed,
  nobody looking — it returns *no*. Silence is never consent.

One assistant, one lock: requests that drive the agent are serialised (a second
one gets `429` rather than interleaving), while confirmations and event polling
deliberately do not take that lock — they have to answer while a turn is blocked
waiting on them, and that ordering is what stops it deadlocking on its own safety
prompt.

## The upgrade switches

Everything sits behind an interface, so each is a one-line `.env` change. Copy
`.env.example` to `.env`:

| Switch | Default | Upgrade to |
|---|---|---|
| `BN_BRAIN` | `offline` (no key) | `anthropic` (add `ANTHROPIC_API_KEY`) or `ollama` (local) |
| `BN_EFFORT` | `high` | `medium` / `low` for snappier routine commands |
| `BN_STT` | `text` | `macos` (on-device mic) — `pip install -e '.[macspeech]'` |
| `BN_TTS` | `macos_say` | `none` for silent |
| `BN_ADDRESS` | *(none)* | `sir`, `boss`, your name — how it addresses you |
| `BN_CITY` | *(geolocate)* | your city, so "what's the weather" needs no argument |
| `BN_CONFIRM` | `trusted` | `always` (confirm everything) or `never` (allowlist only) |

It never fails to start because something is missing. Each layer degrades to the
one below, and the startup report says which layer you are actually on — a key
with no SDK installed reports `offline`, with the reason, rather than claiming a
brain that cannot run.

## Architecture

```
black_number/
├── core/        config · JSONL transcript logger · scheduler · presentation
├── brain/       reasoning behind one interface:
│                  anthropic · ollama · offline (deterministic) + a fallback router
├── speech/
│   ├── stt/     text · macOS on-device speech (SFSpeechRecognizer)
│   └── tts/     macOS `say` · silent
├── skills/      the unit of capability — one contract, 14 families
├── safety/      policy (what needs confirming) + gate (the one checkpoint)
├── agent/       the perceive→think→act loop, and two-tier memory
└── ui/          runtime (the one composition root) · cli · web + console.html
```

**One idea runs through all of it:** a capability is a `Skill` with a declared
risk level, and the registry routes every call through a single safety `Gate`
before anything that changes state runs. Adding a capability is registering a
`Skill`; nothing in the core changes.

## Safety, concretely

- Every skill declares a **risk**: read-only, reversible, mutating, destructive.
- Under the default `trusted` policy, read-only runs freely; anything that changes
  state is confirmed with the exact action shown (`run shell: rm x`).
- The shell skill classifies each command against a **read-only allowlist** — an
  unknown command is treated as state-changing and confirmed, never the reverse.
- File skills are rooted at your home directory; notebook names are sanitised so
  a name like `../../etc/passwd` cannot leave the notes folder.
- Text bound for AppleScript is escaped in one place, so a reminder called
  `"; do something else` cannot change the meaning of the script carrying it.
- Arithmetic is parsed into an AST and walked against a whitelist. `eval` is never
  called; `__import__('os')`, attribute access and `2**99999999` are each refused
  with a reason.

The suite sweeps the **live registry**: every skill declaring reversible or worse
must prompt before running. Declining means nothing executes, so the check is safe
to run against the real skill set — and it cannot be forgotten when a skill is
added.

### The security skill

Scoped, in code, to **defensive and authorized use**:

- The default action audits **this Mac's own posture** — firewall, FileVault,
  screen lock, updates, exposed sharing services — and changes nothing.
- Any check against a **named target** requires you to affirm, in the moment, that
  you are authorized to test it. This prompt is **never** auto-allowed, even under
  `BN_CONFIRM=never` — enforced and tested.
- Public-internet targets are refused outright. Mass scanning, exploitation of
  third-party systems, credential attacks and detection evasion are out of scope
  by design, and the module will not grow to include them.

## Self-improving, as a mechanism

Every turn — each utterance, tool call, confirmation, scheduled firing and
outcome — is appended to a JSONL transcript. That transcript is not for debugging;
it is what the assistant reads about itself.

- `self_diagnose` ranks its own recent failures, collapses similar ones into a
  shape, and names what would fix each — including which file the fix belongs in.
- `self_metrics` measures its own usage: requests, actions, success rate, what you
  actually use it for.
- `self_inventory` compares what it can do against what it has ever done. A
  registered skill that never runs is usually unreachable rather than unwanted.
- `self_lesson` records a correction that is injected into the system prompt on
  every later turn, in this session and future ones. Lessons are kept apart from
  facts and stated as binding, because a correction buried in a list of trivia is
  a correction that gets ignored.
- `self_test` runs its own suite and reports the result honestly.

Self-extension is deliberately **declarative**: protocols let it acquire new
behaviour as data. It does not write and load its own Python. That line is
intentional — a code-generating skill is a much larger safety surface than a
confirmation prompt can cover, and nothing here needs it yet.

## Testing

```bash
python3 tests/test_core.py      # 57 checks, no pytest required
```

The load-bearing tests are the safety ones, and they are mutation-verified.
Each of these breaks the suite: removing the gate, letting protocols bypass it,
weakening the offline brain's guards, breaking the failure grouper, binding the
console to `0.0.0.0`, dropping its token or origin check, leaking a CORS header,
or making confirmation fail open. Several of the
tests exist because they caught a real bug during development:

- `schedule_add` logged a field named `kind`, which collided with the logger's own
  positional parameter and raised `TypeError` at the call site. Every timer would
  have failed to set, and the first firing would have killed the clock thread.
  Fixed at both ends: the call sites were renamed, and `Log.event`'s `kind` is now
  positional-only so logging can never break its caller again.
- `parse_duration("half an hour")` returned 3630 seconds instead of 1800, because
  the article in "half **an** hour" was counted as a separate quantity.
- Stripping thousands separators from `1,250` also ate the argument separators in
  `max(3,9,2)`, turning it into `max(392)`.
- The failure grouper read the apostrophe in `Couldn't` as an opening quote, so no
  two failures ever grouped — which silently defeated the whole diagnosis, since
  almost every failure message contains a contraction.
- The console built its allowed-origin list from the *requested* port, which is
  `0` when asking for any free port — so on an ephemeral port it rejected its own
  browser. The bound port is only knowable after binding.

## Roadmap

1. **Now** — 81 skills, protocols, scheduler, safety gate, self-diagnosis,
   offline + Anthropic brains, memory with lessons, voice out, a local web
   console, 57 tests. ✅
2. Wake-word listening and barge-in with on-device speech (`BN_STT=macos`).
3. A menu-bar runtime — a third front end on the same composition root.
4. A self-critique loop that proposes protocol changes from its own transcript.

## Requirements

macOS, Python 3.11+. No dependencies for the base. Optional extras:
`pip install -e '.[anthropic,macspeech,web,dev]'`.
