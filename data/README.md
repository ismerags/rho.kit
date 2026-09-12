# Data

Two files live here, written by `python -m legotracker publish` on the machine
that does the collecting:

| File | What |
|---|---|
| `catalog.json` | The ~900 sets available in India — number, name, theme, piece count, image |
| `observations.csv` | Every price ever recorded. Append-only. One row per retailer per check |
| `meta.json` | Counts and date range, shown in the site footer |

They are committed on purpose. GitHub Actions reads them to build the website,
and plain text is both far kinder to git than a binary database and readable by
anyone who wants the data.

`lego.sqlite3` is **not** committed — it is the local working copy that the
retailer adapters write to. These files are exported from it.

If this folder is empty, the site build will stop and tell you to run
`publish` first.
