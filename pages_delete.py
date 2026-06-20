
import os
import asyncio
from datetime import datetime
import json
import logging
import pickle
import re
import sys
import time
import traceback
from typing import Any, Callable
from bs4 import BeautifulSoup
import httpx
import wikidot
from wikidot.common import exceptions
from wikidot.module.site import Site
from wikidot.util.parser import odate as odate_parser
from wikidot.util.parser import user as user_parser
import yaml
from httpx import ConnectError, ConnectTimeout

if not os.path.exists("logs"):
    os.makedirs("logs")

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
file_handler = logging.FileHandler(
    f"logs/{datetime.now().strftime('%Y-%m-%d')}.txt")
console_handler.setLevel(logging.INFO)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

try:
    with open("deleted_pages.pkl", "rb") as file:
        pending_pages: dict[int, list] = pickle.load(file)
except FileNotFoundError:
    pending_pages = {}
logger.info(f'载入历史数据：{pending_pages}')

with open("config.yaml", "r", encoding="utf-8") as f:
    config: dict = yaml.safe_load(f)

deviant: list[dict] = []
staff_unix_names: list[str] = config["staffs"]
pending_delete_pages: list[dict] = []
pending_check_pages: list[dict] = []
js_result: list[dict] = []


async def single_request(site: Site, _body: dict[str, Any]):
    amc = site.client.amc_client
    client = httpx.AsyncClient()
    url = (
        f'http{"s" if site.ssl_supported else ""}://{site.unix_name}.wikidot.com/'
        f"ajax-module-connector.php"
    )
    _body["wikidot_token7"] = 123456
    return await client.post(
        url,
        headers=amc.header.get_header(),
        data=_body,
        timeout=amc.config.request_timeout,
    )


def Retry(retry_text: str | None = None, last_text: str | None = None, times: int = 3, ifRaise: bool = False):
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            for i in range(times):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if retry_text is not None:
                        logger.warning(retry_text)
                    if i == times - 1:
                        if last_text is not None:
                            logger.error(last_text)
                        if ifRaise:
                            raise e
        return wrapper
    return decorator

wd = wikidot.Client(username=config["username"], password=config["password"])
site = wd.site.get(config["siteUnixName"])


@Retry(last_text="放弃重试，跳过修改")
def edit_post(thread_id: int, post_id: int, title: str | None = None, source: str | None = None):
    if title is None and source is None:
        logger.info("标题与源代码为空，放弃修改")
        return

    response = asyncio.run(
        single_request(
            site,
            {
                "postId": post_id,
                "threadId": thread_id,
                "moduleName": "forum/sub/ForumEditPostFormModule"
            }
        )
    )

    error_dict = {
        "threadId": thread_id,
        "postId": post_id,
        "title": title,
        "source": source,
        "errorType": "edit_post_unknown",
    }
    status = response.json()["status"]
    if status == "no_permission":
        error_dict["errorType"] = "edit_post_permission"
        deviant.append(error_dict)
        logger.warning("缺少编辑权限，跳过修改")
        return
    elif status == "ok":
        if error_dict in deviant:
            deviant.remove(error_dict)
    else:
        logger.warning(f"编辑失败，状态为{status}，准备重试")
        if error_dict not in deviant:
            deviant.append(error_dict)
        raise exceptions.WikidotStatusCodeException(status_code=status)

    html = BeautifulSoup(response.json()["body"], "lxml")
    current_id = int(html.select(
        "form#edit-post-form>input")[1].get("value"))
    current_title = html.select_one("input#np-title").get("value")
    current_source = html.select_one("textarea#np-text").get_text()

    if current_title == title and current_source == source:
        logger.info("标题与源代码和原帖相同，放弃修改")
        return
    if title is None:
        title = current_title
    if source is None:
        source = current_source

    response = site.amc_request(
        [
            {
                "postId": post_id,
                "currentRevisionId": current_id,
                "title": title,
                "source": source,
                "action": "ForumAction",
                "event": "saveEditPost",
                "moduleName": "Empty"
            }
        ]
    )[0]


