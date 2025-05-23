import ast
import asyncio
import logging
from datetime import datetime, timedelta
from os.path import join
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional, Set, Tuple, Union
from urllib import parse

from aioaria2 import Aria2WebsocketClient, AsyncAria2Server
from aioaria2.exceptions import Aria2rpcException
from aiofile import AIOFile, Reader, Writer
from aiopath import AsyncPath
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from pyrogram import errors
from tenacity import (
    before_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from bot import command, plugin, util

if TYPE_CHECKING:
    from .gdrive import GoogleDrive
    from .mega import Mega
    from ..core import Bot


class SeedProtocol(asyncio.SubprocessProtocol):

    def __init__(self, future: asyncio.Future, log: logging.Logger):
        self.future = future
        self.log = log

        self.output = bytearray()

    def pipe_data_received(self, fd: int, data: bytes):
        self.output.extend(data)

    def process_exited(self):
        self.future.set_result(True)


class Aria2WebSocketServer:
    log: ClassVar[logging.Logger] = logging.getLogger("aria2ws")

    bot: "Bot"
    cancelled: Set[str]
    downloads: Dict[str, util.aria2.Download]
    lock: asyncio.Lock
    uploads: Dict[str, Any]

    index_link: Optional[str]
    context: command.Context
    stopping: bool
    mega: Set[str]

    _protocol: str

    def __init__(self, bot: "Bot", drive: "GoogleDrive") -> None:
        self.bot = bot
        self.drive = drive

        self.lock = asyncio.Lock()

        self.cancelled = set()
        self.downloads = {}
        self.uploads = {}

        self.index_link = self.drive.index_link
        self.context = None  # type: ignore
        self.stopping = False
        self.mega = set()

    @classmethod
    async def init(cls, bot: "Bot", drive: "GoogleDrive") -> "Aria2WebSocketServer":
        self = cls(bot, drive)

        download_path = self.bot.config["download_path"]
        await download_path.mkdir(parents=True, exist_ok=True)

        link = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
        async with self.bot.http.get(link) as resp:
            trackers_list: str = await resp.text()
            trackers: str = "[" + trackers_list.replace("\n\n", ",") + "]"

        cmd = [
            "aria2c", f"--dir={str(download_path)}", "--enable-rpc",
            "--rpc-listen-all=false", "--max-connection-per-server=10",
            "--rpc-max-request-size=1024M", "--seed-time=0.01",
            "--seed-ratio=0.1", "--max-concurrent-downloads=5",
            "--min-split-size=10M", "--follow-torrent=mem", "--split=10",
            "--bt-save-metadata=true", f"--bt-tracker={trackers}",
            "--daemon=true", "--allow-overwrite=true"
        ]
        key_path = AsyncPath(Path.home() / ".cache" / "bot" / ".certs")
        if await (key_path / "cert.pem"
                  ).is_file() and await (key_path / "key.pem").is_file():
            cmd.insert(4, "--rpc-listen-port=8443")
            cmd.insert(3, "--rpc-secure=true")
            cmd.insert(3, "--rpc-private-key=" + str(key_path / "key.pem"))
            cmd.insert(3, "--rpc-certificate=" + str(key_path / "cert.pem"))
            self._protocol = "https://localhost:8443/jsonrpc"
        else:
            cmd.insert(4, "--rpc-listen-port=8100")
            self._protocol = "http://127.0.0.1:8100/jsonrpc"

        server = AsyncAria2Server(*cmd, daemon=True)
        await server.start()
        await server.wait()

        return self

    async def start(self) -> Aria2WebsocketClient:
        client = await Aria2WebsocketClient.new(url=self._protocol)

        trigger = [(self.onDownloadStart, "onDownloadStart"),
                   (self.onDownloadComplete, "onDownloadComplete"),
                   (self.onDownloadError, "onDownloadError")]
        for handler, name in trigger:
            client.register(handler, f"aria2.{name}")

        self.bot.loop.create_task(self.updateProgress())
        return client

    @property
    def count(self) -> int:
        return len(self.downloads)

    async def checkDelete(self) -> None:
        if self.count == 0 and self.context is not None and self.context.response is not None:
            await self.context.response.delete()
            self.context = None  # type: ignore

    async def getDownload(self, client: Aria2WebsocketClient,
                          gid: str) -> util.aria2.Download:
        res = await client.tellStatus(gid)
        return util.aria2.Download(client, res)

    async def onDownloadStart(self, client: Aria2WebsocketClient,
                              data: Union[Dict[str, Any], Any]) -> None:
        gid = data["params"][0]["gid"]
        async with self.lock:
            self.downloads[gid] = await self.getDownload(client, gid)
        self.log.info(f"Starting download: [gid: '{gid}']")

    async def onDownloadComplete(self, client: Aria2WebsocketClient,
                                 data: Union[Dict[str, Any], Any]) -> None:
        gid = data["params"][0]["gid"]

        async with self.lock:
            self.downloads[gid] = await self.getDownload(client, gid)
            file = self.downloads[gid]
            if file.metadata is True:
                del self.downloads[gid]
                self.log.info(f"Complete download: [gid: '{gid}'] - Metadata")
                return

        if await file.is_file():
            if gid in self.mega:
                async with self.lock:
                    del self.downloads[gid]

                M: "Mega" = self.bot.plugins["Mega"]  # type: ignore
                outputFile: AsyncPath = M.file[gid]["file"]
                aes = M.file[gid]["aes"]
                CHUNK_SIZE = 50 * 1024 * 1024

                self.log.info(f"Decrypting download: [gid: '{gid}']")
                async with AIOFile(outputFile, "w+b") as f:
                    writer = Writer(f)
                    async with AIOFile(file.path, "rb") as temp:
                        reader = Reader(temp, chunk_size=CHUNK_SIZE)
                        async for chunk in reader:
                            chunk = await util.run_sync(aes.decrypt, chunk)
                            await writer(chunk)
                            await f.fsync()
                await file.path.unlink()
                outputFile = await outputFile.rename(outputFile.parent / outputFile.stem)
                async with self.lock:
                    self.mega.remove(gid)
                    self.downloads[gid] = await file.update()
                    self.uploads[gid] = await self.drive.uploadFile(self.downloads[gid])
            else:
                async with self.lock:
                    self.uploads[gid] = await self.drive.uploadFile(file)
        elif await file.is_dir():
            folderId = await self.drive.createFolder(file.name)
            folderTasks = self.drive.uploadFolder(file.dir / file.name,
                                                  gid=gid,
                                                  parent_id=folderId)

            async with self.lock:
                self.uploads[gid] = {"generator": folderTasks, "counter": 0}

            cancelled = False
            async for task in folderTasks:
                try:
                    await task
                except asyncio.CancelledError:
                    cancelled = True
                    break
                else:
                    async with self.lock:
                        self.uploads[gid]["counter"] += 1

            if not cancelled:
                async with self.lock:
                    del self.uploads[gid]
                    del self.downloads[gid]

                folderLink = (
                    f"**GoogleDrive folderLink**: [{file.name}]"
                    f"(https://drive.google.com/drive/folders/{folderId})")
                if self.index_link is not None:
                    link = join(self.index_link, parse.quote(file.name + "/"))
                    folderLink += f"\n\n__IndexLink__: [Here]({link})."

                async with self.lock:
                    if self.count == 0:
                        await asyncio.gather(
                            self.bot.respond(self.context.response,
                                             folderLink,
                                             mode="reply"),
                            self.context.response.delete())
                        self.context = None  # type: ignore
                    else:
                        await self.context.respond(folderLink,
                                                   mode="reply")
        else:
            async with self.lock:
                del self.downloads[gid]
            self.log.warning(f"Can't upload '{file.name}', "
                             f"due to '{file.dir}' is not accessible")

        self.log.info(f"Complete download: [gid: '{gid}']")

        if file.bittorrent:
            self.bot.loop.create_task(self.seedFile(file),
                                      name=f"Seed-{file.gid}")

    async def onDownloadError(self, client: Aria2WebsocketClient,
                              data: Union[Dict[str, Any], Any]) -> None:
        gid = data["params"][0]["gid"]

        file = await self.getDownload(client, gid)
        await self.bot.respond(self.context.msg,
                               f"`{file.name}`\n"
                               f"Status: **{file.status.capitalize()}**\n"
                               f"Error: __{file.error_message}__\n"
                               f"Code: **{file.error_code}**",
                               mode="reply")

        self.log.warning(f"[gid: '{gid}']: {file.error_message}")
        async with self.lock:
            del self.downloads[file.gid]
            await self.checkDelete()

    @retry(wait=wait_random_exponential(multiplier=2, min=3, max=6),
           stop=stop_after_attempt(5),
           retry=retry_if_exception_type(KeyError))
    async def checkProgress(self) -> str:
        progress_string = ""
        time = util.time.format_duration_td
        human = util.file.human_readable_bytes

        for file in list(self.downloads.values()):
            try:
                file = await file.update()
            except Aria2rpcException:
                continue

            if (file.failed or file.paused or
                (file.complete and file.metadata) or file.removed):
                continue

            if file.complete and not file.metadata:
                if await file.is_dir():
                    counter = self.uploads[file.gid]["counter"]
                    length = len(file.files)
                    percent = round(((counter / length) * 100), 2)
                    progress_string += (
                        f"`{file.name}`\nGID: `{file.gid}`\n"
                        f"__ComputingFolder: [{counter}/{length}] "
                        f"{percent}%__\n\n")
                elif await file.is_file():
                    if file.gid in self.mega:
                        M: "Mega" = self.bot.plugins["Mega"]  # type: ignore
                        tempFile = M.file[file.gid]["file"]

                        fileSize = file.total_length
                        decrypted = await (tempFile.stat()).st_size
                        percent = round(((decrypted / fileSize) * 100))
                        progress_string += (
                            f"`{file.name}`\nGID: `{file.gid}`\n"
                            f"Status: **Decrypting"
                            f"__{human(decrypted)} of {human(fileSize)}"
                            f"{percent}%__\n\n")
                        continue

                    f = self.uploads[file.gid]
                    progress, done = await self.uploadProgress(f)
                    if not done:
                        progress_string += progress

                continue

            downloaded = file.completed_length
            file_size = file.total_length
            percent = file.progress
            speed = file.download_speed
            eta = file.eta_formatted
            bullets = "●" * int(round(percent * 10)) + "○"
            if len(bullets) > 10:
                bullets = bullets.replace("○", "")

            space = '    ' * (10 - len(bullets))
            progress_string += (
                f"`{file.name}`\nGID: `{file.gid}`\n"
                f"Status: **{file.status.capitalize()}**\n"
                f"Progress: [{bullets + space}] {round(percent * 100)}%\n"
                f"__{human(downloaded)} of {human(file_size)} @ "  # type: ignore
                f"{human(speed, postfix='/s')}\neta - {time(eta)}__\n\n")

        return progress_string

    async def updateProgress(self) -> None:
        last_update_time = None
        while not self.stopping:
            for gid in self.cancelled.copy():
                async with self.lock:
                    file = None
                    if gid in self.downloads:
                        file = self.downloads[gid]
                        del self.downloads[gid]
                        self.log.info(f"Aborted download: [gid: '{gid}']")
                    if (file is not None and await file.is_file() and
                            gid in self.uploads):
                        del self.uploads[gid]
                        self.log.info(f"Aborted upload file: [gid: '{gid}']")
                    elif (file is not None and await file.is_dir() and
                            gid in self.uploads):
                        for task in asyncio.all_tasks():
                            if task.get_name() == gid:
                                task.cancel()
                        await self.uploads[gid]["generator"].aclose()
                        del self.uploads[gid]
                        self.log.info(f"Aborted upload folder: [gid: '{gid}']")
                    self.cancelled.remove(gid)
                    await self.checkDelete()

            try:
                progress = await self.checkProgress()
            except HttpError as e:
                self.log.error("Error on progress update", exc_info=e)
                continue
            now = datetime.now()

            if last_update_time is None or (
                    now - last_update_time).total_seconds() >= 5 and (progress
                                                                      != ""):
                try:
                    if self.context is not None:
                        async with self.lock:
                            await self.context.respond(progress)
                except errors.MessageNotModified:
                    pass
                except errors.FloodWait as flood:
                    await asyncio.sleep(flood.x)  # type: ignore
                finally:
                    last_update_time = now

            await asyncio.sleep(0.1)

    async def uploadProgress(
            self, file: MediaFileUpload) -> Tuple[Union[str, None], bool]:
        time = util.time.format_duration_td
        human = util.file.human_readable_bytes
        progress = None

        status, response = await util.run_sync(file.next_chunk, num_retries=5)  # type: ignore
        if status:
            file_size = status.total_size
            end = util.time.sec() - file.start_time  # type: ignore
            uploaded = status.resumable_progress
            percent = uploaded / file_size
            speed = round(uploaded / end, 2)
            eta = timedelta(seconds=int(round((file_size - uploaded) / speed)))
            bullets = "●" * int(round(percent * 10)) + "○"
            if len(bullets) > 10:
                bullets = bullets.replace("○", "")

            space = '    ' * (10 - len(bullets))
            progress = (
                f"`{file.name}`\nGID: `{file.gid}`\n"  # type: ignore
                f"Status: **Uploading**\n"
                f"Progress: [{bullets + space}] {round(percent * 100)}%\n"
                f"__{human(uploaded)} of {human(file_size)} @ "
                f"{human(speed, postfix='/s')}\neta - {time(eta)}__\n\n")

        if response is None and progress is not None:
            return progress, False

        file_size = response.get("size")
        mirrorLink = response.get("webContentLink")
        fileLink = (f"**GoogleDrive Link**: [{file.name}]({mirrorLink}) "  # type: ignore
                    f"(__{human(int(file_size))}__)")
        if self.index_link is not None:
            link = join(self.index_link, parse.quote(file.name))  # type: ignore
            fileLink += f"\n\n__IndexLink__: [Here]({link})."

        async with self.lock:
            await self.bot.respond(self.context.msg, fileLink, mode="reply")
            del self.uploads[file.gid]  # type: ignore
            del self.downloads[file.gid]  # type: ignore
            await self.checkDelete()

        return None, True

    async def seedFile(self, file: util.aria2.Download) -> Optional[str]:
        file_path = file.dir / (str(file.info_hash) + ".torrent")
        if not await file_path.is_file():
            return

        port = util.aria2.get_free_port()
        cmd = [
            "aria2c", "--enable-rpc", "--rpc-listen-all=false",
            f"--rpc-listen-port={port}", "--bt-seed-unverified=true",
            "--seed-ratio=1", f"-i {str(file_path)}"
        ]

        future = self.bot.loop.create_future()
        transport = None
        try:
            transport, protocol = await self.bot.loop.subprocess_exec(
                lambda: SeedProtocol(future, self.log), *cmd, stdin=None)

            await future
        finally:
            if transport:
                transport.close()

        data = bytes(protocol.output)  # type: ignore
        return data.decode("ascii").rstrip()


class Aria2(plugin.Plugin):
    name: ClassVar[str] = "Aria2"

    client: Aria2WebsocketClient

    _ws: Aria2WebSocketServer

    @retry(wait=wait_random_exponential(multiplier=2, min=3, max=12),
           stop=stop_after_attempt(10),
           retry=retry_if_exception_type(Aria2rpcException),
           before=before_log(Aria2WebSocketServer.log, logging.DEBUG))
    async def on_start(self, time_us: int) -> None:  # skipcq: PYL-W0613
        try:
            drive = self.bot.plugins["GoogleDrive"]
        except KeyError:
            self.log.warning(f"{self.name} needs GoogleDrive module loaded")
            self.bot.unload_plugin(self)
            return

        try:
            self._ws = await Aria2WebSocketServer.init(self.bot, drive)  # type: ignore
        except FileNotFoundError:
            self.log.warning("Aria2 package is not installed.")
            self.bot.unload_plugin(self)
            return
        else:
            self.client = await self._ws.start()

    async def on_stop(self) -> None:
        if hasattr(self, "_ws"):
            self._ws.stopping = True
            await self.client.shutdown()
            await self.client.close()
            self._ws.context = None  # type: ignore

    async def _formatSE(self, err: Exception) -> str:
        res = await util.run_sync(ast.literal_eval,
                                  str(err).split(":", 2)[-1].strip())
        return "__" + res["error"]["message"] + "__"

    async def addDownload(
        self,types: Union[str, bytes],
        ctx: command.Context,
        mega: bool = False,
        options: Optional[Dict[str, Any]] = None) -> Optional[str]:
        gid = None
        if isinstance(types, str):
            try:
                gid = await self.client.addUri([types], options=options)
            except Aria2rpcException as e:
                return await self._formatSE(e)
        elif isinstance(types, bytes):
            await self.client.addTorrent(str(types, "utf-8"), options=options)
        else:
            self.log.error(f"Unknown types of {type(types)}")
            return f"__Unknown types of {type(types)}__"

        # Save the message but delete first so we don't spam chat with new download
        async with self._ws.lock:
            if self._ws.context is not None and self._ws.context.response is not None:
                await self._ws.context.response.delete()
            self._ws.context = ctx

        if gid:
            if mega:
                self._ws.mega.add(gid)
            return gid
        
        return

    async def pauseDownload(self, gid: str) -> Dict[str, Any]:
        return await self.client.pause(gid)

    async def removeDownload(self, gid: str) -> Dict[str, Any]:
        return await self.client.remove(gid)

    async def cancelMirror(self, gid: str) -> Optional[str]:
        try:
            res = await self.client.tellStatus(gid, ["status", "followedBy"])
        except Aria2rpcException as e:
            res = await self._formatSE(e)
            if gid in res:
                res = res.replace(gid, f"'{gid}'")
            return res

        status = res["status"]
        metadata = bool(res.get("followedBy"))
        ret = None
        if status == "active":
            await self.client.forcePause(gid)
            await self.client.forceRemove(gid)
        elif status == "complete" and metadata is True:
            return "__GID belongs to finished Metadata, can't be abort.__"

        self._ws.cancelled.add(gid)
        return ret
