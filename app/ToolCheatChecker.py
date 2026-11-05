#!/usr/bin/env python3
"""
ToolCheatChecker.py
-------------------
Kiểm tra xem sinh viên có gian lận bằng cách dùng tool với đường dẫn
không hợp lệ (override) trong .bash_history không.

Logic:
  1. Đọc file `tool` trong thư mục lab (vd: .local/pregrade/gdblesson/tool)
     → lấy danh sách tên tool hợp lệ (mỗi dòng 1 tool, ví dụ: gdb)
  2. Với mỗi container của sinh viên, tìm file .bash_history
  3. Với mỗi dòng trong .bash_history, kiểm tra:
     - Có chứa tên tool không?
     - Nếu có, token gọi tool đó có phải đường dẫn tuyệt đối chuẩn
       (/usr/bin/<tool> hoặc /usr/local/bin/<tool>) không?
     - Nếu là lời gọi dạng ./tool, ../path/tool, /home/.../tool, ... → CHEATING

Trả về:
  (is_cheat: bool, detail: str)
  - is_cheat = True  → phát hiện gian lận, detail mô tả dòng vi phạm
  - is_cheat = False → bình thường
"""

import os
import re

# Các prefix đường dẫn hợp lệ cho một tool (không bị coi là cheating)
# Tool được gọi KHÔNG có path prefix cũng được coi là hợp lệ (dùng PATH mặc định)
ALLOWED_PATH_PREFIXES = (
    '/usr/bin/',
    '/usr/local/bin/',
    '/bin/',
    '/usr/sbin/',
    '/sbin/',
)


def _load_tool_list(lab_folder):
    """
    Đọc file `tool` trong lab_folder.
    Trả về list tên tool (lowercase, stripped), hoặc [] nếu không có file.
    """
    tool_file = os.path.join(lab_folder, 'tool')
    if not os.path.isfile(tool_file):
        return []
    tools = []
    with open(tool_file, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            name = line.strip().lower()
            if name and not name.startswith('#'):
                tools.append(name)
    return tools


def _is_cheating_invocation(token, tool_name):
    """
    Kiểm tra xem token (phần đầu của lệnh) có phải là lời gọi gian lận
    của tool_name không.

    Các trường hợp hợp lệ (KHÔNG cheating):
      - Chỉ là tên tool thuần tuý: "gdb"
      - Đường dẫn chuẩn: "/usr/bin/gdb", "/usr/local/bin/gdb", ...

    Các trường hợp CHEATING:
      - ./gdb, ../gdb, ../../usr/bin/gdb
      - /home/ubuntu/gdb, /tmp/gdb, /opt/gdb, /root/gdb, ...
      - Bất kỳ đường dẫn tuyệt đối nào KHÔNG nằm trong ALLOWED_PATH_PREFIXES
    """
    token_lower = token.lower()
    # Token phải kết thúc bằng /<tool_name> hoặc chính là <tool_name>
    base = os.path.basename(token_lower)
    if base != tool_name:
        return False  # không liên quan đến tool này

    # Không có path separator → gọi qua PATH → hợp lệ
    if '/' not in token and '\\' not in token:
        return False

    # Đường dẫn tuyệt đối → kiểm tra prefix hợp lệ
    if token.startswith('/'):
        for allowed in ALLOWED_PATH_PREFIXES:
            if token_lower.startswith(allowed):
                return False
        # Đường dẫn tuyệt đối nhưng không trong whitelist → CHEATING
        return True

    # Đường dẫn tương đối (bắt đầu bằng ./ hoặc ../ hoặc bất kỳ ký tự nào khác) → CHEATING
    return True


def check_tool_cheat(lab_folder, lab_extracted_dir, email_labname, container_list):
    """
    Hàm chính: kiểm tra gian lận tool cho một sinh viên cụ thể.

    Trả về:
        (cheated_tools: set, details: dict)
        - cheated_tools: tập tên tool bị gian lận, vd: {'netstat', 'gdb'}
        - details: dict tool -> chuỗi mô tả vi phạm
        - Nếu không có gian lận: (set(), {})
    """
    tool_list = _load_tool_list(lab_folder)
    if not tool_list:
        return set(), {}

    student_dir = os.path.join(lab_extracted_dir, email_labname)
    cheated_tools = set()
    details = {}

    for container in container_list:
        bash_history_path = os.path.join(student_dir, container, '.bash_history')
        if not os.path.isfile(bash_history_path):
            continue

        with open(bash_history_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        for lineno, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue

            tokens = line.split()
            skip_prefixes = {'sudo', 'env', 'time', 'nohup', 'strace', 'ltrace', 'nice', 'ionice'}
            for token in tokens:
                if token.lower() in skip_prefixes:
                    continue
                # token này là lệnh thật
                for tool_name in tool_list:
                    if _is_cheating_invocation(token, tool_name):
                        if tool_name not in cheated_tools:
                            cheated_tools.add(tool_name)
                            details[tool_name] = (
                                "container=%s, line=%d: '%s' "
                                "-> tool '%s' called with invalid path: '%s'"
                                % (container, lineno, line, tool_name, token)
                            )
                break  # chỉ kiểm tra token lệnh thật đầu tiên

    return cheated_tools, details
