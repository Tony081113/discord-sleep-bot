/**
 * mods/d1.ts — Zero-logic, stateless Cloudflare D1 proxy for the Discord bot.
 *
 * Design contract
 * ---------------
 * - **Zero logic**: this module contains no validation, no structure
 *   checking, no migrations, and no automatic behaviour of any kind.
 *   Every method is a direct pass-through to the underlying D1 binding.
 * - **Stateless**: the only state held is the `D1Database` reference
 *   supplied by the caller.  No flags, caches, or background tasks exist.
 * - **Four methods only**: `query`, `execute`, `batch`, `raw`.
 *   The caller decides what to run and when to run it.
 * - **Error forwarding**: D1 / SQLite errors — including
 *   `"FOREIGN KEY constraint failed"` — propagate to the caller unchanged.
 *   This module never catches, wraps, or transforms errors.
 * - **Full raw SQL**: `raw()` passes any SQL string directly to D1 with no
 *   inspection.  PRAGMA statements, DDL with `REFERENCES`, multi-statement
 *   scripts — all accepted verbatim.
 *
 * Discord ID type convention
 * --------------------------
 * Discord snowflake IDs are 64-bit unsigned integers that exceed
 * JavaScript's safe integer range.  They MUST be stored as TEXT in
 * D1 / SQLite and typed as `string` (or {@link DiscordId}) in TypeScript:
 *
 *   | Discord concept | Column type | TypeScript type |
 *   |-----------------|-------------|-----------------|
 *   | Guild ID        | TEXT        | DiscordId       |
 *   | Channel ID      | TEXT        | DiscordId       |
 *   | Role ID         | TEXT        | DiscordId       |
 *   | User ID         | TEXT        | DiscordId       |
 *
 * Columns declared as TEXT are returned by D1 as JavaScript `string`
 * values — no runtime coercion is performed by this module.
 *
 * Example DDL with FOREIGN KEY and Discord IDs
 * --------------------------------------------
 * ```sql
 * -- Enable FK enforcement first (caller's responsibility):
 * PRAGMA foreign_keys = ON;
 *
 * CREATE TABLE IF NOT EXISTS guilds (
 *   guild_id  TEXT PRIMARY KEY,   -- Discord Guild ID   → TEXT
 *   name      TEXT NOT NULL
 * );
 *
 * CREATE TABLE IF NOT EXISTS sleep_records (
 *   id          INTEGER PRIMARY KEY AUTOINCREMENT,
 *   user_id     TEXT NOT NULL,    -- Discord User ID    → TEXT
 *   guild_id    TEXT NOT NULL,    -- Discord Guild ID   → TEXT
 *   channel_id  TEXT NOT NULL,    -- Discord Channel ID → TEXT
 *   role_id     TEXT,             -- Discord Role ID    → TEXT (nullable)
 *   slept_at    TEXT NOT NULL,
 *   woke_at     TEXT,
 *   FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
 * );
 * ```
 *
 * Usage
 * -----
 * ```ts
 * import { D1Client } from "./mods/d1";
 *
 * // env.DB is the D1Database binding defined in wrangler.toml.
 * // The caller owns the lifecycle — construct and use as needed.
 * const db = new D1Client(env.DB);
 *
 * // Enable FK enforcement (caller's responsibility, run whenever needed):
 * await db.raw("PRAGMA foreign_keys = ON;");
 *
 * // Create tables with REFERENCES:
 * await db.raw(`CREATE TABLE IF NOT EXISTS guilds (
 *   guild_id TEXT PRIMARY KEY,
 *   name     TEXT NOT NULL
 * )`);
 *
 * // Write:
 * await db.execute(
 *   "INSERT INTO guilds (guild_id, name) VALUES (?, ?)",
 *   ["123456789012345678", "My Server"]
 * );
 *
 * // Read:
 * const rows = await db.query<{ guild_id: string; name: string }>(
 *   "SELECT * FROM guilds WHERE guild_id = ?",
 *   ["123456789012345678"]
 * );
 *
 * // Batch write:
 * await db.batch([
 *   { sql: "INSERT INTO guilds (guild_id, name) VALUES (?, ?)", params: [guildId, "A"] },
 *   { sql: "INSERT INTO guilds (guild_id, name) VALUES (?, ?)", params: [guildId2, "B"] },
 * ]);
 * ```
 */

