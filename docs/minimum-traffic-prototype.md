# Minimum necessary traffic: offline design and prototype

## 2026-10-17 decision: name-based admission instead of a per-build capsule

The production allowlist (`traffic_policy.py`, `--traffic-policy report|enforce`) admits
requests by public GraphQL operation name, resource type and host class, not by an exact
per-build capsule of query hashes and static chunks. X ships frontend builds often; a
fail-closed per-build capsule would stop every batch after each build until it was
re-derived, which defeats the goal of stable long batches. The trade-off is explicit:
admission is broader than the capsule below (any `x.com` / `*.twimg.com` script, style
or font), and a renamed or newly required dependency still stops the batch. Unsent
failures under `enforce` are held as blocked; nothing is retried or fabricated.

The candidate set came from the retained 49-success traces: 52 target loads issued about
40 requests each. Two per-load ancillary GETs (`CreatorStudioTabBarItemQuery`,
`api.x.com/1.1/account/settings.json`) were the HTTP 429 after 49 loads (the previously
unidentified g19). Admitted: the target document, static assets, `TweetDetail`,
`TweetResultByRestId` and one armed post mutation. `f01c2341` / `25d2ab19` start only
after `DeleteTweet` is dispatched, and the home traffic after each success comes from the
client returning home, not from kusamushiri. This module stays offline-only and is
unchanged; the live gates below still apply to the new allowlist trials (report first,
then bounded `enforce` runs, each separately authorized).

The direction is to build deletion from the smallest established dependency set.
An accumulating list of incidental endpoint exceptions does not establish that set.
The development script `scripts/minimum_traffic.py` is deliberately **offline only**,
opt-in, unreferenced by every production command and excluded from the application
package. It accepts synthetic records, refuses Playwright routes/real receipts, and has no transport, credential or header interface. It is
not a ready-to-use X allowlist or a new authentication/API deletion implementation.
The earlier suppression experiment is preserved separately as superseded evidence.

## Evidence and dependency map

The sanitized retained 49-success trace has 53 operations/navigations (49 successes,
4 unsent deferrals), 53 GET `TweetDetail`, 53 GET `TweetResultByRestId`, 49 POST
`DeleteTweet` and 49 HTTP200 DeleteTweet responses. It also has 216 script requests
and substantial unidentified traffic. There was no validated target-view reuse.
See `minimum-traffic-dependency-evidence.json` in the review workspace; its source
artifact SHA256 is `3564889fb4e6f659c43372a52fa76b3a4511043264c27e72d57f43edfdf92b10`.
Observed coexistence does not prove that both target reads are necessary. The trace
predates retention of public identity names for every diagnostic group, so it also
cannot prove which static chunks are necessary or identify historic g19.

| Purpose | Established evidence | Minimum candidate | Remaining proof |
| --- | --- | --- | --- |
| Authenticated state | Current login check reads visible account UI; no dedicated auth lookup in the action code | Verify existing authenticated UI in the same target view | Target page can provide a trustworthy account state without visiting home |
| Target identity/read | Owned timestamp-link article locator matches the input post ID, excludes nested/ambiguous peer articles; target document and both read names observed | One exact planned document, current two read candidates, local article proof | Which read(s) and variables actually carry that target and render required controls |
| Ownership/deletion permission | Scoped caret, positively identified delete menu, confirmation button; server is final authority | Local controls in that same validated article/view | Served UI survives missing unrelated subsystems; actual account ownership semantics |
| Mutation | One actual owned DeleteTweet POST required by batch receipts | One armed, target-bound DeleteTweet, after local confirmation proof | Real request body binding, persisted query identity, redirects and once-only dispatch enforcement |
| Truthful result | Actual owned mutation request/response plus local article disconnection; existing HTTP/error-envelope classification | Observe that response and local DOM, no added polling call | Same truthful response evaluator and owned-request association in a future adapter |
| Bootstrap/static | Scripts observed; cached public JS proves some endpoint names | Exact per-build reviewed static closure, not `*.twimg.com` or all `/client-web/` | Document inline bootstrap, runtime/chunk graph, styles/fonts, worker behavior and build freshness |
| Unknown traffic | Unknown g19 was the first 429 and remains unidentified | Abort and clearly stop before any unintended mutation | Dependency identification; do not silently call it optional or broaden permissions |

One target view can supply authenticated state, article identity, menu and
confirmation; each concept does not require its own request. Current Python action
verification does not issue an extra result query. HomeTimeline/home navigation,
profile/identity polls, repeated target navigation, polling after an acknowledged
mutation, telemetry, media and all other requests are absent from the prototype's
capsule. This is a design hypothesis about the rendered frontend, not proof those
requests can be removed from its current build. The static requests remain real
bootstrap dependencies until demonstrated otherwise.

## Smallest viable architecture

1. Start with an empty, controlled deletion page and routing installed **before**
   its first deliberate request, with service workers blocked. A persistent browser
   can restore tabs during `launch_persistent_context` before routes are installed;
   current startup cannot promise zero bootstrap/restored-tab traffic. This must be
   resolved explicitly, not hidden behind a Page.route matcher.
