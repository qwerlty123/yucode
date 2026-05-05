"""Tiny CONNECT proxy that permits only exact provider hostnames."""

from __future__ import annotations

import argparse
import asyncio


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    allowed: frozenset[str],
) -> None:
    try:
        request = await asyncio.wait_for(reader.readline(), timeout=10)
        parts = request.decode("latin-1", errors="replace").strip().split()
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        target = parts[1]
        host, separator, port_text = target.rpartition(":")
        if not separator:
            host, port_text = target, "443"
        host = host.strip("[]").rstrip(".").lower()
        try:
            port = int(port_text)
        except ValueError:
            port = 0
        while True:
            header = await asyncio.wait_for(reader.readline(), timeout=10)
            if header in (b"\r\n", b"\n", b""):
                break
        if host not in allowed or port != 443:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            _relay(reader, upstream_writer),
            _relay(upstream_reader, writer),
        )
    except (ConnectionError, TimeoutError):
        pass
    finally:
        writer.close()


async def serve(hosts: list[str]) -> None:
    allowed = frozenset(host.rstrip(".").lower() for host in hosts)
    server = await asyncio.start_server(lambda reader, writer: _handle(reader, writer, allowed), "0.0.0.0", 8080)
    print("READY", flush=True)
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow", action="append", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.allow))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
