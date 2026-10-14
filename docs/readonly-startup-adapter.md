# Unauthenticated loopback startup fixture

This experimental adapter and runner are isolated from the production CLI and GUI.
They test a fresh unauthenticated browser against synthetic localhost fixtures.
They cannot accept an X URL, authentication export or existing browser profile.

## Current evidence

The user's v2 run actually started the sandboxed browser and localhost server.
Baseline, POST denial, unknown GET, redirect, wrong target and frame cases passed.
Popup failed with `get_control_failed`; `/unexpected` never reached the server.
Browser/server cleanup succeeded and no timeout occurred. Worker and WebSocket
cases were not reached. This is six-case partial evidence, not a full native pass.

The user's v3 run timed out at the unchanged 30-second worker limit. There was no
`worker-result.json`, durable progress or stack trace, and both worker logs were
empty. The exact stalled case or API call cannot be reconstructed. Its result
confirms 18 tracked generations exited, but normal browser/server closure was not
reported. Keep `native_verified=false` and `cleanup_complete=false`.
Preserve both result directories and the retained v3 scratch files.

The latest repair removes synchronous `page.close()` from the unexpected-page
lifecycle callback. That callback latches global refusal and returns; the owned
context closes all pages after callbacks unwind. This removes a reentrancy risk
found in source. It is not a demonstrated cause of the v3 timeout.

The runner now atomically saves `worker-progress.json` before driver/browser/case
start, context creation, route setup, page creation, navigation, observation and
cleanup. Completed case results survive a later stall. `worker-stack.txt` records
Python thread code locations every five seconds without locals or credential
values. Write failures cannot bypass owned cleanup. These changes are offline
validated; their native behavior remains unverified. A further native run awaits
independent review of this repair.

## Policy

Before creating a page or navigating, install exact GET admission, an independent
non-GET blocker, WebSocket denial, transport guards and page/worker listeners.
Only canonical `http://127.0.0.1` URLs at the fixture's ephemeral port are eligible:
`/target/123`, `/bootstrap.js`, and `/read?target=123`, plus the explicit synthetic
redirect negative case. Reject aliases, other origins, userinfo, fragments,
unknown or duplicate requests, wrong targets, redirects and unowned frames/pages.

Admission checks method and exact URL membership before reading `Request.frame`:
an unlisted popup navigation can arrive before its frame exists. Listed requests
still require the positively owned main frame, exact target URL and absent GET
body. Missing frame evidence refuses dispatch. No ownership exemption is added.

Fetch only admitted requests using exact GET, `max_redirects=0`, `max_retries=0`
and the remaining case timeout. Relay the actual matching 2xx response and dispose
it. Every non-2xx signal stops; no retry or mutation arm exists. The independent
server rejects POST. Tests cover response/DOM relay, POST, unknown GET, redirect,
wrong target, frame, popup, worker and WebSocket cases. Full success requires all
nine cases; failure or missing evidence cannot be accepted as success.

## Resource ownership and diagnostics

Each case gets a new owned context with service workers blocked. Routes remain
installed until context closure. A failed creation call may have allocated a
remote resource; a missing handle is not evidence that cleanup completed.
Never enumerate or close borrowed contexts to guess ownership.

The runner owns its disposable browser and server. It closes both normally and
records closure proof. The supervisor captures immutable birth identities and
kernel pidfds for its own child and verified descendants before native startup.
It never refreshes ownership from a reused numeric PID and never falls back to
numeric-PID signals. Missing stable handles stops before worker startup.
All worker exits trigger recorded-descendant cleanup before result JSON is read.
Malformed or missing JSON retains an incomplete result and scratch evidence.
Tracked process exit does not establish normal browser/server cleanup.

Reports contain controlled stages/enums, public identity projections, counts and
synthetic fixture paths. They do not collect auth headers, cookies, credentials,
real post content or arbitrary exception text. Only the runner's private XDG,
TMPDIR and fresh `/tmp` output are writable. No user browser is attached.

## Inspection and future native validation

Safe inspection starts no driver, browser or HTTP server:

```sh
scripts/run-loopback-fixture.sh --inspect
```

The host-specific wrapper uses the already installed staging Python/Playwright
and read-only Chromium cache. It installs or downloads nothing. After independent
review, a normal-user environment that supports standard Chromium sandboxing can
run it with a new output directory under `/tmp`:

```sh
scripts/run-loopback-fixture.sh --output /tmp/kusamushiri-loopback-native-check-v4
```

This command has not been executed for the latest repair. Do not reuse or delete
v2/v3 evidence. Keep `chromium_sandbox=True`; do not use sudo, disable the sandbox,
change host security, raise the timeout or widen permitted traffic to force a pass.
The existing per-case limit is 0.75 seconds and supervised worker limit is 30
seconds. No universal bound on every OS primitive is claimed.

A verified run requires exit 0, `native_verified=true`, `cleanup_complete=true`,
`timed_out=false`, and all nine distinct case pass flags. Inspect `result.json`,
`worker-result.json`, `worker-progress.json`, `worker-stack.txt`, `worker.stdout`
and `worker.stderr`. If it stalls, use durable stage and stack evidence before
claiming a cause. This fixture provides no authenticated X or deletion evidence.
