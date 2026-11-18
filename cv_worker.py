from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
import zxingcpp
from insightface.app import FaceAnalysis

from config import Settings, get_settings
from dahua_client import DahuaClient, DahuaEvent
from database import SyncDatabase
from qr_generator import extract_token
from version import __version__


logger = logging.getLogger(__name__)

METHOD_NAMES = {
    "face": "Распознавание лица",
    "qr_camera": "QR перед камерой",
    "qr_deeplink": "QR через Telegram",
    "button": "Кнопка в Telegram",
    "courier_auto": "Автоматический вход курьера",
    "courier_barcode": "Штрихкод доставки",
}
TAILGATING_WARNING = (
    "⚠️ ВНИМАНИЕ: Зафиксирован вход нескольких человек "
    "(возможен проход постороннего следом)!"
)


@dataclass(frozen=True, slots=True)
class VideoFrame:
    timestamp: float
    image: np.ndarray


@dataclass(frozen=True, slots=True)
class DecodedCode:
    text: str
    format: str


@dataclass(frozen=True, slots=True)
class EntryEvent:
    method: str
    user_id: int | None
    details: str
    expected_people: int = 1
    caption: str | None = None
    keyboard_json: str | None = None


@dataclass(frozen=True, slots=True)
class FaceMatch:
    user_id: int
    person_name: str
    similarity: float


@dataclass(slots=True)
class TempCourierFace:
    user_id: int
    embedding: np.ndarray
    expires_at: float


class FrameCapture:
    def __init__(self, rtsp_url: str, fps: int, pre_roll_seconds: int) -> None:
        self.rtsp_url = rtsp_url
        self.fps = fps
        self._buffer: deque[VideoFrame] = deque(maxlen=pre_roll_seconds * fps)
        self._latest: VideoFrame | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rtsp-capture", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=8.0)
        if self._thread.is_alive():
            logger.error("Поток RTSP не завершился за отведенное время")

    def latest(self) -> VideoFrame | None:
        with self._lock:
            return self._latest

    def pre_roll(self) -> list[VideoFrame]:
        with self._lock:
            return list(self._buffer)

    def _open(self) -> cv2.VideoCapture:
        capture = cv2.VideoCapture(
            self.rtsp_url,
            cv2.CAP_FFMPEG,
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                5000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                5000,
            ],
        )
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def _run(self) -> None:
        failures = 0
        capture = self._open()
        last_buffered_at = 0.0
        while not self._stop_event.is_set():
            if not capture.isOpened():
                logger.warning("RTSP недоступен, повторное подключение через 2 секунды")
                self._stop_event.wait(2.0)
                capture.release()
                capture = self._open()
                continue
            grabbed = capture.grab()
            if grabbed:
                retrieved, image = capture.retrieve()
            else:
                retrieved = False
                image = None
            if not grabbed or not retrieved or image is None:
                failures += 1
                if failures >= 10:
                    logger.warning("Потеряно чтение RTSP, выполняется переподключение")
                    capture.release()
                    self._stop_event.wait(1.0)
                    capture = self._open()
                    failures = 0
                else:
                    self._stop_event.wait(0.05)
                continue
            failures = 0
            now = time.time()
            frame = VideoFrame(now, image)
            with self._lock:
                self._latest = frame
                if now - last_buffered_at >= 1.0 / self.fps:
                    self._buffer.append(frame)
                    last_buffered_at = now
        capture.release()


class MotionDetector:
    def __init__(self) -> None:
        self._previous: np.ndarray | None = None

    def has_motion(self, image: np.ndarray) -> bool:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (320, 240), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (5, 5), 0)
        previous = self._previous
        self._previous = small
        if previous is None:
            return True
        difference = cv2.absdiff(previous, small)
        thresholded = cv2.threshold(difference, 25, 255, cv2.THRESH_BINARY)[1]
        changed_ratio = cv2.countNonZero(thresholded) / thresholded.size
        mean_difference = float(np.mean(difference))
        return changed_ratio >= 0.012 and mean_difference >= 1.8