// ---------------------------------------------------------------------------
// Cloudflare D1 type stubs
// ---------------------------------------------------------------------------
// Minimal interfaces matching @cloudflare/workers-types so this file
// compiles without requiring that package to be installed.

/** Metadata returned by a D1 write statement. */
export interface D1ExecMeta {
  /** Rows affected by the statement. */
  changes: number;
  /** Wall-clock duration in milliseconds. */
  duration: number;
  /** Row-id of the last INSERT, or 0 when not applicable. */
  last_row_id: number;
}

/** A prepared statement ready to send to D1. */
export interface D1PreparedStatement {
  bind(...values: unknown[]): D1PreparedStatement;
  first<T = Record<string, unknown>>(column?: string): Promise<T | null>;
  run(): Promise<{ success: boolean; meta: D1ExecMeta; results: unknown[] }>;
  all<T = Record<string, unknown>>(): Promise<{ success: boolean; meta: D1ExecMeta; results: T[] }>;
}

/**
 * Cloudflare D1Database binding from the Worker environment.
 * Pass `env.DB` (or equivalent) to {@link D1Client}.
 */
export interface D1Database {
  prepare(query: string): D1PreparedStatement;
  batch<T = Record<string, unknown>>(
    statements: D1PreparedStatement[]
  ): Promise<Array<{ success: boolean; meta: D1ExecMeta; results: T[] }>>;
  exec(query: string): Promise<{ count: number; duration: number }>;
}

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * A Discord snowflake ID stored and returned as a `string`.
 *
 * Always use TEXT (never INTEGER) for Discord IDs in D1 / SQLite.
 * Applies to: Guild ID, Channel ID, Role ID, User ID.
 */
export type DiscordId = string;

/**
 * Result returned by {@link D1Client.execute}.
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
 * A single statement entry for {@link D1Client.batch}.
 *
 * @property sql    - SQL string with `?` placeholders.
 * @property params - Values bound to each `?`, in order.
 */
export interface BatchStatement {
  sql: string;
  params?: unknown[];
}

/**
 * Result for one statement in a {@link D1Client.batch} call.
 *
 * @property rows     - Rows returned (empty array for write statements).
 * @property changes  - Number of rows affected.
 * @property duration - Wall-clock time in milliseconds.
 */
export interface BatchResult<T = Record<string, unknown>> {
  rows: T[];
  changes: number;
  duration: number;
}

/**
 * Result returned by {@link D1Client.raw}.
 *
 * @property count    - Number of statements executed.
 * @property duration - Wall-clock time in milliseconds.
 */
export interface RawResult {
  count: number;
  duration: number;
}

// ---------------------------------------------------------------------------
// D1Client
// ---------------------------------------------------------------------------

/**
 * Zero-logic, stateless proxy around a Cloudflare `D1Database` binding.
 *
 * Exposes exactly four methods: {@link query}, {@link execute},
 * {@link batch}, and {@link raw}.  No automatic behaviour, no validation,
 * no error transformation.  All decisions are delegated to the caller.
 */
export class D1Client {
  private readonly db: D1Database;

  /**
   * @param db - The `D1Database` binding from the Cloudflare Worker
   *             environment (e.g. `env.DB`).  No connection is opened;
   *             no side-effects occur.
   */
  constructor(db: D1Database) {
    this.db = db;
  }

  /** Prepare a statement and optionally bind positional parameters. */
  private _prepare(sql: string, params?: unknown[]): D1PreparedStatement {
    return params?.length
      ? this.db.prepare(sql).bind(...params)
      : this.db.prepare(sql);
  }

  // -------------------------------------------------------------------------
  // query — read rows
  // -------------------------------------------------------------------------

  /**
   * Execute a SQL statement and return all result rows.
   *
   * Typically used for SELECT.  Parameterise with `?` placeholders:
   *
   * ```ts
   * const rows = await db.query<{ guild_id: DiscordId; name: string }>(
   *   "SELECT guild_id, name FROM guilds WHERE guild_id = ?",
   *   ["123456789012345678"]
   * );
   * ```
   *
   * Discord ID columns declared as TEXT are returned as JavaScript
   * `string` values by D1.  Type them as {@link DiscordId} in `T`.
   *
   * @param sql    - SQL string (`?` for each bound value).
   * @param params - Positional values bound to each `?`.
   * @returns All result rows typed as `T`.
   */
  async query<T = Record<string, unknown>>(
    sql: string,
    params?: unknown[]
  ): Promise<T[]> {
    const result = await this._prepare(sql, params).all<T>();
    return result.results;
  }

