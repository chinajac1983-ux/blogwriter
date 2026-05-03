from datetime import datetime
import requests

from time_utils import utc_to_cst_str
from site_utils import normalize_publish_mode


def validate_wordpress(wp_url, username, app_password):
    """验证 WordPress REST API 与应用密码是否可用。"""
    if not wp_url or not username or not app_password:
        return False, "WordPress 站点、用户名和应用密码不能为空"
    try:
        test = requests.get(
            f"{wp_url.rstrip('/')}/wp-json/wp/v2/users/me",
            auth=(username, app_password),
            timeout=10
        )
        if test.status_code != 200:
            return False, f"WordPress 验证失败（状态码 {test.status_code}），请检查用户名和应用密码"
        return True, ""
    except Exception as e:
        return False, f"无法连接到 WordPress 站点：{e}"


def create_wordpress_test_draft(wp_url, username, app_password):
    """创建一篇 WordPress 测试草稿，不自动删除，避免误删客户内容。"""
    url = wp_url.rstrip("/") + "/wp-json/wp/v2/posts"
    timestamp = utc_to_cst_str(datetime.utcnow()).replace("T", " ")
    title = f"[Action Required] Delete Me - Runify Connection Test ({timestamp})"
    content = (
        "<p><strong>Action Required:</strong> This is a Runify connection test draft.</p>"
        "<p>If you can see this draft in your WordPress dashboard, the connection test has passed.</p>"
        "<p>Please delete this draft manually after confirming the test. Runify will not delete it automatically to avoid removing customer content by mistake.</p>"
    )
    resp = requests.post(
        url,
        auth=(username, app_password),
        json={"title": title, "content": content, "status": "draft"},
        timeout=20
    )
    resp.raise_for_status()
    data = resp.json()
    post_id = data.get("id")
    return {"id": post_id, "link": data.get("link") or (wp_url.rstrip("/") + "/?p=" + str(post_id))}


def publish_to_wordpress(
    wp_url, username, app_password, title, content,
    publish_mode="draft",
    seo_description="",
    seo_slug="",
    seo_focus_keyword="",
):
    """发布到 WordPress。

    4.0 新增 SEO 参数：
    - seo_description: 写入 Yoast / RankMath 的 meta description
    - seo_slug: 自定义 URL slug
    - seo_focus_keyword: 写入 Yoast / RankMath 的 focus keyword

    注意：meta 字段写入需要 WordPress 站点安装了 Yoast SEO 或 RankMath。
    未安装时 meta 字段会被忽略，不影响发布流程。
    """
    mode = normalize_publish_mode(publish_mode)
    url = wp_url.rstrip("/") + "/wp-json/wp/v2/posts"

    post_data = {
        "title": title,
        "content": content,
        "status": mode,
    }

    # 自定义 slug
    if seo_slug:
        post_data["slug"] = seo_slug

    # SEO meta 字段（同时兼容 Yoast 和 RankMath 的字段名）
    meta = {}
    if seo_description:
        meta["_yoast_wpseo_metadesc"] = seo_description        # Yoast
        meta["rank_math_description"] = seo_description         # RankMath
    if seo_focus_keyword:
        meta["_yoast_wpseo_focuskw"] = seo_focus_keyword        # Yoast
        meta["rank_math_focus_keyword"] = seo_focus_keyword     # RankMath
    if meta:
        post_data["meta"] = meta

    resp = requests.post(
        url,
        auth=(username, app_password),
        json=post_data,
        timeout=45
    )
    resp.raise_for_status()
    data = resp.json()
    post_id = data.get("id")
    if not post_id:
        raise ValueError("WordPress 返回成功但缺少 post id")
    return {
        "id": post_id,
        "link": data.get("link") or (wp_url.rstrip("/") + "/?p=" + str(post_id)),
        "mode": mode,
    }


def verify_wordpress_post_rest(wp_url, username, app_password, post_id, expected_status=None):
    """通过 WordPress REST 回查文章是否真实存在。
    只要 post_id 存在且状态不是 trash，即视为成功。
    """
    if not post_id:
        return False, "缺少 WordPress post_id"
    try:
        url = wp_url.rstrip("/") + f"/wp-json/wp/v2/posts/{post_id}?context=edit"
        resp = requests.get(url, auth=(username, app_password), timeout=20)
        if resp.status_code != 200:
            return False, f"WordPress REST 回查失败，状态码 {resp.status_code}"
        data = resp.json()
        if int(data.get("id") or 0) != int(post_id):
            return False, "WordPress REST 回查返回的 post_id 不一致"
        if data.get("status") == "trash":
            return False, "WordPress 文章已被移入回收站"
        return True, ""
    except Exception as e:
        return False, f"WordPress REST 回查异常：{e}"


def verify_wordpress_public_url(wp_url):
    """publish 模式下检查前台文章 URL 是否可访问；草稿不使用该检查。"""
    if not wp_url:
        return False, "缺少 WordPress 文章 URL"
    try:
        resp = requests.get(wp_url, timeout=20, allow_redirects=True)
        if resp.status_code == 404:
            return False, "WordPress 前台回查 404，文章未正常展示"
        if resp.status_code >= 500:
            return False, f"WordPress 前台回查服务器错误，状态码 {resp.status_code}"
        if resp.status_code >= 400:
            return False, f"WordPress 前台回查失败，状态码 {resp.status_code}"
        return True, ""
    except Exception as e:
        return False, f"WordPress 前台回查异常：{e}"