class FaceEngine:
    def __init__(self) -> None:
        self._application: FaceAnalysis | None = None

    def initialize(self) -> None:
        application = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        application.prepare(ctx_id=-1, det_size=(640, 640))
        self._application = application
        logger.info("InsightFace buffalo_l загружен в процессе cv_worker")

    def analyze(self, image: np.ndarray) -> list[Any]:
        if self._application is None:
            raise RuntimeError("InsightFace не инициализирован")
        return list(self._application.get(image))


class BarcodeDecoder:
    def __init__(self) -> None:
        self._qr_detector = cv2.QRCodeDetector()

    @staticmethod
    def _format_name(value: Any) -> str:
        return str(value).split(".")[-1].replace("_", "").replace("-", "").upper()

    def decode(self, image: np.ndarray) -> list[DecodedCode]:
        decoded: list[DecodedCode] = []
        try:
            results = zxingcpp.read_barcodes(image)
        except (RuntimeError, ValueError) as exc:
            logger.warning("Ошибка zxing-cpp: %s", exc)
            results = []
        for result in results:
            text = getattr(result, "text", "")
            if text:
                decoded.append(
                    DecodedCode(text=text.strip(), format=self._format_name(result.format))
                )
        if decoded:
            return decoded
        try:
            detected, texts, _, _ = self._qr_detector.detectAndDecodeMulti(image)
        except cv2.error:
            detected = False
            texts = ()
        if detected:
            for text in texts:
                if text:
                    decoded.append(DecodedCode(text=text.strip(), format="QRCODE"))
        if decoded:
            return decoded
        text, _, _ = self._qr_detector.detectAndDecode(image)
        if text:
            decoded.append(DecodedCode(text=text.strip(), format="QRCODE"))
        return decoded


