# Public endpoint identity in diagnostics

This offline catalog admits only literal public GraphQL symbols and fixed REST paths.
It does not establish quota buckets, permission to retry, authentication state or the
cause of a batch stop. Unknown 403/429 responses retain the existing gates.

`public_operation` is an exact ASCII operation symbol (1–80 characters) from the frozen
catalog. `route_template` contains a fixed public path or the constant GraphQL template
`/i/api/graphql/{query_id}/{operation}`; the query ID is never logged. Only canonical
HTTPS authorities and bounded ASCII URLs qualify. No percent decoding, Unicode
normalization, prefix matching or truncation is performed. Query strings, fragments,
IDs, usernames, headers, cookies and bodies are never included. An unconfirmed name
or path emits `identity_status: unavailable`; an existing safe `operation_sha256` is
retained when available. A missing digest is possible and is not an error.

The export sanitizer independently verifies the closed catalog and template. Older
traces remain readable and report identity unavailable; identities are not reconstructed
from unrecognized digests. The rate-limit and HTTP-error summaries include method,
status, role, public identity and the safe digest when present. The catalog is only an
observation label: a public symbol may still have role `unknown` and stop the batch.

`traffic_decision` events (only with `traffic_policy` `report` or `enforce`) carry the
same sanitized identity fields plus closed labels: `host_class` (`x_web`, `x_api`,
`x_subdomain`, `twimg`, `other`), `traffic_decision` (`allow`, `block`), `traffic_rule`
and `traffic_enforced`. Hosts outside X are reduced to their class; no host name, URL,
query or body is recorded. The summary groups them under `traffic`.

## Reproducible public catalog sources

The names and fixed v1.1 dispatch paths were extracted from these pinned public
static-analysis datasets. They are unofficial and incomplete, and can miss current,
new or removed operations. These files are not fetched at runtime.

- [GraphQL, develop](https://raw.githubusercontent.com/fa0311/TwitterInternalAPIDocument/ae38e959c621d60bf6ad493155799a604467b22c/docs/json/GraphQL.json)
  SHA256 `2c9737288dde49d992d4a89e1e800537226a739947500717c8f586ead9fb9881`
- [v1.1 dispatches, develop](https://raw.githubusercontent.com/fa0311/TwitterInternalAPIDocument/ae38e959c621d60bf6ad493155799a604467b22c/docs/json/v1.1.json)
  SHA256 `f366130e174d52c75f5ccb424ea7e7acc50e358330d16e249c3b3f700b3e7ed9`
- [GraphQL, twitter-login](https://raw.githubusercontent.com/fa0311/TwitterInternalAPIDocument/c26be7db5ab8beb5658e10d67cb59e64ccc0412e/docs/json/GraphQL.json)
  SHA256 `2ca9ab762d7ddf232352efa15d87917fe71cb3f6e8934bdd8ff103af5761eebb`

The prior closed `API_LABELS` catalog and exact GET fleetline path are also retained.
Fixed v1.1 paths admit their API `/1.1/` form and the web `/i/api/1.1/` form, only on
the corresponding literal authorities and recorded dispatch methods. Paths containing
user IDs, usernames or nonliteral dispatch segments are not admitted. The two unknown
GraphQL digests in the earlier 49-success trace did not match these public datasets;
no retrospective endpoint identification or quota conclusion is claimed.

Catalog attribution: Copyright (c) 2022 yuki, https://yuki0311.com/.
[MIT license](public-endpoint-catalog-LICENSE.txt).
