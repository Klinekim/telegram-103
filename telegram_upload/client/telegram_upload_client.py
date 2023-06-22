import asyncio
import hashlib
import os
import time
from typing import Iterable, Optional

import click
from telethon import TelegramClient, utils, helpers, custom
from telethon.crypto import AES
from telethon.errors import RPCError, FloodWaitError, InvalidBufferError
from telethon.tl import types, functions, TLRequest
from telethon.utils import pack_bot_file_id

from telegram_upload.client.progress_bar import get_progress_bar
from telegram_upload.exceptions import TelegramUploadDataLoss, MissingFileError
from telegram_upload.upload_files import File
from telegram_upload.utils import grouper, async_to_sync, get_environment_integer

PARALLEL_UPLOAD_BLOCKS = get_environment_integer('TELEGRAM_UPLOAD_PARALLEL_UPLOAD_BLOCKS', 4)
ALBUM_FILES = 10
RETRIES = 3
MAX_RECONNECT_RETRIES = get_environment_integer('TELEGRAM_UPLOAD_MAX_RECONNECT_RETRIES', 5)
RECONNECT_TIMEOUT = get_environment_integer('TELEGRAM_UPLOAD_RECONNECT_TIMEOUT', 5)
MIN_RECONNECT_WAIT = get_environment_integer('TELEGRAM_UPLOAD_MIN_RECONNECT_WAIT', 2)