@Retry(last_text="放弃重试，跳过创建")
def new_post(thread_id: int, title: str = "", source: str = "", parent_id: int = ""):
    if source == "":
        logger.info("源代码为空，放弃创建")
        return

    response = asyncio.run(
        single_request(
            site,
            {
                "threadId": thread_id,
                "parentId": parent_id,
                "title": title,
                "source": source,
                "action": "ForumAction",
                "event": "savePost",
                "moduleName": "Empty"
            }
        )
    )

    error_dict = {
        "threadId": thread_id,
        "title": title,
        "source": source,
        "errorType": "new_post_unknown",
    }
    status = response.json()["status"]
    if status == "no_permission":
        error_dict["errorType"] = "new_post_permission"
        deviant.append(error_dict)
        logger.warning("缺少编辑权限，跳过创建")
    elif status == "ok":
        if error_dict in deviant:
            deviant.remove(error_dict)
    else:
        logger.warning(f"编辑失败，状态为{status}，准备重试")
        if error_dict not in deviant:
            deviant.append(error_dict)
        raise exceptions.WikidotStatusCodeException(status_code=status)


@Retry(last_text="放弃重试，跳过修改")
def edit_tags(page_id: int, tags: str):
    response = site.amc_request(
        [
            {
                "tags": tags,
                "pageId": page_id,
                "action": "WikiPageAction",
                "event": "saveTags",
                "moduleName": "Empty"
            }
        ]
    )[0]

    error_dict = {
        "pageId": page_id,
        "tags": tags,
        "errorType": "edit_tags_unknown",
    }
    status = response.json()["status"]
    if status == "no_permission":
        error_dict["errorType"] = "edit_tags_permission"
        deviant.append(error_dict)
        logger.warning("缺少编辑权限，跳过创建")
    elif status == "ok":
        if error_dict in deviant:
            deviant.remove(error_dict)
    else:
        logger.warning(f"编辑失败，状态为{status}，准备重试")
        if error_dict not in deviant:
            deviant.append(error_dict)
        raise exceptions.WikidotStatusCodeException(status_code=status)


def build_delete_announce(score: int, timer: float) -> str:
    """构建删除宣告内容"""
    timestamp_ms = int(timer * 1000)
    return f"""此文章目前为 {score} 分，现依据[[[deletions-guide|删除指导]]]宣告将删除此页面：

[[iframe https://arandintday.github.io/thebackhubscn/Deletion%20Time%20Tool/Deletion%20Time%20Tool.html?timestamp={timestamp_ms}&type=0 style="width: 400px; height: 60px;"]]

请本文章作者尽快进行修改内容提高质量。
如果该页面作者无法及时做出更改，其他人也可以在确认后向管理组申请重写。"""


def get_posts(thread_id: int) -> list[dict]:
    response = site.amc_request(
        [
            {
                "t": thread_id,
                "moduleName": "forum/ForumViewThreadModule"
            }
        ]
    )[0]
    
    html = BeautifulSoup(response.json()["body"], "lxml")
    if (pagerno := html.select_one("span.pager-no")) is None:
        pagers = 1
    else:
        pagers = int(re.search(r"of (\d+)", pagerno.text).group(1))

    responses = site.amc_request(
        [
            {
                "pageNo": no + 1,
                "t": thread_id,
                "order": "",
                "moduleName": "forum/ForumViewThreadPostsModule",
            }
            for no in range(pagers)
            ]
        )

    posts = []

    for response in responses:
        html = BeautifulSoup(response.json()["body"], "lxml")
        for post in html.select("div.post"):
            cuser = post.select_one("div.info span.printuser")
            codate = post.select_one("div.info span.odate")
            if (parent := post.parent.get("id")) != "thread-container-posts":
                parent_id = int(re.search(r"fpc-(\d+)", parent).group(1))
            else:
                parent_id = ""

            posts.append({
                "id" : int(re.search(r"post-(\d+)", post.get("id")).group(1)),
                "thread_id" : thread_id,
                "title" : post.select_one("div.title").text.strip(),
                "parent_id" : parent_id,
                "created_by" : user_parser(wd, cuser),
                "created_at" : odate_parser(codate),
                "source_ele" : post.select_one("div.content")
            })

    return posts


