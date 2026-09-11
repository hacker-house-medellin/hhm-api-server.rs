/**
 * `RpcTransport` over HTTP, WebSocket and TCP.
 *
 * Generated code produces a request that already carries everything all three
 * need — key, method, substituted path, template, query, body, delivery — so
 * the transport is a choice at the edge and the same generated call works over
 * any of them without regeneration.
 *
 * HTTP needs no envelope. WebSocket and TCP carry the frame envelope from
 * `./frame.ts`; `FramedConnection` is one method wide, so multiplexing,
 * reconnect and auth stay in the application and this module stays testable
 * without a socket.
 *
 * Streaming operations are declared and validated in the route map but the
 * emitters do not yet produce a stream-returning signature; `FramedStream` is
 * the seam that will land on. Unary works today.
 */

import { Correlator, type Frame, callFrame } from "./frame.ts";
import { emit, type Carrier, type RpcEvent, type RpcTelemetrySink } from "./telemetry.ts";

/** Structurally compatible with the generated request. */
export interface RidlRequest {
  readonly key: string;
  readonly method: string;
  readonly path: string;
  readonly pathTemplate: string;
  readonly query: ReadonlyArray<readonly [string, string]>;
  readonly body?: string;
  readonly delivery: "direct" | "opto_sync_queued";
}

export interface RpcTransport {
  call(request: RidlRequest): Promise<string>;
}

/** A plain online request. The app owns URLs, auth, retries and TLS. */
export interface HttpCall {
  call(request: RidlRequest): Promise<string>;
}

/** One framed exchange: send the call, resolve with the frames answering it. */
export interface FramedConnection {
  exchange(call: Frame): Promise<Frame[]>;
  readonly carrier: Extract<Carrier, "websocket" | "tcp">;
}

/** The seam a streaming client will use once the emitters produce one. */
export interface FramedStream {
  open(call: Frame): AsyncIterable<Frame>;
}

export class RpcTransportError extends Error {
  readonly reason: "carrier" | "remote" | "protocol";
  readonly code?: string;

  constructor(
    message: string,
    reason: "carrier" | "remote" | "protocol",
    code?: string,
    options?: { cause?: unknown },
  ) {
    super(message, options);
    this.name = "RpcTransportError";
    this.reason = reason;
    this.code = code;
  }
}

function observe(
  sink: RpcTelemetrySink | undefined,
  request: RidlRequest,
  service: string,
  carrier: Carrier,
  startedMs: number,
  correlationId: string | undefined,
  error: unknown,
): void {
  let outcome: RpcEvent["outcome"] = "ok";
  let code: string | undefined;
  if (error instanceof RpcTransportError) {
    outcome = error.reason === "carrier" ? "transport_error" : "failed";
    code = error.code ?? (error.reason === "protocol" ? "protocol" : undefined);
  } else if (error !== undefined) {
    outcome = "transport_error";
  }
  emit(sink, {
    key: request.key,
    service,
    method: request.method,
    pathTemplate: request.pathTemplate,
    carrier,
    outcome,
    durationMicros: Math.round((performance.now() - startedMs) * 1000),
    code,
    correlationId,
  });
}

export class HttpTransport implements RpcTransport {
  readonly #http: HttpCall;
  readonly #service: string;
  readonly #telemetry?: RpcTelemetrySink;

  constructor(http: HttpCall, service: string, telemetry?: RpcTelemetrySink) {
    this.#http = http;
    this.#service = service;
    this.#telemetry = telemetry;
  }

  async call(request: RidlRequest): Promise<string> {
    const started = performance.now();
    try {
      const body = await this.#http.call(request);
      observe(this.#telemetry, request, this.#service, "http", started, undefined, undefined);
      return body;
    } catch (cause) {
      const error =
        cause instanceof RpcTransportError
          ? cause
          : new RpcTransportError(`${request.key}: http call failed`, "carrier", undefined, { cause });
      observe(this.#telemetry, request, this.#service, "http", started, undefined, error);
      throw error;
    }
  }
}

export class FramedTransport implements RpcTransport {
  readonly #correlator: Correlator;
  readonly #conn: FramedConnection;
  readonly #service: string;
  readonly #telemetry?: RpcTelemetrySink;

  constructor(
    conn: FramedConnection,
    service: string,
    idPrefix = "",
    telemetry?: RpcTelemetrySink,
  ) {
    this.#conn = conn;
    this.#service = service;
    this.#telemetry = telemetry;
    this.#correlator = new Correlator(idPrefix);
  }

  async call(request: RidlRequest): Promise<string> {
    const started = performance.now();
    const id = this.#correlator.take();
    let body: { value: unknown } | undefined;
    if (request.body !== undefined) {
      try {
        body = { value: JSON.parse(request.body) };
      } catch (cause) {
        throw new RpcTransportError(`${request.key}: request body is not JSON`, "protocol", undefined, { cause });
      }
    }

    try {
      const frames = await this.#conn.exchange(
        callFrame(id, request.key, request.method, request.path, request.query, body),
      );
      const answer = unaryAnswer(id, frames);
      observe(this.#telemetry, request, this.#service, this.#conn.carrier, started, id, undefined);
      return answer;
    } catch (cause) {
      const error =
        cause instanceof RpcTransportError
          ? cause
          : new RpcTransportError(`${request.key}: framed call failed`, "carrier", undefined, { cause });
      observe(this.#telemetry, request, this.#service, this.#conn.carrier, started, id, error);
      throw error;
    }
  }
}

/**
 * Reduce the frames answering a unary call to its response body.
 *
 * Strict on shape: a stream of data frames arriving for a unary operation is a
 * contract violation on the server's side, and saying so beats quietly keeping
 * the first one.
 */
export function unaryAnswer(id: string, frames: readonly Frame[]): string {
  let body: string | undefined;
  let ended = false;
  for (const frame of frames) {
    if (frame.id !== id) {
      throw new RpcTransportError(
        `frame for correlation id ${frame.id} arrived on the exchange for ${id}`,
        "protocol",
      );
    }
    switch (frame.t) {
      case "data":
        if (body !== undefined) {
          throw new RpcTransportError("a unary operation answered with more than one data frame", "protocol");
        }
        if (!frame.hasBody) throw new RpcTransportError("a data frame arrived without a body", "protocol");
        body = JSON.stringify(frame.body);
        break;
      case "end":
        ended = true;
        break;
      case "error":
        throw new RpcTransportError(frame.message ?? `remote ${frame.code}`, "remote", frame.code);
      case "cancel":
        throw new RpcTransportError("the peer cancelled the exchange", "protocol");
      case "call":
        throw new RpcTransportError("a call frame cannot answer a call", "protocol");
    }
  }
  if (body === undefined) throw new RpcTransportError("the exchange ended without a response body", "protocol");
  if (!ended) throw new RpcTransportError("the exchange delivered a body but never ended", "protocol");
  return body;
}
