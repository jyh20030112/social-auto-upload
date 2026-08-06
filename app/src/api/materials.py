from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from app.src.api.dependencies import get_container
from app.src.container import AppContainer
from app.src.schemas.requests import AccountName, HexId
from app.src.schemas.responses import (
    ApiErrorEnvelope,
    ApiSuccessEnvelope,
    empty_response,
    success_response,
)


router = APIRouter(prefix="/materials", tags=["douyin"])

MATERIAL_UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["account", "files"],
                    "properties": {
                        "account": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9_-]{1,64}$",
                            "description": "素材所属的抖音账号名称",
                        },
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 35,
                            "description": "点击选择一个或多个本地视频/图片文件",
                            "items": {
                                "type": "string",
                                "format": "binary",
                            },
                        },
                    },
                },
                "encoding": {
                    "files": {
                        "contentType": "application/octet-stream",
                        "style": "form",
                        "explode": True,
                    }
                },
            }
        },
    }
}


@router.post(
    "",
    response_model=ApiSuccessEnvelope,
    summary="批量上传抖音素材",
    description=(
        "使用 multipart/form-data 批量上传视频或图片。素材按 account 隔离，"
        "同一账号下内容相同的文件使用 SHA-256 自动去重。批量中单个文件失败"
        "不会影响其他文件，结果会逐项返回。"
    ),
    response_description="各素材的上传结果",
    responses={
        413: {"model": ApiErrorEnvelope, "description": "素材超过大小限制"},
        422: {"model": ApiErrorEnvelope, "description": "素材类型或参数无效"},
    },
    openapi_extra=MATERIAL_UPLOAD_OPENAPI,
)
async def upload_materials(
    request: Request,
    account: Annotated[AccountName, Form(description="素材所属的抖音账号名称")],
    files: Annotated[list[UploadFile], File(description="待上传的视频或图片文件，最多 35 个")],
    container: Annotated[AppContainer, Depends(get_container)],
):
    data = await container.materials.save_many(account, files)
    return success_response(data, request.state.request_id)


@router.delete(
    "/{material_id}",
    status_code=204,
    summary="删除抖音素材",
    description="删除指定账号的素材。素材被排队中或执行中的发布任务引用时不允许删除。",
    response_description="素材已删除；素材不存在时也返回 204",
    responses={
        409: {"model": ApiErrorEnvelope, "description": "素材正在被活动任务使用"},
        422: {"model": ApiErrorEnvelope, "description": "素材 ID 或账号参数无效"},
    },
)
async def delete_material(
    material_id: HexId,
    account: Annotated[AccountName, Query(description="素材所属的抖音账号名称")],
    container: Annotated[AppContainer, Depends(get_container)],
):
    await container.materials.delete(account, material_id)
    return empty_response()
