"""桌面端输入边界；任务只接受目标标识，不接受任意下载地址。"""

import re
from datetime import date
from typing import List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["url", "items", "author", "mix", "collection", "likes", "folders"]
    id: str = ""
    ids: List[str] = Field(default_factory=list, max_length=5000)
    url: str = ""

    @model_validator(mode="after")
    def validate_target(self):
        if self.kind == "url":
            parsed = urlparse(self.url)
            if parsed.scheme != "https" or parsed.hostname not in {
                "www.douyin.com", "douyin.com", "v.douyin.com",
            } or parsed.username or parsed.password or parsed.port:
                raise ValueError("请输入抖音 HTTPS 链接")
        if self.kind == "items":
            if not self.ids or any(not re.fullmatch(r"\d{5,24}", x) for x in self.ids):
                raise ValueError("请选择有效作品")
            self.ids = list(dict.fromkeys(self.ids))
        if self.kind == "author" and not re.fullmatch(r"[A-Za-z0-9_-]{5,160}", self.id):
            raise ValueError("作者标识无效")
        if self.kind == "mix" and not re.fullmatch(r"\d{5,24}", self.id):
            raise ValueError("合集标识无效")
        if self.kind == "collection" and self.id and not re.fullmatch(r"\d{5,24}", self.id):
            raise ValueError("收藏夹标识无效")
        return self


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=0, le=100000)
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    media_type: Literal["all", "video", "gallery"] = "all"
    quality: Literal["highest", "lowest", "original", "1080p", "720p", "540p"] = "highest"
    cleanup: bool = True

    @model_validator(mode="after")
    def validate_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Target
    options: Options = Field(default_factory=Options)


class PageRequest(BaseModel):
    target: Target
    cursor: str = Field(default="0", pattern=r"^\d{1,24}$")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    proxy: str = ""
    thread: int = Field(default=2, ge=1, le=4)
    cleanup: bool = True

    @model_validator(mode="after")
    def validate_proxy(self):
        if self.proxy:
            parsed = urlparse(self.proxy)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("代理必须为 HTTP/HTTPS 地址")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("不支持含凭据或查询参数的代理地址")
            if parsed.path not in {"", "/"}:
                raise ValueError("代理不能包含路径")
            parsed.port
        return self
