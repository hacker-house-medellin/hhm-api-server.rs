/**
 * The telemetry seam. ores-otel plugs in here; this module never imports it.
 *
 * # Direction of the dependency
 *
 * The application depends on `ores-otel` / `next-loggers` and hands this
 * module something that satisfies `RpcTelemetrySink`. Nothing here links an
 * OTel SDK, installs a global provider, owns exporter shutdown, or decides
 * sampling — the same shape `opto-sync-clients` uses for
 * `ProtocolSyncTelemetrySink`, so one application adapter serves both.
 *
 * # Fail-open, always
 *
 * Telemetry that can break a call is worse than no telemetry. A sink that
 * throws or rejects changes nothing about the RPC.
 *
 * # What is deliberately absent
 *
 * No request body, no response body, no path parameter values, no `meta`
 * contents. A route map cannot know which fields are sensitive, so none of the
 * payload crosses this boundary. The operation key, the carrier and the
 * outcome are enough for latency and error-rate signals.
 */

export type Carrier = "http" | "websocket" | "tcp" | "queue";
export type Outcome = "ok" | "failed" | "transport_error" | "queued";

export interface RpcEvent {
  /** Operation key from the route map — low cardinality, safe as a label. */
  readonly key: string;
  readonly service: string;
  readonly method: string;
  /** The path *template*, never the substituted path: no ids in it. */
  readonly pathTemplate: string;
  readonly carrier: Carrier;
  readonly outcome: Outcome;
  readonly durationMicros: number;
  readonly code?: string;
  /** Frame correlation id, for stitching client to server on a framed transport. */
  readonly correlationId?: string;
  /** Passed through if the caller is already in a trace; never created here. */
  readonly traceId?: string;
  readonly spanId?: string;
}

export interface RpcTelemetrySink {
  emit(event: RpcEvent): void | Promise<void>;
}

/** Deliver one event without letting it affect the call. */
export function emit(sink: RpcTelemetrySink | undefined, event: RpcEvent): void {
  if (!sink) return;
  try {
    const result = sink.emit(event);
    // A rejected promise from a fire-and-forget sink must not become an
    // unhandled rejection that takes down the process.
    if (result && typeof (result as Promise<void>).catch === "function") {
      void (result as Promise<void>).catch(() => undefined);
    }
  } catch {
    // A broken exporter is not the caller's problem.
  }
}
