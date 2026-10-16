# JSON format, schema 1

[English](json-format.md) · [Deutsch](json-format.de.md)

`schema_version` describes the JSON contract. `version` identifies the program
that measured the repositories. Adding optional fields does not change schema 1;
a future incompatible schema is rejected. Older object snapshots containing
`version` and `repos` are accepted with explicit capability limits. Bare arrays
from early versions are unsupported.

## Snapshots

Create a complete snapshot with `gmf --json > snapshot.json`. Add `--fetch` when
you want a fresh remote measurement; it updates safe remote-tracking refs.

| Field | Meaning |
|---|---|
| `schema_version` | Integer, currently `1` |
| `version` | Program version |
| `generated_at` | Snapshot creation time, ISO-8601 with timezone |
| `root` | Scan root; repositories match by relative `rel`, independently of this absolute path |
| `repos` | Repository states, including `head_oid`, operation, branches and remotes |
| `filter` | Optional path-search scope: `query`, `matches`, `total` |
| `view` | Optional work-view scope: `id`, `matches`, `total`, `hidden_attention` |

A repository's `measured_at` is its local measurement time. Each remote's
`fetched_at` is the last proven successful fetch for that branch and remote
configuration, known to this process. An empty string means unknown. A null
`head_oid` means a proven branch without its first commit; an empty or absent
identity is unknown. Branch coverage uses locally known remote-tracking refs,
as indicated by `branch_coverage_basis`.

Text fields escape terminal controls and bytes that cannot be represented as
UTF-8. Configured remote URLs have no exported URL fields; endpoint identity is hashed.
Git error evidence retains redacted text.
Do not turn displayed text back into Git arguments.

## Saved comparisons

```sh
gmf --json > before.json
# ... later ...
gmf --json > after.json
gmf --compare before.json after.json
gmf --compare before.json after.json --json --view changes
```

`--compare` reads only the two UTF-8 JSON files. It performs no scan, SSH, fetch,
or config creation. Each file is limited to 64 MiB. Duplicate JSON keys,
non-finite numbers, invalid counts, incompatible schemas and nesting beyond 128 containers
are rejected with exit 2. UTF-8 with a byte-order mark is accepted.

Saved path filters must have the same effect: term order, case and repeated
terms do not matter. Saved work views are rejected because an excluded repo
could hide a counterpart. Save complete snapshots and apply `--view` to the
comparison instead. A repo is then compared on both sides when either side
matches the view. A different checked-out branch does not create remote-counter
drift. Duplicate normalized repo names produce an ambiguity finding and are
not arbitrarily paired.

Reports name snapshot creation times, the range of local measurement times and
unknown capabilities. Time differences alone are not drift. The source metadata
retains saved filter boundaries; an extra comparison filter only narrows that
recorded inventory. Unknown fetch times do not mean a failed fetch.

## Comparison output

SSH `--diff` and saved `--compare` share the same finding generation.

| Field | Meaning |
|---|---|
| `schema_version` | Comparison contract, currently `1` |
| `here`, `host` | Left program version and right comparison label; retained for compatibility |
| `differences` | Localized readable lines, retained for compatibility |
| `findings` | Stable structured findings |
| `sources` | Metadata for `here` and `there`: versions, timestamps, roots, recorded counts and saved scope |
| `limitations` | Non-drift limits: stable `side`, `code`, `count` |
| `filter` | Optional applied comparison filter, counts, and work-view selection information |

Every finding has `type`, `category`, `repo`, `remote`, `branch`, `field`, `side`,
`values` and `message`. Unused context fields are null. `side` is `here`, `there`
or null, independent of language or labels. `message` is the readable line;
scripts should use the other fields. `category` is `drift`, `sync`, `local` or
`warning`. Neither endpoint URLs nor fingerprint values are exposed in findings.

| `type` | Category | Important `values` fields |
|---|---|---|
| `version` | warning | `here_version`, `there_version` |
| `ambiguous_repo` | drift | — |
| `only_on` | drift | —; `side` identifies the observed side |
| `remote_missing` | drift | —; `remote` and `side` |
| `remote_security` | drift | `changed_fields`, configuration field names only |
| `remote_branch` | drift | —; `branch`, `remote`, `side` |
| `remote_state` | drift | `here_ahead`, `here_behind`, `there_ahead`, `there_behind` |
| `sync_even` | sync | `ahead`, `behind`, shared by both sides |
| `branch` | local | `here_branch`, `there_branch` |
| `head` | drift | `here_oid`, `there_oid`; null is an unborn branch |
| `local_branch_missing` | drift | —; `branch`, `side` |
| `branch_coverage` | local | `here_coverage`, `there_coverage`, each with `state` and `uncovered_commits` |
| `branch_pending` | sync | `count`, shared uncovered commits |
| `repo_field` | drift | `here_value`, `there_value`; `field` identifies the property |
| `branch_inventory_failed` | local | `error` |
| `branch_coverage_unknown` | local | `count`, branches with unknown coverage |
| `operation` | local | `operation`, stable Git operation ID |
| `dirty` | local | `count`, changed/new files |
| `fetch_failed_side` | local | `count`, repositories with failed fetches |

Limitation codes are `legacy_schema`, `head_identity_unknown`,
`branch_inventory_unknown`, `local_measurement_unknown`, `fetch_age_unknown`.
Limits alone do not change the exit status. Comparison exits are 0 for no
findings, 1 for at least one finding, and 2 for an unreadable or incompatible
comparison. Local notices and version warnings retain the existing exit-1
behavior. Empty inventories explicitly say nothing was compared.

With a work-view filter, `here_matches` and `host_matches` count retained
counterparts; `here_view_matches` and `host_view_matches` count actual view
matches. `selection: "either-side"` records the union rule.
