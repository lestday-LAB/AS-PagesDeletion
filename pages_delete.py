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
logger.info(f"载入历史数据：{pending_pages}")

with open("config.yaml", "r", encoding="utf-8") as f:
    config: dict = yaml.safe_load(f)

deviant: list[dict] = []
staff_unix_names: list[str] = config["staffs"]
pending_delete_pages: list[dict] = []
pending_check_pages: list[dict] = []
js_result: list[dict] = []

# ======================
# HTTP / AMC
# ======================

async def single_request(site: Site, _body: dict[str, Any]):
    amc = site.client.amc_client
    url = (
        f"http{'s' if site.ssl_supported else ''}://"
        f"{site.unix_name}.wikidot.com/ajax-module-connector.php"
    )
    _body["wikidot_token7"] = 123456
    async with httpx.AsyncClient(timeout=amc.config.request_timeout) as client:
        return await client.post(
            url,
            headers=amc.header.get_header(),
            data=_body,
        )

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

# ======================
# 日志辅助
# ======================

def page_desc(page) -> str:
    if "新手专区" in page.tags:
        return f"{page.fullname}（新手专区）"
    return page.fullname

# ======================
# 重试装饰器
# ======================

def Retry(
    retry_text: str | None = None,
    last_text: str | None = None,
    times: int = 3,
    ifRaise: bool = False,
):
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            for i in range(times):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    if retry_text:
                        logger.warning(retry_text)
                    if i == times - 1:
                        if last_text:
                            logger.error(last_text)
                        if ifRaise:
                            raise
        return wrapper
    return decorator

# ======================
# Wikidot 客户端
# ======================

wd = wikidot.Client(username=config["username"], password=config["password"])
site = wd.site.get(config["siteUnixName"])

# ======================
# 发帖 / 编辑 / 标签
# ======================

@Retry(last_text="放弃重试，跳过修改")
def edit_post(
    thread_id: int,
    post_id: int,
    title: str | None = None,
    source: str | None = None,
):
    if title is None and source is None:
        logger.info("标题与源代码为空，放弃修改")
        return

    resp = run_async(
        single_request(
            site,
            {
                "postId": post_id,
                "threadId": thread_id,
                "moduleName": "forum/sub/ForumEditPostFormModule",
            },
        )
    )

    error = {
        "threadId": thread_id,
        "postId": post_id,
        "errorType": "edit_post_unknown",
    }

    status = resp.json()["status"]
    if status == "no_permission":
        error["errorType"] = "edit_post_permission"
        deviant.append(error)
        logger.warning("缺少编辑权限，跳过修改")
        return
    elif status == "ok":
        if error in deviant:
            deviant.remove(error)
    else:
        logger.warning(f"编辑失败，状态为 {status}，准备重试")
        if error not in deviant:
            deviant.append(error)
        raise exceptions.WikidotStatusCodeException(status_code=status)

    html = BeautifulSoup(resp.json()["body"], "lxml")
    current_id = int(html.select("form#edit-post-form>input")[1].get("value"))
    current_title = html.select_one("input#np-title").get("value")
    current_source = html.select_one("textarea#np-text").get_text()

    if title is None:
        title = current_title
    if source is None:
        source = current_source

    if current_title == title and current_source == source:
        logger.info("标题与源代码和原帖相同，放弃修改")
        return

    site.amc_request(
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
    )


@Retry(last_text="放弃重试，跳过创建")
def new_post(
    thread_id: int,
    title: str = "",
    source: str = "",
    parent_id: int | None = None,
):
    if not source:
        logger.info("源代码为空，放弃创建")
        return

    body = {
        "threadId": thread_id,
        "title": title,
        "source": source,
        "action": "ForumAction",
        "event": "savePost",
        "moduleName": "Empty",
    }
    if parent_id is not None:
        body["parentId"] = parent_id

    resp = run_async(single_request(site, body))

    error = {
        "threadId": thread_id,
        "errorType": "new_post_unknown",
    }

    status = resp.json()["status"]
    if status == "no_permission":
        error["errorType"] = "new_post_permission"
        deviant.append(error)
        logger.warning("缺少编辑权限，跳过创建")
    elif status == "ok":
        if error in deviant:
            deviant.remove(error)
    else:
        logger.warning(f"发帖失败，状态为 {status}，准备重试")
        if error not in deviant:
            deviant.append(error)
        raise exceptions.WikidotStatusCodeException(status_code=status)