2. Require a versioned dependency capsule tied to a verified client build. The
   real capsule is presently unresolved and must stop before document dispatch.
   Do not accept a caller's bare `bootstrap_complete=True` as live evidence. The
   prototype accepts only the explicit `offline-fixture` scope and a synthetic
   fixture asset graph, which is not a production capsule.
3. Navigate to a planned target once. Allow only exact resource/method identities
   in that capsule, target-bound read candidates, and the one later armed DeleteTweet.
   Reject subframes, other pages, workers, redirects, unknown chunks, unknown APIs,
   noncanonical URLs, non-GET reads and unexpected mutations. Unknown dependencies
   abort and stop clearly; no synthetic success or automatic permission expansion.
4. Reuse that validated rendered view for account/article/menu/confirmation evidence.
   The prototype permits choosing one target-read candidate in a synthetic ablation
   fixture, but this does not certify dropping either real read. Future elimination
   must remove a demonstrated duplicate with equivalent target/UI evidence.
5. Arm one mutation only after all local evidence predicates pass. A future real
   adapter must validate request payload target identity and the public persisted
   operation's relationship; the prototype's `target_id` field is explicitly fixture
   evidence, not a production variables parser or sufficient live protection.
6. Finish from the exact owned mutation request/response and required DOM evidence.
   Missing/malformed/5xx/wrong-request responses remain uncertain, with no replay.
   An independently acknowledged success survives a later cancellation/control fault,
   while that fault still stops subsequent work. Explicit refusals/rate limits stay
   blocked and require existing pacing/storage policy; prototype never retries.

The prototype has finite once-only budgets for document, each static resource and
each candidate target read, plus one mutation. Thus redirects/refetch/polling cannot
reset budgets. The actual browser may need more fetches; exhaustion is evidence of
an unresolved dependency, not permission to retry or guess. Default product behavior,
HTTP403/429 gates, receipt persistence, recovery, quarantine and all dirty live work
remain byte-for-byte unchanged. Existing cancellation/cooldown/journal guards must
wrap any future browser adapter; none are replaced by this state machine.

## Offline validation and limits

Synthetic tests cover opt-in, unresolved bootstrap, exact static capsule, method/
identity/frame/target confusion, unknown requests, duplicate navigation/reads,
unarmed/duplicate mutation, missing UI predicates, abort failure, permitted-traffic
401/403/420/429, owned receipt/no-replay, truthful late success and real Python
pre-submission cancellation stages. Existing regression cases run unchanged.
A capsule with one or two synthetic reads can render its synthetic fixture; this is
not a React/DOM live dependency test. The full X application, bundle graph,
ServiceWorker/WebSocket/WebTransport/redirect control and real request body adapter
remain unsupported. No authenticated or unauthenticated browser was launched.

## Exact live validation gate (not authorized or performed)

The unanswered passive observation request is still pending; this implementation
request does not authorize it. No network capture, browser login, real deletion,
secret handling, application, deployment, Git publication or live commit occurred.

Before claiming a production allowlist works, obtain separate permission for one
bounded **read-only** target-page dependency observation. Scope it to a named owned
post, a named existing authenticated state, a finite time/request budget and redacted
method/resource/public identity/count/outcome fields. Do not collect payloads,
cookies, authorization headers, tokens or post bodies. Keep all mutation requests
blocked independently. No login or profile reset is implied by this gate. Routing
must cover startup/restored tabs, all pages/frames and workers before dispatch;
unsupported transports must be disabled or cause an explicit stop.

Use that observation only to establish the exact build-bound document/static/read
closure and target/authenticated UI predicates. Then separately authorize a
read-only closed-capsule ablation that removes redundant home/target/polling traffic
one change at a time, without mutations. Any unknown request or required-view failure
stops, with no automatic broader allowlist or fallback to unrestricted traffic.
Review the real payload/owned-request adapter and existing journal/no-replay gates
independently before integration. **A real deletion remains a separate authorization
and verification step** after these gates; it is not authorized by observing a page.
No claim is made that this will resolve g19 or the 49-success stopping point.


## Independent-review corrections (offline v2)

The capsule now requires exact `(method, canonical full API URL, resource)` entries
for both reads and the mutation. Full URL membership includes the persisted query
hash; a recognized operation name alone cannot grant dispatch. API membership is
empty by default and incomplete/invalid capsules stop before loading. Queries and
fragments, including empty `?`, `#` and `;` suffixes normalized away by URL parsing,
are rejected. Static and document URLs use the same canonical round-trip check.
Real GraphQL query variables/body binding remain unimplemented: no production
request is enabled by declaring a synthetic capsule.

A semantic `explicit-refusal` fixture label is definite only for an established
2xx response/error interpretation. Submitted 5xx, malformed status and otherwise
unestablished status/semantics remain ambiguous with no replay. Auth/rate statuses
401/403/420/429 retain the blocked/rate-refused outcome and stored Retry-After.
The future real evaluator must provide actual validated error-envelope evidence;
the label remains synthetic here.

Once an owned accepted acknowledgement and required DOM evidence prove success,
completion stays latched. Repeated missing, cloned, contradictory or server-error
completion observations return success while setting/retaining a stop for future
work. Later 401/403/420/429 observations retain the stop/status/deadline and cannot
erase the acknowledged success. Duplicate correct completion is idempotent, and
no repeat completion or target/resource variant resets the once-only budgets.
