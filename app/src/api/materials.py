from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import AnyHttpUrl

from app.src.api.dependencies import get_container, get_user_id
from app.src.container import AppContainer
from app.src.schemas.requests import HexId
from app.src.schemas.responses import (
    ApiErrorEnvelope,
    ApiSuccessEnvelope,
    empty_response,
    success_response,
)

router = APIRouter(prefix="/materials", tags=["materials"])

MATERIAL_UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["files"],
                    "properties": {
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
                        "callback_url": {
                            "type": "string",
                            "format": "uri",
                            "description": "可选回调地址；传入后素材在后台处理并回调最终结果",
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
    operation_id="materials_upload",
    summary="上传通用素材",
    description=(
        "使用 multipart/form-data 批量上传视频或图片。素材按 X-User-ID 隔离，"
        "同一用户下内容相同的文件使用 SHA-256 自动去重。批量中单个文件失败"
        "不会影响其他文件，结果会逐项返回。未传 callback_url 时同步处理并直接"
        "返回结果；传入 callback_url 时先暂存文件、返回任务 ID，再后台处理并回调。"
    ),
    response_description="各素材的上传结果；异步模式下为任务受理结果",
    responses={
        202: {"model": ApiSuccessEnvelope, "description": "异步素材任务已受理"},
        413: {"model": ApiErrorEnvelope, "description": "素材超过大小限制"},
        422: {"model": ApiErrorEnvelope, "description": "素材类型或参数无效"},
    },
    openapi_extra=MATERIAL_UPLOAD_OPENAPI,
)
async def upload_materials(
    request: Request,
    files: Annotated[list[UploadFile], File(description="待上传的视频或图片文件，最多 35 个")],
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
    callback_url: Annotated[
        AnyHttpUrl | None,
        Form(description="可选回调地址；仅支持 HTTP/HTTPS"),
    ] = None,
):
    if callback_url is not None:
        task = await container.materials.stage_many(user_id, files, str(callback_url))
        return success_response(
            {"task_id": task.id, "status": task.status},
            request.state.request_id,
            status_code=202,
        )
    data = await container.materials.save_many(user_id, files)
    return success_response(data, request.state.request_id)


@router.delete(
    "/{material_id}",
    status_code=204,
    summary="删除通用素材",
    description="删除当前用户的素材。素材被排队中或执行中的发布任务引用时不允许删除。",
    response_description="素材已删除；素材不存在时也返回 204",
    responses={
        409: {"model": ApiErrorEnvelope, "description": "素材正在被活动任务使用"},
        422: {"model": ApiErrorEnvelope, "description": "素材 ID 或用户标识无效"},
    },
)
async def delete_material(
    material_id: HexId,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
):
    await container.materials.delete(user_id, material_id)
    return empty_response()
