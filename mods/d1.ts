/**
 * mods/d1.ts — Thin Cloudflare D1 client for the Discord bot.
 *
 * Design principles
 * -----------------
 * - **No-logic proxy**: this module only exposes four low-level methods
 *   (`query`, `execute`, `batch`, `applySchema`).  It does not inspect,
 *   create, or migrate any tables on its own.
 * - **Not automatic**: no side-effects occur at construction time.
 *   Call `init()` explicitly before using any other method.
 * - **Manual schema**: pass raw DDL (including `REFERENCES` / foreign-key
 *   clauses) to `applySchema` when you are ready.
 * - **Error forwarding**: D1 errors — especially foreign-key constraint
 *   violations — are re-thrown verbatim so the caller can handle or
 *   surface the original message.
 *
 * Discord ID type convention
 * --------------------------
 * All Discord snowflake IDs MUST be stored as TEXT in D1/SQLite because
 * JavaScript cannot represent 64-bit integers without loss of precision:
 *
 *   - Guild ID   → TEXT   (e.g. `guild_id TEXT NOT NULL`)
 *   - Channel ID → TEXT   (e.g. `channel_id TEXT NOT NULL`)
 *   - Role ID    → TEXT   (e.g. `role_id TEXT NOT NULL`)
 *   - User ID    → TEXT   (e.g. `user_id TEXT NOT NULL`)
 *
 * Example schema with foreign keys
 * ---------------------------------
 * ```sql
 * CREATE TABLE IF NOT EXISTS guilds (
 *   guild_id   TEXT PRIMARY KEY,   -- Discord Guild ID  → TEXT
 *   name       TEXT NOT NULL
 * );
 *
 * CREATE TABLE IF NOT EXISTS members (
 *   user_id    TEXT NOT NULL,       -- Discord User ID   → TEXT
 *   guild_id   TEXT NOT NULL,       -- Discord Guild ID  → TEXT
 *   role_id    TEXT,                -- Discord Role ID   → TEXT (nullable)
 *   joined_at  TEXT NOT NULL,
 *   PRIMARY KEY (user_id, guild_id),
 *   FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
 * );
 * ```
 *
 * Usage
 * -----
 * ```ts
 * import { D1Client } from "./mods/d1";
 *
 * // env.DB is the D1Database binding defined in wrangler.toml
 * const db = new D1Client(env.DB);
 * await db.init();                       // enables PRAGMA foreign_keys = ON
 *
 * await db.applySchema(`
 *   CREATE TABLE IF NOT EXISTS guilds (
 *     guild_id TEXT PRIMARY KEY,
 *     name     TEXT NOT NULL
 *   )
 * `);
 *
 * await db.execute(
 *   "INSERT INTO guilds (guild_id, name) VALUES (?, ?)",
 *   ["123456789012345678", "My Server"]
 * );
 *
 * const rows = await db.query("SELECT * FROM guilds");
 * ```
 */

// ---------------------------------------------------------------------------
// Cloudflare D1 type stubs
// ---------------------------------------------------------------------------
// The `@cloudflare/workers-types` package provides these types.  They are
// declared here as a minimal interface so the file compiles without extra
// dependencies in environments where the package is not installed.

/** Metadata returned by a D1 write statement. */
export interface D1ExecMeta {
  /** Number of rows modified by the statement. */
  changes: number;
  /** Duration of the query in milliseconds. */
  duration: number;
  /** Last inserted row id (or 0 when not applicable). */
  last_row_id: number;
}

/** A single prepared statement ready to be sent to D1. */
export interface D1PreparedStatement {
  bind(...values: unknown[]): D1PreparedStatement;
  first<T = Record<string, unknown>>(column?: string): Promise<T | null>;
  run(): Promise<{ success: boolean; meta: D1ExecMeta; results: unknown[] }>;
  all<T = Record<string, unknown>>(): Promise<{ success: boolean; meta: D1ExecMeta; results: T[] }>;
}

/** Cloudflare D1Database binding injected from the Worker environment. */
export interface D1Database {
  prepare(query: string): D1PreparedStatement;
  batch<T = Record<string, unknown>>(
    statements: D1PreparedStatement[]
  ): Promise<Array<{ success: boolean; meta: D1ExecMeta; results: T[] }>>;
  exec(query: string): Promise<{ count: number; duration: number }>;
}