class TelegramUploadClient(TelegramClient):
    parallel_upload_blocks = PARALLEL_UPLOAD_BLOCKS

    def __init__(self, *args, **kwargs):
        self.reconnecting_lock = asyncio.Lock()
        self.upload_semaphore = asyncio.Semaphore(self.parallel_upload_blocks)
        super().__init__(*args, **kwargs)

    def forward_to(self, message, destinations):
        for destination in destinations:
            self.forward_messages(destination, [message])

    async def _send_album_media(self, entity, media):
        entity = await self.get_input_entity(entity)
        request = functions.messages.SendMultiMediaRequest(
            entity, reply_to_msg_id=None, multi_media=media,
            silent=None, schedule_date=None, clear_draft=None
        )
        result = await self(request)

        random_ids = [m.random_id for m in media]
        return self._get_response_message(random_ids, result, entity)

    def send_files_as_album(self, entity, files, delete_on_success=False, print_file_id=False,
                            forward=()):
        for files_group in grouper(ALBUM_FILES, files):
            media = self.send_files(entity, files_group, delete_on_success, print_file_id, forward, send_as_media=True)
            async_to_sync(self._send_album_media(entity, media))

    def _send_file_message(self, entity, file, thumb, progress):
        message = self.send_file(entity, file, thumb=thumb,
                                 file_size=file.file_size if isinstance(file, File) else None,
                                 caption=file.file_caption, force_document=file.force_file,
                                 progress_callback=progress, attributes=file.file_attributes)
        if hasattr(message.media, 'document') and file.file_size != message.media.document.size:
            raise TelegramUploadDataLoss(
                'Remote document size: {} bytes (local file size: {} bytes)'.format(
                    message.media.document.size, file.file_size))
        return message

    async def _send_media(self, entity, file: File, progress):
        entity = await self.get_input_entity(entity)
        supports_streaming = False  # TODO
        fh, fm, _ = await self._file_to_media(
            file, supports_streaming=file, progress_callback=progress)
        if isinstance(fm, types.InputMediaUploadedPhoto):
            r = await self(functions.messages.UploadMediaRequest(
                entity, media=fm
            ))

            fm = utils.get_input_media(r.photo)
        elif isinstance(fm, types.InputMediaUploadedDocument):
            r = await self(functions.messages.UploadMediaRequest(
                entity, media=fm
            ))

            fm = utils.get_input_media(
                r.document, supports_streaming=supports_streaming)

        return types.InputSingleMedia(
            fm,
            message=file.short_name,
            entities=None,
            # random_id is autogenerated
        )

    def send_one_file(self, entity, file: File, send_as_media: bool = False, thumb: Optional[str] = None,
                      retries=RETRIES):
        message = None
        progress, bar = get_progress_bar('Uploading', file.file_name, file.file_size)

        try:
            try:
                # TODO: remove distinction?
                if send_as_media:
                    message = async_to_sync(self._send_media(entity, file, progress))
                else:
                    message = self._send_file_message(entity, file, thumb, progress)
            finally:
                bar.render_finish()
        except FloodWaitError as e:
            click.echo(f'{e}. Waiting for {e.seconds} seconds.', err=True)
            time.sleep(e.seconds)
            message = self.send_one_file(entity, file, send_as_media, thumb, retries)
        except RPCError as e:
            if retries > 0:
                click.echo(f'The file "{file.file_name}" could not be uploaded: {e}. Retrying...', err=True)
                message = self.send_one_file(entity, file, send_as_media, thumb, retries - 1)
            else:
                click.echo(f'The file "{file.file_name}" could not be uploaded: {e}. It will not be retried.', err=True)
        return message

    def send_files(self, entity, files: Iterable[File], delete_on_success=False, print_file_id=False,
                   forward=(), send_as_media: bool = False):
        has_files = False
        messages = []
        for file in files:
            has_files = True
            thumb = file.get_thumbnail()
            try:
                message = self.send_one_file(entity, file, send_as_media)
            finally:
                if thumb and not file.is_custom_thumbnail and os.path.lexists(thumb):
                    os.remove(thumb)
            if message is None:
                click.echo('Failed to upload file "{}"'.format(file.file_name), err=True)
            if message and print_file_id:
                click.echo('Uploaded successfully "{}" (file_id {})'.format(file.file_name,
                                                                            pack_bot_file_id(message.media)))
            if message and delete_on_success:
                click.echo('Deleting "{}"'.format(file))
                os.remove(file.path)
            if message:
                self.forward_to(message, forward)
        if not has_files:
            raise MissingFileError('Files do not exist.')
        return messages

    async def upload_file(
            self: 'TelegramClient',
            file: 'hints.FileLike',
            *,
            part_size_kb: float = None,
            file_size: int = None,
            file_name: str = None,
            use_cache: type = None,
            key: bytes = None,
            iv: bytes = None,
            progress_callback: 'hints.ProgressCallback' = None) -> 'types.TypeInputFile':
        """
        Uploads a file to Telegram's servers, without sending it.

        .. note::

            Generally, you want to use `send_file` instead.

        This method returns a handle (an instance of :tl:`InputFile` or
        :tl:`InputFileBig`, as required) which can be later used before
        it expires (they are usable during less than a day).

        Uploading a file will simply return a "handle" to the file stored
        remotely in the Telegram servers, which can be later used on. This
        will **not** upload the file to your own chat or any chat at all.

        Arguments
            file (`str` | `bytes` | `file`):
                The path of the file, byte array, or stream that will be sent.
                Note that if a byte array or a stream is given, a filename
                or its type won't be inferred, and it will be sent as an
                "unnamed application/octet-stream".

            part_size_kb (`int`, optional):
                Chunk size when uploading files. The larger, the less
                requests will be made (up to 512KB maximum).

            file_size (`int`, optional):
                The size of the file to be uploaded, which will be determined
                automatically if not specified.

                If the file size can't be determined beforehand, the entire
                file will be read in-memory to find out how large it is.

            file_name (`str`, optional):
                The file name which will be used on the resulting InputFile.
                If not specified, the name will be taken from the ``file``
                and if this is not a `str`, it will be ``"unnamed"``.

            use_cache (`type`, optional):
                This parameter currently does nothing, but is kept for
                backward-compatibility (and it may get its use back in
                the future).

            key ('bytes', optional):
                In case of an encrypted upload (secret chats) a key is supplied

            iv ('bytes', optional):
                In case of an encrypted upload (secret chats) an iv is supplied

            progress_callback (`callable`, optional):
                A callback function accepting two parameters:
                ``(sent bytes, total)``.

                When sending an album, the callback will receive a number
                between 0 and the amount of files as the "sent" parameter,
                and the amount of files as the "total". Note that the first
                parameter will be a floating point number to indicate progress
                within a file (e.g. ``2.5`` means it has sent 50% of the third
                file, because it's between 2 and 3).

        Returns
            :tl:`InputFileBig` if the file size is larger than 10MB,
            `InputSizedFile <telethon.tl.custom.inputsizedfile.InputSizedFile>`
            (subclass of :tl:`InputFile`) otherwise.

        Example
            .. code-block:: python

                # Photos as photo and document
                file = await client.upload_file('photo.jpg')
                await client.send_file(chat, file)                       # sends as photo
                await client.send_file(chat, file, force_document=True)  # sends as document

                file.name = 'not a photo.jpg'
                await client.send_file(chat, file, force_document=True)  # document, new name

                # As song or as voice note
                file = await client.upload_file('song.ogg')
                await client.send_file(chat, file)                   # sends as song
                await client.send_file(chat, file, voice_note=True)  # sends as voice note
        """
        if isinstance(file, (types.InputFile, types.InputFileBig)):
            return file  # Already uploaded

        async with helpers._FileStream(file, file_size=file_size) as stream:
            # Opening the stream will determine the correct file size
            file_size = stream.file_size

            if not part_size_kb:
                part_size_kb = utils.get_appropriated_part_size(file_size)

            if part_size_kb > 512:
                raise ValueError('The part size must be less or equal to 512KB')

            part_size = int(part_size_kb * 1024)
            if part_size % 1024 != 0:
                raise ValueError(
                    'The part size must be evenly divisible by 1024')

            # Set a default file name if None was specified
            file_id = helpers.generate_random_long()
            if not file_name:
                file_name = stream.name or str(file_id)

            # If the file name lacks extension, add it if possible.
            # Else Telegram complains with `PHOTO_EXT_INVALID_ERROR`
            # even if the uploaded image is indeed a photo.
            if not os.path.splitext(file_name)[-1]:
                file_name += utils._get_extension(stream)

            # Determine whether the file is too big (over 10MB) or not
            # Telegram does make a distinction between smaller or larger files
            is_big = file_size > 10 * 1024 * 1024
            hash_md5 = hashlib.md5()

            part_count = (file_size + part_size - 1) // part_size
            self._log[__name__].info('Uploading file of %d bytes in %d chunks of %d',
                                    file_size, part_count, part_size)

            pos = 0
            for part_index in range(part_count):
                # Read the file by in chunks of size part_size
                part = await helpers._maybe_await(stream.read(part_size))

                if not isinstance(part, bytes):
                    raise TypeError(
                        'file descriptor returned {}, not bytes (you must '
                        'open the file in bytes mode)'.format(type(part)))

                # `file_size` could be wrong in which case `part` may not be
                # `part_size` before reaching the end.
                if len(part) != part_size and part_index < part_count - 1:
                    raise ValueError(
                        'read less than {} before reaching the end; either '
                        '`file_size` or `read` are wrong'.format(part_size))

                pos += len(part)

                # Encryption part if needed
                if key and iv:
                    part = AES.encrypt_ige(part, key, iv)

                if not is_big:
                    # Bit odd that MD5 is only needed for small files and not
                    # big ones with more chance for corruption, but that's
                    # what Telegram wants.
                    hash_md5.update(part)

                # The SavePartRequest is different depending on whether
                # the file is too large or not (over or less than 10MB)
                if is_big:
                    request = functions.upload.SaveBigFilePartRequest(
                        file_id, part_index, part_count, part)
                else:
                    request = functions.upload.SaveFilePartRequest(
                        file_id, part_index, part)
                await self.upload_semaphore.acquire()
                self.loop.create_task(
                    self._send_file_part(request, part_index, part_count, pos, file_size, progress_callback),
                    name=f"telegram-upload-file-{part_index}"
                )
            # Wait for all tasks to finish
            await asyncio.wait([
                task for task in asyncio.all_tasks() if task.get_name().startswith(f"telegram-upload-file-")
            ])
        if is_big:
            return types.InputFileBig(file_id, part_count, file_name)
        else:
            return custom.InputSizedFile(
                file_id, part_count, file_name, md5=hash_md5, size=file_size
            )

    # endregion

    async def _send_file_part(self, request: TLRequest, part_index: int, part_count: int, pos: int, file_size: int,
                              progress_callback: Optional['hints.ProgressCallback'] = None, retry: int = 0) -> None:
        """
        Submit the file request part to Telegram. This method waits for the request to be executed, logs the upload,
        and releases the semaphore to allow further uploading.

        :param request: SaveBigFilePartRequest or SaveFilePartRequest. This request will be awaited.
        :param part_index: Part index as integer. Used in logging.
        :param part_count: Total parts count as integer. Used in logging.
        :param pos: Number of part as integer. Used for progress bar.
        :param file_size: Total file size. Used for progress bar.
        :param progress_callback: Callback to use after submit the request. Optional.
        :return: None
        """
        result = None
        try:
            result = await self(request)
        except InvalidBufferError as e:
            if e.code == 429:
                # Too many connections
                click.echo(f'Too many connections to Telegram servers.', err=True)
            else:
                raise
        except ConnectionError:
            # Retry to send the file part
            click.echo(f'Detected connection error. Retrying...', err=True)
        else:
            self.upload_semaphore.release()
        if result is None and retry < MAX_RECONNECT_RETRIES:
            # An error occurred, retry
            await asyncio.sleep(max(MIN_RECONNECT_WAIT, retry * MIN_RECONNECT_WAIT))
            await self.reconnect()
            await self._send_file_part(
                request, part_index, part_count, pos, file_size, progress_callback, retry + 1
            )
        elif result:
            self._log[__name__].debug('Uploaded %d/%d',
                                      part_index + 1, part_count)
            if progress_callback:
                await helpers._maybe_await(progress_callback(pos, file_size))
        else:
            raise RuntimeError(
                'Failed to upload file part {}.'.format(part_index))

    def decrease_upload_semaphore(self):
        """
        Decreases the upload semaphore by one. This method is used to reduce the number of parallel uploads.
        :return:
        """
        if self.parallel_upload_blocks > 1:
            self.parallel_upload_blocks -= 1
            self.loop.create_task(self.upload_semaphore.acquire())

    async def reconnect(self):
        """
        Reconnects to Telegram servers.

        :return: None
        """
        await self.reconnecting_lock.acquire()
        if self.is_connected():
            # Reconnected in another task
            self.reconnecting_lock.release()
            return
        self.decrease_upload_semaphore()
        try:
            click.echo(f'Reconnecting to Telegram servers...')
            await asyncio.wait_for(self.connect(), RECONNECT_TIMEOUT)
            click.echo(f'Reconnected to Telegram servers.')
        except InvalidBufferError:
            click.echo(f'InvalidBufferError connecting to Telegram servers.', err=True)
        except asyncio.TimeoutError:
            click.echo(f'Timeout connecting to Telegram servers.', err=True)
        finally:
            self.reconnecting_lock.release()