def _inline_keyboard(rows: list[list[tuple[str, str]]]) -> str:
    keyboard = {
        "inline_keyboard": [
            [{"text": text, "callback_data": callback_data} for text, callback_data in row]
            for row in rows
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


UNLOAD_KEYBOARD = _inline_keyboard(
    [[("⏳ Режим разгрузки (Еще 10 мин)", "courier_unload:10")]]
)
DOOR_KEYBOARD = _inline_keyboard([[("🔓 Открыть дверь", "unlock_door")]])
RING_KEYBOARD = _inline_keyboard(
    [
        [("🔓 Открыть дверь", "unlock_door")],
        [("🚛 Открыть + Разгрузка 10 мин", "unlock_unload:10")],
    ]
)


class CVWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = SyncDatabase(settings.database_path)
        self.dahua = DahuaClient(
            settings.dahua_ip, settings.dahua_user, settings.dahua_password
        )
        self.capture = FrameCapture(settings.rtsp_url, settings.rtsp_fps, settings.pre_roll_seconds)
        self.motion = MotionDetector()
        self.face_engine = FaceEngine()
        self.barcode_decoder = BarcodeDecoder()
        self._face_inference_lock = asyncio.Lock()
        self._unlock_lock = asyncio.Lock()
        self._courier_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._face_matrix = np.empty((0, 512), dtype=np.float32)
        self._face_user_ids: list[int] = []
        self._face_names: list[str] = []
        self._face_cooldowns: dict[int, float] = {}
        self._last_unlock_ts = 0.0
        self._last_analyzed_frame_ts = 0.0
        self._last_barcode_analysis = 0.0
        self._last_motion_db = 0.0
        self._last_person_db = 0.0
        self._last_faces: list[Any] = []
        self._last_faces_frame_ts = 0.0
        self._last_seen_codes: dict[str, float] = {}
        self._temp_courier_faces: list[TempCourierFace] = []
        self._last_call_monotonic = 0.0
        self._last_ring_notification = 0.0

    async def run(self) -> None:
        await asyncio.to_thread(self.db.init)
        state = await asyncio.to_thread(self.db.get_door_state)
        self._last_unlock_ts = float(state["last_unlock_ts"])
        await asyncio.to_thread(self.face_engine.initialize)
        await self.reload_faces()
        self.capture.start()
        logger.info("CV worker запущен, версия %s", __version__)
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._analysis_loop())
                task_group.create_task(self._face_tasks_loop())
                task_group.create_task(self._ipc_loop())
                task_group.create_task(self._events_loop())
        finally:
            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self.capture.stop()
            await self.dahua.close()
            logger.info("CV worker остановлен")

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "Фоновая задача записи завершилась с ошибкой",
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    async def _infer_faces(self, image: np.ndarray) -> list[Any]:
        async with self._face_inference_lock:
            return await asyncio.to_thread(self.face_engine.analyze, image)

    async def reload_faces(self) -> None:
        rows = await asyncio.to_thread(self.db.get_active_face_embeddings)
        vectors: list[np.ndarray] = []
        user_ids: list[int] = []
        names: list[str] = []
        for row in rows:
            vector = np.frombuffer(row["embedding"], dtype=np.float32).reshape(512)
            norm = float(np.linalg.norm(vector))
            if vector.size != 512 or norm == 0.0 or not np.isfinite(vector).all():
                logger.warning("Пропущен поврежденный эмбеддинг id=%s", row["id"])
                continue
            vectors.append(vector / norm)
            user_ids.append(int(row["user_id"]))
            names.append(str(row["person_name"]))
        matrix = np.stack(vectors).astype(np.float32) if vectors else np.empty((0, 512), dtype=np.float32)
        self._face_matrix = matrix
        self._face_user_ids = user_ids
        self._face_names = names
        logger.info("Загружено активных эмбеддингов: %d", len(user_ids))

    def _match_resident(self, embedding: np.ndarray) -> FaceMatch | None:
        if self._face_matrix.shape[0] == 0:
            return None
        vector = np.asarray(embedding, dtype=np.float32).reshape(512)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return None
        vector /= norm
        similarities = self._face_matrix @ vector
        index = int(np.argmax(similarities))
        return FaceMatch(
            user_id=self._face_user_ids[index],
            person_name=self._face_names[index],
            similarity=float(similarities[index]),
        )

    def _unlock_ready(self) -> bool:
        return time.time() - self._last_unlock_ts >= self.settings.door_unlock_cooldown_seconds

    async def unlock_door(self, event: EntryEvent) -> bool:
        async with self._unlock_lock:
            if not self._unlock_ready():
                return False
            pre_roll = self.capture.pre_roll()
            await self.dahua.unlock_door()
            self._last_unlock_ts = time.time()
            try:
                await asyncio.to_thread(self.db.record_unlock, self._last_unlock_ts)
            except Exception as exc:
                logger.error("Не удалось сохранить время открытия двери: %s", exc)
            self._spawn(self._record_entry(pre_roll, event))
            logger.info("Открытие: method=%s user_id=%s details=%s", event.method, event.user_id, event.details)
            return True

    async def _analysis_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self._analyze_latest_frame()
            except Exception as exc:
                logger.exception("Ошибка цикла анализа кадра: %s", exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.01, self.settings.face_analysis_interval - elapsed))

    async def _analyze_latest_frame(self) -> None:
        frame = self.capture.latest()
        if frame is None or time.time() - frame.timestamp > 2.0:
            return
        if frame.timestamp <= self._last_analyzed_frame_ts:
            return
        self._last_analyzed_frame_ts = frame.timestamp
        if not self.motion.has_motion(frame.image):
            return
        now = time.monotonic()
        if now - self._last_motion_db >= 0.5:
            self._last_motion_db = now
            await asyncio.to_thread(self.db.record_motion)
        faces = await self._infer_faces(frame.image)
        self._last_faces = faces
        self._last_faces_frame_ts = frame.timestamp
        if faces:
            if now - self._last_person_db >= 0.5:
                self._last_person_db = now
                await asyncio.to_thread(self.db.record_person, len(faces), frame.timestamp)
            await self._process_face_access(faces)
        if now - self._last_barcode_analysis >= 0.4:
            self._last_barcode_analysis = now
            codes = await asyncio.to_thread(self.barcode_decoder.decode, frame.image)
            await self._process_codes(codes)

    async def _process_face_access(self, faces: Iterable[Any]) -> None:
        now = time.monotonic()
        self._temp_courier_faces = [
            item for item in self._temp_courier_faces if item.expires_at > now
        ]
        for face in faces:
            embedding = np.asarray(face.normed_embedding, dtype=np.float32).reshape(512)
            match = self._match_resident(embedding)
            if match is not None and match.similarity >= self.settings.face_similarity_threshold:
                last_attempt = self._face_cooldowns.get(match.user_id, 0.0)
                if now - last_attempt < self.settings.face_cooldown_seconds or not self._unlock_ready():
                    continue
                self._face_cooldowns[match.user_id] = now
                event = EntryEvent(
                    method="face",
                    user_id=match.user_id,
                    details=f"{match.person_name}, сходство {match.similarity:.3f}",
                )
                await self.unlock_door(event)
                return
            courier = self._match_temp_courier(embedding, now)
            if courier is not None and self._unlock_ready():
                courier.expires_at = now + self.settings.courier_face_ttl_seconds
                event = EntryEvent(
                    method="courier_auto",
                    user_id=courier.user_id,
                    details="Курьер узнан по временной памяти лица",
                    caption="📦 Курьер впущен автоматически!",
                    keyboard_json=UNLOAD_KEYBOARD,
                )
                if await self.unlock_door(event):
                    await self._remember_current_courier_face(courier.user_id)
                return

    def _match_temp_courier(self, embedding: np.ndarray, now: float) -> TempCourierFace | None:
        vector = np.asarray(embedding, dtype=np.float32).reshape(512)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return None
        vector /= norm
        best: TempCourierFace | None = None
        best_similarity = 0.55
        for courier in self._temp_courier_faces:
            if courier.expires_at <= now:
                continue
            similarity = float(np.dot(courier.embedding, vector))
            if similarity >= best_similarity:
                best = courier
                best_similarity = similarity
        return best

    async def _process_codes(self, codes: Iterable[DecodedCode]) -> None:
        now = time.monotonic()
        self._last_seen_codes = {
            text: seen for text, seen in self._last_seen_codes.items() if now - seen < 15.0
        }
        for code in codes:
            if not code.text or now - self._last_seen_codes.get(code.text, 0.0) < 3.0:
                continue
            self._last_seen_codes[code.text] = now
            token = extract_token(code.text)
            if token is not None:
                await self._process_door_qr(token)
                continue
            if code.format not in {"CODE128", "EAN13", "DATAMATRIX", "QRCODE"}:
                continue
            if len(code.text) <= 8 or not self._person_is_close():
                continue
            await self._try_courier_entry(
                method="courier_barcode",
                details=f"Штрихкод {code.format}, длина {len(code.text)} символов",
            )

    async def _process_door_qr(self, token: str) -> None:
        if not token or not self._unlock_ready():
            return
        result = await asyncio.to_thread(self.db.consume_qr_token, token)
        if not result.allowed or result.token is None:
            logger.info("QR отклонен: token=%s reason=%s", token, result.reason)
            return
        qr = result.token
        event = EntryEvent(
            method="qr_camera",
            user_id=int(qr["creator_id"]),
            details=f"QR «{qr['label']}», тип {qr['qr_type']}",
        )
        try:
            opened = await self.unlock_door(event)
        except Exception:
            await asyncio.to_thread(
                self.db.refund_qr_use, int(qr["id"]), int(qr["used_count"])
            )
            raise
        if not opened:
            await asyncio.to_thread(
                self.db.refund_qr_use, int(qr["id"]), int(qr["used_count"])
            )

    def _person_is_close(self) -> bool:
        return bool(self._last_faces) and time.time() - self._last_faces_frame_ts <= 3.0

    async def _events_loop(self) -> None:
        async for event in self.dahua.listen_events():
            try:
                await self._handle_dahua_event(event)
            except Exception as exc:
                logger.exception("Ошибка обработки события Dahua: %s", exc)

    async def _handle_dahua_event(self, event: DahuaEvent) -> None:
        if event.code == "VideoMotion" and event.is_start:
            await asyncio.to_thread(self.db.record_motion)
            return
        if not event.is_call or not event.is_start:
            return
        now = time.monotonic()
        if now - self._last_call_monotonic < 2.0:
            return
        self._last_call_monotonic = now
        state = await asyncio.to_thread(self.db.get_door_state)
        wall_now = time.time()
        presence = (
            wall_now - float(state["last_person_ts"]) <= 3.0
            or wall_now - float(state["last_motion_ts"]) <= 3.0
        )
        result = "no_session"
        if presence:
            result = await self._try_courier_entry(
                method="courier_auto",
                details="Курьер нажал физическую кнопку вызова на панели",
            )
        if result == "no_session":
            await self._notify_ring()

    async def _try_courier_entry(self, method: str, details: str) -> str:
        async with self._courier_lock:
            if not self._unlock_ready():
                return "busy"
            session = await asyncio.to_thread(self.db.consume_active_courier_session)
            owner_id: int | None = None
            if session is not None:
                owner_id = int(session["user_id"])
            else:
                unload = await asyncio.to_thread(self.db.get_active_unload_session)
                if unload is not None:
                    owner_id = int(unload["user_id"])
            if owner_id is None:
                return "no_session"
            event = EntryEvent(
                method=method,
                user_id=owner_id,
                details=details,
                caption="📦 Курьер впущен автоматически!",
                keyboard_json=UNLOAD_KEYBOARD,
            )
            try:
                opened = await self.unlock_door(event)
            except Exception as exc:
                if session is not None:
                    await asyncio.to_thread(
                        self.db.set_courier_session_active, int(session["id"]), True
                    )
                await self._enqueue_text(owner_id, f"⚠️ Не удалось открыть дверь курьеру: {exc}")
                logger.exception("Ошибка автоматического открытия курьеру: %s", exc)
                return "failed"
            if not opened:
                if session is not None:
                    await asyncio.to_thread(
                        self.db.set_courier_session_active, int(session["id"]), True
                    )
                return "busy"
            await self._remember_current_courier_face(owner_id)
            return "entered"

    async def _remember_current_courier_face(self, owner_id: int) -> None:
        if not self._last_faces or time.time() - self._last_faces_frame_ts > 5.0:
            return
        face = max(self._last_faces, key=self._face_area)
        embedding = np.asarray(face.normed_embedding, dtype=np.float32).reshape(512)
        norm = float(np.linalg.norm(embedding))
        if norm == 0.0 or not np.isfinite(embedding).all():
            return
        embedding = embedding / norm
        now = time.monotonic()
        self._temp_courier_faces = [
            item
            for item in self._temp_courier_faces
            if item.expires_at > now and item.user_id != owner_id
        ]
        self._temp_courier_faces.append(
            TempCourierFace(
                user_id=owner_id,
                embedding=embedding,
                expires_at=now + self.settings.courier_face_ttl_seconds,
            )
        )

    @staticmethod
    def _face_area(face: Any) -> float:
        bbox = np.asarray(face.bbox, dtype=np.float32).reshape(4)
        return float(max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1]))

    async def _notify_ring(self) -> None:
        now = time.monotonic()
        if now - self._last_ring_notification < 5.0:
            return
        self._last_ring_notification = now
        recipients = await asyncio.to_thread(self.db.get_notification_chat_ids)
        if self.settings.log_chat_id is not None:
            recipients.append(self.settings.log_chat_id)
        recipients = list(dict.fromkeys(recipients))
        if not recipients:
            logger.warning("Звонок в панель: нет получателей уведомления")
            return
        try:
            image = await self.dahua.snapshot()
            path = self.settings.temp_media_dir / f"ring_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.jpg"
            await asyncio.to_thread(path.write_bytes, image)
            for chat_id in recipients:
                await asyncio.to_thread(
                    self.db.enqueue_outbox,
                    chat_id,
                    "photo",
                    "🔔 Звонок в домофон",
                    str(path),
                    RING_KEYBOARD,
                )
        except Exception as exc:
            logger.exception("Не удалось отправить snapshot звонящего: %s", exc)
            for chat_id in recipients:
                await self._enqueue_text(chat_id, f"🔔 Звонок в домофон. Snapshot недоступен: {exc}")

    async def _face_tasks_loop(self) -> None:
        while True:
            tasks = await asyncio.to_thread(self.db.get_pending_face_tasks, 10)
            for task in tasks:
                await self._process_face_task(task)
            await asyncio.sleep(1.0)

    async def _process_face_task(self, task: dict[str, Any]) -> None:
        task_id = int(task["id"])
        user_id = int(task["user_id"])
        image_path = Path(task["image_path"])
        try:
            image_data = await asyncio.to_thread(np.fromfile, image_path, np.uint8)
            image = cv2.imdecode(image_data, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("Не удалось декодировать изображение")
            faces = await self._infer_faces(image)
            if not faces:
                raise ValueError("На фотографии не обнаружено лицо")
            if len(faces) != 1:
                raise ValueError("На фотографии должно быть ровно одно лицо")
            embedding = np.asarray(faces[0].normed_embedding, dtype=np.float32)
            message = f"Лицо «{task['person_name']}» успешно зарегистрировано"
            await asyncio.to_thread(self.db.complete_face_task, task_id, embedding, message)
            await self.reload_faces()
            await self._enqueue_text(user_id, f"✅ {message}")
        except Exception as exc:
            message = f"Ошибка регистрации лица: {exc}"
            await asyncio.to_thread(self.db.fail_face_task, task_id, message)
            await self._enqueue_text(user_id, f"⚠️ {message}")
            logger.warning("Задача лица id=%s завершена ошибкой: %s", task_id, exc)
        finally:
            try:
                await asyncio.to_thread(image_path.unlink, True)
            except OSError as exc:
                logger.warning("Не удалось удалить %s: %s", image_path, exc)

    async def _ipc_loop(self) -> None:
        while True:
            commands = await asyncio.to_thread(self.db.get_pending_ipc_commands, 20)
            for command in commands:
                await self._process_ipc_command(command)
            await asyncio.sleep(0.5)

    async def _process_ipc_command(self, command: dict[str, Any]) -> None:
        command_id = int(command["id"])
        chat_id: int | None = None
        try:
            payload = json.loads(command["payload"])
            if not isinstance(payload, dict):
                raise ValueError("payload IPC-команды должен быть JSON-объектом")
            if payload.get("chat_id") is not None:
                chat_id = int(payload["chat_id"])
            elif payload.get("user_id") is not None:
                chat_id = int(payload["user_id"])
            command_type = command["cmd_type"]
            if command_type == "unlock_door":
                user_id = int(payload["user_id"])
                method = str(payload.get("method", "button"))
                if method not in METHOD_NAMES:
                    raise ValueError(f"Недопустимый метод открытия: {method}")
                qr_refund: tuple[int, int] | None = None
                courier_session_id: int | None = None
                if method == "qr_deeplink":
                    qr_id = int(payload["qr_id"])
                    expected_used_count = int(payload["expected_used_count"])
                    if qr_id <= 0 or expected_used_count <= 0:
                        raise ValueError("Некорректные данные возврата QR-использования")
                    qr_refund = (qr_id, expected_used_count)
                    if payload.get("courier_session_id") is not None:
                        courier_session_id = int(payload["courier_session_id"])
                        if courier_session_id <= 0:
                            raise ValueError("Некорректный ID курьерской сессии")
                entry = EntryEvent(
                    method=method,
                    user_id=user_id,
                    details=str(payload.get("details", "Команда из Telegram")),
                )
                try:
                    opened = await self.unlock_door(entry)
                except Exception:
                    if qr_refund is not None:
                        await asyncio.to_thread(self.db.refund_qr_use, *qr_refund)
                    raise
                if not opened and qr_refund is not None:
                    await asyncio.to_thread(self.db.refund_qr_use, *qr_refund)
                if opened and courier_session_id is not None:
                    await asyncio.to_thread(
                        self.db.set_courier_session_active, courier_session_id, False
                    )
                if not opened and chat_id is not None:
                    remaining = max(
                        0,
                        int(self.settings.door_unlock_cooldown_seconds - (time.time() - self._last_unlock_ts)) + 1,
                    )
                    await self._enqueue_text(chat_id, f"⏳ Дверь недавно открывалась. Повторите через {remaining} с")
            elif command_type == "capture_live_clip":
                if chat_id is None:
                    raise ValueError("Для capture_live_clip необходим chat_id")
                await self._capture_live_clip(chat_id)
            elif command_type == "enable_unload_mode":
                user_id = int(payload["user_id"])
                until = await asyncio.to_thread(self.db.enable_unload_mode, user_id, 600)
                if chat_id is not None:
                    await self._enqueue_text(
                        chat_id,
                        f"⏳ Режим разгрузки включен до {time.strftime('%H:%M:%S', time.localtime(until))}",
                    )
            elif command_type == "reload_faces":
                await self.reload_faces()
            else:
                raise ValueError(f"Неизвестная IPC-команда: {command_type}")
        except Exception as exc:
            logger.exception("Ошибка IPC-команды id=%s: %s", command_id, exc)
            if chat_id is not None:
                await self._enqueue_text(chat_id, f"⚠️ Команда не выполнена: {exc}")
        finally:
            await asyncio.to_thread(self.db.mark_ipc_command_processed, command_id)

    async def _capture_live_clip(self, chat_id: int) -> None:
        latest = self.capture.latest()
        after = latest.timestamp if latest is not None else 0.0
        frames = await self._collect_frames(self.settings.live_clip_seconds, after)
        if not frames:
            raise RuntimeError("Нет свежих кадров с RTSP-потока")
        path = self.settings.temp_media_dir / f"live_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.mp4"
        await asyncio.to_thread(self._encode_frames, frames, path)
        await asyncio.to_thread(
            self.db.enqueue_outbox,
            chat_id,
            "video",
            "📹 Свежая трансляция с панели",
            str(path),
            DOOR_KEYBOARD,
        )

    async def _record_entry(self, pre_roll: list[VideoFrame], event: EntryEvent) -> None:
        after = pre_roll[-1].timestamp if pre_roll else 0.0
        post_roll = await self._collect_frames(self.settings.post_roll_seconds, after)
        identities = await self._post_roll_identities(post_roll)
        tailgating = (
            event.expected_people == 1
            and len(identities) >= 2
            and any(not known for _, known in identities)
        )
        people_entered = max(1, len(identities))
        path = self.settings.temp_media_dir / f"pass_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.mp4"
        video_path: str | None = None
        frames = pre_roll + post_roll
        if frames:
            try:
                await asyncio.to_thread(self._encode_frames, frames, path)
                video_path = str(path)
            except Exception as exc:
                logger.exception("Не удалось собрать видео входа: %s", exc)
        log_id = await asyncio.to_thread(
            self.db.add_access_log,
            event.method,
            event.user_id,
            event.details,
            people_entered,
            tailgating,
            video_path,
        )
        caption = event.caption or "🚪 Зафиксирован вход"
        caption = f"{caption}\nКто: {event.details}\nМетод: {METHOD_NAMES[event.method]}"
        if tailgating:
            caption = f"{caption}\n{TAILGATING_WARNING}"
        recipients: list[int] = []
        if self.settings.log_chat_id is not None:
            recipients.append(self.settings.log_chat_id)
        if event.user_id is not None:
            recipients.append(event.user_id)
        recipients = list(dict.fromkeys(recipients))
        for chat_id in recipients:
            await asyncio.to_thread(
                self.db.enqueue_outbox,
                chat_id,
                "video" if video_path is not None else "text",
                caption,
                video_path,
                event.keyboard_json,
            )
        if not recipients and path.exists():
            await asyncio.to_thread(path.unlink, True)
        logger.info(
            "Событие входа записано: log_id=%s people=%s tailgating=%s video=%s",
            log_id,
            people_entered,
            tailgating,
            video_path,
        )

    async def _collect_frames(self, duration: int, after_timestamp: float) -> list[VideoFrame]:
        frames: list[VideoFrame] = []
        deadline = time.monotonic() + duration
        last_timestamp = after_timestamp
        sample_interval = 1.0 / self.settings.rtsp_fps
        next_sample_timestamp = after_timestamp + sample_interval
        while time.monotonic() < deadline:
            frame = self.capture.latest()
            if frame is not None and frame.timestamp > last_timestamp:
                last_timestamp = frame.timestamp
                if frame.timestamp >= next_sample_timestamp:
                    frames.append(frame)
                    next_sample_timestamp = frame.timestamp + sample_interval
            await asyncio.sleep(min(sample_interval, max(0.001, deadline - time.monotonic())))
        return frames

    async def _post_roll_identities(self, frames: list[VideoFrame]) -> list[tuple[np.ndarray, bool]]:
        identities: list[tuple[np.ndarray, bool]] = []
        stride = max(1, self.settings.rtsp_fps // 2)
        for index, frame in enumerate(frames):
            if index % stride != 0:
                continue
            faces = await self._infer_faces(frame.image)
            for face in faces:
                embedding = np.asarray(face.normed_embedding, dtype=np.float32).reshape(512)
                norm = float(np.linalg.norm(embedding))
                if norm == 0.0 or not np.isfinite(embedding).all():
                    continue
                embedding = embedding / norm
                match = self._match_resident(embedding)
                known = match is not None and match.similarity >= self.settings.face_similarity_threshold
                existing_index = -1
                best_similarity = 0.60
                for identity_index, (existing, _) in enumerate(identities):
                    similarity = float(np.dot(existing, embedding))
                    if similarity >= best_similarity:
                        existing_index = identity_index
                        best_similarity = similarity
                if existing_index >= 0:
                    old_embedding, old_known = identities[existing_index]
                    identities[existing_index] = ((old_embedding + embedding) / 2.0, old_known or known)
                else:
                    identities.append((embedding, known))
        return identities

    def _encode_frames(self, frames: list[VideoFrame], path: Path) -> None:
        if not frames:
            raise ValueError("Нельзя создать видео без кадров")
        height, width = frames[0].image.shape[:2]
        width -= width % 2
        height -= height % 2
        if width < 2 or height < 2:
            raise ValueError("Некорректный размер кадра")
        path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(self.settings.rtsp_fps),
            "-i",
            "pipe:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            str(path),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            if process.stdin is None or process.stderr is None:
                raise RuntimeError("Не удалось открыть каналы ffmpeg")
            for frame in frames:
                image = frame.image
                if image.shape[0] != height or image.shape[1] != width:
                    image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
                process.stdin.write(np.ascontiguousarray(image).tobytes())
            process.stdin.close()
            errors = process.stderr.read().decode("utf-8", errors="replace")
            return_code = process.wait(timeout=60)
        except Exception:
            process.kill()
            process.wait()
            raise
        if return_code != 0:
            raise RuntimeError(f"ffmpeg завершился с кодом {return_code}: {errors[-1000:]}")
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("ffmpeg не создал непустой MP4-файл")

    async def _enqueue_text(self, chat_id: int, text: str) -> None:
        await asyncio.to_thread(self.db.enqueue_outbox, chat_id, "text", text, None, None)


async def main() -> None:
    settings = get_settings()
    worker = CVWorker(settings)
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cv2.setNumThreads(max(1, int(os.getenv("OPENCV_THREADS", "1"))))
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Остановка по Ctrl+C")
