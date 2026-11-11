"""Forward container loopback model connections to an operator-owned Unix socket."""

import asyncio
import os
import stat
from pathlib import Path


async def main():
    path = Path(os.environ["ARCHER_MODEL_SOCKET"])
    directory = path.parent.lstat()
    socket = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.geteuid()
        or directory.st_mode & 0o077
        or not stat.S_ISSOCK(socket.st_mode)
        or socket.st_uid != os.geteuid()
        or socket.st_mode & 0o077
    ):
        raise ValueError("Model bridge requires an owned private directory and Unix socket")
    slots = asyncio.Semaphore(16)

    async def relay(reader, writer):
        async with slots:
            upstream = None
            try:
                source, upstream = await asyncio.open_unix_connection(str(path))

                async def pipe(incoming, outgoing):
                    while data := await asyncio.wait_for(incoming.read(65536), 300):
                        outgoing.write(data)
                        await outgoing.drain()
                    if outgoing.can_write_eof():
                        outgoing.write_eof()

                await asyncio.gather(pipe(reader, upstream), pipe(source, writer))
            except (OSError, TimeoutError, ConnectionError):
                pass
            finally:
                for stream in (upstream, writer):
                    if stream is not None:
                        stream.close()
                for stream in (upstream, writer):
                    if stream is not None:
                        try:
                            await stream.wait_closed()
                        except (OSError, ConnectionError):
                            pass

    server = await asyncio.start_server(relay, "127.0.0.1", 11434, limit=65536)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