@Retry(last_text="放弃重试，跳过修改")
def edit_tags(page_id: int, tags: str):
    resp = site.amc_request(
        [
            {
                "pageId": page_id,
                "tags": tags,
                "action": "WikiPageAction",
                "event": "saveTags",
                "moduleName": "Empty",
            }
        ]
    )[0]

    error = {
        "pageId": page_id,
        "errorType": "edit_tags_unknown",
    }

    status = resp.json()["status"]
    if status == "no_permission":
        error["errorType"] = "edit_tags_permission"
        deviant.append(error)
        logger.warning("缺少编辑权限，跳过修改")
    elif status == "ok":
        if error in deviant:
            deviant.remove(error)
    else:
        logger.warning(f"编辑标签失败，状态为 {status}，准备重试")
        if error not in deviant:
            deviant.append(error)
        raise exceptions.WikidotStatusCodeException(status_code=status)

# ======================
# 删除宣告构造
# ======================

def build_delete_announce(score: int, timer: float) -> str:
    iso_time = datetime.utcfromtimestamp(timer).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    timestamp_ms = int(timer * 1000)
    return f"""由于条目的分数为 {score} 分，现根据[[[deletions-policy|删除政策]]]，宣告将删除此页：

[[iframe https://arandintday.github.io/thebackhubscn/Deletion%20Time%20Tool/Deletion%20Time%20Tool.html?timestamp={timestamp_ms}&type=0 style="width: 400px; height: 60px;"]]

如果你不是作者又想要重写该条目，请在此帖回复申请。请先取得作者的同意，并将原文的源代码复制至沙盒里。除非你是工作人员，否则请勿就申请重写以外的范围回复此帖。
"""


# ======================
# 讨论区工具
# ======================

def get_posts(thread_id: int) -> list[dict]:
    resp = site.amc_request(
        [{"t": thread_id, "moduleName": "forum/ForumViewThreadModule"}]
    )[0]

    html = BeautifulSoup(resp.json()["body"], "lxml")
    pagers = 1
    if pagerno := html.select_one("span.pager-no"):
        pagers = int(re.search(r"of (\d+)", pagerno.text).group(1))

    resps = site.amc_request(
        [
            {
                "pageNo": n + 1,
                "t": thread_id,
                "order": "",
                "moduleName": "forum/ForumViewThreadPostsModule",
            }
            for n in range(pagers)
        ]
    )

    posts = []
    for r in resps:
        h = BeautifulSoup(r.json()["body"], "lxml")
        for p in h.select("div.post"):
            parent = p.parent.get("id")
            parent_id = (
                int(re.search(r"fpc-(\d+)", parent).group(1))
                if parent != "thread-container-posts"
                else ""
            )
            posts.append(
                {
                    "id": int(re.search(r"post-(\d+)", p["id"]).group(1)),
                    "thread_id": thread_id,
                    "title": p.select_one("div.title").text.strip(),
                    "parent_id": parent_id,
                    "created_by": user_parser(
                        wd, p.select_one("div.info span.printuser")
                    ),
                    "created_at": odate_parser(
                        p.select_one("div.info span.odate")
                    ),
                    "source_ele": p.select_one("div.content"),
                }
            )
    return posts


@Retry(ifRaise=True)
def get_discuss_id(page_id: int) -> int:
    resp = site.amc_request(
        [
            {
                "page_id": page_id,
                "action": "ForumAction",
                "event": "createPageDiscussionThread",
                "moduleName": "Empty",
            }
        ]
    )[0]
    return int(resp.json()["thread_id"])


def find_staff_post(posts: list[dict]) -> dict | None:
    for p in posts:
        if (
            "职员帖" in p["title"]
            and "删除宣告" in p["title"]
            and p["created_by"].name in staff_unix_names
        ):
            return p
    return None


def get_delete_hours(tags: list[str], score: int) -> int:
    if "新手专区" in tags:
        return 48 if score <= -6 else 0
    if score <= -4:
        return 24
    elif score <= -2:
        return 48
    return 0

# ======================
# 主逻辑
# ======================

