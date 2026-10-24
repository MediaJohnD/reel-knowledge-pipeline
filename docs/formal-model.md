# Formal Model

This document specifies the pipeline as a mathematical object: the identity
function that keys every item, the classification that routes it, the state
machine that drives it, the failure semantics that bound retries, and the
concurrency discipline that keeps `state.json` consistent. Each claim names
the code that implements it, so the model can be checked against the source
rather than taken on trust. Where the implementation only approximates a
property, the gap is stated.

[`architecture.md`](architecture.md) is the module-level view;
this document is the behavioral contract underneath it.

## Contents

1. [Notation](#1-notation)
2. [Identity: normalization and content IDs](#2-identity-normalization-and-content-ids)
3. [Routing: the classification function](#3-routing-the-classification-function)
4. [The item state machine](#4-the-item-state-machine)
5. [Failure semantics and retry policy](#5-failure-semantics-and-retry-policy)
6. [Stage memoization and crash recovery](#6-stage-memoization-and-crash-recovery)
7. [Invariants](#7-invariants)
8. [Concurrency model](#8-concurrency-model)
9. [Complexity](#9-complexity)
10. [Safety properties](#10-safety-properties)
11. [Output contract](#11-output-contract)
12. [Known gaps and threats to validity](#12-known-gaps-and-threats-to-validity)

## 1. Notation

| Symbol | Meaning |
| --- | --- |
| $\mathcal{U}$ | Set of URL strings |
| $\nu : \mathcal{U} \to \mathcal{U}$ | URL normalization (`validators.normalize_url`) |
| $\sigma : \mathcal{U} \to \mathcal{U}$ | Secret-parameter stripping (`validators.sanitize_url`) |
| $\kappa : \mathcal{U} \to \mathcal{U}$ | Dedup key (`queue_manager._dedup_key`) |
| $\mathrm{id} : \mathcal{U} \to \{0,1\}^{64}$ | Content ID (`validators.compute_content_id`) |
| $\chi : \mathcal{U} \to \{\textsf{media}, \textsf{text}, \bot\}$ | Route classifier (`validators.classify_url_kind`) |
| $\Sigma$ | Store: a finite partial map $\{0,1\}^{64} \rightharpoonup R$ from content ID to record (`state.json`) |
| $R$ | Record type (`models.StateRecord`) |
| $S$ | Status set (`models.ItemStatus`) |
| $T \subset S$ | Terminal statuses |
| $\Lambda$ | Stage markers (`models.ItemStage`) with $\bot$ for "none" |
| $a(r)$ | `attempt_count` of record $r$ |
| $\rho(r)$ | `next_retry_at` of record $r$, or $\bot$ |
| $\mathbf{b} = (b_1, \dots, b_k)$ | Backoff schedule in minutes (`retry.backoff_schedule_minutes`) |
| $M$ | Attempt budget (`retry.max_attempts`) |
| $n$ | $\lvert \mathrm{dom}(\Sigma) \rvert$, records in the store |

## 2. Identity: normalization and content IDs

### 2.1 Normalization

$\nu$ maps the many spellings of one resource to a single canonical URL:
lowercase scheme and host, strip a leading `www.`, drop the fragment and any
trailing slash, drop tracking query parameters, and sort what remains. Two
host-specific rules refine it:

- **Instagram**: the query string is filtered by *allow-list*, not deny-list.
  Only `img_index` survives, because it is the one parameter that changes
  what gets captured. Share-token parameter names rotate (`igshid`, `igsh`,
  `igsi`, `stkn`, ...), so any deny-list eventually leaks a fresh ID for the
  same post.
- **Notion**: `source` is additionally stripped (`?source=copy_link`).

**Property 2.1 (idempotence).** $\nu \circ \nu = \nu$. Every rule removes or
reorders components and none introduces one that a later rule would change,
so a second application is the identity. Checked by
`tests/test_validators.py::test_normalize_url_is_idempotent`.

### 2.2 Content IDs

$$
\mathrm{id}(u) \;=\; \mathrm{trunc}_{64}\big(\mathrm{SHA\text{-}256}(\nu(u))\big)
$$

rendered as 16 hex characters. $\mathrm{id}$ is deterministic and depends on
$u$ only through $\nu(u)$, so equivalent spellings share one ID.

**Collision bound.** Modeling the truncated digest as uniform over
$2^{64}$ values, the birthday bound gives

$$
\Pr[\exists\, u \ne u' : \mathrm{id}(u) = \mathrm{id}(u')] \;\le\; \binom{n}{2} 2^{-64} \;\approx\; \frac{n^2}{2^{65}} .
$$

At $n = 10^4$ records this is about $2.7 \times 10^{-12}$; the bound reaches
$10^{-3}$ only near $n \approx 1.9 \times 10^8$. A collision would make the
second URL a silent dedup hit, so this is the only correctness cost of
truncation.

### 2.3 The dedup key

$\mathrm{id}$ deliberately keeps credential-bearing parameters (e.g. a DM
bot's `?mcp_token=`), because the token is often what makes the URL
fetchable. That means one page can carry two IDs. A second, coarser key closes
the gap:

$$
\kappa(u) \;=\; g\big(\sigma(\nu(u))\big)
$$

where $\sigma$ removes secret parameters and $g$ collapses every Google Docs
URL form (`/edit?usp=sharing`, `/mobilebasic`, `/u/0/...`) to
`docs.google.com/<kind>/d/<doc-id>`.

**Registration rule.** A submitted $u$ is a duplicate of $r \in \Sigma$ iff

$$
\mathrm{id}(u) \in \mathrm{dom}(\Sigma) \;\;\lor\;\; \exists\, r \in \mathrm{ran}(\Sigma) : \kappa(r.\mathit{normalized\_url}) = \kappa(u),
$$

in which case the existing record is returned and $\Sigma$ is unchanged
(`queue_manager._find_existing`).

## 3. Routing: the classification function

Let $h(u)$ be the host of $u$ without `www.`, $A$ the media allow-list
(`download.allowed_domains`) and $B_t$ the text block-list
(`text_capture.blocked_domains`). Write $h \sqsubseteq D$ when $h$ equals, or
is a subdomain of, some entry of $D$, matched against the *full* host so that
an entry like `drive.google.com` does not admit the rest of `google.com`.

$$
\chi(u) =
\begin{cases}
\bot & \text{scheme} \notin \{\texttt{http}, \texttt{https}\} \text{ or no host} \\
\pi(u) & h(u) = \texttt{drive.google.com} \text{ and } h(u) \sqsubseteq A \\
\textsf{media} & h(u) \sqsubseteq A \\
\bot & h(u) \sqsubseteq B_t \\
\bot & h(u) = \texttt{localhost} \text{ or } h(u) \text{ is a literal loopback, private, or link-local IP} \\
\textsf{text} & \text{otherwise}
\end{cases}
$$

Cases are evaluated top to bottom. $\pi$ is a metadata-only `yt-dlp` probe
(`download=False`): $\textsf{media}$ if it resolves a video, $\textsf{text}$
on any failure. Drive is the one host where URL shape cannot distinguish a
video from a document.

The media path is allow-list gated (it runs cookie-capable downloaders); the
text path is a catch-all (it is an unauthenticated GET). This asymmetry is
the core of the risk posture: the expensive, higher-risk capability needs
explicit opt-in per host, the cheap one needs explicit opt-out.

## 4. The item state machine

### 4.1 States

$$
S = \{\textsf{pending}, \textsf{downloading}, \textsf{transcribing}, \textsf{enriching}, \textsf{writing\_note}, \textsf{done}, \textsf{failed}, \textsf{failed\_permanent}, \textsf{blocked}\}
$$

$$
T = \{\textsf{done}, \textsf{blocked}, \textsf{failed\_permanent}\}
$$

The four *in-flight* statuses $\{\textsf{downloading}, \textsf{transcribing},
\textsf{enriching}, \textsf{writing\_note}\}$ describe what a worker is
currently doing. A record observed in one of them after a crash is simply an
interrupted attempt.

### 4.2 Transitions

Writing $\textsf{fail}$ for the failure handler of §5:

| From | Event | To |
| --- | --- | --- |
| (none) | register, $\chi(u) = \bot$ or domain blocked | $\textsf{blocked}$ |
| (none) | register, otherwise | $\textsf{pending}$ |
| $\textsf{pending}$, $\textsf{failed}$, any in-flight | worker picks up (media download or text fetch) | $\textsf{downloading}$ |
| $\textsf{downloading}$ | media fetched | $\textsf{transcribing}$ |
| $\textsf{downloading}$ (text) / $\textsf{transcribing}$ | transcript ready | $\textsf{enriching}$ |
| $\textsf{enriching}$ | enrichment ready | $\textsf{writing\_note}$ |
| $\textsf{writing\_note}$ | note written | $\textsf{done}$ |
| any non-terminal | stage raises | $\textsf{fail}(r, e) \in \{\textsf{failed}, \textsf{failed\_permanent}\}$ |
| $\textsf{failed\_permanent}$ | manual `cli retry` | $\textsf{pending}$ with $a := 0$, $\rho := \bot$ |

A memoized stage (§6) is skipped, so a resumed record may jump straight from
pickup to $\textsf{enriching}$ or $\textsf{writing\_note}$.

### 4.3 The actionable set

At time $t$ the worker processes exactly

$$
\mathrm{Act}(\Sigma, t) = \{\, r \in \mathrm{ran}(\Sigma) \;:\; r.\mathit{status} \notin T \;\land\; (\rho(r) = \bot \lor \rho(r) \le t) \,\}
$$

(`QueueManager.get_actionable_items`). Two consequences fall out directly:

- Terminal states are absorbing under automatic processing. The only exit is
  the manual `retry` edge out of $\textsf{failed\_permanent}$; $\textsf{done}$
  and $\textsf{blocked}$ have none.
- In-flight records are actionable, which is the whole crash-recovery
  mechanism at the state level: there is no separate "recover" path.

## 5. Failure semantics and retry policy

When a stage raises exception $e$ on record $r$ at time $t$, the handler in
`WorkerPipeline.process_item` partitions failures into three classes:

$$
\textsf{fail}(r, e) =
\begin{cases}
(\textsf{failed},\; a,\; t + b_1) & e \in \mathcal{E}_{\mathrm{acct}} \\
(\textsf{failed\_permanent},\; a + 1,\; \bot) & e \in \mathcal{E}_{\mathrm{det}} \\
(\textsf{failed\_permanent},\; a + 1,\; \bot) & e \in \mathcal{E}_{\mathrm{tr}},\; a + 1 \ge M \\
(\textsf{failed},\; a + 1,\; t + b_{\min(a+1,\,k)}) & e \in \mathcal{E}_{\mathrm{tr}},\; a + 1 < M
\end{cases}
$$

as (status, attempt count, next retry), where:

- $\mathcal{E}_{\mathrm{acct}}$: **account-level** provider failures (no
  credit, revoked key). They are true of every queued item and say nothing
  about this URL, so no attempt is charged and the whole pass halts after
  this item. Charging attempts here would walk healthy items to
  $\textsf{failed\_permanent}$ over someone else's billing problem.
- $\mathcal{E}_{\mathrm{det}}$: **deterministic** failures, identified by
  exception *type or status code only*, never by message text: terminal LLM
  request errors, an empty transcript, and `NotImplementedError` from the
  event loop. Waiting cannot change the answer.
- $\mathcal{E}_{\mathrm{tr}}$: everything else, assumed **transient**.

**Proposition 5.1 (bounded automatic retries).** Excluding
$\mathcal{E}_{\mathrm{acct}}$, a record is attempted at most $M$ times between
manual resets.

*Proof.* Every non-account failure increments $a$. A record is actionable only
outside $T$. The transient case moves to $T$ as soon as $a \ge M$, and
deterministic failures move to $T$ immediately. A successful attempt also
lands in $T$. So the $M$-th charged attempt is the last. $\square$

**Minimum time to give up.** With the shipped configuration
$\mathbf{b} = (1, 5, 30, 120, 480)$ and $M = 5$, four transient failures are
followed by retries no sooner than $1, 5, 30, 120$ minutes later, and the
fifth is final, so

$$
t_{\mathrm{give\,up}} - t_{\mathrm{first\,fail}} \;\ge\; \sum_{i=1}^{M-1} b_i = 156 \text{ min}.
$$

This is a lower bound only: a retry happens on the first `run-once` at or
after $\rho(r)$, so real latency also depends on how often the worker runs.
Note that $b_5 = 480$ is unreachable while $M = 5$; it only matters if $M$ is
raised.

**Liveness caveat.** Account-level failures are not counted, so Proposition
5.1 gives no bound on them. If a provider never recovers, affected records
stay $\textsf{failed}$ and are retried forever on the $b_1$ cadence. This is a
deliberate choice: an outage is an operator problem, and the halted pass keeps
it from consuming the queue.

## 6. Stage memoization and crash recovery

### 6.1 The pipeline as a composition

For one record the worker computes

$$
\mathrm{note} \;=\; W \circ E \circ X \circ D \,(u)
$$

with $D$ download, $X$ extract (transcribe, vision-describe, or text fetch),
$E$ enrich, and $W$ write. On the text path $D$ is absent and $X$ is the
fetch. Skill generation $K$ runs after $W$ and is outside the composition on
purpose (§6.3).

### 6.2 Resume point

Stage markers are totally ordered:
$\bot < \textsf{downloaded} < \textsf{transcribed} < \textsf{enriched} < \textsf{note\_written}$.
Each completed stage writes its output to `data/tmp/<id>/` (media files,
`transcript.json`, `enrichment.json`) and advances $\lambda(r)$.

The marker alone is never trusted. The resume stage is

$$
k^\ast(r) \;=\; \max\{\, k \le \lambda(r) \;:\; \text{the artifact of stage } k \text{ exists and parses} \,\}
$$

and execution restarts at the stage after $k^\ast$. A missing or unreadable
artifact means its stage simply runs again; it is never an error.

**Proposition 6.1 (no redundant work).** After a crash during stage $j$,
the next attempt re-runs only stages $> k^\ast$, and $k^\ast \ge j - 1$
whenever the artifacts of completed stages survived.

The proof is immediate from the definition. The property matters because
$D$ and $E$ are the expensive, rate-limited, sometimes paid calls.

Two refinements:

- An empty transcript is never cached. Otherwise a manual `retry` would load
  the cached emptiness and fail identically, which would make the retry edge
  of §4.2 useless for exactly the case it exists for.
- The post caption is appended at extraction time and is not part of the
  cached download artifact. A crash between $D$ and $X$ loses the caption.
  This is a known, accepted gap.

### 6.3 Commit point

$\textsf{done}$ is persisted *after* $W$ and *before* $K$. Skill generation
is the slowest remaining step and the most exposed to a hard kill. Committing
first means a crash inside $K$ cannot cause the next pass to re-run
$D, X, E, W$ for an item whose note already exists. A failure in $K$ sets
`skill_error` and never changes `status`.

## 7. Invariants

The following hold for every reachable store $\Sigma$:

**I1 (key integrity).** $\forall\, c \in \mathrm{dom}(\Sigma) :
\Sigma(c).\mathit{content\_id} = c$. Records are inserted only under their
own ID.

**I2 (no duplicate registration).** Registering a $u$ that matches an
existing record by $\mathrm{id}$ or $\kappa$ leaves $\Sigma$ unchanged (§2.3).
Re-submitting a URL is a no-op.

**I3 (retry window).** $r.\mathit{status} = \textsf{failed} \Rightarrow
\rho(r) \ne \bot$, and $r.\mathit{status} \in T \Rightarrow \rho(r) = \bot$.

**I4 (success resets).** $r.\mathit{status} = \textsf{done} \Rightarrow
a(r) = 0 \land \rho(r) = \bot \land r.\mathit{error} = \bot$.

**I5 (one note per record).** At most one note file in the vault carries
$r.\mathit{content\_id}$ in its frontmatter. Filenames are $\mathrm{slug}(\mathit{title})$;
when a re-run changes the title, the previous note is deleted
(`_cleanup_stale_note`). On a genuine slug collision with a *different* ID,
the writer picks `<slug>-2.md`, `<slug>-3.md`, ...; an existing file with the
*same* ID is reused in place.

**I6 (skill implies note).** $r.\mathit{skill\_path} \ne \bot \Rightarrow
r.\mathit{status} = \textsf{done}$.

I1 to I4 are maintained by every transition in §4 and §5 by inspection.
I5 depends on the vault not being edited out from under the pipeline;
`scripts/reconcile_ledger.py` detects and repairs drift between
`note_path` and the vault when it is.

## 8. Concurrency model

Three kinds of process touch the store: the CLI worker, the webhook server
(registration plus a background pass), and the vault organizer. Two
cross-process file locks govern them:

| Lock | File | Held for | Timeout |
| --- | --- | --- | --- |
| $L_{\mathrm{run}}$ | `state.run_once.lock` | one entire `run_once()` pass | 600 s |
| $L_{\Sigma}$ | `state.lock` | one read-modify-write of $\Sigma$ | 5 s |

**Serializability.** Every mutation of $\Sigma$ goes through
`_locked_mutate`: acquire $L_\Sigma$, load, apply $f$, save, release. All
mutations are therefore totally ordered, and the history is equivalent to a
serial one.

**Atomic visibility.** `save_state` writes a temp file and `os.replace`s it
over `state.json`. Replacement is atomic on the platforms targeted, so a
reader observes the old store or the new one, never a torn write.

**Deadlock freedom.** The only nesting is $L_{\mathrm{run}} \to L_\Sigma$
(a pass calls `update_record` on every stage transition). Registration
(`add_url`) takes $L_\Sigma$ alone and never $L_{\mathrm{run}}$. With one
global acquisition order there is no cycle in the wait-for graph, so no
deadlock. The two locks must be distinct files: a pass already holding
$L_{\mathrm{run}}$ would otherwise block on itself at its first
`update_record`. Timeouts turn a stuck holder into a raised error instead of
an indefinite hang.

**Mutual exclusion of passes.** $L_{\mathrm{run}}$ ensures at most one pass
at a time, so no record is processed by two workers concurrently.
Registration stays live during a long pass because it never waits on
$L_{\mathrm{run}}$.

**Read cache.** `load_state` reuses its last parse when the file's
$(\mathit{mtime\_ns}, \mathit{size})$ stamp equals the one recorded at the
last write. The check runs inside $L_\Sigma$, so a foreign write is always
detected, *assuming* the filesystem's mtime resolution separates two writes
of identical size. NTFS (100 ns) and ext4 (1 ns) both satisfy this in
practice; a coarse-timestamp filesystem would not.

## 9. Complexity

Let $n = \lvert \mathrm{dom}(\Sigma) \rvert$ and let one mutation change
$c$ records.

| Operation | Cost |
| --- | --- |
| `load_state`, stamp unchanged | $O(1)$ |
| `load_state`, stamp changed | $O(n)$ parse and validate |
| `save_state` | $O(n)$ bytes written, $O(c)$ records re-serialized |
| Registration (`_find_existing`) | $O(n)$ linear $\kappa$ scan |
| `get_actionable_items` | $O(n)$ |

Each processed item performs a constant number of mutations (about six), so
a pass over $m$ actionable items writes $\Theta(m \cdot n)$ bytes, and
ingesting $q$ new URLs costs $O(q \cdot n)$ for dedup. Both are quadratic in
the worst case $m, q = \Theta(n)$.

This is an intentional tradeoff at the target scale ($n \approx 10^3$, a
personal knowledge base). It keeps the store a single human-readable JSON
document with atomic replacement and no database dependency. The
`maintenance.state_size_warning_threshold` setting logs a warning as $n$
approaches the range where this stops being reasonable. The upgrade path is
an index on $\kappa$ (for dedup) and an append-only log or SQLite (for
writes).

## 10. Safety properties

Stated as predicates over every outbound request the pipeline makes:

**S1 (media gating).** A downloader (`yt-dlp`, `gallery-dl`) is invoked on
$u$ only if $\chi(u) = \textsf{media}$, hence only if $h(u) \sqsubseteq A$.

**S2 (no local targets).** No fetch is made to `localhost` or to a literal
loopback, private, or link-local IP address, since $\chi(u) = \bot$ for those.

**S3 (credential isolation).** Cookies are passed only to the media
downloaders. The text path, including the headless render fallback, sends
no cookies and reuses no session.

**S4 (secret hygiene).** No secret lives in `config/settings.yaml`; secrets
come only from the environment. Notes and LLM prompts receive
$\sigma(\nu(u))$, never $u$, so share tokens do not reach the vault.

**S5 (read-only rendering).** The JS-render fallback navigates, waits, and
reads the DOM. It performs no clicks, form input, scrolling, or login, and
uses no anti-detection tooling; a bot challenge is reported as an error, not
bypassed.

The single scoped exception to S5 is `comment_gate.py`, an owner-run,
rate-limited, dry-run-by-default module documented in
[`superpowers/specs/2026-09-26-comment-gated-links-design.md`](superpowers/specs/2026-09-26-comment-gated-links-design.md).
It is never invoked by the worker or the webhook.

## 11. Output contract

For a $\textsf{done}$ record with enrichment $\varepsilon$ the note is

$$
N = \big\langle \mathit{title}, \mathit{source\_url}, \mathit{content\_id}, \mathit{created\_at}, \mathit{tags}, \mathit{tools\_mentioned} \big\rangle_{\text{frontmatter}} \;\Vert\; \big\langle \mathit{summary}, \mathit{key\_takeaways}, \mathit{transcript} \big\rangle_{\text{body}}
$$

and a skill artifact exists iff

$$
\textsf{skill}(r) \iff \texttt{skill\_writer.enabled} \;\land\; \varepsilon.\mathit{high\_signal} \;\land\; \varepsilon.\mathit{skill\_candidate\_reason} \ne \varnothing
$$

(`skill_writer.should_generate_skill`). Requiring a stated reason, not just
the boolean, means a model cannot emit a skill without justifying it.

Every extractor ($X$ on the video, image, and text paths) produces the same
`TranscriptResult` type, so $E$, $W$, and $K$ are independent of how the text
was obtained.

## 12. Known gaps and threats to validity

- **DNS rebinding.** S2 checks literal IPs only. A hostname that resolves to
  a private address passes $\chi$. Acceptable for a single-user tool that only
  ingests URLs its owner submits; not acceptable if submissions ever come
  from untrusted parties.
- **Undocumented Notion API.** The Notion fetcher calls Notion's internal
  `loadPageChunk` endpoint. It can change without notice, and only the first
  100-block chunk is read.
- **LLM nondeterminism.** $E$ is not a function in the mathematical sense:
  re-enriching the same transcript can yield a different title, and hence a
  different filename. I5 is preserved by deleting the stale note, but the
  note's *path* is not stable across manual re-runs.
- **Truncated hash.** §2.2 assumes SHA-256 truncation behaves like a random
  function, the standard assumption, not a proven one.
- **Quadratic store cost.** §9. Fine at $10^3$ records, a real limit at
  $10^5$.
- **Test coverage of the model.** The suite (`uv run pytest`) exercises the
  transitions, retry classes, resume logic, and locking with fakes; it does
  not model-check the state machine exhaustively.
