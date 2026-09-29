# Three defects: when a declaration stops being true at the point of enforcement

[The technical write-up](technical-write-up.md) describes the design as a set of claims: *the gate
fails closed*; *the policy engine owns the decision*; *the record accounts for every call*. This is the
record of three reviews in which the implementation had quietly stopped honouring one of them.

All three are the same shape, which is why they are worth keeping together: **a declaration that was
still true where it was written and no longer true where it was enforced** — a list, a port, a record.
None of them was a logic error in a function. Each was a boundary between two modules that both looked
correct in isolation.

---

## 1. Two lists of credential paths, and the gate's was the weaker one

**What the code did.** Two modules each decided what a credential path is. The scrubber, which redacts
command text before it is recorded, matched a shape list that included `.netrc` and `.env`. The
adapter, which decides whether a command reads a credential store and hands that to the policy engine,
matched its own shorter list — `SECRET_PATH_MARKERS` — which did not. Worse, the adapter's entries were
written with a leading `~` (`~/.ssh`), matched literally, so an *absolute* path to the same store
(`/home/agent/.ssh/config`) did not match either.

**Why it survived.** Neither list is wrong on its face, and no test compared them. The test that
existed looped over `SECRET_PATH_SHAPES` — the declaration — to build its cases, so it agreed with the
declaration by construction and could not observe a disagreement with it. That is the general failure
mode: a test derived from the thing it is supposed to check.

**Why it mattered.** Isolating only the adapter's matcher and holding the judgment constant, three
credential stores were not merely judged, they were **allowed**:

| command | old adapter list | shared declaration |
|---|---|---|
| `cat ~/.ssh/config` | phase 1 `block` | phase 1 `block` |
| `cat /home/agent/.ssh/config` | phase 2 **`allow`** | phase 1 `block` |
| `cat .netrc` | phase 2 **`allow`** | phase 1 `block` |
| `cat .env` | phase 2 **`allow`** | phase 1 `block` |
| `cat /workspace/notes.txt` *(control)* | phase 2 `allow` | phase 2 `allow` |

Read the second column and the third together. When the gate did not recognise the path, the call fell
to the gray middle, where a clean judgment produced `clean-call-permit-v1` — an *allow*. So the gate was
relying on the judgment model to notice a credential path that the deterministic phase had missed,
which is precisely what phase-1 policies exist to prevent. And because the scrubber's list was the
longer one, the record redacted a path in the same call that the gate had permitted. The evidence was
hidden and the action allowed.

**The fix.** One declaration in the domain — `SECRET_PATH_SHAPES` in `toolgate/domain/model.py` — read
by both consumers. The adapter's private list is gone; `_detects_secret_read` now asks the shared
declaration, and the scrubber builds its regular expression from the same tuple.

> A note on the earlier draft of this document: it claimed these paths "escaped both consumers". They
> did not. Measurement showed the scrubber had always caught absolute paths; only the gate was weak.
> The claim was corrected in the code comment and the test docstring as well as here.

**The trade-off, made on purpose.** The shared shapes match as *substrings* of a command, so a command
touching `.envrc` or `.env.example` is treated as touching a credential store, and a read of an
unrelated file whose path happens to contain one of the shapes is refused or escalated. That
over-matching is the safe direction for this decision, and it is documented at the declaration rather
than left as a surprise. The alternative — matching path components exactly — trades a false block for
a false allow, which is the wrong trade for a credential store.

**Evidence.** The test is written over *hard-coded* paths, not over the declaration, so it is capable of
disagreeing with the declaration — which is the whole point.
`test_a_credential_store_is_hidden_and_refused_however_it_is_written` walks a literal tuple of nine
paths — `("~/.ssh/config", "/home/agent/.ssh/config", "~/.aws/credentials", "~/.gnupg/secring.gpg",
".netrc", ".env", "id_rsa", "id_ed25519", "/etc/shadow")` — and asserts both that the gate refuses the
read and that the record does not contain the path. (The first version of this test looped over
`SECRET_PATH_SHAPES` instead, and passed for the wrong reason.)
`test_the_scrubber_covers_every_declared_shape` is the complementary check that the two consumers stay
joined. Two mutations prove the pair: restoring the adapter's short list fails the first, and dropping
`.env` or `.netrc` from the declaration fails both.

---

## 2. A default that reached the network

**The defect.** The engine takes a judgment client through a declared port:

```python
def __init__(self, principal, *, gate_authorizer, judgment_authorizer, decisions,
             policy_version, jev: Jev | None = None, ...):
    ...
    self.jev = jev or JevClient()          # ← a real HTTP client, reaching a billed API
```

The signature read as though the engine could not reach the network — you hand it its world — and then
it built one itself whenever a caller omitted the argument. The port was declared and then bypassed at
the exact point it was supposed to be load-bearing.

**What it had been hiding.** Seven tests across four files were omitting the argument, so every full
run made real billed HTTP calls to a paid judgment API. The suite looked hermetic and was not.

