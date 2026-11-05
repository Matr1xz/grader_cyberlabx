import datetime
import json
import logging
import optparse
import os
import shutil
import time
import traceback
import uuid
import urllib.error
import urllib.request


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'tmp', 'labs')
MAX_CONTENT_LENGTH = 1024 * 1024 * 1024
POLL_INTERVAL_SECONDS = 10
EMPTY_POLL_SECONDS = 1
STATUS_WF = 11
STATUS_WFN = 13

logger = logging.getLogger(__name__)
FAILED_LOG = os.path.join(BASE_DIR, 'failed_submissions.log')


def _log_failure(solution, reason, tb=''):
    # ponytail: plain append, no rotation; add logrotate if it grows large
    try:
        with open(FAILED_LOG, 'a') as fh:
            fh.write('[{}] id={} user={} question={} file={} reason={}\n{}\n'.format(
                datetime.datetime.now().isoformat(timespec='seconds'),
                solution.get('id'), solution.get('username'), solution.get('question_code'),
                solution.get('upload_file_name'), reason, tb))
    except Exception:
        pass


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def _normalize_server_url(server):
    return server.rstrip('/')


def _request_headers(judge_id, judge_key):
    headers = {
        'User-Agent': 'JUDGE ONLINE',
        'Connection': 'close',
        'Judge-Id': judge_id,
    }
    if judge_key is not None:
        headers['Judge-Key'] = judge_key
    return headers


def _http_get(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _http_post_json(url, payload, headers, timeout):
    body = json.dumps(payload).encode('utf-8')
    request_headers = dict(headers)
    request_headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=body, headers=request_headers, method='POST')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _safe_json_loads(text):
    try:
        return json.loads(text)
    except Exception:
        logger.error('Invalid server response: %s', text)
        return {}


def _submission_key(filename):
    return os.path.splitext(os.path.basename(filename or ''))[0]


def _cleanup_submission_workspace(workspace, filepath):
    if filepath:
        try:
            import instructor_grade

            submission_key = _submission_key(filepath)
            instructor_grade.cleanup_submission_artifacts(BASE_DIR, submission_key, tmp_root=workspace)
        except Exception:
            logger.debug('Failed to cleanup submission artifacts\n%s', traceback.format_exc())

    if workspace and os.path.isdir(workspace):
        try:
            shutil.rmtree(workspace)
        except Exception:
            logger.debug('Failed to remove workspace %s\n%s', workspace, traceback.format_exc())


def _download_submission_file(server, solution, headers):
    upload_file_name = os.path.basename(solution.get('upload_file_name') or '')
    if not upload_file_name:
        return None, None

    submission_id = solution.get('id')
    status_code, content = _http_get(
        '{}/api/submission/{}'.format(server, submission_id),
        headers,
        timeout=120,
    )
    if status_code == 404:
        logger.warning('Submission file not found for id=%s', submission_id)
        return None, None
    if status_code < 200 or status_code >= 300:
        raise RuntimeError('Failed to download submission file. HTTP status: %s' % status_code)

    if len(content) > MAX_CONTENT_LENGTH:
        raise ValueError('Downloaded file is too large')

    workspace = os.path.join(UPLOAD_FOLDER, str(submission_id or uuid.uuid4().hex))
    _ensure_dir(workspace)
    filepath = os.path.join(workspace, upload_file_name)
    with open(filepath, 'wb') as fh:
        fh.write(content)
    return workspace, filepath


def _wrong_filename_detail(username, question_code, upload_file_name):
    correct_filename = '{}.{}.lab'.format(username, question_code)
    return (
        '[{"output": "", "input_file": "Current upload file is '
        + str(upload_file_name)
        + '. Need filename like '
        + correct_filename
        + '" , "status": '
        + str(STATUS_WFN)
        + '}]'
    )


def _parse_grade_data(result):
    try:
        from parsing_grade import parsing_gradedata

        return parsing_gradedata(result)
    except Exception:
        logger.error('Failed to parse grade result\n%s', traceback.format_exc())
        return '[{"output": "", "input_file": "exception_occured", "status": ' + str(STATUS_WF) + '}]'


