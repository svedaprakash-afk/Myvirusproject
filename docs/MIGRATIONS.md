# Schema migrations

Alembic, with a reviewed baseline (`backend/migrations/versions/*_0001_baseline.py`). Verified on SQLite and PostgreSQL 16 (CI runs the
whole test-suite on both). **Never rehearsed against your managed PostgreSQL service or your data** — do that on a restored copy first.

## Everyday commands (run from `backend/`)
| Command | Purpose |
|---|---|
| `python -m app.cli migrate` | Upgrade to the latest revision. Safe to run from several pods at once (PostgreSQL advisory lock). |
| `python -m app.cli status` | Prints current vs latest revision and **drift** (live schema vs models). Exit 1 if behind or drifting. |
| `python -m app.cli create-migration "add foo to bar"` | Developer: autogenerate a revision after changing `app/models.py`. **Read it** before committing. |
| `python -m app.cli downgrade <rev> --yes` | Roll back. Drops columns/tables: data in them is lost. Take a backup first. |

## Rules that keep this safe
1. **Every model change ships with a migration.** `tests/test_migrations.py::test_fresh_database_upgrades_to_exactly_the_models` and the CI job `migrations-in-sync` fail if the models and the migrated schema differ.
2. **Production never auto-migrates.** With `APP_ENV=prod` the API checks the revision on start and **refuses to start** if the schema is not at the release's head. Run `migrate` as a Job or init container *before* rolling the new version. (Dev/test default `AUTO_MIGRATE=1` upgrades on start.)
3. **Expand → migrate → contract.** For a rolling deploy, a migration must work with both the old and new code: add nullable columns / new tables first, deploy, backfill, and only drop or tighten in a later release.
4. **Backfills are data migrations.** Write them in the revision using `op.execute` / lightweight `sa.table()` constructs — never import `app.models` inside a revision (models change; old revisions must not).
5. **Naming conventions** (`app/db.py`) make constraint names deterministic, so later revisions can drop/alter them on both engines. SQLite uses batch mode (table rebuild) automatically; large tables on SQLite are for development only.
6. **PostgreSQL enums**: shared enum types are created once up front in the baseline. A later revision that adds an enum value needs `ALTER TYPE … ADD VALUE` (cannot run inside a transaction block on older PostgreSQL); handle it explicitly rather than trusting autogenerate.

## Databases created before Alembic existed (`create_all`)
`migrate` adopts them: if the app tables exist without an `alembic_version` table **and every model column is present**, the database is stamped at the baseline and upgraded normally. If any table/column is missing it **refuses and changes nothing**, listing what is missing (constraint and index *names* are ignored — they predate the naming convention). Restore from a backup or fix by hand, then retry.

## Failure handling
- A failed migration runs in its own transaction on PostgreSQL and rolls back; on SQLite DDL is also transactional for these operations, but always keep a backup.
- If `migrate` fails in the init container the pod does not become ready, so the old ReplicaSet keeps serving.
- To check which revision a database is at without the app: `select * from alembic_version;`.