@Retry(ifRaise=True)
def check_original_pages():
    pages = site.pages.search(
        category="-archived -setting -space -system -topic",
        tags="-已归档 -管理 -作者 -待删除 -重写中 -_低分删除豁免 原创 搬运 文章 设定框架 _test -组件后端 -组件 -版式",
        rating="<=0",
    )

    now = time.time()

    for page in pages:
        score = page.rating
        hours = get_delete_hours(page.tags, score)

        if hours == 0:
            if "新手专区" in page.tags:
                logger.info(
                    f"{page_desc(page)} 分数 {score}，未达新手专区删除条件（≤ -6），跳过"
                )
            else:
                logger.info(
                    f"{page_desc(page)} 分数 {score}，不满足删除条件，跳过"
                )
            continue

        pending_pages.pop(page.id, None)

        tid = get_discuss_id(page.id)
        post = find_staff_post(get_posts(tid))
        deadline = now + hours * 3600
        src = build_delete_announce(score, deadline)

        if post is None:
            new_post(tid, "【职员帖】删除宣告", src)
        else:
            edit_post(tid, post["id"], source=src)

        if post is not None:
            err = next(
                (
                    e
                    for e in deviant
                    if e.get("postId") == post["id"]
                    or (e.get("threadId") == tid and "post" in e.get("errorType", ""))
                ),
                None,
            )
            if err:
                logger.warning(f"{page_desc(page)} 发帖失败，不标记待删除")
                continue

        pending_pages[page.id] = [score, deadline, page.fullname]

        if "新手专区" in page.tags:
            logger.info(
                f"{page_desc(page)} 分数 {score}，触发新手专区删除线，设置 48h 删除宣告"
            )
        else:
            logger.info(
                f"{page_desc(page)} 分数 {score}，设置 {hours}h 删除宣告"
            )

        edit_tags(page.id, " ".join(page.tags) + " 待删除")


@Retry(ifRaise=True)
def check_pending_pages():
    pages = site.pages.search(category="-reserve", tags="+待删除")
    now = time.time()

    def bucket(tags: list[str], score: int) -> int:
        if "新手专区" in tags:
            return 48 if score <= -6 else 0
        if score <= -4:
            return 24
        elif score <= -2:
            return 48
        return 0

    for page in pages:
        pid = page.id
        tid = get_discuss_id(pid)
        post = find_staff_post(get_posts(tid))
        tags = page.tags
        score = page.rating

        if not post:
            edit_tags(pid, " ".join(tags).replace("待删除", ""))
            pending_pages.pop(pid, None)
            continue

        iframe = post["source_ele"].select_one("iframe")
        if not iframe:
            continue

        src = iframe.get("src", "")
        ts = None

        if "timerdfc.pages.dev" in src:
            m = re.search(r"time=([^&]+)", src)
            if m:
                ts = datetime.fromisoformat(
                    m.group(1).replace("%3A", ":").replace("Z", "+00:00")
                ).timestamp()

        if ts is None:
            continue

        if pid in pending_pages:
            announced, _, _ = pending_pages[pid]
        else:
            announced = score
            pending_pages[pid] = [score, ts, page.fullname]

        left = ts - now

        # 冻结期
        if left < 21600:
            if score >= -2:
                edit_post(tid, post["id"], source="【分数回升，倒计时终止】")
                edit_tags(pid, " ".join(tags).replace("待删除", ""))
                pending_pages.pop(pid, None)

                if "新手专区" in tags:
                    logger.info(
                        f"{page_desc(page)}（新手专区）在冻结期内分数回升至 {score}，取消删除"
                    )
                else:
                    logger.info(
                        f"{page_desc(page)} 在冻结期内分数回升至 {score}，取消删除"
                    )
                continue
            else:
                if "新手专区" in tags:
                    logger.info(
                        f"{page_desc(page)}（新手专区）处于冻结期（剩余 {int(left)}s），禁止刷新"
                    )
                else:
                    logger.info(
                        f"{page_desc(page)} 处于冻结期（剩余 {int(left)}s），禁止刷新"
                    )
                continue

        if score > -2:
            edit_post(tid, post["id"], source="【分数回升，倒计时停止】")
            edit_tags(pid, " ".join(tags).replace("待删除", ""))
            pending_pages.pop(pid, None)

            if "新手专区" in tags:
                logger.info(
                    f"{page_desc(page)} 分数回升至 {score}，取消新手专区删除宣告"
                )
            else:
                logger.info(
                    f"{page_desc(page)} 分数回升至 {score}，取消删除宣告"
                )
            continue

        ob = bucket(tags, announced)
        nb = bucket(tags, score)
        if ob != nb and nb > 0:
            new_ts = now + nb * 3600
            if abs(ts - new_ts) > 300:
                edit_post(
                    tid,
                    post["id"],
                    source=build_delete_announce(score, new_ts),
                )
                pending_pages[pid] = [score, new_ts, page.fullname]

                if "新手专区" in tags:
                    logger.info(
                        f"{page_desc(page)}（新手专区）跨档更新 "
                        f"({announced} → {score})，新倒计时 {nb}h"
                    )
                else:
                    logger.info(
                        f"{page_desc(page)} 跨档更新 "
                        f"({announced} → {score})，新倒计时 {nb}h"
                    )
                ts = new_ts

        if now >= ts:
            if "新手专区" in tags:
                logger.info(
                    f"{page_desc(page)}（新手专区）删除倒计时到期，加入删除宣告列表"
                )
            else:
                logger.info(
                    f"{page_desc(page)} 删除倒计时到期，加入删除宣告列表"
                )

            pending_check_pages.append([page.fullname, announced, "normal"])
        else:
            pending_delete_pages.append(
                {
                    "link": page.get_url(),
                    "title": page.title,
                    "score": score,
                    "release_score": announced,
                    "time": round((ts - now) / 3600, 1),
                    "discuss_link": f"https://{config['siteUnixName']}.wikidot.com/forum/t-{tid}",
                    "post_id": post["id"],
                    "timestamp": ts,
                }
            )

        # ✅ -30 分直接加入删除宣告
        if score <= -30:
            logger.info(
                f"{page_desc(page)} 已低于 -30 分，直接加入删除宣告列表"
            )
            pending_check_pages.append(
                [page.fullname, announced, "minusThirty"]
            )


