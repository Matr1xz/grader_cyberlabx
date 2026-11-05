"""API máy chấm: client POST bài lên /grade, nhận điểm ngay trong response.
Thay cho judge.py (pull bài từ server). Lõi chấm giữ nguyên, xem grade_worker.py.

Chạy:  uvicorn app:app --host 0.0.0.0 --port 5000
Env:   GRADE_QUEUE_WORKERS (mặc định 1), GRADE_QUEUE_MAX_SIZE (100), GRADE_QUEUE_ENQUEUE_TIMEOUT_SECONDS (5),
       GRADE_QUEUE_RESULT_TIMEOUT_SECONDS (240), GRADE_MAX_UPLOAD_MB (2048), GRADE_MAX_UNZIPPED_MB (2048), GRADE_CLEANUP_ARTIFACTS (true),
       GRADER_TOKEN (nếu đặt thì bắt buộc header Authorization: Bearer <token>)
"""
import asyncio
import hmac
import logging
import multiprocessing
import os
import shutil
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse

import grade_worker

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOB_ROOT = os.path.join(BASE_DIR, 'tmp', 'queue_jobs')
PREGRADE_DIR = os.path.join(BASE_DIR, '.local', 'pregrade')
ALLOWED_EXTENSIONS = {'lab', 'zib'}


def _env_int(name, default):
    try:
        value = int(os.getenv(name, default))
        return value if value > 0 else default
    except ValueError:
        return default


WORKERS = _env_int('GRADE_QUEUE_WORKERS', 1)
QUEUE_MAX_SIZE = _env_int('GRADE_QUEUE_MAX_SIZE', 100)
ENQUEUE_TIMEOUT = _env_int('GRADE_QUEUE_ENQUEUE_TIMEOUT_SECONDS', 5)
RESULT_TIMEOUT = _env_int('GRADE_QUEUE_RESULT_TIMEOUT_SECONDS', 240)
MAX_UPLOAD_BYTES = _env_int('GRADE_MAX_UPLOAD_MB', 2048) * 1024 * 1024
CLEANUP = os.getenv('GRADE_CLEANUP_ARTIFACTS', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}
TOKEN = os.getenv('GRADER_TOKEN') or None

os.makedirs(JOB_ROOT, exist_ok=True)

# Mỗi bài một process mới, tự quản lý để kill được khi quá giờ; process chết chỉ hỏng đúng bài đó,
# không có pool dùng chung nên không có trạng thái "bộ chấm hỏng". os.environ/global của lõi không rò sang bài khác.
RUN = threading.BoundedSemaphore(WORKERS)
JOB_THREADS = ThreadPoolExecutor(max_workers=WORKERS + QUEUE_MAX_SIZE)
# Giới hạn số bài đang chạy + đang chờ.
SLOTS = threading.BoundedSemaphore(WORKERS + QUEUE_MAX_SIZE)
_pending = 0
_pending_lock = threading.Lock()

app = FastAPI(title='Labtainer grader API')


def err(status, code, message):
    return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status)


def _release(_future=None):
    global _pending
    with _pending_lock:
        _pending -= 1
    SLOTS.release()


class GradeTimeout(Exception):
    pass


def _child(conn, *args):
    os.setsid()  # process group riêng để _run_job giết cả bash/tshark/sqlite3... con cháu
    try:
        conn.send(('ok', grade_worker.grade(*args)))
    except (Exception, SystemExit) as e:  # lõi gọi sys.exit(1) khi cấu hình lab lỗi
        conn.send(('exc', repr(e)))
    finally:
        conn.close()