**The uncomfortable part: they passed.** All seven were green, because they asserted that the call
escalated — and an outage and a genuine low-confidence judgment both produce `escalate`. The record
distinguished them (`decision_reason: "jev_outage"` on the first, no such key on the second); the
assertions never looked. This is the failure mode a *perception* input invites: a non-deterministic
dependency that is invisible precisely when it degrades, because degradation and a real answer land in
the same band. The lesson generalises past this defect — a healthy judgment carries **no**
`decision_reason`, so any consumer distinguishing outcomes must use `.get(...)` and never assume the key
is there.

**The fix, and the fix's own fix.** `jev` became a required keyword parameter with no default. The
first mutation check then exposed something worse: restoring `jev: Jev | None = None` left the suite
**green**. Nothing pinned the absence of the default. A required collaborator that is only required by
convention is not required, so a test now asserts it structurally:

```python
def test_engine_builds_none_of_its_collaborators():
    for name in ("gate_authorizer", "judgment_authorizer", "decisions", "policy_version", "jev"):
        assert inspect.signature(Engine.__init__).parameters[name].default is inspect.Parameter.empty
```

With that in place the mutation fails, as it should. The seven call sites now name their client
explicitly, and a `keyless(monkeypatch)` helper strips both credential variables for tests that want the
no-key path deliberately. Only the two tests in `test_jev_live.py` still make real calls, which is what
they exist to do — and the suite reports its counts both ways (`245 passed` with a key,
`243 passed, 2 skipped` without).

**A related trap, documented rather than discovered twice.** Suppressing the key for a subprocess needs
care: the MCP client spawns a gateway with a minimal environment —
`HOME, LOGNAME, PATH, SHELL, TERM, USER` — so exported or blanked variables never reach it, and the
gateway loads `.env` itself. `TYPESAFE_API_KEY= JEV_API_KEY= … ` therefore does **not** suppress the
key for a spawned gateway; moving `.env` aside does. The demo's keyless run depends on that.

---

## 3. The one call the record could not account for

**The defect.** The decision record is written after a verdict is reached. A defect anywhere before that
point therefore produced a refusal for the caller and **no line at all** for the record. The invariant
the whole design leans on is one line per call — the log is the audit trail, the eval set and the tuning
data — and the single call the gate could not account for was the one it failed on.

**The fix.** The authorization body is wrapped so that *any* unexpected failure is recorded before it is
re-raised:

```python
try:
    ...                       # the whole decision
except Exception:
    self._record_failure(call_id, session, tool, command, start)
    raise                     # the caller's fail-closed refusal is unchanged
```

`_record_failure` appends a line with `decision: "block"`, `decision_reason: "engine_error"`,
`phase: 0`, `resource: "unknown"` and `cost_usd: 0.0`. Recording is itself best-effort — wrapped in
`except Exception: pass` — so a broken sink can never replace the original error with a second one.

**Why the band is `block` and not `escalate`.** This is the part worth being deliberate about, and it is
the reason the vocabulary has four entries rather than three. For an outage, a malformed response or an
exhausted budget, a human can still answer: nothing ran, the call is paused, escalation *is* the safe
outcome. An engine defect is different — nothing was paused, and the caller has already been told its
call failed. Recording it as an escalation would put a phantom item in a human's queue, and worse, an
approval would later rewrite the line to `allow` for a call that never executed. So the band recorded is
the band the caller was actually given.

**Evidence, and a second lesson in weak assertions.** Three tests cover it: that the failure is
recorded, that a failed call is *never* queued for a human, and that a failure while recording still
raises the original error. Two of the three initially **passed under the mutation** that removed the
failure record entirely — they asserted properties that happen to hold when nothing is recorded at all.
They were strengthened to assert the file's content directly (`read_text().strip()` is non-empty) and
the number of appends (`record.append_calls == 1`). All three now fail without the fix.

---

## How each fix was proven

Every new check was verified by reverting the fix and requiring the test to fail. A test that has never
failed has not been tested:

| mutation (the fix reverted) | test that must fail |
|---|---|
| restore the adapter's short credential list | `test_a_credential_store_is_hidden_and_refused_however_it_is_written` |
| drop `.env` / `.netrc` from `SECRET_PATH_SHAPES` | the above, and `test_the_scrubber_covers_every_declared_shape` |
| restore `jev: Jev \| None = None` | `test_engine_builds_none_of_its_collaborators` |
| drop the failure record from the handler | the three `test_trace.py` failure tests |
| narrow `except Exception` to `except ValueError` | `test_a_failure_while_recording_one_still_raises_the_original` (fails with `OSError`) |

Three of those rows are the whole value of the exercise. The credential test was *weak* before mutation
testing (it looped over the declaration), the signature default was *unpinned* until a mutation stayed
green, and two of the three `engine_error` tests were *vacuous* until a mutation passed. Reverting the
fix is what made each of those visible; reading the tests would not have.

The last row also shows the discipline catching the experimenter rather than the code: the first attempt
at that mutation deleted the `except` line and left a bare `try:`, which is a syntax error, and the
suite failed to collect. That is not a passing mutation test — it is a broken one — so it was redone as
a narrowed exception type, which fails for the right reason.

---

*These three are the defects found by review, not a claim that the code is now free of them. Known
remaining gaps — including the state sent to the judgment model not being scrubbed, which the spec
requires — are listed in the write-up's closing section.*
