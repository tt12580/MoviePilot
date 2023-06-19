import re
from pathlib import Path
from typing import List, Optional, Union

from app.chain import ChainBase
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent
from app.schemas.types import TorrentStatus, EventType, MediaType
from app.utils.string import StringUtils


class TransferChain(ChainBase):
    """
    文件转移处理链
    """

    def __init__(self):
        super().__init__()
        self.downloadhis = DownloadHistoryOper()
        self.transferhis = TransferHistoryOper()

    def process(self, arg_str: str = None, userid: Union[str, int] = None) -> bool:
        """
        获取下载器中的种子列表，并执行转移
        """

        def extract_hash_and_number(string: str):
            """
            从字符串中提取种子hash和编号
            """
            pattern = r'([a-fA-F0-9]+) (\d+)'
            match = re.search(pattern, string)
            if match:
                hash_value = match.group(1)
                number = match.group(2)
                return hash_value, int(number)
            else:
                return None, None

        if arg_str:
            logger.info(f"开始转移下载器文件，参数：{arg_str}")
            # 解析中种子hash，TMDB ID
            torrent_hash, tmdbid = extract_hash_and_number(arg_str)
            if not hash or not tmdbid:
                logger.error(f"参数错误，参数：{arg_str}")
                return False
            # 获取种子
            torrents: Optional[List[TransferTorrent]] = self.list_torrents(hashs=torrent_hash)
            if not torrents:
                logger.error(f"没有获取到种子，参数：{arg_str}")
                return False
            # 查询媒体信息
            arg_mediainfo = self.recognize_media(tmdbid=tmdbid)
        else:
            arg_mediainfo = None
            logger.info("开始执行下载器文件转移 ...")
            # 从下载器获取种子列表
            torrents: Optional[List[TransferTorrent]] = self.list_torrents(status=TorrentStatus.TRANSFER)
            if not torrents:
                logger.info("没有获取到已完成的下载任务")
                return False

        logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")
        # 识别
        for torrent in torrents:
            # 识别前预处理
            result: Optional[tuple] = self.prepare_recognize(title=torrent.title)
            if result:
                title, subtitle = result
            else:
                title, subtitle = torrent.title, None
            # 识别元数据
            meta: MetaBase = MetaInfo(title=title, subtitle=subtitle)
            if not meta.name:
                logger.warn(f'未识别到元数据，标题：{title}')
                continue
            if not arg_mediainfo:
                # 查询下载记录识别情况
                downloadhis: DownloadHistory = self.downloadhis.get_by_hash(torrent.hash)
                if downloadhis:
                    mtype = MediaType.TV if downloadhis.type == MediaType.TV.value else MediaType.MOVIE
                    mediainfo = self.recognize_media(mtype=mtype, tmdbid=downloadhis.tmdbid)
                else:
                    # 使用标题识别媒体信息
                    mediainfo: MediaInfo = self.recognize_media(meta=meta)
                if not mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{torrent.title}')
                    self.post_message(title=f"{torrent.title} 未识别到媒体信息，无法入库！\n"
                                            f"回复：```\n/transfer {torrent.hash} [tmdbid]\n``` 手动识别转移。",
                                      userid=userid)
                    continue
            else:
                mediainfo = arg_mediainfo
            logger.info(f"{torrent.title} 识别为：{mediainfo.type.value} {mediainfo.title_year}")
            # 更新媒体图片
            self.obtain_image(mediainfo=mediainfo)
            # 转移
            transferinfo: TransferInfo = self.transfer(mediainfo=mediainfo, path=torrent.path)
            if not transferinfo or not transferinfo.target_path:
                # 转移失败
                logger.warn(f"{torrent.title} 入库失败")
                self.post_message(
                    title=f"{mediainfo.title_year}{meta.season_episode} 入库失败！",
                    text=f"原因：{transferinfo.message if transferinfo else '未知'}",
                    image=mediainfo.get_message_image(),
                    userid=userid
                ),
                continue
            # 新增转移历史记录
            self.transferhis.add(
                src=str(torrent.path),
                dest=str(transferinfo.target_path),
                mode=settings.TRANSFER_TYPE,
                type=mediainfo.type.value,
                category=mediainfo.category,
                title=mediainfo.title,
                year=mediainfo.year,
                tmdbid=mediainfo.tmdb_id,
                imdbid=mediainfo.imdb_id,
                tvdbid=mediainfo.tvdb_id,
                doubanid=mediainfo.douban_id,
                seasons=meta.season,
                episodes=meta.episode,
                image=mediainfo.get_poster_image(),
                download_hash=torrent.hash
            )
            # 转移完成
            self.transfer_completed(hashs=torrent.hash, transinfo=transferinfo)
            # 刮剥
            self.scrape_metadata(path=transferinfo.target_path, mediainfo=mediainfo)
            # 刷新媒体库
            self.refresh_mediaserver(mediainfo=mediainfo, file_path=transferinfo.target_path)
            # 发送通知
            self.__send_transfer_message(meta=meta, mediainfo=mediainfo, transferinfo=transferinfo)
            # 广播事件
            self.eventmanager.send_event(EventType.TransferComplete, {
                'meta': meta,
                'mediainfo': mediainfo,
                'transferinfo': transferinfo
            })

        logger.info("下载器文件转移执行完成")
        return True

    def __send_transfer_message(self, meta: MetaBase, mediainfo: MediaInfo, transferinfo: TransferInfo):
        """
        发送入库成功的消息
        """
        msg_title = f"{mediainfo.title_year} 已入库"
        if mediainfo.vote_average:
            msg_str = f"评分：{mediainfo.vote_average}，类型：{mediainfo.type.value}"
        else:
            msg_str = f"类型：{mediainfo.type.value}"
        if mediainfo.category:
            msg_str = f"{msg_str}，类别：{mediainfo.category}"
        if meta.resource_term:
            msg_str = f"{msg_str}，质量：{meta.resource_term}"
        msg_str = f"{msg_str}，共{transferinfo.file_count}个文件，" \
                  f"大小：{StringUtils.str_filesize(transferinfo.total_size)}"
        if transferinfo.message:
            msg_str = f"{msg_str}，以下文件处理失败：\n{transferinfo.message}"
        # 发送
        self.post_message(title=msg_title, text=msg_str, image=mediainfo.get_message_image())
