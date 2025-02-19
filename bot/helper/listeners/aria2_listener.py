from asyncio import sleep
from time import time
from aiofiles.os import remove as aioremove, path as aiopath

from bot import aria2, task_dict_lock, task_dict, LOGGER, config_dict
from bot.helper.mirror_utils.gdrive_utils.search import gdSearch
from bot.helper.mirror_utils.status_utils.aria2_status import Aria2Status
from bot.helper.ext_utils.files_utils import get_base_name, clean_unwanted
from bot.helper.ext_utils.bot_utils import (
    new_thread,
    bt_selection_buttons,
    sync_to_async,
    get_telegraph_list,
)
from bot.helper.telegram_helper.message_utils import (
    sendMessage,
    deleteMessage,
    update_status_message,
)
from bot.helper.ext_utils.status_utils import getTaskByGid
from bot.helper.ext_utils.links_utils import is_gdrive_id


@new_thread
async def _onDownloadStarted(api, gid):
    download = await sync_to_async(api.get_download, gid)
    if download.options.follow_torrent == "false":
        return
    if download.is_metadata:
        LOGGER.info(f"onDownloadStarted: {gid} METADATA")
        await sleep(1)
        if task := await getTaskByGid(gid):
            if task.listener.select:
                metamsg = "Downloading Metadata, wait then you can select files. Use torrent file to avoid this wait."
                meta = await sendMessage(task.listener.message, metamsg)
                while True:
                    await sleep(0.5)
                    if download.is_removed or download.followed_by_ids:
                        await deleteMessage(meta)
                        break
                    download = download.live
        return
    else:
        LOGGER.info(f"onDownloadStarted: {download.name} - Gid: {gid}")
        await sleep(1)

    if task := await getTaskByGid(gid):
        if (
            task.listener.upDest.startswith("mtp:")
            and task.listener.user_dict("stop_duplicate", False)
            or not task.listener.upDest.startswith("mtp:")
            and config_dict["STOP_DUPLICATE"]
        ):
            if (
                task.listener.isLeech
                or task.listener.select
                or not is_gdrive_id(task.listener.upDest)
            ):
                return
            download = await sync_to_async(api.get_download, gid)
            if not download.is_torrent:
                await sleep(2)
                download = download.live
            LOGGER.info("Checking File/Folder if already in Drive...")
            name = download.name
            if task.listener.compress:
                name = f"{name}.zip"
            elif task.listener.extract:
                try:
                    name = get_base_name(name)
                except:
                    name = None
            if name is not None:
                telegraph_content, contents_no = await sync_to_async(
                    gdSearch(stopDup=True).drive_list,
                    name,
                    task.listener.upDest,
                    task.listener.user_id,
                )
                if telegraph_content:
                    msg = f"File/Folder is already available in Drive.\nHere are {contents_no} list results:"
                    button = await get_telegraph_list(telegraph_content)
                    await task.listener.onDownloadError(msg, button)
                    await sync_to_async(api.remove, [download], force=True, files=True)


@new_thread
async def _onDownloadComplete(api, gid):
    try:
        download = await sync_to_async(api.get_download, gid)
    except:
        return
    if download.options.follow_torrent == "false":
        return
    if download.followed_by_ids:
        new_gid = download.followed_by_ids[0]
        LOGGER.info(f"Gid changed from {gid} to {new_gid}")
        if task := await getTaskByGid(new_gid):
            if config_dict["BASE_URL"] and task.listener.select:
                if not task.queued:
                    await sync_to_async(api.client.force_pause, new_gid)
                SBUTTONS = bt_selection_buttons(new_gid)
                msg = "Your download paused. Choose files then press Done Selecting button to start downloading."
                await sendMessage(task.listener.message, msg, SBUTTONS)
    elif download.is_torrent:
        if task := await getTaskByGid(gid):
            if hasattr(task, "seeding") and task.seeding:
                LOGGER.info(f"Cancelling Seed: {download.name} onDownloadComplete")
                await task.listener.onUploadError(
                    f"Seeding stopped with Ratio: {task.ratio()} and Time: {task.seeding_time()}"
                )
                await sync_to_async(api.remove, [download], force=True, files=True)
    else:
        LOGGER.info(f"onDownloadComplete: {download.name} - Gid: {gid}")
        if task := await getTaskByGid(gid):
            await task.listener.onDownloadComplete()
            await sync_to_async(api.remove, [download], force=True, files=True)


@new_thread
async def _onBtDownloadComplete(api, gid):
    seed_start_time = time()
    await sleep(1)
    download = await sync_to_async(api.get_download, gid)
    if download.options.follow_torrent == "false":
        return
    LOGGER.info(f"onBtDownloadComplete: {download.name} - Gid: {gid}")
    if task := await getTaskByGid(gid):
        if task.listener.select:
            res = download.files
            for file_o in res:
                f_path = file_o.path
                if not file_o.selected and await aiopath.exists(f_path):
                    try:
                        await aioremove(f_path)
                    except:
                        pass
            await clean_unwanted(download.dir)
        if task.listener.seed:
            try:
                await sync_to_async(
                    api.set_options, {"max-upload-limit": "0"}, [download]
                )
            except Exception as e:
                LOGGER.error(
                    f"{e} You are not able to seed because you added global option seed-time=0 without adding specific seed_time for this torrent GID: {gid}"
                )
        else:
            try:
                await sync_to_async(api.client.force_pause, gid)
            except Exception as e:
                LOGGER.error(f"{e} GID: {gid}")
        await task.listener.onDownloadComplete()
        download = download.live
        if task.listener.seed:
            if download.is_complete:
                if task := await getTaskByGid(gid):
                    LOGGER.info(f"Cancelling Seed: {download.name}")
                    await task.listener.onUploadError(
                        f"Seeding stopped with Ratio: {task.ratio()} and Time: {task.seeding_time()}"
                    )
                    await sync_to_async(api.remove, [download], force=True, files=True)
            else:
                async with task_dict_lock:
                    if task.listener.mid not in task_dict:
                        await sync_to_async(
                            api.remove, [download], force=True, files=True
                        )
                        return
                    task_dict[task.listener.mid] = Aria2Status(task.listener, gid, True)
                    task_dict[task.listener.mid].start_time = seed_start_time
                LOGGER.info(f"Seeding started: {download.name} - Gid: {gid}")
                await update_status_message(task.listener.message.chat.id)
        else:
            await sync_to_async(api.remove, [download], force=True, files=True)


@new_thread
async def _onDownloadStopped(api, gid):
    await sleep(6)
    if task := await getTaskByGid(gid):
        await task.listener.onDownloadError("Dead torrent!")


@new_thread
async def _onDownloadError(api, gid):
    LOGGER.info(f"onDownloadError: {gid}")
    error = "None"
    try:
        download = await sync_to_async(api.get_download, gid)
        if download.options.follow_torrent == "false":
            return
        error = download.error_message
        LOGGER.info(f"Download Error: {error}")
    except:
        pass
    if task := await getTaskByGid(gid):
        await task.listener.onDownloadError(error)


def start_aria2_listener():
    aria2.listen_to_notifications(
        threaded=False,
        on_download_start=_onDownloadStarted,
        on_download_error=_onDownloadError,
        on_download_stop=_onDownloadStopped,
        on_download_complete=_onDownloadComplete,
        on_bt_download_complete=_onBtDownloadComplete,
        timeout=60,
    )