@Retry(ifRaise=True)
def get_discuss_id(page_id: int) -> int:
    response = site.amc_request(
        [
            {
                "page_id": page_id,
                "action": "ForumAction",
                "event": "createPageDiscussionThread",
                "moduleName": "Empty"
            }
        ]
    )[0]

    return int(response.json()["thread_id"])


def find_staff_post(posts: list[dict]) -> dict:
    for post in posts:
        title = post["title"]
        user = post["created_by"].name
        if "职员帖" in title and "删除宣告" in title and user in staff_unix_names:
            return post


# ====== 新增：判断页面是否含"新手专区"标签 ======
def is_newbie_zone(tags: list[str]) -> bool:
    """判断页面是否属于新手专区"""
    return "新手专区" in tags


def get_delete_hours(score: int, tags: list[str] | None = None) -> int:
    """根据分数决定删除倒计时小时数，新手专区标签的页面删除线放宽为-6"""
    if tags is None:
        tags = []
    if is_newbie_zone(tags):
        # 新手专区：仅当分数 <= -6 时删除，给 72 小时
        if score <= -6:
            return 72
        else:
            return 0
    else:
        # 普通页面：原有逻辑不变
        if score <= -6:
            return 24
        elif score <= -2:
            return 72
        else:
            return 0


def get_cancel_threshold(tags: list[str] | None = None) -> int:
    """获取分数回升取消删除的阈值，新手专区为-6，普通为-2"""
    if tags is None:
        tags = []
    if is_newbie_zone(tags):
        return -6
    else:
        return -2


@Retry(ifRaise=True)
def check_original_pages():
    """检查文章页面，根据评分发布删除宣告"""
    pages = site.pages.search(
        category="-archived -setting -space -system -topic",
        tags="-已归档 -旧页面 -管理 -作者 -待删除 -重写中 -_低分删除豁免 原创 搬运 _test -功能 -中心 -组件后端 -组件 -版式",
        rating="<=0"
    )

    current_time = time.time()

    for page in pages:
        score = page.rating
        tags = page.tags
        hours = get_delete_hours(score, tags)
        if hours == 0:
            logger.info(f"页面 {page.fullname} 分数为{score}，不满足删除条件，跳过")
            continue

        # 清除 pending_pages 中该页面的旧数据
        if page.id in pending_pages:
            logger.info(f"清除 {page.fullname} 的旧 pending 数据")
            del pending_pages[page.id]

        discuss_id = get_discuss_id(page.id)
        deletion_post = find_staff_post(get_posts(discuss_id))
        timer_timestamp = current_time + hours * 3600
        post_source = build_delete_announce(score, timer_timestamp)

        if deletion_post is None:
            new_post(discuss_id, "【职员帖】删除宣告", post_source)
        else:
            edit_post(discuss_id, deletion_post["id"], source=post_source)

        # 如果发帖/编辑失败，跳过打标签
        if deviant != [] and deletion_post is not None and deviant[-1].get("postId") == deletion_post["id"]:
            continue

        # 记录 pending 信息
        pending_pages[page.id] = [score, timer_timestamp, page.fullname]
        edit_tags(page.id, " ".join(tags) + " 待删除")
        logger.info(f"页面 {page.fullname} 分数 {score}，设置 {hours} 小时删除宣告")


