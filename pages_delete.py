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

# ======================
# 基础配置
# ======================

NEWBIER_TAG = "新手专区"

if not os.path.exists("logs"):
    os.makedirs("logs")

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
file_handler = logging.FileHandler(
    f"logs/{datetime.now().strftime('%Y-%m-%d')}.txt"
)

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

# ======================
# 工具函数
# ======================

def is_newbie_page(page) -> bool:
    return NEWBIER_TAG in page.tags


def Retry(
    retry_text: str | None = None,
    last_text: str | None = None,
    times: int = 3,
    ifRaise: bool = False
):
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            for i in range(times):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if retry_text:
                        logger.warning(retry_text)
                    if i == times - 1:
                        if last_text:
                            logger.error(last_text)
                        if ifRaise:
                            raise e
        return wrapper
    return decorator


wd = wikidot.Client(username=config["username"], password=config["password"])
site = wd.site.get(config["siteUnixName"])


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


# ======================
# 页面操作
# ======================

@Retry(last_text="放弃重试，跳过修改")
def edit_post(thread_id: int, post_id: int, title: str | None = None, source: str | None = None):
    if title is None and source is None:
        return

    response = asyncio.run(
        single_request(
            site,
            {
                "postId": post_id,
                "threadId": thread_id,
                "moduleName": "forum/sub/ForumEditPostFormModule",
            },
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
    current_id = int(html.select("form#edit-post-form>input")[1].get("value"))

    if title is None:
        title = html.select_one("input#np-title").get("value")
    if source is None:
        source = html.select_one("textarea#np-text").get_text()

    response = site.amc_request(
        [
            {
                "postId": post_id,
                "currentRevisionId": current_id,
                "title": title,
                "source": source,
                "action": "ForumAction",
                "event": "saveEditPost",
                "moduleName": "Empty",
            }
        ]
    )[0]


@Retry(last_text="放弃重试，跳过创建")
def new_post(thread_id: int, title: str = "", source: str = "", parent_id: int = ""):
    if source == "":
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
                "moduleName": "Empty",
            },
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
                "moduleName": "Empty",
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
        logger.warning("缺少编辑权限，跳过修改")
    elif status == "ok":
        if error_dict in deviant:
            deviant.remove(error_dict)
    else:
        logger.warning(f"编辑失败，状态为{status}，准备重试")
        if error_dict not in deviant:
            deviant.append(error_dict)
        raise exceptions.WikidotStatusCodeException(status_code=status)


# ======================
# 删除宣告
# ======================

def build_delete_announce(score: int, timer: float, newbie: bool = False) -> str:
    timestamp_ms = int(timer * 1000)
    extra_note = "且属于新手专区文章" if newbie else ""
    return f"""此文章目前为 {score} 分{extra_note}，现依据[[[deletions-guide|删除指导]]]宣告将删除此页面：

[[iframe https://arandintday.github.io/thebackhubscn/Deletion%20Time%20Tool/Deletion%20Time%20Tool.html?timestamp={timestamp_ms}&type=0 style="width: 400px; height: 60px;"]]

请本文章作者尽快进行修改内容提高质量。
如果该页面作者无法及时做出更改，其他人也可以在确认后向管理组申请重写。"""


# ======================
# 论坛相关
# ======================

def get_posts(thread_id: int) -> list[dict]:
    response = site.amc_request(
        [
            {
                "t": thread_id,
                "moduleName": "forum/ForumViewThreadModule",
            }
        ]
    )[0]

    html = BeautifulSoup(response.json()["body"], "lxml")
    pagerno = html.select_one("span.pager-no")
    pagers = int(re.search(r"of (\d+)", pagerno.text).group(1)) if pagerno else 1

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
            parent = post.parent.get("id")

            parent_id = (
                int(re.search(r"fpc-(\d+)", parent).group(1))
                if parent != "thread-container-posts"
                else ""
            )

            posts.append(
                {
                    "id": int(re.search(r"post-(\d+)", post.get("id")).group(1)),
                    "thread_id": thread_id,
                    "title": post.select_one("div.title").text.strip(),
                    "parent_id": parent_id,
                    "created_by": user_parser(wd, cuser),
                    "created_at": odate_parser(codate),
                    "source_ele": post.select_one("div.content"),
                }
            )

    return posts


@Retry(ifRaise=True)
def get_discuss_id(page_id: int) -> int:
    response = site.amc_request(
        [
            {
                "page_id": page_id,
                "action": "ForumAction",
                "event": "createPageDiscussionThread",
                "moduleName": "Empty",
            }
        ]
    )[0]
    return int(response.json()["thread_id"])


def find_staff_post(posts: list[dict]) -> dict:
    for post in posts:
        if (
            "职员帖" in post["title"]
            and "删除宣告" in post["title"]
            and post["created_by"].name in staff_unix_names
        ):
            return post


# ======================
# 删除策略核心
# ======================

def get_delete_hours(score: int, is_newbie: bool = False) -> int:
    if is_newbie:
        return 24 if score <= -6 else 0
    else:
        if score <= -6:
            return 24
        elif score <= -2:
            return 78
        else:
            return 0


def score_bucket(score: int, is_newbie: bool = False) -> int:
    if is_newbie:
        return 24 if score <= -6 else 0
    else:
        if score <= -6:
            return 24
        elif score <= -2:
            return 78
        else:
            return 0


# ======================
# 主逻辑
# ======================

@Retry(ifRaise=True)
def check_original_pages():
    pages = site.pages.search(
        category="-archived -deleted -setting -space -system -topic",
        tags="-已归档 -管理 -作者 -待删除 -重写中 -_低分删除豁免 原创 搬运 文章 设定框架 _test -组件后端 -组件 -版式",
        rating="<=0",
    )

    current_time = time.time()

    for page in pages:
        newbie_flag = is_newbie_page(page)
        score = page.rating
        hours = get_delete_hours(score, newbie_flag)

        if hours == 0:
            logger.info(
                f"页面 {page.fullname} 分数为 {score}，"
                f"{'新手专区 ' if newbie_flag else ''}不满足删除条件，跳过"
            )
            continue

        if page.id in pending_pages:
            del pending_pages[page.id]

        discuss_id = get_discuss_id(page.id)
        deletion_post = find_staff_post(get_posts(discuss_id))
        timer_timestamp = current_time + hours * 3600
        post_source = build_delete_announce(score, timer_timestamp, newbie_flag)

        if deletion_post is None:
            new_post(discuss_id, "【职员帖】删除宣告", post_source)
        else:
            edit_post(discuss_id, deletion_post["id"], source=post_source)

        if deviant and deletion_post and deviant[-1].get("postId") == deletion_post["id"]:
            continue

        pending_pages[page.id] = [score, timer_timestamp, page.fullname]
        edit_tags(page.id, " ".join(page.tags) + " 待删除")
        logger.info(f"页面 {page.fullname} 分数 {score}，设置 {hours} 小时删除宣告")


@Retry(ifRaise=True)
def check_pending_pages():
    pages = site.pages.search(category="-reserve", tags="+待删除")
    current_time = time.time()

    for page in pages:
        newbie_flag = is_newbie_page(page)
        page_id = page.id
        discuss_id = get_discuss_id(page_id)
        deletion_post = find_staff_post(get_posts(discuss_id))

        if deletion_post is None:
            new_tags = [t for t in page.tags if t != "待删除"]
            edit_tags(page_id, " ".join(new_tags))
            pending_pages.pop(page_id, None)
            continue

        score = page.rating
        iframe = deletion_post["source_ele"].select_one("iframe")
        if iframe is None:
            continue

        src = iframe.get("src", "")
        record_timestamp = None

        if "timerdfc.pages.dev" in src:
            m = re.search(r"time=([^&]+)", src)
            if m:
                t = m.group(1).replace("%3A", ":").replace("Z", "+00:00")
                record_timestamp = datetime.fromisoformat(t).timestamp()

        if record_timestamp is None:
            continue

        if page_id in pending_pages:
            announced_score, _, _ = pending_pages[page_id]
        else:
            announced_score = score
            pending_pages[page_id] = [score, record_timestamp, page.fullname]

        remaining_seconds = record_timestamp - current_time

        if remaining_seconds < 21600:
            if (newbie_flag and score > -6) or (not newbie_flag and score > -2):
                edit_post(
                    discuss_id,
                    deletion_post["id"],
                    source="【分数回升，倒计时终止】",
                )
                edit_tags(page_id, page.tags.replace("待删除", ""))
                pending_pages.pop(page_id, None)
                continue

            pending_delete_pages.append(
                {
                    "link": page.get_url(),
                    "title": page.title,
                    "score": score,
                    "release_score": announced_score,
                    "time": round(remaining_seconds / 3600, 1),
                    "discuss_link": f"https://{config['siteUnixName']}.wikidot.com/forum/t-{discuss_id}",
                    "post_id": deletion_post["id"],
                    "timestamp": record_timestamp,
                    "status": "frozen",
                }
            )
            continue

        if (newbie_flag and score > -6) or (not newbie_flag and score > -2):
            edit_post(
                discuss_id,
                deletion_post["id"],
                source="【分数回升，倒计时停止】",
            )
            edit_tags(page_id, page.tags.replace("待删除", ""))
            pending_pages.pop(page_id, None)
            continue

        old_bucket = score_bucket(announced_score, newbie_flag)
        new_bucket = score_bucket(score, newbie_flag)

        if old_bucket != new_bucket and new_bucket > 0:
            new_timestamp = current_time + new_bucket * 3600
            if abs(record_timestamp - new_timestamp) > 300:
                edit_post(
                    discuss_id,
                    deletion_post["id"],
                    source=build_delete_announce(score, new_timestamp, newbie_flag),
                )
                pending_pages[page_id] = [score, new_timestamp, page.fullname]
                record_timestamp = new_timestamp

        if current_time >= record_timestamp:
            pending_check_pages.append([page.fullname, announced_score, "normal"])
        else:
            pending_delete_pages.append(
                {
                    "link": page.get_url(),
                    "title": page.title,
                    "score": score,
                    "release_score": announced_score,
                    "time": round((record_timestamp - current_time) / 3600, 1),
                    "discuss_link": f"https://{config['siteUnixName']}.wikidot.com/forum/t-{discuss_id}",
                    "post_id": deletion_post["id"],
                    "timestamp": record_timestamp,
                    "status": "normal",
                }
            )

        if page.rating <= -10:
            pending_check_pages.append([page.fullname, announced_score, "minusTen"])


@Retry(ifRaise=True)
def check_deleted_pages():
    pages = site.pages.search(category="deleted", tags="-已归档 -重写中 -管理")
    for page in pages:
        pending_check_pages.append([page.fullname, page.rating, "deleted"])


@Retry(ifRaise=True)
def check_pending_delete_pages():
    for page_id in list(pending_pages.keys()):
        if site.page.get(pending_pages[page_id][2], False) is None:
            del pending_pages[page_id]


@Retry(ifRaise=True)
def generate_announce():
    for page_info in pending_check_pages:
        unix_name, release_score, page_type = page_info
        page = site.page.get(unix_name)
        for value in js_result:
            if value["link"] == page.get_url():
                if page_type not in value["page_type"]:
                    value["page_type"].append(page_type)
                break
        else:
            js_result.append(
                {
                    "link": page.get_url(),
                    "title": page.title,
                    "score": page.rating,
                    "time": 24 if release_score <= -6 else 72,
                    "context": page.source.wiki_text,
                    "page_type": [page_type],
                    "release_score": release_score,
                }
            )


def main():
    global deviant, js_result, pending_check_pages, pending_delete_pages
    deviant = []
    js_result = []
    pending_check_pages = []
    pending_delete_pages = []

    logger.info("开始检查文章页面并发布删除宣告")
    check_original_pages()

    logger.info("开始更新待删除文章信息")
    check_pending_pages()

    logger.info("将自删页面加入待删除列表")
    check_deleted_pages()

    logger.info("清理不存在页面")
    check_pending_delete_pages()

    with open("deleted_pages.pkl", "wb") as file:
        pickle.dump(pending_pages, file)

    logger.info("生成删除宣告")
    generate_announce()

    with open("data.json", "w", encoding="utf-8") as json_file:
        json.dump(
            {
                "pre_delete_pages": pending_delete_pages,
                "deleted_pages": js_result,
                "errors": deviant,
                "update_timestamp": time.time(),
            },
            json_file,
            ensure_ascii=False,
        )


if __name__ == "__main__":
    flag = 0
    while flag < 5:
        try:
            main()
            break
        except (ConnectError, ConnectTimeout):
            logger.error("网络错误，1分钟后重试")
            time.sleep(60)
        except Exception:
            flag += 1
            logger.exception(f"第 {flag}/5 次重试失败，3 秒后重试")
            time.sleep(3)

    if flag >= 5:
        logger.critical("多次错误致使程序退出，等待人工重新启动")
        sys.exit(1)