// ---------------------------------------------------------------------------
// Public result types
// ---------------------------------------------------------------------------

/**
 * Result returned by {@link D1Client.execute} (write statements).
 *
 * @property changes     - Number of rows affected.
 * @property last_row_id - Row-id of the last INSERT (0 when not applicable).
 * @property duration    - Wall-clock time in milliseconds.
 */
export interface ExecResult {
  changes: number;
  last_row_id: number;
  duration: number;
}

/**
 * A statement entry passed to {@link D1Client.batch}.
 *
 * @property sql    - Parameterised SQL string (use `?` placeholders).
 * @property params - Optional positional parameter values.
 */
export interface BatchStatement {
  sql: string;
  params?: unknown[];
}

/**
 * Result for one statement in a {@link D1Client.batch} call.
 *
 * @property rows    - Rows returned by the statement (empty for writes).
 * @property changes - Number of rows modified.
 * @property duration - Wall-clock time in milliseconds.
 */
export interface BatchResult<T = Record<string, unknown>> {
  rows: T[];
  changes: number;
  duration: number;
}

// ---------------------------------------------------------------------------
// Error helpers
// ---------------------------------------------------------------------------

/**
 * Wraps a D1 error, preserving the original message verbatim.
 *
 * D1 foreign-key violations surface as errors with the message
 * `"FOREIGN KEY constraint failed"`.  This class ensures that string is
 * never swallowed or reformatted.
 */
export class D1Error extends Error {
  /** The raw error message from D1 / SQLite, forwarded unchanged. */
  readonly original: string;

  constructor(message: string) {
    super(message);
    this.name = "D1Error";
    this.original = message;
  }
}

/**
 * Re-throw `err` as a {@link D1Error} with its message intact.
 * If `err` is already a {@link D1Error} it is re-thrown as-is.
 */
function forwardError(err: unknown): never {
  if (err instanceof D1Error) throw err;
  const msg = err instanceof Error ? err.message : String(err);
  throw new D1Error(msg);
}

// ---------------------------------------------------------------------------
// D1Client
// ---------------------------------------------------------------------------

/**
 * Thin, no-logic proxy around a Cloudflare `D1Database` binding.
 *
 * **Lifecycle**
 * 1. Construct with the `D1Database` from your Worker environment.
 * 2. Call `await db.init()` once before any other method.
 *    This runs `PRAGMA foreign_keys = ON` so that `REFERENCES` constraints
 *    are enforced at runtime.
 * 3. Call `await db.applySchema(sql)` with your DDL when you want tables
 *    created.  No tables are created automatically.
 * 4. Use `query`, `execute`, and `batch` for all subsequent data access.
 */
export class D1Client {
  private readonly db: D1Database;

  /**
   * @param db - The `D1Database` binding from the Cloudflare Worker
   *             environment (e.g. `env.DB`).
   */
  constructor(db: D1Database) {
    this.db = db;
  }

  // -------------------------------------------------------------------------
  // Initialisation
  // -------------------------------------------------------------------------

  /**
   * Initialise the connection.
   *
   * Must be called before any other method.  Runs:
   * ```sql
   * PRAGMA foreign_keys = ON;
   * ```
   * so that `REFERENCES` constraints declared in your schema are actually
   * enforced by SQLite / D1.
   *
   * No tables are created or altered by this method.
   *
   * @throws {D1Error} If the PRAGMA statement fails.
   */
  async init(): Promise<void> {
    try {
      await this.db.exec("PRAGMA foreign_keys = ON;");
    } catch (err) {
      forwardError(err);
    }
  }

  // -------------------------------------------------------------------------
  // Schema
  // -------------------------------------------------------------------------

  /**
   * Apply a raw DDL statement (e.g. `CREATE TABLE … REFERENCES …`).
   *
   * The SQL is executed exactly as provided — no parsing, no rewriting.
   * You can include any SQLite DDL including `FOREIGN KEY … REFERENCES`
   * clauses.
   *
   * ```ts
   * await db.applySchema(`
   *   CREATE TABLE IF NOT EXISTS sleep_records (
   *     id          INTEGER PRIMARY KEY AUTOINCREMENT,
   *     user_id     TEXT NOT NULL,      -- Discord User ID  → TEXT
   *     guild_id    TEXT NOT NULL,      -- Discord Guild ID → TEXT
   *     channel_id  TEXT,               -- Discord Channel ID → TEXT
   *     slept_at    TEXT NOT NULL,
   *     woke_at     TEXT,
   *     FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
   *   )
   * `);
   * ```
   *
   * @param sql - A complete DDL statement.
   * @throws {D1Error} On any D1 / SQLite error, including syntax errors.
   */
  async applySchema(sql: string): Promise<void> {
    try {
      await this.db.exec(sql);
    } catch (err) {
      forwardError(err);
    }
  }