def _grade_solution(solution, filepath):
    username = solution.get('username', None)
    question_code = solution.get('question_code')
    upload_file_name = solution.get('upload_file_name')

    correct_filename = '{}.{}.lab'.format(username, question_code).lower()
    upload_file_name_lower = str(upload_file_name).lower()
    if correct_filename != upload_file_name_lower:
        logger.error('wrong_student_or_wrong_filename %s != %s', correct_filename, upload_file_name_lower)
        result = 'wrong_student_or_wrong_filename'
        detail = _wrong_filename_detail(username, question_code, upload_file_name)
    else:
        try:
            from instructor_grade import instructor_grade_lab

            result = instructor_grade_lab(
                filepath,
                expected_student_id=username,
                expected_lab_id=question_code,
            )
        except (Exception, SystemExit) as e:
            # grader code calls sys.exit() on bad lab config; SystemExit is not an Exception
            tb = traceback.format_exc()
            logger.error('An exception occurred for instructor_grade_lab(filepath)\n%s', tb)
            _log_failure(solution, repr(e), tb)
            result = 'An exception occurred'
        if isinstance(result, dict) and result.get('error') in ('invalid_submission_identity', 'invalid_submission_structure'):
            detail = '[{"output": "' + str(result.get('detail', 'Invalid submission.')) + '", "input_file": "' + result['error'] + '", "status": ' + str(STATUS_WFN) + '}]'
        else:
            detail = _parse_grade_data(result)



    return {
        'submission-id': solution.get('id'),
        'result': result,
        'score': 0,
        'detail': detail,
    }


def process_once(server, headers):
    try:
        status_code, content = _http_get('{}/api/submission'.format(server), headers, timeout=60)
        if status_code < 200 or status_code >= 300:
            raise RuntimeError('Failed to fetch submission. HTTP status: %s' % status_code)
    except Exception:
        logger.debug('Failed to fetch submission\n%s', traceback.format_exc())
        return False

    text = content.decode('utf-8', errors='replace')
    print(text)
    response_json = _safe_json_loads(text)
    if response_json.get('code', -1) != 0:
        return False

    solution = response_json.get('solution', {})
    if solution is None:
        return None  # queue empty (distinct from error -> shorter sleep)

    workspace = None
    filepath = None
    report = None
    try:
        workspace, filepath = _download_submission_file(server, solution, headers)
        if not filepath:
            return False
        report = _grade_solution(solution, filepath)
        print(report)
        _http_post_json(
            '{}/api/submission_report'.format(server),
            report,
            headers,
            timeout=60,
        )
        return True
    except (Exception, SystemExit) as e:
        logger.error('Failed to process submission id=%s\n%s', solution.get('id'), traceback.format_exc())
        _log_failure(solution, repr(e), traceback.format_exc())
        if report is not None:
            print(report)
        return False
    finally:
        _cleanup_submission_workspace(workspace, filepath)


def main():
    parser = optparse.OptionParser()
    parser.add_option('--id', help='id', type='string', default='PTIT')
    parser.add_option('--server', help='server', type='string', default='http://127.0.0.1:8000')
    parser.add_option('--key', help='key', type='string', default=None)
    parser.add_option('--once', action='store_true', default=False, help='Process at most one polling cycle.')
    opts, args = parser.parse_args()

    _ensure_dir(UPLOAD_FOLDER)
    judge_id = opts.id
    judge_key = opts.key
    server = _normalize_server_url(opts.server)
    headers = _request_headers(judge_id, judge_key)

    while True:
        date_license = datetime.date(2042, 5, 25)
        date_now = datetime.datetime.now().date()
        if date_now <= date_license:
            print(server)
            ok = False
            try:
                # graded one -> fetch next immediately; 1s when queue empty; 10s on error
                ok = process_once(server, headers)
                if ok and not opts.once:
                    continue
            except (Exception, SystemExit):
                logger.error('process_once crashed\n%s', traceback.format_exc())

        if opts.once:
            break
        time.sleep(EMPTY_POLL_SECONDS if ok is None else POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