@Retry(ifRaise=True)
def check_pending_pages():
    """
    维护待删除页面状态：
    - pending_pages 为唯一真相源
    - 分数跨档才更新
    - 倒计时最后 6 小时标记为 frozen
    """
    pages = site.pages.search(
        category="-reserve",
        tags="+待删除"
    )

    current_time = time.time()

    def score_bucket(score: int) -> int:
        """根据分数决定删除倒计时小时数"""
        if score <= -6:
            return 24
        elif score <= -2:
            return 72
        else:
            return 0

    for page in pages:
        page_id = page.id
        tags = page.tags

        discuss_id = get_discuss_id(page_id)
        deletion_post = find_staff_post(get_posts(discuss_id))
        score = page.rating

        if deletion_post is None:
            edit_tags(page_id, " ".join(tags).replace("待删除", ""))
            logger.warning(f"页面 {page.fullname} 未找到删除帖，移除待删除标签")
            pending_pages.pop(page_id, None)
            continue

        source_ele = deletion_post["source_ele"]

        # ---------- iframe 时间解析 ----------
        iframe = source_ele.select_one("iframe")
        if iframe is None:
            logger.warning(f"页面 {page.fullname} 未找到 iframe")
            continue

        src = iframe.get("src", "")
        record_timestamp = None

        if "timerdfc.pages.dev" in src:
            m = re.search(r"time=([^&]+)", src)
            if m:
                t = m.group(1).replace("%3A", ":").replace("Z", "+00:00")
                record_timestamp = datetime.fromisoformat(t).timestamp()

        elif "arandintday.github.io" in src:
            m = re.search(r"timestamp=(\d+)", src)
            if m:
                record_timestamp = float(m.group(1)) / 1000

        elif "timer.backroomswiki.cn" in src:
            if ".000Z" in src:
                m = re.search(r"/time=(.*?)\.000Z", src)
                if m:
                    record_timestamp = datetime.fromisoformat(m.group(1)).timestamp()
            else:
                m = re.search(r"/time=(\d+)", src)
                if m:
                    record_timestamp = float(m.group(1)) / 1000

        if record_timestamp is None:
            logger.warning(f"页面 {page.fullname} 无法解析倒计时时间")
            continue

        # ---------- 真相源 ----------
        if page_id in pending_pages:
            announced_score, _, fullname = pending_pages[page_id]
        else:
            announced_score = score
            pending_pages[page_id] = [score, record_timestamp, page.fullname]

        remaining_seconds = record_timestamp - current_time

        # ---------- 冻结期 ----------
        if remaining_seconds < 21600:  # 6 小时
            cancel_threshold = get_cancel_threshold(tags)
            if score > cancel_threshold:
                logger.info(
                    f"页面 {page.fullname} 在冻结期内分数回升至 {score}，取消删除"
                )
                edit_post(
                    discuss_id,
                    deletion_post["id"],
                    source="【分数回升，倒计时终止】"
                )
                edit_tags(page_id, " ".join(tags).replace("待删除", ""))
                pending_pages.pop(page_id, None)
                continue

            # 冻结期仍然记录，但标记为 frozen
            logger.info(
                f"页面 {page.fullname} 处于冻结期（剩余 {int(remaining_seconds)}s）"
            )

            pending_delete_pages.append({
                "link": page.get_url(),
                "title": page.title,
                "score": score,
                "release_score": announced_score,
                "time": round(remaining_seconds / 3600, 1),
                "discuss_link": f"https://{config['siteUnixName']}.wikidot.com/forum/t-{discuss_id}",
                "post_id": deletion_post["id"],
                "timestamp": record_timestamp,
                "status": "frozen",
            })
            continue

        # ---------- 正常分数回升 ----------
        cancel_threshold = get_cancel_threshold(tags)
        if score > cancel_threshold:
            edit_post(
                discuss_id,
                deletion_post["id"],
                source="【分数回升，倒计时停止】"
            )
            pending_pages.pop(page_id, None)
            edit_tags(page_id, " ".join(tags).replace("待删除", ""))
            logger.info(f"页面 {page.fullname} 分数回升至 {score}，取消删除")
            continue

        # ---------- 跨档更新 ----------
        old_bucket = score_bucket(announced_score)
        new_bucket = score_bucket(score)

        if old_bucket != new_bucket and new_bucket > 0:
            new_timestamp = current_time + new_bucket * 3600
            time_diff = abs(record_timestamp - new_timestamp)

            if time_diff > 300:
                logger.info(
                    f"页面 {page.fullname} 跨档更新 "
                    f"({announced_score} → {score})，新倒计时 {new_bucket}h"
                )
                new_source = build_delete_announce(score, new_timestamp)
                edit_post(discuss_id, deletion_post["id"], source=new_source)
                pending_pages[page_id] = [score, new_timestamp, page.fullname]
                record_timestamp = new_timestamp
        else:
            logger.debug(
                f"页面 {page.fullname} 分数未跨档 ({score})，不更新宣告"
            )

        # ---------- 到期 ----------
        if current_time >= record_timestamp:
            logger.info(f"页面 {page.fullname} 倒计时到期，加入删除宣告列表")
            pending_check_pages.append(
                [page.fullname, announced_score, "normal"]
            )
        else:
            pending_delete_pages.append({
                "link": page.get_url(),
                "title": page.title,
                "score": score,
                "release_score": announced_score,
                "time": round((record_timestamp - current_time) / 3600, 1),
                "discuss_link": f"https://{config['siteUnixName']}.wikidot.com/forum/t-{discuss_id}",
                "post_id": deletion_post["id"],
                "timestamp": record_timestamp,
                "status": "normal",
            })


