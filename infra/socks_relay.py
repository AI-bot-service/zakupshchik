"""Ретранслятор TCP только для стенда на VPS: SOCKS туннеля рабочего ПК слушает 127.0.0.1 хоста,
контейнеры его не видят. Слушает адрес шлюза docker-сети и пересылает байты как есть.

У жюри и у заказчика в России прокси не нужен: PROXY_URL пустой, сервис не запускается (профиль tunnel).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    # Только контейнеры своей сети: остальные сети хоста тоже могут достучаться до адреса шлюза
    peer = ipaddress.ip_address(client_w.get_extra_info("peername")[0])
    if peer not in ipaddress.ip_network(os.environ["RELAY_ALLOW"]):
        client_w.close()
        return
    host, port = os.environ["RELAY_TARGET"].rsplit(":", 1)
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=10)
    except (OSError, TimeoutError):
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


async def main() -> None:
    host, port = os.environ["RELAY_LISTEN"].rsplit(":", 1)
    server = await asyncio.start_server(_handle, host, int(port))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
