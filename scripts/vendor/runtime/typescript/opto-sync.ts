/**
 * `RpcTransport` implemented over opto-sync's durable queue.
 *
 * # Direction of the dependency
 *
 * This module calls opto-sync. opto-sync never calls it. `@opto-sync/client`
 * exports a namespaced surface explicitly "for wrapper libraries that
 * re-export a curated slice of opto-sync", which is what this is. Teaching
 * opto-sync to speak RPC would invert the arrow its own interfaces README
 * pins down.
 *
 * # Why only some calls are queued
 *
 * opto-sync's queue is record-shaped, not a general message bus: a payload
 * must be a JSON object, a table must be a SQL-safe scope id, a queued delete
 * is a tombstone with no data, and there is no per-mutation response channel.
 * So reads go straight to the server and only mutations are queued. Which is
 * which comes from the route map's `delivery`, checked at generation time.
 *
 * # What a queued call returns
 *
 * The caller's own write, read back through opto-sync's local view. That is
 * the honest answer while offline: the mutation is durable, the local
 * projection reflects it, and the authoritative value arrives later through
 * the ordinary pull/reconcile path.
 */

/** Structurally compatible with the generated `RpcRequest`. */
export interface RidlRequest {
  readonly key: string;
  readonly method: string;
  readonly path: string;
  readonly pathTemplate: string;
  readonly query: ReadonlyArray<readonly [string, string]>;
  readonly body?: string;
  readonly delivery: "direct" | "opto_sync_queued";
  readonly optoSync?: {
    readonly table: string;
    readonly operation: "upsert" | "delete";
    readonly recordId: {
      readonly from: "path" | "request" | "minted";
      readonly name?: string;
    };
  };
}

/** Performs a plain, online request. Usually a thin `fetch` wrapper. */
export interface DirectTransport {
  call(request: RidlRequest): Promise<string>;
}

/**
 * The slice of `OptoSyncClient` this transport needs.
 *
 * Declared structurally rather than importing the class, so this module builds
 * and tests without opto-sync installed and a caller can pass the real client,
 * the reactive wrapper, or a fake.
 */
export interface MutationQueue {
  queueMutation(
    tableName: string,
    recordId: string,
    payload: Record<string, unknown>,
  ): Promise<number>;
  queueDelete(tableName: string, recordId: string): Promise<number>;
}

/**
 * Projects a queued mutation into the response the route map declares.
 * Back it with `OptoSyncClient.localView` — the caller's own write layered over
 * the last authoritative row, which is the optimistic value the UI should show.
 */
export interface LocalReadback {
  localJson(table: string, recordId: string): Promise<string | undefined>;
}

export class OptoSyncTransportError extends Error {
  constructor(
    message: string,
    readonly reason:
      | "not-queueable"
      | "no-local-projection"
      | "queue-failed"
      | "direct-failed",
    options?: { cause?: unknown },
  ) {
    super(message, options);
    this.name = "OptoSyncTransportError";
  }
}

/**
 * Find the substituted value of `{name}` by walking the template and the real
 * path in lockstep.
 *
 * Taking the last segment would be wrong for `/v1/matters/{id}/walk`, where the
 * final segment is a literal — every matter would be filed under `walk`.
 */
export function segmentForParam(
  template: string,
  path: string,
  name: string,
): string | undefined {
  const wanted = `{${name}}`;
  const expected = template.split("/");
  const actual = path.split("/");
  for (let i = 0; i < expected.length && i < actual.length; i += 1) {
    if (expected[i] === wanted) return decodeURIComponent(actual[i]!);
  }
  return undefined;
}

/**
 * A deterministic id for `recordId.from === "minted"`, derived from the
 * request. opto-sync dedupes on `(clientId, mutationId)`, so a stable id keeps
 * a retry of the same call idempotent rather than creating a second record.
 */
export function mintRecordId(request: RidlRequest): string {
  const input = `${request.key}${request.path}${request.body ?? ""}`;
  // FNV-1a, 32-bit, matching the shape of the Rust runtime's 64-bit version.
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `${request.key}-${hash.toString(16).padStart(8, "0")}`;
}

/** Routes each call by the `delivery` the route map declared. */
export class OptoSyncTransport {
  constructor(
    private readonly direct: DirectTransport,
    private readonly queue: MutationQueue,
    private readonly readback: LocalReadback,
  ) {}

  async call(request: RidlRequest): Promise<string> {
    const binding = request.optoSync;
    if (request.delivery === "direct" || binding === undefined) {
      try {
        return await this.direct.call(request);
      } catch (cause) {
        throw new OptoSyncTransportError(
          `direct call to ${request.key} failed`,
          "direct-failed",
          { cause },
        );
      }
    }

    const recordId = this.recordId(request, binding.recordId);

    try {
      if (binding.operation === "delete") {
        await this.queue.queueDelete(binding.table, recordId);
      } else {
        if (request.body === undefined) {
          // Validation rejects this shape, so reaching it means the generated
          // code is older than the route map it came from.
          throw new OptoSyncTransportError(
            `${request.key}: a queued upsert needs a JSON body`,
            "not-queueable",
          );
        }
        const payload = JSON.parse(request.body) as Record<string, unknown>;
        await this.queue.queueMutation(binding.table, recordId, payload);
      }
    } catch (cause) {
      if (cause instanceof OptoSyncTransportError) throw cause;
      throw new OptoSyncTransportError(
        `${request.key}: opto-sync rejected the mutation`,
        "queue-failed",
        { cause },
      );
    }

    const local = await this.readback.localJson(binding.table, recordId);
    if (local === undefined) {
      throw new OptoSyncTransportError(
        `queued ${binding.table}/${recordId} but no local projection was available`,
        "no-local-projection",
      );
    }
    return local;
  }

  private recordId(
    request: RidlRequest,
    source: NonNullable<RidlRequest["optoSync"]>["recordId"],
  ): string {
    if (source.from === "minted") return mintRecordId(request);

    if (source.from === "path") {
      const value = segmentForParam(
        request.pathTemplate,
        request.path,
        source.name ?? "",
      );
      if (value === undefined) {
        throw new OptoSyncTransportError(
          `${request.key}: recordId names path parameter ` +
            `'${source.name}', which is not in '${request.pathTemplate}'`,
          "not-queueable",
        );
      }
      return value;
    }

    if (request.body === undefined) {
      throw new OptoSyncTransportError(
        `${request.key}: recordId names a request field but there is no body`,
        "not-queueable",
      );
    }
    const parsed = JSON.parse(request.body) as Record<string, unknown>;
    const value = parsed[source.name ?? ""];
    if (typeof value !== "string") {
      throw new OptoSyncTransportError(
        `${request.key}: request field '${source.name}' is absent or not a string`,
        "not-queueable",
      );
    }
    return value;
  }
}