@Retry(ifRaise=True)
def check_deleted_pages():
    pages = site.pages.search(
        category="deleted", tags="-已归档 -重写中 -管理"
    )
    for p in pages:
        pending_check_pages.append([p.fullname, p.rating, "deleted"])


@Retry(ifRaise=True)
def check_pending_delete_pages():
    for pid in list(pending_pages):
        if site.page.get(pending_pages[pid][2], False) is None:
            del pending_pages[pid]


@Retry(ifRaise=True)
def generate_announce():
    for fullname, rel_score, ptype in pending_check_pages:
        page = site.page.get(fullname)
        link = page.get_url()
        idx = next(
            (i for i, v in enumerate(js_result) if v["link"] == link),
            -1,
        )
        if idx == -1:
            js_result.append(
                {
                    "link": link,
                    "title": page.title,
                    "score": page.rating,
                    "time": 24 if rel_score <= -10 else 72,
                    "context": page.source.wiki_text,
                    "page_type": [ptype],
                    "release_score": rel_score,
                }
            )
        else:
            if ptype not in js_result[idx]["page_type"]:
                js_result[idx]["page_type"].append(ptype)


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

    logger.info("清理 pending_pages")
    check_pending_delete_pages()

    with open("deleted_pages.pkl", "wb") as f:
        pickle.dump(pending_pages, f)

    logger.info("生成删除宣告")
    generate_announce()

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "pre_delete_pages": pending_delete_pages,
                "deleted_pages": js_result,
                "errors": deviant,
                "update_timestamp": time.time(),
            },
            f,
            ensure_ascii=False,
        )


# ======================
# 入口
# ======================

if __name__ == "__main__":
    flag = 0
    while flag < 5:
        try:
            logger.info("启动页面管理程序")
            main()
            logger.info("主程序运行完成")
            break
        except (ConnectError, ConnectTimeout):
            logger.error("网络错误，10秒后重试")
            time.sleep(10)
        except Exception:
            flag += 1
            logger.error(
                f"第 {flag}/5 次重试，错误如下：",
                exc_info=True,
            )
            time.sleep(3)

    if flag >= 5:
        logger.critical("多次错误，程序退出")
        sys.exit(1)