from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

AccountName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$", strip_whitespace=True),
    Field(description="抖音账号名称，1—64 位，只允许字母、数字、下划线和连字符"),
]
HexId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{32}$"),
    Field(description="32 位小写十六进制 UUID，无连字符"),
]


def _normalize_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    for tag in tags:
        cleaned = tag.strip().lstrip("#")
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _validate_schedule(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule 必须包含时区，例如 2026-08-07T20:30:00+08:00")
    if value.astimezone(timezone.utc) <= datetime.now(timezone.utc) + timedelta(hours=2):
        raise ValueError("schedule 必须至少晚于当前时间 2 小时")
    return value


class LoginRequest(BaseModel):
    account: AccountName
    cookie: str = Field(
        min_length=2,
        max_length=8 * 1024 * 1024,
        description=(
            "支持三种字符串格式：浏览器请求头中的 `name=value; name2=value2` "
            "原始 Cookie、Cookie-Editor 导出数组的 JSON 字符串，或 Playwright "
            "storage_state 对象的 JSON 字符串"
        ),
        examples=["sessionid=example; sid_tt=example"],
    )
    callback_url: AnyHttpUrl | None = Field(
        default=None,
        description="可选回调地址；提供后接口立即返回任务 ID，结果通过 HTTP POST 回调",
    )


class CheckRequest(BaseModel):
    account: AccountName


class VideoPublishRequest(BaseModel):
    account: AccountName
    video_material_id: HexId = Field(description="通过素材上传接口获得的视频素材 ID")
    title: str = Field(min_length=1, max_length=30, description="视频标题，最多 30 个字符")
    description: str = Field(default="", description="视频文案/描述")
    tags: list[str] = Field(
        default_factory=list,
        description="话题标签列表，可以包含 # 前缀，服务端会自动去除和去重",
    )
    schedule: datetime | None = Field(
        default=None,
        description="定时发布时间；必须包含时区且至少晚于当前时间 2 小时",
        examples=["2026-08-08T20:30:00+08:00"],
    )
    thumbnail_landscape_material_id: HexId | None = Field(
        default=None,
        description="可选的横版封面图片素材 ID",
    )
    thumbnail_portrait_material_id: HexId | None = Field(
        default=None,
        description="可选的竖版封面图片素材 ID",
    )
    product_link: str = Field(default="", description="可选的抖音商品链接，需与 product_title 同时提供")
    product_title: str = Field(
        default="",
        max_length=10,
        description="商品短标题，最多 10 个字符，需与 product_link 同时提供",
    )
    declaration: str | None = Field(
        default=None,
        description="可选的抖音自主声明，必须与平台界面中的声明文案完全一致",
    )
    callback_url: AnyHttpUrl | None = Field(
        default=None,
        description="可选回调地址；提供后异步推送验证码等待和最终结果",
    )

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, value: datetime | None) -> datetime | None:
        return _validate_schedule(value)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title 不能为空")
        return value

    @field_validator("declaration")
    @classmethod
    def normalize_declaration(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_product_pair(self) -> VideoPublishRequest:
        if bool(self.product_link.strip()) != bool(self.product_title.strip()):
            raise ValueError("product_link 和 product_title 必须同时提供或同时为空")
        return self


class NotePublishRequest(BaseModel):
    account: AccountName
    image_material_ids: list[HexId] = Field(
        min_length=1,
        max_length=35,
        description="图文图片素材 ID 列表，按发布顺序传入 1—35 个",
    )
    title: str = Field(min_length=1, max_length=20, description="图文标题，最多 20 个字符")
    note: str = Field(default="", max_length=1000, description="图文正文，最多 1000 个字符")
    tags: list[str] = Field(
        default_factory=list,
        description="话题标签列表，可以包含 # 前缀，服务端会自动去除和去重",
    )
    schedule: datetime | None = Field(
        default=None,
        description="定时发布时间；必须包含时区且至少晚于当前时间 2 小时",
        examples=["2026-08-08T20:30:00+08:00"],
    )
    bgm: str = Field(default="", description="可选的抖音背景音乐搜索关键词")
    callback_url: AnyHttpUrl | None = Field(
        default=None,
        description="可选回调地址；提供后异步推送验证码等待和最终结果",
    )

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, value: datetime | None) -> datetime | None:
        return _validate_schedule(value)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title 不能为空")
        return value


class VerificationCodeRequest(BaseModel):
    account: AccountName
    code: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9]{4,8}$"),
        Field(description="抖音短信验证码，4—8 位数字", examples=["123456"]),
    ]


class CancelTaskRequest(BaseModel):
    account: AccountName