  // -------------------------------------------------------------------------
  // execute — single write statement
  // -------------------------------------------------------------------------

  /**
   * Execute a single write SQL statement (INSERT, UPDATE, DELETE).
   *
   * ```ts
   * const result = await db.execute(
   *   "INSERT INTO guilds (guild_id, name) VALUES (?, ?)",
   *   ["123456789012345678", "My Server"]
   * );
   * console.log(result.changes); // 1
   * ```
   *
   * If D1 rejects the statement — for example because a `REFERENCES`
   * constraint is violated — the error is thrown to the caller unchanged.
   * The error message will contain `"FOREIGN KEY constraint failed"`.
   *
   * @param sql    - SQL string with `?` placeholders.
   * @param params - Positional values bound to each `?`.
   * @returns {@link ExecResult} — `changes`, `last_row_id`, `duration`.
   */
  async execute(sql: string, params?: unknown[]): Promise<ExecResult> {
    const result = await this._prepare(sql, params).run();
    return {
      changes: result.meta.changes,
      last_row_id: result.meta.last_row_id,
      duration: result.meta.duration,
    };
  }

  // -------------------------------------------------------------------------
  // batch — multiple statements in one round-trip
  // -------------------------------------------------------------------------

  /**
   * Execute multiple SQL statements in a single D1 round-trip.
   *
   * ```ts
   * const results = await db.batch([
   *   {
   *     sql: "INSERT INTO guilds (guild_id, name) VALUES (?, ?)",
   *     params: ["111111111111111111", "Server A"],
   *   },
   *   {
   *     sql: "INSERT INTO sleep_records (user_id, guild_id, slept_at) VALUES (?, ?, ?)",
   *     params: ["222222222222222222", "111111111111111111", new Date().toISOString()],
   *   },
   * ]);
   * ```
   *
   * If any statement violates a `REFERENCES` constraint D1 aborts and
   * throws the original error (e.g. `"FOREIGN KEY constraint failed"`)
   * to the caller — this module does not catch it.
   *
   * @param statements - Ordered list of {@link BatchStatement} objects.
   * @returns One {@link BatchResult} per input statement, in order.
   */
  async batch<T = Record<string, unknown>>(
    statements: BatchStatement[]
  ): Promise<BatchResult<T>[]> {
    const prepared = statements.map(({ sql, params }) =>
      this._prepare(sql, params)
    );
    const results = await this.db.batch<T>(prepared);
    return results.map((r) => ({
      rows: r.results,
      changes: r.meta.changes,
      duration: r.meta.duration,
    }));
  }

  // -------------------------------------------------------------------------
  // raw — verbatim SQL execution (DDL, PRAGMA, scripts)
  // -------------------------------------------------------------------------

  /**
   * Execute any SQL string verbatim via `D1Database.exec`.
   *
   * No parameters are supported (use {@link execute} or {@link batch} for
   * parameterised DML).  Intended for:
   *
   * - PRAGMA statements:
   *   ```ts
   *   await db.raw("PRAGMA foreign_keys = ON;");
   *   ```
   * - DDL with REFERENCES / FOREIGN KEY:
   *   ```ts
   *   await db.raw(`
   *     CREATE TABLE IF NOT EXISTS sleep_records (
   *       id          INTEGER PRIMARY KEY AUTOINCREMENT,
   *       user_id     TEXT NOT NULL,    -- Discord User ID    → TEXT
   *       guild_id    TEXT NOT NULL,    -- Discord Guild ID   → TEXT
   *       channel_id  TEXT NOT NULL,    -- Discord Channel ID → TEXT
   *       role_id     TEXT,             -- Discord Role ID    → TEXT (nullable)
   *       slept_at    TEXT NOT NULL,
   *       woke_at     TEXT,
   *       FOREIGN KEY (guild_id) REFERENCES guilds (guild_id) ON DELETE CASCADE
   *     )
   *   `);
   *   ```
   * - Multi-statement scripts (separated by `;`).
   *
   * Any error thrown by D1 is forwarded to the caller exactly as received.
   *
   * @param sql - Raw SQL string passed to D1 without modification.
   * @returns {@link RawResult} — `count` (statements run) and `duration`.
   */
  async raw(sql: string): Promise<RawResult> {
    return this.db.exec(sql);
  }
}