def _run_job(deadline, job_dir, *args):
    """Chạy trong thread: chờ chỗ chạy, chấm trong process riêng, kill khi quá giờ, luôn nhả slot + dọn job_dir."""
    try:
        if not RUN.acquire(timeout=max(0, deadline - time.monotonic())):
            raise GradeTimeout()
        try:
            recv, send = multiprocessing.Pipe(False)
            proc = multiprocessing.Process(target=_child, args=(send, *args), daemon=True)
            proc.start()
            send.close()
            try:
                if not recv.poll(max(0, deadline - time.monotonic())):
                    raise GradeTimeout()
                kind, val = recv.recv()  # EOFError nếu process bị OS giết
            finally:
                # ponytail: process tự setsid/daemonize sẽ thoát group; cần cgroup nếu lab nào làm vậy
                try:
                    os.killpg(proc.pid, signal.SIGKILL)  # luôn kill: cả khi xong đúng giờ vẫn có thể còn cháu sót
                except ProcessLookupError:
                    pass
                if proc.is_alive():  # chưa kịp setsid
                    proc.kill()
                proc.join()
                recv.close()
        finally:
            RUN.release()
        if kind == 'exc':
            raise RuntimeError(val)
        return val
    finally:
        _release()
        shutil.rmtree(job_dir, ignore_errors=True)


@app.get('/health')
def health():
    labs = sum(1 for e in os.scandir(PREGRADE_DIR) if e.is_dir()) if os.path.isdir(PREGRADE_DIR) else 0
    return {'status': 'ok', 'queueSize': _pending, 'workers': WORKERS, 'labs': labs}


@app.post('/grade')
@app.post('/api/grade')
async def grade(
    file: UploadFile | None = File(default=None),
    student_id: str | None = Form(default=None),
    lab_id: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
):
    """multipart/form-data: file=<mã SV>.<tên lab>.lab; tùy chọn student_id, lab_id để đối chiếu danh tính
    (giống judge.py cũ đối chiếu username/question_code từ server)."""
    global _pending
    if TOKEN and not hmac.compare_digest(authorization or '', 'Bearer ' + TOKEN):
        return err(401, 'UNAUTHORIZED', 'Thiếu hoặc sai token xác thực.')
    if file is None:
        return err(400, 'NO_FILE', 'Không tìm thấy file trong request.')
    filename = os.path.basename(file.filename or '')
    if not filename:
        return err(400, 'NO_FILE', 'Chưa chọn file nào.')
    if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in ALLOWED_EXTENSIONS:
        return err(400, 'BAD_EXTENSION', 'Định dạng không hỗ trợ. Vui lòng upload file .lab hoặc .zib')

    if not await asyncio.to_thread(SLOTS.acquire, timeout=ENQUEUE_TIMEOUT):
        return err(503, 'QUEUE_FULL', 'Hệ thống đang bận. Vui lòng thử lại sau ít phút.')
    with _pending_lock:
        _pending += 1

    job_dir = os.path.join(JOB_ROOT, uuid.uuid4().hex)
    os.makedirs(job_dir)
    path = os.path.join(job_dir, filename)
    submitted = False
    try:
        size = 0
        with open(path, 'wb') as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    return err(413, 'FILE_TOO_LARGE', 'File quá lớn.')
                out.write(chunk)

        submitted = True
        deadline = time.monotonic() + RESULT_TIMEOUT
        try:
            status, payload = await asyncio.get_running_loop().run_in_executor(
                JOB_THREADS, _run_job, deadline, job_dir, path, student_id, lab_id, CLEANUP)
        except GradeTimeout:
            logger.error('Grading timeout file=%s (process killed)', filename)
            return err(504, 'TIMEOUT', 'Hết thời gian chờ xử lý. Vui lòng thử lại sau.')
        return JSONResponse(payload, status_code=status)
    except (Exception, SystemExit):  # lõi gọi sys.exit(1) khi cấu hình lab lỗi
        error_id = uuid.uuid4().hex
        logger.exception('Grading failed file=%s error_id=%s', filename, error_id)
        return err(500, 'INTERNAL', f'Lỗi khi xử lý. Mã lỗi: {error_id}. Vui lòng thử lại hoặc liên hệ hỗ trợ.')
    finally:
        if not submitted:
            _release()
            shutil.rmtree(job_dir, ignore_errors=True)



if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=5000)