@Retry(ifRaise=True)
def check_deleted_pages():
    """将已删除分类的页面加入宣告列表"""
    pages = site.pages.search(
        category="deleted",
        tags="-已归档 -重写中 -管理"
    )

    for page in pages:
        pending_check_pages.append([page.fullname, page.rating, "deleted"])


@Retry(ifRaise=True)
def check_pending_delete_pages():
    """清理 pending_pages 中已不存在的页面"""
    for page_id in list(pending_pages.keys()):
        if site.page.get(pending_pages[page_id][2], False) is None:
            del pending_pages[page_id]


@Retry(ifRaise=True)
def generate_announce():
    """生成最终的删除宣告数据"""
    for page_info in pending_check_pages:
        index = -1
        unix_name, release_score, page_type = page_info
        page = site.page.get(unix_name)
        logger.info(f'正在生成 {unix_name} 的删除宣告')
        for j, value in enumerate(js_result):
            if value["link"] == page.get_url():
                index = j
                break
        if index == -1:
            js_result.append(
                {
                    "link": page.get_url(),
                    "title": page.title,
                    "score": page.rating,
                    "time": (
                        24 if release_score <= -6 else 72
                    ),
                    "context": page.source.wiki_text,
                    "page_type": [page_type],
                    "release_score": release_score,
                }
            )
        else:
            if page_type not in js_result[index]["page_type"]:
                js_result[index]["page_type"] += [page_type]
            logger.info(f'当前页面类型为 {js_result[index]["page_type"]}')


def main():
    global deviant, js_result, pending_check_pages, pending_delete_pages
    deviant = []
    js_result = []
    pending_check_pages = []
    pending_delete_pages = []
    logger.info('开始检查文章页面并发布删除宣告')
    check_original_pages()
    logger.info('开始更新待删除文章信息')
    check_pending_pages()
    logger.info('将自删页面加入待删除列表')
    check_deleted_pages()
    logger.info('删除待删除页面信息中的不存在页面')
    check_pending_delete_pages()
    with open("deleted_pages.pkl", "wb") as file:
        pickle.dump(pending_pages, file)
        logger.debug(f'保存待删除页面信息：{pending_pages}')
    logger.info('开始检验并生成删除宣告')
    generate_announce()
    logger.info('导出js文件')
    logger.debug(pending_delete_pages, js_result, deviant)
    with open("data.json", "w") as json_file:
        json.dump(
            {
                "pre_delete_pages": pending_delete_pages,
                "deleted_pages": js_result,
                "errors": deviant,
                "update_timestamp": time.time(),
            },
            json_file,
        )

if __name__ == "__main__":
    flag = 0
    while flag < 5:
        try:
            logger.info('开始启动页面管理程序')
            main()
            logger.info('主程序运行完成')
            break
        except (ConnectError, ConnectTimeout):
            logger.error("网络错误，1分钟后重试")
            time.sleep(60)
        except Exception as e:
            flag += 1
            exc_type, exc_value, exc_traceback_obj = sys.exc_info()
            logger.error(f'第{flag}/5次重试，错误类型：{exc_type}，错误内容：{exc_value}, 3s后重试')
            traceback.print_exc()
            time.sleep(3)

    if flag >= 5:
        logger.critical('多次错误致使程序退出，等待人工重新启动')
        sys.exit(1)