  // -------------------------------------------------------------------------
  // Query (read)
  // -------------------------------------------------------------------------

  /**
   * Run a read-only SQL statement and return all matching rows.
   *
   * Use `?` placeholders for parameters:
   * ```ts
   * const rows = await db.query<{ user_id: string; guild_id: string }>(
   *   "SELECT user_id, guild_id FROM members WHERE guild_id = ?",
   *   [guildId]
   * );
   * ```
   *
   * @param sql    - Parameterised SQL (use `?` for each value).
   * @param params - Values bound to each `?`, in order.
   * @returns Array of rows typed as `T`.
   * @throws {D1Error} On any D1 / SQLite error, forwarded verbatim.
   */
  async query<T = Record<string, unknown>>(
    sql: string,
    params?: unknown[]
  ): Promise<T[]> {
    try {
      const stmt = params?.length
        ? this.db.prepare(sql).bind(...params)
        : this.db.prepare(sql);
      const result = await stmt.all<T>();
      return result.results;
    } catch (err) {
      forwardError(err);
    }
  }

  // -------------------------------------------------------------------------
  // Execute (write)
  // -------------------------------------------------------------------------

  /**
   * Run a write SQL statement (INSERT, UPDATE, DELETE) and return metadata.
   *
   * ```ts
   * const meta = await db.execute(
   *   "INSERT INTO guilds (guild_id, name) VALUES (?, ?)",
   *   [guildId, "My Server"]
   * );
   * console.log(meta.changes); // 1
   * ```
   *
   * **FK constraint errors are forwarded verbatim.**  If you insert a row
   * that violates a `REFERENCES` constraint, D1 throws with the message
   * `"FOREIGN KEY constraint failed"` and this method re-throws it
   * unchanged as a {@link D1Error}.
   *
   * @param sql    - Parameterised SQL.
   * @param params - Positional parameter values.
   * @returns {@link ExecResult} with `changes`, `last_row_id`, `duration`.
   * @throws {D1Error} On any D1 / SQLite error, including FK violations.
   */
  async execute(sql: string, params?: unknown[]): Promise<ExecResult> {
    try {
      const stmt = params?.length
        ? this.db.prepare(sql).bind(...params)
        : this.db.prepare(sql);
      const result = await stmt.run();
      return {
        changes: result.meta.changes,
        last_row_id: result.meta.last_row_id,
        duration: result.meta.duration,
      };
    } catch (err) {
      forwardError(err);
    }
  }

  // -------------------------------------------------------------------------
  // Batch
  // -------------------------------------------------------------------------

  /**
   * Run multiple SQL statements in a single D1 round-trip.
   *
   * Statements are executed in order.  If any statement fails (e.g. due to
   * an FK violation), D1 aborts the batch and this method throws a
   * {@link D1Error} with the original error message.
   *
   * ```ts
   * const results = await db.batch([
   *   { sql: "INSERT INTO guilds (guild_id, name) VALUES (?, ?)",
   *     params: [guildId, "My Server"] },
   *   { sql: "INSERT INTO members (user_id, guild_id) VALUES (?, ?)",
   *     params: [userId, guildId] },
   * ]);
   * console.log(results[0].changes); // 1
   * ```
   *
   * @param statements - Array of {@link BatchStatement} objects.
   * @returns Array of {@link BatchResult}, one per input statement.
   * @throws {D1Error} On any D1 / SQLite error, forwarded verbatim.
   */
  async batch<T = Record<string, unknown>>(
    statements: BatchStatement[]
  ): Promise<BatchResult<T>[]> {
    try {
      const prepared = statements.map(({ sql, params }) =>
        params?.length
          ? this.db.prepare(sql).bind(...params)
          : this.db.prepare(sql)
      );
      const results = await this.db.batch<T>(prepared);
      return results.map((r) => ({
        rows: r.results,
        changes: r.meta.changes,
        duration: r.meta.duration,
      }));
    } catch (err) {
      forwardError(err);
    }
  }
}
