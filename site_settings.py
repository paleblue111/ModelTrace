"""Shared public-site configuration, persisted with the existing Redis data."""
import json

from redis import WatchError

SETTINGS = "modeltrace:site:settings"
DEFAULTS = {
    "title": "每一次调用，都有迹可循。",
    "description": "持续观察模型的真实身份，查看精选监测与公开检测结果。",
    "announcement": "",
    "public_enabled": True,
    "public_hourly_limit": 5,
    "public_pending_limit": 1,
    "public_queue_limit": 10,
    "public_concurrency": 2,
}


class SiteSettings:
    def __init__(self, redis):
        self.redis = redis

    def get(self, client=None):
        raw = (client if client is not None else self.redis).get(SETTINGS)
        return {**DEFAULTS, **(json.loads(raw) if raw else {})}

    def update(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
            raise ValueError("首页设置包含不支持的字段")
        for field, maximum in (("title", 80), ("description", 300), ("announcement", 2000)):
            if field in changes and (not isinstance(changes[field], str) or len(changes[field]) > maximum):
                raise ValueError(f"{field} 最多 {maximum} 个字符")
        if "title" in changes and not changes["title"].strip():
            raise ValueError("首页标题不能为空")
        if "public_enabled" in changes and not isinstance(changes["public_enabled"], bool):
            raise ValueError("开放检测必须为布尔值")
        for field, maximum in (("public_hourly_limit", 1000), ("public_pending_limit", 20),
                               ("public_queue_limit", 1000), ("public_concurrency", 100)):
            if field in changes and (type(changes[field]) is not int or not 1 <= changes[field] <= maximum):
                raise ValueError(f"{field} 必须为 1～{maximum} 的整数")
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(SETTINGS)
                    saved = {**self.get(pipe), **changes}
                    pipe.multi()
                    pipe.set(SETTINGS, json.dumps(saved))
                    pipe.execute()
                    return saved
                except WatchError:
                    continue
