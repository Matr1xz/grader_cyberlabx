"""Self-check API: python test_api.py [đường dẫn bài AC .lab]
Dùng TestClient (không cần mở cổng). Bài mặc định: ../B23DCAT314.ops-backup3.lab (AC, 10 điểm)."""
import io
import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import app

if __name__ == '__main__':  # Windows spawn re-import main module
    HERE = os.path.dirname(os.path.abspath(__file__))
    LAB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '..', 'B23DCAT314.ops-backup3.lab')
    NAME = os.path.basename(LAB)
    DATA = open(LAB, 'rb').read()


    def post(client, name=NAME, data=DATA, **form):
        return client.post('/grade', files={'file': (name, data)}, data=form)


    def code(r):
        return r.json()['error']['code']


    with TestClient(app.app) as c:
        h = c.get('/health').json()
        assert h['status'] == 'ok' and h['labs'] > 0, h

        r = post(c)
        assert r.status_code == 200, r.text
        j = r.json()
        print(j)
        assert j['score'] == 10.0 and j['completed_tasks'] == j['total_tasks'] == 3, j
        assert j['integrity'] == {'emailMatch': True, 'watermarkMatch': True, 'skippedContainers': []}, j

        # Đối chiếu danh tính tùy chọn (như judge.py cũ)
        assert post(c, student_id='B23DCAT314', lab_id='ops-backup3').status_code == 200
        assert code(post(c, student_id='B23DCAT999')) == 'IDENTITY_MISMATCH'

        # Lỗi có mã
        assert c.post('/grade').status_code == 400 and code(c.post('/grade')) == 'NO_FILE'
        assert code(post(c, name='x.txt')) == 'BAD_EXTENSION'
        assert code(post(c, data=b'not a zip')) == 'INVALID_LAB_FILE'
        assert code(post(c, name='B23DCAT314.no-such-lab.lab')) in {'LAB_NOT_FOUND', 'IDENTITY_MISMATCH'}

        # Zip bomb: một mục khai báo 300 MB (nén ~300 KB)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('B23DCAT314.ops-backup3=ops-backup3.x.student.zip', b'\0' * (300 * 1024 * 1024))
        r = post(c, data=buf.getvalue())
        assert r.status_code == 413 and code(r) == 'FILE_TOO_LARGE', r.text

        # Sửa watermark một container: container bị loại, integrity báo rõ
        src, buf = zipfile.ZipFile(io.BytesIO(DATA)), io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as out:
            for i in src.infolist():
                b = src.read(i)
                if 'destination' in i.filename:
                    inner_src, inner = zipfile.ZipFile(io.BytesIO(b)), io.BytesIO()
                    with zipfile.ZipFile(inner, 'w') as zz:
                        for k in inner_src.infolist():
                            zz.writestr(k, b'0' * 32 if k.filename == '.local/.watermark' else inner_src.read(k))
                    b = inner.getvalue()
                out.writestr(i, b)
        j = post(c, data=buf.getvalue()).json()
        print(j)
        assert j['integrity']['watermarkMatch'] is False
        assert j['integrity']['skippedContainers'] == ['ops-backup3.destination.student'], j

        # Song song: 4 bài cùng lúc đều ra đúng kết quả
        with ThreadPoolExecutor(4) as ex:
            results = list(ex.map(lambda _: post(c).json()['score'], range(4)))
        assert results == [10.0] * 4, results

        # Token
        app.TOKEN = 'secret'
        assert post(c).status_code == 401
        assert c.post('/grade', files={'file': (NAME, DATA)}, headers={'Authorization': 'Bearer secret'}).status_code == 200
        app.TOKEN = None

    print('OK')
