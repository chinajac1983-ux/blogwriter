"""
Runify · BlogWriter — Hermes Bridge 4.0

职责：
- 接收 Hermes Agent 推送的行业信息
- 标准化后写入 IndustryNews 表，source="hermes"
- 支持两种传输方式：
  1. 文件模式：Hermes 把结果写成 JSON 文件，BlogWriter 定时读取
  2. 直接调用模式：Hermes 通过内部调用 ingest_hermes_results()

同一台 VPS 部署推荐文件模式，简单可靠，无需网络通信。

Hermes 侧配置说明（见设计文档第十六节）：
- 每周一次 cron 任务，搜索各客户的行业动态
- 把结果写入 JSON 文件：{HERMES_OUTPUT_DIR}/client_{client_id}.json
- BlogWriter 每 6 小时读取一次，导入后删除文件

JSON 格式：
[
  {
    "title": "文章标题",
    "summary": "摘要",
    "full_text": "全文（可选）",
    "url": "原文链接",
    "published_at": "2026-05-01T10:00:00",
    "source_api": "hermes_felo",
    "quality_score": 7,
    "keywords_matched": ["keyword1", "keyword2"],
    "language": "en"
  }
]
"""

import json
import logging
import os
from datetime import datetime, timedelta

from config import HERMES_OUTPUT_DIR
from extensions import db
from models import IndustryNews

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# 直接调用模式
# ══════════════════════════════════════════════════════════════════

def ingest_hermes_results(client_id, results):
    """
    接收 Hermes 推送的行业信息列表，写入 IndustryNews 表。

    参数：
    - client_id: 客户ID
    - results: list，每个元素是一个字典，包含 title/summary/url 等字段

    返回：写入的条数
    """
    if not results:
        return 0

    # 已存在的URL（去重）
    existing_urls = {
        row.url for row in IndustryNews.query.filter_by(
            client_id=client_id, source="hermes"
        ).with_entities(IndustryNews.url).all()
    }

    saved = 0
    for item in results:
        url = (item.get("url") or "").strip()
        if not url or url in existing_urls:
            continue

        title = (item.get("title") or "").strip()
        if len(title) < 10:
            continue

        try:
            news = IndustryNews(
                client_id=client_id,
                source="hermes",
                source_api=item.get("source_api", "hermes_felo"),
                title=title[:500],
                summary=(item.get("summary") or "")[:2000],
                full_text=item.get("full_text") or None,
                url=url[:500],
                published_at=_parse_dt(item.get("published_at")),
                language=(item.get("language") or "en")[:20],
                keywords_matched=json.dumps(
                    item.get("keywords_matched", []), ensure_ascii=False
                ),
                has_full_text=bool(item.get("full_text")),
                quality_score=int(item.get("quality_score", 5)),
                # Hermes 返回的内容默认给 5 分基础分
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(days=60),
            )
            db.session.add(news)
            existing_urls.add(url)
            saved += 1
        except Exception as e:
            log.warning(f"[HermesBridge] 写入失败 client={client_id}：{e}")

    try:
        db.session.commit()
        log.info(f"[HermesBridge] client={client_id} 导入 {saved} 条 Hermes 数据")
    except Exception as e:
        db.session.rollback()
        log.error(f"[HermesBridge] client={client_id} 提交失败：{e}")
        return 0

    return saved


# ══════════════════════════════════════════════════════════════════
# 文件模式（推荐，同VPS部署）
# ══════════════════════════════════════════════════════════════════

def read_hermes_output_files():
    """
    定时任务入口（每6小时执行一次）。
    扫描 HERMES_OUTPUT_DIR 目录，读取 Hermes 输出的 JSON 文件并导入。
    文件格式：client_{client_id}.json
    导入成功后删除文件，避免重复处理。
    """
    if not os.path.exists(HERMES_OUTPUT_DIR):
        log.debug(f"[HermesBridge] 输出目录不存在，跳过：{HERMES_OUTPUT_DIR}")
        return

    files = [f for f in os.listdir(HERMES_OUTPUT_DIR)
             if f.startswith("client_") and f.endswith(".json")]

    if not files:
        log.debug("[HermesBridge] 没有新的 Hermes 输出文件")
        return

    log.info(f"[HermesBridge] 发现 {len(files)} 个 Hermes 输出文件")

    for filename in files:
        filepath = os.path.join(HERMES_OUTPUT_DIR, filename)
        client_id = _extract_client_id(filename)

        if client_id is None:
            log.warning(f"[HermesBridge] 无法解析 client_id，跳过：{filename}")
            continue

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                results = json.load(f)

            if not isinstance(results, list):
                log.warning(f"[HermesBridge] 文件格式错误（非列表），跳过：{filename}")
                _move_to_error(filepath)
                continue

            saved = ingest_hermes_results(client_id, results)
            log.info(f"[HermesBridge] 处理完成 {filename}，导入 {saved} 条")

            # 导入成功后删除文件
            os.remove(filepath)

        except json.JSONDecodeError as e:
            log.error(f"[HermesBridge] JSON 解析失败 {filename}：{e}")
            _move_to_error(filepath)
        except Exception as e:
            log.error(f"[HermesBridge] 处理文件失败 {filename}：{e}")
            _move_to_error(filepath)


def _extract_client_id(filename):
    """从文件名 client_123.json 提取 client_id"""
    try:
        name = filename.replace("client_", "").replace(".json", "")
        return int(name)
    except (ValueError, AttributeError):
        return None


def _move_to_error(filepath):
    """把解析失败的文件移到 error 子目录，方便排查"""
    try:
        error_dir = os.path.join(os.path.dirname(filepath), "error")
        os.makedirs(error_dir, exist_ok=True)
        filename = os.path.basename(filepath)
        error_path = os.path.join(error_dir, filename)
        os.rename(filepath, error_path)
        log.info(f"[HermesBridge] 已移至错误目录：{error_path}")
    except Exception as e:
        log.warning(f"[HermesBridge] 移动文件失败：{e}")


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _parse_dt(value):
    """安全解析日期字符串"""
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        from dateutil import parser as dateparser
        dt = dateparser.parse(str(value))
        return dt.replace(tzinfo=None) if dt else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# Hermes 侧配置示例（供参考，在 Hermes 里配置）
# ══════════════════════════════════════════════════════════════════

HERMES_TASK_TEMPLATE = """
# Hermes cron 任务配置示例
# 在 Hermes dashboard 里创建一个每周执行的任务

任务名称：BlogWriter 行业新闻扫描
执行频率：每周一次（建议周一凌晨2点）
执行内容：

1. 连接 BlogWriter MySQL 数据库
   查询所有 is_active=True 的客户
   SELECT id, product_desc, markets, language FROM clients
   JOIN sites ON clients.id = sites.client_id
   WHERE clients.is_active = 1

2. 对每个客户：
   - 用 product_desc + markets 构建搜索词
   - 用 Felo Search 搜索行业动态（最近30天）
   - 用 Felo Web Fetch 抓取重要文章全文
   - 整理结果为标准JSON格式

3. 把结果写入 JSON 文件：
   路径：/tmp/hermes_news/client_{client_id}.json
   
   格式：
   [
     {
       "title": "文章标题",
       "summary": "摘要（100-300字）",
       "full_text": "全文（可选，有则更好）",
       "url": "原文链接",
       "published_at": "2026-05-01T10:00:00",
       "source_api": "hermes_felo",
       "quality_score": 7,
       "keywords_matched": ["关键词1", "关键词2"],
       "language": "en"
     }
   ]

4. BlogWriter 会在6小时内自动读取并导入
"""
