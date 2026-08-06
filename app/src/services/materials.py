from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.domain.states import TaskOperation
from app.src.persistence.repositories import Repository
from app.src.persistence.tables import MaterialRecord
from uploader.base_video import BaseVideoUploader


class MaterialTooLargeError(ValueError):
    pass


class MaterialService:
    VIDEO_EXTENSIONS = BaseVideoUploader.SUPPORTED_VIDEO_EXTENSIONS
    IMAGE_EXTENSIONS = BaseVideoUploader.SUPPORTED_IMAGE_EXTENSIONS

    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository

    def _classify(self, original_name: str) -> tuple[str, str, int]:
        extension = Path(original_name).suffix.lower()
        if extension in self.VIDEO_EXTENSIONS:
            return extension, "video", self.settings.video_max_bytes
        if extension in self.IMAGE_EXTENSIONS:
            return extension, "image", self.settings.image_max_bytes
        raise ApiError(
            422,
            "UNSUPPORTED_MATERIAL_TYPE",
            f"不支持的素材扩展名: {extension or '<none>'}",
        )

    @staticmethod
    def _copy_and_hash(source, destination: Path, limit: int) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with destination.open("wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise MaterialTooLargeError(f"文件超过大小限制 {limit} bytes")
                digest.update(chunk)
                output.write(chunk)
        return size, digest.hexdigest()

    async def save_one(self, account: str, upload: UploadFile) -> dict:
        original_name = Path(upload.filename or "").name
        extension, kind, size_limit = self._classify(original_name)

        temporary_path = self.settings.temporary_dir / f"{uuid4().hex}.upload"
        try:
            await upload.seek(0)
            size, sha256 = await asyncio.to_thread(
                self._copy_and_hash,
                upload.file,
                temporary_path,
                size_limit,
            )
            existing = await self.repository.get_material_by_hash(account, sha256)
            if existing is not None and Path(existing.stored_path).exists():
                return self.serialize(existing, deduplicated=True)

            account_dir = self.settings.materials_dir / account
            account_dir.mkdir(parents=True, exist_ok=True)
            stored_path = account_dir / f"{sha256}{extension}"
            os.replace(temporary_path, stored_path)
            record = await self.repository.add_material(
                MaterialRecord(
                    id=uuid4().hex,
                    account=account,
                    original_name=original_name,
                    stored_path=str(stored_path),
                    kind=kind,
                    extension=extension,
                    mime_type=upload.content_type,
                    size_bytes=size,
                    sha256=sha256,
                )
            )
            if record.stored_path != str(stored_path) and stored_path.exists():
                stored_path.unlink()
            return self.serialize(record, deduplicated=record.stored_path != str(stored_path))
        except MaterialTooLargeError as exc:
            raise ApiError(413, "MATERIAL_TOO_LARGE", str(exc)) from exc
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    async def save_many(self, account: str, uploads: list[UploadFile]) -> dict:
        if not uploads:
            raise ApiError(422, "MATERIALS_REQUIRED", "至少上传一个素材")
        if len(uploads) > 35:
            raise ApiError(422, "TOO_MANY_MATERIALS", "单次最多上传 35 个素材")
        items: list[dict] = []
        succeeded = 0
        for upload in uploads:
            try:
                material = await self.save_one(account, upload)
                items.append({"success": True, "material": material})
                succeeded += 1
            except ApiError as exc:
                items.append(
                    {
                        "success": False,
                        "filename": upload.filename,
                        "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                    }
                )
            finally:
                await upload.close()
        return {
            "items": items,
            "succeeded_count": succeeded,
            "failed_count": len(items) - succeeded,
        }

    async def stage_many(
        self,
        account: str,
        uploads: list[UploadFile],
        callback_url: str,
    ):
        if not uploads:
            raise ApiError(422, "MATERIALS_REQUIRED", "至少上传一个素材")
        if len(uploads) > 35:
            raise ApiError(422, "TOO_MANY_MATERIALS", "单次最多上传 35 个素材")

        task_id = uuid4().hex
        task_dir = self.settings.task_staging_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        staged_items: list[dict] = []
        try:
            for index, upload in enumerate(uploads):
                original_name = Path(upload.filename or "").name
                try:
                    extension, kind, size_limit = self._classify(original_name)
                    staged_path = task_dir / f"{index:02d}-{uuid4().hex}{extension}"
                    await upload.seek(0)
                    size, _ = await asyncio.to_thread(
                        self._copy_and_hash,
                        upload.file,
                        staged_path,
                        size_limit,
                    )
                    staged_items.append(
                        {
                            "filename": original_name,
                            "content_type": upload.content_type,
                            "kind": kind,
                            "size_bytes": size,
                            "staged_path": str(staged_path),
                        }
                    )
                except MaterialTooLargeError as exc:
                    staged_items.append(
                        {
                            "filename": original_name,
                            "error": {
                                "code": "MATERIAL_TOO_LARGE",
                                "message": str(exc),
                                "details": {},
                            },
                        }
                    )
                except ApiError as exc:
                    staged_items.append(
                        {
                            "filename": original_name,
                            "error": {
                                "code": exc.code,
                                "message": exc.message,
                                "details": exc.details,
                            },
                        }
                    )
                finally:
                    await upload.close()

            return await self.repository.create_task(
                task_id=task_id,
                account=account,
                operation=TaskOperation.UPLOAD_MATERIALS.value,
                payload={
                    "callback_url": callback_url,
                    "staging_dir": str(task_dir),
                    "items": staged_items,
                },
            )
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

    async def process_staged(self, account: str, payload: dict) -> dict:
        results: list[dict] = []
        succeeded = 0
        try:
            for item in payload["items"]:
                if item.get("error"):
                    results.append(
                        {
                            "success": False,
                            "filename": item.get("filename"),
                            "error": item["error"],
                        }
                    )
                    continue
                staged_path = Path(item["staged_path"])
                if not staged_path.exists():
                    results.append(
                        {
                            "success": False,
                            "filename": item["filename"],
                            "error": {
                                "code": "STAGED_FILE_MISSING",
                                "message": "暂存素材文件不存在",
                                "details": {},
                            },
                        }
                    )
                    continue
                with staged_path.open("rb") as source:
                    upload = UploadFile(
                        file=source,
                        filename=item["filename"],
                        headers=Headers({"content-type": item.get("content_type") or "application/octet-stream"}),
                    )
                    try:
                        material = await self.save_one(account, upload)
                        results.append({"success": True, "material": material})
                        succeeded += 1
                    except ApiError as exc:
                        results.append(
                            {
                                "success": False,
                                "filename": item["filename"],
                                "error": {
                                    "code": exc.code,
                                    "message": exc.message,
                                    "details": exc.details,
                                },
                            }
                        )
                    finally:
                        await upload.close()
        finally:
            self.cleanup_staged(payload)
        return {
            "items": results,
            "succeeded_count": succeeded,
            "failed_count": len(results) - succeeded,
        }

    @staticmethod
    def cleanup_staged(payload: dict) -> None:
        staging_dir = payload.get("staging_dir")
        if staging_dir:
            shutil.rmtree(Path(staging_dir), ignore_errors=True)

    async def delete(self, account: str, material_id: str) -> None:
        material = await self.repository.get_material_for_account(material_id, account)
        if material is None:
            return
        if await self.repository.material_has_active_task(material_id):
            raise ApiError(409, "MATERIAL_IN_USE", "素材正在被活动任务使用")

        source = Path(material.stored_path)
        trash = self.settings.trash_dir / f"{uuid4().hex}{material.extension}.deleted"
        moved = False
        if source.exists():
            os.replace(source, trash)
            moved = True
        try:
            await self.repository.delete_material_record(material_id, account)
        except Exception:
            if moved and trash.exists():
                os.replace(trash, source)
            raise
        if trash.exists():
            trash.unlink()

    @staticmethod
    def serialize(record: MaterialRecord, *, deduplicated: bool = False) -> dict:
        return {
            "id": record.id,
            "account": record.account,
            "filename": record.original_name,
            "kind": record.kind,
            "extension": record.extension,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "deduplicated": deduplicated,
        }
