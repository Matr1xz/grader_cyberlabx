"""Chạy trong process con: một bài = một process (xem app.py).
Lõi chấm (instructor_grade + parsing_grade) giữ nguyên; file này chỉ bọc kết quả thành JSON API.
Process riêng cách ly os.environ['LABTAINER_*'], biến global của ResultParser/UniqueCheck và sys.exit().
"""
import io
import os
import zipfile
from hashlib import md5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)  # instructor_grade dùng os.getcwd() để tìm .local/pregrade và ./tmp

import instructor_grade  # noqa: E402
import parsing_grade  # noqa: E402

MAX_UNZIPPED_BYTES = int(os.getenv('GRADE_MAX_UNZIPPED_MB', '2048')) * 1024 * 1024
MAX_ZIP_ENTRIES = int(os.getenv('GRADE_MAX_ZIP_ENTRIES', '20000'))

MSG_INVALID = 'Không thể chấm điểm. File có thể bị lỗi hoặc không đúng định dạng.'
MSG_LAB_NOT_FOUND = 'File không hợp lệ. Không tìm thấy lab tương ứng trong hệ thống.'
MSG_IDENTITY = 'File bị từ chối: mã sinh viên trong file .lab không khớp với tên file hoặc archive bên trong.'
MSG_ZIP_BOMB = 'File quá lớn sau khi giải nén.'


class ZipLimitError(Exception):
    pass


def error(status, code, message, **extra):
    return status, {'error': {'code': code, 'message': message}, **extra}


def check_zip_limits(path):
    """Cộng file_size khai báo của zip cấp 1 + cấp 2 trước khi lõi giải nén.
    zipfile không đọc quá file_size khai báo, nên trần này có hiệu lực thật."""
    total = count = 0

    def add(infos):
        nonlocal total, count
        for info in infos:
            total += info.file_size
            count += 1
        if total > MAX_UNZIPPED_BYTES or count > MAX_ZIP_ENTRIES:
            raise ZipLimitError()

    with zipfile.ZipFile(path) as outer:
        add(outer.infolist())
        for info in outer.infolist():
            if info.filename.lower().endswith('.zip'):
                with zipfile.ZipFile(io.BytesIO(outer.read(info))) as nested:
                    add(nested.infolist())


def skipped_containers(path):
    """Container bị lõi bỏ qua vì watermark lệch (cùng công thức Check_SecondLevel_EmailWatermark_OK).
    Lệch email đã bị validate_submission_identity chặn từ trước nên không xuất hiện ở đây."""
    skipped = []
    with zipfile.ZipFile(path) as outer:
        for info in outer.infolist():
            name = os.path.basename(info.filename)
            if not name.lower().endswith('.zip') or name == 'docs.zip' or '=' not in name:
                continue
            with zipfile.ZipFile(io.BytesIO(outer.read(info))) as z:
                names = set(z.namelist())
                if '.local/.watermark' not in names:
                    continue
                seed = z.read('.local/.seed').decode('utf-8', 'replace').strip() if '.local/.seed' in names else ''
                expected = md5(('%s:LABTAINER_WATERMARK1' % seed).encode('utf-8')).hexdigest()
                if z.read('.local/.watermark').decode('utf-8', 'replace').strip() != expected:
                    skipped.append(name[:-4].rsplit('=', 1)[1])
    return skipped


def to_response(raw, path):
    if isinstance(raw, dict) and raw.get('error') == 'invalid_submission_identity':
        return error(400, 'IDENTITY_MISMATCH', MSG_IDENTITY, detail=raw.get('detail', ''))
    if isinstance(raw, dict) and raw.get('error') == 'invalid_submission_structure':
        return error(400, 'INVALID_LAB_FILE', MSG_INVALID, detail=raw.get('detail', ''))
    if raw == 'wrong_input_file':
        return error(400, 'LAB_NOT_FOUND', MSG_LAB_NOT_FOUND)
    if not isinstance(raw, dict) or not raw:
        return error(400, 'INVALID_LAB_FILE', MSG_INVALID)

    key, sv = next(iter(raw.items()))
    skipped = skipped_containers(path)
    integrity = {
        'emailMatch': sv.get('firstlevelzip') == {} and sv.get('secondlevelzip') == {},
        'watermarkMatch': bool(sv.get('actualwatermark')) and sv.get('actualwatermark') == sv.get('expectedwatermark') and not skipped,
        'skippedContainers': skipped,
    }

    # Logic chấm cũ của judge.py: parsing_grade quyết định AC/WA/CPY(12)/TOOL_CHEAT(14) cho từng goal.
    interp = parsing_grade.grade_interpreter_from_ws(raw)
    statuses = interp['Status'] if sv.get('grades') else []
    if not statuses:
        return error(400, 'INVALID_LAB_FILE', MSG_INVALID, integrity=integrity)

    tasks = [{'task': s['input_file'], 'completed': s['status'] == parsing_grade.STATUS_AC, 'status': s['status']}
             for s in statuses]
    completed = sum(t['completed'] for t in tasks)
    parts = key.split('.')
    return 200, {
        'email': '.'.join(parts[:-1]) if len(parts) > 1 else 'unknown',
        'lab_name': parts[-1],
        'score': round(10 * completed / len(tasks), 1),
        'completed_tasks': completed,
        'total_tasks': len(tasks),
        'tasks': tasks,
        'note': interp.get('Note', ''),
        'integrity': integrity,
    }


def grade(path, expected_student_id=None, expected_lab_id=None, cleanup=True):
    """Trả (http_status, payload). Lỗi lạ (kể cả SystemExit từ lõi) được ném lên cho app.py thành 500."""
    try:
        check_zip_limits(path)
        raw = instructor_grade.instructor_grade_lab(path, expected_student_id=expected_student_id,
                                                    expected_lab_id=expected_lab_id)
        return to_response(raw, path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        return error(400, 'INVALID_LAB_FILE', MSG_INVALID)
    except ZipLimitError:
        return error(413, 'FILE_TOO_LARGE', MSG_ZIP_BOMB)
    finally:
        if cleanup:
            key = os.path.splitext(os.path.basename(path))[0]
            instructor_grade.cleanup_submission_artifacts(BASE_DIR, key, tmp_root=os.path.dirname(path))
